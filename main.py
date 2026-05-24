from shlex import quote
import discord
from discord.ext import commands
from discord import app_commands 
import aiohttp
import random
from datetime import datetime
import sys
import traceback
import os
from dotenv import load_dotenv

# 載入 .env 檔案內的環境變數
load_dotenv()

# 讀取名為 DISCORD_TOKEN 的環境變數
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='/', intents=intents)

    # 當機器人啟動時，將指令同步到 Discord 伺服器
    async def setup_hook(self):
        # ⚠️ 請把這邊的數字換成你的 Discord 伺服器 ID
        MY_GUILD = discord.Object(id=1477930950288478262) 
        
        # 1. 先清空這個伺服器上所有卡住的舊指令 (消滅幽靈指令)
        self.tree.clear_commands(guild=MY_GUILD)
        
        # 2. 將程式碼中的指令複製過去
        self.tree.copy_global_to(guild=MY_GUILD)
        
        # 3. 執行同步
        await self.tree.sync(guild=MY_GUILD)

        print("✅ 已成功清理快取並重新同步指令！")

        self.tree.on_error = self.on_app_command_error

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # 為了獲得最原始的錯誤，我們可以使用 error.original (如果有被包裝的話)
        # 但如果是檢查失敗(如冷卻、權限)，通常錯誤本身就是我們要捕捉的對象
        
        # 判斷是否已經回應過該 interaction，避免引發 InteractionResponded 錯誤
        responded = interaction.response.is_done()

        # 1. 處理指令冷卻中 (CommandOnCooldown)
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"系統冷卻中，請稍後再試！(還需 {error.retry_after:.1f} 秒)"
            

        # 2. 處理其他未知錯誤
        else:
            msg = "發生了未知錯誤，已回報給開發者。"
            
            # 在終端機印出詳細的錯誤追蹤 (Traceback)，方便開發者除錯
            print(f"Ignoring exception in command {interaction.command.name}:", file=sys.stderr)
            traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

        # 根據 interaction 的狀態發送錯誤訊息 (設定 ephemeral=True 讓錯誤訊息只有觸發者看得到)
        try:
            if not responded:
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            # 如果連發送訊息都失敗（例如 Webhook 逾時），則忽略以避免無限迴圈
            pass
# 這裡實例化了 MyBot，整份檔案只要這一個 bot 就夠了！
bot = MyBot()

@bot.event
async def on_ready():
    print(f'目前登入身份：{bot.user}')

def generate_server_info_embed(guild: discord.Guild) -> discord.Embed:
    """
    將伺服器的資訊打包成一個 discord.Embed 物件
    """
    # 善用 Discord 內建的時間格式化（會自動根據觀看者的時區與語系顯示）
    # style='F' 顯示完整日期時間，style='R' 顯示相對時間（例如：4 年前）
    created_time_full = discord.utils.format_dt(guild.created_at, style='F')
    created_time_relative = discord.utils.format_dt(guild.created_at, style='R')
    
    # 建立 Embed 基底
    embed = discord.Embed(
        title=f"📊 {guild.name} 的伺服器資訊",
        color=discord.Color.teal(),
        timestamp=discord.utils.utcnow() # 在頁尾顯示這則訊息的產出時間
    )
    
    # 如果伺服器有設定頭像，就把它當作 Embed 的右上角縮圖
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
        
    # 填入伺服器資料
    embed.add_field(name="🏰 伺服器名稱", value=guild.name, inline=True)
    embed.add_field(name="👥 總成員人數", value=f"{guild.member_count} 人", inline=True)
    embed.add_field(
        name="📅 建立時間", 
        value=f"{created_time_full}\n({created_time_relative})", 
        inline=False
    )
    
    # 設定頁尾
    embed.set_footer(text=f"伺服器 ID: {guild.id}")
    
    return embed

# ================= 機器人指令區 =================

@bot.tree.command(name="ping", description="測試機器人的連線延遲")
@app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f'延遲 `{round(bot.latency * 1000)}ms`')

