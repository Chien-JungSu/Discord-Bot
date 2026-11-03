# 第 7 週開發日誌｜Spotify 歌單批次匯入與面板收尾機制修正

> 狀態說明：本週程式與離線測試已完成；**需要真實 Spotify API 的驗收項目尚未實測**（原因見「遇到的問題與排查」第 1 項：Development mode 要求 App 擁有者有 Premium）。

---

## 一、本週目標

1. 使用者在 `/music_play` 貼上 Spotify 歌單／專輯／單曲連結，機器人能讀取曲目資料，批次轉成可播放的曲目加入佇列，且**不阻塞 Gateway 事件迴圈**。
2. 第一首解析完立刻播放，其餘曲目在背景分批匯入，進度顯示在播放面板（Container）底部。
3. `/stop`、`/leave`、被踢出語音頻道、空房自動離開時，背景匯入要能取消並清理。
4. 一併修正第 6 週防洗版機制：播放結束後**拋棄舊 Container**，下次點歌建立新的；所有通知（播完、自動離開、失敗、操作結果）都寫在 Container 內，不再另外發送訊息。

---

## 二、完成項目

### 1. 新增 `services/spotify.py`（Spotify 服務）
- `parse_spotify_url()`：純函式，支援 `open.spotify.com/{playlist|album|track}/<id>`、`spotify:xxx:<id>`、`?si=` 參數、`intl-xx` 路徑與 `<>` 包裹；無法辨識回傳 `None`。
- `SpotifyClient`（Client Credentials Flow，全程 `aiohttp`、共用單一 `ClientSession`）：
  - access token 快取，到期前 60 秒更新，以 Lock 避免重複取得。
  - 分頁（歌單 100／專輯 50 首一頁，跟隨 `next`），逐頁只保留「曲名、第一位歌手、時長」。
  - HTTP 429：讀 `Retry-After` 等待重試，最多 3 次；要求等待超過 30 秒則直接回報。
  - 401／403／404 與 Premium 限制各有明確的繁體中文訊息。
  - 缺少 `SPOTIFY_CLIENT_ID`／`SPOTIFY_CLIENT_SECRET` 時 `enabled=False`，功能優雅停用，不影響 Bot 啟動。

### 2. `cogs/music.py`：批次匯入
- 新增可調常數：`IMPORT_CONCURRENCY=3`、`IMPORT_MAX_TRACKS=200`、`IMPORT_SEARCH_TIMEOUT=15`、`IMPORT_BATCH_SIZE=9`、`IMPORT_PROGRESS_INTERVAL>=5s`（皆可用環境變數覆寫）。
- `/music_play` 偵測 Spotify 連結 → 驗證 → `asyncio.create_task` 建立背景任務並立刻返回。
- 背景任務流程：取曲目 → 逐首嘗試到**第一首**解析成功就立刻播放 → 其餘分批（每批同時搜尋受 `Semaphore` 限制）→ 每批結果**依原順序**一次加入 `player.song_queue`。
- 任務參照存在 `self._import_tasks[guild_id]`，結束時由 done callback 只移除「自己」。
- 同伺服器同時只允許一個匯入；重複匯入被擋並提示。
- 取消入口：`/music_stop`（含投票通過）、面板停止鈕、`/music_leave`、`/music_queue_clear`、空房自動離開、Bot 被踢出／斷線、`cog_unload`。
- 結果回報：`✅ Spotify 匯入完成：成功 X 首／失敗 Y 首`（含截斷與略過提示），顯示在 Container 底部，15 秒後自動消失。

### 3. 面板收尾機制修正（第 6 週遺留問題）
- `_update_panel_ended()` 結束時**移除 `panel_messages` 記錄**：舊面板停在「已結束」最終狀態，下次點歌找不到記錄，就在頻道最下方送出全新 Container。
- 結束原因直接寫進最終面板：播完、停止（含操作者）、離開、空房自動離開、被踢出。
- 其餘公開通知改為寫入 Container 底部提示（`_notify_panel` / `_announce`）：自動播放失敗、Lavalink 載入失敗、`/music_pause`、`/music_resume`、`/music_skip`、`/music_loop`、`/music_seek`。指令的互動回應會被收掉，聊天頻道不會多出訊息。

---

## 三、技術決策與設計說明

### 1. 為何不用 spotipy
spotipy 是同步套件（底層為 `requests`），在 discord.py 的事件迴圈裡呼叫會阻塞 Gateway 心跳；改用 `run_in_executor` 又多一層執行緒開銷，在 0.1 CPU／512MB 環境不划算。實際只需要「取 token + 讀三種端點」，自行用 `aiohttp` 實作不到兩百行，還能完整控制 429、逾時與記憶體。

### 2. 為何用 `Semaphore`
每首歌都要向 Lavalink 搜尋一次。不限制並發會同時送出數百個請求，壓垮節點、也讓協程物件暴增。`Semaphore(3)` 讓同時搜尋數有硬上限；再搭配「分批」（每批 9 首才建立任務），避免一次替 200 首建立 200 個協程。

