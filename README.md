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
- `/music_join`：加入使用者目前所在的語音頻道
- `/music_leave`：離開目前所在的語音頻道
- `/music_play <query>`：搜尋並播放音樂；若目前已在播放，會自動排入佇列（支援關鍵字或直接貼 YouTube／SoundCloud 網址）
- `/music_play_next <query>`：插播，搜尋一首歌曲並插入佇列最前面，下一首就會播放它
- `/music_queue`：顯示目前伺服器的播放佇列（正在播放中的歌曲＋接下來排隊的清單）
- `/music_queue_clear`：清空目前的播放佇列（不影響正在播放的歌曲）
- `/music_node_status`：查看目前 Lavalink 節點的連線狀態（包含節點 URI、連線狀態、Session ID 及目前連線的伺服器數）

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
- wavelink

`wavelink` 已包含在 `requirements.txt` 中，執行 `pip install -r requirements.txt` 即可一併安裝。音樂功能另外需要準備可連線的 Lavalink 節點；未設定節點時，其他功能仍可正常啟動，但 `/music_join`、`/music_leave`、`/music_play` 系列指令無法使用。

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
EMPTY_VOICE_CHANNEL_TIMEOUT=語音頻道空掉多久後自動離開（秒，選填，預設 60）
LAVALINK_CONNECT_TIMEOUT=連線 Lavalink 節點的逾時秒數（選填，預設 15）
NODE_HEALTH_CHECK_INTERVAL=定期健康檢查節點連線狀態的間隔秒數（選填，預設 30）
```

`TDX_CLIENT_ID` 與 `TDX_CLIENT_SECRET` 用於 `/bus` 公車查詢。`DISCORD_OWNER_ID` 為選填，用來接收機器人錯誤通知。若未設定，錯誤通知會略過。
也可以使用 `OWNER_ID` 作為 `DISCORD_OWNER_ID` 的替代名稱。`LAVALINK_URI` 與 `LAVALINK_PASSWORD` 用於音樂功能的 Lavalink 連線；這兩項未設定時會略過節點連線。
`EMPTY_VOICE_CHANNEL_TIMEOUT` 為選填，設定語音頻道裡沒有真人成員（只剩機器人自己）超過幾秒後自動離開並清空佇列；未設定時預設為 60 秒。
`LAVALINK_CONNECT_TIMEOUT` 為選填，設定連線 Lavalink 節點的逾時秒數；超時後會 DM 通知 `DISCORD_OWNER_ID`，未設定時預設為 15 秒。
`NODE_HEALTH_CHECK_INTERVAL` 為選填，設定定期健康檢查 Lavalink 節點連線狀態的間隔秒數；節點離線時只 DM 通知一次，恢復連線後重置；未設定時預設為 30 秒。

若要啟用音樂功能，請確保已安裝 `wavelink`（已包含於 `requirements.txt`）並自行準備可連線的 Lavalink 節點，設定 `LAVALINK_URI` 與 `LAVALINK_PASSWORD` 環境變數。

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
- `/music_join`：機器人加入你目前所在的語音頻道；需要已安裝 `wavelink` 並設定 Lavalink。
- `/music_leave`：機器人離開目前所在的語音頻道，並清空該伺服器的播放佇列。
- `/music_play query:<關鍵字或網址>`：搜尋並播放音樂；若機器人尚未加入語音頻道會自動加入。若目前已經有歌曲在播放（或暫停中），新點的歌會依序排入佇列（FIFO），而不是直接取代。
- `/music_play_next query:<關鍵字或網址>`：插播。跟 `/music_play` 一樣會搜尋歌曲，但會插入佇列最前面，目前這首播完後會優先播放插播的歌曲。
- `/music_queue`：顯示目前伺服器正在播放的歌曲，以及接下來排隊中的清單（最多列出前 10 首）。
- `/music_queue_clear`：清空目前伺服器的播放佇列，正在播放中的歌曲不受影響。
- `/music_node_status`：查看目前所有已註冊 Lavalink 節點的連線狀態（URI、連線狀態、Session ID、目前連線的伺服器數）；僅使用者可見。

### 歡迎訊息功能說明

啟用後，每當有新成員加入伺服器，機器人會在指定的歡迎頻道發送一則嵌入訊息，包含：

- 新成員的大頭貼與 @ 標註
- 加入時間與目前成員人數
- 若有設定規則頻道，附上引導連結
- 若有設定身份組頻道，附上引導連結

歡迎設定會儲存於 `welcome_settings.json`，重啟機器人後不會遺失。

> **注意**：使用歡迎訊息功能前，請至 [Discord Developer Portal](https://discord.com/developers/applications) → **Bot** → **Privileged Gateway Intents** 開啟 **Server Members Intent**，否則 `on_member_join` 事件不會觸發。

機器人也會啟用 `Message Content Intent` 與語音狀態 intents，以支援目前的指令與 `/music_join`、`/music_leave`、`/music_play` 語音功能；請在 Discord Developer Portal 的 Bot 設定中依需求開啟對應權限。

### 特別說明

- `/weather` 會呼叫中央氣象署公開資料 API，若 SSL 驗證失敗會自動嘗試不驗證模式重試。
- `/bus` 會呼叫交通部 TDX API 查詢公車路線站點與即時到站資訊。所有公車查詢訊息皆為僅使用者可見，避免干擾頻道版面。
- `/bus` 若輸入不存在的公車號碼，會提示找不到站牌或到站資料；若發生未知錯誤，會自動嘗試 DM 通知 `DISCORD_OWNER_ID`。
- `/music_join`、`/music_leave`、`/music_play`、`/music_play_next`、`/music_queue`、`/music_queue_clear`、`/music_node_status` 需要已安裝 `wavelink` 並設定 `LAVALINK_URI` / `LAVALINK_PASSWORD`；未安裝或節點無法連線時，這些指令會回覆友善錯誤訊息，不影響機器人其他功能。
- Music Cog 啟動後會在背景以 `LAVALINK_CONNECT_TIMEOUT`（預設 15 秒）為逾時限制嘗試連線 Lavalink 節點（非同步，不阻擋機器人其他功能啟動）；連線成功後，每隔 `NODE_HEALTH_CHECK_INTERVAL`（預設 30 秒）定期檢查節點狀態，節點離線時 DM 通知 `DISCORD_OWNER_ID` 一次，恢復連線後重置旗標，避免重複通知。
- 播放佇列是各伺服器獨立的 FIFO（先進先出）佇列，實作於 `cogs/music.py` 的 `player.song_queue`（`collections.deque`）。歌曲播完（或被跳過、發生錯誤）時會觸發 wavelink 的 `on_wavelink_track_end` 事件，由監聽器從佇列最前面取出下一首並自動播放；佇列空了則會發一次通知。`player.autoplay` 因此設為 `disabled`，改由這個監聽器完全接管「播完接下一首」的邏輯，避免跟 wavelink 內建的 autoplay 互相搶播。
- 語音頻道裡只剩機器人自己（沒有真人成員）超過 `EMPTY_VOICE_CHANNEL_TIMEOUT` 秒（預設 60 秒）就會自動離開並清空佇列，並在最近一次下指令的文字頻道發一則通知。這個功能監聽 `discord.py` 的 `on_voice_state_update` 事件，每次有人加入/離開/切換頻道時檢查機器人所在頻道還有沒有真人；沒有的話才啟動倒數計時器，且倒數期間只要有人回來就會立刻取消，避免誤判暫時性的斷線重連。
- `/music_play` 搜尋失敗（來源網站無回應、反爬蟲封鎖等）會立即回覆錯誤訊息。歌曲成功排入播放後，若 Lavalink 節點在背景載入音訊時才失敗（例如 YouTube 判定需要登入、影片地區限制，或公開節點的來源連結失效），機器人會透過 `on_wavelink_track_exception` 監聽器把失敗原因回報到下指令當下的文字頻道，而不是只留在後台 log。目前公開 Lavalink 節點偶爾不穩定是已知風險，後續計畫改為自架節點。
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
- `/music_join`: join the voice channel where the user is currently connected
- `/music_leave`: leave the current voice channel
- `/music_play <query>`: search and play music; automatically queues the track if something is already playing (accepts keywords, or a YouTube/SoundCloud URL)
- `/music_play_next <query>`: play next (jump the queue), search a track and insert it at the front of the queue
- `/music_queue`: show the current server's playback queue (now playing + upcoming tracks)
- `/music_queue_clear`: clear the current playback queue (does not affect the currently playing track)
- `/music_node_status`: view the connection status of all registered Lavalink nodes (URI, status, Session ID, and connected guild count)

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
- wavelink

`wavelink` is already included in `requirements.txt`, so running `pip install -r requirements.txt` installs it together with all other dependencies. The music features also require a reachable Lavalink node configured via `LAVALINK_URI` and `LAVALINK_PASSWORD`. If Lavalink is unavailable, the bot still starts normally but music commands will not work.

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
EMPTY_VOICE_CHANNEL_TIMEOUT=seconds of an empty voice channel before auto-leaving (optional, default 60)
LAVALINK_CONNECT_TIMEOUT=seconds before the Lavalink connection attempt times out (optional, default 15)
NODE_HEALTH_CHECK_INTERVAL=interval in seconds between Lavalink node health checks (optional, default 30)
```

