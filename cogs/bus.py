import os
import ssl
import sys
import traceback
from urllib.parse import quote

import aiohttp
import certifi
import discord
from discord import app_commands
from discord.ext import commands

TDX_CLIENT_ID = os.getenv('TDX_CLIENT_ID')
TDX_CLIENT_SECRET = os.getenv('TDX_CLIENT_SECRET')

TDX_CITY_MAP = {
    "臺北": "Taipei", "台北": "Taipei", "臺北市": "Taipei", "台北市": "Taipei", "Taipei": "Taipei",
    "新北": "NewTaipei", "新北市": "NewTaipei", "New Taipei": "NewTaipei", "NewTaipei": "NewTaipei",
    "基隆": "Keelung", "基隆市": "Keelung", "Keelung": "Keelung",
    "桃園": "Taoyuan", "桃園市": "Taoyuan", "Taoyuan": "Taoyuan",
    "新竹": "Hsinchu", "新竹市": "Hsinchu", "Hsinchu": "Hsinchu",
    "新竹縣": "HsinchuCounty", "HsinchuCounty": "HsinchuCounty",
    "苗栗": "MiaoliCounty", "苗栗縣": "MiaoliCounty", "Miaoli": "MiaoliCounty", "MiaoliCounty": "MiaoliCounty",
    "臺中": "Taichung", "台中": "Taichung", "臺中市": "Taichung", "台中市": "Taichung", "Taichung": "Taichung",
    "彰化": "ChanghuaCounty", "彰化縣": "ChanghuaCounty", "Changhua": "ChanghuaCounty", "ChanghuaCounty": "ChanghuaCounty",
    "南投": "NantouCounty", "南投縣": "NantouCounty", "Nantou": "NantouCounty", "NantouCounty": "NantouCounty",
    "雲林": "YunlinCounty", "雲林縣": "YunlinCounty", "Yunlin": "YunlinCounty", "YunlinCounty": "YunlinCounty",
    "嘉義": "Chiayi", "嘉義市": "Chiayi", "Chiayi": "Chiayi",
    "嘉義縣": "ChiayiCounty", "ChiayiCounty": "ChiayiCounty",
    "臺南": "Tainan", "台南": "Tainan", "臺南市": "Tainan", "台南市": "Tainan", "Tainan": "Tainan",
    "高雄": "Kaohsiung", "高雄市": "Kaohsiung", "Kaohsiung": "Kaohsiung",
    "屏東": "PingtungCounty", "屏東縣": "PingtungCounty", "Pingtung": "PingtungCounty", "PingtungCounty": "PingtungCounty",
    "宜蘭": "YilanCounty", "宜蘭縣": "YilanCounty", "Yilan": "YilanCounty", "YilanCounty": "YilanCounty",
    "花蓮": "HualienCounty", "花蓮縣": "HualienCounty", "Hualien": "HualienCounty", "HualienCounty": "HualienCounty",
    "臺東": "TaitungCounty", "台東": "TaitungCounty", "臺東縣": "TaitungCounty", "台東縣": "TaitungCounty",
    "Taitung": "TaitungCounty", "TaitungCounty": "TaitungCounty",
    "澎湖": "PenghuCounty", "澎湖縣": "PenghuCounty", "Penghu": "PenghuCounty", "PenghuCounty": "PenghuCounty",
    "金門": "KinmenCounty", "金門縣": "KinmenCounty", "Kinmen": "KinmenCounty", "KinmenCounty": "KinmenCounty",
    "連江": "LienchiangCounty", "連江縣": "LienchiangCounty", "馬祖": "LienchiangCounty",
    "Lienchiang": "LienchiangCounty", "LienchiangCounty": "LienchiangCounty", "Matsu": "LienchiangCounty",
}

TDX_CITY_OPTIONS = [
    ("臺北市", "Taipei"), ("新北市", "NewTaipei"), ("基隆市", "Keelung"), ("桃園市", "Taoyuan"),
    ("新竹市", "Hsinchu"), ("新竹縣", "HsinchuCounty"), ("苗栗縣", "MiaoliCounty"), ("臺中市", "Taichung"),
    ("彰化縣", "ChanghuaCounty"), ("南投縣", "NantouCounty"), ("雲林縣", "YunlinCounty"), ("嘉義市", "Chiayi"),
    ("嘉義縣", "ChiayiCounty"), ("臺南市", "Tainan"), ("高雄市", "Kaohsiung"), ("屏東縣", "PingtungCounty"),
    ("宜蘭縣", "YilanCounty"), ("花蓮縣", "HualienCounty"), ("臺東縣", "TaitungCounty"), ("澎湖縣", "PenghuCounty"),
    ("金門縣", "KinmenCounty"), ("連江縣", "LienchiangCounty"),
]

