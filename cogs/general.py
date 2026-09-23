import random
import re
import sys
import traceback

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


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


class FunView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)  # 按鈕長期有效
        self.bot = bot

    @discord.ui.button(label="隨機名言", style=discord.ButtonStyle.primary)
    async def quote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
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


class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="測試機器人的連線延遲")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(f'延遲 `{round(self.bot.latency * 1000)}ms`')

    @app_commands.command(name="choice", description="選擇困難救星")
    @app_commands.describe(options="請輸入選項，用空格隔開")
    async def choice(self, interaction: discord.Interaction, options: str):
        opts = options.split()
        result = random.choice(opts)
        await interaction.response.send_message(f'# 選 **{result}** 就對了!!!')

    @app_commands.command(name="quotes", description="獲取隨機名言或笑話")
    async def quotes(self, interaction: discord.Interaction):
        view = FunView(self.bot)
        await interaction.response.send_message("請選擇你想要看的內容：", view=view)

    @app_commands.command(name="steal", description="從其他伺服器偷表情符號")
    @app_commands.describe(
        emoji="要偷的表情符號(可填入多個)，也可直接填入含表情符號的字串",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_emojis_and_stickers=True)
    async def steal(
        self,
        interaction: discord.Interaction,
        emoji: str,
    ):
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

        try:
            created = []
            for index, ref in enumerate(references):
                async with aiohttp.ClientSession() as session:
                    async with session.get(ref['cdn_url'], timeout=30) as resp:
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

                custom_name = sanitize_emoji_name(ref['emoji_name'])
                if custom_name in [emoji_obj.name for emoji_obj in created]:
                    custom_name = f"{custom_name}_{index + 1}"

                created_emoji = await interaction.guild.create_custom_emoji(
                    name=custom_name,
                    image=source_bytes,
                    reason=f"/steal 指令由 {interaction.user} 建立",
                )
                created.append(created_emoji)

            if len(created) == 1:
                preview = created[0].mention
                message = f"✅ 成功偷到{len(created)}個表情符號: {preview}"
            else:
                preview = ' '.join(emoji_obj.mention for emoji_obj in created)
                message = f"✅ 成功偷到{len(created)}個表情符號: {preview}"

            await interaction.followup.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=False,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ 機器人沒有權限建立自訂表情符號，請確認這個伺服器已授予「管理表情符號與貼圖」權限。",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ 建立表情符號失敗，請確認這個伺服器可建立自訂表情符號。", ephemeral=True)
        except Exception as exc:
            print(f"[steal] 發生未知錯誤: {exc}", file=sys.stderr)
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
            await self.bot.notify_owner_error(exc, interaction, extra_info="General.steal")
            await interaction.followup.send("❌ 發生未知錯誤，已回報給開發者。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