`TDX_CLIENT_ID` and `TDX_CLIENT_SECRET` are required for `/bus`. `DISCORD_OWNER_ID` is optional and is used to receive bot error notifications. If it is not set, error notifications will be skipped.
`OWNER_ID` can also be used as an alternative name for `DISCORD_OWNER_ID`. `LAVALINK_URI` and `LAVALINK_PASSWORD` configure the Lavalink connection for the music features. If either is missing, the bot skips the Lavalink connection.
`EMPTY_VOICE_CHANNEL_TIMEOUT` is optional: how many seconds a voice channel can have no human members (bot only) before the bot auto-leaves and clears its queue. Defaults to 60 seconds.
`LAVALINK_CONNECT_TIMEOUT` is optional: timeout in seconds for the initial Lavalink connection attempt; if exceeded, the owner receives a DM notification. Defaults to 15 seconds.
`NODE_HEALTH_CHECK_INTERVAL` is optional: interval in seconds between periodic Lavalink node health checks; the owner is notified once when a node goes offline and the flag resets upon recovery. Defaults to 30 seconds.

To enable the music features, ensure `wavelink` is installed (already included in `requirements.txt`) and set up a reachable Lavalink node with `LAVALINK_URI` and `LAVALINK_PASSWORD`.

### Run

```bash
python main.py
```

