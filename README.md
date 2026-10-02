# Discord Bot

## 中文版

### 簡介

這是一個使用 `discord.py` 與 `aiohttp` 編寫的 Discord 機器人專案。

支援功能：

- `/ping`：檢查機器人延遲
- `/choice`：從使用者輸入的選項中隨機選一個
- `/quotes`：顯示隨機名言或笑話，並支援互動按鈕
- `/steal emoji:<emoji>`：從 Discord 表情符號字串或引用直接下載並複製到本伺服器；可一次處理多個 emoji，超過 256 KB 時會給出友善文字提示
- `/weather <city>`：查詢全台各縣市即時天氣預報
- `/bus`：使用下拉式選單查詢台灣公車即時到站資訊
- `/server_info`：顯示目前伺服器詳細資訊
- `/welcome_active`：設定伺服器歡迎訊息（歡迎頻道必填，規則頻道與身份組頻道選填）
- `/welcome_inactive`：取消伺服器歡迎訊息功能
- `/reaction_roles`：發送「按表符領身份組」訊息（每伺服器可多則），成員按表符自動獲得身份組、取消表符自動收回，亂按的其他表符會被自動移除；需「管理伺服器」權限
- `/music_join`：加入使用者目前所在的語音頻道
- `/music_leave`：離開目前所在的語音頻道
- `/music_play <query>`：搜尋並播放音樂；若目前已在播放，會自動排入佇列（支援關鍵字或直接貼 YouTube／SoundCloud 網址）
- `/music_play_next <query>`：插播，搜尋一首歌曲並插入佇列最前面，下一首就會播放它
- `/music_queue`：顯示目前伺服器的播放佇列（正在播放中的歌曲＋接下來排隊的清單）
- `/music_queue_clear`：清空目前的播放佇列（不影響正在播放的歌曲）
- `/music_pause`：暫停目前播放的歌曲
- `/music_resume`：繼續播放被暫停的歌曲
- `/music_skip`：跳過目前歌曲並播放佇列中的下一首
- `/music_seek <time>`：跳轉到指定時間（支援秒數、`mm:ss`、`hh:mm:ss`，或 `+10`／`-15` 相對秒數）
- `/music_stop`：停止播放並清空佇列（需全員投票同意）
- `/music_set_channel [channel]`：設定點歌頻道，音樂指令只在該頻道回應；不帶參數則取消限制。需「管理伺服器」權限
- `/music_node_status`：查看目前 Lavalink 節點的連線狀態（包含節點 URI、連線狀態、Session ID 及目前連線的伺服器數）
- `/auto_reply_add`：新增關鍵字自動回覆規則（規則名稱、偵測關鍵字、機器人回覆內容、生效範圍）。需「管理伺服器」權限
- `/auto_reply_edit`：編輯自動回覆規則（輸入框編輯關鍵字／回覆內容，留空＝刪除該項；也能暫停或恢復整條規則）。需「管理伺服器」權限
- `/auto_reply_list`：列出所有自動回覆規則（一頁一條，可翻頁：規則名稱、狀態、關鍵字、回覆內容、生效範圍）。需「管理伺服器」權限
- `/auto_reply_remove`：刪除自動回覆規則（從已生效的偵測關鍵字選單中選取，刪除前需確認）。需「管理伺服器」權限

#### 點歌者操作鎖

`/music_pause`、`/music_resume`、`/music_skip`、`/music_stop`、`/music_seek` 五個指令受到「點歌者操作鎖」保護：

