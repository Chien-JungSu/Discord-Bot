import random
import sys
import traceback

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


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


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
