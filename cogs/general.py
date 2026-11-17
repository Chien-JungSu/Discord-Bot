import asyncio
import datetime
import importlib
import inspect
import math
import os
import random
import re
import sys
import time
import traceback

from dotenv import load_dotenv

load_dotenv()

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.database import (
    build_panel_link_button,
    build_panel_link_view,
    get_bot_status_rotation_settings,
    get_panel_url,
)

OWNER_GUILD_ID = os.getenv('DISCORD_OWNER_GUILD_ID') or os.getenv('OWNER_GUILD_ID')
try:
    OWNER_GUILD_ID = int(OWNER_GUILD_ID) if OWNER_GUILD_ID else None
except ValueError:
    OWNER_GUILD_ID = None


def owner_guild_only(command_func):
    if OWNER_GUILD_ID is None:
        return command_func
    return app_commands.guilds(OWNER_GUILD_ID)(command_func)


def parse_emoji_reference(reference: str):
    """Parse a Discord emoji reference like <:name:id> or <a:name:id> and return CDN info."""
    if not reference:
        return None

    text = reference.strip()
    match = re.fullmatch(r'<a?:(\w+):(\d+)>', text)
    if not match:
        return None

    emoji_name, emoji_id = match.groups()
    is_animated = text.startswith('<a:')
    suffix = 'gif' if is_animated else 'png'
    return {
        'emoji_id': emoji_id,
        'emoji_name': emoji_name,
        'is_animated': is_animated,
        'cdn_url': f'https://cdn.discordapp.com/emojis/{emoji_id}.{suffix}',
    }


def parse_emoji_references(raw_text: str):
    """Support multiple emoji references and CDN URLs in one user input."""
    if not raw_text:
        return []

    text = raw_text.strip()
    results = []

    for match in re.finditer(r'<a?:(\w+):(\d+)>|https://cdn\.discordapp\.com/emojis/(\d+)(?:\.(png|gif))', text):
        if match.group(1) and match.group(2):
            emoji_name = match.group(1)
            emoji_id = match.group(2)
            is_animated = text[match.start():match.end()].startswith('<a:')
            suffix = 'gif' if is_animated else 'png'
        else:
            emoji_name = f'emote_{match.group(3)}'
            emoji_id = match.group(3)
            is_animated = match.group(4) == 'gif'
            suffix = 'gif' if is_animated else 'png'

        results.append({
            'emoji_id': emoji_id,
            'emoji_name': emoji_name,
            'is_animated': is_animated,
            'cdn_url': f'https://cdn.discordapp.com/emojis/{emoji_id}.{suffix}',
        })

    return results


def suggestion_name_from_reference(reference: str):
    parsed = parse_emoji_reference(reference)
    return parsed['emoji_name'] if parsed else None


def sanitize_emoji_name(name: str):
    cleaned = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip())
    cleaned = cleaned.strip('_') or 'stolen_emoji'
    return cleaned[:32]


def resolve_unique_emoji_name(
    raw_name: str,
    used_names: set[str],
    *,
    min_length: int = 2,
    start_index: int = 2,
) -> str:
    """清理表情符號名稱並確保長度合法且不與既有清單重複（2～32 字元）。"""
    base_name = sanitize_emoji_name(raw_name)
    if len(base_name) < min_length:
        base_name = f"{base_name}_emoji"[:32]

    candidate = base_name
    if candidate in used_names:
        counter = max(2, start_index)
        while True:
            suffix = f"_{counter}"
            trimmed = base_name[: max(1, 32 - len(suffix))]
            candidate = f"{trimmed}{suffix}"
            if candidate not in used_names:
                break
            counter += 1

    used_names.add(candidate)
    return candidate


