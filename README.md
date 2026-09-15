# Discord Bot

## 中文版

### 簡介

這是一個使用 `discord.py` 與 `aiohttp` 編寫的 Discord 機器人專案。

支援功能：

- `/ping`：檢查機器人延遲
- `/choice`：從使用者輸入的選項中隨機選一個
- `/quotes`：顯示隨機名言或笑話，並支援互動按鈕
- `/weather <city>`：查詢全台各縣市即時天氣預報
- `/bus`：使用下拉式選單查詢台灣公車即時到站資訊
- `/server_info`：顯示目前伺服器詳細資訊
- `/welcome_active`：設定伺服器歡迎訊息（歡迎頻道必填，規則頻道與身份組頻道選填）
- `/welcome_inactive`：取消伺服器歡迎訊息功能
- `/join`：加入使用者目前所在的語音頻道
- `/leave`：離開目前所在的語音頻道
- `/play <query>`：搜尋並播放音樂（支援關鍵字或直接貼 YouTube／SoundCloud 網址）

專案也包含 `cogs/web_server.py`，會在背景啟動一個 Flask 網頁伺服器（搭配 `templates/index.html` 儀表板），提供機器人狀態 API（伺服器數、延遲、運行時間等），方便部署於需要存活檢查的雲端平台。

### 專案需求

請先安裝套件：

```bash
pip install -r requirements.txt
```

`requirements.txt` 內容：

- discord.py
- python-dotenv
- Flask
- certifi
- aiohttp

音樂功能另外需要安裝 `wavelink`，並準備可連線的 Lavalink 節點；未安裝或未設定時，其他功能仍可正常啟動，但 `/join`、`/leave`、`/play` 無法使用。

### 環境變數

請在專案根目錄建立 `.env`，或直接將以下變數設定於系統環境：

```env
DISCORD_TOKEN=你的 Discord Bot Token
CWA_API_KEY=中央氣象署 API 金鑰
TDX_CLIENT_ID=交通部 TDX Client ID
TDX_CLIENT_SECRET=交通部 TDX Client Secret
DISCORD_OWNER_ID=你的 Discord 使用者 ID
LAVALINK_URI=Lavalink 節點網址
LAVALINK_PASSWORD=Lavalink 節點密碼
```

`TDX_CLIENT_ID` 與 `TDX_CLIENT_SECRET` 用於 `/bus` 公車查詢。`DISCORD_OWNER_ID` 為選填，用來接收機器人錯誤通知。若未設定，錯誤通知會略過。
也可以使用 `OWNER_ID` 作為 `DISCORD_OWNER_ID` 的替代名稱。`LAVALINK_URI` 與 `LAVALINK_PASSWORD` 用於音樂功能的 Lavalink 連線；這兩項未設定時會略過節點連線。

若要啟用音樂功能，請另外安裝套件並啟動 Lavalink：

```bash
pip install wavelink
```

### 啟動方式

```bash
python main.py
```

啟動後，機器人會先啟動 `cogs/web_server.py` 中的 Flask 背景伺服器，再檢查必要環境變數，最後以 `bot.run(TOKEN)` 連線 Discord。

### 機器人指令說明

- `/ping`：回傳機器人目前延遲
- `/choice options:<文字>`：輸入用空格分隔的選項，機器人會隨機選一個
- `/quotes`：展示名言 / 笑話選擇按鈕
- `/weather city:<縣市名稱或英文>`：查詢天氣，支援如 `臺北`、`Taichung`、`Matsu` 等對照
- `/bus`：先選擇縣市，再輸入公車號碼，接著從下拉式選單選擇站牌並查詢即時到站資訊
- `/server_info`：顯示所在伺服器的詳細資訊
- `/welcome_active welcome_channel:<頻道> [rules_channel:<頻道>] [role_channel:<頻道>]`：啟用歡迎訊息，設定歡迎頻道（必填）、規則頻道（選填）、身份組頻道（選填）。需要「管理伺服器」權限。
- `/welcome_inactive`：停用本伺服器的歡迎訊息功能。需要「管理伺服器」權限。
- `/join`：機器人加入你目前所在的語音頻道；需要已安裝 `wavelink` 並設定 Lavalink。
- `/leave`：機器人離開目前所在的語音頻道。
- `/play query:<關鍵字或網址>`：搜尋並播放音樂；若機器人尚未加入語音頻道會自動加入。目前尚無播放佇列（預計第3週實作），重複下指令會直接取代正在播放的歌曲。