- 當前曲目的點歌者若還在機器人所在的語音頻道，僅點歌者本人能直接操作；點歌者退出語音頻道後，任何人都能操作。
- **暫停／續播**：非點歌者使用時，機器人會在原文字頻道公開發送請求，附「✅ 允許／❌ 拒絕」按鈕，只有點歌者能按；按鈕為一次性（決定後即失效），30 秒內未決定視為拒絕。
- **停止**：任何人使用都會發起全員投票，同意／不同意按鈕即時顯示人數；任何一票不同意即否決，全員同意才會停止，按鈕同樣 30 秒有效。頻道裡只剩自己時不用投票、直接執行。投票被否決後，同一首歌在下一首開始播放前無法再次發起投票。
- **拒絕鎖**：同一首歌的操作請求被拒絕或逾時後（含暫停／續播按鈕請求，以及被否決或逾時結算為否決的停止投票），在下一首開始播放前不得再次提出或發起；換歌後自動解鎖。

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
- waitress
- certifi
- aiohttp
- wavelink

`wavelink` 已包含在 `requirements.txt` 中，執行 `pip install -r requirements.txt` 即可一併安裝。音樂功能另外需要準備可連線的 Lavalink 節點；未設定節點時，其他功能仍可正常啟動，但 `/music_join`、`/music_leave`、`/music_play` 系列指令無法使用。

網頁儀表板改以 production WSGI server `waitress` 啟動（已加入 `requirements.txt`），監聽 `0.0.0.0` 與 `PORT` 環境變數（未設定時預設 `20198`）。

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
- `/steal emoji:<表情符號字串或引用>`：從 Discord 的表情符號 CDN 直接下載，並建立為本伺服器自訂表情；支援多個 emoji 一次處理，超過 256 KB 會回覆友善提示，不會直接顯示原始錯誤代碼
- `/weather city:<縣市名稱或英文>`：查詢天氣，支援如 `臺北`、`Taichung`、`Matsu` 等對照
- `/bus`：先選擇縣市，再輸入公車號碼，接著從下拉式選單選擇站牌並查詢即時到站資訊
- `/server_info`：顯示所在伺服器的詳細資訊
- `/welcome_active welcome_channel:<頻道> [rules_channel:<頻道>] [role_channel:<頻道>]`：啟用歡迎訊息，設定歡迎頻道（必填）、規則頻道（選填）、身份組頻道（選填）。需要「管理伺服器」權限。
- `/welcome_inactive`：停用本伺服器的歡迎訊息功能。需要「管理伺服器」權限。
- `/reaction_roles message:<文字> pairs:<表符 身份組 表符 身份組 …> [channel:<頻道>] [strict:<開/關>]`：發送表符身份組訊息（表符＋身份組一對一、以空白分隔，最多 20 組），未指定頻道時自動使用 `/welcome_active` 設定的身份組領取頻道；發送後機器人會自動按下所有表符，成員按下表符獲得身份組、取消表符則收回。每個伺服器可同時保留多則領取訊息（上限 10 則）。`strict` 預設開啟，會自動移除亂按的其他表符（需機器人在該頻道有「管理訊息」權限）。需要「管理伺服器」權限。
- `/music_join`：機器人加入你目前所在的語音頻道；需要已安裝 `wavelink` 並設定 Lavalink。
- `/music_leave`：機器人離開目前所在的語音頻道，並清空該伺服器的播放佇列。
- `/music_play query:<關鍵字或網址>`：搜尋並播放音樂；若機器人尚未加入語音頻道會自動加入。若目前已經有歌曲在播放（或暫停中），新點的歌會依序排入佇列（FIFO），而不是直接取代。
- `/music_play_next query:<關鍵字或網址>`：插播。跟 `/music_play` 一樣會搜尋歌曲，但會插入佇列最前面，目前這首播完後會優先播放插播的歌曲。
- `/music_queue`：顯示目前伺服器正在播放的歌曲，以及接下來排隊中的清單（最多列出前 10 首）。
- `/music_queue_clear`：清空目前伺服器的播放佇列，正在播放中的歌曲不受影響。
- `/music_node_status`：查看目前所有已註冊 Lavalink 節點的連線狀態（URI、連線狀態、Session ID、目前連線的伺服器數）；僅使用者可見。
- `/auto_reply_add keyword:<關鍵字> reply:<回覆內容> [name:<規則名稱>] [channel:<頻道>]`：新增自動回覆規則。成員訊息「包含」關鍵字（不分大小寫、忽略多餘空白）時，機器人會引用該訊息自動回覆；未指定 `channel` 時整個伺服器生效；未指定 `name` 時以關鍵字作為規則名稱。每個伺服器上限 10 條。需要「管理伺服器」權限。
- `/auto_reply_edit`：編輯自動回覆規則。送出後自動抓取目前所有規則的**規則名稱**製成下拉選單供選取（舊規則退回顯示偵測關鍵字）；選取後有四顆按鈕：「編輯關鍵字」、「編輯回覆內容」、「⏸️ 暫停規則」（暫停後變成「▶️ 恢復規則」）、「取消」。兩個編輯視窗都是每頁 5 個輸入框（Discord 上限），預先帶入現有內容，**留空＝刪除該項**；超過 5 項時存檔訊息上會附「繼續編輯第 6–10 個」按鈕。回覆超過一則時自動切換成**隨機回覆模式**（每次觸發從所有回覆中隨機挑選一則）。需要「管理伺服器」權限。
- `/auto_reply_list`：快速檢視目前的自動回覆規則。**一頁一條規則**（用「◀️ 上一頁／下一頁 ▶️」兩顆按鈕翻頁），完整顯示規則名稱、狀態、全部關鍵字、回覆內容與生效範圍；單則回覆超過 300 字時會截斷，內容真的塞不下時會標示省略並提示用 `/auto_reply_edit` 查看完整內容。沒有任何規則時會提示先用 `/auto_reply_add` 新增。需要「管理伺服器」權限。
- `/auto_reply_remove`：刪除自動回覆規則。送出後自動抓取目前**已生效**的偵測關鍵字製成下拉選單供選取，選取後先顯示確認訊息（附「確認刪除／取消」按鈕，只有執行指令的人能按）。需要「管理伺服器」權限。

