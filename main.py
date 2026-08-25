import discord
from discord.ext import commands
from discord import app_commands 
import aiohttp
import certifi
import random
import ssl
from datetime import datetime
import sys
import traceback
import os
from urllib.parse import quote
from dotenv import load_dotenv
from keep_alive import keep_alive

# 載入環境變數
dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(dotenv_path=dotenv_path)
TOKEN = os.getenv('DISCORD_TOKEN')
CWA_API_KEY = os.getenv('CWA_API_KEY')
TDX_CLIENT_ID = os.getenv('TDX_CLIENT_ID')
TDX_CLIENT_SECRET = os.getenv('TDX_CLIENT_SECRET')
OWNER_ID = os.getenv('DISCORD_OWNER_ID') or os.getenv('OWNER_ID')
try:
    OWNER_ID = int(OWNER_ID) if OWNER_ID else None
except ValueError:
    print('❌ 環境變數 DISCORD_OWNER_ID 必須是 Discord 使用者 ID 的整數格式。')
    OWNER_ID = None

intents = discord.Intents.default()
intents.message_content = True

# ================= 機器人本體定義 =================
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='/', intents=intents)
        # 標誌是否已同步過全域指令（供 fallback 檢查用）
        self._global_synced = False
        # 管理者 / 開發者 Discord 使用者 ID
        self.owner_id = OWNER_ID

    async def on_connect(self):
        print("ℹ️ on_connect event fired")

    async def notify_owner_error(self, error: Exception, interaction: discord.Interaction | None = None, extra_info: str = ""):
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

    # 💡 關鍵點：必須在這一生只執行一次的 setup_hook 裡進行「全域指令同步」
    async def setup_hook(self):
        # 綁定你寫的全局錯誤處理器
        self.tree.on_error = self.on_app_command_error

        # 加入更多 debug 訊息，方便在 Render log 中追蹤是否有執行到這裡
        print("🔧 setup_hook 已被呼叫")
        print("⏳ [後台提示] 正在向 Discord 官方伺服器發送全域指令同步請求...")
        try:
            # （已註解）清除舊全域殘影可能會移除本地註冊的命令，先暫時註解避免誤刪
            # self.tree.clear_commands(guild=None)
            # 列出本地註冊於 tree 的命令，協助診斷為何 sync 會回傳 0
            local_cmds = list(self.tree.get_commands())
            print(f"ℹ️ 本地已註冊的 command 數量: {len(local_cmds)}")
            if local_cmds:
                print("ℹ️ 本地 command 名稱:", [c.name for c in local_cmds])

            # 執行全域同步（不要帶任何參數，直接同步全域指令樹）
            synced = await self.tree.sync()

            print(f"成功同步了 {len(synced)} 個全域斜線指令！")
            self._global_synced = True
        except Exception as e:
            print(f"❌ 同步全域指令時發生錯誤: {e}")
            traceback.print_exc()

    # 你原本寫得很漂亮的錯誤處理器（已融合 NoneType 防呆）
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        responded = interaction.response.is_done()

        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"系統冷卻中，請稍後再試！(還需 {error.retry_after:.1f} 秒)"
        else:
            msg = "發生了未知錯誤，已回報給開發者。"
            
            # 安全防呆：避免在 CommandNotFound 時讀取 .name 導致崩潰
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

# 2. 實例化機器人
bot = MyBot()

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

