import os
import ssl

import aiohttp
import certifi
import discord
from discord import app_commands
from discord.ext import commands

CWA_API_KEY = os.getenv('CWA_API_KEY')

CITY_MAP = {
    "臺北": "臺北市", "新北": "新北市", "基隆": "基隆市", "桃園": "桃園市",
    "新竹": "新竹市", "苗栗": "苗栗縣", "臺中": "臺中市", "彰化": "彰化縣",
    "南投": "南投縣", "雲林": "雲林縣", "嘉義": "嘉義市", "臺南": "臺南市",
    "高雄": "高雄市", "屏東": "屏東縣", "宜蘭": "宜蘭縣", "花蓮": "花蓮縣",
    "臺東": "臺東縣", "澎湖": "澎湖縣", "金門": "金門縣", "連江": "連江縣",
    "馬祖": "連江縣",
    "Taipei": "臺北市", "New Taipei": "新北市", "Keelung": "基隆市",
    "Taoyuan": "桃園市", "Hsinchu": "新竹市", "Miaoli": "苗栗縣",
    "Taichung": "臺中市", "Changhua": "彰化縣", "Nantou": "南投縣",
    "Yunlin": "雲林縣", "Chiayi": "嘉義市", "Tainan": "臺南市",
    "Kaohsiung": "高雄市", "Pingtung": "屏東縣", "Yilan": "宜蘭縣",
    "Hualien": "花蓮縣", "Taitung": "臺東縣", "Penghu": "澎湖縣",
    "Kinmen": "金門縣", "Lienchiang": "連江縣", "Matsu": "連江縣",
}


class Weather(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="weather", description="查詢全台各縣市的即時天氣預報")
    @app_commands.describe(city="請輸入縣市名稱（中英皆可，英文請確保首字母大寫）")
    async def weather(self, interaction: discord.Interaction, city: str):
        deferred = False
        try:
            await interaction.response.defer(thinking=True)
            deferred = True
        except (discord.NotFound, discord.HTTPException) as e:
            print(f">>> interaction.defer() 失敗: {e}")

        async def send_result(content=None, embed=None, ephemeral=False):
            nonlocal deferred
            if deferred:
                try:
                    return await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
                except discord.HTTPException as exc:
                    print(f">>> followup.send failed: {exc}")
                    if getattr(exc, 'code', None) in (10062, 40060):
                        return None
                    raise
            try:
                return await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
            except discord.HTTPException as exc:
                print(f">>> send_message failed: {exc}")
                if getattr(exc, 'code', None) not in (40060, 10062):
                    raise
                deferred = True
                return await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)

        user_input = city.replace("台", "臺").strip()
        if user_input in CITY_MAP.values():
            formatted_city = user_input
        elif user_input in CITY_MAP.keys():
            formatted_city = CITY_MAP[user_input]
        else:
            formatted_city = user_input

        if not CWA_API_KEY:
            await send_result("❌ 伺服器端尚未設定氣象 API KEY，請聯絡管理員。", ephemeral=True)
            return

        url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={CWA_API_KEY}&locationName={formatted_city}"

        async def fetch_weather_data(use_insecure: bool = False):
            if use_insecure:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=10, ssl=False) as resp:
                        if resp.status != 200:
                            return resp.status, await resp.text()
                        return resp.status, await resp.json()

            ssl_context = ssl.create_default_context(cafile=certifi.where())
            ssl_context.check_hostname = True
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=ssl_context)
            ) as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        return resp.status, await resp.text()
                    return resp.status, await resp.json()

        try:
            try:
                status, data = await fetch_weather_data()
            except (ssl.SSLCertVerificationError, aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError) as ssl_err:
                print(f">>> SSL 驗證失敗，改用 ssl=False 重試: {ssl_err}")
                await self.bot.notify_owner_error(
                    ssl_err, interaction,
                    extra_info=f"weather command SSL verification failed for city={formatted_city}"
                )
                status, data = await fetch_weather_data(use_insecure=True)

            if status != 200:
                print(f">>> 氣象 API 非 200 回應: {status} / {data}")
                await send_result("⚠️ 氣象署伺服器連線異常，請稍後再試！")
                return

            if not isinstance(data, dict):
                print(f">>> 取得的資料不是 JSON 物件: {data}")
                await send_result("⚠️ 取得資料格式異常，請稍後再試！")
                return

            locations = data.get('records', {}).get('location', [])
            if not locations:
                await send_result(f"找不到「{city}」的資料，請確認輸入的是台灣的縣市名稱喔！")
                return

            weather_elements = locations[0]['weatherElement']
            elements = {}
            for el in weather_elements:
                name = el['elementName']
                value = el['time'][0]['parameter']['parameterName']
                elements[name] = value

            wx = elements.get('Wx', '未知')
            pop = elements.get('PoP', '0')
            min_t = elements.get('MinT', '?')
            max_t = elements.get('MaxT', '?')
            ci = elements.get('CI', '未知')

            embed = discord.Embed(
                title=f"🌦️ {formatted_city} 最新天氣預報",
                description=f"**天氣狀況：** {wx}",
                color=discord.Color.from_rgb(102, 204, 255),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="🌡️ 氣溫區間", value=f"{min_t}°C ~ {max_t}°C", inline=True)
            embed.add_field(name="🌧️ 降雨機率", value=f"{pop}%", inline=True)
            embed.add_field(name="💡 舒適度", value=ci, inline=False)
            embed.set_footer(text="資料來源：交通部中央氣象署")

            await send_result(embed=embed)

        except Exception as e:
            print(f">>> 氣象 API 發生錯誤: {e}")
            await self.bot.notify_owner_error(e, interaction, extra_info=f"weather command for city={formatted_city}")
            try:
                await send_result("❌ 獲取天氣資料時發生錯誤，已回報開發者!", ephemeral=True)
            except Exception as e_send:
                print(f">>> 無法送出錯誤回應: {e_send}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Weather(bot))