### 表符身份組功能說明

`/reaction_roles` 用來發送「按表符領身份組」的訊息：機器人送出訊息後會自己先把所有指定的表符按一輪，成員按下表符即可獲得對應身份組，取消表符則自動收回；每個伺服器可同時保留多則領取訊息（上限 10 則，達上限時會提示先刪除舊訊息），設定儲存於 `reaction_roles.json`，重啟後不會遺失（舊的單則訊息格式會自動遷移）。

**防止亂按（僅限有效表符）**：`strict` 預設為開啟。成員在領取訊息上按了**不對應任何身份組**的表符時，機器人會自動移除該反應，只留下能換到身份組的表符。機器人替別人移除反應需要該頻道的「管理訊息」權限；發送時若權限不足會直接列出缺少的權限並中止。權限不足、訊息已被刪除等情況只會在主控台留下紀錄、不影響其他功能。若希望保留其他反應（例如當成留言板），發送時把 `strict` 關掉即可。舊的領取訊息（設定檔裡沒有 `strict` 欄位）一律視為開啟。

**反應限速**：成員對「同一個表符」的發放／收回動作最快每 5 秒生效一次；冷卻期間內重複按／取消同一個表符會被靜默忽略（不同表符、不同成員互不影響），避免高頻點擊洗身份組。移除無效表符也套用同一套冷卻，連續狂按同一個無效表符只會移除一次。冷卻狀態儲存於 `reaction_roles_cooldowns.json`，重啟後仍有效。

`pairs` 參數的拆分規則：