TDX_CITY_MAP = {
    "臺北": "Taipei",
    "台北": "Taipei",
    "臺北市": "Taipei",
    "台北市": "Taipei",
    "Taipei": "Taipei",
    "新北": "NewTaipei",
    "新北市": "NewTaipei",
    "New Taipei": "NewTaipei",
    "NewTaipei": "NewTaipei",
    "基隆": "Keelung",
    "基隆市": "Keelung",
    "Keelung": "Keelung",
    "桃園": "Taoyuan",
    "桃園市": "Taoyuan",
    "Taoyuan": "Taoyuan",
    "新竹": "Hsinchu",
    "新竹市": "Hsinchu",
    "Hsinchu": "Hsinchu",
    "新竹縣": "HsinchuCounty",
    "HsinchuCounty": "HsinchuCounty",
    "苗栗": "MiaoliCounty",
    "苗栗縣": "MiaoliCounty",
    "Miaoli": "MiaoliCounty",
    "MiaoliCounty": "MiaoliCounty",
    "臺中": "Taichung",
    "台中": "Taichung",
    "臺中市": "Taichung",
    "台中市": "Taichung",
    "Taichung": "Taichung",
    "彰化": "ChanghuaCounty",
    "彰化縣": "ChanghuaCounty",
    "Changhua": "ChanghuaCounty",
    "ChanghuaCounty": "ChanghuaCounty",
    "南投": "NantouCounty",
    "南投縣": "NantouCounty",
    "Nantou": "NantouCounty",
    "NantouCounty": "NantouCounty",
    "雲林": "YunlinCounty",
    "雲林縣": "YunlinCounty",
    "Yunlin": "YunlinCounty",
    "YunlinCounty": "YunlinCounty",
    "嘉義": "Chiayi",
    "嘉義市": "Chiayi",
    "Chiayi": "Chiayi",
    "嘉義縣": "ChiayiCounty",
    "ChiayiCounty": "ChiayiCounty",
    "臺南": "Tainan",
    "台南": "Tainan",
    "臺南市": "Tainan",
    "台南市": "Tainan",
    "Tainan": "Tainan",
    "高雄": "Kaohsiung",
    "高雄市": "Kaohsiung",
    "Kaohsiung": "Kaohsiung",
    "屏東": "PingtungCounty",
    "屏東縣": "PingtungCounty",
    "Pingtung": "PingtungCounty",
    "PingtungCounty": "PingtungCounty",
    "宜蘭": "YilanCounty",
    "宜蘭縣": "YilanCounty",
    "Yilan": "YilanCounty",
    "YilanCounty": "YilanCounty",
    "花蓮": "HualienCounty",
    "花蓮縣": "HualienCounty",
    "Hualien": "HualienCounty",
    "HualienCounty": "HualienCounty",
    "臺東": "TaitungCounty",
    "台東": "TaitungCounty",
    "臺東縣": "TaitungCounty",
    "台東縣": "TaitungCounty",
    "Taitung": "TaitungCounty",
    "TaitungCounty": "TaitungCounty",
    "澎湖": "PenghuCounty",
    "澎湖縣": "PenghuCounty",
    "Penghu": "PenghuCounty",
    "PenghuCounty": "PenghuCounty",
    "金門": "KinmenCounty",
    "金門縣": "KinmenCounty",
    "Kinmen": "KinmenCounty",
    "KinmenCounty": "KinmenCounty",
    "連江": "LienchiangCounty",
    "連江縣": "LienchiangCounty",
    "馬祖": "LienchiangCounty",
    "Lienchiang": "LienchiangCounty",
    "LienchiangCounty": "LienchiangCounty",
    "Matsu": "LienchiangCounty",
}

TDX_CITY_OPTIONS = [
    ("臺北市", "Taipei"),
    ("新北市", "NewTaipei"),
    ("基隆市", "Keelung"),
    ("桃園市", "Taoyuan"),
    ("新竹市", "Hsinchu"),
    ("新竹縣", "HsinchuCounty"),
    ("苗栗縣", "MiaoliCounty"),
    ("臺中市", "Taichung"),
    ("彰化縣", "ChanghuaCounty"),
    ("南投縣", "NantouCounty"),
    ("雲林縣", "YunlinCounty"),
    ("嘉義市", "Chiayi"),
    ("嘉義縣", "ChiayiCounty"),
    ("臺南市", "Tainan"),
    ("高雄市", "Kaohsiung"),
    ("屏東縣", "PingtungCounty"),
    ("宜蘭縣", "YilanCounty"),
    ("花蓮縣", "HualienCounty"),
    ("臺東縣", "TaitungCounty"),
    ("澎湖縣", "PenghuCounty"),
    ("金門縣", "KinmenCounty"),
    ("連江縣", "LienchiangCounty"),
]

BUS_STOP_STATUS = {
    0: "即將進站",
    1: "尚未發車",
    2: "交管不停靠",
    3: "末班車已過",
    4: "今日未營運",
}

BUS_DIRECTION = {
    0: "去程",
    1: "返程",
}

def resolve_tdx_city(city: str) -> str | None:
    normalized = city.strip()
    return TDX_CITY_MAP.get(normalized) or TDX_CITY_MAP.get(normalized.replace("台", "臺"))