BUS_STOP_STATUS = {0: "即將進站", 1: "尚未發車", 2: "交管不停靠", 3: "末班車已過", 4: "今日未營運"}
BUS_DIRECTION = {0: "去程", 1: "返程"}


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
                "name": stop_name, "direction": direction, "direction_name": direction_name,
                "sequence": sequence, "uid": stop_uid,
            })

    stops.sort(key=lambda item: (
        item["direction"] if item["direction"] is not None else 9,
        item["sequence"], item["name"],
    ))
    return stops


class BusRouteModal(discord.ui.Modal, title="輸入公車路線"):
    route = discord.ui.TextInput(label="公車路線", placeholder="例如：307、紅2、藍20", max_length=30)

    def __init__(self, cog: "Bus", city_name: str, city_code: str):
        super().__init__()
        self.cog = cog
        self.city_name = city_name
        self.city_code = city_code

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        await self.cog.send_bus_stop_picker(interaction, self.city_name, self.city_code, self.route.value.strip())

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f">>> 公車路線輸入視窗發生未知錯誤: {error}")
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        await self.cog.bot.notify_owner_error(error, interaction, extra_info="BusRouteModal.on_error")
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
                    label=label[:100], value=str(index),
                    description=f"查詢 {stop_info['name']} 到站資訊"[:100]
                )
            )

        super().__init__(
            placeholder=f"選擇站牌（第 {parent_view.page + 1}/{parent_view.total_pages} 頁）",
            min_values=1, max_values=1, options=options
        )

    async def callback(self, interaction: discord.Interaction):
        stop_info = self.parent_view.stops[int(self.values[0])]
        await interaction.response.defer(thinking=True, ephemeral=True)
        await self.parent_view.cog.send_bus_result(
            interaction, self.parent_view.city_name, self.parent_view.route,
            stop_info["name"], city_code=self.parent_view.city_code,
            stop_direction=stop_info["direction"], stop_uid=stop_info["uid"]
        )