- 以**空白分隔**的自動拆分：`pairs` 會先依空白切成 token，依序「表符 → 身份組 → 表符 → 身份組…」兩兩一組解析，例如：`🎉 @帥 🎮 @打電動`。
- 表符支援 Unicode 表符與自訂表符代碼（`<:name:id>`、`<a:name:id>`，需真實的 13～20 位 ID）；純文字（如英文單字）不會被誤判成表符，中文身份組名稱可以安全使用。
- 身份組支援三種寫法：@提及（`<@&id>`）、純數字 ID、身份組名稱（不分大小寫）。因為名稱本身不能含空白，**含空白的名稱請改用 @提及或 ID**。
- 同一個表符只能對應一個身份組；單一訊息最多 20 組（Discord 反應數上限）。
- 解析失敗時會停在第一個無法解析的位置並回覆中文錯誤訊息（指出第 N 組與原因：無法辨識的表符、表符重複、缺少身份組、找不到身份組、超過上限），先前已成功解析的配對仍會保留在錯誤訊息外的檢查流程中；任何錯誤存在時都不會發送訊息。

### 自動回覆功能說明

`/auto_reply_add` 用來新增「關鍵字自動回覆」規則，四個要素：

- **規則名稱**：選填；之後 `/auto_reply_edit`、`/auto_reply_remove` 的下拉選單以此辨識規則（最多 40 字，不填時以關鍵字作為名稱）。
- **偵測的關鍵字**：成員訊息內容「包含」此關鍵字即觸發（不分大小寫、比對前會壓縮連續空白），最多 60 字。一條規則可以有多個關鍵字（上限 10 個），**命中任一個即會觸發**，之後可用 `/auto_reply_edit` 在輸入框中自行增修。
- **機器人回覆內容**：觸發後機器人會引用觸發訊息並回覆這段文字（一律不 @ 任何人），最多 1500 字。
- **生效範圍**：可指定單一文字頻道，或整個伺服器（不填 `channel` 時）。

每個伺服器最多同時保留 **10 條**規則（達上限時會提示先刪除舊規則）；一則訊息最多只會觸發第一條命中的規則；同一個成員對同一條規則 3 秒內重複觸發不會重複回覆（防洗版）。設定儲存於 `auto_reply.json`，重啟後不會遺失（舊的單一關鍵字／單一回覆格式會自動遷移成關鍵字清單與多回覆清單）。

**隨機回覆模式**：一條規則可以有多則回覆內容（上限 10 則）；當規則有超過一則回覆時，每次觸發會從所有回覆中**隨機挑選一則**回覆。

`/auto_reply_edit` 用來編輯現有規則：送出後機器人會自動抓取目前所有規則的**規則名稱**製成下拉選單（舊規則退回顯示偵測關鍵字）；選取規則後會顯示規則詳情與四顆按鈕——「編輯關鍵字」、「編輯回覆內容」、「⏸️ 暫停規則」、「取消」：

- **編輯關鍵字**／**編輯回覆內容**：彈出視窗，每個輸入框對應清單中的一項並預先帶入現有內容，**留空＝刪除該項**、往空格補字＝新增一項，整份清單一次儲存（沿用空白壓縮、60／1500 字與 10 項的上限檢查，重複關鍵字會自動略過）。
- **分頁**：Discord 每個彈出視窗最多只能放 5 個輸入框，所以清單的第 1–5 項在第一頁、第 6–10 項在第二頁；存檔訊息上會附「繼續編輯第 6-10 個」按鈕，需要時才開下一頁。**沒有編到的那一頁不會被動到**。
- **全空會追問**：若關鍵字或回覆內容**每一格都留空**，機器人不會直接存成空清單，而是跳出「是否要刪除整條規則？」的確認訊息（附「🗑️ 確認刪除／取消」，按取消則規則完全不動）。
- **⏸️ 暫停規則**：按下即暫停這條規則，**不會刪掉任何設定**（關鍵字、回覆內容、生效範圍都保留），只是讓它暫時不再回覆；暫停後按鈕會變成「▶️ 恢復規則」，再按一次就恢復，主訊息的規則詳情也會同步更新狀態。
- 回覆刪到只剩一則時，會自動回到固定回覆模式（不再隨機挑選）。

編輯操作只有執行指令的人能按，按鈕可連續使用（例如先改關鍵字再改回覆），180 秒沒有操作才自動失效；儲存失敗時會回滾變更並回報開發者。

