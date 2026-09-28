import os
import sys
import time
import traceback

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

from cogs.sanitize import redact_secrets
from cogs.web_server import app, start_web_server

# ================= 環境變數載入 =================
dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(dotenv_path=dotenv_path)

TOKEN = os.getenv('DISCORD_TOKEN')
CWA_API_KEY = os.getenv('CWA_API_KEY')  # 由 cogs/weather.py 讀取，這裡僅做啟動前檢查
OWNER_ID = os.getenv('DISCORD_OWNER_ID') or os.getenv('OWNER_ID')
try:
    OWNER_ID = int(OWNER_ID) if OWNER_ID else None
except ValueError:
    print('❌ 環境變數 DISCORD_OWNER_ID 必須是 Discord 使用者 ID 的整數格式。')
    OWNER_ID = None

OWNER_GUILD_ID = os.getenv('DISCORD_OWNER_GUILD_ID') or os.getenv('OWNER_GUILD_ID')

# ================= Owner DM 錯誤通知限流（H4） =================
# 修正：任何使用者只要連續觸發錯誤（例如狂打外部 API 或輸入異常內容），
# 就會透過 notify_owner_error 灌爆開發者的 DM，也可能撞到 Discord 的 DM
# rate limit。這裡加入兩層保護：
#   1. 去重：相同「錯誤類型 + 錯誤訊息」在 OWNER_ERROR_DEDUP_SECONDS 秒內只送一次。
#   2. 上限：任何 60 秒滾動視窗內最多送 OWNER_ERROR_MAX_PER_WINDOW 封，
#      超過就略過並在後台 log 提示（錯誤本身仍會完整印到 stderr，不會漏掉）。
OWNER_ERROR_DEDUP_SECONDS = 60.0
OWNER_ERROR_MAX_PER_WINDOW = 5
OWNER_ERROR_WINDOW_SECONDS = 60.0
_owner_error_last_sent: dict[str, float] = {}
_owner_error_send_times: list[float] = []


def _should_send_owner_error(error: Exception) -> bool:
    """判斷這個錯誤是否應該送出 DM 通知（去重 + 速率限制）。"""
    now = time.monotonic()
    key = f"{type(error).__name__}:{str(error)[:200]}"

    last = _owner_error_last_sent.get(key)
    if last is not None and now - last < OWNER_ERROR_DEDUP_SECONDS:
        return False

    global _owner_error_send_times
    _owner_error_send_times = [t for t in _owner_error_send_times if now - t < OWNER_ERROR_WINDOW_SECONDS]
    if len(_owner_error_send_times) >= OWNER_ERROR_MAX_PER_WINDOW:
        print(
            f"⚠️ 達到 owner 錯誤通知上限（{OWNER_ERROR_MAX_PER_WINDOW} 封 / {OWNER_ERROR_WINDOW_SECONDS:.0f} 秒），"
            "略過這次 DM 通知（錯誤仍會印在後台 log）。"
        )
        return False

    _owner_error_last_sent[key] = now
    _owner_error_send_times.append(now)
    return True


try:
    OWNER_GUILD_ID = int(OWNER_GUILD_ID) if OWNER_GUILD_ID else None
except ValueError:
    print('❌ 環境變數 DISCORD_OWNER_GUILD_ID 必須是 Discord 伺服器 ID 的整數格式。')
    OWNER_GUILD_ID = None

# ================= Owner DM 錯誤通知限流（H4） =================
# 修正：任何使用者只要連續觸發錯誤（例如狂打外部 API 或輸入異常內容），
# 就會透過 notify_owner_error 灌爆開發者的 DM，也可能撞到 Discord 的 DM
# rate limit。這裡加入兩層保護：
#   1. 去重：相同「錯誤類型 + 錯誤訊息」在 OWNER_ERROR_DEDUP_SECONDS 秒內只送一次。
#   2. 上限：任何 60 秒滾動視窗內最多送 OWNER_ERROR_MAX_PER_WINDOW 封，
#      超過就略過並在後台 log 提示（錯誤本身仍會完整印到 stderr，不會漏掉）。
OWNER_ERROR_DEDUP_SECONDS = 60.0
OWNER_ERROR_MAX_PER_WINDOW = 5
OWNER_ERROR_WINDOW_SECONDS = 60.0
_owner_error_last_sent: dict[str, float] = {}
_owner_error_send_times: list[float] = []