### 3. 為何背景分流
Interaction 必須 3 秒內回應，且匯入一份 100 首的歌單要花數十秒。`/music_play` 只做驗證與建立任務就返回，其餘指令（`/pause`、`/skip`）完全不受影響；第一首先播，使用者不必等整批。

### 4. 為何「整批全進或全不進」
批次解析完成後，加入佇列那一段**沒有任何 `await`**，因此任務在批次解析途中被取消時，已解析但尚未加入的曲目會被自然丟棄，不會留下半批資料，也不需要額外的清理旗標。

### 5. 匯入期間佇列暫空 ≠ 播放結束
第一首播完時，下一批可能還沒解析完。若 `on_wavelink_track_end` 此時把面板收尾，面板會被拋棄，匯入完成後也沒人接著播。因此：匯入進行中佇列空時不收尾；每批加入佇列後呼叫 `_start_if_idle` 接手播放；`player.advancing` 旗標避免與 `track_end` 同時呼叫 `play` 互相覆蓋。匯入結束時若播放器已閒置且佇列為空，再補上「結束」最終狀態。

### 6. 為何結束時拋棄 Container
舊做法結束後仍保留面板記錄，下次點歌是「編輯舊訊息」；若舊訊息已被聊天擠到很上面，使用者看不到。現在結束＝最終狀態＋移除記錄，點歌＝新訊息，永遠出現在最底部。

### 7. 進度更新節流
匯入進度沿用第 6 週機制：間隔至少 5 秒、尊重 429 退避記錄；進度字串寫入 notice 後透過既有 `_sync_panel` 編輯同一則訊息。

---

## 四、遇到的問題與排查

| 問題現象 | 根本原因 | 處置與解決方式 |
|---|---|---|
| **Spotify 歌單可能無法讀取（重大限制）** | 2026 年 2 月 Spotify 政策調整：Development mode App 的擁有者需有 Premium，否則所有 API 請求被拒；且歌單曲目端點僅對擁有者／協作者的使用者授權開放，Client Credentials 可能得到 403 | 依指示**不自行繞過**。程式遇到時回報明確訊息；專輯與單曲理論上可用 Client Credentials。實測需 Premium 帳號建立的 App 金鑰，目前尚未取得 |
| 重新點歌時仍沿用舊 Container | `_update_panel_ended` 結束後仍保留 `panel_messages` 記錄 | 結束時移除記錄；下次 `on_wavelink_track_start` 找不到記錄就送出新訊息 |
| 結束面板後 `interaction` 與記錄中的面板可能不是同一則 | 使用者可能按到較舊的面板按鈕 | `_sync_panel(final=True)`：處理完互動後，若記錄中的面板是另一則，也一併編輯成最終狀態 |
| 匯入期間第一首播完，面板被誤收尾 | 佇列暫時為空（下一批尚未解析完） | `track_end` 偵測到有匯入任務就不收尾；匯入任務自行 `_start_if_idle` 接手 |
| `track_end` 與匯入任務可能同時 `play()` | 兩邊都看到「播放器閒置」 | `player.advancing` 旗標，匯入任務遇到時不搶播 |
| 取消匯入後面板仍顯示「匯入中」 | 進度 notice 是持久提示，沒有自動清除 | `_cancel_import` 立即清除 `📥` 開頭的提示；任務 `finally` 再保險一次 |
| 在 Windows 沙盒內執行含 `asyncio.run` 的測試會卡住 | 沙盒阻擋事件迴圈建立（連 `asyncio.run(asyncio.sleep(0))` 都不會結束） | 測試腳本改在沙盒外執行（本機純測試、不連網）；程式本身不受影響 |
| 第 5 週 `tools/test_music_panel.py` 失敗 | 該測試仍假設「5 顆按鈕／單一 ActionRow」，第 6 週改為 Components V2 後結構已變 | 屬既有過時測試，非本週改動造成；第 6 週的 `test_music_progress.py` 已涵蓋新結構（未在本週修改） |

---

## 五、測試紀錄

### 1. 自動化測試（皆為離線，用假物件）
| 測試檔 | 結果 |
|---|---|
| `tools/test_spotify.py`（連結解析、token 快取、分頁、截斷、429 重試、錯誤訊息） | 42／42 通過 |
| `tools/test_music_import.py`（面板拋棄、匯入流程、Semaphore、逾時、取消、重複匯入、錯誤提示、track_end 保護） | 46／46 通過 |
| `tools/test_music_progress.py`（第 6 週回歸） | 87／87 通過 |
| `tools/test_music_lavalink.py`（回歸） | 50／50 通過 |

429 重試以假 session 模擬：`Retry-After: 2` 後等待 2 秒重試成功；連續 429 超過 3 次後放棄；`Retry-After: 3600` 不等待直接回報。