When launched, the bot starts the Flask background server from `cogs/web_server.py`, checks required environment variables, and then connects to Discord with `bot.run(TOKEN)`.

### Commands

- `/ping`: reply with current bot latency
- `/choice options:<text>`: enter options separated by spaces and the bot chooses one randomly
- `/quotes`: show buttons for random quote or joke
- `/weather city:<city name or English name>`: query weather, supports mappings like `臺北`, `Taichung`, `Matsu`
- `/bus`: select a city, enter a bus route, choose a stop from a dropdown menu, and view real-time arrival information
- `/server_info`: display the current server's details
- `/welcome_active welcome_channel:<channel> [rules_channel:<channel>] [role_channel:<channel>]`: enable welcome messages with a required welcome channel and optional rules/role channels. Requires **Manage Server** permission.
- `/welcome_inactive`: disable welcome messages for this server. Requires **Manage Server** permission.
- `/music_join`: join the user's current voice channel. Requires `wavelink` and a configured Lavalink node.
- `/music_leave`: leave the current voice channel, and clear that server's playback queue.
- `/music_play query:<keywords or URL>`: search and play music; auto-joins your voice channel if the bot isn't connected yet. If something is already playing (or paused), the new track is appended to the FIFO queue instead of replacing it.
- `/music_play_next query:<keywords or URL>`: play next / jump the queue. Same search as `/music_play`, but the track is inserted at the front of the queue and plays right after the current one.
- `/music_queue`: show what's currently playing plus the upcoming tracks in the queue (up to the first 10).
- `/music_queue_clear`: clear the current server's queue. The currently playing track is not affected.
- `/music_node_status`: view the connection status of all registered Lavalink nodes (URL, connection state, Session ID, connected guild count). Ephemeral (only visible to you).