`/auto_reply_list` 用來快速檢視目前的設定：**一頁一條規則**，用「◀️ 上一頁／下一頁 ▶️」翻頁，每頁完整顯示該條規則的規則名稱、狀態、全部關鍵字、回覆內容與生效範圍。因為不用把所有規則塞進同一則訊息，單則回覆可以顯示到 300 字；真的超出 embed 字數上限時會截斷並標示省略。

被暫停的規則不會被 `on_message` 偵測，但其他規則不受影響，一則訊息仍最多只觸發第一條命中的**啟用中**規則。

`/auto_reply_remove` 送出後，機器人會自動抓取目前**已生效**的規則製成下拉選單供管理員選取；選取後會先發送確認訊息（顯示該規則的名稱、關鍵字、回覆內容與生效範圍），附「🗑️ 確認刪除／取消」兩顆按鈕，按確認才會真的刪除；只有執行指令的人能操作這些按鈕。

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

- `/weather` 會呼叫中央氣象署公開資料 API，全程啟用 TLS 憑證驗證（沿用 `cogs/tls.py` 的共用 SSL context，相容舊式政府 CA 憑證鏈）；若憑證驗證失敗會直接回報連線異常，不會關閉驗證重試。
- `/bus` 會呼叫交通部 TDX API 查詢公車路線站點與即時到站資訊。所有公車查詢訊息皆為僅使用者可見，避免干擾頻道版面。
- `/bus` 若輸入不存在的公車號碼，會提示找不到站牌或到站資料；若發生未知錯誤，會自動嘗試 DM 通知 `DISCORD_OWNER_ID`。
- `/music_join`、`/music_leave`、`/music_play`、`/music_play_next`、`/music_queue`、`/music_queue_clear`、`/music_node_status` 需要已安裝 `wavelink` 並設定 `LAVALINK_URI` / `LAVALINK_PASSWORD`；未安裝或節點無法連線時，這些指令會回覆友善錯誤訊息，不影響機器人其他功能。
- Music Cog 啟動後會在背景以 `LAVALINK_CONNECT_TIMEOUT`（預設 15 秒）為逾時限制嘗試連線 Lavalink 節點（非同步，不阻擋機器人其他功能啟動）；連線成功後，每隔 `NODE_HEALTH_CHECK_INTERVAL`（預設 30 秒）定期檢查節點狀態，節點離線時 DM 通知 `DISCORD_OWNER_ID` 一次，恢復連線後重置旗標，避免重複通知。
- 播放佇列是各伺服器獨立的 FIFO（先進先出）佇列，實作於 `cogs/music.py` 的 `player.song_queue`（`collections.deque`）。歌曲播完（或被跳過、發生錯誤）時會觸發 wavelink 的 `on_wavelink_track_end` 事件，由監聽器從佇列最前面取出下一首並自動播放；佇列空了則會發一次通知。`player.autoplay` 因此設為 `disabled`，改由這個監聽器完全接管「播完接下一首」的邏輯，避免跟 wavelink 內建的 autoplay 互相搶播。
- 語音頻道裡只剩機器人自己（沒有真人成員）超過 `EMPTY_VOICE_CHANNEL_TIMEOUT` 秒（預設 60 秒）就會自動離開並清空佇列，並在最近一次下指令的文字頻道發一則通知。這個功能監聽 `discord.py` 的 `on_voice_state_update` 事件，每次有人加入/離開/切換頻道時檢查機器人所在頻道還有沒有真人；沒有的話才啟動倒數計時器，且倒數期間只要有人回來就會立刻取消，避免誤判暫時性的斷線重連。
- `/music_play` 搜尋失敗（來源網站無回應、反爬蟲封鎖等）會立即回覆錯誤訊息。歌曲成功排入播放後，若 Lavalink 節點在背景載入音訊時才失敗（例如 YouTube 判定需要登入、影片地區限制，或公開節點的來源連結失效），機器人會透過 `on_wavelink_track_exception` 監聽器把失敗原因回報到下指令當下的文字頻道，而不是只留在後台 log。目前公開 Lavalink 節點偶爾不穩定是已知風險，後續計畫改為自架節點。
- `/steal` 會解析 Discord 的 emoji 引用字串（如 `<:pepe_smile:123456789>`、`<a:cat:456789>`），依據 `emoji_id` 及是否動態組出對應 CDN 下載網址，下載圖片 bytes 後透過 `guild.create_custom_emoji()` 建立到目前伺服器；若來源超過 256 KB 或無法讀取，會回覆友善文字提示，而不是直接曝露原始錯誤代碼。
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
- `/steal emoji:<emoji>`: download a Discord emoji reference or mention directly from the CDN and add it to the current guild as a custom emoji; supports multiple emoji in one call and replies with a friendly message when a source exceeds the 256 KB Discord limit
- `/weather <city>`: query real-time weather for Taiwan cities
- `/bus`: query Taiwan bus arrivals through dropdown menus
- `/server_info`: display detailed server information
- `/welcome_active`: set up a server welcome message (welcome channel required; rules and role channels optional)
- `/welcome_inactive`: disable the server welcome message feature
- `/reaction_roles`: post a reaction-role message with emoji + role pairs (multiple messages per server); members gain the role by reacting and lose it when un-reacting, and reactions that grant nothing are removed automatically. Requires **Manage Server** permission
- `/music_join`: join the voice channel where the user is currently connected
- `/music_leave`: leave the current voice channel
- `/music_play <query>`: search and play music; automatically queues the track if something is already playing (accepts keywords, or a YouTube/SoundCloud URL)
- `/music_play_next <query>`: play next (jump the queue), search a track and insert it at the front of the queue
- `/music_queue`: show the current server's playback queue (now playing + upcoming tracks)
- `/music_queue_clear`: clear the current playback queue (does not affect the currently playing track)
- `/music_pause`: pause the currently playing track
- `/music_resume`: resume a paused track
- `/music_skip`: skip the current track and play the next one in the queue
- `/music_seek <time>`: seek to a position (seconds, `mm:ss`, `hh:mm:ss`, or relative `+10` / `-15`)
- `/music_stop`: stop playback and clear the queue (requires a group vote)
- `/music_set_channel [channel]`: restrict music commands to a single text channel; run without arguments to lift the restriction. Requires **Manage Server** permission
- `/music_node_status`: view the connection status of all registered Lavalink nodes (URI, status, Session ID, and connected guild count)