async def sync_app_commands_after_reload(bot: commands.Bot) -> str:
    """重載 cog 後把最新的指令樹同步到 Discord，回傳給使用者看的同步結果摘要。

    同步策略與 main.py 的 setup_hook() 一致：
    - 有設定 owner_guild_id 時，先同步 owner guild（guild 範圍的同步是立即生效
      的，開發者重載完馬上就能用到新的指令參數），再做一次全域同步。
    - 全域同步在 Discord 端可能需要一段時間才會散播到所有伺服器，這是
      Discord API 的限制，只能靠 guild sync 先讓 owner 伺服器即時生效來緩解。
    - 同步失敗不應該讓「重載成功」變成失敗（模組本身已經載好了），所以每個
      步驟各自 try/except，把錯誤印到後台並回報在訊息裡。
    """
    results = []

    owner_guild_id = getattr(bot, 'owner_guild_id', None)
    if owner_guild_id:
        try:
            guild = bot.get_guild(owner_guild_id) or discord.Object(id=owner_guild_id)
            synced = await bot.tree.sync(guild=guild)
            print(f"✅ [reload 後同步] 已同步 {len(synced)} 個 guild 指令到伺服器 {owner_guild_id}")
            results.append(f"guild `{owner_guild_id}` 同步 {len(synced)} 個指令")
        except Exception as exc:
            print(f"❌ [reload 後同步] owner guild 同步失敗: {exc}", file=sys.stderr)
            results.append(f"guild `{owner_guild_id}` 同步失敗：{exc}")

    try:
        synced = await bot.tree.sync()
        print(f"✅ [reload 後同步] 已同步 {len(synced)} 個全域指令")
        results.append(f"全域同步 {len(synced)} 個指令")
    except Exception as exc:
        print(f"❌ [reload 後同步] 全域同步失敗: {exc}", file=sys.stderr)
        results.append(f"全域同步失敗：{exc}")

    return "；".join(results)


def owner_only(interaction: discord.Interaction):
    bot = interaction.client
    owner_id = getattr(bot, 'owner_id', None)
    return owner_id is not None and interaction.user.id == owner_id


def get_available_cog_modules(bot: commands.Bot | None = None):
    cogs_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'cogs')
    if not os.path.isdir(cogs_dir):
        return []

    loaded_modules = set(getattr(bot, 'extensions', {}).keys()) if bot is not None else set()
    modules = []
    for filename in sorted(os.listdir(cogs_dir)):
        if filename.endswith('.py') and not filename.startswith('__'):
            module_name = f"cogs.{os.path.splitext(filename)[0]}"
            if bot is not None and module_name not in loaded_modules:
                continue
            modules.append(module_name)
    return modules


class ReloadCogSelect(discord.ui.Select):
    def __init__(self, bot: commands.Bot):
        modules = get_available_cog_modules(bot)
        options = [
            discord.SelectOption(label=module.split('.')[-1], value=module, description=f"重載 {module}")
            for module in modules
        ]
        if not options:
            options = [
                discord.SelectOption(label='無可用模組', value='none', description='目前沒有已載入的 cog 可重載')
            ]
        super().__init__(
            placeholder='選擇要重載的模組...',
            min_values=1,
            max_values=1,
            options=options,
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        module_name = self.values[0]
        if module_name == 'none':
            await interaction.response.send_message('目前沒有可重載的模組。', ephemeral=True)
            return

        if module_name not in self.bot.extensions:
            await interaction.response.send_message(
                f'❌ 模組 `{module_name}` 尚未載入，無法重載。',
                ephemeral=True,
            )
            return

        # 修正：重載會改變指令樹（新增 / 修改 / 刪除 app command），但先前重載完
        # 沒有重新 sync，Discord 端的指令選單還是舊的，要重啟機器人才會更新。
        # sync 需要打 Discord API、可能超過 interaction 3 秒的回應時限，所以先
        # defer 之後再重載 + 同步，最後用 followup 一次回報結果。
        await interaction.response.defer(ephemeral=True)
        try:
            if 'cogs.database' in sys.modules:
                importlib.reload(sys.modules['cogs.database'])
            await self.bot.reload_extension(module_name)
            timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"✅ [{timestamp}] 已重載模組: {module_name}")
        except Exception as exc:
            timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"❌ [{timestamp}] 重載失敗: {module_name}", file=sys.stderr)
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
            await self.bot.notify_owner_error(exc, interaction, extra_info=f'General.reload_module target={module_name}')
            await interaction.followup.send(
                f'❌ 重載模組失敗：`{module_name}`\n```py\n{exc}\n```',
                ephemeral=True,
            )
            return

        sync_summary = await sync_app_commands_after_reload(self.bot)
        await interaction.followup.send(
            f'✅ 已重載模組 `{module_name}`。\n🔄 指令同步完成（{sync_summary}）。',
            view=build_panel_link_view(owner=True),
            ephemeral=True,
        )


class ReloadCogView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.add_item(ReloadCogSelect(bot))
        self.add_item(build_panel_link_button(owner=True, row=1))


