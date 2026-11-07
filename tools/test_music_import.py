"""第 7 週單元測試：驗證 cogs/music.py 的面板拋棄機制與 Spotify 批次匯入流程（全程離線、用假物件）。

涵蓋項目：
1. 面板拋棄：結束後移除記錄、最終狀態含原因；下次播放建立「新」Container 而不是編輯舊的
2. 通知集中：播完／自動離開原因寫進面板，不另外發訊息
3. 匯入：第一首立刻播放、其餘分批依序加入、成功／失敗統計、截斷提示
4. Semaphore 並發上限、單首逾時只失敗該首
5. 取消：/stop、/leave、清佇列取消背景任務，已解析未加入的曲目丟棄，無殘留任務
6. 同伺服器重複匯入被擋、金鑰未設定／連結無效的友善提示
7. 匯入期間佇列暫空不會被當成播放結束

執行方式：python tools/test_music_import.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import os
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cogs.music as music_mod  # noqa: E402
from cogs.music import Music  # noqa: E402
from services.spotify import SpotifyClient, SpotifyFetchResult, SpotifyTrack  # noqa: E402

PASS, FAIL = [], []
GID = 1


def check(cond, name):
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name)


class FakePlayer:
    def __init__(self):
        self.song_queue = deque()
        self.current = None
        self.paused = False
        self.connected = True
        self.loop_mode = "off"
        self.end_intent = None
        self.volume = 100
        self.position = 0
        self.home_channel = None
        self.channel = None
        self.guild = SimpleNamespace(id=GID)
        self.played = []

    @property
    def playing(self):
        return self.current is not None and not self.paused

    async def play(self, track):
        self.current = track
        self.played.append(track)


def fake_track(name, **kw):
    return SimpleNamespace(title=name, uri=f"https://x/{name}", author="A", length=200000,
                           is_stream=False, is_seekable=True, artwork=None, source="youtube", **kw)


def make_cog(player):
    bot = MagicMock()
    guild = SimpleNamespace(id=GID, voice_client=player)
    bot.get_guild = lambda gid: guild if gid == GID else None
    bot.user = SimpleNamespace(id=999)
    bot.notify_owner_error = AsyncMock()
    cog = Music(bot)
    cog._sync_panel = AsyncMock()  # 匯入邏輯測試不關心 Discord 實際 edit
    cog.panel_messages[GID] = (10, 20)
    return cog


def make_interaction():
    it = MagicMock()
    it.guild = SimpleNamespace(id=GID)
    it.user = SimpleNamespace(id=7, display_name="測試者")
    it.delete_original_response = AsyncMock()
    it.followup.send = AsyncMock()
    return it


def make_tracks(n):
    return [SpotifyTrack(title=f"歌{i}", artist=f"手{i}", duration_ms=1000) for i in range(n)]


def patch_fetch(cog, n, truncated=False, skipped=0):
    cog.spotify = MagicMock()
    cog.spotify.enabled = True
    cog.spotify.fetch_tracks = AsyncMock(return_value=SpotifyFetchResult("playlist", make_tracks(n), truncated, skipped))
    cog.spotify.close = AsyncMock()


async def wait_task(cog):
    task = cog._import_tasks.get(GID)
    if task:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)


# ---------- 測試 1、2：面板拋棄 ----------
async def test_panel_abandon():
    print("\n--- 測試 1/2: 面板拋棄與通知集中 ---")
    player = FakePlayer()
    player.current = fake_track("now")
    bot = MagicMock()
    guild = SimpleNamespace(id=GID, voice_client=player)
    bot.get_guild = lambda gid: guild
    msg = MagicMock()
    msg.id = 20
    msg.edit = AsyncMock()
    channel = MagicMock()
    channel.id = 10
    channel.fetch_message = AsyncMock(return_value=msg)
    new_msg = MagicMock()
    new_msg.id = 30
    channel.send = AsyncMock(return_value=new_msg)
    bot.get_channel = lambda cid: channel
    cog = Music(bot)
    cog.panel_messages[GID] = (10, 20)
    player.home_channel = channel

    shown = await cog._update_panel_ended(GID, reason="📭 播完了")
    check(shown is True, "結束時有面板可顯示 → 回傳 True")
    check(GID not in cog.panel_messages, "結束後面板記錄被移除（拋棄舊 Container）")
    check(msg.edit.await_count == 1, "舊面板被編輯成最終狀態")
    final_view = msg.edit.await_args.kwargs["view"]
    texts = " ".join(getattr(c, "content", "") for c in final_view.container.children if hasattr(c, "content"))
    check("播完了" in texts, "結束原因寫在面板最終狀態內")
    check(all(getattr(i, "disabled", False) for i in final_view.walk_children() if isinstance(i, music_mod.ui.Button)),
          "最終狀態的按鈕全部 disabled")

    # 下次點歌：沒有記錄 → 必須送出全新的面板
    player.current = fake_track("next")
    await cog._sync_panel(GID, player)
    check(channel.send.await_count == 1, "重新點歌時送出新的 Container 訊息")
    check(cog.panel_messages.get(GID) == (10, 30), "記錄改指向新訊息")
    check(msg.edit.await_count == 1, "舊面板不再被編輯")

    # 沒有面板時 → 回傳 False（呼叫端才會退回其他方式）
    cog.panel_messages.clear()
    check(await cog._update_panel_ended(GID, reason="x") is False, "沒有面板時回傳 False")

    # 通知集中：有面板 → 寫進 notice；沒面板 → False
    cog.panel_messages[GID] = (10, 30)
    cog._sync_panel = AsyncMock()
    check(await cog._notify_panel(GID, "⚠️ 測試通知", duration=None) is True, "有面板時通知寫進 Container")
    check(cog._panel_notices.get(GID) == "⚠️ 測試通知", "notice 內容正確")
    cog.panel_messages.clear()
    check(await cog._notify_panel(GID, "x") is False, "沒有面板時 _notify_panel 回傳 False")


# ---------- 測試 3：匯入流程 ----------
async def test_import_flow():
    print("\n--- 測試 3: 匯入流程 ---")
    player = FakePlayer()
    cog = make_cog(player)
    patch_fetch(cog, 25, truncated=True, skipped=2)

    async def fake_search(sem, st):
        async with sem:
            await asyncio.sleep(0.01)
        if st.title in ("歌5", "歌10"):
            return None  # 模擬找不到
        return fake_track(st.title)

    cog._search_spotify_track = fake_search
    it = make_interaction()
    await cog._start_spotify_import(it, player, "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc")
    check(GID in cog._import_tasks, "背景任務已建立並保存參照")
    await wait_task(cog)
    await asyncio.sleep(0.05)

    check(player.played and player.played[0].title == "歌0", "第一首解析完立刻開始播放")
    check(it.delete_original_response.await_count >= 1, "第一首開始後收掉 defer 思考訊息")
    queued = [i[0].title for i in player.song_queue]
    expected = [f"歌{i}" for i in range(1, 25) if i not in (5, 10)]
    check(queued == expected, "其餘曲目依原順序加入佇列（失敗的被略過）")
    check(all(i[1] == "測試者" and i[2] == 7 for i in player.song_queue), "佇列項目格式與一般點播一致 (track, 名稱, ID)")
    notice = cog._panel_notices.get(GID, "")
    check("成功 23 首／失敗 2 首" in notice, f"結果回報成功／失敗數 ({notice})")
    check("僅匯入前" in notice and "略過 2 首" in notice, "回報包含截斷與略過提示")
    check(GID not in cog._import_tasks, "任務結束後字典自動清除（無殘留）")


# ---------- 測試 4：並發與逾時 ----------
async def test_semaphore_and_timeout():
    print("\n--- 測試 4: Semaphore 並發與單首逾時 ---")
    cog = make_cog(FakePlayer())
    running = {"now": 0, "max": 0}

    class FakePlaylist:
        pass

    async def slow_search(q):
        running["now"] += 1
        running["max"] = max(running["max"], running["now"])
        await asyncio.sleep(0.02)
        running["now"] -= 1
        return [fake_track(q)]

    orig = music_mod.wavelink
    music_mod.wavelink = SimpleNamespace(Playable=SimpleNamespace(search=slow_search), Playlist=FakePlaylist)
    try:
        sem = asyncio.Semaphore(3)
        res = await asyncio.gather(*(cog._search_spotify_track(sem, st) for st in make_tracks(12)))
        check(running["max"] <= 3, f"同時搜尋數不超過 Semaphore 上限 (最大 {running['max']})")
        check(all(r is not None for r in res), "12 首全部成功解析")

        async def hang(q):
            await asyncio.sleep(5)

        music_mod.wavelink.Playable.search = hang
        old = music_mod.IMPORT_SEARCH_TIMEOUT
        music_mod.IMPORT_SEARCH_TIMEOUT = 0.05
        r = await cog._search_spotify_track(asyncio.Semaphore(1), make_tracks(1)[0])
        music_mod.IMPORT_SEARCH_TIMEOUT = old
        check(r is None, "單首逾時只回傳 None，不拋例外")

        async def boom(q):
            raise RuntimeError("node down")

        music_mod.wavelink.Playable.search = boom
        check(await cog._search_spotify_track(asyncio.Semaphore(1), make_tracks(1)[0]) is None, "搜尋例外只讓該首失敗")

        async def empty(q):
            return []

        music_mod.wavelink.Playable.search = empty
        check(await cog._search_spotify_track(asyncio.Semaphore(1), make_tracks(1)[0]) is None, "找不到結果回傳 None")
    finally:
        music_mod.wavelink = orig


# ---------- 測試 5：取消 ----------
async def test_cancel():
    print("\n--- 測試 5: 取消與清理 ---")
    player = FakePlayer()
    cog = make_cog(player)
    patch_fetch(cog, 60)
    gate = asyncio.Event()

    async def gated_search(sem, st):
        if st.title != "歌0":
            await gate.wait()  # 第一首之後全部卡住，模擬還在解析
        return fake_track(st.title)

    cog._search_spotify_track = gated_search
    it = make_interaction()
    await cog._start_spotify_import(it, player, "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    await asyncio.sleep(0.1)
    task = cog._import_tasks.get(GID)
    check(task is not None and not task.done(), "背景匯入進行中")
    check(len(player.song_queue) == 0 and player.played[0].title == "歌0", "第一首已播放、其餘尚在解析")

    # 重複匯入被擋
    cog._ephemeral_error = AsyncMock()
    await cog._start_spotify_import(it, player, "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    msg = cog._ephemeral_error.await_args.args[1] if cog._ephemeral_error.await_args else ""
    check("已經有" in msg, "同伺服器重複匯入被擋下並提示")
    check(cog._import_tasks.get(GID) is task, "原任務不受影響")

    check(cog._cancel_import(GID) is True, "_cancel_import 回報有任務被取消")
    gate.set()  # 就算解析現在才完成，也不能再加入佇列
    await asyncio.sleep(0.1)
    check(task.cancelled() or task.done(), "任務已結束")
    check(len(player.song_queue) == 0, "已解析但未加入的曲目被丟棄（佇列維持空）")
    check(GID not in cog._import_tasks, "取消後字典無殘留")
    check(not cog._panel_notices.get(GID, "").startswith("📥"), "取消後『匯入中』提示被清除")
    check(cog._cancel_import(GID) is False, "沒有任務時再次取消回傳 False")


# ---------- 測試 6：錯誤提示 ----------
async def test_errors():
    print("\n--- 測試 6: 友善錯誤提示 ---")
    player = FakePlayer()
    cog = make_cog(player)
    cog._ephemeral_error = AsyncMock()
    it = make_interaction()

    await cog._start_spotify_import(it, player, "https://open.spotify.com/artist/37i9dQZF1DXcBWIGoYBM5M")
    check("無法辨識" in cog._ephemeral_error.await_args.args[1], "無法辨識的連結 → 友善錯誤")

    cog.spotify = SpotifyClient("", "")
    await cog._start_spotify_import(it, player, "spotify:track:37i9dQZF1DXcBWIGoYBM5M")
    check("未啟用" in cog._ephemeral_error.await_args.args[1], "金鑰未設定 → 友善提示")
    check(GID not in cog._import_tasks, "上述情況不會建立背景任務")

    # Spotify 回報私人歌單 → 友善訊息，且不建立殘留任務
    from services.spotify import SpotifyForbiddenError
    cog = make_cog(player)
    cog.spotify = MagicMock()
    cog.spotify.enabled = True
    cog.spotify.fetch_tracks = AsyncMock(side_effect=SpotifyForbiddenError("無法讀取：私人歌單"))
    cog._ephemeral_error = AsyncMock()
    await cog._start_spotify_import(it, player, "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    await asyncio.sleep(0.1)
    check("私人歌單" in cog._ephemeral_error.await_args.args[1], "私人歌單 → 友善訊息")
    check(GID not in cog._import_tasks, "失敗後無殘留任務")


# ---------- 測試 7：匯入期間佇列暫空 ----------
async def test_track_end_guard():
    print("\n--- 測試 7: 匯入期間佇列暫空不視為播放結束 ---")
    player = FakePlayer()
    cog = make_cog(player)
    cog._update_panel_ended = AsyncMock(return_value=True)
    cog._cleanup_control_state = MagicMock()
    track = fake_track("a")
    payload = SimpleNamespace(player=player, reason="finished", track=track)

    cog._import_tasks[GID] = asyncio.create_task(asyncio.sleep(5))
    await cog.on_wavelink_track_end(payload)
    check(cog._update_panel_ended.await_count == 0, "匯入進行中：佇列空時不收掉面板")
    cog._cancel_import(GID)

    await cog.on_wavelink_track_end(payload)
    check(cog._update_panel_ended.await_count == 1, "沒有匯入時：佇列空 → 收掉面板")
    check("播完" in cog._update_panel_ended.await_args.kwargs.get("reason", ""), "播完原因寫在面板，不另外發訊息")

    # 匯入的下一批抵達時，閒置的播放器會被接手播放
    player.current = None
    player.song_queue.append((fake_track("b"), "x", 1))
    await cog._start_if_idle(player)
    check(player.played and player.played[-1].title == "b", "閒置時 _start_if_idle 接手播放下一批")
    player.song_queue.append((fake_track("c"), "x", 1))
    await cog._start_if_idle(player)
    check(player.played[-1].title == "b" and len(player.song_queue) == 1, "播放中不會被覆蓋")
    player.current = None
    player.advancing = True
    await cog._start_if_idle(player)
    check(player.played[-1].title == "b", "track_end 正在接歌時匯入不搶播")


# ---------- 測試 8：面板循環按鈕與關閉循環不自動跳歌 ----------
async def test_loop_button_and_no_autoskip():
    print("\n--- 測試 8: 控制面板循環按鈕與關閉循環防自動跳歌 ---")
    player = FakePlayer()
    cog = make_cog(player)
    song_a = fake_track("SongA", identifier="id_a", encoded="enc_a")
    song_b = fake_track("SongB", identifier="id_b", encoded="enc_b")
    song_c = fake_track("SongC", identifier="id_c", encoded="enc_c")

    await cog._play_track(player, song_a, "測試者", 7)
    player.song_queue.append((song_b, "測試者", 7))
    player.song_queue.append((song_c, "測試者", 7))

    view = music_mod.MusicControlView(cog, player=player)
    check(hasattr(view, "btn_loop") and view.btn_loop in view.row2.children, "控制面板第二列包含循環按鈕")
    check(view.btn_loop.label == "循環：關閉" and not view.btn_loop.disabled, "初始狀態為『循環：關閉』且可點擊")

    it = make_interaction()
    it.guild.voice_client = player

    # 點擊第 1 次：off -> single
    await view.toggle_loop(it)
    view.refresh_buttons(player)
    check(player.loop_mode == "single" and view.btn_loop.label == "循環：單曲", "點擊第 1 次切換為『循環：單曲』")

    # 點擊第 2 次：single -> all
    await view.toggle_loop(it)
    view.refresh_buttons(player)
    check(player.loop_mode == "all" and view.btn_loop.label == "循環：佇列", "點擊第 2 次切換為『循環：佇列』")

    # 點擊第 3 次：all -> off（關閉循環狀態時絕不跳過當前歌曲）
    await view.toggle_loop(it)
    view.refresh_buttons(player)
    check(player.loop_mode == "off" and view.btn_loop.label == "循環：關閉", "點擊第 3 次切換回『循環：關閉』")
    check(player.current is song_a and len(player.played) == 1, "關閉循環狀態時不會跳過當前播放中的 SongA")

    # 情境 A：SongA 自然結束 -> 切換到 SongB；若緊接著收到同一次播放重複派發的 TrackEndEvent，不能把剛開始的 SongB 跳過
    end_a = SimpleNamespace(player=player, reason="finished", track=fake_track("SongA", identifier="id_a", encoded="enc_a"))
    await cog.on_wavelink_track_end(end_a)
    check(player.current is song_b and len(player.song_queue) == 1, "SongA 結束後正常換播 SongB")

    # 模擬重複抵達的 SongA TrackEndEvent（包含模擬 wavelink/websocket.py 將 _current 清成 None）
    player._current = None
    player.current = None
    await cog.on_wavelink_track_end(end_a)
    check(
        getattr(player, "_current", None) is song_b and len(player.song_queue) == 1,
        "延遲抵達的上一首 TrackEndEvent 被攔截並還原 _current，不會在關閉循環時自動跳過 SongB",
    )
    player.current = song_b

    # 情境 B：沒有 end_intent 的殘留 stopped 事件不能把當前歌曲跳過
    spurious_stop = SimpleNamespace(player=player, reason="stopped", track=fake_track("SongB", identifier="id_b", encoded="enc_b"))
    await cog.on_wavelink_track_end(spurious_stop)
    check(player.current is song_b and len(player.song_queue) == 1, "無意圖的殘留 stopped 事件不會跳過當前歌曲 SongB")


# ---------- 測試 9：防洗版自動置底（Sticky Panel） ----------
async def test_sticky_panel_auto_bump():
    print("\n--- 測試 9: 控制面板防被普通訊息刷掉（自動置底與暫時訊息過濾） ---")
    player = FakePlayer()
    player.current = fake_track("PlayingSong")
    bot = MagicMock()
    guild = SimpleNamespace(id=GID, voice_client=player)
    bot.get_guild = lambda gid: guild

    old_panel_msg = MagicMock()
    old_panel_msg.delete = AsyncMock()
    new_panel_msg = MagicMock()
    new_panel_msg.id = 999

    channel = MagicMock()
    channel.id = 10
    channel.last_message_id = 500  # 最新訊息是普通聊天訊息，不是舊面板 (id=20)
    channel.send = AsyncMock(return_value=new_panel_msg)
    channel.get_partial_message = MagicMock(return_value=old_panel_msg)
    bot.get_channel = lambda cid: channel

    cog = Music(bot)
    cog.panel_messages[GID] = (10, 20)

    old_delay = music_mod.PANEL_BUMP_DELAY
    music_mod.PANEL_BUMP_DELAY = 0.05
    try:
        # 1. 暫時訊息發出後立刻被刪除（如指令 defer 思考訊息）-> 取消置底，不重發面板
        temp_msg = SimpleNamespace(
            id=300,
            guild=guild,
            channel=SimpleNamespace(id=10),
            flags=SimpleNamespace(ephemeral=False, loading=False),
        )
        await cog.on_message(temp_msg)
        check(GID in cog._panel_bump_tasks, "收到同頻道訊息後建立防抖置底任務")
        await cog.on_message_delete(temp_msg)
        check(GID not in cog._panel_bump_tasks, "暫時訊息被刪除後自動取消置底任務，避免無謂重發")
        await asyncio.sleep(0.08)
        check(channel.send.await_count == 0, "暫時訊息刪除後不會觸發面板重發")

        # 2. 一般聊天訊息保留在頻道上 -> 防抖結束後自動重發新面板至底部並刪除舊面板
        chat_msg = SimpleNamespace(
            id=500,
            guild=guild,
            channel=SimpleNamespace(id=10),
            flags=SimpleNamespace(ephemeral=False, loading=False),
        )
        await cog.on_message(chat_msg)
        await asyncio.sleep(0.08)
        check(channel.send.await_count == 1, "普通訊息刷過面板後，於頻道底部重發新面板")
        check(cog.panel_messages.get(GID) == (10, 999), "panel_messages 更新為新面板訊息 ID")
        check(old_panel_msg.delete.await_count == 1, "舊面板訊息被自動刪除")
    finally:
        music_mod.PANEL_BUMP_DELAY = old_delay


async def main_async():
    await test_panel_abandon()
    await test_import_flow()
    await test_semaphore_and_timeout()
    await test_cancel()
    await test_errors()
    await test_track_end_guard()
    await test_loop_button_and_no_autoskip()
    await test_sticky_panel_auto_bump()


def main():
    print("=== 開始執行第 7 週匯入與面板單元測試 ===")
    asyncio.run(main_async())
    total = len(PASS) + len(FAIL)
    print(f"\n測試統計：共 {total} 項，{len(PASS)} 通過，{len(FAIL)} 失敗")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()