class BusStopView(discord.ui.View):
    page_size = 25

    def __init__(self, cog: "Bus", city_name: str, city_code: str, route: str, stops: list[dict], page: int = 0):
        super().__init__(timeout=180)
        self.cog = cog
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
        previous_button = discord.ui.Button(label="上一頁", style=discord.ButtonStyle.secondary, disabled=self.page <= 0)
        next_button = discord.ui.Button(label="下一頁", style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages - 1)
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
        await self.cog.bot.notify_owner_error(error, interaction, extra_info=f"BusStopView.on_error item={item}")
        if interaction.response.is_done():
            await interaction.followup.send("❌ 公車站牌選單發生未知錯誤，已回報開發者。", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 公車站牌選單發生未知錯誤，已回報開發者。", ephemeral=True)


class BusCitySelect(discord.ui.Select):
    def __init__(self, cog: "Bus"):
        self.cog = cog
        options = [discord.SelectOption(label=city_name, value=city_code) for city_name, city_code in TDX_CITY_OPTIONS]
        super().__init__(placeholder="選擇縣市", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        city_code = self.values[0]
        city_name = next(name for name, code in TDX_CITY_OPTIONS if code == city_code)
        await interaction.response.send_modal(BusRouteModal(self.cog, city_name, city_code))


class BusCityView(discord.ui.View):
    def __init__(self, cog: "Bus"):
        super().__init__(timeout=120)
        self.cog = cog
        self.add_item(BusCitySelect(cog))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        print(f">>> 公車縣市選單發生未知錯誤: {error}")
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        await self.cog.bot.notify_owner_error(error, interaction, extra_info=f"BusCityView.on_error item={item}")
        if interaction.response.is_done():
            await interaction.followup.send("❌ 公車縣市選單發生未知錯誤，已回報開發者。", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 公車縣市選單發生未知錯誤，已回報開發者。", ephemeral=True)


class Bus(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def send_bus_stop_picker(self, interaction: discord.Interaction, city_name: str, city_code: str, route: str):
        route = route.strip()
        if not route:
            await interaction.followup.send("請輸入公車路線名稱。", ephemeral=True)
            return

        if not TDX_CLIENT_ID or not TDX_CLIENT_SECRET:
            await interaction.followup.send("❌ 伺服器端尚未設定 TDX_CLIENT_ID / TDX_CLIENT_SECRET，請聯絡管理員。", ephemeral=True)
            return

        try:
            try:
                status, data = await fetch_bus_stops(city_code, route)
            except (ssl.SSLCertVerificationError, aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError) as ssl_err:
                print(f">>> TDX 站牌 API SSL 驗證失敗，改用 ssl=False 重試: {ssl_err}")
                await self.bot.notify_owner_error(ssl_err, interaction, extra_info=f"bus stops SSL verification failed for city={city_code}, route={route}")
                status, data = await fetch_bus_stops(city_code, route, use_insecure=True)

            if status != 200:
                print(f">>> TDX 站牌 API 非 200 回應: {status} / {data}")
                if status in (400, 404):
                    await interaction.followup.send(f"找不到「{city_name} {route}」的站牌資料，請確認公車號碼是否正確。", ephemeral=True)
                else:
                    await interaction.followup.send("⚠️ 公車站牌資料服務暫時無法連線，請稍後再試！", ephemeral=True)
                return

            if not isinstance(data, list) or not data:
                await interaction.followup.send(f"找不到「{city_name} {route}」的站牌資料，請確認公車號碼是否正確。", ephemeral=True)
                return

            stops = parse_bus_stops(data)
            if not stops:
                await interaction.followup.send(f"「{city_name} {route}」目前沒有可選擇的站牌資料。", ephemeral=True)
                return

            await interaction.followup.send(
                f"請選擇「{city_name} {route}」要查詢的站牌：",
                view=BusStopView(self, city_name, city_code, route, stops),
                ephemeral=True
            )

        except Exception as e:
            print(f">>> TDX 站牌 API 發生錯誤: {e}")
            await self.bot.notify_owner_error(e, interaction, extra_info=f"bus stops for city={city_code}, route={route}")
            await interaction.followup.send("❌ 獲取站牌資料時發生錯誤，已回報開發者!", ephemeral=True)

    async def send_bus_result(self, interaction, city, route, stop=None, city_code=None, stop_direction=None, stop_uid=None):
        city_code = city_code or resolve_tdx_city(city)
        if not city_code:
            await interaction.followup.send(f"找不到「{city}」對應的縣市，請輸入例如：臺北、新北、桃園、臺中、臺南、高雄。", ephemeral=True)
            return

        if not TDX_CLIENT_ID or not TDX_CLIENT_SECRET:
            await interaction.followup.send("❌ 伺服器端尚未設定 TDX_CLIENT_ID / TDX_CLIENT_SECRET，請聯絡管理員。", ephemeral=True)
            return

        try:
            try:
                status, data = await fetch_bus_estimates(city_code, route)
            except (ssl.SSLCertVerificationError, aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError) as ssl_err:
                print(f">>> TDX SSL 驗證失敗，改用 ssl=False 重試: {ssl_err}")
                await self.bot.notify_owner_error(ssl_err, interaction, extra_info=f"bus command SSL verification failed for city={city_code}, route={route}")
                status, data = await fetch_bus_estimates(city_code, route, use_insecure=True)

            if status != 200:
                print(f">>> TDX 公車 API 非 200 回應: {status} / {data}")
                if status in (400, 404):
                    await interaction.followup.send(f"找不到「{city} {route}」的公車到站資料，請確認公車號碼是否正確。", ephemeral=True)
                else:
                    await interaction.followup.send("⚠️ 公車資料服務暫時無法連線，請稍後再試！", ephemeral=True)
                return

            if not isinstance(data, list) or not data:
                await interaction.followup.send(f"找不到「{city} {route}」的公車到站資料，請確認公車號碼是否正確。", ephemeral=True)
                return

            results = data
            if stop:
                stop_keyword = stop.strip()
                results = [item for item in data if stop_keyword in item.get("StopName", {}).get("Zh_tw", "")]
            if stop_direction is not None:
                results = [item for item in results if item.get("Direction") == stop_direction]
            if stop_uid:
                uid_results = [item for item in results if item.get("StopUID") == stop_uid]
                if uid_results:
                    results = uid_results

            if not results:
                await interaction.followup.send(f"找不到「{route}」在「{stop}」的站牌到站資料。", ephemeral=True)
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
            await self.bot.notify_owner_error(e, interaction, extra_info=f"bus command for city={city_code}, route={route}, stop={stop}")
            await interaction.followup.send("❌ 獲取公車資料時發生錯誤，已回報開發者!", ephemeral=True)

    @app_commands.command(name="bus", description="使用下拉式選單查詢台灣公車")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def bus(self, interaction: discord.Interaction):
        await interaction.response.send_message("請先選擇查詢縣市：", view=BusCityView(self), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Bus(bot))