#### Requester control lock

The five commands `/music_pause`, `/music_resume`, `/music_skip`, `/music_stop`, and `/music_seek` are protected by a requester control lock:

- While the current track's requester stays in the bot's voice channel, only they can control that track; once the requester leaves the voice channel, anyone may control it.
- **Pause/Resume**: when used by someone else, the bot posts a public request in the original text channel with "✅ Allow / ❌ Deny" buttons. Only the requester can press them; the buttons are one-shot (disabled once decided) and unanswered requests are denied after 30 seconds.
- **Stop**: any use starts a group vote with agree/disagree buttons showing live tallies; a single disagreement vetoes the request, and only unanimous agreement stops playback. Buttons are valid for 30 seconds. If the channel only has one human, the command executes directly without a vote. Once vetoed, no new stop vote can be started for that track until the next one begins.
- **Rejection lock**: once a request for a track is denied (or times out) — including pause/resume button requests and a vetoed (or timed-out-as-denied) stop vote — no new request or vote can be started for that track until the next one starts playing; the lock clears when the track changes.

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
- waitress
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
- `/steal emoji:<emoji string or reference>`: parse a Discord emoji string, download the asset from the CDN, and create it as a custom emoji in the current server; supports multiple emoji at once and prevents failed uploads from exposing raw HTTP error codes by returning a friendly message when the asset is too large
- `/weather city:<city name or English name>`: query weather, supports mappings like `臺北`, `Taichung`, `Matsu`
- `/bus`: select a city, enter a bus route, choose a stop from a dropdown menu, and view real-time arrival information
- `/server_info`: display the current server's details
- `/welcome_active welcome_channel:<channel> [rules_channel:<channel>] [role_channel:<channel>]`: enable welcome messages with a required welcome channel and optional rules/role channels. Requires **Manage Server** permission.
- `/welcome_inactive`: disable welcome messages for this server. Requires **Manage Server** permission.
- `/reaction_roles message:<text> pairs:<emoji role emoji role ...> [channel:<channel>] [strict:<true/false>]`: post a reaction-role message to the role channel configured via `/welcome_active` (or the given channel). Emoji + role pairs are space-separated, one-to-one, up to 20 pairs; the bot reacts with every emoji first, and members gain or lose the matching role as they add or remove reactions. Each server keeps up to 10 reaction-role messages at once. `strict` is on by default and auto-removes reactions that grant no role (the bot needs **Manage Messages** in that channel). Requires **Manage Server** permission.
- `/music_join`: join the user's current voice channel. Requires `wavelink` and a configured Lavalink node.
- `/music_leave`: leave the current voice channel, and clear that server's playback queue.
- `/music_play query:<keywords or URL>`: search and play music; auto-joins your voice channel if the bot isn't connected yet. If something is already playing (or paused), the new track is appended to the FIFO queue instead of replacing it.
- `/music_play_next query:<keywords or URL>`: play next / jump the queue. Same search as `/music_play`, but the track is inserted at the front of the queue and plays right after the current one.
- `/music_queue`: show what's currently playing plus the upcoming tracks in the queue (up to the first 10).
- `/music_queue_clear`: clear the current server's queue. The currently playing track is not affected.
- `/music_node_status`: view the connection status of all registered Lavalink nodes (URL, connection state, Session ID, connected guild count). Ephemeral (only visible to you).