intents = discord.Intents.default()
intents.message_content = True
intents.members = True       # on_member_join（歡迎訊息）需要
intents.voice_states = True  # /music_join、/music_leave（語音狀態）需要，第1週新增

# 每週新增的功能模組，依序加入這個清單即可自動載入
INITIAL_EXTENSIONS = [
    'cogs.general',       # /ping /choice /quotes
    'cogs.weather',       # /weather
    'cogs.bus',           # /bus
    'cogs.server_info',   # /server_info
    'cogs.welcome',       # /welcome_active /welcome_inactive + on_member_join
    'cogs.music',         # /music_join /music_leave /music_play ...（第1週新增，之後每週持續擴充）
]


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='/', intents=intents)
        self._global_synced = False
        self.owner_id = OWNER_ID
        self.owner_guild_id = OWNER_GUILD_ID

    def _print_synced_commands(self, synced_commands, scope: str):
        if not synced_commands:
            print(f"ℹ️ [{scope}] 本次沒有任何指令同步成功。")
            return
        names = [cmd.name for cmd in synced_commands]
        print(f"ℹ️ [{scope}] 已同步指令：{names}")

    async def on_connect(self):
        print("ℹ️ on_connect event fired")

    async def notify_owner_error(self, error: Exception, interaction: discord.Interaction | None = None, extra_info: str = ""):
        """統一的錯誤回報函式，所有 Cog 都透過 self.bot.notify_owner_error(...) 呼叫。"""
        if not self.owner_id:
            return

        # H4 限流：去重 + 速率限制，避免被惡意使用者灌爆 DM。
        if not _should_send_owner_error(error):
            return

        try:
            owner = self.get_user(self.owner_id)
            if owner is None:
                owner = await self.fetch_user(self.owner_id)
            if owner is None:
                print(f"❌ 無法取得管理者 Discord 使用者: {self.owner_id}")
                return

            command_name = interaction.command.name if interaction and interaction.command else 'N/A'
            user_info = f"{interaction.user} ({interaction.user.id})" if interaction else 'N/A'
            guild_info = f"{interaction.guild} ({interaction.guild.id})" if interaction and interaction.guild else 'Direct Message / N/A'
            trace = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
            if len(trace) > 1500:
                trace = trace[-1500:]

            content = (
                f"⚠️ **機器人錯誤通知**\n"
                f"**使用者**: {user_info}\n"
                f"**伺服器**: {guild_info}\n"
                f"**指令**: {command_name}\n"
                f"**錯誤類型**: {type(error).__name__}\n"
                f"**錯誤訊息**: {redact_secrets(str(error))}\n"
            )
            if extra_info:
                content += f"**額外資訊**: {redact_secrets(extra_info)}\n"
            content += f"```py\n{redact_secrets(trace)}\n```"

            await owner.send(content)
        except discord.HTTPException as exc:
            print(f"❌ 無法將錯誤 DM 給管理者: {exc}")
        except Exception as exc:
            print(f"❌ notify_owner_error 發生例外: {exc}")

    async def setup_hook(self):
        # 綁定全局 app command 錯誤處理器
        self.tree.on_error = self.on_app_command_error

        print("🔧 setup_hook 已被呼叫，開始載入 Cogs...")
        for extension in INITIAL_EXTENSIONS:
            try:
                await self.load_extension(extension)
                print(f"✅ 已載入模組：{extension}")
            except Exception as e:
                print(f"❌ 載入模組 {extension} 失敗：{e}")
                traceback.print_exc()

        if self.owner_guild_id:
            print(f"ℹ️ [同步策略] owner_guild_id 已設定，將先同步 owner guild ({self.owner_guild_id})。")
            print(f"⏳ [後台提示] 正在同步 owner guild ({self.owner_guild_id}) 的指令...")
            try:
                guild = self.get_guild(self.owner_guild_id) or discord.Object(id=self.owner_guild_id)
                synced = await self.tree.sync(guild=guild)
                self._global_synced = True
                print(f"✅ 成功同步了 {len(synced)} 個 guild 指令到伺服器 {self.owner_guild_id}！")
                self._print_synced_commands(synced, 'guild')
            except Exception as e:
                print(f"⚠️ owner guild 同步失敗，將改為全域同步：{e}")
                traceback.print_exc()

        print("⏳ [後台提示] 正在向 Discord 官方伺服器發送全域指令同步請求...")
        print("ℹ️ [同步策略] guild sync 已完成，接著實際執行全域同步以便確認全域指令狀態。")
        try:
            synced = await self.tree.sync()
            self._global_synced = True
            print(f"✅ 成功同步了 {len(synced)} 個全域斜線指令！")
            self._print_synced_commands(synced, 'global')
        except Exception as e:
            print(f"❌ 同步全域指令時發生錯誤: {e}")
            traceback.print_exc()

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        responded = interaction.response.is_done()

        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"系統冷卻中，請稍後再試！(還需 {error.retry_after:.1f} 秒)"
        elif isinstance(error, app_commands.MissingPermissions):
            perms = '、'.join(error.missing_permissions) if error.missing_permissions else '所需權限'
            msg = f"❌ 你沒有使用此指令所需的權限（{perms}）。"
        elif isinstance(error, app_commands.CheckFailure):
            msg = "❌ 你沒有權限使用此指令。"
        else:
            msg = "發生了未知錯誤，已回報給開發者。"

            if interaction.command is not None:
                cmd_name = interaction.command.name
            else:
                cmd_name = "未知或未同步之全域指令"

            print(f"Ignoring exception in command {cmd_name}:", file=sys.stderr)
            traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
            await self.notify_owner_error(error, interaction)

        try:
            if not responded:
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_command_error(self, context: commands.Context, error: commands.CommandError):
        """實測發現的雜訊：本專案完全沒有定義任何傳統前綴指令，全部都是 slash
        commands（app_commands），錯誤處理應該走上面的 on_app_command_error。

        但 commands.Bot(command_prefix='/') 這個設定，讓 discord.py 仍然會監看
        頻道裡「以 / 開頭的一般文字訊息」（不是透過 Discord 指令選單觸發的真正
        Interaction），只要有人手滑打出這種訊息，就會嘗試解析成前綴指令、找不到
        就丟 CommandNotFound，灌爆後台 log，但其實不是真正的錯誤，直接忽略即可。
        其他種類的例外（理論上不太會發生，因為沒有任何前綴指令）還是照印出來，
        避免真的有 bug 時被靜默吃掉。
        """
        if isinstance(error, commands.CommandNotFound):
            return

        print(f"Ignoring exception in command {context.command}:", file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)


