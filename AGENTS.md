# AGENTS.md — 專案架構、運作原理與修改原則指南

> 本文件專為接手、維護或協同開發本專案的 AI Agents 與工程師設計。在進行任何代碼變更、架構調整或功能擴充前，請**務必完整閱讀並嚴格遵守**本文件所列之規範。

---

## 1. 專案核心定位與技術堆疊

本專案為「自主學習計畫」所開發之現代化、多功能 Discord 音樂與伺服器社群管理機器人，並結合獨立的 Web 控制面板（Web Panel）與個人主頁（User Profile）系統。

### 核心技術堆疊
- **執行環境**：Python 3.11+ / Python 3.14 相容，支援 Windows 本機開發與 Linux (Pterodactyl / Docker) 容器化部署。
- **Discord Bot 核心**：`discord.py` 2.4+
  - **100% 斜線指令（Application Commands / Slash Commands）**，完全不使用傳統前綴指令（Prefix Commands）。
  - 全面導入 Discord 最新 **UI Components V2（Container、Section、Accessory Thumbnail、ActionRow 按鈕）**。
- **音訊與音樂串流**：`wavelink` 3.x + **Lavalink v4** 節點。
- **Web 伺服器與 API**：`Flask` + `Jinja2` 模板引擎，以獨立背景執行緒（`threading.Thread`）運作，支援平台存活檢查（Health Check）。
- **資料持久層**：`SQLite`（單一資料庫 `bot_database.db`），強制啟用 **WAL 模式（Write-Ahead Logging）**、`NORMAL` 同步與外鍵限制，並使用全域重入鎖（`threading.RLock`）保證執行緒安全。
- **前端架構**：原生 JavaScript（Vanilla JS）+ CSS3 客製化深色玻璃擬態（Glassmorphism）設計系統。
- **外部分析與網路請求**：`aiohttp`、`requests`、Spotify Web API、交通部 TDX 公車 API、中央氣象署 CWA API、OpenRouter AI API、Google TTS (gTTS)。

---

## 2. 專案目錄結構樹

