from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import tempfile
import time
import traceback
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

from cogs.sanitize import redact_secrets

from services.spotify import (
    SpotifyClient,
    SpotifyError,
    SpotifyTrack,
    looks_like_spotify,
    parse_spotify_url,
)

if TYPE_CHECKING:
    import wavelink
    import wavelink as wavelink_module
else:
    try:
        import wavelink
        wavelink_module = wavelink
    except ImportError:  # 尚未安裝 wavelink 時，讓其他 Cog 仍可正常運作
        wavelink = None
        wavelink_module = None

# Lavalink 連線逾時秒數，可用環境變數覆寫，避免連線卡住拖垮整個 bot 啟動流程
LAVALINK_CONNECT_TIMEOUT = float(os.getenv('LAVALINK_CONNECT_TIMEOUT', '15'))

# 語音頻道裡沒有真人成員（只剩機器人自己）超過這個秒數，就自動離開並清空佇列。
# 可用環境變數覆寫；設計成有一段緩衝時間，避免使用者只是暫時斷線重連或切頻道
# 晃一下，就把佇列清空、把機器人踢出去。
EMPTY_VOICE_CHANNEL_TIMEOUT = float(os.getenv('EMPTY_VOICE_CHANNEL_TIMEOUT', '60'))

# 多久檢查一次 Lavalink 節點的連線狀態（秒），可用環境變數覆寫。
NODE_HEALTH_CHECK_INTERVAL = float(os.getenv('NODE_HEALTH_CHECK_INTERVAL', '30'))

# 連線失敗後對節點 /version 做一次快速探測的逾時秒數（診斷用；刻意比連線逾時短，避免失敗回報又拖很久）。
LAVALINK_PROBE_TIMEOUT = 5.0

# Pool 為空時自動重連的冷卻秒數，可用環境變數覆寫。
# 沒有冷卻的話，health check 每 NODE_HEALTH_CHECK_INTERVAL 秒就會重連一次，節點長時間故障時會變成洗版式重試。
LAVALINK_RECONNECT_COOLDOWN = float(os.getenv('LAVALINK_RECONNECT_COOLDOWN', '60'))

# 第 6 週新增：播放面板進度條定時更新間隔（秒，集中常數設定，至少 5 秒防 API rate limit）
PROGRESS_UPDATE_INTERVAL = float(os.getenv('PROGRESS_UPDATE_INTERVAL', '5.0'))

# 控制面板防洗版自動置底（Sticky Panel）防抖延遲與最小冷卻秒數：
# - PANEL_BUMP_DELAY：當點歌頻道有普通訊息把控制面板往上刷時，等待頻道靜止的防抖延遲秒數（預設 6 秒）。
# - PANEL_BUMP_COOLDOWN：兩次置底重發之間的最短冷卻間隔（預設 12 秒），避免短時間內重複觸發導致舊面板來不及清除或觸發 Discord 429。
PANEL_BUMP_DELAY = float(os.getenv('PANEL_BUMP_DELAY', '6.0'))
PANEL_BUMP_COOLDOWN = float(os.getenv('PANEL_BUMP_COOLDOWN', '12.0'))

# Lavalink Session 失效（REST 回傳 404 /v4/sessions/...）時，強制重連 WebSocket 後等待新 Session 就緒的逾時秒數；
# 超過這個時間仍未收到 node_ready，就允許下一次偵測再觸發一次重置。
SESSION_RESET_TIMEOUT = float(os.getenv('SESSION_RESET_TIMEOUT', '30'))

# ---------- 第 7 週新增：Spotify 批次匯入設定（集中常數，方便依機器效能調整） ----------
# 同時進行的 Lavalink 搜尋數量。免費方案 0.1 CPU／512MB，並發太高會壓垮節點與記憶體，預設只開 3 個。
IMPORT_CONCURRENCY = max(1, int(os.getenv('IMPORT_CONCURRENCY', '3')))
# 單次匯入的曲目上限，超過會截斷並提示；避免超大歌單長時間佔用背景任務與搜尋額度。
IMPORT_MAX_TRACKS = max(1, int(os.getenv('IMPORT_MAX_TRACKS', '200')))
# 單首搜尋逾時秒數：避免某一首卡死拖住整批。
IMPORT_SEARCH_TIMEOUT = float(os.getenv('IMPORT_SEARCH_TIMEOUT', '15'))
# 每批同時建立的搜尋任務數：分批處理，避免一次替 200 首建立 200 個協程物件。
IMPORT_BATCH_SIZE = max(IMPORT_CONCURRENCY, int(os.getenv('IMPORT_BATCH_SIZE', '9')))
# 匯入進度訊息的最短更新間隔：沿用第 6 週節流原則（至少 5 秒），避免觸發 Discord 429。
IMPORT_PROGRESS_INTERVAL = max(5.0, PROGRESS_UPDATE_INTERVAL)


# ---------- YouTube 播放清單批量匯入 ----------
# 已刪除／私人影片在 extract_flat 清單中的標題（皆為小寫比對）。
YT_INVALID_TITLES = frozenset({"[deleted video]", "[private video]", "deleted video", "private video"})
_YT_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})


def parse_youtube_playlist_url(text: str | None) -> str | None:
    """判斷是否為「明確的 YouTube 播放清單網址」（/playlist?list=...），是的話回傳正規化後的網址。

    - 帶 list=RD...（YouTube 自動生成的無限合輯 Mix）一律不視為播放清單，維持單曲播放。
    - watch?v=...&list=... 這類「正在播放某首並附帶清單」的網址也只播單曲。
    """
    if not text:
        return None
    from urllib.parse import parse_qs, urlparse

    raw = text.strip()
    if " " in raw:
        return None
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in _YT_HOSTS:
        return None
    if parsed.path.rstrip("/") != "/playlist":
        return None
    list_ids = parse_qs(parsed.query).get("list") or []
    if not list_ids:
        return None
    list_id = list_ids[0].strip()
    if not list_id or list_id.upper().startswith("RD"):
        return None
    return f"https://www.youtube.com/playlist?list={list_id}"


@dataclass(slots=True)
class YouTubeTrack:
    """YouTube 清單中的單一曲目：直接攜帶 Lavalink 已解析好的 Playable，不需再逐首搜尋。"""

    title: str
    playable: Any

    def search_query(self) -> str:
        return self.title


@dataclass(slots=True)
class YouTubeFetchResult:
    """YouTube 播放清單解析結果，欄位與 SpotifyFetchResult 對齊以共用匯入流程。"""

    tracks: list[YouTubeTrack]
    truncated: bool
    skipped: int


def filter_youtube_tracks(playables: list[Any] | None, limit: int) -> YouTubeFetchResult:
    """過濾 Lavalink 回傳的曲目：剔除已刪除／私人／無標題或無法辨識的項目，並套用數量上限。"""
    tracks: list[YouTubeTrack] = []
    skipped = 0
    truncated = False
    for pl in playables or []:
        title = str(getattr(pl, "title", "") or "").strip()
        if not title or title.lower() in YT_INVALID_TITLES or not getattr(pl, "identifier", None):
            skipped += 1
            continue
        if len(tracks) >= limit:
            truncated = True
            break
        tracks.append(YouTubeTrack(title=title, playable=pl))
    return YouTubeFetchResult(tracks=tracks, truncated=truncated, skipped=skipped)


def format_duration(length_ms: int | None) -> str:
    """將 wavelink Playable.length（毫秒）轉成 mm:ss 字串，供 Embed 顯示用。"""
    if not length_ms:
        return "直播 / 未知長度"
    total_seconds = length_ms // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


# ---------- 第4週新增：循環模式與 Seek 時間換算 ----------
# 循環模式狀態機：off -> single -> all -> off（/music_loop 不帶參數時依序切換）
LOOP_LABELS = {
    "off": "🔁 循環：關閉",
    "single": "🔂 循環：單曲",
    "all": "🔁 循環：整個佇列",
}
LOOP_NEXT = {"off": "single", "single": "all", "all": "off"}

from cogs.database import (
    load_music_settings as _load_music_settings,
    save_music_settings as _save_music_settings,
    build_panel_link_button,
    build_panel_link_view,
    get_track_artwork,
    PLAYLIST_MAX_TRACKS,
    are_tracks_duplicate,
    get_user_playlist,
    get_playlist_by_share_code,
    create_user_playlist,
    update_user_playlist,
    add_tracks_to_user_playlist,
    remove_track_from_user_playlist,
    reorder_track_in_user_playlist,
    regenerate_user_playlist_share_code,
    delete_user_playlist,
    save_shared_playlist_for_user,
    get_saved_playlists_for_user,
    get_all_public_playlists,
)

PLAYLIST_PAGE_SIZE = 10


_TIME_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+)$")


def parse_time_to_ms(text: str) -> int | None:
    """'90' / '1:30' / '1:02:03' -> 毫秒；格式錯誤回傳 None。"""
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    seconds = 0
    for part in (p for p in m.groups() if p is not None):  # 由高位到低位
        seconds = seconds * 60 + int(part)
    return seconds * 1000


def format_position(ms: int) -> str:
    """播放位置（毫秒）-> m:ss 或 h:mm:ss。不能用 format_duration：位置 0 會被當成「直播」。"""
    total = max(int(ms), 0) // 1000
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# ---------- 第 6 週新增：進度條渲染與動態 Embed ----------
def format_time(ms: int | float | None) -> str:
    """第 6 週新增：將毫秒時間格式化為 mm:ss 或 h:mm:ss 字串。

    - ms 為 None 或負值時夾在 00:00。
    - 超過一小時時以 h:mm:ss 格式呈現（例如 1:02:03）。
    - 小於一小時以 mm:ss 格式呈現（例如 04:05、00:00）。
    """
    if ms is None or ms < 0:
        total_seconds = 0
    else:
        total_seconds = int(ms) // 1000

    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def build_progress_bar(
    position_ms: int | float | None,
    length_ms: int | float | None,
    width: int = 20,
    is_stream: bool = False,
) -> str:
    """第 6 週新增：純函式渲染進度條字串，例如 `▬▬▬🔘▬▬▬▬▬▬ 01:23 / 04:05`。

    邊界情況處理：
    - is_stream 為 True、length_ms 為 0、None、負值、或極大值（>= 86,400,000 即 24 小時以上視為直播）
      -> 顯示「🔴 直播中」，不繪製進度條。
    - position_ms 負值 -> 夾在 0%。
    - position_ms 超過 length_ms -> 夾在 100%。
    - width 小於 2 時自動調整為預設 20。
    - 本函式不依賴 Discord 物件，方便單元測試與狀態比對。
    """
    if is_stream or length_ms is None or length_ms <= 0 or length_ms >= 86_400_000:
        return "🔴 直播中"

    total_len = int(length_ms)
    pos = 0 if position_ms is None else int(position_ms)
    safe_pos = max(0, min(pos, total_len))

    w = width if width > 1 else 20
    ratio = safe_pos / total_len if total_len > 0 else 0.0
    index = int(round(ratio * (w - 1)))
    index = max(0, min(w - 1, index))

    bar = "▬" * index + "🔘" + "▬" * (w - 1 - index)
    return f"{bar} {format_time(safe_pos)} / {format_time(total_len)}"


def build_volume_bar(volume: int | float | None, width: int = 12) -> str:
    """第 6 週新增：換算音量百分比為進度條字串：🔊 − ▬▬▬▬🔘▬▬▬▬ + 80%"""
    try:
        vol = 100 if volume is None else max(0, min(100, int(volume)))
    except (ValueError, TypeError):
        vol = 100
    w = max(5, width)
    ratio = vol / 100.0
    index = max(0, min(w - 1, int(round(ratio * (w - 1)))))
    bar = "▬" * index + "🔘" + "▬" * (w - 1 - index)
    return f"🔊 − {bar} + {vol}%"


