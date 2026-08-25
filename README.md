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
- `/active_welcome`：設定伺服器歡迎訊息（歡迎頻道必填，規則頻道與身份組頻道選填）
- `/inactive_welcome`：取消伺服器歡迎訊息功能

專案也包含 `keep_alive.py`，用於在背景啟動一個 Flask HTTP 伺服器，方便部署於需要存活檢查的雲端平台。

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

### 環境變數

請在專案根目錄建立 `.env`，或直接將以下變數設定於系統環境：

```env
DISCORD_TOKEN=你的 Discord Bot Token
CWA_API_KEY=中央氣象署 API 金鑰
TDX_CLIENT_ID=交通部 TDX Client ID
TDX_CLIENT_SECRET=交通部 TDX Client Secret
DISCORD_OWNER_ID=你的 Discord 使用者 ID
```

`TDX_CLIENT_ID` 與 `TDX_CLIENT_SECRET` 用於 `/bus` 公車查詢。`DISCORD_OWNER_ID` 為選填，用來接收機器人錯誤通知。若未設定，錯誤通知會略過。

### 啟動方式

```bash
python main.py
```

啟動後，機器人會先檢查必要環境變數，然後啟動 `keep_alive.py` 中的 Flask 背景伺服器，再以 `bot.run(TOKEN)` 連線 Discord。

### 機器人指令說明

- `/ping`：回傳機器人目前延遲
- `/choice options:<文字>`：輸入用空格分隔的選項，機器人會隨機選一個
- `/quotes`：展示名言 / 笑話選擇按鈕
- `/weather city:<縣市名稱或英文>`：查詢天氣，支援如 `臺北`、`Taichung`、`Matsu` 等對照
- `/bus`：先選擇縣市，再輸入公車號碼，接著從下拉式選單選擇站牌並查詢即時到站資訊
- `/server_info`：顯示所在伺服器的詳細資訊
- `/active_welcome welcome_channel:<頻道> [rules_channel:<頻道>] [role_channel:<頻道>]`：啟用歡迎訊息，設定歡迎頻道（必填）、規則頻道（選填）、身份組頻道（選填）。需要「管理伺服器」權限。
- `/inactive_welcome`：停用本伺服器的歡迎訊息功能。需要「管理伺服器」權限。

### 歡迎訊息功能說明

啟用後，每當有新成員加入伺服器，機器人會在指定的歡迎頻道發送一則嵌入訊息，包含：

- 新成員的大頭貼與 @ 標註
- 加入時間與目前成員人數
- 若有設定規則頻道，附上引導連結
- 若有設定身份組頻道，附上引導連結

歡迎設定會儲存於 `welcome_settings.json`，重啟機器人後不會遺失。

> **注意**：使用歡迎訊息功能前，請至 [Discord Developer Portal](https://discord.com/developers/applications) → **Bot** → **Privileged Gateway Intents** 開啟 **Server Members Intent**，否則 `on_member_join` 事件不會觸發。

### 特別說明

- `/weather` 會呼叫中央氣象署公開資料 API，若 SSL 驗證失敗會自動嘗試不驗證模式重試。
- `/bus` 會呼叫交通部 TDX API 查詢公車路線站點與即時到站資訊。所有公車查詢訊息皆為僅使用者可見，避免干擾頻道版面。
- `/bus` 若輸入不存在的公車號碼，會提示找不到站牌或到站資料；若發生未知錯誤，會自動嘗試 DM 通知 `DISCORD_OWNER_ID`。
- 機器人在 `setup_hook` 內進行全域斜線指令同步，若同步失敗會在 `on_ready` 內再嘗試一次作為 fallback。
- `keep_alive.py` 會在背景執行一個簡單的 Flask 網頁服務，並回傳 `機器人正在運作中！`。

### 開發建議

- 若要新增指令，可在 `main.py` 中擴充 `bot.tree.command`。
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
- `/active_welcome`: set up a server welcome message (welcome channel required; rules and role channels optional)
- `/inactive_welcome`: disable the server welcome message feature

The project also includes `keep_alive.py`, which starts a Flask HTTP server in the background for cloud deployments that require a keep-alive endpoint.

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

### Environment Variables

Create a `.env` file in the project root, or set these variables in your environment:

```env
DISCORD_TOKEN=your Discord bot token
CWA_API_KEY=your Central Weather Administration API key
TDX_CLIENT_ID=your TDX Client ID
TDX_CLIENT_SECRET=your TDX Client Secret
DISCORD_OWNER_ID=your Discord user ID
```

`TDX_CLIENT_ID` and `TDX_CLIENT_SECRET` are required for `/bus`. `DISCORD_OWNER_ID` is optional and is used to receive bot error notifications. If it is not set, error notifications will be skipped.

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
- `/active_welcome welcome_channel:<channel> [rules_channel:<channel>] [role_channel:<channel>]`: enable welcome messages with a required welcome channel and optional rules/role channels. Requires **Manage Server** permission.
- `/inactive_welcome`: disable welcome messages for this server. Requires **Manage Server** permission.

### Welcome Message Feature

When enabled, the bot sends an embed to the configured welcome channel whenever a new member joins. The embed includes:

- The new member's avatar and @mention
- Join timestamp and current member count
- A link to the rules channel (if configured)
- A link to the role pickup channel (if configured)

Welcome settings are saved to `welcome_settings.json` and persist across restarts.

> **Important**: Before using the welcome feature, go to the [Discord Developer Portal](https://discord.com/developers/applications) → **Bot** → **Privileged Gateway Intents** and enable **Server Members Intent**, otherwise the `on_member_join` event will not fire.

### Notes

- `/weather` calls the Taiwan Central Weather Administration API. If SSL verification fails, it retries with SSL verification disabled.
- `/bus` calls the Taiwan TDX API for route stops and real-time arrival estimates. Bus query messages are ephemeral, so only the user who started the query can see them.
- `/bus` handles unknown route numbers with a clear not-found message. Unexpected errors trigger an owner DM when `DISCORD_OWNER_ID` is configured.
- The bot syncs global slash commands in `setup_hook`. If that fails, it retries in `on_ready` as a fallback.
- `keep_alive.py` runs a background Flask web service that responds with `機器人正在運作中！`.

### Tips

- To add commands, extend `bot.tree.command` in `main.py`.
- For cloud deployment, ensure the `PORT` environment variable or default port `8080` is accessible.