### Reaction Roles Feature

`/reaction_roles` posts a reaction-role message: after sending it, the bot reacts with every configured emoji first, so members can gain the matching role by reacting and lose it when the reaction is removed. Each server can keep multiple reaction-role messages at once (up to 10; the command asks you to remove old ones when full). Settings are stored in `reaction_roles.json` and persist across restarts (the old single-message format is migrated automatically).

**Only valid emojis (strict mode)**: `strict` is on by default. When a member reacts with an emoji that maps to no role, the bot removes that reaction so only role-granting emojis stay. Removing someone else's reaction requires **Manage Messages** in that channel; the command refuses to send and lists the missing permission if the bot lacks it. Missing permissions, a deleted message, or other failures are only logged in the console and never break the other features. Pass `strict: false` to keep free-form reactions (e.g. to use the message as a comment board). Reaction-role messages stored before this feature (no `strict` key in the settings file) are treated as strict.

**Reaction rate limit**: per (member, emoji) pair, a grant/revoke takes effect at most once every 5 seconds; repeated clicks/un-reacts on the same emoji during the cooldown are silently ignored (other emojis and other members are unaffected), preventing role spam through rapid toggling. Invalid-emoji cleanup shares the same cooldown, so spamming one invalid emoji only triggers a single removal. Cooldown state is stored in `reaction_roles_cooldowns.json` and survives restarts.

How the `pairs` parameter is parsed:

