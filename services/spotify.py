"""第 7 週新增：Spotify 公開資料讀取服務（Client Credentials Flow，全程 aiohttp）。

設計重點（方便寫開發日誌）：
- 不用 spotipy：spotipy 是同步套件（底層 requests），在 discord.py 事件迴圈裡會阻塞 Gateway
  心跳；若包 run_in_executor 又多一層執行緒，在 0.1 CPU / 512MB 的環境很浪費。我們只需要
  「取 token + 讀三種端點」，自己用 aiohttp 寫不到兩百行，還能完整控制 429 與逾時。
- 整個服務共用一個 ClientSession（連線池重用，避免每次請求都重新 TLS 握手）。
- access token 快取，到期前 TOKEN_REFRESH_MARGIN 秒就更新，並用 Lock 避免多個協程同時重取。
- 只保留「曲名、歌手、時長」三個欄位：大型歌單的完整 JSON 一頁就上百 KB，
  逐頁解析、取完欄位就丟棄，不讓原始 JSON 長期留在記憶體。
- 缺少金鑰時 enabled=False，呼叫端據此提示使用者；絕不讓 Bot 啟動失敗。

重要的平台限制（2026 年 2 月 Spotify 政策調整，請以官方文件為準）：
- Development mode 的 App 擁有者需有 Premium，否則所有 API 請求都會被拒絕。
- 歌單曲目端點只對「擁有者／協作者的使用者授權」開放，Client Credentials 可能得到 403。
  本模組遇到這些情況只會回報明確訊息，不會嘗試任何繞過手段。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

# ---------- 可調常數 ----------
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

# token 到期前多少秒就視為過期並重取，避免「剛好在請求途中過期」。
TOKEN_REFRESH_MARGIN = 60.0

# 遇到 HTTP 429 時最多重試幾次；超過就放棄並回報，避免無限等待卡住背景任務。
MAX_RETRIES = 3

# Retry-After 若超過這個秒數就不等了（直接回報），否則背景任務可能被卡上好幾分鐘。
MAX_RETRY_AFTER = 30.0

# 單次 HTTP 請求逾時（秒）。
REQUEST_TIMEOUT = 10.0

# Spotify 各端點每頁上限：歌單 100、專輯曲目 50。
PLAYLIST_PAGE_SIZE = 100
ALBUM_PAGE_SIZE = 50


# ---------- 例外：message 屬性都是可以直接給使用者看的繁體中文訊息 ----------
class SpotifyError(Exception):
    """所有 Spotify 相關錯誤的基底類別；message 可直接顯示給使用者。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SpotifyDisabledError(SpotifyError):
    """尚未設定 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET。"""


class SpotifyAuthError(SpotifyError):
    """金鑰錯誤、被撤銷，或 App 擁有者沒有 Premium。"""


class SpotifyNotFoundError(SpotifyError):
    """歌單／專輯／單曲不存在（404）。"""


class SpotifyForbiddenError(SpotifyError):
    """私人歌單，或平台限制 Client Credentials 讀取（401／403）。"""


class SpotifyRateLimitError(SpotifyError):
    """重試多次後仍被 429 限流。"""


# ---------- 資料結構 ----------
@dataclass(slots=True)
class SpotifyTrack:
    """只保留轉譯搜尋需要的欄位，避免把完整 JSON 留在記憶體。"""

    title: str
    artist: str
    duration_ms: int

    def search_query(self) -> str:
        """搜尋字串格式固定為「曲名 第一位歌手」。"""
        return f"{self.title} {self.artist}".strip()


@dataclass(slots=True)
class SpotifyFetchResult:
    """fetch_tracks 的回傳值。"""

    kind: str                 # track / album / playlist
    tracks: list[SpotifyTrack]
    truncated: bool           # 是否因超過上限而被截斷
    skipped: int              # 本地檔案／Podcast／已下架等無法轉譯而略過的數量


# ---------- 連結解析（純函式，不依賴網路，方便單元測試） ----------
# 網址形式：https://open.spotify.com/[intl-xx/]playlist/<id>[?si=...]
_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-zA-Z-]+/)?(?:embed/)?"
    r"(?P<kind>playlist|album|track)/(?P<id>[A-Za-z0-9]{22})(?:[/?#].*)?$"
)
# URI 形式：spotify:playlist:<id>
_URI_RE = re.compile(r"^spotify:(?P<kind>playlist|album|track):(?P<id>[A-Za-z0-9]{22})$")