def format_bus_arrival(item: dict) -> str:
    stop_name = item.get("StopName", {}).get("Zh_tw", "未知站牌")
    direction = BUS_DIRECTION.get(item.get("Direction"), "方向未知")
    plate = item.get("PlateNumb") or "未發車"
    stop_status = item.get("StopStatus")
    estimate_time = item.get("EstimateTime")

    if stop_status == 0 and estimate_time is not None:
        minutes = max(estimate_time // 60, 0)
        arrival = "進站中" if minutes == 0 else f"約 {minutes} 分鐘"
    else:
        arrival = BUS_STOP_STATUS.get(stop_status, "狀態未知")

    return f"**{stop_name}**（{direction}）\n{arrival}｜車牌：{plate}"

async def fetch_tdx_token(use_insecure: bool = False) -> str:
    token_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": TDX_CLIENT_ID,
        "client_secret": TDX_CLIENT_SECRET,
    }
    ssl_arg = False if use_insecure else ssl.create_default_context(cafile=certifi.where())

    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, data=payload, timeout=10, ssl=ssl_arg) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"TDX token API 回應 {resp.status}: {data}")
            return data["access_token"]

async def fetch_bus_estimates(city_code: str, route: str, use_insecure: bool = False):
    token = await fetch_tdx_token(use_insecure=use_insecure)
    encoded_route = quote(route.strip(), safe="")
    url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/City/{city_code}/{encoded_route}"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"$format": "JSON"}
    ssl_arg = False if use_insecure else ssl.create_default_context(cafile=certifi.where())

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params, timeout=10, ssl=ssl_arg) as resp:
            if resp.status != 200:
                return resp.status, await resp.text()
            return resp.status, await resp.json()

async def fetch_bus_stops(city_code: str, route: str, use_insecure: bool = False):
    token = await fetch_tdx_token(use_insecure=use_insecure)
    encoded_route = quote(route.strip(), safe="")
    url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/StopOfRoute/City/{city_code}/{encoded_route}"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"$format": "JSON"}
    ssl_arg = False if use_insecure else ssl.create_default_context(cafile=certifi.where())

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params, timeout=10, ssl=ssl_arg) as resp:
            if resp.status != 200:
                return resp.status, await resp.text()
            return resp.status, await resp.json()

def parse_bus_stops(data: list) -> list[dict]:
    stops = []
    seen = set()
    for route_info in data:
        direction = route_info.get("Direction")
        direction_name = BUS_DIRECTION.get(direction, "方向未知")
        for stop_info in route_info.get("Stops", []):
            stop_name = stop_info.get("StopName", {}).get("Zh_tw", "未知站牌")
            sequence = stop_info.get("StopSequence", 0)
            stop_uid = stop_info.get("StopUID") or f"{direction}-{sequence}-{stop_name}"
            key = (direction, sequence, stop_uid)
            if key in seen:
                continue
            seen.add(key)
            stops.append({
                "name": stop_name,
                "direction": direction,
                "direction_name": direction_name,
                "sequence": sequence,
                "uid": stop_uid,
            })

    stops.sort(key=lambda item: (
        item["direction"] if item["direction"] is not None else 9,
        item["sequence"],
        item["name"],
    ))
    return stops

async def send_bus_stop_picker(interaction: discord.Interaction, city_name: str, city_code: str, route: str):
    route = route.strip()
    if not route:
        await interaction.followup.send("請輸入公車路線名稱。", ephemeral=True)
        return

    if not TDX_CLIENT_ID or not TDX_CLIENT_SECRET:
        await interaction.followup.send(
            "❌ 伺服器端尚未設定 TDX_CLIENT_ID / TDX_CLIENT_SECRET，請聯絡管理員。",
            ephemeral=True
        )
        return

    try:
        try:
            status, data = await fetch_bus_stops(city_code, route)
        except (ssl.SSLCertVerificationError, aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError) as ssl_err:
            print(f">>> TDX 站牌 API SSL 驗證失敗，改用 ssl=False 重試: {ssl_err}")
            await bot.notify_owner_error(
                ssl_err,
                interaction,
                extra_info=f"bus stops SSL verification failed for city={city_code}, route={route}"
            )
            status, data = await fetch_bus_stops(city_code, route, use_insecure=True)

        if status != 200:
            print(f">>> TDX 站牌 API 非 200 回應: {status} / {data}")
            if status in (400, 404):
                await interaction.followup.send(
                    f"找不到「{city_name} {route}」的站牌資料，請確認公車號碼是否正確。",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "⚠️ 公車站牌資料服務暫時無法連線，請稍後再試！",
                    ephemeral=True
                )
            return

        if not isinstance(data, list) or not data:
            await interaction.followup.send(
                f"找不到「{city_name} {route}」的站牌資料，請確認公車號碼是否正確。",
                ephemeral=True
            )
            return

        stops = parse_bus_stops(data)
        if not stops:
            await interaction.followup.send(
                f"「{city_name} {route}」目前沒有可選擇的站牌資料。",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            f"請選擇「{city_name} {route}」要查詢的站牌：",
            view=BusStopView(city_name, city_code, route, stops),
            ephemeral=True
        )

    except Exception as e:
        print(f">>> TDX 站牌 API 發生錯誤: {e}")
        await bot.notify_owner_error(e, interaction, extra_info=f"bus stops for city={city_code}, route={route}")
        await interaction.followup.send("❌ 獲取站牌資料時發生錯誤，已回報開發者!", ephemeral=True)

