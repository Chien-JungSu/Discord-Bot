# Discord Bot

## 中文版

### 簡介

這是一個使用 `discord.py` 與 `aiohttp` 編寫的 Discord 機器人專案。

支援功能：

- `/ping`：檢查機器人延遲
- `/choice`：從使用者輸入的選項中隨機選一個
- `/quotes`：顯示隨機名言或笑話，並支援互動按鈕
- `/weather <city>`：查詢全台各縣市即時天氣預報
- `/server_info`：顯示目前伺服器詳細資訊

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

### 環境變數

請在專案根目錄建立 `.env`，或直接將以下變數設定於系統環境：

```env
DISCORD_TOKEN=你的 Discord Bot Token
CWA_API_KEY=中央氣象署 API 金鑰
DISCORD_OWNER_ID=你的 Discord 使用者 ID
```

`DISCORD_OWNER_ID` 為選填，用來接收機器人錯誤通知。若未設定，錯誤通知會略過。

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
- `/server_info`：顯示所在伺服器的詳細資訊

### 特別說明

- `/weather` 會呼叫中央氣象署公開資料 API，若 SSL 驗證失敗會自動嘗試不驗證模式重試。
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
- `/server_info`: display detailed server information

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

### Environment Variables

Create a `.env` file in the project root, or set these variables in your environment:

```env
DISCORD_TOKEN=your Discord bot token
CWA_API_KEY=your Central Weather Administration API key
DISCORD_OWNER_ID=your Discord user ID
```

`DISCORD_OWNER_ID` is optional and is used to receive bot error notifications. If it is not set, error notifications will be skipped.

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
- `/server_info`: display the current server's details

### Notes

- `/weather` calls the Taiwan Central Weather Administration API. If SSL verification fails, it retries with SSL verification disabled.
- The bot syncs global slash commands in `setup_hook`. If that fails, it retries in `on_ready` as a fallback.
- `keep_alive.py` runs a background Flask web service that responds with `機器人正在運作中！`.

### Tips

- To add commands, extend `bot.tree.command` in `main.py`.
- For cloud deployment, ensure the `PORT` environment variable or default port `8080` is accessible.