```text
自主學習/
├── main.py                     # 機器人啟動入口、全域事件監聽、Cog 載入與全域錯誤分派
├── bot_database.db             # 核心 SQLite 資料庫檔案（WAL 模式）
├── requirements.txt            # Python 套件依賴清單
├── README.md                   # 繁體中文使用者與功能說明手冊
├── README.en.md                # 英文版功能說明
├── AGENTS.md                   # 本文件：Agent 協同開發與修改原則指南
├── week7_devlog.md             # 專案開發進度紀錄日誌
│
├── cogs/                       # 功能模組目錄 (每個功能獨立為 Cog)
│   ├── ai.py                   # /ai, /ai_config 智慧對話、頻道專屬人設與記憶
│   ├── auto_reply.py           # /auto_reply_* 關鍵字自動回覆規則系統
│   ├── bus.py                  # /bus 台灣公車即時動態查詢
│   ├── database.py             # 核心資料庫層：所有 SQLite 表結構、CRUD 與資料遷移邏輯
│   ├── economy.py              # /economy_* 全域經濟、簽到加成、轉帳與娛樂小遊戲
│   ├── general.py              # /ping, /choice, /quotes, /steal, /reload_cogs
│   ├── llm_router.py           # 多模型 LLM 路由與 OpenRouter API 調度
│   ├── memory.py               # AI 短期上下文對話記憶管理
│   ├── music.py                # 音樂核心：Lavalink 播放器、Container 面板、歌單與置底機制
│   ├── panel_bp.py             # Flask Blueprint：Web 控制面板 RESTful API 與 OAuth2 驗證
│   ├── profile_bp.py           # Flask Blueprint：用戶個人主頁系統與權限管理
│   ├── reaction_roles.py       # /reaction_roles_* 表符領身分組系統（嚴格防亂按）
│   ├── sanitize.py             # 機密脫敏工具：Token、API Key、Webhook 過濾
│   ├── server_info.py          # /server_info 伺服器詳細資訊與權限面板
│   ├── shortener.py            # /shorten, /reurl 短網址產生器 (reurl.cc)
│   ├── table_renderer.py       # 表格渲染輔助工具
│   ├── temp_message.py         # /temp_message 自毀限時訊息與右鍵快捷選單
│   ├── tls.py                  # SSL/TLS 憑證驗證與安全 Context 建立
│   ├── tts.py                  # /tts Google 文字轉語音 (gTTS)
│   ├── weather.py              # /weather 台灣各縣市氣象即時查詢
│   ├── web_server.py           # Flask Web 伺服器本體、路由與健康監控端點
│   └── welcome.py              # /welcome_* 歡迎訊息設定、新成員自動賦予身分組
│
├── services/                   # 外部第三方 API 封裝服務
│   └── spotify.py              # Spotify 外部歌單/專輯/單曲非同步解析服務
│
├── templates/                  # Flask Jinja2 前端網頁模板
│   ├── index.html              # 官方首頁儀表板
│   ├── panel.html              # 伺服器控制面板與個人設定專區
│   ├── profile.html            # 個人公開主頁與個人資料編輯
│   ├── docs.html               # 指令線上文件頁面
│   ├── terms.html              # 服務條款
│   ├── privacy.html            # 隱私權政策
│   └── _site_shell.html        # 共用頂部導航與抽屜選單框架
│
├── static/                     # 前端靜態資源
│   ├── css/
│   │   ├── common.css          # 全站基礎樣式、Glassmorphism 變數、按鈕、下拉選單
│   │   ├── panel.css           # 控制面板專屬樣式（包含個人歌單編輯器）
│   │   └── profile.css         # 個人主頁專屬樣式
│   └── js/
│       ├── panel.js            # 控制面板前端互動邏輯（AJAX、拖曳排序、彈框）
│       └── profile.js          # 個人主頁前端互動邏輯
│
└── tools/                      # 離線單元測試與效能驗證腳本
    ├── test_music_import.py    # 音樂核心、Container 面板、歌單與置底防抖測試 (125+ 測試)
    ├── test_auto_reply.py      # 自動回覆邏輯、冷卻與權限測試
    ├── test_panel.py           # Web 控制面板 API 整合測試
    ├── test_shortener.py       # 短網址模組測試
    └── test_spotify.py         # Spotify 解析離線模擬測試
```

---

## 3. 系統核心架構與運作原理

### 3.1 雙引擎驅動架構 (Dual-Engine Lifecycle)
1. **Discord Gateway 引擎**：
   - 位於主執行緒，由 `bot.run(TOKEN)` 驅動 `asyncio` 事件迴圈。
   - 負責處理 Discord WebSocket 訊息、Interaction 互動回呼、語音狀態監聽等。
2. **Web 伺服器引擎**：
   - 在 `main.py` 啟動前，透過 `threading.Thread(target=..., daemon=True)` 啟動 Flask 服務（預設埠 8080）。
   - 與 Bot 共享記憶體狀態（`app.config['BOT'] = bot`），提供無縫的 Web-Bot 雙向控制能力。
   - 提供 `/health` 存活檢查端點，供雲端 PaaS 平台進行健康探針偵測。

### 3.2 資料持久層與並發控制 (`cogs/database.py`)
- **單一入口規範**：專案嚴禁在個別 Cog 或 Route 內直接撰寫原始 SQL 或開啟新連線。所有資料庫表結構定義、查詢與異動，**必須統一收斂在 `cogs/database.py`**。
- **WAL 模式與安全鎖**：
  - 連線時一律執行 `PRAGMA journal_mode=WAL;`、`PRAGMA synchronous=NORMAL;`、`PRAGMA foreign_keys=ON;`。
  - 使用全域 `_lock = threading.RLock()` 進行讀寫保護，消除 Flask 執行緒池與 Discord `asyncio` 執行緒之間的 SQLite 寫入衝突（`database is locked`）。