bot = MyBot()
app.config['BOT'] = bot


@bot.event
async def on_ready():
    print(f'目前登入身份：{bot.user}')
    print('ℹ️ on_ready event fired')
    print('✅ 機器人已經百分之百在雲端準備就緒！')

    if getattr(bot, '_global_synced', False):
        return

    # 保底：如果 setup_hook 沒有跑完，就只做一次最後同步，避免重複執行。
    if bot.owner_guild_id:
        print(f'ℹ️ [同步策略] owner_guild_id 已設定，on_ready 只會嘗試同步 owner guild ({bot.owner_guild_id})。')
        try:
            guild = bot.get_guild(bot.owner_guild_id) or discord.Object(id=bot.owner_guild_id)
            synced = await bot.tree.sync(guild=guild)
            bot._global_synced = True
            print(f'✅ on_ready 最後同步了 {len(synced)} 個 guild 指令！')
            bot._print_synced_commands(synced, 'guild')
            return
        except Exception as e:
            print(f'⚠️ owner guild on_ready 同步失敗，改為全域同步：{e}')
            traceback.print_exc()

    print('⏳ [後台提示] 正在向 Discord 官方伺服器發送全域指令同步請求...')
    print('ℹ️ [同步策略] owner_guild_id 未設定或 guild 同步失敗，on_ready 將執行全域同步。')
    synced = await bot.tree.sync()
    bot._global_synced = True
    print(f'✅ on_ready 最終同步了 {len(synced)} 個全域斜線指令！')
    bot._print_synced_commands(synced, 'global')


if __name__ == "__main__":
    start_web_server()
    print("🌐 外部 Flask 網頁伺服器已透過 web_server 模組在背景啟動...")

    if not TOKEN:
        print("❌ 環境變數 DISCORD_TOKEN 未設定或為空！請在環境變數中設定機器人 Token。")
        sys.exit(1)

    if not CWA_API_KEY:
        print("❌ 環境變數 CWA_API_KEY 未設定或為空！請在 .env 或系統環境變數中設定中央氣象署 API KEY。")
        sys.exit(1)

    print("🤖 正在啟動 Discord 機器人主程式...")
    bot.run(TOKEN)