### 2. 壓力測試（離線）
`python tools/bench_import_memory.py 200 0.02`（200 首，單首模擬 20ms 搜尋延遲）：
- 全部 200 首依序進入播放／佇列，耗時 2.13 秒
- tracemalloc 峰值 +143 KiB，結束 +136 KiB（程式端物件，不含 discord.py 與 Lavalink）
- 事件迴圈最大額外延遲 15.3 ms（沒有阻塞）
- 殘留匯入任務 0

> 這是模擬數字。真實情況單首搜尋延遲更高、會經過網路，**需實機再量一次**。

### 3. 手動測試清單（需真實金鑰與 Lavalink，尚未執行）

**正常情境**
- [ ] 貼 Spotify 專輯連結 → 第一首立刻播放，其餘背景匯入，Container 底部顯示進度，完成後顯示成功／失敗數並自動消失
- [ ] 貼單曲連結 → 直接播放；正在播放時貼單曲 → 排入佇列並顯示位置
- [ ] 匯入中使用 `/music_pause`、`/music_skip`、`/music_queue` → 即時回應
- [ ] `/music_queue` 與面板「下一首預告」顯示匯入的曲目，格式與一般點播一致
- [ ] 50 首以上歌單穩定匯入，最後回報成功／失敗數

**邊界情境**
- [ ] 超過 200 首的歌單 → 只匯入前 200 首並提示截斷
- [ ] 帶 `?si=...` 的連結、`spotify:` URI、`intl-zh` 連結
- [ ] 匯入期間第一首播完 → 面板不收尾，下一批到達後接著播
- [ ] 匯入完成前把頻道成員全部離開 → 空房離開後匯入被取消，無殘留

**錯誤情境**
- [ ] 私人歌單／不存在的歌單 → 只有自己看到的友善錯誤
- [ ] 隨便貼 `open.spotify.com/artist/...` → 無法辨識的提示
- [ ] 不設定金鑰 → 提示功能未啟用，Bot 正常啟動
- [ ] 匯入中再貼另一份 → 被擋下並提示
- [ ] 匯入中 `/music_stop`、`/music_leave`、`/music_queue_clear` → 匯入取消、佇列不再增加、「匯入中」提示消失
- [ ] 手動把 Bot 踢出語音頻道 → 匯入取消，面板顯示已離開

**面板收尾**
- [ ] 播完全部歌曲 → 舊面板變灰並顯示「播放佇列已經全部播完」；再次點歌 → 頻道最下方出現**新的** Container
- [ ] 播完、自動離開、暫停／繼續／切歌／循環／跳轉，都沒有額外的公開訊息

### 4. 大型歌單穩定度與記憶體量測方法（實機）
1. **本機**：`python tools/bench_import_memory.py 1000 0.05`，觀察 tracemalloc 峰值是否隨首數線性成長、結束後是否回落。
2. **實機 RSS**：在雲端面板的 Metrics 觀察匯入前、匯入中、匯入後的記憶體曲線；匯入結束後應回到接近匯入前。
3. **事件迴圈延遲**：匯入中連續使用 `/music_pause`／`/music_resume`，主觀延遲不應明顯變差；也可觀察 bench 腳本印出的最大額外延遲。
4. **Lavalink 壓力**：調整 `IMPORT_CONCURRENCY`（1→3→5）觀察失敗率與總耗時，找出公開節點能承受的並發。
5. **取消後清理**：匯入中 `/music_stop`，用 `len(cog._import_tasks)` 或日誌確認任務已移除。

---

## 六、AI 參與度說明

- AI 負責：Spotify 服務實作、匯入流程與面板收尾修正、單元測試與壓力測試腳本、本日誌撰寫。
- 人工決策：歌單限制的處理方向（先照常實作並優雅降級）、進度提示放在 Container 底部、日誌格式。
- Spotify 政策內容由網路搜尋摘要取得，**尚未逐字核對官方文件**，需自行到 Spotify 開發者文件確認。

---

## 七、尚未解決的限制與下週預告

**尚未解決的限制**
1. 歌單匯入的真實可行性未驗證：取決於 Spotify 對 Client Credentials 讀取歌單的政策，以及是否有 Premium 帳號可建立 App。
2. 轉譯準確度：以「曲名 + 第一位歌手」在 YouTube 搜尋第一筆，可能搜到翻唱、現場版或 MV，尚未做時長比對。
3. 匯入進行中若發生重複的曲目，不會去重。
4. 暫停／續播的「請求點歌者同意」與停止投票訊息仍是獨立的公開訊息（它們是需要其他人按按鈕的互動，無法放進單一 Container）。
5. 公開 Lavalink 節點本身不穩定，搜尋失敗率可能偏高。

**下週預告（第 8 週，本週未動）**
多節點選擇、故障轉移、poToken／OAuth2。