- **資料庫遷移與相容性**：
  - 初始化函式 `init_db()` 具備自我修復與遷移能力，舊版 `.json` 設定檔會在首次啟動時自動遷移進 SQLite，若偵測到缺失欄位會自動執行 `ALTER TABLE` 補齊。
- **核心資料表一覽**：
  - `welcome_settings`：伺服器歡迎、規則、身分組領取頻道與新成員自動身分組 ID。
  - `music_settings`：各伺服器綁定之專屬點歌頻道 ID。
  - `auto_reply_rules`：關鍵字自動回覆規則、生效頻道與暫停狀態。
  - `reaction_roles_messages` & `reaction_roles_pairs`：表情符號身分組訊息與表情-身分組對應關係。
  - `economy_users` & `economy_config`：用戶全域金幣帳本、連續簽到紀錄、轉帳手續費與小遊戲賠率。
  - `ai_settings` & `ai_authorized_guilds`：頻道專屬人設、使用模型與伺服器 AI 授權白名單。
  - `user_profiles` & `profile_permissions`：用戶個人主頁自訂背景、自介、社交連結與編輯審核權限。
  - `user_playlists` & `user_saved_playlists`：每位用戶上限 1 個專屬歌單（最多 500 首，自動攔截直播串流）、6 碼跨服分享碼、公開權限與他人收藏清單。
  - `temp_messages`：限時自毀訊息追蹤（伺服器、頻道、訊息 ID 與到期時間）。

### 3.3 音樂核心系統 (`cogs/music.py`)
音樂系統為本專案技術密度最高之模組，核心機制包含：
1. **Lavalink & 自訂播放器**：
   - 繼承 `wavelink.Player` 實作自訂播放器，內部維護伺服器隔離之 `song_queue = deque()`。
   - 支援歌曲結束前預先解析下一首，實現無縫換曲體驗。
2. **點歌者操作鎖 (DJ Lock) 與投票否決 (Stop Vote)**：
   - 點歌者若仍在語音頻道內，暫停、續播、跳過、跳轉等動作受操作鎖保護；非點歌者發起操作時會彈出「同意/拒絕」確認按鈕由點歌者審核。
   - 停止播放（`/music_stop`）會自動發起全員投票，採「一票否決制」，全部在線聆聽者同意方可停止。
   - 拒絕鎖（Rejection Lock）：同一首歌的操作請求被拒絕或逾時後，在下一首歌播放前禁止重複發起。
3. **Container 視覺化控制面板**：
   - 採用 Discord 最新 UI Components V2：
     - 最外層為 `ui.Container`。
     - 頂部為 `ui.Section`，右上角配置 `accessory=ui.Thumbnail(artwork_url)` 小圖縮圖。
     - 封面縮圖透過 `get_track_artwork()` 自動從 YouTube 連結或 Video ID 推導（`https://i.ytimg.com/vi/{video_id}/hqdefault.jpg`），不消耗主機記憶體快取。
     - 包含 5 顆即時操作按鈕（暫停/續播、切歌、停止、音量調整、循環模式切換、隨機打亂按鈕）。
4. **全域進度條渲染背景迴圈**：
   - 單一全域 `@tasks.loop(seconds=5)` 輪詢所有播放中伺服器。
   - **三重節流防護**：(1) 播放器暫停時跳過；(2) 渲染後進度字串無變動時跳過；(3) 語音頻道僅剩 Bot 無真人時跳過。
   - 遭遇 Discord HTTP 429 Rate Limit 時自動依 `retry_after` 退避，狀態伺服器間嚴格隔離。
