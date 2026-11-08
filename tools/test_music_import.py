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

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

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
    defaults = {
        "title": name,
        "uri": f"https://x/{name}",
        "author": "A",
        "length": 200000,
        "is_stream": False,
        "is_seekable": True,
        "artwork": None,
        "source": "youtube",
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


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
    it.response.defer = AsyncMock()
    it.response.send_message = AsyncMock()
    it.response.is_done = MagicMock(return_value=False)
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

    # 情境 A：SongA 自然結束（Lavalink v4 會在 TrackEndEvent 的 encoded 寫入結尾播放位置，導致 encoded 與開始時不同）
    # 必須能透過 identifier / uri / title 識別為同一首歌並自動播放下一首 SongB
    end_a = SimpleNamespace(
        player=player,
        reason="finished",
        track=fake_track("SongA", identifier="id_a", encoded="enc_a_finished_pos_200000"),
    )
    await cog.on_wavelink_track_end(end_a)
    check(
        player.current is song_b and len(player.song_queue) == 1,
        "SongA 結束後（即使 Lavalink 改變 encoded 內含的播放進度）仍正常自動換播下一首 SongB",
    )

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
    old_cooldown = music_mod.PANEL_BUMP_COOLDOWN
    music_mod.PANEL_BUMP_DELAY = 0.05
    music_mod.PANEL_BUMP_COOLDOWN = 0.15
    try:
        # 0. 機器人自身訊息或面板訊息絕不能觸發置底任務
        bot.user = SimpleNamespace(id=999)
        bot_msg = SimpleNamespace(
            id=250,
            guild=guild,
            channel=SimpleNamespace(id=10),
            author=SimpleNamespace(id=999, bot=True),
            flags=SimpleNamespace(ephemeral=False, loading=False),
        )
        await cog.on_message(bot_msg)
        check(GID not in cog._panel_bump_tasks, "機器人自身發送的普通訊息不會誤觸置底任務")

        # 0.05 驗證其他機器人發送的訊息出現在面板下方時會成功觸發置底任務
        other_bot_msg = SimpleNamespace(
            id=255,
            guild=guild,
            channel=SimpleNamespace(id=10),
            author=SimpleNamespace(id=888, bot=True),
            flags=SimpleNamespace(ephemeral=False, loading=False),
        )
        await cog.on_message(other_bot_msg)
        check(GID in cog._panel_bump_tasks, "其他機器人發送的訊息會正常觸發面板置底任務")
        cog._cancel_panel_bump(GID, force=True)

        # 0.1 驗證 /music_queue 產生的公開佇列訊息會成功觸發置底任務（即使 author.bot 為 True）
        queue_bot_msg = SimpleNamespace(
            id=260,
            guild=guild,
            channel=SimpleNamespace(id=10),
            author=SimpleNamespace(id=999, bot=True),
            flags=SimpleNamespace(ephemeral=False, loading=False),
            interaction_metadata=SimpleNamespace(name="music_queue"),
            embeds=[SimpleNamespace(title="🎵 播放佇列")],
        )
        await cog.on_message(queue_bot_msg)
        check(GID in cog._panel_bump_tasks, "on_message: /music_queue 的播放佇列訊息會觸發置底任務")
        cog._cancel_panel_bump(GID, force=True)

        # 0.2 驗證呼叫 /music_queue 指令時會主動為該訊息排程面板置底任務
        it_q = make_interaction()
        it_q.guild = guild
        it_q.channel = SimpleNamespace(id=10)
        it_q.channel_id = 10
        q_msg_obj = SimpleNamespace(id=270)
        it_q.original_response = AsyncMock(return_value=q_msg_obj)
        cog._check_music_channel = AsyncMock(return_value=True)
        await cog.queue_.callback(cog, it_q)
        check(GID in cog._panel_bump_tasks, "/music_queue 指令執行後成功排程面板置底任務")
        check(270 in cog._bump_trigger_msgs.get(GID, set()), "/music_queue 訊息 ID 成功加入置底觸發集合")
        cog._cancel_panel_bump(GID, force=True)

        # 1. 暫時訊息發出後立刻被刪除（如指令 defer 思考訊息）-> 取消置底，不重發面板
        temp_msg = SimpleNamespace(
            id=300,
            guild=guild,
            channel=SimpleNamespace(id=10),
            author=SimpleNamespace(id=123, bot=False),
            flags=SimpleNamespace(ephemeral=False, loading=False),
        )
        await cog.on_message(temp_msg)
        check(GID in cog._panel_bump_tasks, "收到同頻道普通訊息後建立防抖置底任務")
        await cog.on_message_delete(temp_msg)
        check(GID not in cog._panel_bump_tasks, "暫時訊息被刪除後自動取消置底任務，避免無謂重發")
        await asyncio.sleep(0.08)
        check(channel.send.await_count == 0, "暫時訊息刪除後不會觸發面板重發")

        # 2. 一般聊天訊息保留在頻道上，且模擬 Discord Gateway 在 channel.send 回傳前先觸發 on_message ->
        #    絕不能中斷 _bump_panel、必須刪除舊面板、且不能無限重發
        async def send_simulating_gateway_race(**kwargs):
            # 模擬新面板訊息在 send() 尚未返回時先由 Gateway 推播 on_message
            gateway_panel_msg = SimpleNamespace(
                id=999,
                guild=guild,
                channel=SimpleNamespace(id=10),
                author=SimpleNamespace(id=999, bot=True),
                components=[SimpleNamespace(custom_id="music:toggle_pause")],
                flags=SimpleNamespace(ephemeral=False, loading=False),
            )
            await cog.on_message(gateway_panel_msg)
            return new_panel_msg

        channel.send = AsyncMock(side_effect=send_simulating_gateway_race)

        chat_msg = SimpleNamespace(
            id=500,
            guild=guild,
            channel=SimpleNamespace(id=10),
            author=SimpleNamespace(id=123, bot=False),
            flags=SimpleNamespace(ephemeral=False, loading=False),
        )
        await cog.on_message(chat_msg)
        await asyncio.sleep(0.08)
        check(channel.send.await_count == 1, "普通訊息刷過面板後，於頻道底部重發新面板（僅發送 1 次）")
        check(cog.panel_messages.get(GID) == (10, 999), "panel_messages 更新為新面板訊息 ID")
        check(old_panel_msg.delete.await_count == 1, "舊面板訊息被自動刪除")

        # 再等一輪防抖時間，確認不會無限重發
        await asyncio.sleep(0.08)
        check(channel.send.await_count == 1 and GID not in cog._panel_bump_tasks, "置底完成後不會再次觸發或無限重發面板")

        # 3. 短時間重複觸發冷卻保護（PANEL_BUMP_COOLDOWN）：剛置底完立刻又有訊息時，在冷卻結束前不會馬上重發，冷卻後才置底並清除舊面板
        cog._last_bump_at[GID] = music_mod.time.monotonic()
        second_panel_msg = MagicMock()
        second_panel_msg.id = 1500
        new_panel_msg.delete = AsyncMock()
        new_panel_msg.edit = AsyncMock()
        channel.last_message_id = 1200
        channel.send = AsyncMock(return_value=second_panel_msg)
        channel.get_partial_message = MagicMock(return_value=new_panel_msg)
        channel.fetch_message = AsyncMock(return_value=second_panel_msg)
        second_panel_msg.edit = AsyncMock()

        burst_msg = SimpleNamespace(
            id=1200,
            guild=guild,
            channel=SimpleNamespace(id=10),
            author=SimpleNamespace(id=123, bot=False),
            flags=SimpleNamespace(ephemeral=False, loading=False),
        )
        await cog.on_message(burst_msg)
        await asyncio.sleep(0.07)  # 超過 PANEL_BUMP_DELAY (0.05s)，但尚未超過 PANEL_BUMP_COOLDOWN (0.15s)
        check(channel.send.await_count == 0, "冷卻期間內不會短時間重複觸發置底")
        await asyncio.sleep(0.12)  # 冷卻時間到達後才執行置底並清除上一張面板 (id=999)
        check(channel.send.await_count == 1 and new_panel_msg.delete.await_count == 1, "冷卻結束後正常置底並清除舊面板")

        # 4. 播放停止或結束後：立即取消排程中的置底任務，且後續新訊息絕不再觸發置底
        pending_msg = SimpleNamespace(
            id=1800,
            guild=guild,
            channel=SimpleNamespace(id=10),
            author=SimpleNamespace(id=123, bot=False),
            flags=SimpleNamespace(ephemeral=False, loading=False),
        )
        channel.last_message_id = 1800
        await cog.on_message(pending_msg)
        check(GID in cog._panel_bump_tasks, "結束前收到訊息已排程置底任務")
        await cog._update_panel_ended(GID, reason="⏹️ 已停止播放")
        check(GID not in cog._panel_bump_tasks and GID not in cog.panel_messages, "播放停止／結束時立即取消置底任務並移除面板追蹤")

        after_end_msg = SimpleNamespace(
            id=1900,
            guild=guild,
            channel=SimpleNamespace(id=10),
            author=SimpleNamespace(id=123, bot=False),
            flags=SimpleNamespace(ephemeral=False, loading=False),
        )
        await cog.on_message(after_end_msg)
        await asyncio.sleep(0.18)
        check(channel.send.await_count == 1 and GID not in cog._panel_bump_tasks, "播放停止或結束後新訊息不再觸發置底")
    finally:
        music_mod.PANEL_BUMP_DELAY = old_delay
        music_mod.PANEL_BUMP_COOLDOWN = old_cooldown


async def main_async():
    await test_panel_abandon()
    await test_import_flow()
    await test_semaphore_and_timeout()
    await test_cancel()
    await test_errors()
    await test_track_end_guard()
    await test_loop_button_and_no_autoskip()
    await test_sticky_panel_auto_bump()
    await test_session_lost_recovery()


# ---------- 測試 10：Lavalink Session 失效（404）自動復原 ----------
class SessionLostError(Exception):
    def __init__(self):
        self.status = 404
        self.path = "/v4/sessions/ax2kzn2hj4g42fpk/players/1"
        super().__init__(f"Failed to fulfill request to Lavalink: status=404, reason=Not Found, path={self.path}")


class RecoverablePlayer(FakePlayer):
    def __init__(self):
        super().__init__()
        self.play_calls = []
        self.voice_updates = 0
        self.fail_next_play = False

    async def _dispatch_voice_update(self):
        self.voice_updates += 1

    async def play(self, track, **kwargs):
        if self.fail_next_play:
            self.fail_next_play = False
            self.current = None
            raise SessionLostError()
        self.play_calls.append((track, kwargs))
        await super().play(track)


async def test_session_lost_recovery():
    print("\n--- 測試 10: Lavalink Session 失效（404）自動復原 ---")
    check(Music._is_session_lost_error(SessionLostError()), "辨識 404 /v4/sessions/... 為 Session 失效")
    check(not Music._is_session_lost_error(RuntimeError("boom")), "一般例外不會被誤判為 Session 失效")

    # 1. 重置去重：短時間內多次偵測只關閉一次 WebSocket，並清掉舊 session_id
    cog = make_cog(FakePlayer())
    socket = SimpleNamespace(closed=False, close=AsyncMock())
    node = SimpleNamespace(identifier="n1", _session_id="old", _websocket=SimpleNamespace(socket=socket))
    first = await cog._reset_lavalink_session(node, "test")
    second = await cog._reset_lavalink_session(node, "test")
    check(first and not second and socket.close.await_count == 1, "Session 重置受去重保護，只關閉一次 WebSocket")
    check(node._session_id is None, "重置時清除失效的 session_id，重連會取得全新 Session")

    # 2. 中斷點恢復：歌曲播到一半 → 重送語音狀態並從原位置續播
    player = RecoverablePlayer()
    cog = make_cog(player)
    song = fake_track("Mid", identifier="id_mid")
    player._current = song
    player.current = song
    player._active_track = song
    player.position = 60000
    await cog._recover_player_after_session_reset(player)
    check(player.voice_updates == 1, "新 Session 就緒後重送語音狀態")
    check(player.play_calls and player.play_calls[-1][0] is song and player.play_calls[-1][1].get("start") == 60000,
          "從中斷位置 (60 秒) 恢復播放同一首歌")

    # 3. 中斷時已播完 → 直接換下一首（修復「播完沒自動播下一首」）
    player = RecoverablePlayer()
    cog = make_cog(player)
    ended = fake_track("Ended", identifier="id_end")
    nxt = fake_track("Next", identifier="id_next")
    await cog._play_track(player, ended, "測試者", 7)
    player._current = ended
    player.position = 199500  # 長度 200000，已到結尾
    player.song_queue.append((nxt, "測試者", 7))
    await cog._recover_player_after_session_reset(player)
    check(player.current is nxt and not player.song_queue, "中斷時歌曲已播完 → 恢復後自動播放佇列下一首")

    # 4. 自動換歌時遇到 Session 失效：不 DM 開發者，觸發重置，恢復後從頭播放下一首
    player = RecoverablePlayer()
    cog = make_cog(player)
    cog._handle_session_lost = AsyncMock()
    a = fake_track("A", identifier="id_a2")
    b = fake_track("B", identifier="id_b2")
    await cog._play_track(player, a, "測試者", 7)
    player.song_queue.append((b, "測試者", 7))
    player.fail_next_play = True
    await cog.on_wavelink_track_end(SimpleNamespace(player=player, reason="finished", track=a))
    check(cog._handle_session_lost.await_count == 1 and cog.bot.notify_owner_error.await_count == 0,
          "自動換歌遇到 Session 失效時觸發重置且不 DM 開發者")
    player._current = None
    await Music._recover_player_after_session_reset(cog, player)
    check(player.current is b and player.play_calls[-1][1].get("start") == 0, "重連後從頭播放原本要接的下一首")

    # 5. 面板按鈕遇到 404：友善提示、觸發復原、不 DM 開發者
    player = RecoverablePlayer()
    cog = make_cog(player)
    cog._handle_session_lost = AsyncMock()
    view = music_mod.MusicControlView(cog, player=player)
    it = make_interaction()
    it.guild = SimpleNamespace(id=GID, voice_client=player)
    it.client = cog.bot
    it.response.is_done = MagicMock(return_value=False)
    it.response.send_message = AsyncMock()
    await view.on_error(it, SessionLostError(), SimpleNamespace(custom_id="music:skip"))
    msg = it.response.send_message.await_args.args[0] if it.response.send_message.await_args else ""
    check(cog._handle_session_lost.await_count == 1 and cog.bot.notify_owner_error.await_count == 0 and "重新連線" in msg,
          "按鈕遇到 Session 失效時提示使用者並自動復原，不 DM 開發者")

    # 6. 跳過按鈕遇到 404 時直接推進佇列（即使 Lavalink 未送出 TrackEndEvent 也能播下一首）
    player = RecoverablePlayer()
    cog = make_cog(player)
    cur_t = fake_track("StuckTrack", identifier="id_stuck")
    nxt_t = fake_track("NextAfter404", identifier="id_nxt404")
    await cog._play_track(player, cur_t, "測試者", 7)
    player.song_queue.append((nxt_t, "測試者", 7))
    player.skip = AsyncMock(side_effect=SessionLostError())
    view = music_mod.MusicControlView(cog, player=player)
    it2 = make_interaction()
    it2.guild = SimpleNamespace(id=GID, voice_client=player)
    it2.client = cog.bot
    it2.response.defer = AsyncMock()
    await view.skip(it2)
    check(player.current is nxt_t and not player.song_queue, "skip 按鈕遇到 404 時直接觸發換歌流程播放下一首")

    # ----------------------------------------------------------
    print("\n[G] 直播串流自動斷流重連與用戶歌單 SQLite 功能測試")

    # G-1: 直播串流（is_stream=True）播放一段時間後被 Lavalink 觸發 finished，應自動重抓串流並無縫續播，不結束播放
    player = RecoverablePlayer()
    cog = make_cog(player)
    live_t = SimpleNamespace(
        title="24/7 Lofi Live",
        author="Lofi Girl",
        uri="https://www.youtube.com/watch?v=live123",
        identifier="live123",
        length=0,
        is_stream=True,
        is_seekable=False,
        source="youtube",
        artwork=None,
    )
    refreshed_live_t = SimpleNamespace(
        title="24/7 Lofi Live",
        author="Lofi Girl",
        uri="https://www.youtube.com/watch?v=live123",
        identifier="live123",
        length=0,
        is_stream=True,
        is_seekable=False,
        source="youtube",
        artwork=None,
    )
    cog._refresh_live_stream_track = AsyncMock(return_value=refreshed_live_t)
    await cog._play_track(player, live_t, "直播聽眾", 99)
    check(cog._is_live_track(live_t) is True, "_is_live_track 正確識別直播串流")
    await cog.on_wavelink_track_end(SimpleNamespace(player=player, reason="finished", track=live_t))
    check(
        player.current is refreshed_live_t and getattr(player, "_live_retry_count", 0) == 1,
        "直播串流因 HLS 視窗刷新觸發 finished 時自動重連續播，不會自動結束播放",
    )

    # G-1b: 直播串流重連失敗（重試次數達上限或續播異常）時，直接視為直播結束，不向開發者報錯 (不呼叫 notify_owner_error)
    player = RecoverablePlayer()
    cog = make_cog(player)
    cog.bot.notify_owner_error = AsyncMock()
    channel = AsyncMock()
    player.home_channel = channel
    live_t_fail = SimpleNamespace(
        title="24/7 Lofi Live Ended",
        author="Lofi Girl",
        uri="https://www.youtube.com/watch?v=live_ended",
        identifier="live_ended",
        length=0,
        is_stream=True,
        is_seekable=False,
        source="youtube",
        artwork=None,
    )
    cog._refresh_live_stream_track = AsyncMock(side_effect=RuntimeError("Stream ended (404)"))
    cog._update_panel_ended = AsyncMock(return_value=True)
    await cog._play_track(player, live_t_fail, "聽眾A", 123)
    await cog.on_wavelink_track_end(SimpleNamespace(player=player, reason="finished", track=live_t_fail))
    check(
        cog.bot.notify_owner_error.call_count == 0,
        "直播重連失敗時不呼叫 notify_owner_error（不報錯）",
    )
    check(
        cog._update_panel_ended.called is True,
        "直播重連失敗時直接視為直播結束並更新面板為結束狀態",
    )
    check(
        getattr(player, "_live_retry_count", 0) == 0 and getattr(player, "_is_current_stream", False) is False,
        "直播結束後播放器串流狀態與重試計數成功重置",
    )

    # G-1c: 直播中收到 TrackException (404/斷流) 時直接視為直播結束，不向開發者報錯，亦不在頻道發送錯誤訊息
    player = RecoverablePlayer()
    cog = make_cog(player)
    cog.bot.notify_owner_error = AsyncMock()
    channel = AsyncMock()
    player.home_channel = channel
    cog._update_panel_ended = AsyncMock(return_value=True)
    await cog._play_track(player, live_t_fail, "聽眾B", 456)
    exception_payload = SimpleNamespace(
        player=player,
        track=live_t_fail,
        exception={"severity": "fault", "message": "invalid status code: 404", "cause": "404 Not Found"},
    )
    await cog.on_wavelink_track_exception(exception_payload)
    check(
        cog.bot.notify_owner_error.call_count == 0,
        "直播收到 TrackException 時不呼叫 notify_owner_error（不報錯）",
    )
    check(
        channel.send.call_count == 0,
        "直播收到 TrackException 時不在文字頻道發送錯誤訊息",
    )

    # G-2: 用戶歌單 SQLite CRUD、6 碼英數分享碼、重產失效、以及 /music_play 分享碼自動收藏測試
    from cogs.database import (
        create_user_playlist,
        get_user_playlist,
        get_playlist_by_share_code,
        add_tracks_to_user_playlist,
        remove_track_from_user_playlist,
        reorder_track_in_user_playlist,
        regenerate_user_playlist_share_code,
        delete_user_playlist,
        get_saved_playlists_for_user,
        are_tracks_duplicate,
    )

    # 歌曲重複判斷邏輯測試（必須歌曲連結一致才判定為重複）
    t_a1 = fake_track("Idol", identifier="id_idol", author="YOASOBI", uri="https://youtu.be/id_idol")
    t_a2 = fake_track("Idol", identifier="id_idol", author="YOASOBI", uri="https://www.youtube.com/watch?v=id_idol")
    t_a3 = SimpleNamespace(title="idol", author="yoasobi", uri="https://youtu.be/id_idol?si=123")
    t_same_title_diff_url1 = fake_track("Amore", uri="https://www.youtube.com/watch?v=11111111111", identifier="11111111111")
    t_same_title_diff_url2 = fake_track("Amore", uri="https://www.youtube.com/watch?v=22222222222", identifier="22222222222")
    check(are_tracks_duplicate(t_a1, t_a2) is True, "are_tracks_duplicate: 相同 YouTube ID 連結判定重複")
    check(are_tracks_duplicate(t_a1, t_a3) is True, "are_tracks_duplicate: 帶追蹤參數與短網址判定重複")
    check(are_tracks_duplicate(t_same_title_diff_url1, t_same_title_diff_url2) is False, "are_tracks_duplicate: 同歌名但不同連結不判定重複（解決同名誤判）")

    # 播放佇列點歌重複測試：加入播放佇列時不阻擋重複歌曲點播
    player_dup = RecoverablePlayer()
    cog_dup = make_cog(player_dup)
    t_playing = fake_track("PlayingNow", uri="https://x/play_now", identifier="id_play_now")
    await cog_dup._play_track(player_dup, t_playing, "點歌者", 1)
    player_dup._current = t_playing
    player_dup.current = t_playing
    it_dup_play = make_interaction()
    it_dup_play.user = SimpleNamespace(id=1, display_name="點歌者")
    it_dup_play.guild = SimpleNamespace(id=GID, voice_client=player_dup)
    cog_dup._check_music_channel = AsyncMock(return_value=True)
    cog_dup._ensure_player = AsyncMock(return_value=player_dup)
    cog_dup._search_one_track = AsyncMock(return_value=fake_track("PlayingNow", uri="https://x/play_now", identifier="id_play_now"))
    await cog_dup.play.callback(cog_dup, it_dup_play, query="PlayingNow")
    check(len(player_dup.song_queue) == 1, "/music_play 點播重複歌曲不被阻擋，正常加入待播佇列")

    # 測試隨機播放模式（shuffle mode）
    player_shuf = RecoverablePlayer()
    cog_shuf = make_cog(player_shuf)
    view_shuf = music_mod.MusicControlView(cog_shuf, player=player_shuf)
    check(hasattr(view_shuf, "btn_shuffle") and view_shuf.btn_shuffle.custom_id == "music:shuffle", "控制面板具備隨機播放按鈕 (CUSTOM_ID_SHUFFLE)")
    check(view_shuf.btn_shuffle.label == "隨機：關閉" and getattr(player_shuf, "shuffle", False) is False, "隨機播放預設關閉")

    # 點擊開啟隨機播放模式：打亂佇列（不影響當前正在播放曲目）
    t_curr = fake_track("Playing", uri="https://x/playing")
    await cog_shuf._play_track(player_shuf, t_curr, "DJ", 1)
    player_shuf.current = t_curr
    for i in range(10):
        player_shuf.song_queue.append((fake_track(f"QSong-{i}", uri=f"https://x/q{i}"), "DJ", 1))

    it_shuf = make_interaction()
    it_shuf.guild = SimpleNamespace(id=GID, voice_client=player_shuf)
    await view_shuf.toggle_shuffle(it_shuf)
    check(player_shuf.shuffle is True, "點擊按鈕成功切換 player.shuffle 為 True")
    view_shuf.refresh_buttons(player_shuf)
    check(view_shuf.btn_shuffle.label == "隨機：開啟", "按鈕標籤刷新為「隨機：開啟」")
    check(player_shuf.current is t_curr, "隨機播放打亂佇列時，當前正在播放曲目保持不變")

    # /music_play 支援 shuffle 參數
    it_play_shuf = make_interaction()
    it_play_shuf.user = SimpleNamespace(id=1, display_name="點歌者")
    it_play_shuf.guild = SimpleNamespace(id=GID, voice_client=player_shuf)
    cog_shuf._check_music_channel = AsyncMock(return_value=True)
    cog_shuf._ensure_player = AsyncMock(return_value=player_shuf)
    cog_shuf._search_one_track = AsyncMock(return_value=fake_track("NewShufSong", uri="https://x/new_shuf"))
    await cog_shuf.play.callback(cog_shuf, it_play_shuf, query="NewShufSong", shuffle=True)
    check(player_shuf.shuffle is True, "/music_play 帶入 shuffle=True 成功啟用隨機播放模式")

    uid_owner = "999000111222333444"
    uid_listener = 888000111222333444
    delete_user_playlist(uid_owner)
    delete_user_playlist(uid_listener)

    pl = create_user_playlist(uid_owner, "測試精選歌單", owner_name="歌單主人", is_public=False)
    code1 = pl["share_code"]
    check(
        len(code1) == 6 and code1.isalnum() and any(c.isalpha() for c in code1) and any(c.isdigit() for c in code1),
        f"建立歌單自動產生英數混合 6 碼分享碼 ({code1})",
    )

    # 新增 3 首歌曲並測試重新排序與刪除
    sample_tracks = [
        cog._playable_to_playlist_dict(fake_track(f"Song-{i}", identifier=f"id_{i}"))
        for i in range(1, 4)
    ]
    pl, added_n, dup_n, overflow_n = add_tracks_to_user_playlist(uid_owner, sample_tracks, owner_name="歌單主人")
    check(added_n == 3 and dup_n == 0 and len(pl["tracks"]) == 3, "成功新增 3 首歌曲至 SQLite 歌單")

    # 測試歌單新增重複歌曲：自動忽略並回報重複首數
    dup_sample = [
        cog._playable_to_playlist_dict(fake_track("Song-1", identifier="id_1")),
        cog._playable_to_playlist_dict(fake_track("Song-New", identifier="id_new")),
    ]
    pl, added_n2, dup_n2, _ = add_tracks_to_user_playlist(uid_owner, dup_sample, owner_name="歌單主人")
    check(added_n2 == 1 and dup_n2 == 1 and len(pl["tracks"]) == 4, "歌單新增曲目時自動忽略重複歌曲 (dup_count=1, added=1)")

    pl, moved = reorder_track_in_user_playlist(uid_owner, 0, 2)
    check(
        moved["title"] == "Song-1" and [t["title"] for t in pl["tracks"]] == ["Song-2", "Song-3", "Song-1", "Song-New"],
        "重新排序歌單曲目成功更新 SQLite 順序",
    )

    pl, removed = remove_track_from_user_playlist(uid_owner, 1)
    check(
        removed["title"] == "Song-3" and [t["title"] for t in pl["tracks"]] == ["Song-2", "Song-1", "Song-New"],
        "刪除指定編號歌曲成功",
    )

    # 測試跨伺服器透過 6 碼分享碼解析歌單，並自動為該用戶儲存歌單
    it_play = make_interaction()
    it_play.user = SimpleNamespace(id=uid_listener, display_name="跨服聽眾")
    resolved_pl, err_msg, used_code = cog._resolve_playlist_selection(it_play, code1)
    saved_for_listener = get_saved_playlists_for_user(uid_listener)
    check(
        resolved_pl is not None
        and err_msg is None
        and used_code is True
        and any(str(s["user_id"]) == uid_owner for s in saved_for_listener),
        "在 /music_play 輸入 6 碼分享碼可跨服讀取私人歌單，並自動為該用戶儲存至歌單選單",
    )

    # 測試重新產生分享碼後，舊分享碼立即失效，且他人已儲存的舊連結同步失效
    regen_pl = regenerate_user_playlist_share_code(uid_owner)
    code2 = regen_pl["share_code"]
    check(
        code2 != code1
        and get_playlist_by_share_code(code1) is None
        and get_playlist_by_share_code(code2) is not None
        and len(get_saved_playlists_for_user(uid_listener)) == 0,
        f"重新產生分享碼 ({code1} -> {code2}) 後舊分享碼與他人舊收藏立即失效",
    )

    # 測試 PlaylistPaginationView / PlaylistEditView / PlaylistRemoveConfirmView 參數容錯與 slash 指令呼叫
    # 1. 驗證傳入 cog (self) 作為第一參數不會報 TypeError
    pv_cog = music_mod.PlaylistPaginationView(cog, int(uid_owner), page=0)
    check(pv_cog.user_id == int(uid_owner), "PlaylistPaginationView 支援 (cog, user_id, ...) 呼叫且不拋 TypeError")
    pv_direct = music_mod.PlaylistPaginationView(int(uid_owner), page=0)
    check(pv_direct.user_id == int(uid_owner), "PlaylistPaginationView 支援 (user_id, ...) 呼叫")

    ev_cog = music_mod.PlaylistEditView(cog, int(uid_owner), page=0)
    check(ev_cog.user_id == int(uid_owner), "PlaylistEditView 支援 (cog, user_id, ...) 呼叫且不拋 TypeError")
    ev_direct = music_mod.PlaylistEditView(int(uid_owner), page=0)
    check(ev_direct.user_id == int(uid_owner), "PlaylistEditView 支援 (user_id, ...) 呼叫")

    rv_cog = music_mod.PlaylistRemoveConfirmView(cog, int(uid_owner), "測試歌單")
    check(rv_cog.user_id == int(uid_owner) and rv_cog.playlist_name == "測試歌單", "PlaylistRemoveConfirmView 支援 (cog, user_id, ...) 呼叫且不拋 TypeError")
    rv_direct = music_mod.PlaylistRemoveConfirmView(int(uid_owner), "測試歌單")
    check(rv_direct.user_id == int(uid_owner) and rv_direct.playlist_name == "測試歌單", "PlaylistRemoveConfirmView 支援 (user_id, ...) 呼叫")

    # 2. 呼叫 /music_playlist 指令回呼驗證
    it_view = make_interaction()
    it_view.user = SimpleNamespace(id=int(uid_owner), display_name="歌單主人")
    it_view.original_response = AsyncMock()
    await cog.music_playlist_view.callback(cog, it_view)
    check(it_view.response.send_message.called, "/music_playlist 指令正常發送訊息")
    view_call_kwargs = it_view.response.send_message.call_args.kwargs
    sent_embed = view_call_kwargs.get("embed")
    sent_view = view_call_kwargs.get("view")
    check(isinstance(sent_embed, music_mod.discord.Embed), "/music_playlist 送出之 embed 為 discord.Embed 實例（非 tuple）")
    check(isinstance(sent_view, music_mod.PlaylistPaginationView), "/music_playlist 正確掛載 PlaylistPaginationView")
    panel_btn = next((b for b in sent_view.children if "控制面板" in getattr(b, "label", "")), None)
    check(panel_btn is not None and panel_btn.style == music_mod.discord.ButtonStyle.link and "#personal" in panel_btn.url, "/music_playlist 回覆包含指向個人設定的「前往控制面板」按鈕")

    # 2b. 呼叫 /music_playlist（用戶無歌單情況）
    it_noplist = make_interaction()
    it_noplist.user = SimpleNamespace(id=999999999, display_name="無歌單用戶")
    await cog.music_playlist_view.callback(cog, it_noplist)
    check(it_noplist.response.send_message.called, "/music_playlist 無歌單時正常發送提示訊息")
    noplist_kwargs = it_noplist.response.send_message.call_args.kwargs
    noplist_view = noplist_kwargs.get("view")
    noplist_btn = next((b for b in noplist_view.children if "控制面板" in getattr(b, "label", "")), None) if noplist_view else None
    check(noplist_btn is not None and noplist_btn.style == music_mod.discord.ButtonStyle.link and "#personal" in noplist_btn.url, "/music_playlist 無歌單提示訊息下方包含「前往控制面板」按鈕")

    # 3. 呼叫 /music_playlist_edit 指令回呼驗證
    it_edit = make_interaction()
    it_edit.user = SimpleNamespace(id=int(uid_owner), display_name="歌單主人")
    it_edit.original_response = AsyncMock()
    await cog.music_playlist_edit.callback(cog, it_edit, name=None, is_public=None, move_from=None, move_to=None, delete_index=None, regenerate_share_code=False)
    check(it_edit.response.send_message.called, "/music_playlist_edit 指令正常發送訊息")
    edit_call_kwargs = it_edit.response.send_message.call_args.kwargs
    sent_edit_embed = edit_call_kwargs.get("embed")
    sent_edit_view = edit_call_kwargs.get("view")
    check(isinstance(sent_edit_embed, music_mod.discord.Embed), "/music_playlist_edit 送出之 embed 為 discord.Embed 實例（非 tuple）")
    check(isinstance(sent_edit_view, music_mod.PlaylistEditView), "/music_playlist_edit 正確掛載 PlaylistEditView")

    # 4. 呼叫 /music_playlist_remove 指令回呼驗證
    it_remove = make_interaction()
    it_remove.user = SimpleNamespace(id=int(uid_owner), display_name="歌單主人")
    await cog.music_playlist_remove.callback(cog, it_remove)
    check(it_remove.response.send_message.called, "/music_playlist_remove 指令正常發送確認訊息")
    rem_call_kwargs = it_remove.response.send_message.call_args.kwargs
    sent_rem_embed = rem_call_kwargs.get("embed")
    sent_rem_view = rem_call_kwargs.get("view")
    check(isinstance(sent_rem_embed, music_mod.discord.Embed), "/music_playlist_remove 送出之 embed 為 discord.Embed 實例")
    check(isinstance(sent_rem_view, music_mod.PlaylistRemoveConfirmView), "/music_playlist_remove 正確掛載 PlaylistRemoveConfirmView")

    # 5. 測試封面縮圖解析與 Container 右上角排版 (build_now_playing_container)
    from cogs.database import get_track_artwork
    # (a) 驗證 YouTube URL / identifier 自動推導封面縮圖
    t_yt = SimpleNamespace(title="Test YT", uri="https://www.youtube.com/watch?v=dQw4w9WgXcQ", identifier="dQw4w9WgXcQ", artwork=None)
    art_yt = get_track_artwork(t_yt)
    check(art_yt == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg", "get_track_artwork: 成功從 YouTube 連結自動推導封面縮圖")

    # (b) 驗證已有 artwork 者優先保留
    t_sp = SimpleNamespace(title="Test SP", artwork="https://i.scdn.co/image/abc12345")
    check(get_track_artwork(t_sp) == "https://i.scdn.co/image/abc12345", "get_track_artwork: 保留原生 artwork 網址")

    # (c) 驗證 Container 將封面縮圖作為頂部 Section accessory 置於右上角 (小圖)
    player_art = RecoverablePlayer()
    player_art.current = t_yt
    container = music_mod.build_now_playing_container(player_art)
    top_item = container.children[0]
    check(isinstance(top_item, music_mod.ui.Section), "build_now_playing_container: 頂部為 ui.Section 元件")
    check(isinstance(getattr(top_item, "accessory", None), music_mod.ui.Thumbnail), "build_now_playing_container: 頂部 Section 具有 ui.Thumbnail accessory (右上角小圖)")
    thumb_media = getattr(top_item.accessory, "media", None)
    thumb_url = getattr(thumb_media, "url", None) if hasattr(thumb_media, "url") else str(thumb_media)
    check(thumb_url == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg", "build_now_playing_container: 縮圖網址正確對應自動推導之封面")

    # 清理測試歌單資料
    delete_user_playlist(uid_owner)
    delete_user_playlist(uid_listener)
    check(get_user_playlist(uid_owner) is None, "刪除歌單成功清除 SQLite 紀錄")


def main():
    print("=== 開始執行第 7 週匯入與面板單元測試 ===")
    asyncio.run(main_async())
    total = len(PASS) + len(FAIL)
    print(f"\n測試統計：共 {total} 項，{len(PASS)} 通過，{len(FAIL)} 失敗")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()