async def send_bus_result(
    interaction: discord.Interaction,
    city: str,
    route: str,
    stop: str | None = None,
    city_code: str | None = None,
    stop_direction: int | None = None,
    stop_uid: str | None = None
):
    city_code = city_code or resolve_tdx_city(city)
    if not city_code:
        await interaction.followup.send(
            f"找不到「{city}」對應的縣市，請輸入例如：臺北、新北、桃園、臺中、臺南、高雄。",
            ephemeral=True
        )
        return

    if not TDX_CLIENT_ID or not TDX_CLIENT_SECRET:
        await interaction.followup.send(
            "❌ 伺服器端尚未設定 TDX_CLIENT_ID / TDX_CLIENT_SECRET，請聯絡管理員。",
            ephemeral=True
        )
        return

    try:
        try:
            status, data = await fetch_bus_estimates(city_code, route)
        except (ssl.SSLCertVerificationError, aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError) as ssl_err:
            print(f">>> TDX SSL 驗證失敗，改用 ssl=False 重試: {ssl_err}")
            await bot.notify_owner_error(
                ssl_err,
                interaction,
                extra_info=f"bus command SSL verification failed for city={city_code}, route={route}"
            )
            status, data = await fetch_bus_estimates(city_code, route, use_insecure=True)

        if status != 200:
            print(f">>> TDX 公車 API 非 200 回應: {status} / {data}")
            if status in (400, 404):
                await interaction.followup.send(
                    f"找不到「{city} {route}」的公車到站資料，請確認公車號碼是否正確。",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "⚠️ 公車資料服務暫時無法連線，請稍後再試！",
                    ephemeral=True
                )
            return

        if not isinstance(data, list) or not data:
            await interaction.followup.send(
                f"找不到「{city} {route}」的公車到站資料，請確認公車號碼是否正確。",
                ephemeral=True
            )
            return

        results = data
        if stop:
            stop_keyword = stop.strip()
            results = [
                item for item in data
                if stop_keyword in item.get("StopName", {}).get("Zh_tw", "")
            ]
        if stop_direction is not None:
            results = [
                item for item in results
                if item.get("Direction") == stop_direction
            ]
        if stop_uid:
            uid_results = [
                item for item in results
                if item.get("StopUID") == stop_uid
            ]
            if uid_results:
                results = uid_results

        if not results:
            await interaction.followup.send(
                f"找不到「{route}」在「{stop}」的站牌到站資料。",
                ephemeral=True
            )
            return

        results.sort(key=lambda item: (
            item.get("Direction", 9),
            item.get("EstimateTime") if item.get("EstimateTime") is not None else 999999,
            item.get("StopSequence", 999999),
        ))
        shown_results = results[:8]

        embed = discord.Embed(
            title=f"🚌 {city} {route} 公車到站資訊",
            description="\n\n".join(format_bus_arrival(item) for item in shown_results),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        if stop:
            embed.add_field(name="查詢站牌", value=stop, inline=True)
        embed.set_footer(text="資料來源：交通部 TDX 運輸資料流通服務")

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        print(f">>> TDX 公車 API 發生錯誤: {e}")
        await bot.notify_owner_error(e, interaction, extra_info=f"bus command for city={city_code}, route={route}, stop={stop}")
        await interaction.followup.send("❌ 獲取公車資料時發生錯誤，已回報開發者!", ephemeral=True)

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
    # 1. 立即回應 Discord，先佔住這次互動
    deferred = False
    acknowledged = False
    try:
        await interaction.response.defer(thinking=True)
        deferred = True
        acknowledged = True
    except (discord.NotFound, discord.HTTPException) as e:
        print(f">>> interaction.defer() 失敗: {e}")

    async def send_result(content=None, embed=None, ephemeral=False):
        nonlocal acknowledged, deferred
        if deferred:
            try:
                return await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
            except discord.HTTPException as exc:
                print(f">>> followup.send failed: {exc}")
                if getattr(exc, 'code', None) in (10062, 40060):
                    return None
                raise

        try:
            response = await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
            acknowledged = True
            return response
        except discord.HTTPException as exc:
            print(f">>> send_message failed: {exc}")
            if getattr(exc, 'code', None) not in (40060, 10062):
                raise
            deferred = True
            return await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)

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

    
    if not CWA_API_KEY:
        await send_result("❌ 伺服器端尚未設定氣象 API KEY，請聯絡管理員。", ephemeral=True)
        return

    # 接下來的 API 網址，就使用轉換後的 formatted_city
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
        # 3. 發送網路請求
        try:
            status, data = await fetch_weather_data()
        except (ssl.SSLCertVerificationError, aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError) as ssl_err:
            print(f">>> SSL 驗證失敗，改用 ssl=False 重試: {ssl_err}")
            await bot.notify_owner_error(
                ssl_err,
                interaction,
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

        # 4. 檢查是否有抓到該城市的資料
        locations = data.get('records', {}).get('location', [])
        if not locations:
            await send_result(f"找不到「{city}」的資料，請確認輸入的是台灣的縣市名稱喔！")
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

        await send_result(embed=embed)

    except Exception as e:
        print(f">>> 氣象 API 發生錯誤: {e}")
        await bot.notify_owner_error(e, interaction, extra_info=f"weather command for city={formatted_city}")
        try:
            await send_result("❌ 獲取天氣資料時發生錯誤，已回報開發者!", ephemeral=True)
        except Exception as e_send:
            print(f">>> 無法送出錯誤回應: {e_send}")

@bot.tree.command(name="bus", description="使用下拉式選單查詢台灣公車")
@app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
async def bus(interaction: discord.Interaction):
    await interaction.response.send_message(
        "請先選擇查詢縣市：",
        view=BusCityView(),
        ephemeral=True
    )

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

class BusRouteModal(discord.ui.Modal, title="輸入公車路線"):
    route = discord.ui.TextInput(
        label="公車路線",
        placeholder="例如：307、紅2、藍20",
        max_length=30
    )

    def __init__(self, city_name: str, city_code: str):
        super().__init__()
        self.city_name = city_name
        self.city_code = city_code

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        await send_bus_stop_picker(
            interaction,
            self.city_name,
            self.city_code,
            self.route.value.strip()
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f">>> 公車路線輸入視窗發生未知錯誤: {error}")
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        await bot.notify_owner_error(error, interaction, extra_info="BusRouteModal.on_error")
        if interaction.response.is_done():
            await interaction.followup.send("❌ 公車查詢發生未知錯誤，已回報開發者。", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 公車查詢發生未知錯誤，已回報開發者。", ephemeral=True)

class BusStopSelect(discord.ui.Select):
    def __init__(self, parent_view: "BusStopView"):
        self.parent_view = parent_view
        start = parent_view.page * parent_view.page_size
        end = start + parent_view.page_size
        page_stops = parent_view.stops[start:end]
        options = []

        for index, stop_info in enumerate(page_stops, start=start):
            label = f"{stop_info['direction_name']} {stop_info['sequence']}. {stop_info['name']}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(index),
                    description=f"查詢 {stop_info['name']} 到站資訊"[:100]
                )
            )

        super().__init__(
            placeholder=f"選擇站牌（第 {parent_view.page + 1}/{parent_view.total_pages} 頁）",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        stop_info = self.parent_view.stops[int(self.values[0])]
        await interaction.response.defer(thinking=True, ephemeral=True)
        await send_bus_result(
            interaction,
            self.parent_view.city_name,
            self.parent_view.route,
            stop_info["name"],
            city_code=self.parent_view.city_code,
            stop_direction=stop_info["direction"],
            stop_uid=stop_info["uid"]
        )

class BusStopView(discord.ui.View):
    page_size = 25

    def __init__(self, city_name: str, city_code: str, route: str, stops: list[dict], page: int = 0):
        super().__init__(timeout=180)
        self.city_name = city_name
        self.city_code = city_code
        self.route = route
        self.stops = stops
        self.page = page
        self.total_pages = max((len(stops) + self.page_size - 1) // self.page_size, 1)
        self.refresh_items()

    def refresh_items(self):
        self.clear_items()
        self.add_item(BusStopSelect(self))
        previous_button = discord.ui.Button(
            label="上一頁",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0
        )
        next_button = discord.ui.Button(
            label="下一頁",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.total_pages - 1
        )
        previous_button.callback = self.go_previous_page
        next_button.callback = self.go_next_page
        self.add_item(previous_button)
        self.add_item(next_button)

    async def go_previous_page(self, interaction: discord.Interaction):
        self.page = max(self.page - 1, 0)
        self.refresh_items()
        await interaction.response.edit_message(view=self)

    async def go_next_page(self, interaction: discord.Interaction):
        self.page = min(self.page + 1, self.total_pages - 1)
        self.refresh_items()
        await interaction.response.edit_message(view=self)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        print(f">>> 公車站牌選單發生未知錯誤: {error}")
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        await bot.notify_owner_error(error, interaction, extra_info=f"BusStopView.on_error item={item}")
        if interaction.response.is_done():
            await interaction.followup.send("❌ 公車站牌選單發生未知錯誤，已回報開發者。", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 公車站牌選單發生未知錯誤，已回報開發者。", ephemeral=True)

class BusCitySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=city_name, value=city_code)
            for city_name, city_code in TDX_CITY_OPTIONS
        ]
        super().__init__(
            placeholder="選擇縣市",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        city_code = self.values[0]
        city_name = next(
            name for name, code in TDX_CITY_OPTIONS
            if code == city_code
        )
        await interaction.response.send_modal(BusRouteModal(city_name, city_code))

class BusCityView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(BusCitySelect())

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        print(f">>> 公車縣市選單發生未知錯誤: {error}")
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        await bot.notify_owner_error(error, interaction, extra_info=f"BusCityView.on_error item={item}")
        if interaction.response.is_done():
            await interaction.followup.send("❌ 公車縣市選單發生未知錯誤，已回報開發者。", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 公車縣市選單發生未知錯誤，已回報開發者。", ephemeral=True)

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

# ================= 啟動與準備就緒事件 =================

@bot.event
async def on_ready():
    print(f'目前登入身份：{bot.user}')
    print('ℹ️ on_ready event fired')
    print('✅ 機器人已經百分之百在雲端準備就緒！')

    # fallback: 如果 setup_hook 沒成功執行導致還沒同步全域指令，這裡嘗試同步並把錯誤印出
    try:
        if not getattr(bot, '_global_synced', False):
            print('🔁 on_ready fallback: 嘗試同步全域指令...')
            synced = await bot.tree.sync()
            print(f'🎉 fallback 成功同步了 {len(synced)} 個全域斜線指令！')
            bot._global_synced = True
    except Exception as e:
        print(f'❌ on_ready fallback 同步失敗: {e}')
        traceback.print_exc()

# ================= 程式執行入口 =================
if __name__ == "__main__":
    # 啟動前檢查：若沒有設定 DISCORD_TOKEN，直接印出錯誤並退出，避免 silent failure
    if not TOKEN:
        print("❌ 環境變數 DISCORD_TOKEN 未設定或為空！請在環境變數中設定機器人 Token。")
        sys.exit(1)

    if not CWA_API_KEY:
        print("❌ 環境變數 CWA_API_KEY 未設定或為空！請在 .env 或系統環境變數中設定中央氣象署 API KEY。")
        sys.exit(1)

    # 3. 呼叫你寫的 keep_alive.py 裡面的函式，它會在背景自己開一條 Thread 跑網頁，絕不卡住主程式！
    keep_alive()
    print("🌐 外部 Flask 網頁伺服器已透過 keep_alive 模組在背景啟動...")
    
    # 4. 最後一行，大膽交給 Discord 機器人接管主執行緒！
    print("🤖 正在啟動 Discord 機器人主程式...")
    bot.run(TOKEN)