### Welcome Message Feature

When enabled, the bot sends an embed to the configured welcome channel whenever a new member joins. The embed includes:

- The new member's avatar and @mention
- Join timestamp and current member count
- A link to the rules channel (if configured)
- A link to the role pickup channel (if configured)

Welcome settings are saved to `welcome_settings.json` and persist across restarts.

> **Important**: Before using the welcome feature, go to the [Discord Developer Portal](https://discord.com/developers/applications) → **Bot** → **Privileged Gateway Intents** and enable **Server Members Intent**, otherwise the `on_member_join` event will not fire.

The bot also enables the `Message Content Intent` and voice-state intents for the current commands and the `/music_join`, `/music_leave`, and `/music_play` voice features. Enable the corresponding intents in the Discord Developer Portal as needed.

### Notes

- `/weather` calls the Taiwan Central Weather Administration API. If SSL verification fails, it retries with SSL verification disabled.
- `/bus` calls the Taiwan TDX API for route stops and real-time arrival estimates. Bus query messages are ephemeral, so only the user who started the query can see them.
- `/bus` handles unknown route numbers with a clear not-found message. Unexpected errors trigger an owner DM when `DISCORD_OWNER_ID` is configured.
- `/music_join`, `/music_leave`, `/music_play`, `/music_play_next`, `/music_queue`, `/music_queue_clear`, and `/music_node_status` use Wavelink and require a reachable Lavalink node configured with `LAVALINK_URI` and `LAVALINK_PASSWORD`. The music Cog is loaded without stopping the bot when Wavelink or Lavalink is unavailable.
- On startup, the Music Cog connects to Lavalink in the background within `LAVALINK_CONNECT_TIMEOUT` seconds (default 15) without blocking other bot features. Once connected, a health-check loop runs every `NODE_HEALTH_CHECK_INTERVAL` seconds (default 30), DMing the owner once when a node goes offline and resetting the flag upon recovery to avoid repeated notifications.
- The playback queue is a per-server FIFO queue, implemented as `player.song_queue` (a `collections.deque`) in `cogs/music.py`. When a track ends (finishes, is skipped, or errors out), wavelink fires `on_wavelink_track_end`; the listener pops the next track off the front of the queue and plays it automatically, and sends a one-time notice once the queue is empty. `player.autoplay` is set to `disabled` so this listener fully owns the "advance to next track" logic instead of racing with wavelink's built-in autoplay.
- If a voice channel is left with only the bot (no human members) for more than `EMPTY_VOICE_CHANNEL_TIMEOUT` seconds (default 60), the bot automatically leaves and clears its queue, posting a notice to the text channel where it was last used. This listens to discord.py's `on_voice_state_update` event, checking the bot's channel every time someone joins, leaves, or switches channels; the countdown only starts once no humans remain, and is cancelled immediately if someone comes back, to avoid false positives from brief disconnects/reconnects.
- `/music_play` reports search failures (source unreachable, anti-bot blocking, etc.) immediately. If a track is accepted but later fails to load in the background (e.g. YouTube requiring login, region restrictions, or a broken stream link on a public node), the bot reports the failure to the text channel where the command was last used via the `on_wavelink_track_exception` listener, instead of only logging it. Public Lavalink node instability is a known risk here; self-hosting a node is planned for later weeks.
- The bot syncs global slash commands in `setup_hook`. If that fails, it retries in `on_ready` as a fallback.
- `cogs/web_server.py` runs a background Flask web service serving a status dashboard (`templates/index.html`) and the `/api/bot-stats` and `/api/uptime` endpoints.

### Tips

- To add a command, create or update a Cog under `cogs/`, then add its module path to `INITIAL_EXTENSIONS` in `main.py`.
- For cloud deployment, ensure the `PORT` environment variable or default port `8080` is accessible.