class FunView(discord.ui.View):
    BUTTON_COOLDOWN_SECONDS = 10.0
    _button_cooldowns: dict[int, float] = {}  # user_id -> 上次點擊時間（跨 View 實例共用）

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)  # 按鈕長期有效
        self.bot = bot

    def _check_button_cooldown(self, interaction: discord.Interaction) -> tuple[bool, str]:
        """按鈕不是 app command，app_commands.checks.cooldown 對它無效，
        所以手動做 per-user 冷卻，避免任何人連點灌爆外部 API 與 owner DM。"""
        now = time.monotonic()
        last = self._button_cooldowns.get(interaction.user.id)
        if last is not None and now - last < self.BUTTON_COOLDOWN_SECONDS:
            remaining = self.BUTTON_COOLDOWN_SECONDS - (now - last)
            return False, f"⏳ 按鈕冷卻中，請稍後再試（還需 {remaining:.1f} 秒）。"
        self._button_cooldowns[interaction.user.id] = now
        return True, ""

    @discord.ui.button(label="隨機名言", style=discord.ButtonStyle.primary)
    async def quote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, cooldown_msg = self._check_button_cooldown(interaction)
        if not allowed:
            await interaction.response.send_message(cooldown_msg, ephemeral=True)
            return
        await interaction.response.defer()

        api_url = 'https://zenquotes.io/api/random'
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        quote_text = f"「{data[0]['q']}」\n—— *{data[0]['a']}*"
                    else:
                        quote_text = f"⚠️ 伺服器忙碌中 (Status: {resp.status})"
        except Exception as e:
            # 修正：這裡雖然有幫使用者準備友善的錯誤文字，但先前沒有通知開發者，
            # 導致 API 掛掉時只有使用者知道，開發者完全不會發現。
            print(f">>> 發生錯誤: {e}")
            await self.bot.notify_owner_error(e, interaction, extra_info="FunView.quote_button")
            quote_text = "❌ 連線失敗，請檢查你的網路連線或稍後再試。"

        try:
            await interaction.followup.send(content=quote_text)
        except Exception as e:
            print(f">>> 送出訊息時失敗: {e}")

    @discord.ui.button(label="隨機笑話", style=discord.ButtonStyle.success)
    async def joke_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, cooldown_msg = self._check_button_cooldown(interaction)
        if not allowed:
            await interaction.response.send_message(cooldown_msg, ephemeral=True)
            return
        await interaction.response.defer()

        async with aiohttp.ClientSession() as session:
            async with session.get('https://official-joke-api.appspot.com/jokes/random') as resp:
                if resp.status == 200:
                    data = await resp.json()
                    joke = f"{data['setup']}\n{data['punchline']}"
                else:
                    joke = "暫時無法取得笑話，請稍後再試。"
        await interaction.followup.send(content=joke)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        # 修正：先前 FunView 沒有覆寫 on_error，joke_button 等按鈕若拋出未捕捉的例外
        # （例如 aiohttp 逾時、連線失敗）會掉到 discord.ui.View 預設的 on_error，
        # 那個預設行為只會印到後台 stderr，開發者完全不會被通知。統一比照
        # bus.py 裡 View 的寫法補上，確保所有按鈕的未知錯誤都會 DM 給管理者。
        print(f">>> FunView 按鈕發生未知錯誤: {error}")
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        await self.bot.notify_owner_error(error, interaction, extra_info=f"FunView.on_error item={item}")
        if interaction.response.is_done():
            await interaction.followup.send("❌ 發生未知錯誤，已回報開發者。", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 發生未知錯誤，已回報開發者。", ephemeral=True)


def format_status_template(text: str, bot: commands.Bot | None = None) -> str:
    """將狀態訊息模板中的動態變數安全替換為即時統計數據（最多 128 字元）。"""
    raw = str(text or '').strip()
    if not raw:
        return ''

    guilds = getattr(bot, 'guilds', None) or [] if bot is not None else []
    guilds_count = len(guilds)
    users_count = sum(
        (getattr(g, 'member_count', None) or len(getattr(g, 'members', None) or []) or 0)
        for g in guilds
    )

    tree = getattr(bot, 'tree', None) if bot is not None else None
    if tree is not None and hasattr(tree, 'get_commands'):
        try:
            commands_count = len(tree.get_commands())
        except Exception:
            commands_count = 0
    else:
        commands_count = 0

    raw_latency = getattr(bot, 'latency', None) if bot is not None else None
    if isinstance(raw_latency, (int, float)) and math.isfinite(raw_latency) and raw_latency >= 0:
        latency_ms = round(raw_latency * 1000)
    else:
        latency_ms = 0

    rendered = (
        raw.replace('{guilds}', str(guilds_count))
        .replace('{users}', str(users_count))
        .replace('{commands}', str(commands_count))
        .replace('{latency}', str(latency_ms))
    )
    return rendered[:128]


def build_discord_activity(activity_type: str, text: str) -> discord.BaseActivity:
    """根據活動類型與文字建立對應的 Discord Activity 物件。"""
    kind = str(activity_type or 'playing').strip().lower()
    clean_text = str(text or '').strip()[:128]
    if kind == 'listening':
        return discord.Activity(type=discord.ActivityType.listening, name=clean_text)
    if kind == 'watching':
        return discord.Activity(type=discord.ActivityType.watching, name=clean_text)
    if kind == 'competing':
        return discord.Activity(type=discord.ActivityType.competing, name=clean_text)
    if kind == 'custom':
        return discord.CustomActivity(name=clean_text)
    return discord.Game(name=clean_text)


def resolve_discord_status(online_status: str) -> discord.Status:
    """將狀態字串對應為 discord.Status。"""
    mapping = {
        'online': discord.Status.online,
        'idle': discord.Status.idle,
        'dnd': discord.Status.dnd,
        'invisible': discord.Status.invisible,
    }
    return mapping.get(str(online_status or 'online').strip().lower(), discord.Status.online)


class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._status_index = 0
        self.status_settings = get_bot_status_rotation_settings()
        interval = max(10, int(self.status_settings.get('interval', 30)))
        self._status_rotation_loop.change_interval(seconds=interval)

    async def cog_load(self):
        if not self._status_rotation_loop.is_running():
            self._status_rotation_loop.start()

    def cog_unload(self):
        if self._status_rotation_loop.is_running():
            self._status_rotation_loop.cancel()

    async def apply_next_status(self) -> bool:
        """依序切換下一則機器人狀態訊息並更新 Discord Presence。"""
        if self.bot is None or self.bot.is_closed() or not self.bot.is_ready():
            return False

        settings = self.status_settings or get_bot_status_rotation_settings()
        discord_status = resolve_discord_status(settings.get('online_status', 'online'))
        items = settings.get('items') or []

        if not settings.get('enabled', True) or not items:
            await self.bot.change_presence(status=discord_status, activity=None)
            return True

        idx = self._status_index % len(items)
        current = items[idx]
        self._status_index = (idx + 1) % len(items)

        rendered_text = format_status_template(current.get('text', ''), self.bot)
        if not rendered_text:
            await self.bot.change_presence(status=discord_status, activity=None)
            return True

        activity = build_discord_activity(current.get('type', 'playing'), rendered_text)
        await self.bot.change_presence(status=discord_status, activity=activity)
        return True

    @tasks.loop(seconds=30)
    async def _status_rotation_loop(self):
        try:
            await self.apply_next_status()
        except Exception as exc:
            print(f"⚠️ [狀態輪替] 更新機器人狀態失敗: {exc}")

    @_status_rotation_loop.before_loop
    async def _before_status_rotation_loop(self):
        if self.bot is not None:
            await self.bot.wait_until_ready()

    def reload_settings(self):
        """從資料庫重新載入狀態訊息輪替設定，並立即套用最新狀態與輪替間隔。"""
        self.status_settings = get_bot_status_rotation_settings()
        self._status_index = 0
        interval = max(10, int(self.status_settings.get('interval', 30)))
        self._status_rotation_loop.change_interval(seconds=interval)

        if self.bot is not None:
            loop = getattr(self.bot, 'loop', None)
            if loop is not None and loop.is_running() and self.bot.is_ready():
                asyncio.run_coroutine_threadsafe(self.apply_next_status(), loop)

    @app_commands.command(name="ping", description="查看延遲")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(f'延遲 `{round(self.bot.latency * 1000)}ms`')

    @app_commands.command(name="panel", description="取得網頁控制面板連結")
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)
    async def panel(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id if interaction.guild else None
        embed = discord.Embed(
            title="🌐 炙彗JI器人 — 網頁控制面板",
            description=(
                "點擊下方按鈕即可前往網頁控制面板，透過瀏覽器輕鬆管理伺服器各項設定：\n"
                "• 🎉 **歡迎與自動身分組**\n"
                "• 🎵 **音樂播放頻道限制**\n"
                "• 💬 **關鍵字自動回覆**\n"
                "• 🎭 **表符身分組配置**"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=build_panel_link_view(guild_id),
            ephemeral=True,
        )

    @owner_guild_only
    @app_commands.command(name="reload_cogs", description="重載指定模組（僅擁有者可用）")
    @app_commands.describe(module_name="可選：直接輸入模組名稱，例如 cogs.general；不填則顯示選單")
    @app_commands.check(owner_only)
    async def reload_module(self, interaction: discord.Interaction, module_name: str | None = None):
        target = (module_name or '').strip()
        if target:
            normalized = target if target.startswith('cogs.') else f'cogs.{target}'
            if normalized not in self.bot.extensions:
                await interaction.response.send_message(
                    f'❌ 模組 `{normalized}` 尚未載入，無法重載。',
                    view=build_panel_link_view(owner=True),
                    ephemeral=True,
                )
                return
            # 修正：與下拉選單路徑相同，重載後自動同步指令樹到 Discord。
            # 先 defer 再做事，避免 sync 打 API 時超過 3 秒回應時限。
            await interaction.response.defer(ephemeral=True)
            try:
                if 'cogs.database' in sys.modules:
                    importlib.reload(sys.modules['cogs.database'])
                await self.bot.reload_extension(normalized)
                timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                print(f"✅ [{timestamp}] 已重載模組: {normalized}")
            except Exception as exc:
                timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                print(f"❌ [{timestamp}] 重載失敗: {normalized}", file=sys.stderr)
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
                await self.bot.notify_owner_error(exc, interaction, extra_info=f'General.reload_module target={normalized}')
                await interaction.followup.send(
                    f'❌ 重載模組失敗：`{normalized}`\n```py\n{exc}\n```',
                    view=build_panel_link_view(owner=True),
                    ephemeral=True,
                )
                return

            sync_summary = await sync_app_commands_after_reload(self.bot)
            await interaction.followup.send(
                f'✅ 已重載模組 `{normalized}`。\n🔄 指令同步完成（{sync_summary}）。',
                view=build_panel_link_view(owner=True),
                ephemeral=True,
            )
            return

        modules = get_available_cog_modules(self.bot)
        if not modules:
            await interaction.response.send_message(
                '目前沒有已載入的 cog 可重載。',
                view=build_panel_link_view(owner=True),
                ephemeral=True,
            )
            return

        await interaction.response.send_message('請選擇要重載的模組：', view=ReloadCogView(self.bot), ephemeral=True)

    @app_commands.command(name="choice", description="選擇困難救星")
    @app_commands.describe(options="請輸入選項，用空格隔開")
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)
    async def choice(self, interaction: discord.Interaction, options: str):
        opts = options.split()
        # 修正：空白輸入會讓 random.choice([]) 拋 IndexError 並觸發 owner 通知。
        if not opts:
            await interaction.response.send_message("❌ 請至少輸入一個選項，用空格隔開。", ephemeral=True)
            return
        result = random.choice(opts)
        await interaction.response.send_message(f'# 選 **{result}** 就對了!!!')

    @app_commands.command(name="quotes", description="獲取隨機名言或笑話")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def quotes(self, interaction: discord.Interaction):
        view = FunView(self.bot)
        await interaction.response.send_message("請選擇你想要看的內容：", view=view)

    async def _steal_emojis(
        self,
        interaction: discord.Interaction,
        emoji: str,
        *,
        to_bot: bool = False,
    ) -> None:
        """共用偷表情符號流程：支援新增至目前伺服器或機器人應用程式表情符號。"""
        if interaction.guild is None:
            await interaction.response.send_message("這個指令只能在伺服器中使用。", ephemeral=True)
            return

        references = parse_emoji_references(emoji)
        if not references:
            await interaction.response.send_message(
                "格式錯誤，請傳入類似 <:pepe_smile:123456789> 或 <a:pepe_smile:123456789> 的表情符號字串。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        log_tag = "bot_steal" if to_bot else "steal"
        extra_info = "General.bot_steal" if to_bot else "General.steal"

        try:
            used_names: set[str] = set()
            if to_bot:
                fetch_fn = getattr(self.bot, 'fetch_application_emojis', None)
                if callable(fetch_fn):
                    try:
                        existing_emojis = fetch_fn()
                        if inspect.isawaitable(existing_emojis):
                            existing_emojis = await existing_emojis
                        used_names = {
                            getattr(emoji_obj, 'name', '')
                            for emoji_obj in (existing_emojis or [])
                            if getattr(emoji_obj, 'name', None)
                        }
                    except Exception:
                        used_names = set()

            created = []
            async with aiohttp.ClientSession() as session:
                for index, ref in enumerate(references):
                    async with session.get(ref['cdn_url'], timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status != 200:
                            await interaction.followup.send(
                                f"❌ 無法下載第 {index + 1} 個 emoji，請確認它還能正常讀取。",
                                ephemeral=True,
                            )
                            return
                        source_bytes = await resp.read()

                    if len(source_bytes) > 262144:
                        await interaction.followup.send(
                            f"❌ 第 {index + 1} 個 emoji 檔案太大，Discord 自訂 emoji 上限為 256 KB，無法上傳。",
                            ephemeral=True,
                        )
                        return

                    custom_name = resolve_unique_emoji_name(
                        ref['emoji_name'],
                        used_names,
                        min_length=2,
                        start_index=index + 1,
                    )

                    try:
                        if to_bot:
                            created_emoji = await self.bot.create_application_emoji(
                                name=custom_name,
                                image=source_bytes,
                            )
                        else:
                            created_emoji = await interaction.guild.create_custom_emoji(
                                name=custom_name,
                                image=source_bytes,
                                reason=f"/steal 指令由 {interaction.user} 建立",
                            )
                    except ValueError as exc:
                        if 'Unsupported image type' in str(exc):
                            await interaction.followup.send(
                                "❌ 不支援的圖片格式，僅支援 PNG、JPG、GIF 或 WebP 圖片。",
                                ephemeral=True,
                            )
                            return
                        raise
                    created.append(created_emoji)

            formatted = []
            for emoji_obj in created:
                if getattr(emoji_obj, 'animated', False):
                    formatted.append(f'<a:{emoji_obj.name}:{emoji_obj.id}>')
                else:
                    formatted.append(f'<:{emoji_obj.name}:{emoji_obj.id}>')

            preview = ' '.join(formatted)
            if to_bot:
                message = f"✅ 成功偷到{len(created)}個機器人表情符號: {preview}"
            else:
                message = f"✅ 成功偷到{len(created)}個表情符號: {preview}"

            await interaction.followup.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=False,
            )
        except discord.MissingApplicationID:
            await interaction.followup.send(
                "❌ 無法取得機器人的 Application ID，暫時無法建立機器人表情符號。",
                ephemeral=True,
            )
        except discord.Forbidden:
            if to_bot:
                await interaction.followup.send(
                    "❌ 沒有權限建立機器人表情符號。",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "❌ 機器人沒有權限建立自訂表情符號，請確認這個伺服器已授予「管理表情符號與貼圖」權限。",
                    ephemeral=True,
                )
        except discord.HTTPException:
            if to_bot:
                await interaction.followup.send(
                    "❌ 建立機器人表情符號失敗，請確認表情符號名稱未重複、圖片格式有效，且尚未達到機器人表情符號上限。",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send("❌ 建立表情符號失敗，請確認這個伺服器可建立自訂表情符號。", ephemeral=True)
        except Exception as exc:
            print(f"[{log_tag}] 發生未知錯誤: {exc}", file=sys.stderr)
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
            await self.bot.notify_owner_error(exc, interaction, extra_info=extra_info)
            await interaction.followup.send("❌ 發生未知錯誤，已回報給開發者。", ephemeral=True)

    @app_commands.command(name="steal", description="從其他伺服器偷表情符號")
    @app_commands.describe(
        emoji="要偷的表情符號(可填入多個)，也可直接填入含表情符號的字串",
    )
    @app_commands.default_permissions(manage_emojis_and_stickers=True)
    @app_commands.checks.has_permissions(manage_emojis_and_stickers=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    @app_commands.guild_only()
    async def steal(
        self,
        interaction: discord.Interaction,
        emoji: str,
    ):
        await self._steal_emojis(interaction, emoji, to_bot=False)

    @owner_guild_only
    @app_commands.command(
        name="bot_steal",
        description="從其他伺服器偷表情符號並新增至機器人的表情符號（僅擁有者可用）",
    )
    @app_commands.describe(
        emoji="要偷的表情符號(可填入多個)，也可直接填入含表情符號的字串",
    )
    @app_commands.check(owner_only)
    async def steal_to_bot(
        self,
        interaction: discord.Interaction,
        emoji: str,
    ):
        await self._steal_emojis(interaction, emoji, to_bot=True)

    if OWNER_GUILD_ID is None:
        steal_to_bot = app_commands.guild_only()(steal_to_bot)


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