5. **動態防洗版自動置底 (Sticky Panel)**：
   - 當普通成員在點歌頻道發言時，為防面板被洗掉，觸發防抖定時器（Debounce Timer）。
   - 防抖延遲內若有新訊息持續湧入，直接順延時間戳，不中途粗暴 cancel 執行中任務。
   - 置底執行時，由 `_panel_locks[guild_id]` 加鎖，發送新面板後立即安全刪除舊面板訊息，杜絕雙重面板與幽靈訊息。
   - **嚴格自我排除**：記錄所有已發送面板的 Message ID（`_known_panel_msg_ids`）以及 `/music_queue` 公開佇列訊息，新版 discord.py 嚴格使用 `hasattr(message, "interaction_metadata")` 判定，徹底杜絕無限觸發迴圈與 `DeprecationWarning`。
6. **用戶個人專屬歌單 (Personal Playlist)**：
   - 每位用戶可建立 1 個專屬歌單（上限 500 首，自動攔截直播串流）。
   - 系統配發一組獨一無二的 6 碼英數跨服分享碼，可在任何伺服器輸入 6 碼播放。
   - 支援歌曲 URL 去重（重複加入直接忽略並提示）。
   - 支援隨機播放模式（Shuffle Mode）：打亂佇列順序但不影響當前播放曲目。
7. **Lavalink 斷線與直播自動復原**：
   - 自動捕捉 Lavalink 404 Session Lost，自動關閉失效 WebSocket 並在重新建立 Session 後重送語音狀態與斷點續播。
   - 直播串流（`is_stream=True`）長播斷流時自動重抓 URL 續播，防止直播中斷結束。

### 3.4 Web 控制面板與 API 架構 (`cogs/panel_bp.py`)
- **OAuth2 驗證**：支援 Discord 官方授權登入，亦具備本機開發專用的 `dev-login` 模式。
- **權限分級機制**：
  - **Bot Owner**：最高管理者，可存取 Cog 熱重載、全域指令同步、經濟參數全域調整、AI 授權伺服器名單、API 呼叫額度監視、個人主頁審核等。
  - **Guild Administrator**：擁有「管理伺服器」權限之成員，可設定歡迎頻道、點歌限制頻道、自動回覆規則、表符身分組等。
  - **一般已登入用戶**：可存取「個人設定」，全權管理自己的個人專屬歌單、編輯歌曲順序、更換公開狀態、重新產生分享碼與管理收藏歌單。
- **防禦設計**：
  - 關鍵修改端點均實作參數白名單校驗。
  - 刪除操作提供防呆確認機制。

---

## 4. 代碼修改原則與開發規範

在修改或新增本專案的任何代碼時，必須遵守以下六大原則：

### 原則一：斜線指令（Slash Commands）唯一性與權限雙層防護
1. **絕不使用前綴指令**：所有 Discord 指令必須是 `app_commands.command`，禁止使用 `@commands.command()`。
2. **權限雙層防護（缺一不可）**：
   ```python
   @app_commands.command(name="example", description="範例指令說明")
   @app_commands.default_permissions(manage_guild=True)  # Discord 客戶端選單隱藏
   @app_commands.checks.has_permissions(manage_guild=True)  # 伺服器端執行期真實檢驗
   async def example(self, interaction: discord.Interaction):
       ...
   ```
3. **耗時操作必加 Defer**：凡涉及外部 API 呼叫、音訊解析、資料庫批次操作等可能超過 2.5 秒的動作，一律在開頭使用：
   ```python
   await interaction.response.defer(thinking=True)  # 公開訊息
   # 或
   await interaction.response.defer(ephemeral=True)  # 私人訊息
   ```
   後續回應一律使用 `await interaction.followup.send(...)`。

### 原則二：統一錯誤處理與機密資訊脫敏
1. **錯誤回報規範**：所有未預期的非業務邏輯錯誤，一律呼叫：
   ```python
   await self.bot.notify_owner_error(e, interaction, extra_info="描述發生情境")
   ```