@bot.tree.command(name="choice", description="選擇困難救星")
@app_commands.describe(options="請輸入選項，用空格隔開")
async def choice(interaction: discord.Interaction, options: str):
    opts = options.split() # 將字串拆解成清單
    result = random.choice(opts)
    await interaction.response.send_message(f'# 選 **{result}** 就對了!!!')

@bot.tree.command(name="quotes", description="獲取隨機名言或笑話")
async def quotes(interaction: discord.Interaction):
    view = FunView() # 實例化我們的按鈕視圖
    await interaction.response.send_message("請選擇你想要看的內容：", view=view)


@bot.tree.command(name="weather", description="查詢全台各縣市的即時天氣預報")
@app_commands.describe(city="請輸入縣市名稱（中英皆可，英文請確保首字母大寫）")
async def weather(interaction: discord.Interaction, city: str):
    # 1. 立即回應 Discord，因為網路請求需要時間
    await interaction.response.defer()

    # --- 新增的字典防呆區塊 開始 ---
    
    # 先把使用者輸入的「台」統一換成「臺」，減少字典的複雜度
    user_input = city.replace("台", "臺")

    # 建立防呆對照字典 (Key: 使用者可能的縮寫, Value: 氣象署標準名稱)
    CITY_MAP = {
        "臺北": "臺北市",
        "新北": "新北市",
        "基隆": "基隆市",
        "桃園": "桃園市",
        "新竹": "新竹市", 
        "苗栗": "苗栗縣",
        "臺中": "臺中市",
        "彰化": "彰化縣",
        "南投": "南投縣",
        "雲林": "雲林縣",
        "嘉義": "嘉義市",
        "臺南": "臺南市",
        "高雄": "高雄市",
        "屏東": "屏東縣",
        "宜蘭": "宜蘭縣",
        "花蓮": "花蓮縣",
        "臺東": "臺東縣",
        "澎湖": "澎湖縣",
        "金門": "金門縣",
        "連江": "連江縣",
        "馬祖": "連江縣",
        "Taipei": "臺北市",
        "New Taipei": "新北市",
        "Keelung": "基隆市",
        "Taoyuan": "桃園市",
        "Hsinchu": "新竹市",
        "Miaoli": "苗栗縣",
        "Taichung": "臺中市",
        "Changhua": "彰化縣",
        "Nantou": "南投縣",
        "Yunlin": "雲林縣",
        "Chiayi": "嘉義市",
        "Tainan": "臺南市",
        "Kaohsiung": "高雄市",
        "Pingtung": "屏東縣",
        "Yilan": "宜蘭縣",
        "Hualien": "花蓮縣",
        "Taitung": "臺東縣",
        "Penghu": "澎湖縣",
        "Kinmen": "金門縣",
        "Lienchiang": "連江縣",
        "Matsu": "連江縣"
    }

    # 判斷邏輯：
    # 1. 先把使用者的輸入去掉頭尾的空白 (防呆)
    user_input = user_input.strip()

    # 2. 如果使用者已經輸入了標準名稱 (例如直接輸入了 "臺中市" 或 "花蓮縣")
    # 我們可以把 CITY_MAP 的所有 Value 拿出來比對，如果是標準名稱就直接用
    if user_input in CITY_MAP.values():
        formatted_city = user_input
        
    # 3. 如果使用者輸入的是縮寫 (例如 "臺中"、"馬祖")，我們就用字典把它轉成標準名稱
    elif user_input in CITY_MAP.keys():
        formatted_city = CITY_MAP[user_input]
        
    # 4. 如果都不是 (可能是亂打的字)，就先原封不動傳過去，稍後交給 API 報錯
    else:
        formatted_city = user_input

    # --- 新增的字典防呆區塊 結束 ---


    # ⚠️ 請把這裡換成你剛剛在第一步申請到的授權碼
    API_KEY = "CWA-A8DCADF8-822A-4F74-A32B-5906D9143BB2" 
    
    # 接下來的 API 網址，就使用轉換後的 formatted_city
    url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={API_KEY}&locationName={formatted_city}"

    try:
        # 3. 發送網路請求
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    await interaction.followup.send("⚠️ 氣象署伺服器連線異常，請稍後再試！")
                    return
                
                data = await resp.json()

        # 4. 檢查是否有抓到該城市的資料
        locations = data.get('records', {}).get('location', [])
        if not locations:
            await interaction.followup.send(f"找不到「{city}」的資料，請確認輸入的是台灣的縣市名稱喔！")
            return

        # 5. 拆解 JSON 資料
        # 我們鎖定抓取的第一個地區 (index 0)
        weather_elements = locations[0]['weatherElement']
        
        # 建立一個空字典來整理抓到的數據
        elements = {}
        for el in weather_elements:
            name = el['elementName']
            # 取未來 12 小時的資料 (第一個時間區塊 time[0])
            value = el['time'][0]['parameter']['parameterName']
            elements[name] = value

        # 將氣象署的代號轉換為我們要呈現的變數
        wx = elements.get('Wx', '未知') # 天氣現象 (如：多雲時晴)
        pop = elements.get('PoP', '0')  # 降雨機率 (%)
        min_t = elements.get('MinT', '?') # 最低溫
        max_t = elements.get('MaxT', '?') # 最高溫
        ci = elements.get('CI', '未知')  # 舒適度建議

        # 6. 建立與發送 Embed
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

        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f">>> 氣象 API 發生錯誤: {e}")
        await interaction.followup.send("❌ 獲取天氣資料時發生錯誤，請檢查程式碼。")