### 歡迎訊息功能說明

啟用後，每當有新成員加入伺服器，機器人會在指定的歡迎頻道發送一則嵌入訊息，包含：

- 新成員的大頭貼與 @ 標註
- 加入時間與目前成員人數
- 若有設定規則頻道，附上引導連結
- 若有設定身份組頻道，附上引導連結

歡迎設定會儲存於 `welcome_settings.json`，重啟機器人後不會遺失。

> **注意**：使用歡迎訊息功能前，請至 [Discord Developer Portal](https://discord.com/developers/applications) → **Bot** → **Privileged Gateway Intents** 開啟 **Server Members Intent**，否則 `on_member_join` 事件不會觸發。

機器人也會啟用 `Message Content Intent` 與語音狀態 intents，以支援目前的指令與 `/join`、`/leave`、`/play` 語音功能；請在 Discord Developer Portal 的 Bot 設定中依需求開啟對應權限。

### 特別說明

- `/weather` 會呼叫中央氣象署公開資料 API，若 SSL 驗證失敗會自動嘗試不驗證模式重試。
- `/bus` 會呼叫交通部 TDX API 查詢公車路線站點與即時到站資訊。所有公車查詢訊息皆為僅使用者可見，避免干擾頻道版面。
- `/bus` 若輸入不存在的公車號碼，會提示找不到站牌或到站資料；若發生未知錯誤，會自動嘗試 DM 通知 `DISCORD_OWNER_ID`。
- `/join`、`/leave`、`/play` 需要已安裝 `wavelink` 並設定 `LAVALINK_URI` / `LAVALINK_PASSWORD`；未安裝或節點無法連線時，這些指令會回覆友善錯誤訊息，不影響機器人其他功能。
- `/play` 搜尋失敗（來源網站無回應、反爬蟲封鎖等）會立即回覆錯誤訊息。歌曲成功排入播放後，若 Lavalink 節點在背景載入音訊時才失敗（例如 YouTube 判定需要登入、影片地區限制，或公開節點的來源連結失效），機器人會透過 `on_wavelink_track_exception` 監聽器把失敗原因回報到下指令當下的文字頻道，而不是只留在後台 log。目前公開 Lavalink 節點偶爾不穩定是已知風險，後續計畫改為自架節點。
- 機器人在 `setup_hook` 內進行全域斜線指令同步，若同步失敗會在 `on_ready` 內再嘗試一次作為 fallback。
- `cogs/web_server.py` 會在背景執行 Flask 網頁服務，首頁 `/` 顯示機器人狀態儀表板（`templates/index.html`），並提供 `/api/bot-stats`、`/api/uptime` 兩支 API。

### 開發建議

- 若要新增指令，請在 `cogs/` 建立或修改 Cog，再將模組路徑加入 `main.py` 的 `INITIAL_EXTENSIONS`。
- 若要部署到雲端平台，請確認 `PORT` 環境變數或預設 `8080` 可正常對外連線。

---

## English Version

### Overview

This is a Discord bot project written with `discord.py` and `aiohttp`.

Supported features:

- `/ping`: check bot latency
- `/choice`: randomly select one option from user input
- `/quotes`: show random quotes or jokes with interaction buttons
- `/weather <city>`: query real-time weather for Taiwan cities
- `/bus`: query Taiwan bus arrivals through dropdown menus
- `/server_info`: display detailed server information
- `/welcome_active`: set up a server welcome message (welcome channel required; rules and role channels optional)
- `/welcome_inactive`: disable the server welcome message feature
- `/join`: join the voice channel where the user is currently connected
- `/leave`: leave the current voice channel
- `/play <query>`: search and play music (accepts keywords, or a YouTube/SoundCloud URL)

The project also includes `cogs/web_server.py`, which starts a Flask web server in the background (backing a `templates/index.html` dashboard) that exposes bot status APIs (guild count, latency, uptime, etc.) for cloud deployments that require a keep-alive endpoint.

### Requirements

Install dependencies first:

```bash
pip install -r requirements.txt
```

`requirements.txt` contains:

- discord.py
- python-dotenv
- Flask
- certifi
- aiohttp

The music features also require `wavelink` and a reachable Lavalink node. If Wavelink or Lavalink is unavailable, the bot can still start, but the music features will not work.

### Environment Variables

Create a `.env` file in the project root, or set these variables in your environment:

```env
DISCORD_TOKEN=your Discord bot token
CWA_API_KEY=your Central Weather Administration API key
TDX_CLIENT_ID=your TDX Client ID
TDX_CLIENT_SECRET=your TDX Client Secret
DISCORD_OWNER_ID=your Discord user ID
LAVALINK_URI=your Lavalink node URL
LAVALINK_PASSWORD=your Lavalink node password
```

`TDX_CLIENT_ID` and `TDX_CLIENT_SECRET` are required for `/bus`. `DISCORD_OWNER_ID` is optional and is used to receive bot error notifications. If it is not set, error notifications will be skipped.
`OWNER_ID` can also be used as an alternative name for `DISCORD_OWNER_ID`. `LAVALINK_URI` and `LAVALINK_PASSWORD` configure the Lavalink connection for the music features. If either is missing, the bot skips the Lavalink connection.

To enable the music features, install Wavelink separately and run a Lavalink node:

```bash
pip install wavelink
```

### Run

```bash
python main.py
```

When launched, the bot checks required environment variables, starts the Flask background server from `keep_alive.py`, and then connects to Discord with `bot.run(TOKEN)`.

### Commands

- `/ping`: reply with current bot latency
- `/choice options:<text>`: enter options separated by spaces and the bot chooses one randomly
- `/quotes`: show buttons for random quote or joke
- `/weather city:<city name or English name>`: query weather, supports mappings like `臺北`, `Taichung`, `Matsu`
- `/bus`: select a city, enter a bus route, choose a stop from a dropdown menu, and view real-time arrival information
- `/server_info`: display the current server's details
- `/welcome_active welcome_channel:<channel> [rules_channel:<channel>] [role_channel:<channel>]`: enable welcome messages with a required welcome channel and optional rules/role channels. Requires **Manage Server** permission.
- `/welcome_inactive`: disable welcome messages for this server. Requires **Manage Server** permission.
- `/join`: join the user's current voice channel. Requires `wavelink` and a configured Lavalink node.
- `/leave`: leave the current voice channel.
- `/play query:<keywords or URL>`: search and play music; auto-joins your voice channel if the bot isn't connected yet. There's no playback queue yet (planned for Week 3), so calling it again replaces the currently playing track.

### Welcome Message Feature

When enabled, the bot sends an embed to the configured welcome channel whenever a new member joins. The embed includes:

- The new member's avatar and @mention
- Join timestamp and current member count
- A link to the rules channel (if configured)
- A link to the role pickup channel (if configured)

Welcome settings are saved to `welcome_settings.json` and persist across restarts.

> **Important**: Before using the welcome feature, go to the [Discord Developer Portal](https://discord.com/developers/applications) → **Bot** → **Privileged Gateway Intents** and enable **Server Members Intent**, otherwise the `on_member_join` event will not fire.

The bot also enables the `Message Content Intent` and voice-state intents for the current commands and the `/join`, `/leave`, and `/play` voice features. Enable the corresponding intents in the Discord Developer Portal as needed.

### Notes

- `/weather` calls the Taiwan Central Weather Administration API. If SSL verification fails, it retries with SSL verification disabled.
- `/bus` calls the Taiwan TDX API for route stops and real-time arrival estimates. Bus query messages are ephemeral, so only the user who started the query can see them.
- `/bus` handles unknown route numbers with a clear not-found message. Unexpected errors trigger an owner DM when `DISCORD_OWNER_ID` is configured.
- `/join`, `/leave`, and `/play` use Wavelink and require a reachable Lavalink node configured with `LAVALINK_URI` and `LAVALINK_PASSWORD`. The music Cog is loaded without stopping the bot when Wavelink or Lavalink is unavailable.
- `/play` reports search failures (source unreachable, anti-bot blocking, etc.) immediately. If a track is accepted but later fails to load in the background (e.g. YouTube requiring login, region restrictions, or a broken stream link on a public node), the bot reports the failure to the text channel where the command was last used via the `on_wavelink_track_exception` listener, instead of only logging it. Public Lavalink node instability is a known risk here; self-hosting a node is planned for later weeks.
- The bot syncs global slash commands in `setup_hook`. If that fails, it retries in `on_ready` as a fallback.
- `cogs/web_server.py` runs a background Flask web service serving a status dashboard (`templates/index.html`) and the `/api/bot-stats` and `/api/uptime` endpoints.

### Tips

- To add a command, create or update a Cog under `cogs/`, then add its module path to `INITIAL_EXTENSIONS` in `main.py`.
- For cloud deployment, ensure the `PORT` environment variable or default port `8080` is accessible.