def build_now_playing_container(
    player: Any,
    notice: str | None = None,
    action_rows: list[ui.ActionRow] | None = None,
) -> ui.Container:
    """第 6 週新增：依照手繪架構使用 Discord Components V2 建構播放器主面板 Container：
    - 頂部 Badge 狀態標籤（🎶 正在播放 / ⏸️ 已暫停 / ⏹️ 播放已結束）
    - 曲目標題（Markdown 超連結）
    - 作者與循環模式
    - 中間區塊：左側封面縮圖（Thumbnail），右側播放進度條與音量條
    - 底部資訊：下一首預告、點播者與音訊來源
    - 操作按鈕區（ActionRow）
    - 最下方可選之動態提示 Banner（例如：已新增至待播清單，逾時自動移除）
    """
    children: list[discord.ui.Item] = []

    if player is None or getattr(player, "current", None) is None:
        accent_color = 0x979C9F  # light_grey
        header = ui.TextDisplay(
            "### ⏹️ 播放已結束\n目前沒有正在播放的歌曲。可使用 `/music_play` 點歌！"
        )
        children.append(header)
    else:
        track = player.current
        is_paused = bool(getattr(player, "paused", False))
        accent_color = 0xE67E22 if is_paused else 0x5865F2
        status_icon = "⏸️" if is_paused else "🎶"
        status_text = "已暫停" if is_paused else "正在播放"

        # 1. 標題與超連結
        raw_title = getattr(track, "title", None) or "未知曲目"
        uri = getattr(track, "uri", None)
        title_display = f"[{raw_title}]({uri})" if uri else raw_title

        # 2. 作者與循環模式
        author = getattr(track, "author", None) or "未知"
        loop_mode = getattr(player, "loop_mode", "off")
        loop_label = LOOP_LABELS.get(loop_mode, "🔁 循環：關閉")
        is_shuffle = bool(getattr(player, "shuffle", False))
        mode_str = f"**{loop_label}**" + (" ｜ **🔀 隨機播放**" if is_shuffle else "")

        # 3. 頂部區塊：狀態、標題、作者與循環狀態，並將封面縮圖置於右上角（Section accessory，小圖）
        artwork_url = get_track_artwork(track)
        top_lines = [
            f"### {status_icon} {status_text}",
            f"## {title_display}",
            f"**👤 {author}**",
            f"{mode_str}",
        ]
        top_text = "\n".join(top_lines)

        if artwork_url:
            top_section = ui.Section(
                ui.TextDisplay(top_text),
                accessory=ui.Thumbnail(artwork_url),
            )
        else:
            top_section = ui.TextDisplay(top_text)
        children.append(top_section)
        children.append(ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))

        # 4. 中間區塊：播放進度條與音量條
        pos_ms = getattr(player, "position", 0)
        len_ms = getattr(track, "length", None)
        is_stream = bool(getattr(track, "is_stream", False))
        progress_str = build_progress_bar(pos_ms, len_ms, width=14, is_stream=is_stream)

        try:
            vol = int(getattr(player, "volume", 100))
        except (ValueError, TypeError):
            vol = 100
        vol_str = build_volume_bar(vol, width=12)

        middle_lines = [
            f"{progress_str}\n",
            f"{vol_str}",
        ]
        middle_text = "\n".join(middle_lines)
        children.append(ui.TextDisplay(middle_text))
        children.append(ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))

        # 4. 下一首預告與點播者/來源
        song_queue = getattr(player, "song_queue", None)
        next_preview = "無"
        if song_queue and len(song_queue) > 0:
            first_item = song_queue[0]
            next_track = first_item[0] if isinstance(first_item, tuple) else first_item
            next_title = getattr(next_track, "title", None) or "未知曲目"
            next_uri = getattr(next_track, "uri", None)
            next_preview = f"[{next_title}]({next_uri})" if next_uri else next_title

        requester_name = getattr(player, "current_requester", None) or "未知"
        source_raw = getattr(track, "source", None) or ""
        source_map = {
            "youtube": "YouTube",
            "soundcloud": "SoundCloud",
            "spotify": "Spotify",
            "applemusic": "Apple Music",
        }
        source_display = source_map.get(
            str(source_raw).lower(), str(source_raw).capitalize() if source_raw else "Lavalink"
        )

        # 底部資訊：左側為點播者，右側為音訊來源（使用全形空格推至右側，無分隔點，完全貼合手繪 wireframe 架構）
        footer_text = (
            f"**📜 下一首預告**：{next_preview}\n"
            f"-# 由 {requester_name} 點播　　　　　　　　　　　　　　來源：{source_display}"
        )
        children.append(ui.TextDisplay(footer_text))

    # 5. 操作按鈕（若有傳入 ActionRows）
    if action_rows:
        children.append(ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        for row in action_rows:
            children.append(row)

    # 6. 最下方提示 Banner（若有）
    if notice and notice.strip():
        children.append(ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        children.append(ui.TextDisplay(notice.strip()))

    return ui.Container(*children, accent_color=accent_color)


def build_now_playing_embed(player: Any) -> discord.Embed:
    """第 6 週新增：建立音樂播放面板的動態 Embed。

    從 wavelink.Playable 取出：標題、作者、長度、封面（artwork，縮圖）、連結（uri）、來源（source）。
    內容包含：
    - 標題（可點擊連結）、作者
    - 播放進度條（包含時間換算）
    - 音量、目前循環模式
    - 下一首預告（取自 song_queue 第一首，沒有則顯示「無」）
    - 頁尾顯示點播者與來源
    - 外觀依播放/暫停狀態切換圖示與顏色（播放中藍紫色，暫停橘色，結束淺灰色）
    - 缺少封面或欄位時提供預設值，絕不拋出例外。
    """
    if player is None or getattr(player, "current", None) is None:
        return discord.Embed(
            title="⏹️ 播放已結束",
            description="目前沒有正在播放的歌曲。可使用 `/music_play` 點歌！",
            color=discord.Color.light_grey(),
        )

    track = player.current
    is_paused = bool(getattr(player, "paused", False))
    status_icon = "⏸️" if is_paused else "🎶"
    status_text = "已暫停" if is_paused else "正在播放"

    # 標題與超連結
    raw_title = getattr(track, "title", None) or "未知曲目"
    uri = getattr(track, "uri", None)
    title_display = f"[{raw_title}]({uri})" if uri else raw_title

    embed = discord.Embed(
        title=f"{status_icon} {status_text}",
        description=title_display,
        color=discord.Color.orange() if is_paused else discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )

    # 封面縮圖（不快取圖片本體以節省 Render 512MB RAM）
    artwork = get_track_artwork(track)
    if artwork:
        embed.set_thumbnail(url=str(artwork))

    # 作者
    author = getattr(track, "author", None) or "未知"
    embed.add_field(name="👤 作者", value=author, inline=True)

    # 音量
    try:
        volume = int(getattr(player, "volume", 100))
    except (ValueError, TypeError):
        volume = 100
    embed.add_field(name="🔊 音量", value=f"{volume}%", inline=True)

    # 循環模式
    loop_mode = getattr(player, "loop_mode", "off")
    loop_label = LOOP_LABELS.get(loop_mode, "關閉")
    embed.add_field(name="🔁 循環模式", value=loop_label, inline=True)

    # 進度條與時間換算
    pos_ms = getattr(player, "position", 0)
    len_ms = getattr(track, "length", None)
    is_stream = bool(getattr(track, "is_stream", False))
    progress_bar = build_progress_bar(pos_ms, len_ms, width=20, is_stream=is_stream)
    embed.add_field(name="⏱️ 播放進度", value=progress_bar, inline=False)

    # 下一首預告（取自 song_queue 第一首）
    song_queue = getattr(player, "song_queue", None)
    next_preview = "無"
    if song_queue and len(song_queue) > 0:
        first_item = song_queue[0]
        next_track = first_item[0] if isinstance(first_item, tuple) else first_item
        next_title = getattr(next_track, "title", None) or "未知曲目"
        next_uri = getattr(next_track, "uri", None)
        next_preview = f"[{next_title}]({next_uri})" if next_uri else next_title

    embed.add_field(name="📜 下一首預告", value=next_preview, inline=False)

    # 頁尾：點播者與音訊來源
    requester_name = getattr(player, "current_requester", None)
    source_raw = getattr(track, "source", None) or ""
    source_map = {
        "youtube": "YouTube",
        "soundcloud": "SoundCloud",
        "spotify": "Spotify",
        "applemusic": "Apple Music",
    }
    source_display = source_map.get(str(source_raw).lower(), str(source_raw).capitalize() if source_raw else "Lavalink")

    footer_parts = []
    if requester_name:
        footer_parts.append(f"由 {requester_name} 點播")
    footer_parts.append(f"來源：{source_display}")
    embed.set_footer(text=" • ".join(footer_parts))

    return embed


# ---------- Lavalink 節點連線診斷 ----------
# 背景：wavelink 遇到連線錯誤（TLS 握手失敗、拒絕連線…）只會寫進它自己的 logger，
# 然後在背景無限退避重試；外層 asyncio.wait_for 逾時取消後，丟給我們的只有一個
# 沒有任何訊息的 TimeoutError（開發者收到的 DM「錯誤訊息」欄位是空白的）。
# 實測踩過的坑：LAVALINK_URI 寫成 https:// 但節點連接埠只提供明文 HTTP，
# TLS 握手必失敗，音樂功能就這樣壞掉。這裡在失敗時主動對 {uri}/version 探測一次，
# 把真實原因翻譯成可行動的中文訊息（該改哪個環境變數、密碼對不對、節點有沒有開）。


async def _probe_lavalink_version(
    url: str, headers: dict, timeout: float
) -> tuple[int | None, str, Exception | None]:
    """GET 一次 Lavalink 端點，回傳 (status, body, exception)，絕不拋例外。

    診斷函式本身不能再丟例外，否則錯誤回報流程會被原始連線錯誤整個炸掉。
    """
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, headers=headers) as resp:
                return resp.status, (await resp.text()).strip(), None
    except Exception as exc:
        return None, '', exc


def _describe_lavalink_version(url: str, status: int, body: str) -> str:
    """把 /version 的 HTTP 回應翻譯成人話（供後台 log 與開發者 DM）。"""
    if status == 200:
        return (
            f"{url} 回應 200（Lavalink {body or '未知版本'}），節點本身在線；"
            "問題可能出在 WebSocket（/v4/websocket）連線，請確認反向代理有轉發 WebSocket 升級，或節點是否過載。"
        )
    if status == 401:
        return f"{url} 回應 401：LAVALINK_PASSWORD 密碼錯誤。"
    if status == 404:
        return f"{url} 回應 404：LAVALINK_URI 指向的不是 Lavalink v4 服務，請確認連接埠與路徑。"
    return f"{url} 回應非預期的 HTTP {status}。"


def _describe_lavalink_probe_error(url: str, exc: Exception, timeout: float) -> str:
    """把探測時丟出的例外翻譯成人話（訊息裡刻意不帶密碼）。"""
    if isinstance(exc, (TimeoutError, aiohttp.ServerTimeoutError)):
        return f"節點探測逾時（{timeout:g} 秒內無回應）：{url}"
    if isinstance(exc, aiohttp.ClientConnectorError):
        reason = getattr(exc, 'os_error', None) or exc
        return (
            f"無法連線到 Lavalink 節點：{url}（{reason}）。"
            "請確認節點是否在線，以及 LAVALINK_URI 的協定與連接埠是否正確。"
        )
    if isinstance(exc, aiohttp.ClientError):
        return f"探測 Lavalink 節點時發生連線錯誤：{type(exc).__name__}: {exc}"
    return f"探測 Lavalink 節點時發生未預期錯誤：{type(exc).__name__}: {exc}"


async def _safe_probe_lavalink_version(
    url: str, headers: dict, timeout: float
) -> tuple[int | None, str, Exception | None]:
    """_probe_lavalink_version 的保險絲：即使 probe 本身改壞丟了例外，診斷也不會炸。"""
    try:
        return await _probe_lavalink_version(url, headers, timeout)
    except Exception as exc:
        return None, '', exc


async def diagnose_lavalink_node(
    lavalink_uri: str, lavalink_password: str, timeout: float = LAVALINK_PROBE_TIMEOUT
) -> str:
    """連線失敗後探測節點，回傳「給人看的」診斷字串；只讀、絕不拋例外、不包含密碼。

    呼叫端（_report_lavalink_failure）會把它印進後台 log，並附在 notify_owner_error
    的 extra_info 裡；回報前 main.py 還會再過一層 redact_secrets。
    """
    base = lavalink_uri.rstrip('/')
    url = f'{base}/version'
    headers = {'Authorization': lavalink_password}
    status, body, exc = await _safe_probe_lavalink_version(url, headers, timeout)
    if exc is None:
        return _describe_lavalink_version(url, status, body)

    if base.startswith('https://'):
        # 本次故障的根因：https:// 指到只講明文 HTTP 的節點（TLS 握手必失敗）。
        # 改探 http:// 並比對結果，直接在訊息裡告訴開發者要把 LAVALINK_URI 改成什麼。
        http_base = 'http://' + base[len('https://'):]
        h_status, _, h_exc = await _safe_probe_lavalink_version(f'{http_base}/version', headers, timeout)
        if h_exc is None:
            hint = '；另外 LAVALINK_PASSWORD 也不正確（401），請一併修正' if h_status == 401 else ''
            return (
                f'LAVALINK_URI 使用了 https://，但節點不支援 TLS（{exc}）。'
                f'改用 http:// 可正常連線（{http_base}/version 回應 HTTP {h_status}）{hint}，'
                f'請把 LAVALINK_URI 從「{base}」改成「{http_base}」。'
            )
        return f'無法連線到 Lavalink 節點：{url}（{exc}），改用 {http_base} 探測同樣失敗（{h_exc}）。'

    return _describe_lavalink_probe_error(url, exc, timeout)


# ---------- 點歌者限定操作：按鈕確認與全員投票 ----------
# 按鈕互動的有效期限（秒）；超過後按鈕自動失效。
CONTROL_REQUEST_TIMEOUT = 30


class RequesterApprovalView(ui.View):
    """非點歌者想暫停／續播時，向點歌者請求允許的按鈕。

    規則：只有點歌者本人能按；兩顆按鈕都是一次性的（按完整組 disabled）。
    timeout 到期自動視為拒絕，避免佔著按鈕讓指令結果懸而未決。
    """

    def __init__(self, cog: "Music", requester_id: int, requester_name: str, track=None):
        super().__init__(timeout=CONTROL_REQUEST_TIMEOUT)
        self.cog = cog
        self.requester_id = requester_id
        self.requester_name = requester_name
        self.track = track  # 被拒絕時用來鎖住「同一首歌不得再提請求」
        self.message: discord.Message | None = None
        self.decided = False
        self.result = None
        self.after_decision = None  # async callable(allowed: bool)；點歌者決定後執行實際操作

    def _finish(self, allowed: bool, interaction: discord.Interaction):
        self.decided = True
        self.result = allowed
        for item in self.children:
            item.disabled = True  # 一次性：決定後整組按鈕失效
        label = "✅ 允許" if allowed else "❌ 拒絕"
        return f"{label} — 由 {self.requester_name} 決定"

    async def on_timeout(self):
        # 超時視為拒絕；把畫面上的按鈕停用，讓使用者知道已經失效
        if self.decided or self.message is None:
            return
        self.decided = True
        self.result = False
        self.cog._resolve_control_request(self.requester_id, allowed=False, view=self)
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(
                content=f"⌛ 操作請求已逾時（{CONTROL_REQUEST_TIMEOUT} 秒），視為拒絕。",
                view=self,
            )
        except discord.HTTPException:
            pass

    @ui.button(label="允許", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("🔒 只有點這首歌的人能決定！", ephemeral=True)
            return
        if self.decided:
            await interaction.response.send_message("ℹ️ 這個請求已經被決定過了。", ephemeral=True)
            return
        self.cog._resolve_control_request(self.requester_id, allowed=True, view=self)
        text = self._finish(True, interaction)
        await interaction.response.edit_message(content=text, view=self)
        if self.after_decision is not None:
            await self.after_decision(True)

    @ui.button(label="拒絕", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("🔒 只有點這首歌的人能決定！", ephemeral=True)
            return
        if self.decided:
            await interaction.response.send_message("ℹ️ 這個請求已經被決定過了。", ephemeral=True)
            return
        self.cog._resolve_control_request(self.requester_id, allowed=False, view=self)
        text = self._finish(False, interaction)
        await interaction.response.edit_message(content=text, view=self)
        if self.after_decision is not None:
            await self.after_decision(False)


class StopVoteView(ui.View):
    """停止播放的全員投票：同意／不同意按鈕即時顯示人數。

    任何一票不同意就否決，全員同意（或所有在場的人都投了同意）才停止。
    每個人只能投一票、不能改票；按鈕在結果出爐後整組失效。
    投票被否決時會鎖住該首歌：下一首開始播放前不得再發起停止投票（拒絕鎖）。
    """

    def __init__(self, cog: "Music", guild_id: int, initiator_id: int, initiator_name: str,
                 voter_ids: set[int], track=None):
        super().__init__(timeout=CONTROL_REQUEST_TIMEOUT)
        self.cog = cog
        self.guild_id = guild_id
        self.initiator_id = initiator_id
        self.initiator_name = initiator_name
        self.track = track  # 發起投票當下正在播的歌；被否決時用來鎖住「同一首歌不得再發起」
        self.voter_ids = voter_ids          # 投票當下在語音頻道的真人 ID
        self.votes: dict[int, bool] = {}    # user_id -> True(同意) / False(不同意)
        self.message: discord.Message | None = None
        self.finished = False
        self.approved = None
        self.on_approved = None  # async callable()；投票通過後執行實際停止流程

    def _ballot_text(self, footer: str) -> str:
        yes = sum(1 for v in self.votes.values() if v)
        no = sum(1 for v in self.votes.values() if not v)
        return (
            f"🗳️ {self.initiator_name} 提議停止播放並清空佇列。\n"
            f"✅ 同意：{yes} 人　❌ 不同意：{no} 人\n{footer}"
        )

    def _finish(self, approved: bool, footer: str) -> str:
        self.finished = True
        self.approved = approved
        self.cog._resolve_stop_vote(self.guild_id, approved=approved, view=self)
        for item in self.children:
            item.disabled = True  # 結果出爐，按鈕全部失效
        return self._ballot_text(footer)

    async def _maybe_execute(self, approved: bool):
        """結算後若通過且掛了執行回呼，就實際執行停止流程。"""
        if approved and self.on_approved is not None:
            await self.on_approved()

    def _auto_resolve(self) -> bool:
        """所有在場的人都投了就提前結算：全同意 -> 通過，否則否決。"""
        return all(uid in self.votes for uid in self.voter_ids)

    async def on_timeout(self):
        if self.finished or self.message is None:
            return
        # 逾時結算：已有任何人投不同意 -> 否決；否則視為同意
        approved = all(self.votes.get(uid, True) for uid in self.voter_ids)
        text = self._finish(approved, "⌛ 投票逾時，自動結算。")
        try:
            await self.message.edit(content=text, view=self)
        except discord.HTTPException:
            pass
        await self._maybe_execute(approved)

    async def _vote(self, interaction: discord.Interaction, choice: bool):
        if interaction.user.id not in self.voter_ids:
            await interaction.response.send_message("🔒 只有在語音頻道裡的人能投票！", ephemeral=True)
            return
        if interaction.user.id in self.votes:
            await interaction.response.send_message("ℹ️ 你已經投過票了，不能重複投票或改票。", ephemeral=True)
            return
        if self.finished:
            await interaction.response.send_message("ℹ️ 投票已經結束了。", ephemeral=True)
            return

        self.votes[interaction.user.id] = choice
        if not choice:
            # 任何人拒絕 -> 立即否決
            text = self._finish(False, "❌ 有人不同意，停止請求已否決。")
            await interaction.response.edit_message(content=text, view=self)
            await self._maybe_execute(False)
            return

        if self._auto_resolve():
            text = self._finish(True, "🎉 全員同意！")
            await interaction.response.edit_message(content=text, view=self)
            await self._maybe_execute(True)
            return

        await interaction.response.edit_message(content=self._ballot_text("按鈕會即時更新人數。"), view=self)

    @ui.button(label="同意", style=discord.ButtonStyle.success, emoji="👍")
    async def agree(self, interaction: discord.Interaction, button: ui.Button):
        await self._vote(interaction, True)

    @ui.button(label="不同意", style=discord.ButtonStyle.danger, emoji="👎")
    async def disagree(self, interaction: discord.Interaction, button: ui.Button):
        await self._vote(interaction, False)


# ---------- 第 5 週新增：多按鈕音樂控制面板 ----------
_PANEL_PLAYER_UNSET = object()


class MusicControlView(ui.LayoutView):
    """第 5 & 6 週新增：多按鈕音樂控制面板 LayoutView（持久化 View，timeout=None）。

    設計理由與技術架構：
    1. 持久化（bot.add_view()）：
       - timeout 必須設為 None：若設有逾時，Bot 在重開機或長時間運作後，舊訊息上的按鈕
         將無法再觸發 Discord 事件。設為 None 並指定唯一的 custom_id，可確保任何時候
         按鈕被點擊都能正常被 Gateway 與 Bot 派發。
       - 所有按鈕皆設定固定且唯一的 custom_id（music:toggle_pause, music:rewind_10, music:forward_10 等）。
       - View 本身不綁定特定的 Guild 物件或 Player 實例。在按鈕被點擊時，統一透過
         interaction.guild 取得該伺服器的 Player，實現單一 View 類別跨伺服器複用，且狀態嚴格隔離。
    2. Components V2 容器化架構：
       - 繼承 ui.LayoutView，以 ui.Container 包裝上方 Badge、曲目資訊、Section 封面與雙進度條、
         下一首預告、按鈕 ActionRow、以及最下方動態提示 Banner。
    3. 逾時與生命週期替代方案：
       - 持久化面板不依賴 View 內建的 on_timeout，而是將生命週期綁定播放狀態：
         當播放結束、佇列清空、或 Bot 離開語音頻道時，由 Music Cog 主動將該伺服器的面板按鈕全部 disabled。
       - 若 Bot 重啟後，舊面板按鈕被按下（此時記憶體中 Player 已不存在），於 interaction_check 中優雅攔截，
         回覆 ephemeral 提示「目前沒有播放中的內容」，並嘗試將舊訊息上的按鈕全部 disable，避免拋出未處理例外。
    4. 權限防呆（interaction_check & requester check）：
       - 操作者必須與 Bot 處於同一個語音頻道，否則以 ephemeral 訊息提示並拒絕。
       - 快進與倒退按鈕繼承點歌者專屬保護邏輯（_check_requester_control），並於直播時自動 disable。
    5. 統一更新邏輯（refresh_buttons）：
       - 統一依 player 狀態設定每顆按鈕的 label / emoji / disabled，避免狀態邏輯散落各處。
    """

    CUSTOM_ID_TOGGLE_PAUSE = "music:toggle_pause"
    CUSTOM_ID_REWIND_10 = "music:rewind_10"
    CUSTOM_ID_FORWARD_10 = "music:forward_10"
    CUSTOM_ID_SKIP = "music:skip"
    CUSTOM_ID_STOP = "music:stop"
    CUSTOM_ID_VOL_DOWN = "music:vol_down"
    CUSTOM_ID_VOL_UP = "music:vol_up"
    CUSTOM_ID_LOOP = "music:loop"
    CUSTOM_ID_SHUFFLE = "music:shuffle"

    def __init__(
        self,
        cog: "Music | None" = None,
        player: "wavelink_module.Player | None" = None,
        notice: str | None = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog

        # 按鈕實例建立
        self.btn_toggle = ui.Button(
            label="暫停",
            emoji="⏸️",
            style=discord.ButtonStyle.primary,
            custom_id=self.CUSTOM_ID_TOGGLE_PAUSE,
        )
        self.btn_toggle.callback = self.toggle_pause

        self.btn_rewind = ui.Button(
            label="-10s",
            emoji="⏪",
            style=discord.ButtonStyle.secondary,
            custom_id=self.CUSTOM_ID_REWIND_10,
        )
        self.btn_rewind.callback = self.rewind_10

        self.btn_forward = ui.Button(
            label="+10s",
            emoji="⏩",
            style=discord.ButtonStyle.secondary,
            custom_id=self.CUSTOM_ID_FORWARD_10,
        )
        self.btn_forward.callback = self.forward_10

        self.btn_skip = ui.Button(
            label="切歌",
            emoji="⏭️",
            style=discord.ButtonStyle.secondary,
            custom_id=self.CUSTOM_ID_SKIP,
        )
        self.btn_skip.callback = self.skip

        self.btn_stop = ui.Button(
            label="停止",
            emoji="⏹️",
            style=discord.ButtonStyle.danger,
            custom_id=self.CUSTOM_ID_STOP,
        )
        self.btn_stop.callback = self.stop

        self.btn_vol_down = ui.Button(
            label="音量 −",
            emoji="🔉",
            style=discord.ButtonStyle.secondary,
            custom_id=self.CUSTOM_ID_VOL_DOWN,
        )
        self.btn_vol_down.callback = self.volume_down

        self.btn_vol_up = ui.Button(
            label="音量 +",
            emoji="🔊",
            style=discord.ButtonStyle.secondary,
            custom_id=self.CUSTOM_ID_VOL_UP,
        )
        self.btn_vol_up.callback = self.volume_up

        self.btn_loop = ui.Button(
            label="循環：關閉",
            emoji="🔁",
            style=discord.ButtonStyle.secondary,
            custom_id=self.CUSTOM_ID_LOOP,
        )
        self.btn_loop.callback = self.toggle_loop

        self.btn_shuffle = ui.Button(
            label="隨機：關閉",
            emoji="🔀",
            style=discord.ButtonStyle.secondary,
            custom_id=self.CUSTOM_ID_SHUFFLE,
        )
        self.btn_shuffle.callback = self.toggle_shuffle

        # 組裝成兩列 ActionRow：Row 1 (5 顆)，Row 2 (4 顆：音量 - / 音量 + / 循環 / 隨機)
        self.row1 = ui.ActionRow(
            self.btn_toggle,
            self.btn_rewind,
            self.btn_forward,
            self.btn_skip,
            self.btn_stop,
        )
        self.row2 = ui.ActionRow(self.btn_vol_down, self.btn_vol_up, self.btn_loop, self.btn_shuffle)

        # 組裝 Container
        self.container = build_now_playing_container(
            player=player,
            notice=notice,
            action_rows=[self.row1, self.row2],
        )
        self.add_item(self.container)
        self.refresh_buttons(player)

    def refresh_buttons(self, player: "wavelink_module.Player | None" = None):
        """統一依據 player 播放狀態設定每顆按鈕的 label / emoji / disabled 狀態。

        - 沒有正在播放的曲目（或 player 為 None）：全部按鈕 disabled。
        - 暫停中：切換按鈕顯示「繼續」、綠色；播放中：顯示「暫停」、藍色。
        - 直播中或無法 Seek：快進／倒退按鈕 disabled。
        - 佇列沒有下一首：切歌按鈕 disabled。
        - 音量到 0% 時，「−」disabled；音量到 100% 時，「+」disabled。
        - 循環按鈕：依 loop_mode（off / single / all）切換文字、圖示與按鈕顏色。
        - 隨機按鈕：依 shuffle（開啟 / 關閉）切換文字、顏色。
        """
        has_current = player is not None and getattr(player, "current", None) is not None
        is_paused = bool(getattr(player, "paused", False)) if player else False

        # 狀態 1：沒有播放中的曲目或 player 不存在 -> 全部 disabled
        if not has_current:
            for item in self.walk_children():
                if isinstance(item, ui.Button):
                    item.disabled = True
            self.btn_toggle.label = "暫停"
            self.btn_toggle.emoji = "⏸️"
            self.btn_toggle.style = discord.ButtonStyle.secondary
            self.btn_loop.label = "循環：關閉"
            self.btn_loop.emoji = "🔁"
            self.btn_loop.style = discord.ButtonStyle.secondary
            self.btn_shuffle.label = "隨機：關閉"
            self.btn_shuffle.emoji = "🔀"
            self.btn_shuffle.style = discord.ButtonStyle.secondary
            return

        track = player.current
        song_queue = getattr(player, "song_queue", None) or deque()
        is_stream = bool(getattr(track, "is_stream", False))
        is_seekable = bool(getattr(track, "is_seekable", True)) and not is_stream

        try:
            volume = int(getattr(player, "volume", 100))
        except (ValueError, TypeError):
            volume = 100

        # 暫停 / 繼續切換按鈕
        self.btn_toggle.disabled = False
        if is_paused:
            self.btn_toggle.label = "繼續"
            self.btn_toggle.emoji = "▶️"
            self.btn_toggle.style = discord.ButtonStyle.success
        else:
            self.btn_toggle.label = "暫停"
            self.btn_toggle.emoji = "⏸️"
            self.btn_toggle.style = discord.ButtonStyle.primary

        # 倒退 / 快進按鈕：直播或不支援 Seek 則 disabled
        self.btn_rewind.disabled = not is_seekable
        self.btn_forward.disabled = not is_seekable

        # 切歌按鈕：佇列沒有下一首時 disabled
        self.btn_skip.disabled = (len(song_queue) == 0)

        # 停止按鈕
        self.btn_stop.disabled = False

        # 音量按鈕：邊界 disabled (0% ~ 100%)
        self.btn_vol_down.disabled = (volume <= 0)
        self.btn_vol_up.disabled = (volume >= 100)

        # 循環按鈕：依 loop_mode 切換狀態（off -> single -> all -> off）
        loop_mode = getattr(player, "loop_mode", "off")
        self.btn_loop.disabled = False
        if loop_mode == "single":
            self.btn_loop.label = "循環：單曲"
            self.btn_loop.emoji = "🔂"
            self.btn_loop.style = discord.ButtonStyle.primary
        elif loop_mode == "all":
            self.btn_loop.label = "循環：佇列"
            self.btn_loop.emoji = "🔁"
            self.btn_loop.style = discord.ButtonStyle.primary
        else:
            self.btn_loop.label = "循環：關閉"
            self.btn_loop.emoji = "🔁"
            self.btn_loop.style = discord.ButtonStyle.secondary

        # 隨機播放按鈕：依 shuffle（開啟 / 關閉）切換狀態
        is_shuffle = bool(getattr(player, "shuffle", False))
        self.btn_shuffle.disabled = False
        if is_shuffle:
            self.btn_shuffle.label = "隨機：開啟"
            self.btn_shuffle.emoji = "🔀"
            self.btn_shuffle.style = discord.ButtonStyle.primary
        else:
            self.btn_shuffle.label = "隨機：關閉"
            self.btn_shuffle.emoji = "🔀"
            self.btn_shuffle.style = discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """統一權限與狀態防呆：
        1. 伺服器環境檢查。
        2. 播放狀態防呆：Bot 重啟後或無播放中內容時，優雅提示並 disable 按鈕。
        3. 語音頻道檢查：操作者必須與 Bot 在同一個語音頻道。
        """
        if interaction.guild is None:
            await interaction.response.send_message("❌ 此控制面板僅限伺服器中使用。", ephemeral=True)
            return False

        player = interaction.guild.voice_client

        # 情況 A：機器人不在語音頻道，或沒有正在播放的曲目（例如 Bot 重開機後按舊面板）
        if player is None or (wavelink is not None and not isinstance(player, wavelink.Player)) or getattr(player, "current", None) is None:
            disabled_view = MusicControlView(self.cog, player=None)
            disabled_view.refresh_buttons(None)

            await interaction.response.send_message("目前沒有播放中的內容", ephemeral=True)

            try:
                if interaction.message:
                    await interaction.message.edit(content=None, embed=None, view=disabled_view)
            except (discord.NotFound, discord.HTTPException):
                pass
            return False

        # 情況 B：操作者與機器人是否在同一個語音頻道
        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None or user_voice.channel.id != player.channel.id:
            channel_mention = player.channel.mention if player.channel else "語音頻道"
            await interaction.response.send_message(
                f"❌ 你必須跟我在同一個語音頻道（{channel_mention}）才能操作控制面板！",
                ephemeral=True,
            )
            return False

        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item) -> None:
        """統一錯誤回報，調用 bot.notify_owner_error。"""
        print(f"❌ MusicControlView 按鈕操作錯誤 [{getattr(item, 'custom_id', 'unknown')}]: {error}", file=sys.stderr)

        # Lavalink Session 失效（REST 404 /v4/sessions/...）：不是程式錯誤，自動重連並恢復播放，不 DM 開發者
        cog: "Music | None" = self.cog or interaction.client.get_cog("Music")
        if cog is not None and cog._is_session_lost_error(error):
            player = interaction.guild.voice_client if interaction.guild else None
            await cog._handle_session_lost(player, error)
            msg = "🔄 音訊節點連線已中斷，正在自動重新連線並恢復播放，請稍候幾秒後再操作一次。"
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    await interaction.followup.send(msg, ephemeral=True)
            except discord.HTTPException:
                pass
            return

        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

        bot = interaction.client
        if hasattr(bot, "notify_owner_error"):
            await bot.notify_owner_error(
                error,
                interaction=interaction,
                extra_info=f"MusicControlView item={getattr(item, 'custom_id', 'unknown')}",
            )

        msg = "❌ 操作控制面板時發生錯誤，已回報給開發者。"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def toggle_pause(self, interaction: discord.Interaction, button: ui.Button | None = None):
        player: "wavelink_module.Player" = interaction.guild.voice_client
        cog: "Music" = self.cog or interaction.client.get_cog("Music")

        is_paused = getattr(player, "paused", False)
        await player.pause(not is_paused)
        if cog:
            await cog._sync_panel(interaction.guild.id, player, interaction=interaction)

    async def rewind_10(self, interaction: discord.Interaction, button: ui.Button | None = None):
        player: "wavelink_module.Player" = interaction.guild.voice_client
        cog: "Music" = self.cog or interaction.client.get_cog("Music")

        # 繼承既有防濫用檢驗：點歌者保護
        if cog and not await cog._check_requester_control(interaction):
            return

        track = getattr(player, "current", None)
        if not track:
            await interaction.response.send_message("ℹ️ 目前沒有正在播放的歌曲。", ephemeral=True)
            return

        if not getattr(track, "is_seekable", True) or getattr(track, "is_stream", False):
            await interaction.response.send_message("❌ 這首歌不支援跳轉（可能是直播）。", ephemeral=True)
            return

        current_pos = getattr(player, "position", 0)
        target = max(0, current_pos - 10000)
        await player.seek(target)
        if cog:
            await cog._sync_panel(interaction.guild.id, player, interaction=interaction)

    async def forward_10(self, interaction: discord.Interaction, button: ui.Button | None = None):
        player: "wavelink_module.Player" = interaction.guild.voice_client
        cog: "Music" = self.cog or interaction.client.get_cog("Music")

        # 繼承既有防濫用檢驗：點歌者保護
        if cog and not await cog._check_requester_control(interaction):
            return

        track = getattr(player, "current", None)
        if not track:
            await interaction.response.send_message("ℹ️ 目前沒有正在播放的歌曲。", ephemeral=True)
            return

        if not getattr(track, "is_seekable", True) or getattr(track, "is_stream", False):
            await interaction.response.send_message("❌ 這首歌不支援跳轉（可能是直播）。", ephemeral=True)
            return

        current_pos = getattr(player, "position", 0)
        track_len = getattr(track, "length", 0) or 0
        target = max(0, min(current_pos + 10000, max(track_len - 1000, 0)))
        await player.seek(target)
        if cog:
            await cog._sync_panel(interaction.guild.id, player, interaction=interaction)

    async def skip(self, interaction: discord.Interaction, button: ui.Button | None = None):
        player: "wavelink_module.Player" = interaction.guild.voice_client
        cog: "Music" = self.cog or interaction.client.get_cog("Music")

        await interaction.response.defer()
        if cog:
            cog._pending_panel_interaction[interaction.guild.id] = interaction

        current_track = getattr(player, "current", None) or getattr(player, "_active_track", None)
        player.end_intent = "skip"
        try:
            if getattr(player, "paused", False):
                await player.pause(False)
            await player.skip(force=True)
        except Exception as exc:
            if cog and cog._is_session_lost_error(exc):
                # Lavalink Session 已失效時，遠端播放器早已停止且不會再送出 TrackEndEvent；
                # 直接觸發本地 on_wavelink_track_end 推進佇列（並在下一首起播或背景重連時自動修復 Session）
                await cog.on_wavelink_track_end(
                    SimpleNamespace(player=player, reason="stopped", track=current_track)
                )
                return
            player.end_intent = None
            if cog:
                cog._pending_panel_interaction.pop(interaction.guild.id, None)
            raise

    async def stop(self, interaction: discord.Interaction, button: ui.Button | None = None):
        player: "wavelink_module.Player" = interaction.guild.voice_client
        cog: "Music" = self.cog or interaction.client.get_cog("Music")

        # 停止時立即同步取消置底任務、背景匯入並清空播放曲目狀態（在任何 await 之前），
        # 防止停止期間或停止後仍觸發置底或被誤判為播放中。
        if cog:
            cog._cancel_panel_bump(interaction.guild.id, force=True)
            cog._cancel_import(interaction.guild.id)
        song_queue = getattr(player, "song_queue", None)
        if song_queue:
            song_queue.clear()
        player.loop_mode = "off"
        player.shuffle = False
        current_track = getattr(player, "current", None) or getattr(player, "_active_track", None)
        if cog:
            cog._clear_rejection(current_track)
            cog._clear_player_playback_state(player)
        await interaction.response.defer()
        if current_track:
            player.end_intent = "stop"
            try:
                if getattr(player, "paused", False):
                    await player.pause(False)
                await player.skip(force=True)
            except Exception as exc:
                # Session 已失效時 Lavalink 端本來就沒在播了，停止仍應完成（收尾面板），並在背景重建 Session
                if not (cog and cog._is_session_lost_error(exc)):
                    raise
                player.end_intent = None
                await cog._handle_session_lost(player, exc)
        if cog:
            await cog._update_panel_ended(
                interaction.guild.id,
                interaction=interaction,
                reason=f"⏹️ 已停止播放並清空佇列（操作者：{interaction.user.display_name}）",
            )

    async def volume_down(self, interaction: discord.Interaction, button: ui.Button | None = None):
        player: "wavelink_module.Player" = interaction.guild.voice_client
        cog: "Music" = self.cog or interaction.client.get_cog("Music")

        current_vol = getattr(player, "volume", 100)
        new_vol = max(0, current_vol - 10)
        await player.set_volume(new_vol)
        if cog:
            await cog._sync_panel(interaction.guild.id, player, interaction=interaction)

    async def volume_up(self, interaction: discord.Interaction, button: ui.Button | None = None):
        player: "wavelink_module.Player" = interaction.guild.voice_client
        cog: "Music" = self.cog or interaction.client.get_cog("Music")

        current_vol = getattr(player, "volume", 100)
        new_vol = min(100, current_vol + 10)
        await player.set_volume(new_vol)
        if cog:
            await cog._sync_panel(interaction.guild.id, player, interaction=interaction)

    async def toggle_loop(self, interaction: discord.Interaction, button: ui.Button | None = None):
        """在音樂控制面板直接循環切換 loop_mode（關閉 -> 單曲循環 -> 佇列循環 -> 關閉）。
        只修改 player.loop_mode 並原地刷新面板，絕不觸動 player.skip() 或當前播放曲目。
        """
        player: "wavelink_module.Player" = interaction.guild.voice_client
        cog: "Music" = self.cog or interaction.client.get_cog("Music")

        current_mode = getattr(player, "loop_mode", "off")
        new_mode = LOOP_NEXT.get(current_mode, "off")
        player.loop_mode = new_mode
        if cog:
            cog._set_panel_notice(
                interaction.guild.id,
                f"{LOOP_LABELS[new_mode]}（操作者：{interaction.user.display_name}）",
                duration=8.0,
            )
            await cog._sync_panel(interaction.guild.id, player, interaction=interaction)

    async def toggle_shuffle(self, interaction: discord.Interaction, button: ui.Button | None = None):
        """在音樂控制面板直接切換隨機播放模式（開啟 / 關閉）。
        開啟時自動將待播佇列（player.song_queue）順序打亂（不包含正在播放中的曲目）。
        """
        player: "wavelink_module.Player" = interaction.guild.voice_client
        cog: "Music" = self.cog or interaction.client.get_cog("Music")

        is_shuffle = bool(getattr(player, "shuffle", False))
        new_shuffle = not is_shuffle
        player.shuffle = new_shuffle

        shuffled_count = 0
        if new_shuffle and cog:
            shuffled_count = cog._shuffle_queue(player)

        if cog:
            if new_shuffle:
                notice = f"🔀 **已開啟隨機播放模式**：已將待播佇列中的 {shuffled_count} 首歌順序隨機打亂！（操作者：{interaction.user.display_name}）"
            else:
                notice = f"➡️ **已關閉隨機播放模式**：佇列將依照目前順序依序播放。（操作者：{interaction.user.display_name}）"
            cog._set_panel_notice(interaction.guild.id, notice, duration=8.0)
            await cog._sync_panel(interaction.guild.id, player, interaction=interaction)


# ==============================================================================
# 用戶個人歌單 UI 元件（分頁檢視、互動編輯、重新排序 Modal、刪除確認）
# ==============================================================================
def build_playlist_page_embed(
    playlist: dict[str, Any],
    page: int = 0,
    *,
    title_prefix: str = "🎵 個人歌單",
    extra_notice: str | None = None,
) -> tuple[discord.Embed, int, int]:
    """建構單頁 10 首歌曲的歌單 Embed，回傳 (embed, 校正後頁碼 0-based, 總頁數)。"""
    tracks: list[dict[str, Any]] = list(playlist.get("tracks") or [])
    total_tracks = len(tracks)
    total_pages = max(1, (total_tracks + PLAYLIST_PAGE_SIZE - 1) // PLAYLIST_PAGE_SIZE)
    safe_page = max(0, min(int(page), total_pages - 1))

    vis_label = "🌐 公開（同伺服器成員可直接選取）" if playlist.get("is_public") else "🔒 私人（僅自己或輸入分享碼可播放）"
    share_code = playlist.get("share_code") or "------"
    owner_name = playlist.get("owner_name") or "未知用戶"

    total_ms = sum(max(0, int(t.get("length") or 0)) for t in tracks if not t.get("is_stream"))
    total_dur_str = format_time(total_ms) if total_ms > 0 else "00:00"

    desc_lines = [
        f"**擁有者：** {owner_name}",
        f"**權限狀態：** {vis_label}",
        f"**🔑 分享碼：** `{share_code}`（可於 `/music_play` 的 `playlist` 參數輸入此碼跨服播放並自動收藏）",
        f"**曲目數量：** `{total_tracks} / {PLAYLIST_MAX_TRACKS}` 首（總時長約 `{total_dur_str}`）",
    ]
    if extra_notice:
        desc_lines.append(f"\n{extra_notice}")
    desc_lines.append("")

    if not tracks:
        desc_lines.append("📭 *目前歌單內還沒有任何歌曲，可使用 `/music_playlist_add` 或 `/music_playlist_sync` 新增！*")
    else:
        start_idx = safe_page * PLAYLIST_PAGE_SIZE
        page_slice = tracks[start_idx : start_idx + PLAYLIST_PAGE_SIZE]
        desc_lines.append(f"**📜 曲目列表（第 {safe_page + 1} / {total_pages} 頁）：**")
        for offset, item in enumerate(page_slice, start=start_idx + 1):
            t_title = str(item.get("title") or "未知曲目")
            t_uri = item.get("uri")
            t_author = str(item.get("author") or "未知")
            t_len = format_duration(item.get("length"))
            link_title = f"[{t_title}]({t_uri})" if t_uri else t_title
            desc_lines.append(f"`{offset:02d}.` {link_title} — *{t_author}*（{t_len}）")

    embed = discord.Embed(
        title=f"{title_prefix}：{playlist.get('name', '未命名歌單')}",
        description="\n".join(desc_lines),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=f"第 {safe_page + 1} / {total_pages} 頁・每頁顯示 {PLAYLIST_PAGE_SIZE} 首")
    return embed, safe_page, total_pages


class PlaylistPaginationView(ui.View):
    """/music_playlist 分頁檢視器（每頁顯示 10 首）。"""

    def __init__(
        self,
        *args,
        user_id: int | None = None,
        playlist_owner_id: str | int | None = None,
        page: int = 0,
        guild_id: int | str | None = None,
        timeout: float = 180.0,
        **kwargs,
    ):
        super().__init__(timeout=timeout)
        pos = list(args)
        if pos and not isinstance(pos[0], (int, str)):
            self.cog = pos.pop(0)
        elif pos and isinstance(pos[0], str) and not pos[0].isdigit():
            self.cog = pos.pop(0)
        else:
            self.cog = None

        if user_id is None:
            if pos:
                user_id = pos.pop(0)
            else:
                user_id = 0
        self.user_id = int(user_id)

        if playlist_owner_id is None:
            if pos and isinstance(pos[0], (int, str)) and (str(pos[0]).isdigit() or len(str(pos[0])) > 10):
                playlist_owner_id = pos.pop(0)
            else:
                playlist_owner_id = self.user_id
        self.playlist_owner_id = str(playlist_owner_id)

        if pos:
            page = pos.pop(0)
        self.page = int(page)

        if guild_id is None:
            guild_id = kwargs.get("guild_id")
        if guild_id is None and pos:
            guild_id = pos.pop(0)
        self.guild_id = guild_id

        self._sync_buttons()

    def _load_playlist(self) -> dict[str, Any] | None:
        return get_user_playlist(self.playlist_owner_id)

    def _sync_buttons(self, playlist: dict[str, Any] | None = None) -> tuple[discord.Embed | None, int, int]:
        pl = playlist if playlist is not None else self._load_playlist()
        self.clear_items()
        if pl is None:
            return None, 0, 1
        embed, safe_page, total_pages = build_playlist_page_embed(pl, self.page)
        self.page = safe_page

        prev_btn = ui.Button(
            label="◀️ 上一頁",
            style=discord.ButtonStyle.secondary,
            disabled=(safe_page <= 0),
        )
        page_indicator = ui.Button(
            label=f"第 {safe_page + 1} / {total_pages} 頁",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        next_btn = ui.Button(
            label="▶️ 下一頁",
            style=discord.ButtonStyle.secondary,
            disabled=(safe_page >= total_pages - 1),
        )
        prev_btn.callback = self._on_prev
        next_btn.callback = self._on_next
        self.add_item(prev_btn)
        self.add_item(page_indicator)
        self.add_item(next_btn)

        try:
            panel_btn = build_panel_link_button(
                getattr(self, "guild_id", None),
                personal=True,
            )
        except TypeError:
            try:
                panel_btn = build_panel_link_button(
                    getattr(self, "guild_id", None),
                )
            except Exception:
                panel_btn = None
        if panel_btn is not None:
            self.add_item(panel_btn)
        return embed, safe_page, total_pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 只有執行此指令的使用者可以翻頁。", ephemeral=True)
            return False
        return True

    async def _on_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        embed, _, _ = self._sync_buttons()
        if embed is None:
            try:
                del_view = build_panel_link_view(getattr(self, "guild_id", None), personal=True)
            except TypeError:
                del_view = build_panel_link_view(getattr(self, "guild_id", None))
            await interaction.response.edit_message(
                content="ℹ️ 此歌單已被刪除。",
                embed=None,
                view=del_view,
            )
            return
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_next(self, interaction: discord.Interaction):
        self.page += 1
        embed, _, _ = self._sync_buttons()
        if embed is None:
            try:
                del_view = build_panel_link_view(getattr(self, "guild_id", None), personal=True)
            except TypeError:
                del_view = build_panel_link_view(getattr(self, "guild_id", None))
            await interaction.response.edit_message(
                content="ℹ️ 此歌單已被刪除。",
                embed=None,
                view=del_view,
            )
            return
        await interaction.response.edit_message(embed=embed, view=self)


class PlaylistRenameModal(ui.Modal, title="修改歌單名稱"):
    new_name = ui.TextInput(
        label="新歌單名稱（最多 50 字）",
        placeholder="請輸入新的歌單名稱...",
        min_length=1,
        max_length=50,
        required=True,
    )

    def __init__(self, edit_view: "PlaylistEditView", current_name: str = ""):
        super().__init__()
        self.edit_view = edit_view
        if current_name:
            self.new_name.default = current_name[:50]

    async def on_submit(self, interaction: discord.Interaction):
        name_val = self.new_name.value.strip()
        if not name_val:
            await interaction.response.send_message("❌ 歌單名稱不可為空。", ephemeral=True)
            return
        updated = update_user_playlist(
            self.edit_view.user_id,
            name=name_val,
            owner_name=interaction.user.display_name,
        )
        if updated is None:
            await interaction.response.edit_message(content="❌ 找不到歌單，可能已被刪除。", embed=None, view=None)
            return
        embed = self.edit_view.refresh_items(updated, notice=f"✅ 已將歌單重新命名為「**{updated['name']}**」。")
        await interaction.response.edit_message(embed=embed, view=self.edit_view)


class PlaylistReorderModal(ui.Modal, title="重新排序歌單曲目"):
    from_pos = ui.TextInput(
        label="原歌曲序號（1 ~ 總首數）",
        placeholder="例如：5（代表第 5 首歌）",
        min_length=1,
        max_length=3,
        required=True,
    )
    to_pos = ui.TextInput(
        label="移至新位置序號（1 ~ 總首數）",
        placeholder="例如：1（代表移到第 1 首）",
        min_length=1,
        max_length=3,
        required=True,
    )

    def __init__(self, edit_view: "PlaylistEditView"):
        super().__init__()
        self.edit_view = edit_view

    async def on_submit(self, interaction: discord.Interaction):
        pl = get_user_playlist(self.edit_view.user_id)
        if pl is None:
            await interaction.response.edit_message(content="❌ 找不到歌單，可能已被刪除。", embed=None, view=None)
            return
        total = len(pl.get("tracks") or [])
        if total < 2:
            await interaction.response.send_message("ℹ️ 歌單至少要有 2 首歌才能重新排序。", ephemeral=True)
            return
        try:
            src_1 = int(self.from_pos.value.strip())
            dst_1 = int(self.to_pos.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ 序號必須是整數數字（例如 `1`、`5`）。", ephemeral=True)
            return
        if not (1 <= src_1 <= total) or not (1 <= dst_1 <= total):
            await interaction.response.send_message(
                f"❌ 序號超出範圍！請輸入 `1` 到 `{total}` 之間的數字。", ephemeral=True
            )
            return
        moved_title = pl["tracks"][src_1 - 1].get("title", "未知曲目")
        updated = reorder_track_in_user_playlist(self.edit_view.user_id, src_1 - 1, dst_1 - 1)
        if updated is None:
            await interaction.response.send_message("❌ 重新排序失敗。", ephemeral=True)
            return
        self.edit_view.page = (dst_1 - 1) // PLAYLIST_PAGE_SIZE
        embed = self.edit_view.refresh_items(
            updated,
            notice=f"🔀 已將「**{moved_title}**」從第 `{src_1}` 首移至第 `{dst_1}` 首。",
        )
        await interaction.response.edit_message(embed=embed, view=self.edit_view)


class PlaylistEditView(ui.View):
    """/music_playlist_edit 互動式歌單編輯面板（支援刪除歌曲、重新排序、切換公開／私人、重新命名、重產分享碼）。"""

    def __init__(
        self,
        *args,
        user_id: int | None = None,
        page: int = 0,
        notice: str | None = None,
        timeout: float = 300.0,
        **kwargs,
    ):
        super().__init__(timeout=timeout)
        pos = list(args)
        if pos and not isinstance(pos[0], (int, str)):
            self.cog = pos.pop(0)
        elif pos and isinstance(pos[0], str) and not pos[0].isdigit():
            self.cog = pos.pop(0)
        else:
            self.cog = None

        if user_id is None:
            if pos:
                user_id = pos.pop(0)
            else:
                user_id = 0
        self.user_id = int(user_id)

        if pos:
            page = pos.pop(0)
        self.page = int(page)

        if notice is None and pos:
            notice = str(pos.pop(0))

        self.last_embed: discord.Embed | None = None
        pl = get_user_playlist(self.user_id)
        if pl is not None:
            self.refresh_items(pl, notice=notice)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 你只能編輯自己的歌單。", ephemeral=True)
            return False
        return True

    def refresh_items(self, playlist: dict[str, Any], notice: str | None = None) -> discord.Embed:
        self.clear_items()
        embed, safe_page, total_pages = build_playlist_page_embed(
            playlist,
            self.page,
            title_prefix="🛠️ 編輯歌單",
            extra_notice=notice,
        )
        self.page = safe_page
        self.last_embed = embed
        tracks: list[dict[str, Any]] = list(playlist.get("tracks") or [])

        # Row 0: 本頁歌曲快速刪除下拉選單
        if tracks:
            start_idx = safe_page * PLAYLIST_PAGE_SIZE
            page_slice = tracks[start_idx : start_idx + PLAYLIST_PAGE_SIZE]
            options = [
                discord.SelectOption(
                    label=f"{idx}. {str(t.get('title') or '未知曲目')[:80]}",
                    description=f"作者：{str(t.get('author') or '未知')[:45]}｜點擊從歌單移除",
                    value=str(idx - 1),
                    emoji="🗑️",
                )
                for idx, t in enumerate(page_slice, start=start_idx + 1)
            ]
            del_select = ui.Select(
                placeholder="🗑️ 選擇本頁歌曲以從歌單刪除...",
                min_values=1,
                max_values=1,
                options=options,
                row=0,
            )
            del_select.callback = self._on_delete_select
            self.add_item(del_select)

        # Row 1: 重新排序、切換公開/私人、修改名稱、重新產生分享碼
        reorder_btn = ui.Button(
            label="🔀 重新排序",
            style=discord.ButtonStyle.primary,
            disabled=(len(tracks) < 2),
            row=1,
        )
        is_pub = bool(playlist.get("is_public"))
        vis_btn = ui.Button(
            label="🔒 改為私人" if is_pub else "🌐 設為公開",
            style=discord.ButtonStyle.secondary if is_pub else discord.ButtonStyle.success,
            row=1,
        )
        rename_btn = ui.Button(
            label="✏️ 修改名稱",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        regen_btn = ui.Button(
            label="🔄 重產分享碼",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        reorder_btn.callback = self._on_open_reorder
        vis_btn.callback = self._on_toggle_public
        rename_btn.callback = self._on_open_rename
        regen_btn.callback = self._on_regenerate_code
        self.add_item(reorder_btn)
        self.add_item(vis_btn)
        self.add_item(rename_btn)
        self.add_item(regen_btn)

        # Row 2: 分頁按鈕（當超過 1 頁時）
        if total_pages > 1:
            prev_btn = ui.Button(
                label="◀️ 上一頁",
                style=discord.ButtonStyle.secondary,
                disabled=(safe_page <= 0),
                row=2,
            )
            page_btn = ui.Button(
                label=f"第 {safe_page + 1} / {total_pages} 頁",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=2,
            )
            next_btn = ui.Button(
                label="▶️ 下一頁",
                style=discord.ButtonStyle.secondary,
                disabled=(safe_page >= total_pages - 1),
                row=2,
            )
            prev_btn.callback = self._on_prev
            next_btn.callback = self._on_next
            self.add_item(prev_btn)
            self.add_item(page_btn)
            self.add_item(next_btn)

        return embed

    async def _on_delete_select(self, interaction: discord.Interaction):
        values = interaction.data.get("values") if isinstance(interaction.data, dict) else None
        if not values:
            await interaction.response.defer()
            return
        idx_0 = int(values[0])
        updated, removed = remove_track_from_user_playlist(self.user_id, idx_0)
        if updated is None:
            await interaction.response.edit_message(content="❌ 找不到歌單，可能已被刪除。", embed=None, view=None)
            return
        rm_title = removed.get("title", "未知曲目") if removed else "指定曲目"
        embed = self.refresh_items(updated, notice=f"🗑️ 已從歌單移除第 `{idx_0 + 1}` 首：「**{rm_title}**」。")
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_open_reorder(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PlaylistReorderModal(self))

    async def _on_toggle_public(self, interaction: discord.Interaction):
        pl = get_user_playlist(self.user_id)
        if pl is None:
            await interaction.response.edit_message(content="❌ 找不到歌單，可能已被刪除。", embed=None, view=None)
            return
        new_pub = not bool(pl.get("is_public"))
        updated = update_user_playlist(
            self.user_id,
            is_public=new_pub,
            owner_name=interaction.user.display_name,
        )
        if updated is None:
            await interaction.response.edit_message(content="❌ 更新失敗。", embed=None, view=None)
            return
        state_str = "🌐 **公開**（同伺服器成員可直接在 `/music_play` 選單看見）" if new_pub else "🔒 **私人**（僅自己或透過分享碼可播放）"
        embed = self.refresh_items(updated, notice=f"✅ 歌單權限已切換為：{state_str}")
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_open_rename(self, interaction: discord.Interaction):
        pl = get_user_playlist(self.user_id)
        cur_name = pl.get("name", "") if pl else ""
        await interaction.response.send_modal(PlaylistRenameModal(self, current_name=cur_name))

    async def _on_regenerate_code(self, interaction: discord.Interaction):
        updated = regenerate_user_playlist_share_code(self.user_id)
        if updated is None:
            await interaction.response.edit_message(content="❌ 找不到歌單，可能已被刪除。", embed=None, view=None)
            return
        embed = self.refresh_items(
            updated,
            notice=f"🔄 已重新產生分享碼為 `{updated['share_code']}`（舊分享碼已立即失效）。",
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        pl = get_user_playlist(self.user_id)
        if pl is None:
            await interaction.response.edit_message(content="❌ 找不到歌單。", embed=None, view=None)
            return
        embed = self.refresh_items(pl)
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_next(self, interaction: discord.Interaction):
        self.page += 1
        pl = get_user_playlist(self.user_id)
        if pl is None:
            await interaction.response.edit_message(content="❌ 找不到歌單。", embed=None, view=None)
            return
        embed = self.refresh_items(pl)
        await interaction.response.edit_message(embed=embed, view=self)


class PlaylistRemoveConfirmView(ui.View):
    """/music_playlist_remove 刪除歌單確認訊息 View。"""

    def __init__(
        self,
        *args,
        user_id: int | None = None,
        playlist_name: str = "",
        timeout: float = 60.0,
        **kwargs,
    ):
        super().__init__(timeout=timeout)
        pos = list(args)
        if pos and not isinstance(pos[0], (int, str)):
            self.cog = pos.pop(0)
        elif pos and isinstance(pos[0], str) and not pos[0].isdigit():
            self.cog = pos.pop(0)
        else:
            self.cog = None

        if user_id is None:
            if pos:
                user_id = pos.pop(0)
            else:
                user_id = 0
        self.user_id = int(user_id)

        if not playlist_name and pos:
            playlist_name = str(pos.pop(0))
        self.playlist_name = playlist_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 只有歌單擁有者本人可以確認刪除。", ephemeral=True)
            return False
        return True

    @ui.button(label="🗑️ 確認刪除歌單", style=discord.ButtonStyle.danger)
    async def confirm_delete(self, interaction: discord.Interaction, button: ui.Button):
        deleted = delete_user_playlist(self.user_id)
        self.stop()
        if deleted:
            await interaction.response.edit_message(
                content=f"🗑️ 已成功刪除歌單「**{self.playlist_name}**」（包含所有曲目與分享碼）。",
                embed=None,
                view=None,
            )
        else:
            await interaction.response.edit_message(
                content="ℹ️ 找不到該歌單，可能已經被刪除了。",
                embed=None,
                view=None,
            )

    @ui.button(label="✖️ 取消", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, button: ui.Button):
        self.stop()
        await interaction.response.edit_message(
            content=f"ℹ️ 已取消刪除，歌單「**{self.playlist_name}**」保留不變。",
            embed=None,
            view=None,
        )


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # key: guild_id, value: 目前正在倒數「語音頻道空了要自動離開」的背景任務。
        # 一個伺服器同時最多只會有一個計時器在跑。
        self.empty_channel_timers: dict[int, asyncio.Task] = {}
        # 第 5 週新增：伺服器控制面板訊息追蹤 (guild_id -> (channel_id, message_id))，多伺服器狀態隔離
        self.panel_messages: dict[int, tuple[int, int]] = {}
        # 進行中的面板 interaction（例如切歌時先 defer，換曲事件抵達時以此回應）
        self._pending_panel_interaction: dict[int, discord.Interaction] = {}
        # key: 節點 identifier，value: 是否已經為「目前這次離線」發過通知。
        # 用來避免健康檢查每隔 NODE_HEALTH_CHECK_INTERVAL 秒就重複 DM 開發者，
        # 只在「狀態從連線變離線」的那一刻通知一次，恢復連線後才會重置旗標。
        self._node_alert_sent: dict[str, bool] = {}
        # Lavalink 連線狀態旗標（修正：開發者 DM 洗版 + 空 Pool 無法自動復原）：
        #   _lavalink_connecting          連線程序進行中，避免 cog_load 的背景連線與
        #                                  health check 的自動重連同時各開一條 WebSocket。
        #   _lavalink_failure_notified    這次「故障期間」是否已 DM 開發者過；連線成功即重置，
        #                                  讓每次故障只通知一次，而不是每輪重連都洗一次版。
        #   _lavalink_last_reconnect_attempt 上次自動重連的時間戳（monotonic），用來冷卻。
        self._lavalink_connecting = False
        self._lavalink_failure_notified = False
        self._lavalink_last_reconnect_attempt = 0.0
        # key: guild_id 字串，value: 點歌頻道 ID。
        # 只載入一次，之後修改時同步更新記憶體 + 檔案。
        self.music_channel_settings: dict = _load_music_settings()
        # 進行中的點歌者操作請求：key=點歌者 ID，value={locked, action, view}
        self._pending_requests: dict[int, dict] = {}
        # 進行中的停止投票：key=guild ID，value=StopVoteView
        self._stop_votes: dict[int, StopVoteView] = {}
        # 被拒絕過的曲目（同一首歌被拒後不得再提請求）：key=track identifier
        self._rejected_track_keys: dict = {}
        # 第 6 週新增：進度條渲染節流與 429 退避記錄
        # key: guild_id, value: 上次成功更新的進度條字串，進度字串沒變時跳過 edit
        self._last_progress_str: dict[int, str] = {}
        # key: guild_id, value: 觸發 429 暫停更新的截止時間戳（time.monotonic()）
        self._rate_limit_until: dict[int, float] = {}
        # 第 6 週新增：面板底部即時提示字串 (guild_id -> notice string) 與自動清除計時任務 (guild_id -> Task)
        self._panel_notices: dict[int, str] = {}
        self._panel_notice_tasks: dict[int, asyncio.Task] = {}
        # 控制面板防洗版自動置底（Sticky Panel）防抖計時任務與觸發訊息追蹤：
        # _panel_bump_tasks: key=guild_id, value=等待防抖結束後將面板移至頻道底部的 Task
        # _bump_due_at: key=guild_id, value=防抖倒數預計到期的 monotonic 時間戳（新訊息抵達時直接順延時間戳，不粗暴 cancel 執行中任務）
        # _bump_trigger_msgs: key=guild_id, value=面板發出後同頻道新增的普通訊息 ID 集合（若暫時訊息全數被刪除則取消置底）
        # _bumping_guilds: 正在執行 _bump_panel（發送新面板與刪除舊面板）的伺服器 ID 集合，防止中途被 cancel 導致舊面板漏刪
        # _sending_panel_guilds: 正在執行 channel.send 發送面板的伺服器 ID 集合，防止 Gateway 先收到 on_message 而誤觸置底
        # _known_panel_msg_ids: 記錄機器人發送過的面板訊息 ID，確保面板訊息本身絕不會觸發置底
        # _panel_locks: key=guild_id, value=asyncio.Lock，確保同一伺服器的面板發送、編輯與置底不會並發衝突產生雙面板
        self._panel_bump_tasks: dict[int, asyncio.Task] = {}
        self._bump_due_at: dict[int, float] = {}
        self._last_bump_at: dict[int, float] = {}
        self._bump_trigger_msgs: dict[int, set[int]] = {}
        self._bumping_guilds: set[int] = set()
        self._sending_panel_guilds: set[int] = set()
        self._known_panel_msg_ids: deque[int] = deque(maxlen=200)
        self._guild_panel_history: dict[int, set[tuple[int, int]]] = {}
        self._panel_locks: dict[int, asyncio.Lock] = {}
        # 第 7 週新增：Spotify 服務（整個 Cog 共用一個 aiohttp session）與各伺服器的背景匯入任務。
        # 任務必須存參照：asyncio 只對 Task 保留弱引用，沒人持有就可能被垃圾回收而中途消失。
        # key: guild_id，每個伺服器同時最多一個匯入任務；任務結束時會由 done callback 自行移除。
        self.spotify = SpotifyClient(os.getenv('SPOTIFY_CLIENT_ID'), os.getenv('SPOTIFY_CLIENT_SECRET'))
        self._import_tasks: dict[int, asyncio.Task] = {}
        # Lavalink Session 失效自動復原：key=節點 identifier，value=開始重置的 monotonic 時間戳。
        # 用來避免多個按鈕／事件同時偵測到 404 時重複關閉 WebSocket；逾時（SESSION_RESET_TIMEOUT）後允許再次重置。
        self._session_resetting: dict[str, float] = {}
        # 曲目結束看門狗（Track-End Watchdog）：key=guild_id, value=(track, 首次偵測到 pos>=length 的 monotonic 時間戳)
        self._track_overdue_since: dict[int, tuple[Any, float]] = {}

    async def cog_unload(self):
        """Cog 被卸載時（例如重新載入模組）順便取消所有還在跑的自動離開計時器，
        避免計時器任務變成孤兒，之後莫名其妙把機器人踢出語音頻道。
        """
        # 第 7 週新增：取消所有背景匯入任務並關閉 Spotify 的 HTTP session，避免任務與連線殘留。
        for gid in list(self._import_tasks.keys()):
            self._cancel_import(gid)
        await self.spotify.close()
        for task in self.empty_channel_timers.values():
            if not task.done():
                task.cancel()
        self.empty_channel_timers.clear()
        # 重載後按鈕 View 的 timer 會跟著舊模組消失，狀態一併清掉，避免殘留鎖
        self._pending_requests.clear()
        self._stop_votes.clear()
        self._rejected_track_keys.clear()
        self.panel_messages.clear()
        self._pending_panel_interaction.clear()
        # 第 6 週新增：取消進度條背景迴圈並清空節流快取
        if self.progress_update_loop.is_running():
            self.progress_update_loop.cancel()
        self._last_progress_str.clear()
        self._rate_limit_until.clear()
        self._track_overdue_since.clear()

        # 取消所有面板提示自動清除任務
        for task in self._panel_notice_tasks.values():
            if not task.done():
                task.cancel()
        self._panel_notice_tasks.clear()
        self._panel_notices.clear()

        # 取消所有面板自動置底防抖任務
        for task in self._panel_bump_tasks.values():
            if not task.done():
                task.cancel()
        self._panel_bump_tasks.clear()
        self._bump_due_at.clear()
        self._last_bump_at.clear()
        self._bump_trigger_msgs.clear()
        self._bumping_guilds.clear()
        self._sending_panel_guilds.clear()
        self._known_panel_msg_ids.clear()
        self._guild_panel_history.clear()
        self._panel_locks.clear()

        if self.node_health_check.is_running():
            self.node_health_check.cancel()

        # 徹底清理 wavelink.Pool.nodes 中的節點與 session，避免模組重載（reload）時
        # 殘留幽靈節點或 session 衝突，解決「重啟機器人正常但重整模組無法解決」的問題。
        if wavelink is not None:
            pool_nodes = getattr(wavelink.Pool, "nodes", {})
            for node in list(pool_nodes.values()):
                try:
                    await node.close(eject=True)
                except Exception as exc:
                    print(f"⚠️ 卸載模組時關閉 Lavalink 節點失敗：{exc}")
            try:
                pool_nodes.clear()
            except Exception:
                pass

    async def cog_load(self):
        """Cog 被 bot.load_extension() 載入時自動呼叫一次，建立節點連線池。

        修正: 連線 Lavalink 改成背景任務執行，不要在這裡 await 完成連線。
        wavelink.Pool.connect() 在節點連不上時可能會卡住重試很久，如果直接
        await 會讓 setup_hook() 整個卡死，導致後面的 tree.sync() 跟 on_ready
        都永遠跑不到（機器人網頁的 bot-stats 也會因此一直是 0）。改成
        create_task 丟到背景後，cog_load() 會立刻返回，其餘啟動流程不受影響；
        另外加上逾時保護，避免背景任務本身無限期卡住。
        """
        # 第 5 週新增：註冊持久化控制面板 View，讓 Bot 重開機後舊按鈕仍可被派發處理
        self.bot.add_view(MusicControlView(self))
        # 第 6 週新增：啟動進度條定時更新背景迴圈
        if not self.progress_update_loop.is_running():
            self.progress_update_loop.start()
        if wavelink is None:
            print("⚠️ 尚未安裝 wavelink，Music Cog 的 /join /leave /play 將無法運作。"
                  "請先執行 pip install wavelink 並設定 Lavalink 節點。")
            return

        # 不論 LAVALINK_URI 現在有沒有設定，都先把健康檢查背景任務跑起來，
        # 這樣之後如果補設定、重新連線，也能持續監控節點狀態。
        if not self.node_health_check.is_running():
            self.node_health_check.start()

        lavalink_uri = os.getenv('LAVALINK_URI')
        lavalink_password = os.getenv('LAVALINK_PASSWORD')

        if not lavalink_uri or not lavalink_password:
            print("⚠️ 環境變數 LAVALINK_URI / LAVALINK_PASSWORD 未設定或為空，"
                  "已略過 Lavalink 節點連線。請檢查 .env 內容，例如：\n"
                  "   LAVALINK_URI=http://lavalink.jirayu.net:13592\n"
                  "   LAVALINK_PASSWORD=youshallnotpass")
            return

        # 不 await，丟到背景執行，讓 cog_load() 立刻返回
        asyncio.create_task(self._connect_lavalink(lavalink_uri, lavalink_password))
        print(f"⏳ 已在背景開始連線 Lavalink 節點：{lavalink_uri}（不會阻擋機器人其他功能啟動）")

    async def _connect_lavalink(self, lavalink_uri: str, lavalink_password: str):
        """連接單一 Lavalink 節點；失敗時診斷、清理並回報（同一故障期間只 DM 一次）。

        實測發現的三個坑，都在這個函式裡處理：
        1. wavelink 的 Pool.connect 在節點連不上時多半「不丟例外」，只寫它自己的
           logger 然後回傳空 dict，所以不能把「沒拋例外」當成連線成功。
        2. 外層 wait_for 逾時拿到的 TimeoutError 通常沒有任何訊息（str(e) 為空），
           開發者收到的 DM「錯誤訊息」欄位就是空白的 → 刻意改丟帶訊息的 TimeoutError，
           並在回報前先探測節點，把真實原因（如 https:// 指到只講明文 HTTP 的節點）寫進去。
        3. 失敗的節點不會被 wavelink 清掉，可能殘留 DISCONNECTED 節點與沒關的
           aiohttp session → 主動 node.close() 清理。
        """
        if self._lavalink_connecting:
            print(f"ℹ️ 已有 Lavalink 連線程序在進行中，略過重複連線：{lavalink_uri}")
            return

        self._lavalink_connecting = True

        # 檢查 Pool 是否已有同 URI 且處於 CONNECTED 的健康節點，避免重複連線導致 session 衝突；
        # 若有舊節點處於離線/異常狀態，先主動關閉並逐出 Pool，避免累積幽靈節點。
        if wavelink is not None:
            existing_nodes = getattr(wavelink.Pool, "nodes", {})
            for existing_id, existing_node in list(existing_nodes.items()):
                if getattr(existing_node, "uri", None) == lavalink_uri:
                    if getattr(getattr(existing_node, "status", None), "name", None) == "CONNECTED":
                        print(f"ℹ️ 已有同 URI 且處於 CONNECTED 的節點（{existing_id}），略過重複建立。")
                        self._lavalink_connecting = False
                        self._lavalink_failure_notified = False
                        return
                    else:
                        print(f"🧹 清理處於非連線狀態的舊節點：{existing_id}")
                        await self._discard_failed_node(existing_node)

        node = wavelink.Node(uri=lavalink_uri, password=lavalink_password)
        error: Exception | None = None
        try:
            await asyncio.wait_for(
                wavelink.Pool.connect(nodes=[node], client=self.bot),
                timeout=LAVALINK_CONNECT_TIMEOUT,
            )
            # 關鍵檢查：Pool.connect 不拋例外 ≠ 連線成功，節點真的被註冊進 Pool 才算數。
            if node.identifier in wavelink.Pool.nodes:
                print(f"🎧 已成功連線至 Lavalink 節點：{lavalink_uri}")
                self._lavalink_failure_notified = False  # 恢復正常，下次故障才會重新通知
                return
            error = ConnectionError(
                f"Lavalink 節點連線被 wavelink 拒絕（Pool.connect 未註冊節點）：{lavalink_uri}。"
                "常見原因：密碼錯誤、節點不是 Lavalink v4、或反向代理未轉發 WebSocket 升級；"
                "wavelink 只把細節寫進它自己的 logger，請一併查看後台日誌。"
            )
            print(f"❌ {error}")
        except asyncio.TimeoutError:
            # 修正：先前直接丟原始 TimeoutError（無 args），DM 的「錯誤訊息」欄位是空白的。
            error = TimeoutError(
                f"Lavalink 節點連線逾時（超過 {LAVALINK_CONNECT_TIMEOUT:.0f} 秒）：{lavalink_uri}"
            )
            print(
                f"❌ 連線 Lavalink 節點逾時（超過 {LAVALINK_CONNECT_TIMEOUT:.0f} 秒）：{lavalink_uri}，"
                "語音功能（/join /leave /play）可能暫時無法使用，但不影響機器人其他功能。"
            )
        except Exception as e:
            # Lavalink 節點若尚未啟動，這裡會失敗；先印出訊息，不讓整個 Bot 崩潰。
            error = e
            print(f"❌ 連線 Lavalink 節點失敗：{e}")
            print("   請確認 Lavalink 是否已啟動，以及 LAVALINK_URI / LAVALINK_PASSWORD 是否正確。")
        finally:
            self._lavalink_connecting = False

        # 失敗路徑：清掉沒註冊成功的節點（WebSocket 退避重試、aiohttp session、Pool 殘留）。
        await self._discard_failed_node(node)
        await self._report_lavalink_failure(error, lavalink_uri, lavalink_password)

    async def _discard_failed_node(self, node: "wavelink_module.Node") -> None:
        """清理連線失敗的節點，避免殘留 DISCONNECTED 節點與沒關閉的 aiohttp session。

        wavelink 的 Pool.connect 失敗時不會幫忙善後（節點可能根本沒進 Pool，
        但 _connect 已經開了 ClientSession 與 WebSocket 重試），必須自己收掉，
        否則每次重連都會多一條漏掉的 session。
        """
        if wavelink is None:
            return
        try:
            await node.close(eject=True)  # eject=True：同時從 Pool 移除（若有的話）
        except Exception as exc:
            print(f"⚠️ 關閉失敗的 Lavalink 節點時發生錯誤（不影響主流程）：{type(exc).__name__}: {exc}")
        session = getattr(node, '_session', None)
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception as exc:
                print(f"⚠️ 關閉 Lavalink aiohttp session 失敗：{type(exc).__name__}: {exc}")

    async def _report_lavalink_failure(
        self, error: Exception, lavalink_uri: str, lavalink_password: str
    ) -> None:
        """連線失敗的統一回報：後台 log + 節點探測診斷 + DM 開發者（每輪故障只 DM 一次）。

        診斷會主動打一次 {uri}/version（https:// 失敗時自動改探 http:// 並比對），
        把 wavelink 刻意吞掉的真實原因（TLS 握手失敗、密碼錯誤、節點離線…）
        翻譯成一句能直接照做的處置建議。訊息不帶密碼，回報前 main.py 還會再過一層
        redact_secrets；診斷本身絕不拋例外，失敗時只降級成 log。
        """
        try:
            diagnosis = await diagnose_lavalink_node(lavalink_uri, lavalink_password)
        except Exception as exc:  # 防禦性：診斷函式設計上不拋例外，但回報流程不能被它炸掉
            diagnosis = f"（診斷過程發生錯誤：{type(exc).__name__}: {exc}）"
        print(f"🔍 Lavalink 連線診斷：{diagnosis}")

        if self._lavalink_failure_notified:
            # 重連冷卻期間每輪都會走到這裡，只記 log，不重複 DM 開發者。
            print("ℹ️ 此次故障已通知過開發者，本次僅記錄後台日誌，不重複發送 DM。")
            return
        self._lavalink_failure_notified = True
        await self.bot.notify_owner_error(
            error,
            extra_info=f"Lavalink 節點連線失敗：{lavalink_uri}\n🔍 診斷：{diagnosis}",
        )

    async def _reconnect_lavalink_if_needed(self) -> None:
        """Pool 為空時自動重連（health check 呼叫；受 LAVALINK_RECONNECT_COOLDOWN 冷卻保護）。

        舊版 health check 只遍歷「已註冊」的節點，Pool 是空的（初次連線失敗、
        節點被剔除）時等於完全沒監控，音樂功能會一路壞到重新啟動。
        """
        if wavelink is None or self._lavalink_connecting:
            return  # 連線中（例如 cog_load 的背景連線還沒結果），不要開第二條 WebSocket
        lavalink_uri = os.getenv('LAVALINK_URI')
        lavalink_password = os.getenv('LAVALINK_PASSWORD')
        if not lavalink_uri or not lavalink_password:
            return  # 沒設定節點 → 維持原本「略過連線」的行為，不重複印警告
        now = time.monotonic()
        if now - self._lavalink_last_reconnect_attempt < LAVALINK_RECONNECT_COOLDOWN:
            return
        self._lavalink_last_reconnect_attempt = now
        print(f"🔁 Lavalink 節點不在 Pool 中，嘗試自動重連（冷卻 {LAVALINK_RECONNECT_COOLDOWN:g} 秒）：{lavalink_uri}")
        await self._connect_lavalink(lavalink_uri, lavalink_password)

    # ---------- Lavalink Session 失效自動復原 ----------
    # 症狀：歌曲播到一半（或剛好播完）後完全沒聲音、不會自動換下一首，所有面板按鈕都噴
    #   LavalinkException: status=404, path=/v4/sessions/<id>/players/<guild>
    # 根因：Lavalink v4 的 PATCH /players/<guild> 在 player 不存在時會「自動建立」，所以 404 代表的是
    #   「整個 Session 已不存在」（公開節點重啟、Session 超過 resume_timeout 被回收、WebSocket 半開未被偵測…）。
    #   Session 一死，Lavalink 不會再送 TrackEndEvent → on_wavelink_track_end 永遠不會觸發 → 佇列卡住；
    #   而 wavelink 仍拿著舊 session_id 呼叫 REST，於是每顆按鈕都 404。只有重開機器人（重建 Session）才會恢復。
    # 解法：偵測到 Session 失效時，主動關閉舊 WebSocket 並清掉舊 session_id，讓 wavelink 內建重連取得全新 Session；
    #   收到 node_ready(resumed=False) 後，把每個 Player 的語音狀態重新送給 Lavalink，並從中斷點恢復播放
    #   （若中斷時歌曲其實已經播完，就直接走正常的換歌流程播下一首）。
    @staticmethod
    def _is_session_lost_error(error: BaseException | None) -> bool:
        """判斷例外是否為「Lavalink Session 已失效」（REST 404 且路徑包含 /sessions/）。"""
        seen: set[int] = set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            if getattr(error, "status", None) == 404:
                path = str(getattr(error, "path", "") or "") or str(error)
                if "/sessions/" in path:
                    return True
            error = getattr(error, "original", None) or error.__cause__
        return False

    async def _reset_lavalink_session(self, node: Any, reason: str = "") -> bool:
        """強制讓指定節點放棄失效的 Session 並重新連線（受 SESSION_RESET_TIMEOUT 去重保護）。

        回傳 True 代表這次確實觸發了重置；False 代表已有重置進行中或節點無效。
        """
        if node is None:
            return False
        ident = str(getattr(node, "identifier", None) or id(node))
        now = time.monotonic()
        started = self._session_resetting.get(ident)
        if started is not None and now - started < SESSION_RESET_TIMEOUT:
            return False
        self._session_resetting[ident] = now
        print(f"🔄 偵測到 Lavalink Session 失效，強制重新連線以取得新 Session（node={ident}）：{reason}")

        # 清掉舊 session_id：重連時不帶 Session-Id 標頭，Lavalink 會直接配發全新的 Session
        try:
            node._session_id = None
        except Exception:
            pass

        ws = getattr(node, "_websocket", None)
        socket = getattr(ws, "socket", None) if ws is not None else None
        try:
            if socket is not None and not socket.closed:
                # 關閉 socket 後 wavelink 的 keep_alive 會收到 CLOSING 訊息並自動呼叫 connect() 重連
                await socket.close()
            elif ws is not None:
                asyncio.create_task(ws.connect())
            else:
                asyncio.create_task(self._reconnect_lavalink_if_needed())
        except Exception as exc:
            print(f"⚠️ 強制重連 Lavalink 節點失敗（node={ident}）：{exc}")
            self._session_resetting.pop(ident, None)
            return False
        return True

    async def _handle_session_lost(self, player: Any, error: BaseException | None = None) -> None:
        """Session 失效的統一入口：找出 player 所屬節點並觸發重置；新 Session 就緒後由 on_wavelink_node_ready 恢復播放。

        若 player 綁定的是已不在 Pool 中的舊節點（例如模組重載後節點被重建，guild.voice_client 卻還拿著舊節點），
        重置舊節點沒有意義，改成直接把 player 掛到目前 Pool 中的節點並立即恢復。
        """
        if self._adopt_orphan_player(player):
            asyncio.create_task(self._recover_player_after_session_reset(player))
            return
        node = getattr(player, "node", None) if player is not None else None
        if node is None and wavelink is not None:
            nodes = list(getattr(wavelink.Pool, "nodes", {}).values())
            node = nodes[0] if nodes else None
        await self._reset_lavalink_session(node, reason=str(error) if error else "")

    def _adopt_orphan_player(self, player: Any, target_node: Any = None) -> bool:
        """把綁在「已不在 Pool 中的舊節點」上的 Player 重新掛到可用節點；回傳 True 代表有進行轉移。"""
        if player is None or wavelink is None:
            return False
        pool_nodes = list(getattr(wavelink.Pool, "nodes", {}).values())
        current_node = getattr(player, "_node", None)
        if current_node is not None and any(n is current_node for n in pool_nodes):
            return False
        if target_node is None:
            target_node = next(
                (n for n in pool_nodes if getattr(getattr(n, "status", None), "name", None) == "CONNECTED"), None
            )
        guild = getattr(player, "guild", None)
        if target_node is None or guild is None:
            return False
        try:
            player._node = target_node
            target_node._players[guild.id] = player
        except Exception as exc:
            print(f"⚠️ 轉移孤兒播放器到新節點失敗 (guild_id={guild.id})：{exc}")
            return False
        print(f"🔄 播放器綁定的 Lavalink 節點已失效，已轉移至新節點 {getattr(target_node, 'identifier', '?')} (guild_id={guild.id})")
        return True

    async def _sync_player_voice_to_lavalink(self, player: Any) -> bool:
        """將 Player 的 Discord 語音憑證（session_id / token / endpoint）重新註冊到 Lavalink 節點。

        刻意優先直接呼叫 `node._update_player(guild_id, data={"voice": ...})` 而非 `player._dispatch_voice_update()`，
        因為 wavelink 內建的 `_dispatch_voice_update()` 在遇到 LavalinkException（例如 404）時會強制呼叫
        `await self.disconnect()` 把機器人踢出 Discord 語音頻道；直接送 `_update_player` 則可在失敗時保留語音連線。
        """
        if player is None:
            return False
        guild = getattr(player, "guild", None)
        if guild is None:
            return False
        self._adopt_orphan_player(player)

        voice_state = getattr(player, "_voice_state", None)
        voice_data = voice_state.get("voice", {}) if isinstance(voice_state, dict) else {}
        session_id = voice_data.get("session_id")
        token = voice_data.get("token")
        endpoint = voice_data.get("endpoint")
        node = getattr(player, "node", None)

        if session_id and token and endpoint and node is not None and hasattr(node, "_update_player"):
            request: dict[str, Any] = {
                "voice": {
                    "sessionId": session_id,
                    "token": token,
                    "endpoint": endpoint,
                }
            }
            ch = getattr(player, "channel", None)
            if ch is not None and getattr(ch, "id", None) is not None:
                request["voice"]["channelId"] = str(ch.id)
            try:
                await node._update_player(guild.id, data=request)
                conn_ev = getattr(player, "_connection_event", None)
                if conn_ev is not None and hasattr(conn_ev, "set"):
                    conn_ev.set()
                return True
            except Exception as exc:
                print(f"⚠️ [Session 復原] 直接同步語音狀態至 Lavalink 失敗 (guild_id={guild.id})：{exc}")
                return False

        if hasattr(player, "_dispatch_voice_update"):
            try:
                await player._dispatch_voice_update()
                return bool(getattr(player, "connected", True))
            except Exception as exc:
                print(f"⚠️ [Session 復原] 重送語音狀態失敗 (guild_id={guild.id})：{exc}")
                return False
        return False

    @staticmethod
    def _is_live_track(track: Any) -> bool:
        """判斷曲目是否為直播串流（is_stream=True、不可跳轉、或時長 >= 24 小時 86,400,000 ms）。"""
        if track is None:
            return False
        if bool(getattr(track, "_is_live_stream", False)):
            return True
        if bool(getattr(track, "is_stream", False)):
            return True
        if not getattr(track, "is_seekable", True):
            return True
        length = getattr(track, "length", None)
        if length is not None:
            try:
                val = int(length)
                if val <= 0 or val >= 86_400_000:
                    return True
            except (ValueError, TypeError):
                pass
        return False

    async def _refresh_live_stream_track(self, track: Any) -> Any:
        """針對直播曲目重新向 Lavalink 解析最新串流憑證（解決直播 HLS 分段憑證過期導致自動結束的問題）。"""
        uri = getattr(track, "uri", None)
        if not uri:
            return track
        try:
            found = await asyncio.wait_for(self._fetch_tracks_with_fallback(str(uri)), timeout=10.0)
            if found:
                if wavelink is not None and isinstance(found, wavelink.Playlist):
                    return found.tracks[0] if found.tracks else track
                return found[0]
        except Exception as exc:
            print(f"⚠️ 重新解析直播串流網址失敗 ({uri})，沿用原曲目重試：{exc}")
        return track

    async def _recover_player_after_session_reset(self, player: Any) -> None:
        """新 Session 就緒後恢復單一 Player：重送語音狀態 → 從中斷點續播（已播完則直接換下一首）。"""
        guild = getattr(player, "guild", None)
        if guild is None:
            return
        guild_id = guild.id
        await self._sync_player_voice_to_lavalink(player)

        if not getattr(player, "connected", False):
            self._cancel_import(guild_id)
            await self._update_panel_ended(
                guild_id, reason="⚠️ 音訊節點連線重置後無法恢復語音連線，請重新使用 `/music_play` 點歌。"
            )
            return

        current = getattr(player, "_current", None)
        track = current or getattr(player, "_active_track", None)
        if track is None:
            return  # 閒置中的 player 只需恢復語音連線，不用播放

        length = int(getattr(track, "length", 0) or 0)
        is_stream = self._is_live_track(track)
        position = 0 if is_stream else (int(getattr(player, "position", 0) or 0) if current is not None else 0)

        try:
            if not is_stream and length > 0 and position >= length - 1500:
                # 中斷時這首其實已經播完（Lavalink 沒來得及送 TrackEndEvent）→ 模擬正常結束事件，走原本的換歌流程
                print(f"ℹ️ [Session 復原] 「{getattr(track, 'title', '?')}」中斷時已播完，直接換下一首 (guild_id={guild_id})")
                await self.on_wavelink_track_end(SimpleNamespace(player=player, reason="finished", track=track))
                return

            if is_stream:
                track = await self._refresh_live_stream_track(track)
                player._active_track = track

            print(
                f"▶️ [Session 復原] {'重新銜接直播' if is_stream else f'從 {format_position(position)} 恢復播放'}"
                f"「{getattr(track, 'title', '?')}」 (guild_id={guild_id})"
            )
            await player.play(
                track,
                replace=True,
                start=0 if is_stream else max(0, position),
                volume=getattr(player, "volume", 100),
                paused=bool(getattr(player, "paused", False)),
            )
            await self._notify_panel(guild_id, "🔄 音訊節點連線已自動恢復，繼續播放中。", duration=8.0)
        except Exception as exc:
            print(f"❌ [Session 復原] 恢復播放失敗 (guild_id={guild_id})：{exc}")
            if is_stream:
                print(f"ℹ️ [Session 復原] 直播「{getattr(track, 'title', '?')}」重連失敗，視為直播結束，不予報錯")
                await self.on_wavelink_track_end(SimpleNamespace(player=player, reason="loadfailed", track=track))
                return
            await self.bot.notify_owner_error(exc, extra_info=f"Lavalink Session 重置後恢復播放失敗 guild_id={guild_id}")
            await self._notify_panel(
                guild_id, "⚠️ 音訊節點連線已恢復，但無法接續播放，請按停止後重新點歌。", duration=15.0
            )

    # ---------- Lavalink 節點健康檢查 ----------
    @staticmethod
    def _describe_node_status(node: "wavelink_module.Node") -> tuple[str, str]:
        """把 wavelink Node 的 status 轉成 (狀態代碼字串, 給人看的中文標籤)。

        不同版本的 wavelink，NodeStatus 這個 Enum 是否存在、叫什麼名字都可能
        不一樣，這裡刻意不 import wavelink.NodeStatus 直接比對，而是用
        `getattr(status, "name", ...)` 取出字串再比對名稱，只要 Enum member
        名稱維持 CONNECTED / CONNECTING / DISCONNECTED 這幾個常見命名，不管是
        哪個版本都能正常運作；真的比對不到就顯示「未知」，不會讓指令或背景
        任務直接壞掉。
        """
        status = getattr(node, "status", None)
        if status is None:
            status_name = "UNKNOWN"
        else:
            status_name = getattr(status, "name", None) or str(status)

        labels = {
            "CONNECTED": "🟢 已連線",
            "CONNECTING": "🟡 連線中",
            "DISCONNECTED": "🔴 已離線",
        }
        return status_name, labels.get(status_name, f"❔ 未知（{status_name}）")

    def _is_any_node_connected(self) -> tuple[bool, str]:
        """檢查目前是否有任何 Lavalink 節點處於可用狀態（CONNECTED）。"""
        if wavelink is None:
            return False, "語音模組尚未載入或安裝完成。"
        nodes = getattr(wavelink.Pool, "nodes", {})
        if not nodes:
            return False, "目前沒有已連線的 Lavalink 音訊節點（節點未設定或連線中斷）。"
        connected = [
            n for n in nodes.values()
            if getattr(getattr(n, "status", None), "name", None) == "CONNECTED"
        ]
        if not connected:
            status_desc = ", ".join(f"{n.identifier} ({self._describe_node_status(n)[1]})" for n in nodes.values())
            return False, f"音訊伺服器目前離線中（狀態：{status_desc}），正在嘗試自動重連，請稍候再試！"
        return True, ""

    @tasks.loop(seconds=NODE_HEALTH_CHECK_INTERVAL)
    async def node_health_check(self):
        """新增：定期檢查所有已註冊 Lavalink 節點的連線狀態，離線時通知開發者。

        wavelink 本身斷線後會自動嘗試背景重連，不會丟一個明確的「節點斷線」
        事件給我們，所以改用 `tasks.loop` 每隔 NODE_HEALTH_CHECK_INTERVAL 秒
        主動檢查一次 `wavelink.Pool.nodes` 裡每個節點目前的 `status`。

        用 `self._node_alert_sent` 這個字典記錄「這次離線有沒有通知過」：
        狀態變成 DISCONNECTED 且還沒通知過，就 DM 開發者一次並把旗標設為
        True；之後只要還是離線，就不會每 30 秒洗一次版。等狀態恢復成
        CONNECTED，才把旗標重置回 False，下次再斷線才會重新觸發通知。
        """
        if wavelink is None:
            return

        try:
            nodes = wavelink.Pool.nodes
        except Exception as e:
            print(f">>> node_health_check 讀取節點清單失敗：{e}")
            return

        if not nodes:
            # Pool 是空的（初次連線失敗／節點被剔除）時，下面的遍歷什麼都不會做，
            # 等於故障期間完全沒有監控 → 改成在這裡受冷卻保護地自動重連。
            await self._reconnect_lavalink_if_needed()
            return

        for identifier, node in nodes.items():
            status_name, _ = self._describe_node_status(node)
            uri = getattr(node, "uri", identifier)

            if status_name == "DISCONNECTED":
                if not self._node_alert_sent.get(identifier):
                    self._node_alert_sent[identifier] = True
                    print(f"❌ 健康檢查偵測到 Lavalink 節點離線：{uri}（identifier={identifier}）")
                    synthetic_error = RuntimeError(f"Lavalink 節點離線：{uri}")
                    await self.bot.notify_owner_error(
                        synthetic_error,
                        extra_info=(
                            f"node_health_check 偵測到節點離線 identifier={identifier} uri={uri}，"
                            f"語音功能（/music_join /music_play 等）可能暫時無法使用。"
                        ),
                    )
            elif status_name == "CONNECTED" and self._node_alert_sent.get(identifier):
                # 節點恢復連線了，重置旗標，下次再斷線才會再通知一次。
                self._node_alert_sent[identifier] = False
                print(f"✅ Lavalink 節點已恢復連線：{uri}（identifier={identifier}）")

            # 主動探測 Session 是否仍然有效：WebSocket 看似 CONNECTED，但 Lavalink 端 Session 可能早已被回收
            # （公開節點重啟／半開連線），此時歌曲會靜默停止且不再送 TrackEndEvent。只在有 player 時探測，避免多餘請求。
            if status_name == "CONNECTED" and getattr(node, "players", None):
                try:
                    remote_players = await node.fetch_players()
                except Exception as exc:
                    if self._is_session_lost_error(exc):
                        await self._reset_lavalink_session(node, reason=f"health check 探測：{exc}")
                    else:
                        print(f">>> node_health_check 探測 Session 失敗（identifier={identifier}）：{exc}")
                else:
                    # Session 仍有效，但 Lavalink 端個別 player 已被銷毀（本地卻還在播）時，同樣進行單一 player 復原
                    remote_ids = {int(getattr(p, "guild_id", 0) or 0) for p in remote_players}
                    for gid, local_player in list(node.players.items()):
                        if gid not in remote_ids and getattr(local_player, "_current", None) is not None:
                            print(f"🔄 Lavalink 端已找不到 guild_id={gid} 的播放器，嘗試恢復播放")
                            asyncio.create_task(self._recover_player_after_session_reset(local_player))

    @node_health_check.before_loop
    async def before_node_health_check(self):
        # 等 bot 完全 ready（含 on_ready 觸發過一次）再開始跑，
        # 避免啟動初期 wavelink.Pool 還沒建立好就先檢查、白跑一次。
        await self.bot.wait_until_ready()

    # ---------- 第 6 週新增：全域播放面板進度條更新背景迴圈 ----------
    @tasks.loop(seconds=PROGRESS_UPDATE_INTERVAL)
    async def progress_update_loop(self):
        """第 6 週新增：單一全域背景迴圈，定時刷新所有播放中伺服器的 Embed 進度條。

        節流與 API Rate Limit 保護機制：
        1. 集中常數 PROGRESS_UPDATE_INTERVAL（預設 5 秒），避免頻繁呼叫 Discord API。
        2. 檢查 HTTP 429 退避時間戳（_rate_limit_until），在冷卻期間內跳過該伺服器。
        3. 若播放器暫停（player.paused），進度不變，直接跳過 edit。
        4. 語音頻道內沒有非機器人使用者時，跳過更新以省資源。
        5. 若計算出的進度條字串與上次完全相同（_last_progress_str），跳過 edit。
        6. 使用 partial message 編輯，省去 GET 訊息請求。
        7. 捕獲 discord.NotFound（訊息被刪）時清除並嘗試重新發送；捕獲 429 時依 retry_after 退避。
        """
        now = time.monotonic()
        guild_ids = list(self.panel_messages.keys())
        for guild_id in guild_ids:
            channel_id = None
            message_id = None
            player = None
            try:
                # 0. 若該伺服器正在進行面板置底、發送或持有面板鎖，跳過本輪刷新，避免與置底競爭產生雙面板
                if (
                    guild_id in self._bumping_guilds
                    or guild_id in self._sending_panel_guilds
                    or self._get_panel_lock(guild_id).locked()
                ):
                    continue

                # 1. 檢查 429 退避冷卻
                cooldown_until = self._rate_limit_until.get(guild_id, 0.0)
                if now < cooldown_until:
                    continue

                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue

                player = getattr(guild, "voice_client", None)
                # 若 player 不存在或未處於有效播放狀態，跳過
                if not self._is_playback_active(player):
                    self._track_overdue_since.pop(guild_id, None)
                    continue

                # 2. 播放器暫停時進度條無變化，跳過 edit 避免消耗 API 額度
                if getattr(player, "paused", False):
                    self._track_overdue_since.pop(guild_id, None)
                    continue

                track = player.current
                is_stream = self._is_live_track(track)
                pos_ms = getattr(player, "position", 0)
                len_ms = getattr(track, "length", None)

                # 曲目結束看門狗（Track-End Watchdog）：
                # wavelink.Player.position 是以本地 monotonic 時間推算並夾限在 track.length。
                # 當 Lavalink 節點在播放期間遺失 Session 或漏發 TrackEndEvent 時，本地進度會停在 100% (pos_ms >= len_ms)，
                # 但 on_wavelink_track_end 永遠不會被觸發，導致「當前歌曲播完卻沒自動播放下一首，且按按鈕全部 404」。
                # 若非直播曲目已達結尾 (pos_ms >= len_ms) 且持續超過 4 秒仍未收到 TrackEndEvent，主動觸發換歌流程。
                if (
                    not is_stream
                    and len_ms
                    and len_ms > 0
                    and pos_ms >= len_ms
                    and not getattr(player, "advancing", False)
                ):
                    overdue_info = self._track_overdue_since.get(guild_id)
                    now_mono = time.monotonic()
                    if overdue_info is None or overdue_info[0] is not track:
                        self._track_overdue_since[guild_id] = (track, now_mono)
                    elif now_mono - overdue_info[1] >= 4.0:
                        self._track_overdue_since.pop(guild_id, None)
                        print(
                            f"⚠️ [播放看門狗] 偵測到「{getattr(track, 'title', '?')}」已達曲目結尾但未收到 TrackEndEvent，"
                            f"自動觸發換歌流程 (guild_id={guild_id})"
                        )
                        await self.on_wavelink_track_end(
                            SimpleNamespace(player=player, reason="finished", track=track)
                        )
                        continue
                else:
                    self._track_overdue_since.pop(guild_id, None)

                # 3. 語音頻道是否有真人成員（排除機器人）
                vc = getattr(player, "channel", None)
                if vc is not None:
                    members = getattr(vc, "members", [])
                    if not any(not getattr(m, "bot", False) for m in members):
                        continue

                # 4. 取得當前進度字串並檢查是否與上次相同
                current_progress_str = build_progress_bar(pos_ms, len_ms, is_stream=is_stream)

                if self._last_progress_str.get(guild_id) == current_progress_str:
                    continue

                # 5. 取得面板資訊
                panel_info = self.panel_messages.get(guild_id)
                if not panel_info:
                    continue
                channel_id, message_id = panel_info

                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except Exception:
                        channel = None
                if channel is None:
                    continue

                if not self._is_playback_active(player) or self.panel_messages.get(guild_id) != (channel_id, message_id):
                    continue

                view = MusicControlView(
                    self,
                    player=player,
                    notice=self._panel_notices.get(guild_id),
                )
                view.refresh_buttons(player)

                # 6. 使用 partial_message 編輯，直接發送 PATCH 請求，省去 fetch_message 的 GET 請求
                msg = channel.get_partial_message(message_id)
                await msg.edit(content=None, embed=None, view=view)
                self._last_progress_str[guild_id] = current_progress_str

            except discord.NotFound:
                # 只有當記錄中的面板 ID 仍然是剛剛編輯失敗的那則 message_id，且仍在播放中時，才視為面板被刪除並重發
                if (
                    channel_id is not None
                    and message_id is not None
                    and self.panel_messages.get(guild_id) == (channel_id, message_id)
                ):
                    self.panel_messages.pop(guild_id, None)
                    self._last_progress_str.pop(guild_id, None)
                    if self._is_playback_active(player):
                        print(f"ℹ️ [進度條更新] 伺服器 {guild_id} 控制面板訊息已被刪除，嘗試重新發送面板...")
                        try:
                            await self._sync_panel(guild_id, player)
                        except Exception as exc:
                            print(f"⚠️ [進度條更新] 重新發送面板失敗 (guild_id={guild_id}): {exc}")
            except discord.HTTPException as exc:
                if getattr(exc, "status", None) == 429:
                    retry_after = float(getattr(exc, "retry_after", 5.0) or 5.0)
                    print(f"⚠️ [進度條更新] 伺服器 {guild_id} 觸發 Discord API Rate Limit (429)，將暫停更新 {retry_after:.1f} 秒")
                    self._rate_limit_until[guild_id] = time.monotonic() + retry_after
                else:
                    print(f"⚠️ [進度條更新] 伺服器 {guild_id} 遭遇 HTTPException (status={getattr(exc, 'status', None)}): {exc}")
            except Exception as exc:
                print(f"❌ [進度條更新] 伺服器 {guild_id} 發生未知異常：{exc}")

    @progress_update_loop.before_loop
    async def before_progress_update_loop(self):
        await self.bot.wait_until_ready()

    # ---------- wavelink 自訂事件 ----------
    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: Any):
        print(f"✅ Lavalink 節點已就緒：{payload.node.uri}（Session ID: {payload.session_id}）")
        node = payload.node
        self._session_resetting.pop(str(getattr(node, "identifier", None) or id(node)), None)
        if getattr(payload, "resumed", False):
            return  # Lavalink 成功恢復舊 Session，player 與播放狀態都還在，不需要處理
        # 模組重載後節點會被重建，但 guild.voice_client 仍綁在舊節點上 → 先把這些孤兒播放器轉移到新節點
        if wavelink is not None:
            for vc in list(getattr(self.bot, "voice_clients", []) or []):
                if isinstance(vc, wavelink.Player):
                    self._adopt_orphan_player(vc, target_node=node)
        # 拿到全新 Session：舊 Session 上的 player 全部不存在了，逐一恢復仍在語音頻道中的 Player
        players = list(getattr(node, "players", {}).values())
        if players:
            print(f"🔄 Lavalink 配發了新 Session，開始恢復 {len(players)} 個播放器的語音連線與播放狀態")
        for player in players:
            asyncio.create_task(self._recover_player_after_session_reset(player))

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: Any):
        """音軌開始播放事件（第 5 週新增）：在下指令的文字頻道產生或更新控制面板。"""
        player = getattr(payload, "player", None)
        if player is None or getattr(player, "guild", None) is None:
            return
        started_track = getattr(payload, "track", None)
        if started_track is not None:
            player._started_track = started_track
        await self._sync_panel(player.guild.id, player)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: Any):
        """實測發現的問題：player.play(track) 呼叫成功不代表歌曲真的能播放。

        Lavalink 是先接受請求、才非同步去源站（YouTube 等）載入音訊，載入失敗時是用
        這個事件回報，不會讓 /play 裡的 try/except 抓到，所以之前失敗時 Discord 上
        完全沒有任何提示，只有後台 log 印出一大串 Java stacktrace。這裡統一攔截、
        簡單判斷常見原因（YouTube 反爬蟲要求登入 / 地區限制），回報到最近一次下
        指令的文字頻道。
        """
        player = getattr(payload, "player", None)
        track = getattr(payload, "track", None)
        exception = getattr(payload, "exception", None)

        # Lavalink v4 的 exception 物件可能是 dict，也可能是有屬性的物件，兩種都相容處理。
        if isinstance(exception, dict):
            message = exception.get("message")
            cause = exception.get("cause")
            severity = exception.get("severity")
        else:
            message = getattr(exception, "message", None)
            cause = getattr(exception, "cause", None)
            severity = getattr(exception, "severity", None)

        track_title = getattr(track, "title", None) or "未知曲目"
        print(f"❌ Lavalink TrackException｜track={track_title} severity={severity} message={message} cause={cause}")

        # 直播防斷流與結束判斷：若為直播串流或重連中，直接視為直播已結束，絕不向開發者報錯亦不向頻道發送錯誤訊息
        is_live = (
            self._is_live_track(track)
            or self._is_live_track(getattr(player, "_active_track", None))
            or self._is_live_track(getattr(player, "current", None))
            or getattr(player, "_retrying_live_stream", False)
            or getattr(player, "_is_current_stream", False)
            or int(getattr(player, "_live_retry_count", 0) or 0) > 0
        )
        if is_live:
            print(f"ℹ️ [直播防斷流] 直播「{track_title}」重連失敗或串流已結束 (TrackException)，視為直播結束，不予報錯")
            if player is not None:
                player._retrying_live_stream = False
                player._live_retry_count = 3  # 標記重試上限，確保後續不再無效重試
                player._is_current_stream = False
                guild_obj = getattr(player, "guild", None)
                if guild_obj is not None and not getattr(player, "advancing", False):
                    await self.on_wavelink_track_end(SimpleNamespace(player=player, reason="loadfailed", track=track))
            return

        channel = getattr(player, "home_channel", None) if player else None
        if channel is None:
            return  # 沒有記錄到文字頻道（例如 Player 不是透過 /join 或 /play 建立），只留後台 log

        combined_text = f"{message or ''} {cause or ''}".lower()
        if "login" in combined_text or "sign in" in combined_text:
            reason_hint = (
                "\n🔍 研判原因：YouTube 判定這個請求需要登入驗證，疑似觸發了反爬蟲機制。"
                "這是已知的技術風險，規劃在第8週設定 poToken／OAuth2 來解決，目前這首歌先跳過。"
            )
        elif "not available" in combined_text or "region" in combined_text:
            reason_hint = "\n🔍 研判原因：這支影片可能有地區限制，換一個來源或影片試試看。"
        elif severity == "fault" or "invalid status code" in combined_text or " 404" in combined_text:
            # 實測發現：目前使用的是公開 Lavalink 節點，曾遇過 SoundCloud 串流連結
            # 直接回傳 404（節點端問題，不是我們程式碼的邏輯錯誤）。這正是計畫書裡
            # 「公開節點本質上不穩定」的實際案例，也是第8週要自架節點的理由之一。
            reason_hint = (
                "\n🔍 研判原因：這個音源的連結目前是壞的（來源站回傳錯誤），"
                "常見於目前使用的公開 Lavalink 節點不穩定，換一首歌或稍後再試看看。"
            )
        else:
            reason_hint = ""

        # 修正：先前只把錯誤印在後台跟發到文字頻道，開發者完全不會被 DM 通知。
        # severity 分三種：common（常見、預期內，例如格式不支援）、suspicious（可疑，
        # 通常是外部來源造成，例如 YouTube 反爬蟲）、fault（節點本身的問題）。
        # common 太常見也不算真的「未知錯誤」，故意不 DM 避免洗版；suspicious／fault
        # 才算是需要開發者留意的狀況，這兩種才會觸發 notify_owner_error。
        if severity in ("suspicious", "fault"):
            synthetic_error = RuntimeError(f"Lavalink TrackException（{severity}）：{message or cause or '未知錯誤'}")
            await self.bot.notify_owner_error(
                synthetic_error,
                extra_info=f"on_wavelink_track_exception track={track_title} severity={severity} cause={cause}",
            )

        # 第 7 週：失敗通知改顯示在面板 Container 底部（15 秒後自動消失），不再另外發訊息；
        # 只有該伺服器還沒有面板（例如第一首就載入失敗）時才退回頻道訊息，否則使用者完全看不到原因。
        fail_text = f"⚠️ 播放「{track_title}」失敗，Lavalink 節點無法載入這首歌曲。{reason_hint}"
        guild_obj = getattr(player, "guild", None)
        if guild_obj is not None and await self._notify_panel(guild_obj.id, fail_text, duration=15.0):
            return
        try:
            await channel.send(fail_text)
        except discord.HTTPException as e:
            print(f">>> 傳送 TrackException 通知訊息失敗: {e}")

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: Any):
        """當音軌（特別是長時間直播 HLS 分段）發生緩衝卡住時，自動重新銜接直播或推進播放。"""
        player = getattr(payload, "player", None)
        track = getattr(payload, "track", None) or getattr(player, "current", None) or getattr(player, "_active_track", None)
        if player is None or track is None:
            return
        guild_obj = getattr(player, "guild", None)
        guild_id = guild_obj.id if guild_obj else None
        if self._is_live_track(track):
            print(f"🔄 [直播防斷流] 偵測到直播「{getattr(track, 'title', '?')}」緩衝卡住 (TrackStuck)，自動重新銜接直播 (guild_id={guild_id})")
            fresh_track = await self._refresh_live_stream_track(track)
            requester_name = getattr(player, "current_requester", None) or "未知"
            requester_id = getattr(player, "current_requester_id", None)
            try:
                player._retrying_live_stream = True
                await self._play_track(player, fresh_track, requester_name, requester_id)
            except Exception as exc:
                print(f"ℹ️ [直播防斷流] TrackStuck 重新銜接直播失敗（視為直播結束）：{exc}")
                player._live_retry_count = 3
                player._is_current_stream = False
                await self.on_wavelink_track_end(SimpleNamespace(player=player, reason="loadfailed", track=track))
            finally:
                player._retrying_live_stream = False
            return
        # 一般非直播曲目若卡住且已接近結尾，直接觸發結束換下一首
        pos = int(getattr(player, "position", 0) or 0)
        length = int(getattr(track, "length", 0) or 0)
        if length > 0 and pos >= length - 3000:
            await self.on_wavelink_track_end(SimpleNamespace(player=player, reason="finished", track=track))

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: Any):
        """第3週：串接播放佇列（播完自動連播）。第4週：加入循環模式與 skip / stop 意圖判斷。

        因為 autoplay 已關閉，「歌曲結束 → 下一首」完全由這裡決定，所以循環模式也
        必須在這裡實作（不能用 wavelink 內建的 queue.mode，你的佇列是自己的 song_queue）。

        判斷依據：
        1. payload.reason：replaced / cleanup 代表曲目被取代或 player 正在銷毀，
           不能再接下一首；loadFailed 的曲目不參與循環，否則壞掉的歌會無限重試。
        2. player.end_intent：/music_skip、/music_stop 在呼叫 player.skip() 之前會先
           寫入 "skip" / "stop"。Lavalink 對「被跳過」與「自然播完」都只會送 track_end，
           靠這個旗標才分得出來（單曲循環時被 skip 就不能重播同一首）。
        3. 防自動跳歌保護（修復關閉循環狀態時自動跳過當前歌曲）：
           - 重入防護（player.advancing）：正在接下一首時忽略並發的 track_end。
           - 曲目身份比對（player._active_track）：wavelink 收到非 replaced 的 TrackEndEvent 時會無條件把
             player._current 設為 None；若這是上一首歌延遲抵達的事件、但新歌已經開播，必須還原 _current 並忽略。
           - 播放序號去重（player._play_seq / _last_ended_seq）：同一首歌的同一次播放最多只允許觸發一次換歌；
             否則單曲循環時重播同一首看不出異狀，一旦關閉循環（loop_mode == "off"）就會連續 pop 兩次佇列而把剛開始的新歌秒跳過。
           - 無意圖的 stopped 過濾：Lavalink 的 reason="stopped" 只應在明確呼叫 skip/stop（帶有 end_intent）時處理。
        """
        player = getattr(payload, "player", None)
        if player is None:
            return

        reason = str(getattr(payload, "reason", "") or "").lower()
        if reason == "replaced":
            return
        if reason == "cleanup":
            self._clear_player_playback_state(player)
            guild_obj = getattr(player, "guild", None)
            if guild_obj is not None:
                self._cancel_panel_bump(guild_obj.id, force=True)
            return

        # 防護 1：若正在進行換歌（advancing），忽略重入事件避免連跳兩首
        if getattr(player, "advancing", False):
            return

        ended_track = getattr(payload, "track", None)
        active_track = getattr(player, "_active_track", None)
        started_track = getattr(player, "_started_track", None)

        # 防護 2：若事件夾帶的曲目與目前實際在播的 _active_track（及 _started_track）皆不同，
        # 代表這是上一首歌延遲送達的舊事件；還原被 wavelink/websocket.py 誤清掉的 _current 並忽略此事件。
        if active_track is not None and ended_track is not None:
            matches_active = self._is_same_track(ended_track, active_track)
            matches_started = started_track is not None and self._is_same_track(ended_track, started_track)
            if not matches_active and not matches_started:
                if getattr(player, "current", None) is None:
                    try:
                        player._current = active_track
                    except Exception:
                        pass
                return

        # 讀出並立刻消耗旗標，避免影響下一首
        intent = getattr(player, "end_intent", None)
        player.end_intent = None

        # 防護 3：Lavalink 的 stopped 只會在主動呼叫 skip/stop 時發出；若沒有對應的 end_intent，
        # 代表是重複派發或殘留的停止事件，不能當成自然播完去切下一首。
        if reason == "stopped" and intent is None:
            if active_track is not None and getattr(player, "current", None) is None:
                try:
                    player._current = active_track
                except Exception:
                    pass
            return

        # 防護 4：同一個播放序號（_play_seq）只允許觸發一次 track_end 換歌流程
        play_seq = getattr(player, "_play_seq", 0)
        if play_seq > 0:
            if getattr(player, "_last_ended_seq", None) == play_seq:
                if active_track is not None and getattr(player, "current", None) is None:
                    try:
                        player._current = active_track
                    except Exception:
                        pass
                return
            player._last_ended_seq = play_seq

        player._active_track = None
        player._started_track = None

        song_queue: deque = getattr(player, "song_queue", None)
        if song_queue is None:
            song_queue = deque()
            player.song_queue = song_queue

        channel = getattr(player, "home_channel", None)

        loop_mode = getattr(player, "loop_mode", "off")
        requester_name = getattr(player, "current_requester", None) or "未知"
        requester_id = getattr(player, "current_requester_id", None)
        replay_track = active_track if active_track is not None else ended_track

        # ---------- 直播防自動結束機制 ----------
        # 根因：YouTube / Twitch 等直播串流在播放一定時間後，常因 HLS 分段清單刷新、CDN 憑證輪替或瞬間緩衝中斷，
        # 導致 Lavalink 發出 TrackEndEvent(reason="finished" 或 "loadFailed")，進而讓機器人誤以為「歌播完了」而自動結束播放。
        # 解法：只要該曲目為直播（_is_live_track）且非使用者主動跳過或停止（intent is None），
        # 自動重新解析直播串流網址並無縫續播（若已穩定播放超過 25 秒則歸零重試計數；連續瞬斷最多重試 3 次）。
        is_live = replay_track is not None and (
            self._is_live_track(replay_track)
            or getattr(player, "_is_current_stream", False)
        )
        if intent is None and is_live and reason in ("finished", "loadfailed", "load_failed"):
            started_mono = float(getattr(player, "_track_started_mono", 0.0) or 0.0)
            if started_mono > 0.0 and (time.monotonic() - started_mono) >= 25.0:
                player._live_retry_count = 0
            retry_count = int(getattr(player, "_live_retry_count", 0) or 0)
            if retry_count < 3:
                player._live_retry_count = retry_count + 1
                guild_obj = getattr(player, "guild", None)
                gid = guild_obj.id if guild_obj else None
                print(
                    f"🔄 [直播防斷流] 直播「{getattr(replay_track, 'title', '?')}」收到 {reason} 事件，"
                    f"自動重新銜接直播串流（第 {player._live_retry_count}/3 次，guild_id={gid}）"
                )
                player.advancing = True
                try:
                    fresh_track = await self._refresh_live_stream_track(replay_track)
                    player._retrying_live_stream = True
                    await self._play_track(player, fresh_track, requester_name, requester_id)
                    return
                except Exception as exc:
                    print(f"ℹ️ [直播防斷流] 自動續播直播失敗（視為直播結束）：{exc}")
                    if self._is_session_lost_error(exc):
                        await self._handle_session_lost(player, exc)
                        return
                    # 直播重連失敗時直接視為直播結束，跳出重試
                finally:
                    player._retrying_live_stream = False
                    player.advancing = False
            else:
                print(
                    f"ℹ️ [直播防斷流] 直播「{getattr(replay_track, 'title', '?')}」重試次數達上限或重連失敗，視為直播已結束。"
                )

            # 直播重連失敗或重試次數達上限：直接視為直播結束，重置計數，不報錯，亦不參與單曲/佇列循環
            player._live_retry_count = 0
            player._is_current_stream = False
            ended_track = None
            replay_track = None

        if reason in ("loadfailed", "load_failed"):
            ended_track = None
            replay_track = None

        # 曲目結束（換歌／被跳過／停止）：解除這首歌的操作請求鎖與拒絕鎖
        self._cleanup_control_state(ended_track)

        if intent == "stop":
            # /music_stop 已清空佇列並回覆使用者，這裡確保清除播放狀態與置底任務，不需要再發「播完了」通知
            self._clear_player_playback_state(player)
            guild_obj = getattr(player, "guild", None)
            if guild_obj is not None:
                self._cancel_panel_bump(guild_obj.id, force=True)
            return

        next_item = None
        if replay_track is not None and loop_mode == "single" and intent != "skip":
            next_item = (replay_track, requester_name, requester_id)
        else:
            if replay_track is not None and loop_mode == "all":
                # 佇列循環：剛結束（或被跳過）的歌排到佇列最後面（使用起播時的 replay_track 確保 position=0）
                song_queue.append((replay_track, requester_name, requester_id))
            if song_queue:
                next_item = song_queue.popleft()

        if next_item is None:
            player.current_requester = None
            player.current_requester_id = None
            # 第 7 週：背景匯入還在跑時，佇列暫時是空的只是因為「下一批還沒解析完」，
            # 不能當成播放結束（否則面板會被拋棄，匯入完成後也沒人接著播）。
            # 匯入任務每批加入佇列後會呼叫 _start_if_idle 接手播放。
            guild_obj = getattr(player, "guild", None)
            if guild_obj is not None and guild_obj.id in self._import_tasks:
                return
            self._clear_player_playback_state(player)
            if guild_obj is not None:
                self._cancel_panel_bump(guild_obj.id, force=True)
                # 第 7 週：「播放完畢」改寫在面板最終狀態裡，不再另外發訊息
                end_reason = "📭 直播已結束，輸入 `/music_play` 繼續點歌吧！" if is_live else "📭 播放佇列已經全部播完囉，輸入 `/music_play` 繼續點歌吧！"
                shown = await self._update_panel_ended(
                    guild_obj.id, reason=end_reason
                )
                if not shown and channel is not None:
                    try:
                        await channel.send(end_reason)
                    except discord.HTTPException:
                        pass
            return

        next_track, next_requester, next_requester_id = self._split_queue_item(next_item)
        player.advancing = True  # 告知匯入任務「我正在接下一首」，避免兩邊同時 play 造成互相覆蓋
        try:
            await self._play_track(player, next_track, next_requester, next_requester_id)
        except Exception as e:
            print(f">>> 自動播放下一首時發生錯誤：{e}")
            guild_obj = getattr(player, "guild", None)
            if self._is_session_lost_error(e):
                # Session 失效：_play_track 已把 _active_track 設為下一首，重連後 _recover_player_after_session_reset 會從頭播放它
                await self._handle_session_lost(player, e)
                if guild_obj is not None:
                    await self._notify_panel(
                        guild_obj.id,
                        f"🔄 音訊節點連線中斷，正在自動重新連線，恢復後會接著播放「{getattr(next_track, 'title', '下一首')}」。",
                        duration=15.0,
                    )
                return
            await self.bot.notify_owner_error(
                e, extra_info=f"on_wavelink_track_end 自動播放失敗 track={getattr(next_track, 'title', '?')}"
            )
            # 第 7 週：錯誤通知改顯示在面板內；沒有面板時才退回頻道訊息
            err_text = f"⚠️ 自動播放「{getattr(next_track, 'title', '未知曲目')}」時發生錯誤，已回報開發者。"
            shown = guild_obj is not None and await self._notify_panel(guild_obj.id, err_text, duration=15.0)
            if not shown and channel is not None:
                try:
                    await channel.send(err_text)
                except discord.HTTPException:
                    pass
            return
        finally:
            player.advancing = False

        # 第 6 週更新：播放下一首時不再單獨發送通知 Embed，統一由 on_wavelink_track_start
        # 觸發 _sync_panel 即時編輯並刷新主面板 Container，避免洗版與刷掉控制面板。

    # ---------- 語音頻道空房自動離開 ----------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        """新增：語音頻道沒有真人成員時，自動離開並清空佇列。

        discord.py 只要「任何人」（包含機器人自己）在語音頻道之間的狀態改變
        （加入、離開、切換頻道）都會觸發這個事件，所以第一件事是先判斷這次
        異動跟機器人所在的頻道有沒有關係，沒關係就直接 return，避免每次任何
        伺服器、任何頻道有人講話 / 開關靜音都觸發一次不必要的檢查。

        真正的判斷邏輯抽成 `_refresh_empty_channel_timer`：頻道裡只要還有
        任何一位「非機器人」成員，就取消計時器；一旦真人成員歸零，就啟動一個
        `EMPTY_VOICE_CHANNEL_TIMEOUT` 秒的倒數計時器，時間到了才真正離開，
        而不是有人一離開就立刻斷線，避免誤判暫時性的斷線重連或切頻道。
        """
        if wavelink is None:
            return

        guild = member.guild
        player: "wavelink_module.Player | None" = guild.voice_client

        if player is None:
            # 機器人目前不在任何語音頻道，理論上不該有殘留的計時器，
            # 但保險起見還是清一次，避免機器人被強制斷線（例如被踢出頻道）
            # 導致計時器變成孤兒、永遠不會被觸發也永遠不會被取消。
            self._cancel_empty_channel_timer(guild.id)
            # 第 7 週：Bot 被踢出／斷線時，背景匯入也要取消，並把面板收成最終狀態
            if guild.id in self._import_tasks:
                self._cancel_import(guild.id)
            if member.id == self.bot.user.id:
                await self._cleanup_guild_voice_and_player(guild)
                if guild.id in self.panel_messages:
                    await self._update_panel_ended(guild.id, reason="👋 我已離開語音頻道，想聽歌再 `/music_play` 一次就可以！")
            return

        bot_channel = player.channel
        if bot_channel is None:
            self._cancel_empty_channel_timer(guild.id)
            return

        # 只有在「這次異動的頻道」跟「機器人目前所在的頻道」有關時才需要重新檢查。
        changed_channels = {before.channel, after.channel}
        if bot_channel not in changed_channels:
            return

        await self._refresh_empty_channel_timer(guild.id, player)

    async def _refresh_empty_channel_timer(self, guild_id: int, player: "wavelink_module.Player"):
        """檢查機器人目前所在頻道還有沒有真人成員，決定要啟動還是取消倒數計時器。"""
        channel = player.channel
        if channel is None:
            self._cancel_empty_channel_timer(guild_id)
            return

        humans = [m for m in channel.members if not m.bot]

        if humans:
            # 還有真人在，不用（或不用再）倒數離開。
            self._cancel_empty_channel_timer(guild_id)
            return

        if guild_id in self.empty_channel_timers:
            return  # 已經有計時器在跑了，不用重複建立

        print(
            f"⏳ 語音頻道「{channel}」目前沒有真人成員，"
            f"{EMPTY_VOICE_CHANNEL_TIMEOUT:.0f} 秒後將自動離開（guild_id={guild_id}）。"
        )
        self.empty_channel_timers[guild_id] = asyncio.create_task(
            self._auto_leave_after_delay(guild_id)
        )

    def _cancel_empty_channel_timer(self, guild_id: int):
        task = self.empty_channel_timers.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _auto_leave_after_delay(self, guild_id: int):
        try:
            await asyncio.sleep(EMPTY_VOICE_CHANNEL_TIMEOUT)
        except asyncio.CancelledError:
            # 倒數過程中有真人回來了（或計時器被其他流程取消），
            # 屬於正常情況，直接結束這個背景任務即可，不用做任何清理。
            return

        # 時間到了，先把自己從字典移除，避免跟下一輪的 refresh 邏輯互相干擾。
        self.empty_channel_timers.pop(guild_id, None)

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        player: "wavelink_module.Player | None" = guild.voice_client
        if player is None:
            return  # 機器人已經不在語音頻道了（例如被 /music_leave 手動離開過）

        channel = player.channel
        if channel is not None:
            humans = [m for m in channel.members if not m.bot]
            if humans:
                # 保險檢查：理論上真人回來時 on_voice_state_update 就會取消計時器，
                # 這裡是避免極端的時序競態（計時器已經醒來、但取消還沒生效）。
                return

        song_queue: deque = getattr(player, "song_queue", None)
        if song_queue:
            song_queue.clear()

        home_channel = getattr(player, "home_channel", None)
        # 第 7 週：空房離開時一併取消背景匯入，並把原因寫進面板最終狀態（不另外發訊息）
        self._cancel_import(guild_id)
        leave_text = (
            f"📤 語音頻道已經空了超過 {EMPTY_VOICE_CHANNEL_TIMEOUT:.0f} 秒，"
            "我先自動離開並清空佇列了，想聽歌再 `/music_play` 一次就可以！"
        )
        shown = await self._update_panel_ended(guild_id, reason=leave_text)

        try:
            await player.disconnect()
        except Exception as e:
            print(f">>> 自動離開語音頻道時發生錯誤: {e}")
            await self.bot.notify_owner_error(e, extra_info=f"自動離開語音頻道失敗 guild_id={guild_id}")
            return

        print(
            f"👋 語音頻道已經沒有真人成員超過 {EMPTY_VOICE_CHANNEL_TIMEOUT:.0f} 秒，"
            f"已自動離開（guild_id={guild_id}）。"
        )
        # 沒有面板可顯示時才退回頻道訊息
        if not shown and home_channel is not None:
            try:
                await home_channel.send(leave_text)
            except discord.HTTPException:
                pass

    # ---------- 共用邏輯 ----------
    async def _cleanup_guild_voice_and_player(self, guild: discord.Guild) -> None:
        """徹底清理指定伺服器的語音連線、wavelink Player 與 Lavalink 節點殘留狀態。

        解決症狀：機器人加入語音房時立刻退出、30 秒交握逾時（ChannelTimeoutException）與幽靈連線。
        發生原因：Discord gateway 語音狀態殘留、wavelink node._players 殘留舊 player、
        或 Lavalink session 端的 player 未正確清理，導致新連線被 Lavalink 即刻拒絕並自動 disconnect。
        """
        if guild is None:
            return

        # 1. 斷開 Discord.py VoiceClient
        vc = guild.voice_client
        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception as exc:
                print(f"⚠️ 清理 VoiceClient 失敗 (guild_id={guild.id})：{exc}")

        # 2. 強制透過 Discord Gateway 發送語音狀態 None，重置伺服器端半開（half-open）狀態
        try:
            await guild.change_voice_state(channel=None)
        except Exception as exc:
            print(f"⚠️ 重置 Gateway 語音狀態失敗 (guild_id={guild.id})：{exc}")

        # 3. 清理 wavelink 節點上的 _players 與 Lavalink 服務端的 player
        if wavelink is not None:
            pool_nodes = getattr(wavelink.Pool, "nodes", {})
            for node in list(pool_nodes.values()):
                players = getattr(node, "_players", {})
                old_player = players.pop(guild.id, None)
                if old_player is not None:
                    try:
                        await old_player.disconnect(force=True)
                    except Exception:
                        pass
                destroy_fn = getattr(node, "_destroy_player", None)
                if callable(destroy_fn):
                    try:
                        await destroy_fn(guild.id)
                    except Exception:
                        pass

    async def _ensure_player(
        self, interaction: discord.Interaction
    ) -> "wavelink_module.Player | None":
        """取得目前伺服器的 Player；若機器人還沒加入語音頻道，就直接幫使用者加入。

        回傳 None 代表已經送出錯誤訊息給使用者，呼叫端應該直接 return，不要繼續播放。
        這裡刻意沒有沿用 /join 指令本身，而是抽成共用函式，因為 /play 需要在
        「還沒 defer」跟「已經 defer」兩種情境下都能重複使用同一段邏輯。
        """
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await self._ephemeral_error(interaction, "❌ 你必須先加入一個語音頻道，我才能播放音樂！")
            return None

        user_channel = interaction.user.voice.channel
        player: "wavelink_module.Player | None" = interaction.guild.voice_client

        # 清理可能殘留的幽靈/失效連線
        if player is not None and not getattr(player, "connected", True):
            await self._cleanup_guild_voice_and_player(interaction.guild)
            player = None

        if player is None:
            # 1. 預先檢查節點可用性，避免無謂等待 30 秒逾時
            node_ready, node_err = self._is_any_node_connected()
            if not node_ready:
                await self._ephemeral_error(interaction, f"❌ {node_err}")
                asyncio.create_task(self._reconnect_lavalink_if_needed())
                return None

            try:
                player = await user_channel.connect(cls=wavelink.Player)
                # 第3週更新：改成 disabled，改由我們自己的 song_queue +
                # on_wavelink_track_end 監聽器接管「播完自動接下一首」的邏輯，
                # 避免跟 wavelink 內建的 autoplay 互相搶著呼叫 player.play()。
                player.autoplay = wavelink.AutoPlayMode.disabled
                player.song_queue = deque()
            except Exception as e:
                print(f">>> /play 加入語音頻道失敗：{e}")
                # 關鍵防護：若 handshake 失敗或逾時（例如 Lavalink session 異常導致機器人被立刻踢出，
                # 觸發 ChannelTimeoutException 30 秒逾時），徹底清理 VoiceClient、Gateway 語音狀態與 Lavalink 端狀態
                await self._cleanup_guild_voice_and_player(interaction.guild)
                asyncio.create_task(self._reconnect_lavalink_if_needed())

                await self.bot.notify_owner_error(e, interaction, extra_info=f"/play 加入 {user_channel} 失敗")
                is_timeout = isinstance(e, (asyncio.TimeoutError, TimeoutError)) or "timeout" in str(e).lower()
                if is_timeout:
                    err_msg = "❌ 加入語音頻道交握逾時（音訊節點未能在期限內完成交握，已自動清理重置連線狀態），請稍後再試一次！"
                else:
                    err_msg = f"❌ 加入語音頻道時發生錯誤：{e}（已自動退出重置狀態）"
                await self._ephemeral_error(interaction, err_msg)
                return None
        elif player.channel.id != user_channel.id:
            await self._ephemeral_error(
                interaction, f"❌ 你必須跟我在同一個語音頻道（{player.channel.mention}）才能點歌！"
            )
            return None

        # 不論是新建立還是原本就存在的 Player，都更新 home_channel，
        # 讓 on_wavelink_track_exception 把錯誤訊息回報到「最近一次下指令」的文字頻道，
        # 而不是固定在第一次 /join 當下的頻道。
        player.home_channel = interaction.channel

        # 保險起見：萬一 player 是透過其他路徑建立（理論上不會發生），
        # 確保一定有 song_queue 可用，避免後面存取時 AttributeError。
        if getattr(player, "song_queue", None) is None:
            player.song_queue = deque()

        if not hasattr(player, "shuffle"):
            player.shuffle = False

        # 使用者現在就在頻道裡跟機器人互動，代表頻道一定不是空的，
        # 保險起見取消任何可能殘留的自動離開計時器。
        self._cancel_empty_channel_timer(interaction.guild.id)

        return player

    # ---------- 指令 ----------
    @app_commands.command(name="music_join", description="讓機器人加入你目前所在的語音頻道")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction):
        if not await self._check_music_channel(interaction):
            return
        if wavelink is None:
            await interaction.response.send_message("❌ 語音模組尚未安裝完成，請聯絡管理員。", ephemeral=True)
            return

        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message("❌ 你必須先加入一個語音頻道，我才能跟過去！", ephemeral=True)
            return

        channel = interaction.user.voice.channel

        if interaction.guild.voice_client is not None:
            vc = interaction.guild.voice_client
            # 檢查是否為殘留的失效/非正常連線
            if not getattr(vc, "connected", True):
                await self._cleanup_guild_voice_and_player(interaction.guild)
            else:
                target_channel = getattr(vc, "channel", None)
                channel_mention = target_channel.mention if target_channel else "語音"
                await interaction.response.send_message(
                    f"ℹ️ 我已經在 {channel_mention} 頻道中了。", ephemeral=True
                )
                return

        # 修正（實測發現）: channel.connect() 內部要完成語音閘道 handshake，
        # 距離 Lavalink 節點較遠或 Render 冷啟動時很容易超過 Discord 給的 3 秒 ack
        # 期限，沒有 defer() 時會直接炸 discord.errors.NotFound (10062 Unknown
        # interaction)。做法沿用 /play：先 defer(ephemeral=True)，之後一律用
        # followup.send 回覆，不再用 interaction.response.send_message。
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except (discord.NotFound, discord.HTTPException) as e:
            print(f">>> /join interaction.defer() 失敗: {e}")
            return

        # 1. 預先檢查節點可用性，避免無謂等待 30 秒逾時
        node_ready, node_err = self._is_any_node_connected()
        if not node_ready:
            await interaction.followup.send(f"❌ {node_err}", ephemeral=True)
            asyncio.create_task(self._reconnect_lavalink_if_needed())
            return

        try:
            player = await channel.connect(cls=wavelink.Player)
        except Exception as e:
            print(f">>> 加入語音頻道失敗：{e}")
            # 關鍵防護：若 handshake 失敗或逾時（例如 Lavalink session 異常導致機器人被立刻踢出，
            # 觸發 ChannelTimeoutException 30 秒逾時），徹底清理 VoiceClient、Gateway 語音狀態與 Lavalink 端狀態
            await self._cleanup_guild_voice_and_player(interaction.guild)
            asyncio.create_task(self._reconnect_lavalink_if_needed())

            await self.bot.notify_owner_error(e, interaction, extra_info=f"/join 加入 {channel} 失敗")
            is_timeout = isinstance(e, (asyncio.TimeoutError, TimeoutError)) or "timeout" in str(e).lower()
            if is_timeout:
                err_msg = "❌ 加入語音頻道交握逾時（音訊節點未能在期限內完成交握，已自動清理重置連線狀態），請稍後再試一次！"
            else:
                err_msg = f"❌ 加入語音頻道時發生錯誤：{e}（已自動退出重置狀態）"
            await interaction.followup.send(err_msg, ephemeral=True)
            return

        # 第3週更新：與 _ensure_player 一致，改成 disabled，
        # 交由自己的 song_queue + on_wavelink_track_end 決定下一首播什麼。
        player.autoplay = wavelink.AutoPlayMode.disabled
        player.song_queue = deque()
        # 記錄下指令的文字頻道，讓 on_wavelink_track_exception 之後能把播放失敗的
        # 通知送回這裡（Lavalink 端的錯誤是非同步事件，不會經過 /join 或 /play 本身）。
        player.home_channel = interaction.channel

        # 剛加入的頻道一定有下指令的這個人在，保險起見取消可能殘留的計時器。
        self._cancel_empty_channel_timer(interaction.guild.id)

        await interaction.followup.send(f"🔊 已加入 {channel.mention}！", ephemeral=True)

    @app_commands.command(name="music_leave", description="讓機器人離開目前所在的語音頻道")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        if not await self._check_music_channel(interaction):
            return
        voice_client = interaction.guild.voice_client

        if voice_client is None:
            await interaction.response.send_message("ℹ️ 我目前不在任何語音頻道中。", ephemeral=True)
            return

        channel_mention = voice_client.channel.mention
        # 手動 /leave 了，不需要（也不該）再讓背景計時器之後又觸發一次自動離開。
        self._cancel_empty_channel_timer(interaction.guild.id)
        # 第3週新增：離開語音頻道時順便清空這個伺服器的佇列，
        # 避免下次 /join 進來時還殘留上一次沒播完的歌曲清單造成混淆。
        song_queue = getattr(voice_client, "song_queue", None)
        if song_queue:
            song_queue.clear()
        # 第 7 週：離開時取消背景匯入（已解析但未加入的曲目直接丟棄），原因寫進面板最終狀態
        self._cancel_import(interaction.guild.id)
        await self._update_panel_ended(
            interaction.guild.id,
            reason=f"👋 已離開 {channel_mention}（操作者：{interaction.user.display_name}）",
        )
        await voice_client.disconnect()
        await interaction.response.send_message(f"👋 已離開 {channel_mention}。", ephemeral=True)

    async def _fetch_tracks_with_fallback(self, query: str) -> Any:
        """核心搜尋演算法：支援 URL、自帶前綴，並對純關鍵字進行多來源智慧 Fallback。

        - 修正問題：wavelink 3.x 預設使用 ytmsearch:（YouTube Music），但多數公開節點對 ytmsearch
          回傳空結果（empty）或錯誤，導致一般關鍵字搜尋完全無法找到歌曲。
        - 解決方案：純關鍵字優先使用「ytsearch:」（標準 YouTube 搜尋），並在無結果或失敗時
          依序自動 fallback 到「scsearch:」（SoundCloud）與「ytmsearch:」（YouTube Music）。
        - 針對 URL 與自帶前綴（例如 ytsearch:、scsearch: 等）直接傳送，不重疊添加前綴。
        """
        clean_query = query.strip().strip("<>")
        is_url = clean_query.startswith("http://") or clean_query.startswith("https://")
        has_prefix = any(
            clean_query.lower().startswith(p)
            for p in ("ytsearch:", "ytmsearch:", "scsearch:", "spsearch:", "amsearch:")
        )

        if is_url or has_prefix:
            return await wavelink.Playable.search(clean_query)

        sources = ["ytsearch", "scsearch", "ytmsearch"]
        last_error = None
        for src in sources:
            try:
                try:
                    tracks = await wavelink.Playable.search(clean_query, source=src)
                except TypeError:
                    # 相容性保護：若環境或測試 mock 未支援 source 參數，回退為單參數呼叫
                    tracks = await wavelink.Playable.search(clean_query)
                if tracks:
                    return tracks
            except Exception as e:
                last_error = e
                print(f">>> 來源 [{src}] 搜尋「{clean_query}」失敗，嘗試下一個來源：{e}")

        if last_error is not None:
            raise last_error
        return None

    async def _fetch_youtube_playlist(self, url: str, limit: int) -> YouTubeFetchResult:
        """透過 Lavalink 解析 YouTube 播放清單（解析在節點端進行，不佔用 bot 事件迴圈）。"""
        found = await asyncio.wait_for(self._fetch_tracks_with_fallback(url), timeout=IMPORT_SEARCH_TIMEOUT * 2)
        if not found:
            return YouTubeFetchResult(tracks=[], truncated=False, skipped=0)
        playables = found.tracks if isinstance(found, wavelink.Playlist) else list(found)
        return filter_youtube_tracks(list(playables), limit)

    async def _search_one_track(
        self, interaction: discord.Interaction, query: str
    ) -> "wavelink_module.Playable | None":
        """共用的搜尋邏輯，/play 與 /play_next（插播）都會用到。

        回傳 None 代表搜尋失敗或找不到結果，錯誤訊息已經透過 _ephemeral_error 送出私人訊息，
        呼叫端只要直接 return 即可，不用重複處理錯誤訊息。
        """
        try:
            tracks = await self._fetch_tracks_with_fallback(query)
        except Exception as e:
            # wavelink 搜尋失敗常見原因：來源網站暫時掛掉、網址格式不支援，
            # 或是 YouTube 端觸發了反爬蟲機制（poToken / OAuth2 相關）。
            # 第8週會處理 poToken/OAuth2 設定，這裡先統一攔截、回報開發者，
            # 避免整個指令直接噴未捕捉例外。
            print(f">>> wavelink 搜尋失敗：{e}")
            await self.bot.notify_owner_error(e, interaction, extra_info=f"音樂搜尋失敗 query={query}")
            await self._ephemeral_error(
                interaction,
                "❌ 搜尋音樂時發生錯誤，可能是來源網站暫時無法連線，或觸發了反爬蟲封鎖，已回報開發者。",
            )
            return None

        if not tracks:
            await self._ephemeral_error(
                interaction,
                f"😕 找不到「{query}」的搜尋結果，可能是關鍵字沒有對應結果、影片為地區限定，或網址無效，換個關鍵字試試看。",
            )
            return None

        # wavelink.Playable.search 對於歌單網址會回傳 Playlist，單曲/關鍵字則回傳 list[Playable]。
        # 本週先只取第一首，完整歌單匯入排到第7週（Spotify 批次解析）再實作。
        if isinstance(tracks, wavelink.Playlist):
            return tracks.tracks[0] if tracks.tracks else None
        return tracks[0]

    def _build_track_embed(
        self,
        title: str,
        track: "wavelink_module.Playable",
        requester_name: str | None,
        color: discord.Color,
        footer_prefix: str = "由",
        footer_suffix: str = "點播",
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=f"[{track.title}]({track.uri})" if getattr(track, "uri", None) else track.title,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        artwork = get_track_artwork(track)
        if artwork:
            embed.set_thumbnail(url=artwork)
        embed.add_field(name="👤 作者", value=getattr(track, "author", None) or "未知", inline=True)
        embed.add_field(name="⏱️ 長度", value=format_duration(getattr(track, "length", None)), inline=True)
        if requester_name:
            embed.set_footer(text=f"{footer_prefix} {requester_name} {footer_suffix}")
        return embed

    def _build_panel_embed(self, player: "wavelink_module.Player | None") -> discord.Embed:
        """建立音樂控制面板的即時資訊 Embed（第 6 週：委派至 build_now_playing_embed）。"""
        return build_now_playing_embed(player)

    def _set_panel_notice(self, guild_id: int, notice: str, duration: float | None = 8.0) -> None:
        """第 6 週新增：設定伺服器控制面板最下方的提示文字，並在一段時間後自動移除並重新同步面板。

        第 7 週更新：duration=None 代表「持久提示」（例如匯入進度），不排程自動清除，
        由呼叫端在工作結束時自行更新或清除。
        """
        old_task = self._panel_notice_tasks.pop(guild_id, None)
        if old_task and not old_task.done():
            old_task.cancel()

        self._panel_notices[guild_id] = notice
        if duration is None:
            return

        async def _auto_clear():
            try:
                await asyncio.sleep(duration)
                if self._panel_notices.get(guild_id) == notice:
                    self._panel_notices.pop(guild_id, None)
                    await self._sync_panel(guild_id)
            except asyncio.CancelledError:
                pass
            finally:
                if self._panel_notice_tasks.get(guild_id) is cur_task:
                    self._panel_notice_tasks.pop(guild_id, None)

        cur_task = asyncio.create_task(_auto_clear())
        self._panel_notice_tasks[guild_id] = cur_task

    async def _notify_panel(self, guild_id: int, text: str, duration: float | None = 8.0) -> bool:
        """第 7 週新增：把「通知」顯示在該伺服器的面板 Container 底部，而不是另外發訊息。

        設計理由：每多發一則公開訊息，面板就會被往上擠；所有通知統一走面板 notice，
        聊天頻道才能維持單一則乾淨的面板。回傳 False 代表該伺服器目前沒有面板，
        呼叫端可自行決定要不要退回其他方式（例如 ephemeral 回覆）。
        """
        if guild_id not in self.panel_messages:
            return False
        self._set_panel_notice(guild_id, text, duration=duration)
        await self._sync_panel(guild_id)
        return True

    async def _announce(self, interaction: discord.Interaction, text: str, duration: float = 8.0) -> None:
        """第 7 週新增：指令的公開成功回覆改顯示在面板上，並收掉 Discord 的互動回應避免洗版。

        沒有面板可顯示時才退回原本的公開訊息，確保使用者一定看得到結果。
        """
        if await self._notify_panel(interaction.guild.id, text, duration=duration):
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                await interaction.delete_original_response()
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        if interaction.response.is_done():
            await interaction.followup.send(text)
        else:
            await interaction.response.send_message(text)

    async def _sync_panel(
        self,
        guild_id: int,
        player: "wavelink_module.Player | None" = _PANEL_PLAYER_UNSET,
        interaction: discord.Interaction | None = None,
        notice_override: str | None = None,
        final: bool = False,
        panel_info_override: tuple[int, int] | None = None,
    ):
        """同步指定伺服器的控制面板（動態刷新按鈕狀態與 Container）。

        - 狀態以 guild 為單位完全隔離。
        - 若傳入 interaction 或有掛起的切歌 interaction，優先透過 edit_message / edit_original_response 回應。
        - 否則透過 channel.fetch_message 編輯既有訊息，避免洗版。
        - 若舊面板訊息被刪除（NotFound），重新發送新面板並更新記錄。
        - 第 7 週新增 notice_override：以指定文字取代底部提示（用於「已結束」原因）。
        - 第 7 週新增 final：最終狀態（播放結束／離開）。此模式下一定會編輯「記錄中的面板訊息」，
          即使 interaction 按的是另一則舊面板，確保被拋棄的面板不會殘留可操作的外觀。
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        async with self._get_panel_lock(guild_id):
            if player is _PANEL_PLAYER_UNSET:
                player = guild.voice_client

            # 非最終收尾模式下，若播放器已經停止／結束且無匯入進行中，絕不更新或重發面板
            if not final and not self._is_playback_active(player) and guild_id not in self._import_tasks:
                return

            # 檢查是否有切歌等操作掛起的 deferred interaction
            if interaction is None and guild_id in self._pending_panel_interaction:
                interaction = self._pending_panel_interaction.pop(guild_id, None)

            view = MusicControlView(
                self,
                player=player,
                notice=notice_override if notice_override is not None else self._panel_notices.get(guild_id),
            )
            view.refresh_buttons(player)

            tracked = panel_info_override if panel_info_override is not None else self.panel_messages.get(guild_id)

            # 優先以 interaction 回應
            if interaction is not None:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.edit_message(content=None, embed=None, view=view)
                    else:
                        await interaction.edit_original_response(content=None, embed=None, view=view)
                    if player is not None and getattr(player, "current", None) is not None:
                        pos = getattr(player, "position", 0)
                        length = getattr(player.current, "length", 0)
                        is_stream = bool(getattr(player.current, "is_stream", False))
                        self._last_progress_str[guild_id] = build_progress_bar(pos, length, is_stream=is_stream)
                    # final 模式：只有「被按的那則就是記錄中的面板」才算處理完，否則繼續編輯記錄中的面板
                    msg_id = getattr(getattr(interaction, "message", None), "id", None)
                    if not final or tracked is None or tracked[1] == msg_id:
                        return
                except (discord.NotFound, discord.HTTPException) as exc:
                    print(f">>> 透過 interaction 更新面板失敗，改走 fetch_message：{exc}")

            # 無 interaction 時，透過記錄的 channel_id 與 message_id 編輯既有訊息
            panel_info = tracked
            target_channel = getattr(player, "home_channel", None) if player else None

            if panel_info:
                channel_id, message_id = panel_info
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except Exception:
                        channel = None

                if channel is not None:
                    try:
                        msg = await channel.fetch_message(message_id)
                        await msg.edit(content=None, embed=None, view=view)
                        if player is not None and getattr(player, "current", None) is not None:
                            pos = getattr(player, "position", 0)
                            length = getattr(player.current, "length", 0)
                            is_stream = bool(getattr(player.current, "is_stream", False))
                            self._last_progress_str[guild_id] = build_progress_bar(pos, length, is_stream=is_stream)
                        return
                    except discord.NotFound:
                        print(f"ℹ️ 控制面板訊息已被刪除 (message_id={message_id})，將重新發送新面板。")
                        if self.panel_messages.get(guild_id) == (channel_id, message_id):
                            self.panel_messages.pop(guild_id, None)
                        self._last_progress_str.pop(guild_id, None)
                    except discord.HTTPException as exc:
                        print(f"⚠️ 編輯控制面板訊息失敗：{exc}")
                        return

            # 既有訊息不存在或已遭刪除時，若非最終狀態且仍在有效播放中，於 target_channel 發送新面板
            if not final and target_channel is not None and self._is_playback_active(player):
                self._sending_panel_guilds.add(guild_id)
                try:
                    new_msg = await target_channel.send(view=view)
                    self._known_panel_msg_ids.append(new_msg.id)
                    self.panel_messages[guild_id] = (target_channel.id, new_msg.id)
                    self._guild_panel_history.setdefault(guild_id, set()).add((target_channel.id, new_msg.id))
                    self._cancel_panel_bump(guild_id)
                    pos = getattr(player, "position", 0)
                    length = getattr(player.current, "length", 0)
                    is_stream = bool(getattr(player.current, "is_stream", False))
                    self._last_progress_str[guild_id] = build_progress_bar(pos, length, is_stream=is_stream)
                    await self._cleanup_old_panels(
                        guild_id, keep_message_id=new_msg.id, default_channel=target_channel
                    )
                except discord.HTTPException as exc:
                    print(f"❌ 發送新控制面板訊息失敗：{exc}")
                finally:
                    self._sending_panel_guilds.discard(guild_id)

    async def _update_panel_ended(
        self,
        guild_id: int,
        interaction: discord.Interaction | None = None,
        reason: str | None = None,
    ) -> bool:
        """將伺服器的控制面板標記為已結束（全部按鈕 disabled），並「拋棄」這個 Container。

        第 7 週修正：以前結束後仍保留 panel_messages 的記錄，所以下次點歌時
        _sync_panel 會去編輯那則舊面板（它可能早就被聊天訊息擠到很上面），看起來像「沿用舊 container」。
        現在結束時把記錄移除：舊面板停在「已結束」的最終狀態，下次點歌 on_wavelink_track_start
        找不到記錄，就會在頻道最下方送出一則全新的 Container。

        reason：結束原因（播完／停止／自動離開…）直接寫在這則最終面板的底部，
        不再另外發送訊息。回傳 True 代表有面板可顯示（呼叫端據此決定是否需要退回其他回覆方式）。
        """
        # 關鍵防護：在進入任何 await 之前，立即同步移除 panel_messages 記錄、清除播放器曲目狀態並強制取消置底任務，
        # 確保在等待鎖或網路請求期間，on_message、_bump_panel 與 progress_update_loop 絕不會再觸發置底或重發面板。
        panel_info = self.panel_messages.pop(guild_id, None)
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            self._clear_player_playback_state(getattr(guild, "voice_client", None))
        self._cancel_panel_bump(guild_id, force=True)
        self._last_bump_at.pop(guild_id, None)
        self._last_progress_str.pop(guild_id, None)
        self._rate_limit_until.pop(guild_id, None)
        task = self._panel_notice_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
        pending = self._panel_notices.pop(guild_id, None)
        self._pending_panel_interaction.pop(guild_id, None)

        # 失敗類（⚠️）提示要保留給使用者看；其他提示（如「已新增」）在結束時就沒意義了。
        parts = [p for p in ((pending if pending and pending.startswith("⚠️") else None), reason) if p]
        notice = "\n".join(parts)

        had_panel = panel_info is not None or interaction is not None
        await self._sync_panel(
            guild_id,
            player=None,
            interaction=interaction,
            notice_override=notice,
            final=True,
            panel_info_override=panel_info,
        )
        # 拋棄並清理同場播放可能殘留的多餘舊面板（保留最終顯示已結束的那一則）
        self.panel_messages.pop(guild_id, None)
        keep_id = (
            panel_info[1]
            if panel_info is not None
            else getattr(getattr(interaction, "message", None), "id", None)
        )
        if keep_id is not None:
            await self._cleanup_old_panels(guild_id, keep_message_id=keep_id)
        self._guild_panel_history.pop(guild_id, None)
        return had_panel

    # ---------- 控制面板防洗版自動置底（Sticky Panel） ----------
    # 設計理由（以可行性為主要考量）：
    # 1. 權限零門檻：刪除他人訊息需要 Manage Messages，建立討論串需要 Create Public Threads + Manage Messages；
    #    而「刪除機器人自己的舊面板訊息 + 在頻道最底部重發新面板」只需要最基本的 Send Messages 權限，在任何頻道皆 100% 可行。
    # 2. 防抖節流（Debounce）+ 最小冷卻間隔（Cooldown）：
    #    - 收到新訊息時順延 _bump_due_at（預設 PANEL_BUMP_DELAY = 6 秒），等頻道安靜下來才置底；
    #    - 兩次置底之間強制間隔至少 PANEL_BUMP_COOLDOWN（預設 12 秒），避免短時間重複觸發導致舊面板來不及清除或觸發 429。
    # 3. 避免播放停止／結束後仍觸發置底，以及確保舊面板 100% 被清除：
    #    - 停止／結束時同步清空 panel_messages 與 player 播放狀態，並檢查 _is_playback_active；
    #    - 以 _guild_panel_history 追蹤同場播放期間發送過的所有面板訊息 ID，置底時逐一刪除所有舊面板（含 429 自動重試），
    #      只有確認刪除成功或 NotFound 才從歷史清單移除，徹底杜絕舊面板殘留。
    def _get_panel_lock(self, guild_id: int) -> asyncio.Lock:
        """取得指定伺服器的面板操作鎖，防止並發 _sync_panel 與 _bump_panel 同時送出多個面板。"""
        lock = self._panel_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._panel_locks[guild_id] = lock
        return lock

    def _is_panel_message(self, message: discord.Message) -> bool:
        """檢查訊息是否為音樂控制面板（透過已知 ID 或內部按鈕 custom_id 辨識）。"""
        msg_id = getattr(message, "id", None)
        if msg_id is not None and msg_id in self._known_panel_msg_ids:
            return True
        components = getattr(message, "components", None) or []
        stack = list(components)
        while stack:
            comp = stack.pop()
            cid = getattr(comp, "custom_id", None)
            if isinstance(cid, str) and cid.startswith("music:"):
                return True
            children = getattr(comp, "children", None)
            if children:
                stack.extend(children)
        return False

    async def _delete_panel_message(self, channel: Any, message_id: int) -> bool:
        """安全刪除舊的控制面板訊息：優先使用 get_partial_message 直接發送 DELETE，遭遇 429 時退避重試，失敗時退回 fetch_message。
        回傳 True 代表已確認刪除（或訊息已不存在 NotFound）；回傳 False 代表暫時失敗，保留於歷史清單待下次清理。
        """
        if hasattr(channel, "get_partial_message"):
            for attempt in range(2):
                try:
                    old_msg = channel.get_partial_message(message_id)
                    await old_msg.delete()
                    return True
                except discord.NotFound:
                    return True
                except discord.HTTPException as exc:
                    if getattr(exc, "status", None) == 429 and attempt == 0:
                        retry_after = min(float(getattr(exc, "retry_after", 1.5) or 1.5), 5.0)
                        await asyncio.sleep(retry_after)
                        continue
                    print(f"⚠️ [面板自動置底] partial_message.delete 失敗，改試 fetch_message (id={message_id})：{exc}")
                    break
                except Exception as exc:
                    print(f"⚠️ [面板自動置底] partial_message.delete 失敗，改試 fetch_message (id={message_id})：{exc}")
                    break

        if hasattr(channel, "fetch_message"):
            try:
                old_msg = await channel.fetch_message(message_id)
                await old_msg.delete()
                return True
            except discord.NotFound:
                return True
            except discord.HTTPException as exc:
                print(f"⚠️ [面板自動置底] fetch_message.delete 失敗 (id={message_id})：{exc}")
            except Exception as exc:
                print(f"⚠️ [面板自動置底] 刪除舊面板異常 (id={message_id})：{exc}")
        return False

    async def _cleanup_old_panels(
        self,
        guild_id: int,
        keep_message_id: int | None,
        default_channel: Any = None,
    ) -> None:
        """清除該伺服器同場播放中除了 keep_message_id 以外的所有舊面板訊息。"""
        history = self._guild_panel_history.get(guild_id)
        if not history:
            return
        for old_cid, old_mid in list(history):
            if keep_message_id is not None and old_mid == keep_message_id:
                continue
            ch = default_channel if (default_channel is not None and getattr(default_channel, "id", None) == old_cid) else None
            if ch is None:
                ch = self.bot.get_channel(old_cid)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(old_cid)
                except Exception:
                    ch = None
            if ch is None:
                history.discard((old_cid, old_mid))
                continue
            if await self._delete_panel_message(ch, old_mid):
                history.discard((old_cid, old_mid))

    def _cancel_panel_bump(self, guild_id: int, force: bool = False) -> None:
        """取消指定伺服器的面板自動置底等待任務並清空觸發訊息紀錄。
        若該伺服器已進入 _bumping_guilds（正在發送新面板與刪除舊面板）且非強制取消，不中斷該次執行，避免舊面板漏刪。
        """
        self._bump_trigger_msgs.pop(guild_id, None)
        self._bump_due_at.pop(guild_id, None)
        if not force and guild_id in self._bumping_guilds:
            return
        task = self._panel_bump_tasks.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_panel_bump(self, guild_id: int) -> None:
        """排程（或順延防抖到期時間）將控制面板自動置底，同時遵守兩次置底間的最短冷卻間隔。"""
        now = time.monotonic()
        last_bump = self._last_bump_at.get(guild_id, 0.0)
        cooldown_due = (last_bump + PANEL_BUMP_COOLDOWN) if last_bump > 0.0 else 0.0
        self._bump_due_at[guild_id] = max(now + PANEL_BUMP_DELAY, cooldown_due)
        existing = self._panel_bump_tasks.get(guild_id)
        if existing is not None and not existing.done():
            return

        async def _delayed_bump():
            try:
                while True:
                    due = self._bump_due_at.get(guild_id, 0.0)
                    remaining = due - time.monotonic()
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                        continue
                    break
                if not self._bump_trigger_msgs.get(guild_id):
                    return
                if guild_id not in self.panel_messages:
                    self._bump_trigger_msgs.pop(guild_id, None)
                    self._bump_due_at.pop(guild_id, None)
                    return
                await self._bump_panel(guild_id)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"⚠️ [面板自動置底] 伺服器 {guild_id} 置底失敗：{exc}")
            finally:
                if self._panel_bump_tasks.get(guild_id) is cur_task:
                    self._panel_bump_tasks.pop(guild_id, None)
                # 若在 _bump_panel 執行期間又有新的普通訊息進來、且面板仍在播放中，補排下一輪防抖（受冷卻保護）
                if self._bump_trigger_msgs.get(guild_id) and guild_id in self.panel_messages:
                    self._schedule_panel_bump(guild_id)

        cur_task = asyncio.create_task(_delayed_bump())
        self._panel_bump_tasks[guild_id] = cur_task

    async def _bump_panel(self, guild_id: int) -> None:
        """執行控制面板置底：在頻道底部發送新面板並刪除所有舊面板，確保面板不被普通訊息刷掉。"""
        async with self._get_panel_lock(guild_id):
            panel_info = self.panel_messages.get(guild_id)
            if not panel_info:
                self._bump_trigger_msgs.pop(guild_id, None)
                self._bump_due_at.pop(guild_id, None)
                return

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            player = getattr(guild, "voice_client", None)
            if not self._is_playback_active(player):
                self._bump_trigger_msgs.pop(guild_id, None)
                self._bump_due_at.pop(guild_id, None)
                return

            channel_id, old_message_id = panel_info
            self._guild_panel_history.setdefault(guild_id, set()).add((channel_id, old_message_id))
            trigger_msgs = self._bump_trigger_msgs.get(guild_id)
            if trigger_msgs is not None:
                trigger_msgs = {mid for mid in trigger_msgs if mid > old_message_id}
            if not trigger_msgs:
                self._bump_trigger_msgs.pop(guild_id, None)
                self._bump_due_at.pop(guild_id, None)
                return

            # 遵守 429 Rate Limit 退避冷卻
            cooldown_until = self._rate_limit_until.get(guild_id, 0.0)
            now = time.monotonic()
            if now < cooldown_until:
                await asyncio.sleep(cooldown_until - now)
                panel_info = self.panel_messages.get(guild_id)
                if not panel_info or not self._is_playback_active(player):
                    self._bump_trigger_msgs.pop(guild_id, None)
                    self._bump_due_at.pop(guild_id, None)
                    return
                channel_id, old_message_id = panel_info
                self._guild_panel_history.setdefault(guild_id, set()).add((channel_id, old_message_id))

            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception:
                    return
            if channel is None:
                return

            # 若頻道最後一則訊息本來就是目前的控制面板，無須重發
            if getattr(channel, "last_message_id", None) == old_message_id:
                self._bump_trigger_msgs.pop(guild_id, None)
                self._bump_due_at.pop(guild_id, None)
                return

            view = MusicControlView(
                self,
                player=player,
                notice=self._panel_notices.get(guild_id),
            )
            view.refresh_buttons(player)

            self._bumping_guilds.add(guild_id)
            self._sending_panel_guilds.add(guild_id)
            try:
                new_msg = await channel.send(view=view)
                self._known_panel_msg_ids.append(new_msg.id)
                # 若在發送新面板的網路等待期間，播放已經停止／結束或面板已被拋棄，立即收回這則新面板並中止
                if not self._is_playback_active(player) or self.panel_messages.get(guild_id) != (channel_id, old_message_id):
                    await self._delete_panel_message(channel, new_msg.id)
                    self._bump_trigger_msgs.pop(guild_id, None)
                    self._bump_due_at.pop(guild_id, None)
                    return

                self._last_bump_at[guild_id] = time.monotonic()
                self.panel_messages[guild_id] = (channel_id, new_msg.id)
                self._guild_panel_history.setdefault(guild_id, set()).add((channel_id, new_msg.id))
                remaining = {
                    mid for mid in (self._bump_trigger_msgs.get(guild_id) or set()) if mid > new_msg.id
                }
                if remaining:
                    self._bump_trigger_msgs[guild_id] = remaining
                else:
                    self._bump_trigger_msgs.pop(guild_id, None)
                    self._bump_due_at.pop(guild_id, None)
                pos = getattr(player, "position", 0)
                length = getattr(player.current, "length", 0)
                is_stream = bool(getattr(player.current, "is_stream", False))
                self._last_progress_str[guild_id] = build_progress_bar(pos, length, is_stream=is_stream)
            except discord.HTTPException as exc:
                if getattr(exc, "status", None) == 429:
                    retry_after = float(getattr(exc, "retry_after", 5.0) or 5.0)
                    self._rate_limit_until[guild_id] = time.monotonic() + retry_after
                print(f"⚠️ [面板自動置底] 發送新面板失敗 (guild_id={guild_id})：{exc}")
                return
            finally:
                self._sending_panel_guilds.discard(guild_id)
                self._bumping_guilds.discard(guild_id)

            # 新面板發送成功後刪除所有舊面板（含本次 old_message_id 與任何先前未刪成功的舊面板）
            await self._cleanup_old_panels(guild_id, keep_message_id=new_msg.id, default_channel=channel)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """監聽點歌頻道的新訊息：當普通訊息出現在控制面板下方時，啟動防抖自動置底。"""
        if message.guild is None:
            return
        guild_id = message.guild.id

        # 1. 正在發送面板期間，或訊息本身就是控制面板訊息時，絕不觸發置底（防止自我觸發無限迴圈）
        if guild_id in self._sending_panel_guilds:
            return
        if self._is_panel_message(message):
            return

        # 檢查是否為 /music_queue 產生的公開佇列訊息（雖然為機器人訊息，但出現於面板下方時應觸發面板置底）
        is_queue_message = False
        if hasattr(message, "interaction_metadata"):
            interaction_meta = message.interaction_metadata
        else:
            interaction_meta = getattr(message, "interaction", None)

        if interaction_meta and getattr(interaction_meta, "name", None) == "music_queue":
            is_queue_message = True
        elif getattr(message, "embeds", None):
            for emb in message.embeds:
                if getattr(emb, "title", None) == "🎵 播放佇列":
                    is_queue_message = True
                    break

        author = getattr(message, "author", None)
        if not is_queue_message and author is not None:
            # 僅忽略本機器人自己發送的非 queue 訊息（避免自我觸發置底迴圈）；
            # 其他機器人（如 Jockie Music、Mee6 等）發送的訊息出現在面板下方時照常觸發置底
            if self.bot.user is not None and getattr(author, "id", None) == getattr(self.bot.user, "id", None):
                return

        panel_info = self.panel_messages.get(guild_id)
        if not panel_info:
            return
        panel_channel_id, panel_msg_id = panel_info
        if message.channel.id != panel_channel_id or message.id <= panel_msg_id:
            return

        # 2. 忽略 ephemeral 或互動 defer「思考中」（loading）的暫時訊息
        flags = getattr(message, "flags", None)
        if flags is not None and (getattr(flags, "ephemeral", False) or getattr(flags, "loading", False)):
            return

        player = getattr(message.guild, "voice_client", None)
        if not self._is_playback_active(player):
            return

        self._bump_trigger_msgs.setdefault(guild_id, set()).add(message.id)
        self._schedule_panel_bump(guild_id)

    def _handle_trigger_message_deleted(self, guild_id: int | None, message_id: int | None) -> None:
        """當面板下方的暫時訊息被刪除且無其他觸發訊息時，取消不必要的置底重發。"""
        if guild_id is None or message_id is None:
            return
        triggers = self._bump_trigger_msgs.get(guild_id)
        if not triggers:
            return
        triggers.discard(message_id)
        if not triggers:
            self._cancel_panel_bump(guild_id)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """監聽快取內的訊息刪除事件。"""
        if message.guild is None:
            return
        self._handle_trigger_message_deleted(message.guild.id, getattr(message, "id", None))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """監聽包含未快取訊息在內的原始刪除事件，確保暫時訊息刪除後可靠取消置底。"""
        self._handle_trigger_message_deleted(
            getattr(payload, "guild_id", None), getattr(payload, "message_id", None)
        )

    # ---------- 第 7 週新增：Spotify 歌單批次匯入 ----------
    # 整體設計（為何這樣拆）：
    # 1. /music_play 收到 Spotify 連結只做「驗證 + 建立背景任務」就立刻返回，
    #    Gateway 事件迴圈與其他指令（/pause、/skip…）完全不會被匯入拖慢。
    # 2. 背景任務：先取得曲目清單 → 逐首嘗試直到第一首解析成功就立刻播放 →
    #    其餘曲目「分批」解析（每批最多 IMPORT_BATCH_SIZE 首，批內用 Semaphore 限制並發），
    #    每批結果依原順序一次加入佇列。
    # 3. 只在批次解析完成後「同步地」append，沒有 await 夾在中間，所以被取消時
    #    已解析但尚未加入的曲目自然被丟棄，不會留下半批資料。
    @staticmethod
    def _is_player_idle(player) -> bool:
        """播放器是否閒置：沒有曲目在播／暫停，且沒有正在接下一首（避免與 track_end 互搶）。"""
        return (
            not (player.playing or player.paused)
            and getattr(player, "current", None) is None
            and not getattr(player, "advancing", False)
        )

    async def _start_if_idle(self, player) -> None:
        """播放器閒置且佇列有歌時，取出第一首開始播放（匯入的第一批曲目、或播完後才到的下一批）。"""
        if not self._is_player_idle(player) or not player.song_queue:
            return
        track, requester_name, requester_id = self._split_queue_item(player.song_queue.popleft())
        await self._play_track(player, track, requester_name, requester_id)

    def _cancel_import(self, guild_id: int) -> bool:
        """取消指定伺服器的背景匯入任務並清理狀態；回傳是否真的有任務被取消。

        會立刻把任務從字典移除（不等任務真正結束），所以同一伺服器可以馬上開始新的匯入；
        任務結束時的 done callback 只會移除「自己」，不會誤刪新任務。
        """
        task = self._import_tasks.pop(guild_id, None)
        cancelled = False
        if task is not None and not task.done():
            task.cancel()
            cancelled = True
        # 匯入進度提示（📥 開頭）要立刻清掉，否則取消後面板會一直顯示「匯入中」
        notice = self._panel_notices.get(guild_id)
        if notice and notice.startswith("📥"):
            self._panel_notices.pop(guild_id, None)
        return cancelled

    async def _ephemeral_error(self, interaction: discord.Interaction, text: str) -> None:
        """defer 之後要回報錯誤給使用者看（只有自己看得到）。

        先刪掉公開的「思考中」訊息再送 ephemeral：defer 出來的第一則 followup 會變成原訊息，
        無法再改成 ephemeral，所以先刪掉才不會把錯誤訊息公開在頻道上。
        """
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass
        try:
            await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException as e:
            print(f">>> 傳送 Spotify 錯誤訊息失敗: {e}")

    async def _search_spotify_track(self, sem: asyncio.Semaphore, st: SpotifyTrack):
        """把一首 Spotify 曲目轉成可播放的 Playable：搜尋「曲名 第一位歌手」，取第一筆。

        Semaphore 限制同時搜尋數量（保護 Lavalink 節點與 512MB 記憶體）；
        單首逾時／失敗只回傳 None，不影響整批。
        """
        preset = getattr(st, "playable", None)
        if preset is not None:
            return preset  # YouTube 清單：Lavalink 已解析完成，無須再搜尋
        async with sem:
            try:
                found = await asyncio.wait_for(
                    self._fetch_tracks_with_fallback(st.search_query()), timeout=IMPORT_SEARCH_TIMEOUT
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 含 TimeoutError：單首失敗只記錄，不中斷整批
                print(f">>> Spotify 曲目搜尋失敗（略過）「{st.search_query()}」：{type(e).__name__}: {e}")
                return None
        if not found:
            return None
        if isinstance(found, wavelink.Playlist):
            return found.tracks[0] if found.tracks else None
        return found[0]

    async def _start_spotify_import(
        self, interaction: discord.Interaction, player, query: str, shuffle: bool = False
    ) -> None:
        """/music_play 的 Spotify / YouTube 播放清單入口：驗證後建立背景任務並立刻返回（呼叫端已 defer）。"""
        yt_url = parse_youtube_playlist_url(query)
        parsed = ("youtube", "playlist") if yt_url else parse_spotify_url(query)
        if parsed is None:
            await self._ephemeral_error(
                interaction,
                "❌ 無法辨識這個 Spotify 連結。目前支援歌單／專輯／單曲，例如 "
                "`https://open.spotify.com/playlist/...` 或 `spotify:album:...`。",
            )
            return
        if not yt_url and not self.spotify.enabled:
            await self._ephemeral_error(
                interaction, "ℹ️ Spotify 匯入功能目前未啟用（管理員尚未設定 Spotify 金鑰），請改用歌名或 YouTube 連結點歌。"
            )
            return

        guild_id = interaction.guild.id
        running = self._import_tasks.get(guild_id)
        if running is not None and not running.done():
            await self._ephemeral_error(
                interaction, "⌛ 這個伺服器已經有一份歌單正在匯入，請等它完成（或先 `/music_stop`）再匯入新的。"
            )
            return

        kind, spotify_id = parsed
        task = asyncio.create_task(
            self._run_spotify_import(interaction, player, kind, spotify_id, yt_url=yt_url, shuffle=shuffle)
        )
        self._import_tasks[guild_id] = task

        def _on_done(t: asyncio.Task, gid: int = guild_id) -> None:
            # 只移除「自己」：任務可能已被取消並由新任務取代
            if self._import_tasks.get(gid) is t:
                self._import_tasks.pop(gid, None)

        task.add_done_callback(_on_done)

    async def _run_spotify_import(
        self,
        interaction: discord.Interaction,
        player,
        kind: str,
        spotify_id: str,
        yt_url: str | None = None,
        shuffle: bool = False,
    ) -> None:
        """背景匯入主流程（每個伺服器同時最多一個），自動檢查並過濾重複歌曲。

        yt_url 有值時改為匯入 YouTube 播放清單；shuffle 為 True 時只在匯入當下將整份清單（含第一首）隨機排列。
        """
        label = "YouTube" if yt_url else "Spotify"
        guild_id = interaction.guild.id
        requester_name = interaction.user.display_name
        requester_id = interaction.user.id
        ok = fail = done = dup = 0
        thinking_cleared = False

        async def clear_thinking():
            nonlocal thinking_cleared
            if thinking_cleared:
                return
            thinking_cleared = True
            try:
                await interaction.delete_original_response()
            except (discord.NotFound, discord.HTTPException):
                pass

        def alive() -> bool:
            guild = self.bot.get_guild(guild_id)
            return guild is not None and guild.voice_client is player and getattr(player, "connected", True)

        try:
            # ---- 1. 取得曲目清單（只保留曲名／歌手／時長）----
            try:
                if yt_url:
                    result = await self._fetch_youtube_playlist(yt_url, IMPORT_MAX_TRACKS)
                else:
                    result = await self.spotify.fetch_tracks(kind, spotify_id, IMPORT_MAX_TRACKS)
            except SpotifyError as e:
                await self._ephemeral_error(interaction, f"❌ {e.message}")
                return
            except Exception as e:
                if not yt_url:
                    raise
                print(f">>> YouTube 播放清單解析失敗：{redact_secrets(str(e))}")
                await self._ephemeral_error(
                    interaction, "❌ 無法讀取這份 YouTube 播放清單，可能是私人清單、網址無效或來源暫時無法連線。"
                )
                return
            tracks = list(result.tracks)
            if shuffle:
                random.shuffle(tracks)  # 只在匯入當下整份隨機排列（含第一首）
            total = len(tracks)
            if total == 0:
                await self._ephemeral_error(interaction, f"😕 這份 {label} 內容裡沒有可以匯入的曲目。")
                return

            sem = asyncio.Semaphore(IMPORT_CONCURRENCY)
            index = 0
            queued_position = 0   # 單曲被排進佇列時的位置（0 代表直接開始播放）

            # ---- 2. 第一首：逐首嘗試到成功為止，立刻開始播放，使用者不必等整批 ----
            while index < total and ok == 0:
                st = tracks[index]
                index += 1
                done += 1
                playable = await self._search_spotify_track(sem, st)
                if playable is None:
                    fail += 1
                    continue
                if not alive():
                    return

                was_idle = self._is_player_idle(player)
                player.song_queue.append((playable, requester_name, requester_id))
                ok += 1
                if not was_idle:
                    queued_position = len(player.song_queue)
                await self._start_if_idle(player)
                await clear_thinking()

            if ok == 0 and fail >= total:
                await self._ephemeral_error(interaction, f"😕 這份 {label} 內容的曲目都找不到對應的音源，請稍後再試。")
                return

            # ---- 3. 其餘曲目：分批解析後依序加入佇列 ----
            trunc_hint = f"（已截斷至前 {IMPORT_MAX_TRACKS} 首）" if result.truncated else ""
            last_progress = 0.0
            while index < total:
                batch = tracks[index:index + IMPORT_BATCH_SIZE]
                index += len(batch)
                results = await asyncio.gather(*(self._search_spotify_track(sem, st) for st in batch))
                if not alive():
                    return  # 已離開語音頻道：解析好的曲目直接丟棄
                # 這一段沒有 await：整批「全進或全不進」，被取消時不會留下半批
                for playable in results:
                    done += 1
                    if playable is None:
                        fail += 1
                        continue
                    player.song_queue.append((playable, requester_name, requester_id))
                    ok += 1
                await self._start_if_idle(player)

                # 進度更新節流：沿用第 6 週的間隔與 429 退避記錄，避免頻繁 edit 觸發 rate limit
                now = time.monotonic()
                if now - last_progress >= IMPORT_PROGRESS_INTERVAL and now >= self._rate_limit_until.get(guild_id, 0.0):
                    last_progress = now
                    self._set_panel_notice(
                        guild_id,
                        f"📥 **{label} 匯入中**：已處理 {done}/{total}（成功 {ok}／失敗 {fail}）{trunc_hint}",
                        duration=None,
                    )
                    await self._sync_panel(guild_id, player)

            # ---- 4. 結果回報（寫在面板底部，一段時間後自動消失）----
            if total == 1:
                if queued_position:
                    st_title = tracks[0].title
                    await self._notify_panel(
                        guild_id,
                        f"➕ **已新增至待播清單（第 {queued_position} 位）**：{st_title} 由 {requester_name} 點播",
                    )
            else:
                extras = []
                if dup > 0:
                    extras.append(f"忽略 {dup} 首重複歌曲")
                if result.truncated:
                    extras.append(f"超過上限，僅匯入前 {IMPORT_MAX_TRACKS} 首")
                if result.skipped:
                    extras.append(f"略過 {result.skipped} 首無法轉譯的項目")
                tail = f"（{'；'.join(extras)}）" if extras else ""
                self._panel_notices.pop(guild_id, None)
                await self._notify_panel(
                    guild_id, f"✅ **{label} 匯入完成**：成功 {ok} 首／失敗 {fail} 首{tail}", duration=15.0
                )

            # 匯入期間曲目可能已全部播完（面板在 track_end 時刻意沒有收尾），此時補上結束狀態
            if alive() and self._is_player_idle(player) and not player.song_queue:
                await self._update_panel_ended(guild_id, reason="📭 播放佇列已經全部播完囉，輸入 `/music_play` 繼續點歌吧！")
        except asyncio.CancelledError:
            raise  # 被 /stop、/leave 等取消：清理已在 _cancel_import 完成
        except Exception as e:
            print(f">>> {label} 匯入發生未預期錯誤：{redact_secrets(str(e))}")
            await self.bot.notify_owner_error(e, interaction, extra_info=f"{label} 匯入失敗 kind={kind}")
            await self._ephemeral_error(interaction, f"❌ 匯入 {label} 內容時發生錯誤，已回報開發者。")
        finally:
            # 保險：若任務是自然結束／出錯，清掉殘留的「匯入中」提示
            notice = self._panel_notices.get(guild_id)
            if notice and notice.startswith("📥"):
                self._panel_notices.pop(guild_id, None)

    # ==========================================================
    # 用戶歌單（User Playlists）曲目序列化與解析輔助方法
    # ==========================================================
    @staticmethod
    def _playable_to_playlist_dict(track, fallback_query: str | None = None) -> dict:
        """將 wavelink.Playable（或相容曲目物件）轉為可寫入 SQLite user_playlists 的 dict。"""
        title = str(getattr(track, "title", None) or "未知曲目").strip()
        author = str(getattr(track, "author", None) or "未知作者").strip()
        uri = getattr(track, "uri", None)
        identifier = getattr(track, "identifier", None)
        length = int(getattr(track, "length", 0) or 0)
        is_stream = bool(getattr(track, "is_stream", False))
        is_seekable = bool(getattr(track, "is_seekable", not is_stream))
        source = getattr(track, "source", None)
        artwork = get_track_artwork(track)
        encoded = getattr(track, "encoded", None)
        query = fallback_query or uri or f"{title} {author}".strip()
        return {
            "title": title,
            "author": author,
            "uri": uri,
            "identifier": identifier,
            "length": length,
            "is_stream": is_stream,
            "is_seekable": is_seekable,
            "source": source,
            "artwork": artwork,
            "encoded": encoded,
            "query": query,
        }

    @staticmethod
    def _playlist_dict_to_playable(item: dict):
        """從 SQLite 歌單曲目 dict 還原為 wavelink.Playable 或相容物件。"""
        title = str(item.get("title") or "未知曲目")
        author = str(item.get("author") or "未知作者")
        uri = item.get("uri")
        identifier = item.get("identifier") or ""
        length = int(item.get("length") or 0)
        is_stream = bool(item.get("is_stream", False))
        is_seekable = bool(item.get("is_seekable", not is_stream))
        source = item.get("source") or "youtube"
        artwork = item.get("artwork") or item.get("artwork_url") or get_track_artwork(item)
        encoded = item.get("encoded")
        query = item.get("query") or uri or f"{title} {author}".strip()

        if wavelink is not None and encoded:
            try:
                raw_payload = {
                    "encoded": encoded,
                    "info": {
                        "identifier": identifier,
                        "isSeekable": is_seekable,
                        "author": author,
                        "length": length,
                        "isStream": is_stream,
                        "position": 0,
                        "title": title,
                        "uri": uri,
                        "artworkUrl": artwork,
                        "isrc": None,
                        "sourceName": source,
                    },
                    "pluginInfo": {},
                    "userData": {},
                }
                playable = wavelink.Playable(raw_payload)
                playable._from_playlist_dict = True
                playable._playlist_query = query
                return playable
            except Exception:
                pass

        return SimpleNamespace(
            title=title,
            author=author,
            uri=uri,
            identifier=identifier or None,
            length=length,
            is_stream=is_stream,
            is_seekable=is_seekable,
            source=source,
            artwork=artwork,
            encoded=encoded,
            _from_playlist_dict=True,
            _playlist_query=query,
        )

    async def _resolve_playlist_track_obj(self, track):
        """當歌單曲目缺少 encoded（例如由網頁控制面板新增）時，向 Lavalink 即時查詢並補齊 Playable。"""
        if wavelink is None:
            return None
        query = (
            getattr(track, "_playlist_query", None)
            or getattr(track, "uri", None)
            or f"{getattr(track, 'title', '')} {getattr(track, 'author', '')}".strip()
        )
        if not query:
            return None
        try:
            tracks = await self._fetch_tracks_with_fallback(query)
            if tracks:
                return tracks[0] if not isinstance(tracks, wavelink.Playlist) else tracks.tracks[0]
        except Exception as e:
            print(f">>> 解析歌單曲目失敗 ({query}): {e}")
        return None

    def _get_guild_public_playlists(self, guild: discord.Guild | None, exclude_user_id: int) -> list[dict]:
        """取得該伺服器內其他成員的公開歌單清單。"""
        all_public = get_all_public_playlists(exclude_user_id=exclude_user_id)
        if guild is None:
            return all_public
        result = []
        for pl in all_public:
            try:
                owner_id = int(pl["user_id"])
            except (TypeError, ValueError):
                continue
            member = guild.get_member(owner_id)
            if member is not None:
                pl_copy = dict(pl)
                pl_copy["owner_name"] = member.display_name
                result.append(pl_copy)
            elif not getattr(guild, "chunked", False) and len(guild.members) <= 5:
                # 若伺服器成員快取尚未完整載入，保留公開歌單以免漏列
                result.append(pl)
        return result

    def _resolve_playlist_selection(
        self, interaction: discord.Interaction, raw_value: str
    ) -> tuple[dict | None, str | None, bool]:
        """解析 /music_play 的 playlist 參數。

        回傳 (playlist_dict, error_message, used_share_code)
        - 當使用者輸入 6 碼英數分享碼時，會自動為該使用者儲存該歌單（寫入 user_saved_playlists），
          之後在 /music_play 的 playlist 選單中就會直接顯示。
        """
        val = (raw_value or "").strip()
        if not val:
            return None, "❌ 請輸入或選擇有效的歌單名稱或 6 碼分享碼。", False

        user_id = interaction.user.id

        # 1. 選單值前綴：own:<uid> / saved:<uid> / pub:<uid>
        if val.startswith("own:"):
            pl = get_user_playlist(user_id)
            if not pl:
                return None, "❌ 你目前還沒有建立個人歌單，請先使用 `/music_playlist_create` 建立。", False
            return pl, None, False

        if val.startswith("saved:"):
            target_uid = val.split(":", 1)[1].strip()
            saved_list = get_saved_playlists_for_user(user_id)
            for s in saved_list:
                if str(s["user_id"]) == target_uid:
                    return s, None, False
            return (
                None,
                "❌ 該收藏歌單已無法存取（擁有者可能已重新產生分享碼或刪除了歌單）。",
                False,
            )

        if val.startswith("pub:"):
            target_uid = val.split(":", 1)[1].strip()
            pl = get_user_playlist(target_uid)
            if not pl or not pl.get("is_public"):
                return None, "❌ 找不到該公開歌單，或擁有者已將其設為私人。", False
            return pl, None, False

        # 2. 檢查是否為 6 碼英數分享碼（不分大小寫）
        cleaned_code = val.upper()
        if len(cleaned_code) == 6 and cleaned_code.isalnum():
            pl = get_playlist_by_share_code(cleaned_code)
            if pl is not None:
                if str(pl["user_id"]) != str(user_id):
                    save_shared_playlist_for_user(user_id, pl["user_id"], pl["share_code"])
                return pl, None, True

        # 3. 依歌單名稱比對（先找自己的歌單 -> 已儲存的分享歌單 -> 伺服器公開歌單）
        val_lower = val.lower()
        own_pl = get_user_playlist(user_id)
        if own_pl and (own_pl["name"].lower() == val_lower or own_pl["share_code"].lower() == val_lower):
            return own_pl, None, False

        for s in get_saved_playlists_for_user(user_id):
            if s["name"].lower() == val_lower or s["share_code"].lower() == val_lower:
                return s, None, False

        for p in self._get_guild_public_playlists(interaction.guild, exclude_user_id=user_id):
            if p["name"].lower() == val_lower:
                return p, None, False

        if len(cleaned_code) == 6 and cleaned_code.isalnum():
            return None, f"❌ 找不到分享碼為 `{cleaned_code}` 的歌單（可能輸入錯誤，或擁有者已重新產生分享碼導致舊碼失效）。", False

        return None, f"❌ 找不到名稱或分享碼為「`{val}`」的歌單。", False

    def _find_queue_duplicate(self, player, track) -> tuple[str, int] | None:
        """檢查曲目是否已在目前播放中或待播佇列中。
        若重複，回傳 ("current", 0) 或 ("queue", 佇列序號 1-based)；若無重複回傳 None。
        """
        current_track = getattr(player, "current", None) or getattr(player, "_active_track", None)
        if current_track is not None and are_tracks_duplicate(track, current_track):
            return ("current", 0)

        song_queue = getattr(player, "song_queue", None)
        if song_queue:
            for idx, item in enumerate(song_queue):
                q_track = item[0] if isinstance(item, (tuple, list)) and item else item
                if q_track is not None and are_tracks_duplicate(track, q_track):
                    return ("queue", idx + 1)

        return None

    async def _play_user_playlist(
        self,
        interaction: discord.Interaction,
        player,
        playlist_data: dict,
        used_share_code: bool = False,
        shuffle: bool = False,
    ) -> None:
        """將整個用戶歌單載入播放佇列，自動過濾與現有播放中/佇列重複之歌曲，若目前閒置則立即開始播放第一首。"""
        tracks_raw = playlist_data.get("tracks") or []
        pl_name = playlist_data.get("name", "未命名歌單")
        owner_name = playlist_data.get("owner_name") or "未知用戶"
        is_own = str(playlist_data.get("user_id")) == str(interaction.user.id)

        if not tracks_raw:
            await self._ephemeral_error(
                interaction,
                f"😕 歌單「**{pl_name}**」目前沒有任何歌曲！請先使用 `/music_playlist_add` 或 `/music_playlist_sync` 新增歌曲。",
            )
            return

        playables = [self._playlist_dict_to_playable(item) for item in tracks_raw[:PLAYLIST_MAX_TRACKS]]
        if shuffle:
            random.shuffle(playables)  # 只在指定 shuffle 的當下整份歌單隨機排列（含第一首）
        requester_name = interaction.user.display_name
        requester_id = interaction.user.id

        was_idle = self._is_player_idle(player)
        started_first = False

        for idx, track_obj in enumerate(playables):
            if was_idle and not started_first:
                try:
                    await self._play_track(player, track_obj, requester_name, requester_id)
                    started_first = True
                except Exception as e:
                    if self._is_session_lost_error(e):
                        player.song_queue.extend(
                            (t, requester_name, requester_id) for t in playables[idx:]
                        )
                        await self._handle_session_lost(player, e)
                        await self._ephemeral_error(
                            interaction,
                            f"🔄 音訊節點連線剛重置，正在自動重新連線，完成後將立即播放歌單「{pl_name}」。",
                        )
                        return
                    print(f">>> 播放歌單首曲失敗，嘗試下一首：{e}")
                    continue
            else:
                player.song_queue.append((track_obj, requester_name, requester_id))

        if was_idle and not started_first:
            await self._ephemeral_error(interaction, f"❌ 歌單「**{pl_name}**」內的曲目目前皆無法播放，請稍後再試。")
            return

        owner_tag = "你的歌單" if is_own else f"{owner_name} 的歌單"
        saved_hint = "（已透過分享碼自動收藏至你的歌單選單）" if (used_share_code and not is_own) else ""
        shuffle_hint = "（🔀 已隨機排列）" if shuffle else ""
        notice = (
            f"📀 **已載入{owner_tag}「{pl_name}」**：共 {len(playables)} 首曲目已排入播放佇列{shuffle_hint}"
            f" 由 {requester_name} 點播{saved_hint}"
        )
        self._set_panel_notice(interaction.guild.id, notice, duration=10.0)
        await self._sync_panel(interaction.guild.id, player)

        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass

        if used_share_code and not is_own:
            try:
                await interaction.followup.send(
                    f"✅ 已透過分享碼 `{playlist_data.get('share_code')}` 播放 **{owner_name}** 的歌單「**{pl_name}**」（共 {len(playables)} 首{shuffle_hint}），並已自動儲存至你的 `/music_play` 歌單選單！",
                    ephemeral=True,
                )
            except (discord.NotFound, discord.HTTPException):
                pass

    @app_commands.command(name="music_play", description="搜尋並播放音樂或用戶歌單（query 與 playlist 請擇一填入）")
    @app_commands.describe(
        query="歌曲名稱 / 關鍵字，或是 YouTube、SoundCloud、Spotify 網址（與 playlist 擇一填入）",
        playlist="選擇你的歌單、伺服器公開歌單，或直接輸入 6 碼分享碼跨伺服器播放（與 query 擇一填入）",
        shuffle="是否開啟隨機播放模式（開啟時自動打亂待播佇列順序，預設關閉）",
    )
    @app_commands.guild_only()
    async def play(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
        playlist: str | None = None,
        shuffle: bool = False,
    ):
        if not await self._check_music_channel(interaction):
            return
        if wavelink is None:
            await interaction.response.send_message("❌ 語音模組尚未安裝完成，請聯絡管理員。", ephemeral=True)
            return

        has_query = bool(query and query.strip())
        has_playlist = bool(playlist and playlist.strip())
        if not has_query and not has_playlist:
            await interaction.response.send_message(
                "❌ 請在 `query`（歌曲關鍵字／網址）與 `playlist`（歌單名稱／6 碼分享碼）之間**擇一填入**！",
                ephemeral=True,
            )
            return
        if has_query and has_playlist:
            await interaction.response.send_message(
                "❌ `query` 與 `playlist` 參數只能**擇一填入**，請勿同時填寫！",
                ephemeral=True,
            )
            return

        # 搜尋 + 連線語音頻道都可能花超過 3 秒，先 defer 避免 Interaction 逾時（沿用 /weather 的慣例）。
        try:
            await interaction.response.defer(thinking=True)
        except (discord.NotFound, discord.HTTPException) as e:
            print(f">>> /play interaction.defer() 失敗: {e}")
            return

        # 若選擇播放歌單，先驗證歌單是否存在，避免歌單無效時還讓機器人白跑進語音房
        if has_playlist:
            pl_data, err_msg, used_share_code = self._resolve_playlist_selection(interaction, playlist)
            if pl_data is None:
                await self._ephemeral_error(interaction, err_msg or "❌ 找不到指定的歌單。")
                return
            if not (pl_data.get("tracks") or []):
                await self._ephemeral_error(
                    interaction,
                    f"😕 歌單「**{pl_data.get('name', '未命名')}**」目前是空的，請先使用 `/music_playlist_add` 新增歌曲。",
                )
                return

            player = await self._ensure_player(interaction)
            if player is None:
                return
            if shuffle:
                player.shuffle = True
            await self._play_user_playlist(
                interaction, player, pl_data, used_share_code=used_share_code, shuffle=shuffle
            )
            return

        player = await self._ensure_player(interaction)
        if player is None:
            return  # 錯誤訊息已經在 _ensure_player 內送出

        if shuffle:
            player.shuffle = True

        # 第 7 週新增：Spotify 連結改走背景匯入流程（立刻返回，不阻塞 Gateway 與其他指令）
        if looks_like_spotify(query) or parse_youtube_playlist_url(query):
            await self._start_spotify_import(interaction, player, query, shuffle=shuffle)
            return

        track = await self._search_one_track(interaction, query)
        if track is None:
            return  # 錯誤訊息已經在 _search_one_track 內送出

        # ---- 第3週更新：有佇列了，不再直接蓋掉正在播放的歌曲 ----
        # player.playing 代表現在有歌在播、player.paused 代表暫停中但還沒播完，
        # 這兩種狀態都視為「有東西在佔用播放器」，新點的歌一律排到佇列尾端（FIFO）。
        # 第 6 週更新：刪除「已新增樂曲」獨立 Embed，改在 Container 底部即時顯示提示 Banner，
        # 一段時間後自動清除，並刪除 defer 訊息避免洗版。
        if not self._is_player_idle(player):
            player.song_queue.append((track, interaction.user.display_name, interaction.user.id))
            position = len(player.song_queue)
            track_title = getattr(track, "title", "未知曲目")
            track_uri = getattr(track, "uri", None)
            title_display = f"[{track_title}]({track_uri})" if track_uri else track_title
            shuffle_tag = f"（第 {position} 位）"
            notice = f"➕ **已新增至待播清單{shuffle_tag}**：{title_display} 由 {interaction.user.display_name} 點播"
            self._set_panel_notice(interaction.guild.id, notice, duration=8.0)
            await self._sync_panel(interaction.guild.id, player)
            try:
                await interaction.delete_original_response()
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        # ---- 佇列與播放器都是空的，直接開始播放 ----
        try:
            await self._play_track(player, track, interaction.user.display_name, interaction.user.id)
        except Exception as e:
            if self._is_session_lost_error(e):
                await self._handle_session_lost(player, e)
                await self._ephemeral_error(
                    interaction,
                    f"🔄 音訊節點連線剛重置，正在自動重新連線，完成後將立即播放「{getattr(track, 'title', '此曲目')}」。",
                )
                return
            print(f">>> 播放音樂時發生錯誤：{e}")
            await self.bot.notify_owner_error(
                e, interaction, extra_info=f"/play 播放失敗 track={getattr(track, 'title', '?')}"
            )
            await self._ephemeral_error(interaction, "❌ 播放音樂時發生錯誤，已回報開發者。")
            return

        # 刪除 defer 訊息，避免洗版與刷掉控制面板（面板由 on_wavelink_track_start 自動產生或更新）
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass

    @play.autocomplete("playlist")
    async def play_playlist_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """自動補全 /music_play 的 playlist 選單：
        1. 用戶自己的歌單
        2. 用戶曾經透過 6 碼分享碼播放而自動儲存的歌單（若對方重產分享碼則自動從選單移除）
        3. 該伺服器內其他成員設定為「公開」的歌單
        4. 若正在輸入 6 碼分享碼，即時預覽對應的跨伺服器歌單
        """
        user_id = interaction.user.id
        kw = (current or "").strip().lower()
        choices: list[app_commands.Choice[str]] = []
        seen_owners: set[str] = set()

        # 1. 自己的歌單
        own_pl = get_user_playlist(user_id)
        if own_pl:
            owner_key = str(own_pl["user_id"])
            seen_owners.add(owner_key)
            count = len(own_pl.get("tracks") or [])
            vis_tag = "公開" if own_pl.get("is_public") else "私人"
            label = f"⭐ 我的歌單：{own_pl['name']}（{count} 首・{vis_tag}）[{own_pl['share_code']}]"
            if not kw or kw in own_pl["name"].lower() or kw in own_pl["share_code"].lower() or kw in "我的歌單":
                choices.append(app_commands.Choice(name=label[:100], value=f"own:{user_id}"))

        # 2. 已透過分享碼自動儲存的歌單
        saved_list = get_saved_playlists_for_user(user_id)
        for s in saved_list:
            owner_key = str(s["user_id"])
            if owner_key in seen_owners:
                continue
            seen_owners.add(owner_key)
            count = len(s.get("tracks") or [])
            owner_name = s.get("owner_name") or f"用戶 {owner_key}"
            label = f"🔖 已收藏：{s['name']} — {owner_name}（{count} 首）[{s['share_code']}]"
            if (
                not kw
                or kw in s["name"].lower()
                or kw in owner_name.lower()
                or kw in s["share_code"].lower()
            ):
                choices.append(app_commands.Choice(name=label[:100], value=f"saved:{owner_key}"))

        # 3. 伺服器內其他成員的公開歌單
        guild_public = self._get_guild_public_playlists(interaction.guild, exclude_user_id=user_id)
        for p in guild_public:
            owner_key = str(p["user_id"])
            if owner_key in seen_owners:
                continue
            seen_owners.add(owner_key)
            count = len(p.get("tracks") or [])
            owner_name = p.get("owner_name") or f"用戶 {owner_key}"
            label = f"🌐 公開歌單：{p['name']} — {owner_name}（{count} 首）"
            if (
                not kw
                or kw in p["name"].lower()
                or kw in owner_name.lower()
                or kw in p.get("share_code", "").lower()
            ):
                choices.append(app_commands.Choice(name=label[:100], value=f"pub:{owner_key}"))

        # 4. 若使用者輸入了 6 碼分享碼且不在上述清單中，即時查詢對應歌單方便點選
        raw_code = (current or "").strip().upper()
        if len(raw_code) == 6 and raw_code.isalnum():
            shared_pl = get_playlist_by_share_code(raw_code)
            if shared_pl and str(shared_pl["user_id"]) not in seen_owners:
                count = len(shared_pl.get("tracks") or [])
                owner_name = shared_pl.get("owner_name") or f"用戶 {shared_pl['user_id']}"
                label = f"🔑 分享碼 [{raw_code}]：{shared_pl['name']} — {owner_name}（{count} 首）"
                choices.insert(0, app_commands.Choice(name=label[:100], value=raw_code))

        return choices[:25]

    @app_commands.command(name="music_play_next", description="（插播）搜尋一首歌曲並插入佇列最前面，下一首就會播放它")
    @app_commands.describe(query="歌曲名稱 / 關鍵字，或是 YouTube、SoundCloud 網址")
    @app_commands.guild_only()
    async def play_next(self, interaction: discord.Interaction, query: str):
        if not await self._check_music_channel(interaction):
            return
        if wavelink is None:
            await interaction.response.send_message("❌ 語音模組尚未安裝完成，請聯絡管理員。", ephemeral=True)
            return

        try:
            await interaction.response.defer(thinking=True)
        except (discord.NotFound, discord.HTTPException) as e:
            print(f">>> /play_next interaction.defer() 失敗: {e}")
            return

        player = await self._ensure_player(interaction)
        if player is None:
            return

        if looks_like_spotify(query):
            await self._ephemeral_error(interaction, "ℹ️ 插播暫不支援 Spotify 連結，請改用 `/music_play` 匯入。")
            return

        track = await self._search_one_track(interaction, query)
        if track is None:
            return

        if not self._is_player_idle(player):
            # 插播的重點：用 appendleft 塞到 FIFO 佇列的最前面，
            # 第 6 週更新：在 Container 底部即時顯示插播提示 Banner，一段時間後自動移除
            player.song_queue.appendleft((track, interaction.user.display_name, interaction.user.id))
            track_title = getattr(track, "title", "未知曲目")
            track_uri = getattr(track, "uri", None)
            title_display = f"[{track_title}]({track_uri})" if track_uri else track_title
            notice = f"⏭️ **已插播至待播清單（下一首）**：{title_display} 由 {interaction.user.display_name} 點播"
            self._set_panel_notice(interaction.guild.id, notice, duration=8.0)
            await self._sync_panel(interaction.guild.id, player)
            try:
                await interaction.delete_original_response()
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        # 目前沒有東西在播，插播跟一般 /play 沒有差別，直接播放。
        try:
            await self._play_track(player, track, interaction.user.display_name, interaction.user.id)
        except Exception as e:
            print(f">>> 插播音樂時發生錯誤：{e}")
            await self.bot.notify_owner_error(
                e, interaction, extra_info=f"/play_next 播放失敗 track={getattr(track, 'title', '?')}"
            )
            await self._ephemeral_error(interaction, "❌ 播放音樂時發生錯誤，已回報開發者。")
            return

        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass

    @app_commands.command(name="music_queue", description="顯示目前伺服器的播放佇列")
    @app_commands.guild_only()
    async def queue_(self, interaction: discord.Interaction):
        if not await self._check_music_channel(interaction):
            return
        player = interaction.guild.voice_client

        if player is None:
            await interaction.response.send_message("ℹ️ 我目前不在任何語音頻道中，也沒有播放佇列。", ephemeral=True)
            return

        current = getattr(player, "current", None)
        song_queue: deque = getattr(player, "song_queue", None) or deque()

        if current is None and not song_queue:
            await interaction.response.send_message("📭 目前沒有正在播放的歌曲，佇列也是空的。", ephemeral=True)
            return

        lines: list[str] = []
        if current is not None:
            status = "⏸️ 暫停中" if player.paused else "▶️ 正在播放"
            lines.append(f"**{status}：** {current.title}（{format_duration(getattr(current, 'length', None))}）")
        else:
            lines.append("**▶️ 正在播放：** （無）")

        if song_queue:
            lines.append("")
            lines.append(f"**📜 接下來（共 {len(song_queue)} 首）：**")
            queue_snapshot = list(song_queue)
            for idx, queue_item in enumerate(queue_snapshot[:10], start=1):
                queued_track, requester_name, _ = self._split_queue_item(queue_item)
                lines.append(
                    f"{idx}. {queued_track.title}"
                    f"（{format_duration(getattr(queued_track, 'length', None))}）- 由 {requester_name} 點播"
                )
            if len(queue_snapshot) > 10:
                lines.append(f"...還有 {len(queue_snapshot) - 10} 首沒有列出")
        else:
            lines.append("\n📭 佇列目前是空的，播完這首就結束囉，輸入 `/music_play` 繼續點歌吧！")

        lines.append("")
        loop_str = LOOP_LABELS.get(getattr(player, "loop_mode", "off"), LOOP_LABELS["off"])
        is_shuffle = bool(getattr(player, "shuffle", False))
        shuffle_str = "🔀 隨機播放：開啟" if is_shuffle else "🔀 隨機播放：關閉"
        lines.append(f"{loop_str} ｜ {shuffle_str}")

        embed = discord.Embed(
            title="🎵 播放佇列",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)
        try:
            queue_msg = await interaction.original_response()
            queue_msg_id = getattr(queue_msg, "id", None)
            if queue_msg_id is not None and interaction.guild is not None:
                g_id = interaction.guild.id
                p_info = self.panel_messages.get(g_id)
                if p_info:
                    p_channel_id, p_msg_id = p_info
                    curr_ch_id = getattr(interaction, "channel_id", None) or getattr(getattr(interaction, "channel", None), "id", None)
                    if (curr_ch_id is None or curr_ch_id == p_channel_id) and queue_msg_id > p_msg_id:
                        if self._is_playback_active(player):
                            self._bump_trigger_msgs.setdefault(g_id, set()).add(queue_msg_id)
                            self._schedule_panel_bump(g_id)
        except Exception:
            pass

    @app_commands.command(name="music_queue_clear", description="清空目前的播放佇列（不影響正在播放的歌曲）")
    @app_commands.guild_only()
    async def queue_clear(self, interaction: discord.Interaction):
        if not await self._check_music_channel(interaction):
            return
        player = interaction.guild.voice_client
        song_queue: deque = getattr(player, "song_queue", None) if player else None

        if not song_queue:
            await interaction.response.send_message("ℹ️ 佇列目前是空的，沒有東西可以清除。", ephemeral=True)
            return

        removed_count = len(song_queue)
        song_queue.clear()
        # 第 7 週：清空佇列時一併取消背景匯入，已解析但未加入的曲目丟棄，避免清完又被塞回來
        self._cancel_import(interaction.guild.id)
        await self._sync_panel(interaction.guild.id, player)
        await interaction.response.send_message(
            f"🗑️ 已清空佇列，移除了 {removed_count} 首歌曲（正在播放的歌曲不受影響）。", ephemeral=True
        )

    def _cleanup_control_state(self, track) -> None:
        """曲目結束（換歌／被跳過／停止）時，清掉該曲的拒絕鎖與還在等的操作請求。

        「同一首歌被拒後不得再提請求」只限於這首歌的這次播放；
        下一首開始播放（或這首再次被播出）時就重新給機會。
        """
        if track is not None:
            self._rejected_track_keys.pop(self._track_key(track), None)
        for rid, state in list(self._pending_requests.items()):
            view_track = getattr(state.get("view"), "track", None)
            if view_track is track or (track is not None and self._is_same_track(view_track, track)):
                self._pending_requests.pop(rid, None)

    @classmethod
    def _is_same_track(cls, track_a, track_b) -> bool:
        """比對兩個曲目物件是否代表同一首歌。

        關鍵細節：Lavalink 在 TrackEndEvent 回傳的 track.encoded 二進位資料尾端會寫入最終播放位置（position），
        因此同一首歌在播完時的 encoded 字串必然與起播時（position=0）的 encoded 不同！
        若直接拿 encoded 做第一順位 return，會把正常播完的同一首歌誤判為不同歌曲而中斷自動連播。
        正確做法：只要 identifier、uri、title 或 encoded 任一有效欄位相同，即判定為同一首歌。
        """
        if track_a is None or track_b is None:
            return False
        if track_a is track_b:
            return True
        has_comparable = False
        for attr in ("identifier", "uri", "title", "encoded"):
            val_a = getattr(track_a, attr, None)
            val_b = getattr(track_b, attr, None)
            if val_a is not None and val_b is not None:
                has_comparable = True
                if val_a == val_b:
                    return True
        return not has_comparable

    # ---------- 第4週新增：播放控制 ----------
    @staticmethod
    def _shuffle_queue(player: Any) -> int:
        """將待播佇列（player.song_queue）中的歌曲順序隨機打亂（不包含正在播放中的曲目）。
        回傳打亂的歌曲數量。
        """
        song_queue = getattr(player, "song_queue", None)
        if not song_queue or len(song_queue) <= 1:
            return len(song_queue) if song_queue else 0

        items = list(song_queue)
        random.shuffle(items)
        song_queue.clear()
        song_queue.extend(items)
        return len(items)

    @staticmethod
    def _split_queue_item(queue_item: tuple) -> tuple:
        """把佇列項目拆成 (track, 點歌者名稱, 點歌者ID)。

        佇列只存在於記憶體，但 cog 重載時同一個 Player 可能殘留舊版的
        2-tuple (track, 名稱)，這裡相容兩種長度，避免直接解包噴 ValueError。
        舊資料沒有 ID，以 None 表示。
        """
        if len(queue_item) >= 3:
            track, requester_name, requester_id = queue_item[0], queue_item[1], queue_item[2]
        else:
            track, requester_name = queue_item[0], queue_item[1]
            requester_id = None
        return track, requester_name, requester_id

    @staticmethod
    def _clear_player_playback_state(player: Any) -> None:
        """徹底清除 Player 上的當前播放曲目與自訂追蹤欄位，確保停止／結束後不會被誤判仍在播放。"""
        if player is None:
            return
        cur = getattr(player, "current", None) or getattr(player, "_active_track", None)
        if cur is not None:
            player._stopped_track = cur
        player._active_track = None
        player._started_track = None
        player.current_requester = None
        player.current_requester_id = None
        player._is_current_stream = False
        player._retrying_live_stream = False
        player._live_retry_count = 0
        for attr in ("_current", "current"):
            try:
                setattr(player, attr, None)
            except Exception:
                pass

    @staticmethod
    def _is_playback_active(player: Any) -> bool:
        """判斷播放器目前是否處於有效播放／暫停狀態（已停止、斷線或結束時回傳 False）。"""
        if player is None:
            return False
        if not getattr(player, "connected", True):
            return False
        if getattr(player, "end_intent", None) == "stop":
            return False
        cur = getattr(player, "current", None)
        if cur is None:
            return False
        stopped_track = getattr(player, "_stopped_track", None)
        if stopped_track is not None and cur is stopped_track:
            return False
        return True

    async def _play_track(
        self,
        player: "wavelink_module.Player",
        track: "wavelink_module.Playable",
        requester_name: str,
        requester_id: int | None = None,
    ):
        """統一的開始播放入口：先記下「目前這首是誰點的」再播。

        循環模式要把剛結束的歌重新排回去，那時候佇列裡已經沒有這首歌的點播者資訊，
        所以必須在播放當下存進 player.current_requester。
        current_requester_id 給點歌者限定操作（_check_requester_control）用：
        名稱可能重複或改名，ID 才能可靠對應成員。
        """
        # 若是從歌單載入但尚未具備 encoded 音軌字串（例如離線由網頁新增），在起播前即時向 Lavalink 解析
        if getattr(track, "_from_playlist_dict", False) and not getattr(track, "encoded", None):
            resolved = await self._resolve_playlist_track_obj(track)
            if resolved is not None:
                track = resolved

        player.current_requester = requester_name
        player.current_requester_id = requester_id
        # 重置可能殘留的 end_intent 與停止標記，並記錄當前播放曲目與播放序號，防止舊事件或重複事件在關閉循環時跳過當前歌曲
        player.end_intent = None
        player._stopped_track = None
        player._active_track = track
        player._play_seq = getattr(player, "_play_seq", 0) + 1
        player._track_started_mono = time.monotonic()
        is_stream = self._is_live_track(track)
        player._is_current_stream = is_stream
        if is_stream:
            try:
                setattr(track, "_is_live_stream", True)
            except Exception:
                pass
        if not getattr(player, "_retrying_live_stream", False):
            player._live_retry_count = 0
        # 開始播放時清除這首歌之前的拒絕鎖（例如上次播放被拒、這次重新播出）
        self._rejected_track_keys.pop(self._track_key(track), None)
        guild_obj = getattr(player, "guild", None)
        if guild_obj is not None:
            self._track_overdue_since.pop(guild_obj.id, None)
        try:
            await player.play(track)
        except Exception as exc:
            if self._is_session_lost_error(exc):
                # 若是孤兒節點或新 Session 尚未同步語音狀態，先嘗試立即轉移節點並重送語音憑證後重試一次
                if self._adopt_orphan_player(player) and await self._sync_player_voice_to_lavalink(player):
                    await player.play(track)
                    return
            raise

    async def _get_control_player(self, interaction: discord.Interaction) -> "wavelink_module.Player | None":
        """控制指令共用的防呆：機器人在語音頻道、且操作者跟機器人在同一個頻道。
        回傳 None 代表已經回覆錯誤訊息，呼叫端直接 return。"""
        if wavelink is None:
            await interaction.response.send_message("❌ 語音模組尚未安裝完成，請聯絡管理員。", ephemeral=True)
            return None

        player = interaction.guild.voice_client
        if not isinstance(player, wavelink.Player):
            await interaction.response.send_message("ℹ️ 我目前不在任何語音頻道中。", ephemeral=True)
            return None

        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None or voice.channel.id != player.channel.id:
            await interaction.response.send_message(
                f"❌ 你必須跟我在同一個語音頻道（{player.channel.mention}）才能使用控制指令！", ephemeral=True
            )
            return None
        return player

    @staticmethod
    def _operator_stamp(interaction: discord.Interaction) -> str:
        """操作者戳記：加在播放控制指令的成功回覆後面，標記這次操作是誰做的。

        用 display_name 而不是 mention：戳記只是事後對照用，不希望每次
        暫停／跳過都在公開頻道 ping 操作者本人。
        """
        return f"（操作者：{interaction.user.display_name}）"

    # ---------- 點歌者限定操作 ----------
    def _get_current_requester(self, player: "wavelink_module.Player") -> tuple[int | None, str | None]:
        """取得當前曲目的點歌者 (ID, 名稱)；沒有播放中曲目或無紀錄回傳 (None, None)。"""
        if getattr(player, "current", None) is None:
            return None, None
        requester_id = getattr(player, "current_requester_id", None)
        requester_name = getattr(player, "current_requester", None)
        if requester_id is None:
            return None, None
        return requester_id, requester_name

    async def _requester_can_directly_control(self, interaction: discord.Interaction, player) -> bool:
        """操作者是否可以直接控制（點歌者本人，或點歌者已不在機器人的語音頻道）。
        必須在 _get_control_player 之後呼叫（此時 player.channel 一定存在）。"""
        requester_id, _ = self._get_current_requester(player)
        if requester_id is None:
            return True
        if interaction.user.id == requester_id:
            return True
        requester = interaction.guild.get_member(requester_id)
        if requester is None or requester.voice is None or requester.voice.channel is None \
                or requester.voice.channel.id != player.channel.id:
            return True  # 點歌者已不在機器人的語音頻道，開放任何人操作
        return False

    async def _check_requester_control(self, interaction: discord.Interaction) -> bool:
        """（skip / seek 用）點歌者限定：當前曲目的點歌者還在機器人所在語音頻道時，
        只有點歌者本人可以直接操作；點歌者已退出則任何人都能操作。

        回傳 False 時已用 ephemeral 訊息告知使用者，呼叫端應立即 return。
        """
        player = interaction.guild.voice_client
        if getattr(player, "current", None) is None:
            return True  # 沒有播放中曲目：不套用點歌者限定，讓呼叫端走原本的錯誤路徑

        if await self._requester_can_directly_control(interaction, player):
            return True

        requester_id, requester_name = self._get_current_requester(player)
        await interaction.response.send_message(
            f"🔒 這首歌是 {requester_name or '某人'} 點的，他還在語音頻道裡，只有他能操作！",
            ephemeral=True,
        )
        return False

    # ---------- 操作請求（允許/拒絕按鈕）與停止投票的狀態 ----------
    def _voice_voter_ids(self, player) -> set[int]:
        """取得機器人語音頻道內所有真人成員的 ID（投票資格）。"""
        channel = getattr(player, "channel", None)
        if channel is None:
            return set()
        return {m.id for m in channel.members if not m.bot}

    def _register_control_request(self, requester_id: int, action_label: str, view) -> None:
        """記錄進行中的操作請求；決定（允許/拒絕/逾時）後由 _resolve_control_request 移除。"""
        self._pending_requests[requester_id] = {"action": action_label, "view": view}

    def _resolve_control_request(self, requester_id: int, allowed: bool, view=None) -> None:
        """請求被決定（按鈕或逾時）時呼叫：移除進行中紀錄。
        被拒絕時寫入 _rejected_track_keys，同一首歌在下一首開始播放前不得再次提出。
        """
        self._pending_requests.pop(requester_id, None)
        track = getattr(view, "track", None) if view is not None else None
        if not allowed and track is not None:
            self._rejected_track_keys[self._track_key(track)] = True

    def _register_stop_vote(self, guild_id: int, view) -> None:
        self._stop_votes[guild_id] = view

    def _resolve_stop_vote(self, guild_id: int, approved: bool, view=None) -> None:
        """投票結束時移除進行中投票，讓下一輪 /music_stop 可以再發起。
        未通過（被否決或逾時結算為否決）時寫入 _rejected_track_keys，
        同一首歌在下一首開始播放前不得再次發起停止投票。
        """
        self._stop_votes.pop(guild_id, None)
        track = getattr(view, "track", None) if view is not None else None
        if not approved and track is not None:
            self._rejected_track_keys[self._track_key(track)] = True

    @staticmethod
    def _track_key(track) -> object:
        """曲目識別 key：優先用 identifier，沒有時退化用 id()（同一次播放物件內仍唯一）。"""
        key = getattr(track, "identifier", None)
        return key if key is not None else id(track)

    def _is_track_rejected(self, player) -> bool:
        """當前播放的這首歌是否已被拒絕過操作請求（拒絕鎖到換歌為止）。"""
        current = getattr(player, "current", None)
        if current is None:
            return False
        return self._track_key(current) in self._rejected_track_keys

    def _clear_rejection(self, track) -> None:
        """換到下一首歌（或停止播放）時清除該首歌的拒絕鎖。"""
        if track is None:
            return
        self._rejected_track_keys.pop(self._track_key(track), None)

    # ---------- 點歌頻道限制（/music_set_channel） ----------
    def _get_music_channel_id(self, guild_id: int) -> int | None:
        """取得伺服器設定的點歌頻道 ID；沒設定（不限頻道）回傳 None。"""
        setting = self.music_channel_settings.get(str(guild_id))
        if not setting:
            return None
        return setting.get('music_channel_id')

    async def _check_music_channel(self, interaction: discord.Interaction) -> bool:
        """檢查目前頻道是否允許使用音樂指令。

        規則：沒設定點歌頻道時全頻道開放；設定後只有該頻道可以用。
        不通過時直接回覆使用者，呼叫端回傳後應立即 return。
        """
        allowed_id = self._get_music_channel_id(interaction.guild_id)
        if allowed_id is None or interaction.channel_id == allowed_id:
            return True

        channel = interaction.guild.get_channel(allowed_id) if interaction.guild else None
        mention = channel.mention if channel else f"ID {allowed_id}（頻道可能已被刪除）"
        await interaction.response.send_message(
            f"🚫 這個伺服器的音樂指令只能在 {mention} 使用，請去那裡再試一次！",
            ephemeral=True,
        )
        return False

    @app_commands.command(name="music_set_channel", description="設定點歌頻道：音樂指令只在該頻道生效（不帶參數則取消限制）")
    @app_commands.describe(channel="點歌專用頻道；留空則取消限制，音樂指令恢復全頻道可用")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # 執行期檢查，伺服器端覆寫權限也擋得住
    async def music_set_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ):
        guild_id = str(interaction.guild.id)

        if channel is None:
            # 不帶參數 = 取消限制，音樂指令恢復所有頻道都能用
            if guild_id not in self.music_channel_settings:
                await interaction.response.send_message(
                    "ℹ️ 這個伺服器本來就沒有設定點歌頻道，音樂指令目前所有頻道都能使用。",
                    view=build_panel_link_view(interaction.guild.id),
                    ephemeral=True,
                )
                return
            del self.music_channel_settings[guild_id]
        else:
            self.music_channel_settings[guild_id] = {'music_channel_id': channel.id}

        try:
            _save_music_settings(self.music_channel_settings)
        except Exception as e:
            await self.bot.notify_owner_error(e, interaction, extra_info="music_set_channel: 儲存設定失敗")
            await interaction.response.send_message(
                "❌ 儲存設定時發生錯誤，設定可能在重新啟動後遺失，已回報開發者。", ephemeral=True
            )
            return

        if channel is None:
            embed = discord.Embed(
                title="🔓 已取消點歌頻道限制",
                description="音樂指令現在所有頻道都能使用了。",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="🔒 點歌頻道已設定",
                description=(
                    f"音樂指令現在只能在 {channel.mention} 使用。\n"
                    "在其他頻道輸入音樂指令會被擋下。\n\n"
                    "再執行一次 `/music_set_channel`（不選頻道）即可取消限制。"
                ),
                color=discord.Color.green(),
            )
        await interaction.response.send_message(
            embed=embed,
            view=build_panel_link_view(interaction.guild.id),
            ephemeral=True,
        )

    async def _run_pause_or_resume(self, interaction: discord.Interaction, player, want_pause: bool):
        """點歌者按了「允許」（或操作者本人有權）之後，實際執行暫停／續播。"""
        if want_pause:
            if not player.current or player.paused:
                await interaction.followup.send("ℹ️ 現在沒有可以暫停的歌曲。", ephemeral=True)
                return
            await player.pause(True)
            await self._announce(interaction, f"⏸️ 已暫停{self._operator_stamp(interaction)}")
        else:
            if not player.paused:
                await interaction.followup.send("ℹ️ 目前不是暫停狀態。", ephemeral=True)
                return
            await player.pause(False)
            await self._announce(interaction, f"▶️ 繼續播放{self._operator_stamp(interaction)}")

    async def _handle_pause_resume_request(self, interaction: discord.Interaction, player, want_pause: bool):
        """pause / resume 的攔截邏輯：非點歌者使用時，改為向點歌者請求允許。

        流程：在原文字頻道發送提示 + 允許/拒絕按鈕（只有點歌者能按、一次性、
        30 秒逾時視為拒絕）。同一首歌被拒絕後，在下一首開始播放前不得再提請求。
        """
        action_label = "暫停" if want_pause else "續播"
        requester_id, requester_name = self._get_current_requester(player)

        # 同一首歌已經有請求在等點歌者決定：先等它結束
        if requester_id in self._pending_requests:
            await interaction.response.send_message(
                "⌳ 這首歌已經有操作請求還在等待點歌者決定，請先等它結束。", ephemeral=True
            )
            return
        # 同一首歌之前被拒絕過：換下一首之前不得再提
        if self._is_track_rejected(player):
            await interaction.response.send_message(
                "🚫 這首歌的操作請求已被拒絕，在下一首開始播放前無法再次提出。", ephemeral=True
            )
            return

        current = player.current
        if want_pause and (not current or player.paused):
            await interaction.response.send_message("ℹ️ 現在沒有可以暫停的歌曲。", ephemeral=True)
            return
        if not want_pause and not player.paused:
            await interaction.response.send_message("ℹ️ 目前不是暫停狀態。", ephemeral=True)
            return

        view = RequesterApprovalView(self, requester_id, requester_name or "未知", track=current)
        self._register_control_request(requester_id, action_label, view)

        async def after_decision(allowed: bool):
            if allowed:
                await self._run_pause_or_resume(interaction, player, want_pause)

        view.after_decision = after_decision

        # 在原文字頻道公開發送請求（不 ephemeral，讓點歌者看得到按鈕）
        await interaction.response.send_message(
            f"🙋 {interaction.user.display_name} 想要{action_label}「{current.title}」，"
            f"等待點歌者 {requester_name or '未知'} 決定（{CONTROL_REQUEST_TIMEOUT} 秒內）：",
            view=view,
        )
        view.message = await interaction.original_response()

    @app_commands.command(name="music_pause", description="暫停目前播放的歌曲")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        if not await self._check_music_channel(interaction):
            return
        player = await self._get_control_player(interaction)
        if player is None:
            return
        if not player.current:
            await interaction.response.send_message("ℹ️ 目前沒有正在播放的歌曲。", ephemeral=True)
            return

        if await self._requester_can_directly_control(interaction, player):
            if player.paused:
                await interaction.response.send_message("ℹ️ 已經是暫停狀態了。", ephemeral=True)
                return
            await player.pause(True)
            await self._announce(interaction, f"⏸️ 已暫停{self._operator_stamp(interaction)}")
            return

        await self._handle_pause_resume_request(interaction, player, want_pause=True)

    @app_commands.command(name="music_resume", description="繼續播放")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        if not await self._check_music_channel(interaction):
            return
        player = await self._get_control_player(interaction)
        if player is None:
            return

        if await self._requester_can_directly_control(interaction, player):
            if not player.paused:
                await interaction.response.send_message("ℹ️ 目前不是暫停狀態。", ephemeral=True)
                return
            await player.pause(False)
            await self._announce(interaction, f"▶️ 繼續播放{self._operator_stamp(interaction)}")
            return

        await self._handle_pause_resume_request(interaction, player, want_pause=False)

    @app_commands.command(name="music_skip", description="跳過目前歌曲")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        if not await self._check_music_channel(interaction):
            return
        player = await self._get_control_player(interaction)
        if player is None:
            return
        if not await self._check_requester_control(interaction):
            return
        current = player.current
        if not current:
            await interaction.response.send_message("ℹ️ 目前沒有正在播放的歌曲。", ephemeral=True)
            return

        # 第 7 週：「已跳過」改顯示在面板底部，不再公開發送訊息把面板擠上去
        await self._announce(
            interaction, f"⏭️ 已跳過：**{current.title}**{self._operator_stamp(interaction)}"
        )
        try:
            # 暫停中直接 skip 的話，下一首可能會沿用暫停狀態，先解除
            if player.paused:
                await player.pause(False)
            # 先寫入意圖，on_wavelink_track_end 才知道這次是「被跳過」而不是「自然播完」
            player.end_intent = "skip"
            await player.skip(force=True)
        except Exception as e:
            if self._is_session_lost_error(e):
                # Lavalink Session 已失效時，遠端早已無曲目在播且不會發送 TrackEndEvent，直接觸發換歌流程推進佇列
                await self.on_wavelink_track_end(
                    SimpleNamespace(player=player, reason="stopped", track=current)
                )
                return
            player.end_intent = None
            print(f">>> /music_skip 失敗：{e}")
            await self.bot.notify_owner_error(e, interaction, extra_info="/music_skip 失敗")
            await interaction.followup.send("❌ 跳過歌曲時發生錯誤，已回報開發者。", ephemeral=True)

    async def _execute_stop(self, interaction: discord.Interaction, player, delete_thinking: bool = False):
        """實際執行停止：清佇列、關循環、停播。投票通過或（未來）直接授權時共用。

        第 7 週：停止結果直接寫進面板最終狀態；delete_thinking 為 True 代表這次互動是
        defer 出來的「思考中」訊息，要收掉避免多一則公開訊息。
        """
        song_queue: deque = getattr(player, "song_queue", None)
        # 先取消背景匯入與面板置底任務：已解析但還沒加入佇列的曲目直接丟棄，避免停止後又被塞回佇列或觸發置底
        self._cancel_import(interaction.guild.id)
        self._cancel_panel_bump(interaction.guild.id, force=True)
        # 順序很重要：先清佇列、關循環、清除播放曲目狀態，再更新面板與停止；否則 track_end 或置底任務會誤判仍在播放
        if song_queue:
            song_queue.clear()
        player.loop_mode = "off"
        current_track = getattr(player, "current", None) or getattr(player, "_active_track", None)
        self._clear_rejection(current_track)
        self._clear_player_playback_state(player)
        stop_text = f"⏹️ 已停止播放並清空佇列{self._operator_stamp(interaction)}"
        shown = await self._update_panel_ended(interaction.guild.id, reason=stop_text)
        if delete_thinking:
            try:
                await interaction.delete_original_response()
            except (discord.NotFound, discord.HTTPException):
                pass
        if not shown:
            # 沒有面板可顯示（例如只剩佇列、沒有曲目在播）才退回公開訊息
            await interaction.followup.send(stop_text)
        if current_track:
            try:
                if player.paused:
                    await player.pause(False)
                player.end_intent = "stop"
                await player.skip(force=True)
            except Exception as e:
                player.end_intent = None
                if self._is_session_lost_error(e):
                    # Session 已失效：Lavalink 端本來就沒在播，停止已完成，只需在背景重建 Session
                    await self._handle_session_lost(player, e)
                    return
                print(f">>> /music_stop 失敗：{e}")
                await self.bot.notify_owner_error(e, interaction, extra_info="/music_stop 失敗")
                await interaction.followup.send("❌ 停止播放時發生錯誤，已回報開發者。", ephemeral=True)

    @app_commands.command(name="music_stop", description="停止播放並清空佇列（需全員投票同意）")
    @app_commands.guild_only()
    async def stop_(self, interaction: discord.Interaction):
        if not await self._check_music_channel(interaction):
            return
        player = await self._get_control_player(interaction)
        if player is None:
            return
        song_queue: deque = getattr(player, "song_queue", None)
        if not player.current and not song_queue:
            await interaction.response.send_message("ℹ️ 目前沒有正在播放的歌曲，佇列也是空的。", ephemeral=True)
            return

        # 同一個伺服器同時只允許一場停止投票
        if interaction.guild.id in self._stop_votes:
            await interaction.response.send_message(
                "⌳ 已經有停止投票正在進行中，請先等它結束。", ephemeral=True
            )
            return

        # 這首歌的投票剛被否決（或逾時結算為否決）：下一首開始播放前不得再發起
        if self._is_track_rejected(player):
            await interaction.response.send_message(
                "🚫 這首歌的停止投票已被否決，在下一首開始播放前無法再次發起。", ephemeral=True
            )
            return

        voter_ids = self._voice_voter_ids(player)
        # 頻道裡只有操作者自己（或投票資格異常）時不用投票，直接執行
        if not voter_ids or voter_ids == {interaction.user.id}:
            await interaction.response.defer()
            await self._execute_stop(interaction, player, delete_thinking=True)
            return

        view = StopVoteView(
            self, interaction.guild.id, interaction.user.id, interaction.user.display_name, voter_ids,
            track=player.current,
        )
        self._register_stop_vote(interaction.guild.id, view)

        async def on_approved():
            await self._execute_stop(interaction, player)

        view.on_approved = on_approved

        await interaction.response.send_message(
            view=view,
            content=view._ballot_text(f"⏱️ {CONTROL_REQUEST_TIMEOUT} 秒內有效，全員同意才會停止；有人不同意即否決。"),
        )
        view.message = await interaction.original_response()

    @app_commands.command(name="music_loop", description="切換循環模式（不選則依 關閉→單曲→佇列 順序切換）")
    @app_commands.describe(mode="指定循環模式；不填則自動切換到下一個模式")
    @app_commands.choices(mode=[
        app_commands.Choice(name="關閉", value="off"),
        app_commands.Choice(name="單曲循環", value="single"),
        app_commands.Choice(name="佇列循環", value="all"),
    ])
    @app_commands.guild_only()
    async def loop_(self, interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None):
        if not await self._check_music_channel(interaction):
            return
        player = await self._get_control_player(interaction)
        if player is None:
            return
        current_mode = getattr(player, "loop_mode", "off")
        new_mode = mode.value if mode is not None else LOOP_NEXT[current_mode]
        player.loop_mode = new_mode
        await self._announce(interaction, f"{LOOP_LABELS[new_mode]}{self._operator_stamp(interaction)}")

    @app_commands.command(name="music_seek", description="跳轉到指定時間，例如 90、1:30、+10、-15")
    @app_commands.describe(time="絕對時間（秒 / mm:ss / hh:mm:ss），或以 +/- 開頭的相對秒數")
    @app_commands.guild_only()
    async def seek(self, interaction: discord.Interaction, time: str):
        if not await self._check_music_channel(interaction):
            return
        player = await self._get_control_player(interaction)
        if player is None:
            return
        if not await self._check_requester_control(interaction):
            return
        track = player.current
        if not track:
            await interaction.response.send_message("ℹ️ 目前沒有正在播放的歌曲。", ephemeral=True)
            return
        if not getattr(track, "is_seekable", True) or getattr(track, "is_stream", False):
            await interaction.response.send_message("❌ 這首歌不支援跳轉（可能是直播）。", ephemeral=True)
            return

        text = time.strip()
        if text[:1] in ("+", "-") and text[1:].isdigit():
            target = player.position + int(text) * 1000  # 相對跳轉
        else:
            target = parse_time_to_ms(text)
            if target is None:
                await interaction.response.send_message(
                    "❌ 時間格式錯誤，請用 `90`、`1:30`、`1:02:03`、`+10` 或 `-15`。", ephemeral=True
                )
                return

        # 夾在合法範圍內，結尾留 1 秒，避免直接跳到最後觸發 track_end
        target = max(0, min(target, max(track.length - 1000, 0)))
        await player.seek(target)
        await self._announce(
            interaction,
            f"⏩ 已跳轉至 `{format_position(target)} / {format_position(track.length)}`"
            f"{self._operator_stamp(interaction)}",
        )

    @app_commands.command(name="music_node_status", description="查看目前 Lavalink 節點的連線狀態")
    @app_commands.guild_only()
    async def node_status(self, interaction: discord.Interaction):
        if not await self._check_music_channel(interaction):
            return
        if wavelink is None:
            await interaction.response.send_message("❌ 語音模組尚未安裝完成，請聯絡管理員。", ephemeral=True)
            return

        try:
            nodes = wavelink.Pool.nodes
        except Exception as e:
            print(f">>> /music_node_status 讀取節點清單失敗：{e}")
            await self.bot.notify_owner_error(e, interaction, extra_info="/music_node_status 讀取節點清單失敗")
            await interaction.response.send_message("❌ 讀取節點狀態時發生錯誤，已回報開發者。", ephemeral=True)
            return

        if not nodes:
            await interaction.response.send_message(
                "⚠️ 目前沒有任何已註冊的 Lavalink 節點，請確認 `LAVALINK_URI` / `LAVALINK_PASSWORD` 是否已設定。",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🖥️ Lavalink 節點狀態",
            description=f"目前共有 {len(nodes)} 個節點",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )

        for identifier, node in nodes.items():
            status_name, status_label = self._describe_node_status(node)
            uri = getattr(node, "uri", "未知網址")
            player_count = len(getattr(node, "players", None) or {})

            value_lines = [
                f"**狀態：** {status_label}",
                f"**目前連線的伺服器數：** {player_count}",
            ]
            session_id = getattr(node, "session_id", None)
            if session_id:
                value_lines.append(f"**Session ID：** `{session_id}`")

            embed.add_field(
                name=f"📡 {identifier}",
                value=f"網址：{uri}\n" + "\n".join(value_lines),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ==========================================================
    # 用戶歌單管理指令（每個用戶上限 1 個歌單、最多 100 首歌）
    # ==========================================================
    @app_commands.command(name="music_playlist_create", description="建立個人音樂歌單並命名（每位用戶上限 1 個歌單，最多 100 首歌）")
    @app_commands.describe(
        name="歌單名稱（最多 50 字）",
        is_public="是否設為公開歌單（公開後同伺服器成員可在 /music_play 選單中直接看到）",
    )
    async def music_playlist_create(
        self,
        interaction: discord.Interaction,
        name: str,
        is_public: bool = False,
    ):
        clean_name = (name or "").strip()[:50]
        if not clean_name:
            await interaction.response.send_message("❌ 歌單名稱不能為空白！", ephemeral=True)
            return

        existing = get_user_playlist(interaction.user.id)
        if existing is not None:
            await interaction.response.send_message(
                f"⚠️ 每位用戶最多只能建立 **1 個歌單**！\n"
                f"你目前已有歌單「**{existing['name']}**」（分享碼：`{existing['share_code']}`，共 {len(existing.get('tracks') or [])} 首）。\n"
                f"• 如需修改名稱或內容，請使用 `/music_playlist_edit`\n"
                f"• 如需刪除重建，請先使用 `/music_playlist_remove`",
                ephemeral=True,
            )
            return

        pl = create_user_playlist(
            interaction.user.id,
            clean_name,
            owner_name=interaction.user.display_name,
            is_public=is_public,
        )
        vis_str = "🌐 公開（同伺服器成員可於選單直接點播）" if pl["is_public"] else "🔒 私人（僅自己或透過分享碼可點播）"
        embed = discord.Embed(
            title=f"✅ 已建立個人歌單：{pl['name']}",
            description=(
                f"**權限狀態：** {vis_str}\n"
                f"**專屬分享碼：** `{pl['share_code']}`（在 `/music_play` 的 `playlist` 輸入此 6 碼即可跨伺服器播放）\n"
                f"**目前曲目數：** 0 / {PLAYLIST_MAX_TRACKS} 首\n\n"
                f"💡 **接下來你可以：**\n"
                f"• 使用 `/music_playlist_add` 搜尋並加入單曲\n"
                f"• 使用 `/music_playlist_sync` 將目前播放佇列整批同步進歌單\n"
                f"• 使用 `/music_playlist_edit` 或前往網頁控制面板「個人設定」編輯歌單"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="music_playlist_add", description="搜尋歌曲並新增至你的個人歌單（上限 100 首）")
    @app_commands.describe(query="歌曲名稱 / 關鍵字，或是 YouTube、SoundCloud、Spotify 網址")
    async def music_playlist_add(self, interaction: discord.Interaction, query: str):
        pl = get_user_playlist(interaction.user.id)
        if pl is None:
            await interaction.response.send_message(
                "❌ 你還沒有建立個人歌單！請先使用 `/music_playlist_create` 建立歌單。",
                ephemeral=True,
            )
            return

        current_tracks = pl.get("tracks") or []
        if len(current_tracks) >= PLAYLIST_MAX_TRACKS:
            await interaction.response.send_message(
                f"⚠️ 你的歌單「**{pl['name']}**」已達上限（{PLAYLIST_MAX_TRACKS} / {PLAYLIST_MAX_TRACKS} 首），無法再新增歌曲！\n"
                f"請先使用 `/music_playlist_edit` 刪除部分歌曲。",
                ephemeral=True,
            )
            return

        if wavelink is None:
            await interaction.response.send_message("❌ 語音模組尚未安裝完成，請聯絡管理員。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # 支援 Spotify 連結（單曲／專輯／歌單）或一般關鍵字／YouTube／SoundCloud
        if looks_like_spotify(query):
            parsed = parse_spotify_url(query)
            if parsed is None:
                await self._ephemeral_error(interaction, "❌ 無法識別這個 Spotify 連結。")
                return
            kind, spotify_id = parsed
            if not self.spotify.configured:
                await self._ephemeral_error(interaction, "❌ 尚未設定 Spotify API 金鑰，無法解析 Spotify 連結。")
                return
            remaining_slots = PLAYLIST_MAX_TRACKS - len(current_tracks)
            try:
                sp_res = await self.spotify.fetch_tracks(kind, spotify_id, remaining_slots)
            except SpotifyError as e:
                await self._ephemeral_error(interaction, f"❌ {e.message}")
                return
            if not sp_res.tracks:
                await self._ephemeral_error(interaction, "😕 這個 Spotify 連結中沒有可匯入的曲目。")
                return
            sem = asyncio.Semaphore(IMPORT_CONCURRENCY)
            resolved_list = await asyncio.gather(*(self._search_spotify_track(sem, st) for st in sp_res.tracks[:remaining_slots]))
            new_dicts = [
                self._playable_to_playlist_dict(t)
                for t in resolved_list
                if t is not None
            ]
            if not new_dicts:
                await self._ephemeral_error(interaction, "😕 找不到對應的音源，無法加入歌單。")
                return
            updated_pl, added_count, dup_count, overflow_count = add_tracks_to_user_playlist(
                interaction.user.id, new_dicts, owner_name=interaction.user.display_name
            )
            total_now = len((updated_pl or {}).get("tracks") or [])
            if added_count == 0 and dup_count > 0:
                await interaction.followup.send(
                    f"⚠️ 所選的 **{dup_count}** 首 Spotify 歌曲皆已存在於歌單「**{pl['name']}**」中，已全部自動忽略！",
                    ephemeral=True,
                )
                return
            notes = []
            if dup_count > 0:
                notes.append(f"已自動忽略 **{dup_count}** 首重複歌曲")
            if overflow_count > 0:
                notes.append(f"超過 {PLAYLIST_MAX_TRACKS} 首上限略過 **{overflow_count}** 首")
            tail_msg = f"（{'，'.join(notes)}）" if notes else ""
            await interaction.followup.send(
                f"✅ 已從 Spotify 新增 **{added_count}** 首歌至歌單「**{pl['name']}**」{tail_msg}（目前共 **{total_now} / {PLAYLIST_MAX_TRACKS}** 首）！",
                ephemeral=True,
            )
            return

        track = await self._search_one_track(interaction, query)
        if track is None:
            return

        track_dict = self._playable_to_playlist_dict(track, fallback_query=query)
        updated_pl, added_count, dup_count, overflow_count = add_tracks_to_user_playlist(
            interaction.user.id, [track_dict], owner_name=interaction.user.display_name
        )
        title_str = track_dict.get("title", "未知曲目")
        uri_str = track_dict.get("uri")
        display_song = f"[{title_str}]({uri_str})" if uri_str else f"**{title_str}**"

        if dup_count > 0:
            await interaction.followup.send(
                f"⚠️ 你的歌單「**{pl['name']}**」中已存在相同歌曲 {display_song}，已自動忽略重複新增！",
                ephemeral=True,
            )
            return

        if added_count <= 0 or updated_pl is None:
            await self._ephemeral_error(
                interaction,
                f"⚠️ 歌單已達上限（{PLAYLIST_MAX_TRACKS} 首），無法再新增。",
            )
            return

        total_now = len(updated_pl.get("tracks") or [])
        await interaction.followup.send(
            f"✅ 已將 {display_song} 加入歌單「**{updated_pl['name']}**」（第 **{total_now} / {PLAYLIST_MAX_TRACKS}** 首）！",
            ephemeral=True,
        )

    @app_commands.command(name="music_playlist_edit", description="編輯個人歌單（刪除歌曲、重新排序、切換公開/私人、重新命名或重產分享碼）")
    @app_commands.describe(
        delete_index="要刪除的歌曲編號（1 ~ 100）",
        move_from="重新排序：要移動的歌曲原編號（1 ~ 100）",
        move_to="重新排序：要移往的目標位置編號（1 ~ 100）",
        is_public="設定歌單為公開（True）或私人（False）",
        name="修改歌單名稱（最多 50 字）",
        regenerate_share_code="是否重新產生 6 碼分享碼（設為 True 後舊分享碼立即失效）",
    )
    async def music_playlist_edit(
        self,
        interaction: discord.Interaction,
        delete_index: int | None = None,
        move_from: int | None = None,
        move_to: int | None = None,
        is_public: bool | None = None,
        name: str | None = None,
        regenerate_share_code: bool = False,
    ):
        pl = get_user_playlist(interaction.user.id)
        if pl is None:
            await interaction.response.send_message(
                "❌ 你還沒有建立個人歌單！請先使用 `/music_playlist_create` 建立歌單。",
                ephemeral=True,
            )
            return

        changes: list[str] = []

        # 1. 名稱與公開狀態更新
        new_name = name.strip()[:50] if (name and name.strip()) else None
        if new_name is not None or is_public is not None:
            pl = update_user_playlist(
                interaction.user.id,
                name=new_name,
                owner_name=interaction.user.display_name,
                is_public=is_public,
            ) or pl
            if new_name is not None:
                changes.append(f"✏️ 歌單名稱已更新為「**{pl['name']}**」")
            if is_public is not None:
                vis_label = "🌐 公開" if pl["is_public"] else "🔒 私人"
                changes.append(f"🔄 公開狀態已切換為 **{vis_label}**")

        # 2. 重新排序
        if move_from is not None or move_to is not None:
            if move_from is None or move_to is None:
                changes.append("⚠️ 重新排序需要同時填寫 `move_from` 與 `move_to` 兩個編號。")
            else:
                updated_pl, moved_track = reorder_track_in_user_playlist(
                    interaction.user.id, move_from - 1, move_to - 1
                )
                if moved_track is None or updated_pl is None:
                    changes.append(f"⚠️ 重新排序失敗：請確認編號 `{move_from}` 與 `{move_to}` 在有效範圍內。")
                else:
                    pl = updated_pl
                    changes.append(
                        f"🔀 已將「**{moved_track.get('title', '未知曲目')}**」從第 {move_from} 首移至第 {move_to} 首"
                    )

        # 3. 刪除指定編號歌曲
        if delete_index is not None:
            updated_pl, removed_track = remove_track_from_user_playlist(
                interaction.user.id, delete_index - 1
            )
            if removed_track is None or updated_pl is None:
                changes.append(f"⚠️ 刪除失敗：找不到編號為 `{delete_index}` 的歌曲。")
            else:
                pl = updated_pl
                changes.append(f"🗑️ 已刪除第 {delete_index} 首：「**{removed_track.get('title', '未知曲目')}**」")

        # 4. 重新產生分享碼
        if regenerate_share_code:
            updated_pl = regenerate_user_playlist_share_code(interaction.user.id)
            if updated_pl is not None:
                pl = updated_pl
                changes.append(f"🔑 分享碼已重新產生為 `{pl['share_code']}`（舊分享碼已失效）")

        view = PlaylistEditView(interaction.user.id, page=0)
        embed, _, _ = build_playlist_page_embed(pl, page=view.page, title_prefix="🛠️ 編輯歌單")
        content_msg = "\n".join(changes) if changes else "👇 請使用下方選單與按鈕編輯歌單（刪除歌曲、重新排序、切換公開/私人、重產分享碼）："
        await interaction.response.send_message(content=content_msg, embed=embed, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except (discord.NotFound, discord.HTTPException):
            pass

    @app_commands.command(name="music_playlist_remove", description="刪除你的個人歌單（會跳出確認訊息）")
    async def music_playlist_remove(self, interaction: discord.Interaction):
        pl = get_user_playlist(interaction.user.id)
        if pl is None:
            await interaction.response.send_message(
                "ℹ️ 你目前沒有建立任何個人歌單。",
                ephemeral=True,
            )
            return

        track_count = len(pl.get("tracks") or [])
        embed = discord.Embed(
            title="⚠️ 確定要刪除個人歌單嗎？",
            description=(
                f"即將刪除歌單：「**{pl['name']}**」\n"
                f"• **目前曲目數：** {track_count} 首\n"
                f"• **分享碼：** `{pl['share_code']}`\n\n"
                f"❗ **此操作無法復原**，且其他用戶透過分享碼收藏的連結也會一併失效。請確認是否繼續："
            ),
            color=discord.Color.red(),
        )
        view = PlaylistRemoveConfirmView(interaction.user.id, pl["name"])
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="music_playlist_sync", description="將目前伺服器的播放佇列直接新增至你的個人歌單內")
    async def music_playlist_sync(self, interaction: discord.Interaction):
        pl = get_user_playlist(interaction.user.id)
        if pl is None:
            await interaction.response.send_message(
                "❌ 你還沒有建立個人歌單！請先使用 `/music_playlist_create` 建立歌單後再同步佇列。",
                ephemeral=True,
            )
            return

        if wavelink is None or interaction.guild is None:
            await interaction.response.send_message("❌ 目前無法讀取伺服器播放佇列。", ephemeral=True)
            return

        player = interaction.guild.voice_client
        if not isinstance(player, wavelink.Player):
            await interaction.response.send_message("ℹ️ 機器人目前不在語音頻道中，沒有播放佇列可供同步。", ephemeral=True)
            return

        queue_tracks = []
        current_track = getattr(player, "current", None) or getattr(player, "_active_track", None)
        if current_track is not None:
            queue_tracks.append(current_track)
        song_queue = getattr(player, "song_queue", None)
        if song_queue:
            for item in list(song_queue):
                if isinstance(item, (tuple, list)) and len(item) >= 1 and item[0] is not None:
                    queue_tracks.append(item[0])

        if not queue_tracks:
            await interaction.response.send_message("ℹ️ 目前沒有正在播放的歌曲，待播佇列也是空的。", ephemeral=True)
            return

        current_saved = len(pl.get("tracks") or [])
        if current_saved >= PLAYLIST_MAX_TRACKS:
            await interaction.response.send_message(
                f"⚠️ 你的歌單「**{pl['name']}**」已達 {PLAYLIST_MAX_TRACKS} 首上限，無法再同步新增曲目！",
                ephemeral=True,
            )
            return

        track_dicts = [self._playable_to_playlist_dict(t) for t in queue_tracks]
        updated_pl, added_count, dup_count, skipped_count = add_tracks_to_user_playlist(
            interaction.user.id, track_dicts, owner_name=interaction.user.display_name
        )
        total_now = len((updated_pl or {}).get("tracks") or [])
        if added_count == 0 and dup_count > 0:
            await interaction.response.send_message(
                f"ℹ️ 目前播放佇列中的歌曲已全部存在於歌單「**{pl['name']}**」中，已自動忽略重複歌曲，未新增任何曲目。",
                ephemeral=True,
            )
            return
        notes = []
        if dup_count > 0:
            notes.append(f"已自動忽略 **{dup_count}** 首重複歌曲")
        if skipped_count > 0:
            notes.append(f"因達 {PLAYLIST_MAX_TRACKS} 首上限略過 **{skipped_count}** 首")
        skip_note = f"（{'，'.join(notes)}）" if notes else ""
        await interaction.response.send_message(
            f"✅ 已將目前播放佇列中的 **{added_count}** 首歌同步新增至歌單「**{pl['name']}**」{skip_note}！\n"
            f"📊 目前歌單共有 **{total_now} / {PLAYLIST_MAX_TRACKS}** 首歌曲。",
            ephemeral=True,
        )

    @app_commands.command(name="music_playlist", description="查看你的個人歌單（分頁顯示，一頁顯示 10 首）")
    async def music_playlist_view(self, interaction: discord.Interaction):
        pl = get_user_playlist(interaction.user.id)
        if pl is None:
            try:
                no_pl_view = build_panel_link_view(interaction.guild_id, personal=True)
            except TypeError:
                no_pl_view = build_panel_link_view(interaction.guild_id)
            await interaction.response.send_message(
                "ℹ️ 你目前還沒有建立個人歌單！\n"
                "• 輸入 `/music_playlist_create` 立即建立歌單\n"
                "• 或前往網頁控制面板右上角頭像選單 →「**個人設定**」管理歌單",
                view=no_pl_view,
                ephemeral=True,
            )
            return

        view = PlaylistPaginationView(
            interaction.user.id,
            playlist_owner_id=interaction.user.id,
            page=0,
            guild_id=interaction.guild_id,
        )
        embed, _, _ = build_playlist_page_embed(pl, page=0)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except (discord.NotFound, discord.HTTPException):
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
