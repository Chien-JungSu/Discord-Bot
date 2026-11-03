"""第 7 週單元測試：驗證 services/spotify.py（全程離線，用假的 aiohttp session 模擬）。

涵蓋項目：
1. parse_spotify_url：網址、URI、?si= 參數、intl 路徑、無效輸入
2. 金鑰缺失優雅停用
3. token 快取（不重複取）與到期前自動更新
4. 分頁（歌單 100 首／頁，循環取 next）、截斷到上限、略過本地檔案
5. HTTP 429 重試（讀 Retry-After、最大次數、等待過久直接放棄）
6. 401／403／404 與 Premium 限制的明確訊息
7. 單曲、專輯、歌單欄位 track／item 相容

執行方式：python tools/test_spotify.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.spotify import (  # noqa: E402
    SpotifyAuthError,
    SpotifyClient,
    SpotifyDisabledError,
    SpotifyError,
    SpotifyForbiddenError,
    SpotifyNotFoundError,
    SpotifyRateLimitError,
    looks_like_spotify,
    parse_spotify_url,
)

PASS, FAIL = [], []


def check(cond, name):
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name)


# ---------- 假的 aiohttp 回應／session ----------
class FakeResp:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status, self._payload, self.headers, self._text = status, payload or {}, headers or {}, text

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """依序回傳預先排好的回應；記錄每次請求，方便驗證呼叫次數與 URL。"""

    closed = False

    def __init__(self, token_responses=None, api_responses=None):
        self.token_responses = list(token_responses or [FakeResp(200, {"access_token": "tok", "expires_in": 3600})])
        self.api_responses = list(api_responses or [])
        self.token_calls = 0
        self.api_calls = []

    def post(self, url, data=None, auth=None):
        self.token_calls += 1
        resp = self.token_responses.pop(0) if len(self.token_responses) > 1 else self.token_responses[0]
        return resp

    def get(self, url, params=None, headers=None):
        self.api_calls.append((url, params))
        return self.api_responses.pop(0)

    async def close(self):
        self.closed = True


def make_client(session, **kw):
    sleeps = []

    async def fake_sleep(sec):  # 不真的等待
        sleeps.append(sec)

    client = SpotifyClient("id", "secret", session=session, sleep=fake_sleep, **kw)
    return client, sleeps


def track_obj(i, **extra):
    d = {"type": "track", "name": f"歌{i}", "artists": [{"name": f"歌手{i}"}, {"name": "其他"}], "duration_ms": 200000}
    d.update(extra)
    return d


def run(coro):
    return asyncio.run(coro)


SID = "37i9dQZF1DXcBWIGoYBM5M"  # 22 碼格式範例


def test_parse():
    print("\n--- 測試 1: parse_spotify_url ---")
    check(parse_spotify_url(f"https://open.spotify.com/playlist/{SID}") == ("playlist", SID), "playlist 網址")
    check(parse_spotify_url(f"https://open.spotify.com/album/{SID}") == ("album", SID), "album 網址")
    check(parse_spotify_url(f"https://open.spotify.com/track/{SID}") == ("track", SID), "track 網址")
    check(parse_spotify_url(f"https://open.spotify.com/playlist/{SID}?si=abc123def") == ("playlist", SID), "容忍 ?si= 參數")
    check(parse_spotify_url(f"https://open.spotify.com/intl-zh/track/{SID}?si=x") == ("track", SID), "容忍 intl 語系路徑")
    check(parse_spotify_url(f"spotify:playlist:{SID}") == ("playlist", SID), "URI 格式")
    check(parse_spotify_url(f"  <https://open.spotify.com/album/{SID}>  ") == ("album", SID), "容忍空白與 <> 包裹")
    check(parse_spotify_url("https://open.spotify.com/artist/" + SID) is None, "不支援 artist → None")
    check(parse_spotify_url("https://open.spotify.com/playlist/short") is None, "ID 太短 → None")
    check(parse_spotify_url("hello world") is None, "一般文字 → None")
    check(parse_spotify_url("") is None and parse_spotify_url(None) is None, "空字串／None 不拋例外")
    check(looks_like_spotify("https://open.spotify.com/artist/xxx") is True, "格式不完整的 Spotify 連結仍判定為 Spotify")
    check(looks_like_spotify("夜に駆ける YOASOBI") is False, "一般關鍵字不是 Spotify")


def test_disabled():
    print("\n--- 測試 2: 金鑰缺失 ---")
    c = SpotifyClient("", "")
    check(c.enabled is False, "缺少金鑰 enabled=False")
    try:
        run(c.fetch_tracks("track", SID, 10))
        check(False, "缺金鑰應拋 SpotifyDisabledError")
    except SpotifyDisabledError as e:
        check("尚未設定" in e.message, "缺金鑰回傳友善訊息")
    check(SpotifyClient("a", "b").enabled is True, "金鑰齊全 enabled=True")


def test_token_cache():
    print("\n--- 測試 3: token 快取 ---")

    async def go():
        s = FakeSession(api_responses=[FakeResp(200, track_obj(1)), FakeResp(200, track_obj(2))])
        c, _ = make_client(s)
        await c.fetch_tracks("track", SID, 10)
        await c.fetch_tracks("track", SID, 10)
        return s.token_calls

    check(run(go()) == 1, "連續兩次請求只取一次 token（快取生效）")

    async def go_expire():
        s = FakeSession(api_responses=[FakeResp(200, track_obj(1)), FakeResp(200, track_obj(2))])
        c, _ = make_client(s)
        await c.fetch_tracks("track", SID, 10)
        c._token_expires_at = 0  # 模擬已到期
        await c.fetch_tracks("track", SID, 10)
        return s.token_calls

    check(run(go_expire()) == 2, "token 到期後自動重取")

    async def go_401():
        s = FakeSession(api_responses=[FakeResp(401), FakeResp(200, track_obj(1))])
        c, _ = make_client(s)
        r = await c.fetch_tracks("track", SID, 10)
        return s.token_calls, len(r.tracks)

    calls, n = run(go_401())
    check(calls == 2 and n == 1, "API 回 401 時強制重取 token 並重試一次")


def test_pagination():
    print("\n--- 測試 4: 分頁與截斷 ---")
    page1 = {"items": [{"track": track_obj(i)} for i in range(100)], "next": "https://api.spotify.com/v1/next2"}
    page2 = {"items": [{"track": track_obj(i)} for i in range(100, 150)], "next": None}

    async def go(limit):
        s = FakeSession(api_responses=[FakeResp(200, page1), FakeResp(200, page2)])
        c, _ = make_client(s)
        return await c.fetch_tracks("playlist", SID, limit), s

    r, s = run(go(200))
    check(len(r.tracks) == 150 and not r.truncated, "兩頁共 150 首全部取回")
    check(len(s.api_calls) == 2 and s.api_calls[1][0].endswith("/next2"), "循環跟隨 next 連結")
    check(s.api_calls[1][1] is None, "後續頁不重複帶 params")
    check(r.tracks[0].title == "歌0" and r.tracks[0].artist == "歌手0", "只取第一位歌手")
    check(r.tracks[0].search_query() == "歌0 歌手0", "搜尋字串格式為「曲名 第一位歌手」")
    check([t.title for t in r.tracks][:3] == ["歌0", "歌1", "歌2"], "保持原順序")

    r2, s2 = run(go(120))
    check(len(r2.tracks) == 120 and r2.truncated, "超過上限時截斷並標記 truncated")

    mixed = {"items": [{"track": track_obj(1)}, {"track": None}, {"track": track_obj(2, is_local=True)},
                       {"track": {"type": "episode", "name": "Podcast"}}, {"item": track_obj(3)}], "next": None}

    async def go_mixed():
        s = FakeSession(api_responses=[FakeResp(200, mixed)])
        c, _ = make_client(s)
        return await c.fetch_tracks("playlist", SID, 50)

    r3 = run(go_mixed())
    check([t.title for t in r3.tracks] == ["歌1", "歌3"], "略過 None／本地檔案／Podcast，並相容 item 欄位")
    check(r3.skipped == 3, "略過數量正確 (3)")

    async def go_album():
        s = FakeSession(api_responses=[FakeResp(200, {"items": [track_obj(1), track_obj(2)], "next": None})])
        c, _ = make_client(s)
        return await c.fetch_tracks("album", SID, 50), s

    ra, sa = run(go_album())
    check(len(ra.tracks) == 2 and "/albums/" in sa.api_calls[0][0], "專輯曲目讀取")

    async def go_track():
        s = FakeSession(api_responses=[FakeResp(200, track_obj(9))])
        c, _ = make_client(s)
        return await c.fetch_tracks("track", SID, 50)

    check(run(go_track()).tracks[0].title == "歌9", "單曲讀取")


def test_rate_limit():
    print("\n--- 測試 5: 429 重試 ---")

    async def go_ok():
        s = FakeSession(api_responses=[FakeResp(429, headers={"Retry-After": "2"}), FakeResp(200, track_obj(1))])
        c, sleeps = make_client(s)
        r = await c.fetch_tracks("track", SID, 10)
        return len(r.tracks), sleeps

    n, sleeps = run(go_ok())
    check(n == 1, "429 後重試成功")
    check(sleeps == [2.0], f"依 Retry-After 等待 2 秒 (實際 {sleeps})")

    async def go_exhaust():
        s = FakeSession(api_responses=[FakeResp(429, headers={"Retry-After": "1"}) for _ in range(10)])
        c, sleeps = make_client(s, max_retries=3)
        try:
            await c.fetch_tracks("track", SID, 10)
        except SpotifyRateLimitError:
            return len(sleeps), len(s.api_calls)
        return None

    res = run(go_exhaust())
    check(res == (3, 4), f"超過最大重試次數後放棄（等待 3 次、共請求 4 次，實際 {res}）")

    async def go_long():
        s = FakeSession(api_responses=[FakeResp(429, headers={"Retry-After": "3600"})])
        c, sleeps = make_client(s)
        try:
            await c.fetch_tracks("track", SID, 10)
        except SpotifyRateLimitError:
            return sleeps
        return None

    check(run(go_long()) == [], "Retry-After 過長時不等待、直接回報")

    async def go_noheader():
        s = FakeSession(api_responses=[FakeResp(429), FakeResp(200, track_obj(1))])
        c, sleeps = make_client(s)
        await c.fetch_tracks("track", SID, 10)
        return sleeps

    check(run(go_noheader()) == [1.0], "缺少 Retry-After 時預設等 1 秒")


def test_errors():
    print("\n--- 測試 6: 401／403／404 與 Premium 限制 ---")

    async def expect(api, token=None, exc=SpotifyError):
        s = FakeSession(token_responses=token, api_responses=api)
        c, _ = make_client(s)
        try:
            await c.fetch_tracks("playlist", SID, 10)
        except exc as e:
            return e
        return None

    e = run(expect([FakeResp(404)], exc=SpotifyNotFoundError))
    check(isinstance(e, SpotifyNotFoundError) and "找不到" in e.message, "404 → 找不到內容")
    e = run(expect([FakeResp(403, text="forbidden")], exc=SpotifyForbiddenError))
    check(isinstance(e, SpotifyForbiddenError) and "私人歌單" in e.message, "403 → 私人歌單／平台限制訊息")
    e = run(expect([FakeResp(403, text="Active premium subscription required for the owner of the app.")], exc=SpotifyAuthError))
    check(isinstance(e, SpotifyAuthError) and "Premium" in e.message, "API 403 含 premium → Premium 限制訊息")
    e = run(expect([], token=[FakeResp(401)], exc=SpotifyAuthError))
    check(isinstance(e, SpotifyAuthError) and "金鑰" in e.message, "token 端點 401 → 金鑰錯誤訊息")
    e = run(expect([], token=[FakeResp(403, text="premium required")], exc=SpotifyAuthError))
    check(isinstance(e, SpotifyAuthError) and "Premium" in e.message, "token 端點 403 premium → Premium 訊息")
    e = run(expect([FakeResp(500)]))
    check(isinstance(e, SpotifyError) and "500" in e.message, "5xx → 暫時異常訊息")

    async def go_local_only():
        s = FakeSession(api_responses=[FakeResp(200, track_obj(1, is_local=True))])
        c, _ = make_client(s)
        try:
            await c.fetch_tracks("track", SID, 10)
        except SpotifyError as e:
            return e.message
        return None

    check("無法播放" in (run(go_local_only()) or ""), "單曲為本地檔案 → 友善訊息")


def main():
    print("=== 開始執行第 7 週 Spotify 服務單元測試 ===")
    test_parse()
    test_disabled()
    test_token_cache()
    test_pagination()
    test_rate_limit()
    test_errors()
    total = len(PASS) + len(FAIL)
    print(f"\n測試統計：共 {total} 項，{len(PASS)} 通過，{len(FAIL)} 失敗")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()

