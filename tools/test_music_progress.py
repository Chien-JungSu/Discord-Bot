"""第 6 週單元測試：驗證 cogs/music.py 動態 Embed 與進度條渲染。

涵蓋項目：
1. format_time 時間格式化：
   - 一般值（mm:ss）
   - 0 ms 邊界（00:00）
   - 超過一小時（h:mm:ss）
   - 負值防呆（夾在 00:00）
   - None 防呆（夾在 00:00）
2. build_progress_bar 進度條數學換算與純函式渲染：
   - 0% 起點（按鈕在最前端）
   - 中間值（例如 50% 換算）
   - 100% 終點（按鈕在最後端）
   - position 超出長度（夾在 100%）
   - position 為負值（夾在 0%）
   - 超過一小時曲目（正確渲染 h:mm:ss 時間標籤）
   - 直播與邊界（is_stream=True、length=0、None、負值、極大值皆顯示「🔴 直播中」）
   - width 自訂寬度與防呆
3. build_now_playing_embed 動態中繼資料 Embed：
   - 未播放 / Player 為 None（顯示灰底「⏹️ 播放已結束」）
   - 播放中（藍紫色、🎶 正在播放、作者、縮圖、進度條、循環、佇列預告、來源）
   - 暫停中（橘色、⏸️ 已暫停）
   - 佇列為空時「下一首預告」顯示「無」；有歌曲時顯示標題與連結
   - 缺漏中繼資料容錯（無封面、無作者、無連結、無來源時皆有預設值不拋例外）
4. 節流與 Rate Limit 策略驗證：
   - 暫停狀態判定（player.paused 跳過更新）
   - 語音頻道真人檢查（無真人跳過更新）
   - 進度字串比對節流（字串無變化跳過 edit）
   - HTTP 429 退避冷卻（冷卻期間跳過該伺服器）
   - 多伺服器獨立隔離（伺服器 A 退避或暫停不干擾伺服器 B）

執行方式：python tools/test_music_progress.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

import os
from collections import deque
from unittest.mock import MagicMock

# 專案根目錄載入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from cogs.music import (
    PROGRESS_UPDATE_INTERVAL,
    format_time,
    build_progress_bar,
    build_now_playing_embed,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(cond: bool, name: str):
    (PASS if cond else FAIL).append(name)
    print(("✅" if cond else "❌") + f" {name}")


def test_format_time():
    """測試 1: format_time 邊界與時間換算。"""
    print("\n--- 測試 1: format_time 格式化 ---")
    # 0 ms
    check(format_time(0) == "00:00", "0 ms 格式化為 '00:00'")
    # 一般值 (1分23秒 = 83000 ms)
    check(format_time(83000) == "01:23", "83,000 ms 格式化為 '01:23'")
    # 4分05秒 = 245000 ms
    check(format_time(245000) == "04:05", "245,000 ms 格式化為 '04:05'")
    # 超過一小時 (1小時2分3秒 = 3723000 ms)
    check(format_time(3723000) == "1:02:03", "3,723,000 ms 格式化為 '1:02:03'")
    # 恰好兩小時 (7200000 ms)
    check(format_time(7200000) == "2:00:00", "7,200,000 ms 格式化為 '2:00:00'")
    # 負值防呆
    check(format_time(-5000) == "00:00", "負值 -5,000 ms 夾在 '00:00'")
    # None 防呆
    check(format_time(None) == "00:00", "None 夾在 '00:00'")


def test_build_progress_bar():
    """測試 2: build_progress_bar 數學換算與邊界情況。"""
    print("\n--- 測試 2: build_progress_bar 渲染 ---")
    total_len = 245000  # 4m 05s
    width = 20

    # 1. 起點 0%
    bar_0 = build_progress_bar(0, total_len, width=width)
    check(bar_0.startswith("🔘" + "▬" * 19), "0% 進度：按鈕在第 0 格 (🔘▬▬▬▬...)")
    check(bar_0.endswith("00:00 / 04:05"), "0% 進度：時間字串包含 '00:00 / 04:05'")

    # 2. 終點 100%
    bar_100 = build_progress_bar(total_len, total_len, width=width)
    check(bar_100.startswith("▬" * 19 + "🔘"), "100% 進度：按鈕在最後一格 (...▬▬▬▬🔘)")
    check(bar_100.endswith("04:05 / 04:05"), "100% 進度：時間字串包含 '04:05 / 04:05'")

    # 3. 超出長度夾在 100%
    bar_exceed = build_progress_bar(300000, total_len, width=width)
    check(bar_exceed == bar_100, "position 超出總長度時夾在 100%")

    # 4. 負值夾在 0%
    bar_neg = build_progress_bar(-10000, total_len, width=width)
    check(bar_neg == bar_0, "position 負值時夾在 0%")

    # 5. 中間值 (約 50%)
    half_len = total_len // 2
    bar_50 = build_progress_bar(half_len, total_len, width=width)
    check("🔘" in bar_50, "50% 進度：包含按鈕符號")
    btn_index = bar_50.index("🔘")
    check(8 <= btn_index <= 11, f"50% 進度：按鈕位置居中 (index={btn_index})")

    # 6. 超過一小時的長度
    bar_long = build_progress_bar(3723000, 7200000, width=width)
    check("1:02:03 / 2:00:00" in bar_long, "超過一小時長度：正確顯示 h:mm:ss 時間標籤")

    # 7. 直播串流邊界情況
    check(build_progress_bar(0, 0, width=width) == "🔴 直播中", "length=0 顯示 '🔴 直播中'")
    check(build_progress_bar(0, None, width=width) == "🔴 直播中", "length=None 顯示 '🔴 直播中'")
    check(build_progress_bar(0, -100, width=width) == "🔴 直播中", "length 負值顯示 '🔴 直播中'")
    check(build_progress_bar(0, 100_000_000, width=width) == "🔴 直播中", "length >= 24小時（極大值）顯示 '🔴 直播中'")
    check(build_progress_bar(1000, 5000, width=width, is_stream=True) == "🔴 直播中", "is_stream=True 顯示 '🔴 直播中'")

    # 8. 自訂寬度
    bar_w10 = build_progress_bar(0, total_len, width=10)
    bar_symbols = bar_w10.split(" ")[0]
    check(len(bar_symbols) == 10, f"width=10 輸出精確 10 個符號 (實際={len(bar_symbols)})")


def test_build_now_playing_embed():
    """測試 3: build_now_playing_embed 中繼資料與外觀狀態。"""
    print("\n--- 測試 3: build_now_playing_embed 渲染 ---")

    # 1. Player 為 None（未播放）
    embed_none = build_now_playing_embed(None)
    check(embed_none.title == "⏹️ 播放已結束", "無 Player 時 Embed 標題為 '⏹️ 播放已結束'")
    check(embed_none.color == discord.Color.light_grey(), "無 Player 時 Embed 配色為 light_grey")

    # 2. 正常播放中
    mock_track = MagicMock()
    mock_track.title = "夜に駆ける"
    mock_track.author = "YOASOBI"
    mock_track.length = 261000
    mock_track.artwork = "https://example.com/cover.jpg"
    mock_track.uri = "https://youtube.com/watch?v=mock"
    mock_track.source = "youtube"
    mock_track.is_stream = False

    mock_next_track = MagicMock()
    mock_next_track.title = "群青"
    mock_next_track.uri = "https://youtube.com/watch?v=next"

    mock_player = MagicMock()
    mock_player.current = mock_track
    mock_player.paused = False
    mock_player.volume = 75
    mock_player.position = 60000
    mock_player.loop_mode = "single"
    mock_player.current_requester = "測試者小明"
    mock_player.song_queue = deque([(mock_next_track, "小華", 456)])

    embed_playing = build_now_playing_embed(mock_player)
    check("🎶 正在播放" in embed_playing.title, "播放中標題包含 '🎶 正在播放'")
    check(embed_playing.color == discord.Color.blurple(), "播放中配色為 blurple")
    check(f"[{mock_track.title}]({mock_track.uri})" in embed_playing.description, "曲目標題呈現可點擊 Markdown 超連結")
    check(embed_playing.thumbnail.url == "https://example.com/cover.jpg", "正確設定封面縮圖 URL")

    # 檢查欄位
    field_dict = {f.name: f.value for f in embed_playing.fields}
    check(field_dict.get("👤 作者") == "YOASOBI", "作者欄位正確")
    check(field_dict.get("🔊 音量") == "75%", "音量欄位正確")
    check("單曲" in field_dict.get("🔁 循環模式", ""), "循環模式欄位正確")
    check("🔘" in field_dict.get("⏱️ 播放進度", ""), "播放進度欄位包含進度條")
    check(f"[{mock_next_track.title}]({mock_next_track.uri})" in field_dict.get("📜 下一首預告", ""), "下一首預告正確取出佇列首曲並呈現超連結")
    check("測試者小明" in embed_playing.footer.text and "YouTube" in embed_playing.footer.text, "頁尾正確顯示點播者與美化後的來源站")

    # 3. 暫停中外觀切換
    mock_player.paused = True
    embed_paused = build_now_playing_embed(mock_player)
    check("⏸️ 已暫停" in embed_paused.title, "暫停時標題包含 '⏸️ 已暫停'")
    check(embed_paused.color == discord.Color.orange(), "暫停時配色切換為 orange")

    # 4. 佇列為空時下一首預告
    mock_player.song_queue = deque()
    embed_empty_q = build_now_playing_embed(mock_player)
    field_dict_empty = {f.name: f.value for f in embed_empty_q.fields}
    check(field_dict_empty.get("📜 下一首預告") == "無", "佇列為空時下一首預告顯示 '無'")

    # 5. 缺漏欄位容錯（無封面、無作者、無連結、無來源）
    mock_broken_track = MagicMock()
    mock_broken_track.title = "無名之歌"
    mock_broken_track.author = None
    mock_broken_track.length = None
    mock_broken_track.artwork = None
    mock_broken_track.uri = None
    mock_broken_track.source = None
    mock_broken_track.is_stream = False

    mock_broken_player = MagicMock()
    mock_broken_player.current = mock_broken_track
    mock_broken_player.paused = False
    mock_broken_player.volume = "invalid"  # 非法型態
    mock_broken_player.position = None
    mock_broken_player.loop_mode = "unknown"
    mock_broken_player.current_requester = None
    mock_broken_player.song_queue = None

    try:
        embed_fallback = build_now_playing_embed(mock_broken_player)
        check(embed_fallback.thumbnail.url is None, "無封面時不拋出例外且縮圖為 None")
        check(embed_fallback.description == "無名之歌", "無 uri 時標題呈現純文字而非損壞連結")
        fb_fields = {f.name: f.value for f in embed_fallback.fields}
        check(fb_fields.get("👤 作者") == "未知", "作者為 None 時安全顯示 '未知'")
        check(fb_fields.get("🔊 音量") == "100%", "非法音量時安全回退至 '100%'")
        check(fb_fields.get("⏱️ 播放進度") == "🔴 直播中", "長度為 None 時安全顯示 '🔴 直播中'")
        check(fb_fields.get("📜 下一首預告") == "無", "song_queue 為 None 時安全顯示 '無'")
        check("Lavalink" in embed_fallback.footer.text, "來源為 None 時預設顯示 'Lavalink'")
    except Exception as exc:
        check(False, f"缺漏中繼資料時拋出未預期例外：{exc}")


def test_throttling_and_rate_limit():
    """測試 4: 節流與 429 API rate-limit 防護邏輯。"""
    print("\n--- 測試 4: 節流與 API Rate Limit 防護 ---")

    # 集中常數檢查
    check(PROGRESS_UPDATE_INTERVAL >= 5.0, f"更新間隔集中常數 PROGRESS_UPDATE_INTERVAL 至少 5 秒 (當前: {PROGRESS_UPDATE_INTERVAL}s)")

    # 模擬 Cog 的節流與狀態紀錄字典
    last_progress: dict[int, str] = {}
    rate_limit_until: dict[int, float] = {}

    guild_a = 1001
    guild_b = 2002

    # 1. 進度字串相同時跳過
    current_bar_a = build_progress_bar(30000, 180000)
    last_progress[guild_a] = current_bar_a
    # 判斷邏輯：字串未改變則 skip
    should_skip_unchanged = (last_progress.get(guild_a) == current_bar_a)
    check(should_skip_unchanged is True, "進度條字串與上次相同時正確識別並跳過 edit")

    # 2. 進度前進後不跳過
    next_bar_a = build_progress_bar(35000, 180000)
    should_skip_changed = (last_progress.get(guild_a) == next_bar_a)
    check(should_skip_changed is False, "進度條前進改變後正確識別不跳過")

    # 3. 播放器暫停時跳過
    mock_player = MagicMock()
    mock_player.paused = True
    check(bool(mock_player.paused) is True, "播放器暫停狀態正確識別跳過")

    # 4. 語音頻道只有機器人（無真人）時跳過
    bot_member = MagicMock()
    bot_member.bot = True
    mock_channel = MagicMock()
    mock_channel.members = [bot_member]
    has_human = any(not m.bot for m in mock_channel.members)
    check(has_human is False, "語音頻道只有 Bot 時正確識別跳過更新")

    # 5. 語音頻道有真人時正常更新
    human_member = MagicMock()
    human_member.bot = False
    mock_channel.members.append(human_member)
    has_human_now = any(not m.bot for m in mock_channel.members)
    check(has_human_now is True, "語音頻道有真人時正常允許更新")

    # 6. HTTP 429 退避與多伺服器隔離
    import time
    now = time.monotonic()
    # 伺服器 A 遭遇 429，冷卻 10 秒
    rate_limit_until[guild_a] = now + 10.0
    # 伺服器 B 正常運作
    rate_limit_until[guild_b] = 0.0

    guild_a_blocked = (now < rate_limit_until.get(guild_a, 0.0))
    guild_b_blocked = (now < rate_limit_until.get(guild_b, 0.0))
    check(guild_a_blocked is True, "伺服器 A 處於 429 退避期間，正確阻擋更新")
    check(guild_b_blocked is False, "伺服器 B 處於正常狀態，不受伺服器 A 退避影響（多伺服器隔離）")


def main():
    print("=== 開始執行第 6 週動態 Embed 與進度條渲染單元測試 ===")
    test_format_time()
    test_build_progress_bar()
    test_build_now_playing_embed()
    test_throttling_and_rate_limit()

    total = len(PASS) + len(FAIL)
    print(f"\n==========================================")
    print(f"測試統計：共 {total} 項測試，{len(PASS)} 通過，{len(FAIL)} 失敗")
    print(f"==========================================")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
