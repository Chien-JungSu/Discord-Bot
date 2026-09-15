import os
import sys
import traceback

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

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

intents = discord.Intents.default()
intents.message_content = True
intents.members = True       # on_member_join（歡迎訊息）需要
intents.voice_states = True  # /join、/leave（語音狀態）需要，第1週新增

# 每週新增的功能模組，依序加入這個清單即可自動載入
INITIAL_EXTENSIONS = [
    'cogs.general',       # /ping /choice /quotes
    'cogs.weather',       # /weather
    'cogs.bus',           # /bus
    'cogs.server_info',   # /server_info
    'cogs.welcome',       # /welcome_active /welcome_inactive + on_member_join
    'cogs.music',         # /join /leave（第1週新增，之後每週持續擴充）
]


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='/', intents=intents)
        self._global_synced = False
        self.owner_id = OWNER_ID

    async def on_connect(self):
        print("ℹ️ on_connect event fired")

    async def notify_owner_error(self, error: Exception, interaction: discord.Interaction | None = None, extra_info: str = ""):
        """統一的錯誤回報函式，所有 Cog 都透過 self.bot.notify_owner_error(...) 呼叫。"""
        if not self.owner_id:
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
                f"**錯誤訊息**: {str(error)}\n"
            )
            if extra_info:
                content += f"**額外資訊**: {extra_info}\n"
            content += f"```py\n{trace}\n```"

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

        print("⏳ [後台提示] 正在向 Discord 官方伺服器發送全域指令同步請求...")
        try:
            local_cmds = list(self.tree.get_commands())
            print(f"ℹ️ 本地已註冊的 command 數量: {len(local_cmds)}")
            if local_cmds:
                print("ℹ️ 本地 command 名稱:", [c.name for c in local_cmds])

            synced = await self.tree.sync()
            print(f"成功同步了 {len(synced)} 個全域斜線指令！")
            self._global_synced = True
        except Exception as e:
            print(f"❌ 同步全域指令時發生錯誤: {e}")
            traceback.print_exc()

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        responded = interaction.response.is_done()

        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"系統冷卻中，請稍後再試！(還需 {error.retry_after:.1f} 秒)"
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

    try:
        if not getattr(bot, '_global_synced', False):
            print('🔁 on_ready fallback: 嘗試同步全域指令...')
            synced = await bot.tree.sync()
            print(f'🎉 fallback 成功同步了 {len(synced)} 個全域斜線指令！')
            bot._global_synced = True
    except Exception as e:
        print(f'❌ on_ready fallback 同步失敗: {e}')
        traceback.print_exc()


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
