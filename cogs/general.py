import random

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


class FunView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # 按鈕長期有效

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
            print(f">>> 發生錯誤: {e}")
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
        view = FunView()
        await interaction.response.send_message("請選擇你想要看的內容：", view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