@bot.tree.command(name="server_info", description="顯示此伺服器的詳細資訊")
@app_commands.guild_only()  # 關鍵：防止使用者在私訊執行此指令導致 guild 為 None
async def server_info(interaction: discord.Interaction):
    # 1. 取得目標 guild 物件
    guild = interaction.guild 
    
    # 2. 呼叫獨立的副程式，取得包裝好的 Embed
    info_embed = generate_server_info_embed(guild)
    
    # 3. 回傳給使用者，主程式乾淨俐落！
    await interaction.response.send_message(embed=info_embed)

#錯誤測試指令，讓它故意崩潰看看我們的錯誤處理機制有沒有正常運作
#@bot.tree.command(name="crash", description="測試未知錯誤的指令")
#async def crash(interaction: discord.Interaction):
    #result = 1 / 0 
    #await interaction.response.send_message(f"結果是 {result}")
# ================= UI 介面區 =================

class FunView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) # timeout=None 代表按鈕長期有效

    @discord.ui.button(label="隨機名言", style=discord.ButtonStyle.primary)
    async def quote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. 立即回應 Discord，避免按鈕轉圈圈後顯示「交互失敗」
        await interaction.response.defer()
        print(">>> 已接收到按鈕請求，正在連線 API...")

        api_url = 'https://zenquotes.io/api/random'
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        quote_text = f"「{data[0]['q']}」\n—— *{data[0]['a']}*"
                        print(f">>> API 請求成功: {data[0]['a']}")
                    else:
                        quote_text = f"⚠️ 伺服器忙碌中 (Status: {resp.status})"
        except Exception as e:
            print(f">>> 發生錯誤: {e}")
            quote_text = "❌ 連線失敗，請檢查你的網路連線或稍後再試。"

        try:
            await interaction.followup.send(content=quote_text)
            print(">>> 訊息已成功送出！")
        except Exception as e:
            print(f">>> 送出訊息時失敗: {e}")

    @discord.ui.button(label="隨機笑話", style=discord.ButtonStyle.success)
    async def joke_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ⚠️ 修正：這裡也需要 defer()，否則後面的 followup 會報錯
        await interaction.response.defer()
        
        async with aiohttp.ClientSession() as session:
            async with session.get('https://official-joke-api.appspot.com/jokes/random') as resp:
                if resp.status == 200:
                    data = await resp.json()
                    joke = f"{data['setup']}\n{data['punchline']}"
                else:
                    joke = "暫時無法取得笑話，請稍後再試。"
        await interaction.followup.send(content=joke)


# 啟動機器人 (請確保這在整份檔案的最底下)
if __name__ == '__main__':
    bot.run(TOKEN)