2. **機密脫敏（Redaction）**：印出至 console 或回報至 Discord 的文字，必須使用 `cogs.sanitize.redact_secrets(...)` 脫敏，嚴禁將 Token、金鑰、連線字串外洩。
3. **友善使用者提示**：給予使用者的錯誤訊息統一使用繁體中文，並以 emoji 前綴（❌ 失敗、⚠️ 警告、ℹ️ 提示、✅ 成功）。

### 原則三：資料庫操作規範與執行緒安全
1. **SQL 禁入業務層**：禁止在 `cogs/` 業務模組（如 `music.py`, `welcome.py`）或前端路由（`panel_bp.py`）中直接撰寫 `sqlite3.connect` 或 `cursor.execute`。所有操作必須封裝為 `cogs/database.py` 中的具名函式。
2. **執行緒鎖保護**：所有寫入與讀取關聯操作，必須包在 `with _lock:` 區塊內。
3. **無損平滑遷移**：修改資料表時，必須確保舊版資料庫檔案能直接相容啟動（使用 `CREATE TABLE IF NOT EXISTS`、`PRAGMA table_info` 檢查欄位並動態 `ALTER TABLE ADD COLUMN`）。

### 原則四：前端 UI 與樣式設計規範
1. **深色玻璃擬態（Glassmorphism）一致性**：
   - 遵循 `static/css/common.css` 中的 CSS 變數（如 `--bg-dark`, `--glass-bg`, `--primary: #38bdf8`, `--radius: 18px`）。
   - 頂部導航下拉選單中的「按鈕」與「超連結」必須擁有完全相同的內距、字體、圓角與 Hover 樣式，禁止出現預設瀏覽器邊框或灰色突兀區塊。
2. **輸入框防干擾**：所有新增、編輯、搜尋表單的文字輸入框，必須加入 `autocomplete="off"`，避免瀏覽器歷史紀錄下拉框遮擋操作介面。
3. **危險操作警戒樣式**：所有具備破壞性（刪除歌單、刪除規則、清空設定）的按鈕，必須具備 `.danger` 紅色警戒樣式，且點擊時必須彈出自訂二次確認彈框（Confirm Modal），禁止未確認直接刪除。
4. **精簡文案原則**：Avoid unnecessary secondary labels and descriptions. Let the layout do the explaining.（避免不必要的次要標籤與說明文字，讓版面配置本身說話。）
5. **未儲存變更統一提示規範（`#unsaved-changes-bar`）**：
   - 所有在 Web 控制面板（`templates/panel.html` / `static/js/panel.js`）中**必須點擊儲存按鈕才會寫入資料庫／生效的設定表單**（包含伺服器歡迎／音樂／AI 設定、擁有者全域／預設設定，以及個人歌單名稱與公開權限設定 `#form-update-personal-playlist` 等），一律必須接入底部懸浮「尚未儲存變更」提示列（`#unsaved-changes-bar`）。
   - 實作要求：
     1. 必須於 `savedFormState` 保存伺服器已儲存基準快照，並於表單 `input` / `change` 事件即時呼叫 `checkUnsavedChanges()` 比對差異；新增任何必須儲存才生效的新分頁或表單時，必須同步納入 `checkUnsavedChanges()`。
     2. 底部懸浮列之「儲存設定」（`#btn-unsaved-save`）與「重設」（`#btn-unsaved-reset`）必須完整支援所有模式（一般伺服器設定、擁有者模式、個人設定模式），於儲存成功後更新基準快照並收起提示列；於重設後還原為最後一次已儲存狀態。
     3. 即時寫入 API 的動作（如新增／刪除／排序歌曲、重新產生分享碼、確認彈框刪除）、純前端篩選輸入框（如 `#input-filter-playlist-tracks`）及初次建立表單（如 `#form-create-personal-playlist`），**不可**誤觸發未儲存提示列；且即時動作觸發畫面重繪時，若同分頁仍有未儲存的表單修改，必須保留該修改與未儲存狀態，不得靜默覆寫。