- **Space-separated auto-splitting**: the string is split on whitespace into tokens, then consumed two at a time (emoji → role → emoji → role ...), e.g. `🎉 @Mods 🎮 @Gamer`.
- Both Unicode emoji and custom emoji codes (`<:name:id>`, `<a:name:id>`, with a real 13-20 digit ID) are accepted; plain text (e.g. English words) is never mistaken for an emoji, and CJK role names are safe to use.
- Roles can be given three ways: an @mention (`<@&id>`), a numeric ID, or a role name (case-insensitive). Since names cannot contain spaces, **use a mention or ID for names with spaces**.
- Each emoji may map to only one role; a single message supports up to 20 pairs (Discord's reaction limit).
- On a parse failure the parser stops at the first unresolvable token and replies with a Chinese error message (pair number and reason: unrecognized emoji, duplicate emoji, missing role, role not found, or over the limit); successfully parsed pairs up to that point are still kept for the remaining validation. No message is sent if any error exists.

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

- `/weather` calls the Taiwan Central Weather Administration API with TLS certificate verification always enabled (via the shared SSL context in `cogs/tls.py`, compatible with legacy government CA chains); if certificate verification fails, it reports a connection error instead of retrying with verification disabled.
- `/bus` calls the Taiwan TDX API for route stops and real-time arrival estimates. Bus query messages are ephemeral, so only the user who started the query can see them.
- `/bus` handles unknown route numbers with a clear not-found message. Unexpected errors trigger an owner DM when `DISCORD_OWNER_ID` is configured.
- `/music_join`, `/music_leave`, `/music_play`, `/music_play_next`, `/music_queue`, `/music_queue_clear`, and `/music_node_status` use Wavelink and require a reachable Lavalink node configured with `LAVALINK_URI` and `LAVALINK_PASSWORD`. The music Cog is loaded without stopping the bot when Wavelink or Lavalink is unavailable.
- On startup, the Music Cog connects to Lavalink in the background within `LAVALINK_CONNECT_TIMEOUT` seconds (default 15) without blocking other bot features. Once connected, a health-check loop runs every `NODE_HEALTH_CHECK_INTERVAL` seconds (default 30), DMing the owner once when a node goes offline and resetting the flag upon recovery to avoid repeated notifications.
- The playback queue is a per-server FIFO queue, implemented as `player.song_queue` (a `collections.deque`) in `cogs/music.py`. When a track ends (finishes, is skipped, or errors out), wavelink fires `on_wavelink_track_end`; the listener pops the next track off the front of the queue and plays it automatically, and sends a one-time notice once the queue is empty. `player.autoplay` is set to `disabled` so this listener fully owns the "advance to next track" logic instead of racing with wavelink's built-in autoplay.
- If a voice channel is left with only the bot (no human members) for more than `EMPTY_VOICE_CHANNEL_TIMEOUT` seconds (default 60), the bot automatically leaves and clears its queue, posting a notice to the text channel where it was last used. This listens to discord.py's `on_voice_state_update` event, checking the bot's channel every time someone joins, leaves, or switches channels; the countdown only starts once no humans remain, and is cancelled immediately if someone comes back, to avoid false positives from brief disconnects/reconnects.
- `/music_play` reports search failures (source unreachable, anti-bot blocking, etc.) immediately. If a track is accepted but later fails to load in the background (e.g. YouTube requiring login, region restrictions, or a broken stream link on a public node), the bot reports the failure to the text channel where the command was last used via the `on_wavelink_track_exception` listener, instead of only logging it. Public Lavalink node instability is a known risk here; self-hosting a node is planned for later weeks.
- `/steal` parses Discord emoji references such as `<:pepe_smile:123456789>` and `<a:cat:456789>`, builds the correct CDN path from the extracted `emoji_id`, and creates custom emoji in the current guild via `guild.create_custom_emoji()`. When a source exceeds Discord's 256 KB limit or cannot be fetched, it returns clear user-facing text instead of leaking the raw API error payload.
- The bot syncs global slash commands in `setup_hook`. If that fails, it retries in `on_ready` as a fallback.
- `cogs/web_server.py` runs a background Flask web service serving a status dashboard (`templates/index.html`) and the `/api/bot-stats` and `/api/uptime` endpoints.

### Tips

- To add a command, create or update a Cog under `cogs/`, then add its module path to `INITIAL_EXTENSIONS` in `main.py`.
- For cloud deployment, ensure the `PORT` environment variable or default port `20198` is accessible. The dashboard runs on the `waitress` production WSGI server (included in `requirements.txt`).
