from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from collections import deque
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

if TYPE_CHECKING:
    import wavelink as wavelink_module

try:
    import wavelink
except ImportError:  # 尚未安裝 wavelink 時，讓其他 Cog 仍可正常運作
    wavelink = None

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

# 點歌頻道限制的設定檔：key 是 guild_id 字串，value 是 {"music_channel_id": int}。
# 沒有設定的伺服器 = 不限制，任何頻道都可以使用音樂指令。
MUSIC_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'music_settings.json')


def _load_music_settings() -> dict:
    """讀取各伺服器的點歌頻道設定；檔案不存在或損壞時回傳空 dict（=全部不限制）。"""
    if os.path.exists(MUSIC_SETTINGS_FILE):
        try:
            with open(MUSIC_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"⚠️ music_settings.json 讀取失敗，所有伺服器將暫時不限頻道：{e}")
            return {}
    return {}


def _save_music_settings(data: dict):
    """原子寫入設定檔（先寫暫存檔再 os.replace），避免寫到一半斷電留下半份 JSON。
    模式與 welcome.py 的 _save_welcome_settings 一致。"""
    dir_path = os.path.dirname(MUSIC_SETTINGS_FILE)
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=dir_path, delete=False, suffix='.tmp') as tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, MUSIC_SETTINGS_FILE)
    except Exception as e:
        print(f"❌ 儲存點歌頻道設定失敗: {e}")
        raise


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


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # key: guild_id, value: 目前正在倒數「語音頻道空了要自動離開」的背景任務。
        # 一個伺服器同時最多只會有一個計時器在跑。
        self.empty_channel_timers: dict[int, asyncio.Task] = {}
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

    async def cog_unload(self):
        """Cog 被卸載時（例如重新載入模組）順便取消所有還在跑的自動離開計時器，
        避免計時器任務變成孤兒，之後莫名其妙把機器人踢出語音頻道。
        """
        for task in self.empty_channel_timers.values():
            if not task.done():
                task.cancel()
        self.empty_channel_timers.clear()
        # 重載後按鈕 View 的 timer 會跟著舊模組消失，狀態一併清掉，避免殘留鎖
        self._pending_requests.clear()
        self._stop_votes.clear()
        self._rejected_track_keys.clear()

        if self.node_health_check.is_running():
            self.node_health_check.cancel()

    async def cog_load(self):
        """Cog 被 bot.load_extension() 載入時自動呼叫一次，建立節點連線池。

        修正: 連線 Lavalink 改成背景任務執行，不要在這裡 await 完成連線。
        wavelink.Pool.connect() 在節點連不上時可能會卡住重試很久，如果直接
        await 會讓 setup_hook() 整個卡死，導致後面的 tree.sync() 跟 on_ready
        都永遠跑不到（機器人網頁的 bot-stats 也會因此一直是 0）。改成
        create_task 丟到背景後，cog_load() 會立刻返回，其餘啟動流程不受影響；
        另外加上逾時保護，避免背景任務本身無限期卡住。
        """
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

    @node_health_check.before_loop
    async def before_node_health_check(self):
        # 等 bot 完全 ready（含 on_ready 觸發過一次）再開始跑，
        # 避免啟動初期 wavelink.Pool 還沒建立好就先檢查、白跑一次。
        await self.bot.wait_until_ready()

    # ---------- wavelink 自訂事件 ----------
    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: Any):
        print(f"✅ Lavalink 節點已就緒：{payload.node.uri}（Session ID: {payload.session_id}）")

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

        try:
            await channel.send(f"⚠️ 播放「{track_title}」失敗，Lavalink 節點無法載入這首歌曲。{reason_hint}")
        except discord.HTTPException as e:
            print(f">>> 傳送 TrackException 通知訊息失敗: {e}")

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: Any):
        """第3週：串接播放佇列（播完自動連播）。第4週：加入循環模式與 skip / stop 意圖判斷。

        因為 autoplay 已關閉，「歌曲結束 → 下一首」完全由這裡決定，所以循環模式也
        必須在這裡實作（不能用 wavelink 內建的 queue.mode，你的佇列是自己的 song_queue）。

        判斷依據有兩個：
        1. payload.reason：replaced / cleanup 代表曲目被取代或 player 正在銷毀，
           不能再接下一首；loadFailed 的曲目不參與循環，否則壞掉的歌會無限重試。
        2. player.end_intent：/music_skip、/music_stop 在呼叫 player.skip() 之前會先
           寫入 "skip" / "stop"。Lavalink 對「被跳過」與「自然播完」都只會送 track_end，
           靠這個旗標才分得出來（單曲循環時被 skip 就不能重播同一首）。
        """
        player = getattr(payload, "player", None)
        if player is None:
            return

        reason = str(getattr(payload, "reason", "") or "").lower()
        if reason in ("replaced", "cleanup"):
            return

        song_queue: deque = getattr(player, "song_queue", None)
        if song_queue is None:
            song_queue = deque()
            player.song_queue = song_queue

        channel = getattr(player, "home_channel", None)

        # 讀出並立刻消耗旗標，避免影響下一首
        intent = getattr(player, "end_intent", None)
        player.end_intent = None
        loop_mode = getattr(player, "loop_mode", "off")
        requester_name = getattr(player, "current_requester", None) or "未知"
        requester_id = getattr(player, "current_requester_id", None)
        ended_track = getattr(payload, "track", None)
        if reason in ("loadfailed", "load_failed"):
            ended_track = None

        # 曲目結束（換歌／被跳過／停止）：解除這首歌的操作請求鎖與拒絕鎖
        self._cleanup_control_state(ended_track)

        if intent == "stop":
            # /music_stop 已清空佇列並回覆使用者，這裡不需要再發「播完了」通知
            player.current_requester = None
            player.current_requester_id = None
            return

        next_item = None
        is_repeat = False
        if ended_track is not None and loop_mode == "single" and intent != "skip":
            next_item = (ended_track, requester_name, requester_id)
            is_repeat = True
        else:
            if ended_track is not None and loop_mode == "all":
                # 佇列循環：剛結束（或被跳過）的歌排到佇列最後面
                song_queue.append((ended_track, requester_name, requester_id))
            if song_queue:
                next_item = song_queue.popleft()

        if next_item is None:
            player.current_requester = None
            player.current_requester_id = None
            if channel is not None:
                try:
                    await channel.send("📭 播放佇列已經全部播完囉，輸入 `/music_play` 繼續點歌吧！")
                except discord.HTTPException:
                    pass
            return

        next_track, next_requester, next_requester_id = self._split_queue_item(next_item)
        try:
            await self._play_track(player, next_track, next_requester, next_requester_id)
        except Exception as e:
            print(f">>> 自動播放下一首時發生錯誤：{e}")
            await self.bot.notify_owner_error(
                e, extra_info=f"on_wavelink_track_end 自動播放失敗 track={getattr(next_track, 'title', '?')}"
            )
            if channel is not None:
                try:
                    await channel.send(
                        f"⚠️ 自動播放「{getattr(next_track, 'title', '未知曲目')}」時發生錯誤，已回報開發者。"
                    )
                except discord.HTTPException:
                    pass
            return

        # 單曲循環重播時不發通知，避免同一首歌每次播完都洗版
        if channel is not None and not is_repeat:
            embed = self._build_track_embed(
                title="🎶 接下來播放",
                track=next_track,
                requester_name=next_requester,
                color=discord.Color.blurple(),
                footer_prefix="佇列自動播放 - 由",
            )
            # 佇列裡還有歌時，順帶提示「下一首」是哪首，讓聽的人有預期
            # （走到這裡時 next_track 已從佇列 popleft，所以 song_queue[0] 就是下一首）
            if song_queue:
                upcoming_track, upcoming_requester, _ = self._split_queue_item(song_queue[0])
                upcoming_value = (
                    f"[{upcoming_track.title}]({upcoming_track.uri})"
                    if getattr(upcoming_track, "uri", None)
                    else upcoming_track.title
                )
                embed.add_field(
                    name="📜 下一首",
                    value=f"{upcoming_value}\n由 {upcoming_requester} 點播",
                    inline=False,
                )
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as exc:
                print(f">>> 傳送佇列自動播放通知失敗: {exc}")

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
        if home_channel is not None:
            try:
                await home_channel.send(
                    f"📤 語音頻道已經空了超過 {EMPTY_VOICE_CHANNEL_TIMEOUT:.0f} 秒，"
                    "我先自動離開並清空佇列了，想聽歌再 `/music_play` 一次就可以！"
                )
            except discord.HTTPException:
                pass

    # ---------- 共用邏輯 ----------
    async def _ensure_player(
        self, interaction: discord.Interaction
    ) -> "wavelink_module.Player | None":
        """取得目前伺服器的 Player；若機器人還沒加入語音頻道，就直接幫使用者加入。

        回傳 None 代表已經送出錯誤訊息給使用者，呼叫端應該直接 return，不要繼續播放。
        這裡刻意沒有沿用 /join 指令本身，而是抽成共用函式，因為 /play 需要在
        「還沒 defer」跟「已經 defer」兩種情境下都能重複使用同一段邏輯。
        """
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.followup.send("❌ 你必須先加入一個語音頻道，我才能播放音樂！", ephemeral=True)
            return None

        user_channel = interaction.user.voice.channel
        player: "wavelink_module.Player | None" = interaction.guild.voice_client

        if player is None:
            try:
                player = await user_channel.connect(cls=wavelink.Player)
                # 第3週更新：改成 disabled，改由我們自己的 song_queue +
                # on_wavelink_track_end 監聽器接管「播完自動接下一首」的邏輯，
                # 避免跟 wavelink 內建的 autoplay 互相搶著呼叫 player.play()。
                player.autoplay = wavelink.AutoPlayMode.disabled
                player.song_queue = deque()
            except Exception as e:
                print(f">>> /play 加入語音頻道失敗：{e}")
                await self.bot.notify_owner_error(e, interaction, extra_info=f"/play 加入 {user_channel} 失敗")
                await interaction.followup.send("❌ 加入語音頻道時發生錯誤，已回報開發者。", ephemeral=True)
                return None
        elif player.channel.id != user_channel.id:
            await interaction.followup.send(
                f"❌ 你必須跟我在同一個語音頻道（{player.channel.mention}）才能點歌！", ephemeral=True
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
            await interaction.response.send_message(
                f"ℹ️ 我已經在 {interaction.guild.voice_client.channel.mention} 頻道中了。", ephemeral=True
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

        try:
            player = await channel.connect(cls=wavelink.Player)
        except Exception as e:
            print(f">>> 加入語音頻道失敗：{e}")
            await self.bot.notify_owner_error(e, interaction, extra_info=f"/join 加入 {channel} 失敗")
            await interaction.followup.send("❌ 加入語音頻道時發生錯誤，已回報開發者。", ephemeral=True)
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
        await voice_client.disconnect()
        await interaction.response.send_message(f"👋 已離開 {channel_mention}。", ephemeral=True)

    async def _search_one_track(
        self, interaction: discord.Interaction, query: str
    ) -> "wavelink_module.Playable | None":
        """共用的搜尋邏輯，/play 與 /play_next（插播）都會用到。

        回傳 None 代表搜尋失敗或找不到結果，錯誤訊息已經透過 followup 送出，
        呼叫端只要直接 return 即可，不用重複處理錯誤訊息。
        """
        try:
            tracks = await wavelink.Playable.search(query)
        except Exception as e:
            # wavelink 搜尋失敗常見原因：來源網站暫時掛掉、網址格式不支援，
            # 或是 YouTube 端觸發了反爬蟲機制（poToken / OAuth2 相關）。
            # 第8週會處理 poToken/OAuth2 設定，這裡先統一攔截、回報開發者，
            # 避免整個指令直接噴未捕捉例外。
            print(f">>> wavelink 搜尋失敗：{e}")
            await self.bot.notify_owner_error(e, interaction, extra_info=f"音樂搜尋失敗 query={query}")
            await interaction.followup.send(
                "❌ 搜尋音樂時發生錯誤，可能是來源網站暫時無法連線，或觸發了反爬蟲封鎖，已回報開發者。",
                ephemeral=True,
            )
            return None

        if not tracks:
            await interaction.followup.send(
                f"😕 找不到「{query}」的搜尋結果，可能是關鍵字沒有對應結果、影片為地區限定，或網址無效，換個關鍵字試試看。",
                ephemeral=True,
            )
            return None

        # wavelink.Playable.search 對於歌單網址會回傳 Playlist，單曲/關鍵字則回傳 list[Playable]。
        # 本週先只取第一首，完整歌單匯入排到第7週（Spotify 批次解析）再實作。
        if isinstance(tracks, wavelink.Playlist):
            return tracks.tracks[0]
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
        artwork = getattr(track, "artwork", None)
        if artwork:
            embed.set_thumbnail(url=artwork)
        embed.add_field(name="👤 作者", value=getattr(track, "author", None) or "未知", inline=True)
        embed.add_field(name="⏱️ 長度", value=format_duration(getattr(track, "length", None)), inline=True)
        if requester_name:
            embed.set_footer(text=f"{footer_prefix} {requester_name} {footer_suffix}")
        return embed

    @app_commands.command(name="music_play", description="搜尋並播放音樂，若目前正在播放則排入佇列（可輸入關鍵字或直接貼網址）")
    @app_commands.describe(query="歌曲名稱 / 關鍵字，或是 YouTube、SoundCloud 網址")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        if not await self._check_music_channel(interaction):
            return
        if wavelink is None:
            await interaction.response.send_message("❌ 語音模組尚未安裝完成，請聯絡管理員。", ephemeral=True)
            return

        # 搜尋 + 連線語音頻道都可能花超過 3 秒，先 defer 避免 Interaction 逾時（沿用 /weather 的慣例）。
        try:
            await interaction.response.defer(thinking=True)
        except (discord.NotFound, discord.HTTPException) as e:
            print(f">>> /play interaction.defer() 失敗: {e}")
            return

        player = await self._ensure_player(interaction)
        if player is None:
            return  # 錯誤訊息已經在 _ensure_player 內送出

        track = await self._search_one_track(interaction, query)
        if track is None:
            return  # 錯誤訊息已經在 _search_one_track 內送出

        # ---- 第3週更新：有佇列了，不再直接蓋掉正在播放的歌曲 ----
        # player.playing 代表現在有歌在播、player.paused 代表暫停中但還沒播完，
        # 這兩種狀態都視為「有東西在佔用播放器」，新點的歌一律排到佇列尾端（FIFO）。
        # 這裡把「點播者是誰」也一起存進佇列（track, requester_name），
        # 這樣輪到這首歌自動播放時，通知訊息才能顯示是誰點的。
        if player.playing or player.paused:
            player.song_queue.append((track, interaction.user.display_name, interaction.user.id))
            position = len(player.song_queue)
            embed = self._build_track_embed(
                title="➕ 已加入佇列",
                track=track,
                requester_name=interaction.user.display_name,
                color=discord.Color.blurple(),
            )
            embed.add_field(name="📌 排隊位置", value=f"第 {position} 位", inline=False)
            await interaction.followup.send(embed=embed)
            return

        # ---- 佇列與播放器都是空的，直接開始播放 ----
        try:
            await self._play_track(player, track, interaction.user.display_name, interaction.user.id)
        except Exception as e:
            print(f">>> 播放音樂時發生錯誤：{e}")
            await self.bot.notify_owner_error(
                e, interaction, extra_info=f"/play 播放失敗 track={getattr(track, 'title', '?')}"
            )
            await interaction.followup.send("❌ 播放音樂時發生錯誤，已回報開發者。", ephemeral=True)
            return

        embed = self._build_track_embed(
            title="🎶 開始播放", track=track, requester_name=interaction.user.display_name, color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=embed)

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

        track = await self._search_one_track(interaction, query)
        if track is None:
            return

        if player.playing or player.paused:
            # 插播的重點：用 appendleft 塞到 FIFO 佇列的最前面，
            # 而不是照排隊順序 append 到最後面；同樣把點播者名稱存起來。
            player.song_queue.appendleft((track, interaction.user.display_name, interaction.user.id))
            embed = self._build_track_embed(
                title="⏭️ 已插播",
                track=track,
                requester_name=interaction.user.display_name,
                color=discord.Color.orange(),
            )
            embed.add_field(name="📌 排隊位置", value="下一首", inline=False)
            await interaction.followup.send(embed=embed)
            return

        # 目前沒有東西在播，插播跟一般 /play 沒有差別，直接播放。
        try:
            await self._play_track(player, track, interaction.user.display_name, interaction.user.id)
        except Exception as e:
            print(f">>> 插播音樂時發生錯誤：{e}")
            await self.bot.notify_owner_error(
                e, interaction, extra_info=f"/play_next 播放失敗 track={getattr(track, 'title', '?')}"
            )
            await interaction.followup.send("❌ 播放音樂時發生錯誤，已回報開發者。", ephemeral=True)
            return

        embed = self._build_track_embed(
            title="🎶 開始播放", track=track, requester_name=interaction.user.display_name, color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=embed)

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
        lines.append(LOOP_LABELS.get(getattr(player, "loop_mode", "off"), LOOP_LABELS["off"]))

        embed = discord.Embed(
            title="🎵 播放佇列",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

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
            if getattr(state.get("view"), "track", None) is track:
                self._pending_requests.pop(rid, None)

    # ---------- 第4週新增：播放控制 ----------
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
        player.current_requester = requester_name
        player.current_requester_id = requester_id
        # 開始播放時清除這首歌之前的拒絕鎖（例如上次播放被拒、這次重新播出）
        self._rejected_track_keys.pop(self._track_key(track), None)
        await player.play(track)

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
                    "ℹ️ 這個伺服器本來就沒有設定點歌頻道，音樂指令目前所有頻道都能使用。", ephemeral=True
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
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _run_pause_or_resume(self, interaction: discord.Interaction, player, want_pause: bool):
        """點歌者按了「允許」（或操作者本人有權）之後，實際執行暫停／續播。"""
        if want_pause:
            if not player.current or player.paused:
                await interaction.followup.send("ℹ️ 現在沒有可以暫停的歌曲。", ephemeral=True)
                return
            await player.pause(True)
            await interaction.followup.send(f"⏸️ 已暫停{self._operator_stamp(interaction)}")
        else:
            if not player.paused:
                await interaction.followup.send("ℹ️ 目前不是暫停狀態。", ephemeral=True)
                return
            await player.pause(False)
            await interaction.followup.send(f"▶️ 繼續播放{self._operator_stamp(interaction)}")

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
            await interaction.response.send_message(f"⏸️ 已暫停{self._operator_stamp(interaction)}")
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
            await interaction.response.send_message(f"▶️ 繼續播放{self._operator_stamp(interaction)}")
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

        await interaction.response.send_message(
            f"⏭️ 已跳過：**{current.title}**{self._operator_stamp(interaction)}"
        )
        try:
            # 暫停中直接 skip 的話，下一首可能會沿用暫停狀態，先解除
            if player.paused:
                await player.pause(False)
            # 先寫入意圖，on_wavelink_track_end 才知道這次是「被跳過」而不是「自然播完」
            player.end_intent = "skip"
            await player.skip(force=True)
        except Exception as e:
            player.end_intent = None
            print(f">>> /music_skip 失敗：{e}")
            await self.bot.notify_owner_error(e, interaction, extra_info="/music_skip 失敗")
            await interaction.followup.send("❌ 跳過歌曲時發生錯誤，已回報開發者。", ephemeral=True)

    async def _execute_stop(self, interaction: discord.Interaction, player):
        """實際執行停止：清佇列、關循環、停播。投票通過或（未來）直接授權時共用。"""
        song_queue: deque = getattr(player, "song_queue", None)
        # 順序很重要：先清佇列、關循環，再停止；否則 track_end 會又撈出下一首或重播
        if song_queue:
            song_queue.clear()
        player.loop_mode = "off"
        self._clear_rejection(getattr(player, "current", None))
        await interaction.followup.send(f"⏹️ 已停止播放並清空佇列{self._operator_stamp(interaction)}")
        if player.current:
            try:
                if player.paused:
                    await player.pause(False)
                player.end_intent = "stop"
                await player.skip(force=True)
            except Exception as e:
                player.end_intent = None
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
            await self._execute_stop(interaction, player)
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
        await interaction.response.send_message(
            f"{LOOP_LABELS[new_mode]}{self._operator_stamp(interaction)}"
        )

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
        await interaction.response.send_message(
            f"⏩ 已跳轉至 `{format_position(target)} / {format_position(track.length)}`"
            f"{self._operator_stamp(interaction)}"
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