6. **非同步送出回饋與輸入法安全（IME-Safe Enter）**：
   - 單行文字輸入表單支援按 `Enter` 直接送出時，必須於 `keydown` 監聽中檢查 `event.isComposing || event.keyCode === 229` 以避免中文／日韓輸入法組字選字期間誤送出，確認非組字狀態後再呼叫 `event.preventDefault()` 並以 `requestSubmit()` 送出，防止瀏覽器雙重送出。
   - 涉及遠端解析或較長等待時間的非同步送出（如新增歌曲至個人歌單），必須在跳出成功／警告／失敗結果前先顯示「新增中／處理中...」提示（包含持久化載入 Toast、無障礙狀態區域與防連點按鈕鎖定），並於請求結束後清除載入提示再顯示最終結果。

### 原則五：Discord API 相容性與版本棄用防護
1. **Message Interaction 判定**：
   - 在 discord.py 2.4+ 中，`message.interaction` 已被棄用（會拋出 `DeprecationWarning`）。
   - 判定訊息來源時，**必須優先檢查 `hasattr(message, "interaction_metadata")`**，直接存取 `message.interaction_metadata`，僅在屬性不存在時（舊版環境）才退回 `getattr(message, "interaction", None)`。
2. **UI 元素不可巢狀傳入**：
   - `discord.ui.Container` 內部元件必須為一維列表，不可將 tuple 或二維陣列直接加入 `children`。

### 原則六：爆炸半徑控制（Blast Radius & Scope Control）
1. **嚴禁修改無關檔案**：除非任務明確要求，否則嚴禁變動未指定或無關聯的模組（如 `cogs/ai.py`、`cogs/auto_reply.py`、`cogs/temp_message.py`）。
2. **全域註解與繁體中文規範**：代碼中的註解、Docstring、錯誤提示與對外文件，必須嚴格維持高品質的**繁體中文（台灣習慣用語）**。

---

## 5. 開發、測試與驗證流程

### 5.1 測試腳本架構與執行
專案測試腳本位於 `tools/` 目錄，遵循以下架構原則：
1. **純離線與安全隔離**：
   - 測試必須完全使用 Fake 物件（如 `FakePlayer`、`FakeGuild`、`FakeMessage`）與 Mock（`AsyncMock`、`MagicMock`）。
   - 測試用的資料庫或檔案必須寫入 `tempfile.mkdtemp()` 暫存目錄，**絕對不可碰觸或污染真實的 `bot_database.db`**。
2. **執行核心單元測試**：
   ```bash
   python tools/test_music_import.py
   python tools/test_auto_reply.py
   python tools/test_panel.py
   ```
   測試通過標準：**所有測試項目必須 100% 通過（FAIL 數為 0）**。
3. 需要時使用: Use `agent-desktop` to see and operate desktop apps. Before the first use, run
`agent-desktop skills get desktop` and `agent-desktop skills get platform`, and follow them.

### 5.2 文件同步義務
當新增、修改或刪除任何斜線指令或重大功能時，必須同步更新以下文件：
1. `README.md`：更新中文版功能清單與指令說明章節。
2. `README.en.md`：更新英文版對應說明。
3. `templates/docs.html`：更新網頁版指令文件列表。

### 5.3 Git 提交規範 (Conventional Commits)
每一次代碼提交必須使用規範化前綴，並將不同改動項目分開提交，嚴禁將不相干的修復混雜於單一 Commit：
- `feat:` 新增功能（例如：`feat(music): 實作個人歌單隨機播放模式`）
- `fix:` 修復問題（例如：`fix(music): 消除 on_message 中 message.interaction 的 DeprecationWarning`）
- `refactor:` 重構代碼架構（未改變外部行為）
- `style:` 排版、樣式或 CSS/HTML 調整
- `docs:` 文件更新（例如：`docs: 新增 AGENTS.md 開發維護指南`）
- `test:` 測試腳本新增或調整
