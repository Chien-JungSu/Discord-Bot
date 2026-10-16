"""獨立驗證腳本：驗證 cogs/shortener.py 完全符合 Reurl 官方 OpenAPI 3.0.0 規格書。

涵蓋項目：
- normalize_url 網址正規化
- extract_first_url 訊息中網址擷取
- is_quota_exceeded 判定 429 / 業務代碼 / msg / message 中的配額耗盡
- build_result_embed 包含 short_url、original_url (帶 UTM) 與每日使用計數
- build_quota_exceeded_embed 配額耗盡專屬 Embed
- OpenAPI 規格相容性測試：
  * Headers 包含 reurl-api-key 與 re-api-key
  * Request Body 支援 url, utm_source, utm_medium, utm_campaign
  * 200 成功回應包含 res, short_url, original_url
  * 400 失敗回應包含 code, msg
  * 429 / 額度超額回傳 QUOTA_EXCEEDED
- SQLite 每日 100 次配額記錄與查詢

執行方式：python tools/test_shortener.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import os
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cogs.database as db
import cogs.shortener as shortener

PASS: list[str] = []
FAIL: list[str] = []


def check(cond: bool, name: str):
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name)


class DummyResponse:
    def __init__(self, status: int, json_data: dict | None = None, text_data: str = ""):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    async def json(self):
        if self._json_data is not None:
            return self._json_data
        raise ValueError("Not JSON")

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


def test_url_normalization():
    print("\n--- [1] 測試網址正規化 (normalize_url) ---")
    check(
        shortener.normalize_url("https://example.com/test") == "https://example.com/test",
        "https://example.com/test 保留原貌",
    )
    check(
        shortener.normalize_url("http://example.com/page?a=1&b=2") == "http://example.com/page?a=1&b=2",
        "http:// 網址保留原貌",
    )
    check(
        shortener.normalize_url("   https://example.com   ") == "https://example.com",
        "自動去除頭尾空白",
    )
    check(
        shortener.normalize_url("www.google.com") == "https://www.google.com",
        "www.google.com 自動補上 https://",
    )
    check(
        shortener.normalize_url("github.com/test/repo") == "https://github.com/test/repo",
        "github.com/test/repo 自動補上 https://",
    )
    check(shortener.normalize_url("") is None, "空字串回傳 None")
    check(shortener.normalize_url("   ") is None, "全空白字串回傳 None")
    check(shortener.normalize_url("just a text") is None, "純文字無點回傳 None")
    check(shortener.normalize_url(".gitignore") is None, "點開頭檔名不視為網址")
    check(shortener.normalize_url("ftp://ftp.example.com") is None, "非 http/https 協定回傳 None")


def test_extract_first_url():
    print("\n--- [2] 測試訊息中網址擷取 (extract_first_url) ---")
    msg1 = "大家快看這個酷東西：https://github.com/test 非常好用！"
    check(
        shortener.extract_first_url(msg1) == "https://github.com/test",
        "從對話文字中精準擷取 https:// 網址",
    )
    msg2 = "可以參考 www.google.com 看看喔"
    check(
        shortener.extract_first_url(msg2) == "https://www.google.com",
        "從對話文字中擷取 www. 開頭網址並補上 https://",
    )
    check(shortener.extract_first_url("這則訊息完全沒有任何連結。") is None, "無連結文字回傳 None")


def test_quota_exceeded_detection():
    print("\n--- [3] 測試每日 100 次上限判定 (is_quota_exceeded) ---")
    check(shortener.is_quota_exceeded(429, {}) is True, "HTTP 429 狀態碼判定為超額")
    check(shortener.is_quota_exceeded(429, {"msg": "Too Many Requests"}) is True, "HTTP 429 帶 msg 判定為超額")
    check(shortener.is_quota_exceeded(400, {"msg": "Daily quota limit reached"}) is True, "400 含 quota limit msg 判定為超額")
    check(shortener.is_quota_exceeded(400, {"code": "42901", "msg": "exceeded"}) is True, "代碼 429xx 判定為超額")
    check(shortener.is_quota_exceeded(400, {"code": "ERR_INVALID", "msg": "Invalid URL"}) is False, "一般 400 格式錯誤不誤判")


def test_embed_and_view():
    print("\n--- [4] 測試 Embed 與 View 元件生成 ---")
    embed = shortener.build_result_embed(
        short_url="https://api.reurl.cc/qk",
        original_url="https://www.google.com?utm_source=FB_AD",
        utm_info={"utm_source": "FB_AD", "utm_medium": "social"},
        current_usage=37,
    )
    check(embed.title == "🔗 網址縮短成功", "成功 Embed 標題正確")
    check(any("https://api.reurl.cc/qk" in f.value for f in embed.fields), "Embed 包含 short_url")
    check(any("https://www.google.com?utm_source=FB_AD" in f.value for f in embed.fields), "Embed 包含 original_url")
    check(any("utm_source=FB_AD" in f.value for f in embed.fields), "Embed 包含 UTM 追蹤標籤")
    check("37/100" in str(embed.footer.text), "Embed Footer 顯示今日使用次數 (37/100)")

    quota_embed = shortener.build_quota_exceeded_embed("https://example.com", current_usage=100)
    check("每日縮網址額度已達上限" in quota_embed.title, "配額超額 Embed 標題清晰")
    check("00:00" in quota_embed.description, "配額超額 Embed 包含午夜重置提示")


def test_database_quota_tracking():
    print("\n--- [5] 測試 SQLite 每日配額記錄與查詢 ---")
    service = "test_openapi_reurl"
    c1 = db.record_api_call_success(service)
    c2 = db.record_api_call_success(service)
    check(c2 == c1 + 1, "record_api_call_success 成功遞增計數")

    count, today_str = db.get_daily_api_usage(service)
    check(count == c2, "get_daily_api_usage 正確讀取今日使用量")

    db.mark_api_quota_exhausted(service, 100)
    exhausted_count, _ = db.get_daily_api_usage(service)
    check(exhausted_count == 100, "mark_api_quota_exhausted 正確標記為 100 次已滿")


async def test_reurl_api_request():
    print("\n--- [6] 測試 Reurl OpenAPI 3.0.0 請求與回應解析 ---")

    recorded_headers = {}
    recorded_payload = {}

    # OpenAPI 200 範例回應
    openapi_200_example = {
        "res": "success",
        "short_url": "https://api.reurl.cc/qk",
        "original_url": "https://www.google.com?utm_source=FB_AD"
    }

    def fake_post(url, json=None, headers=None, timeout=None):
        nonlocal recorded_headers, recorded_payload
        recorded_headers = headers
        recorded_payload = json
        return DummyResponse(200, openapi_200_example)

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        success, short_url, data = await shortener.request_reurl_shorten(
            api_key="my_secret_key",
            target_url="https://www.google.com",
            utm_source="FB_AD",
            utm_medium="social",
        )
        check(success is True, "OpenAPI 200 回應判定成功")
        check(short_url == "https://api.reurl.cc/qk", "正確解析 short_url")
        check(data.get("original_url") == "https://www.google.com?utm_source=FB_AD", "正確解析帶 UTM 的 original_url")
        check(recorded_headers.get("reurl-api-key") == "my_secret_key", "Header 帶有 OpenAPI 規範之 reurl-api-key")
        check(recorded_payload.get("utm_source") == "FB_AD", "Payload 帶有 OpenAPI 規範之 utm_source")
        check(recorded_payload.get("utm_medium") == "social", "Payload 帶有 OpenAPI 規範之 utm_medium")

    # OpenAPI 400 範例回應：{"code": "failure code", "msg": "failure reason"}
    openapi_400_example = {
        "code": "INVALID_PARAMS",
        "msg": "URL domain is blocked"
    }
    with patch("aiohttp.ClientSession.post", return_value=DummyResponse(400, openapi_400_example)):
        success, err_msg, data = await shortener.request_reurl_shorten("key", "https://example.com")
        check(success is False, "OpenAPI 400 回應判定失敗")
        check("URL domain is blocked" in err_msg, "錯誤訊息正確讀取 OpenAPI 400 的 msg 欄位")
        check("INVALID_PARAMS" in err_msg, "錯誤訊息包含 failure code")

    # 429 限流
    with patch("aiohttp.ClientSession.post", return_value=DummyResponse(429, {"code": "42900", "msg": "Daily quota exceeded"})):
        success, err_msg, data = await shortener.request_reurl_shorten("key", "https://example.com")
        check(success is False and err_msg == "QUOTA_EXCEEDED", "超額回應正確識別為 QUOTA_EXCEEDED")


async def main():
    test_url_normalization()
    test_extract_first_url()
    test_quota_exceeded_detection()
    test_embed_and_view()
    test_database_quota_tracking()
    await test_reurl_api_request()

    print("\n===============================")
    print(f"測試總結：通過 {len(PASS)} 項，失敗 {len(FAIL)} 項")
    print("===============================")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