def parse_spotify_url(text: str | None) -> tuple[str, str] | None:
    """把 Spotify 連結或 URI 解析成 (種類, ID)；無法辨識回傳 None（不拋例外）。

    - 容忍前後空白、網址後方的 ?si=... 追蹤參數、intl-xx 語系路徑。
    - 只接受 playlist / album / track 三種。
    """
    if not text or not isinstance(text, str):
        return None
    candidate = text.strip().strip("<>")  # Discord 貼連結常會用 <> 包起來避免預覽
    for pattern in (_URL_RE, _URI_RE):
        m = pattern.match(candidate)
        if m:
            return m.group("kind"), m.group("id")
    return None


def looks_like_spotify(text: str | None) -> bool:
    """粗略判斷輸入是不是 Spotify 連結（即使格式不完整）。

    用途：/music_play 要決定「走 Spotify 流程」還是「走一般搜尋」。
    格式錯誤的 Spotify 連結仍要走 Spotify 流程，才能給使用者友善錯誤，
    而不是被當成關鍵字丟去搜尋。
    """
    if not text or not isinstance(text, str):
        return False
    lowered = text.strip().strip("<>").lower()
    return lowered.startswith("spotify:") or "open.spotify.com" in lowered


# ---------- 客戶端 ----------
class SpotifyClient:
    """Client Credentials Flow 的精簡 Spotify 客戶端。

    sleep / session 可注入，方便測試時不用真的等待、不用真的連網。
    """

    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        session: aiohttp.ClientSession | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = MAX_RETRIES,
    ):
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self._session = session
        self._owns_session = session is None
        self._sleep = sleep
        self._max_retries = max_retries

        self._token: str | None = None
        self._token_expires_at: float = 0.0   # time.monotonic() 的時間點
        self._token_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """金鑰是否齊全；False 時 Spotify 功能優雅停用。"""
        return bool(self._client_id and self._client_secret)

    # ----- session 生命週期 -----
    def _get_session(self) -> aiohttp.ClientSession:
        # 延遲建立：必須在事件迴圈內建立 ClientSession，所以不放在 __init__。
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        """Cog 卸載時呼叫，只關閉自己建立的 session。"""
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ----- token 快取 -----
    async def _get_token(self, force_refresh: bool = False) -> str:
        if not self.enabled:
            raise SpotifyDisabledError("Spotify 功能尚未啟用：管理員尚未設定 Spotify 金鑰。")

        async with self._token_lock:
            # 進鎖後再檢查一次：等鎖的期間可能已被別的協程更新過（double-checked）
            if (
                not force_refresh
                and self._token
                and time.monotonic() < self._token_expires_at - TOKEN_REFRESH_MARGIN
            ):
                return self._token

            session = self._get_session()
            auth = aiohttp.BasicAuth(self._client_id, self._client_secret)
            try:
                async with session.post(
                    SPOTIFY_TOKEN_URL, data={"grant_type": "client_credentials"}, auth=auth
                ) as resp:
                    if resp.status in (400, 401):
                        raise SpotifyAuthError("Spotify 金鑰驗證失敗，請管理員檢查 Client ID / Secret。")
                    if resp.status == 403:
                        raise SpotifyAuthError(self._auth_403_message(await self._safe_text(resp)))
                    if resp.status != 200:
                        raise SpotifyError(f"Spotify 驗證服務暫時異常（HTTP {resp.status}），請稍後再試。")
                    payload = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise SpotifyError("連線 Spotify 驗證服務失敗，請稍後再試。") from e

            self._token = payload.get("access_token")
            expires_in = float(payload.get("expires_in", 3600))
            self._token_expires_at = time.monotonic() + expires_in
            if not self._token:
                raise SpotifyError("Spotify 驗證回應格式異常。")
            return self._token

    @staticmethod
    def _auth_403_message(body: str) -> str:
        # 平台限制：App 擁有者沒有 Premium 時，錯誤訊息會提到 premium
        if "premium" in body.lower():
            return "Spotify 拒絕了請求：Spotify 政策要求開發者 App 的擁有者必須是 Premium 訂閱者。"
        return "Spotify 拒絕了請求（403），可能是平台存取限制。"

    @staticmethod
    async def _safe_text(resp: aiohttp.ClientResponse) -> str:
        try:
            return await resp.text()
        except Exception:
            return ""

    # ----- 帶重試的 GET -----
    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET 並回傳 JSON；處理 429 重試、token 過期重取，以及 401/403/404 的明確訊息。"""
        token = await self._get_token()
        refreshed = False
        attempt = 0

        while True:
            session = self._get_session()
            try:
                async with session.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}"}
                ) as resp:
                    status = resp.status

                    if status == 200:
                        return await resp.json(content_type=None)

                    if status == 429:
                        attempt += 1
                        retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
                        if attempt > self._max_retries:
                            raise SpotifyRateLimitError("Spotify 目前請求過於頻繁，請稍後再試。")
                        if retry_after > MAX_RETRY_AFTER:
                            # 要等太久就不卡住背景任務，直接回報
                            raise SpotifyRateLimitError(
                                f"Spotify 要求等待 {int(retry_after)} 秒，請稍後再試。"
                            )
                        print(f">>> Spotify 429，{retry_after:.1f} 秒後重試（第 {attempt}/{self._max_retries} 次）")
                        await self._sleep(retry_after)
                        continue

                    if status == 401 and not refreshed:
                        # token 可能被提前撤銷：強制重取一次，再失敗才回報
                        refreshed = True
                        token = await self._get_token(force_refresh=True)
                        continue

                    if status == 404:
                        raise SpotifyNotFoundError("找不到這個 Spotify 內容，可能已刪除或連結有誤。")

                    if status in (401, 403):
                        body = await self._safe_text(resp)
                        if "premium" in body.lower():
                            raise SpotifyAuthError(self._auth_403_message(body))
                        raise SpotifyForbiddenError(
                            "無法讀取這個 Spotify 內容：可能是私人歌單，"
                            "或 Spotify 平台限制第三方程式讀取此類內容。"
                        )

                    raise SpotifyError(f"Spotify 服務暫時異常（HTTP {status}），請稍後再試。")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise SpotifyError("連線 Spotify 失敗，請稍後再試。") from e

    @staticmethod
    def _parse_retry_after(value: str | None) -> float:
        try:
            return max(float(value), 0.0) if value is not None else 1.0
        except (TypeError, ValueError):
            return 1.0

    # ----- 曲目轉換 -----
    @staticmethod
    def _to_track(obj: dict[str, Any] | None) -> SpotifyTrack | None:
        """把 Spotify 的曲目物件縮成 SpotifyTrack；不是一般曲目就回傳 None。"""
        if not obj or obj.get("type", "track") != "track" or obj.get("is_local"):
            return None
        title = (obj.get("name") or "").strip()
        if not title:
            return None
        artists = obj.get("artists") or []
        artist = (artists[0].get("name") or "").strip() if artists else ""
        return SpotifyTrack(title=title, artist=artist, duration_ms=int(obj.get("duration_ms") or 0))

    # ----- 對外介面 -----
    async def fetch_tracks(self, kind: str, spotify_id: str, limit: int) -> SpotifyFetchResult:
        """依種類取回曲目清單（最多 limit 首），逐頁處理並只保留必要欄位。"""
        tracks: list[SpotifyTrack] = []
        skipped = 0
        truncated = False

        if kind == "track":
            data = await self._get_json(f"{SPOTIFY_API_BASE}/tracks/{spotify_id}")
            t = self._to_track(data)
            if t is None:
                raise SpotifyError("這個 Spotify 單曲無法播放（可能是本地檔案或已下架）。")
            return SpotifyFetchResult("track", [t], False, 0)

        if kind == "album":
            url = f"{SPOTIFY_API_BASE}/albums/{spotify_id}/tracks"
            params: dict[str, Any] | None = {"limit": ALBUM_PAGE_SIZE}
        elif kind == "playlist":
            # 注意：官方新版文件可能改名為 /items，且項目欄位 track 改為 item，
            # 所以解析時兩種欄位都接受；端點若被平台限制會走 403 明確提示。
            url = f"{SPOTIFY_API_BASE}/playlists/{spotify_id}/tracks"
            params = {"limit": PLAYLIST_PAGE_SIZE}
        else:
            raise SpotifyError("不支援的 Spotify 連結類型。")

        next_url: str | None = url
        while next_url:
            page = await self._get_json(next_url, params)
            params = None  # next 連結已內含 query 參數，之後不能重複帶
            for item in page.get("items") or []:
                # 歌單項目包一層 track/item；專輯項目本身就是曲目
                raw = item.get("track") or item.get("item") if kind == "playlist" else item
                t = self._to_track(raw)
                if t is None:
                    skipped += 1
                    continue
                if len(tracks) >= limit:
                    truncated = True
                    break
                tracks.append(t)
            if truncated:
                break
            next_url = page.get("next")
            del page  # 盡早釋放這一頁的原始 JSON

        return SpotifyFetchResult(kind, tracks, truncated, skipped)

