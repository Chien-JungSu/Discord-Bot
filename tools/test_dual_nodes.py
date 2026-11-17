"""第 8 週單元測試：驗證雙 Lavalink 節點容錯、最佳節點選擇與故障轉移（純離線模擬）。

涵蓋項目：
1. 環境變數讀取（雙節點、單節點降級、向下相容 LAVALINK_URI、無設定警告、密碼缺漏保護）
2. 純函式 pick_best_node()（CONNECTED 過濾、玩家數優先、CPU／記憶體負載次之、無負載退回、exclude 排除）
3. 指數退避計算 compute_reconnect_backoff()（上限 60 秒）
4. 啟動雙節點部分失敗隔離、新玩家建置依 pick_best_node() 選點與連線備援切換
5. 多來源與多節點搜尋 fallback（YouTube -> SoundCloud）及友善中文錯誤分類
6. 節點斷線故障轉移（on_wavelink_node_closed / on_wavelink_node_disconnected）：
   - 保留曲目、從原位置（position）續播、保留佇列／循環／音量／頻道
   - per-guild asyncio.Lock 防止雙重事件重複觸發轉移
   - 故障轉移失敗時優雅清理並通知文字頻道
7. 雙節點全斷處理與指數退避自動重連
8. /music_node_status（含離線節點）、/node_kill、/node_revive 權限與行為驗證
9. 低資源輪詢間隔檢查（NODE_HEALTH_CHECK_INTERVAL >= 30 秒）

執行方式：.venv/Scripts/python.exe tools/test_dual_nodes.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import asyncio
import os
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import wavelink
from wavelink import NodeStatus

import cogs.music as music_mod
from cogs.music import Music, _owner_only_check
from utils.nodes import (
    LavalinkNodeConfig,
    compute_reconnect_backoff,
    extract_node_state,
    load_lavalink_node_configs,
    pick_best_node,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(cond: bool, name: str) -> None:
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name)


class FakeBot:
    def __init__(self, owner_id: int = 999001):
        self.owner_id = owner_id
        self.user = SimpleNamespace(id=888001)
        self.voice_clients: list = []
        self.guilds_map: dict[int, object] = {}
        self.channels_map: dict[int, object] = {}
        self.notifications: list[dict] = []

    def get_guild(self, guild_id: int):
        return self.guilds_map.get(guild_id)

    def get_channel(self, channel_id: int):
        return self.channels_map.get(channel_id)

    async def notify_owner_error(self, error, interaction=None, extra_info=""):
        self.notifications.append({"error": error, "extra_info": extra_info})


def make_interaction(user_id: int = 999001, guild_id: int | None = 1001, *, client=None):
    it = MagicMock(spec=discord.Interaction)
    it.client = client or FakeBot(owner_id=999001)
    it.user = SimpleNamespace(id=user_id, display_name="管理員")
    it.guild_id = guild_id
    it.channel_id = 5001
    it.guild = SimpleNamespace(id=guild_id) if guild_id else None
    it.response = MagicMock()
    it.response.is_done = MagicMock(return_value=False)
    it.response.defer = AsyncMock()
    it.response.send_message = AsyncMock()
    it.followup = MagicMock()
    it.followup.send = AsyncMock()
    it.delete_original_response = AsyncMock()
    return it


def make_fake_track(title="測試歌曲", author="歌手A", uri="https://yt.example/watch?v=abc", length=240_000, encoded="ENC1"):
    return SimpleNamespace(
        title=title,
        author=author,
        uri=uri,
        identifier=uri,
        length=length,
        is_stream=False,
        is_seekable=True,
        source="youtube",
        artwork=None,
        encoded=encoded,
    )


def make_fake_node(
    identifier: str,
    uri: str,
    status=NodeStatus.CONNECTED,
    *,
    players=None,
    total_players: int | None = None,
    cpu_lavalink: float | None = 0.1,
    cpu_system: float | None = 0.2,
    mem_used: int | None = 100 * 1024 * 1024,
    mem_res: int | None = 512 * 1024 * 1024,
):
    node = SimpleNamespace(
        identifier=identifier,
        uri=uri,
        status=status,
        session_id=f"sess_{identifier}" if status == NodeStatus.CONNECTED else None,
        _session_id=f"sess_{identifier}" if status == NodeStatus.CONNECTED else None,
        _players=dict(players or {}),
        _total_player_count=total_players,
        _inactive_channel_tokens=3,
        _inactive_player_timeout=300,
        client=None,
    )
    node.players = node._players

    async def _fetch_stats():
        return SimpleNamespace(
            players=total_players if total_players is not None else len(node._players),
            playing=len(node._players),
            uptime=123456,
            cpu=SimpleNamespace(cores=2, system_load=cpu_system, lavalink_load=cpu_lavalink),
            memory=SimpleNamespace(
                used=mem_used,
                allocated=mem_res,
                reservable=mem_res,
                free=(mem_res - mem_used) if (mem_res and mem_used) else None,
            ),
        )

    async def _fetch_players():
        return []

    async def _close(eject: bool = False):
        node.status = NodeStatus.DISCONNECTED
        node.session_id = None
        node._session_id = None
        node._players.clear()
        if eject:
            wavelink.Pool.nodes.pop(identifier, None)

    node.fetch_stats = AsyncMock(side_effect=_fetch_stats)
    node.fetch_players = AsyncMock(side_effect=_fetch_players)
    node._destroy_player = AsyncMock()
    node.close = AsyncMock(side_effect=_close)
    return node


# ---------- 1. 環境變數載入測試 ----------
def test_env_loading():
    print("\n--- 測試 1: load_lavalink_node_configs 環境變數載入 ---")
    cfgs = load_lavalink_node_configs(
        {
            "LAVALINK_NODE1_URI": "https://n1.example.com:443/",
            "LAVALINK_NODE1_PASSWORD": "pw1",
            "LAVALINK_NODE1_IDENTIFIER": "us-east",
            "LAVALINK_NODE2_URI": "http://n2.example.com:2333",
            "LAVALINK_NODE2_PASSWORD": "pw2",
            "LAVALINK_NODE2_IDENTIFIER": "eu-west",
        },
        log_warnings=False,
    )
    check(
        len(cfgs) == 2
        and cfgs[0].identifier == "us-east"
        and cfgs[0].uri == "https://n1.example.com:443"
        and cfgs[1].identifier == "eu-west",
        "雙節點環境變數正確解析且去除尾端斜線",
    )

    cfgs_one = load_lavalink_node_configs(
        {"LAVALINK_NODE1_URI": "http://n1.example.com:2333", "LAVALINK_NODE1_PASSWORD": "pw1"},
        log_warnings=False,
    )
    check(len(cfgs_one) == 1 and cfgs_one[0].identifier == "node1", "僅設 Node 1 時降級為單節點且套用預設識別名稱")

    cfgs_node2_only = load_lavalink_node_configs(
        {"LAVALINK_NODE2_URI": "http://n2.example.com:2333", "LAVALINK_NODE2_PASSWORD": "pw2"},
        log_warnings=False,
    )
    check(len(cfgs_node2_only) == 1 and cfgs_node2_only[0].identifier == "node2", "僅設 Node 2 時也能降級正常運作")

    cfgs_legacy = load_lavalink_node_configs(
        {"LAVALINK_URI": "http://legacy.example:2333", "LAVALINK_PASSWORD": "pw"},
        log_warnings=False,
    )
    check(
        len(cfgs_legacy) == 1 and cfgs_legacy[0].from_legacy_env and cfgs_legacy[0].uri == "http://legacy.example:2333",
        "未設 NODE1 時向下相容讀取 LAVALINK_URI / LAVALINK_PASSWORD",
    )

    cfgs_empty = load_lavalink_node_configs({}, log_warnings=False)
    check(cfgs_empty == [], "未設定任何節點時回傳空清單且不崩潰")

    cfgs_no_pw = load_lavalink_node_configs({"LAVALINK_NODE1_URI": "http://n1.example:2333"}, log_warnings=False)
    check(cfgs_no_pw == [], "有 URI 缺密碼時安全略過該節點")

    cfgs_dup_id = load_lavalink_node_configs(
        {
            "LAVALINK_NODE1_URI": "http://n1.example:2333",
            "LAVALINK_NODE1_PASSWORD": "pw1",
            "LAVALINK_NODE1_IDENTIFIER": "same",
            "LAVALINK_NODE2_URI": "http://n2.example:2333",
            "LAVALINK_NODE2_PASSWORD": "pw2",
            "LAVALINK_NODE2_IDENTIFIER": "same",
        },
        log_warnings=False,
    )
    check(len(cfgs_dup_id) == 2 and cfgs_dup_id[0].identifier != cfgs_dup_id[1].identifier, "重複 identifier 自動去重避免 Pool 衝突")


# ---------- 2. 純函式 pick_best_node() 與退避測試 ----------
def test_pick_best_node_and_backoff():
    print("\n--- 測試 2: pick_best_node() 與 compute_reconnect_backoff() ---")
    check(
        pick_best_node({"n1": {"status": "DISCONNECTED", "players": 0}, "n2": {"status": "CONNECTING", "players": 0}}) is None,
        "無 CONNECTED 節點時回傳 None",
    )

    check(
        pick_best_node(
            {
                "n1": {"status": "DISCONNECTED", "players": 0, "cpu_load": 0.01},
                "n2": {"status": "CONNECTED", "players": 5, "cpu_load": 0.8},
            }
        ) == "n2",
        "只考慮 CONNECTED 節點（略過玩家數 0 但離線的 n1）",
    )

    check(
        pick_best_node(
            {
                "n1": {"status": "CONNECTED", "players": 3, "cpu_load": 0.05, "memory_ratio": 0.1},
                "n2": {"status": "CONNECTED", "players": 1, "cpu_load": 0.90, "memory_ratio": 0.9},
            }
        ) == "n2",
        "玩家數為主要考量：玩家數少者勝出（忽略 CPU 較低但玩家數較多的節點）",
    )

    check(
        pick_best_node(
            {
                "n1": {"status": "CONNECTED", "players": 2, "cpu_load": 0.65, "memory_ratio": 0.70},
                "n2": {"status": "CONNECTED", "players": 2, "cpu_load": 0.20, "memory_ratio": 0.30},
            }
        ) == "n2",
        "玩家數相同時，CPU／記憶體負載較低者勝出",
    )

    check(
        pick_best_node(
            {
                "n1": {"status": "CONNECTED", "players": 1, "memory_used": 400, "memory_reservable": 500},
                "n2": {"status": "CONNECTED", "players": 1, "memory_used": 100, "memory_reservable": 500},
            }
        ) == "n2",
        "僅取得記憶體使用率時，記憶體負載較低者勝出",
    )

    check(
        pick_best_node(
            {
                "n1": {"status": "CONNECTED", "players": 4},
                "n2": {"status": "CONNECTED", "players": 1},
            }
        ) == "n2",
        "取不到 CPU／記憶體負載時，安全退回只比較玩家數",
    )

    check(
        pick_best_node(
            {
                "n1": {"status": "CONNECTED", "players": 1, "cpu_load": 0.9},
                "n2": {"status": "CONNECTED", "players": 1},
            }
        ) == "n1",
        "部分節點缺少負載資料時，退回只比玩家數且不拋例外",
    )

    check(
        pick_best_node(
            {
                "n1": {"status": "CONNECTED", "players": 0, "cpu_load": 0.1},
                "n2": {"status": "CONNECTED", "players": 3, "cpu_load": 0.5},
            },
            exclude={"n1"},
        ) == "n2",
        "exclude 參數正確排除指定節點",
    )

    seq = [compute_reconnect_backoff(i, base=5.0, cap=60.0) for i in range(7)]
    check(seq == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0], f"指數退避計算正確且上限 60 秒 ({seq})")


# ---------- 3. 啟動並發連線與局部失敗隔離 ----------
async def test_startup_partial_failure():
    print("\n--- 測試 3: 雙節點啟動連線（Node 1 失敗時 Node 2 仍成功連線） ---")
    pool_nodes = getattr(wavelink.Pool, "_Pool__nodes")
    orig_connect = wavelink.Pool.__dict__["connect"]
    orig_diagnose = music_mod.diagnose_lavalink_node
    pool_nodes.clear()

    async def fake_diagnose(uri, pw, timeout=5.0):
        return f"模擬診斷：{uri}"

    async def partial_connect(*, nodes, client=None, cache_capacity=None):
        for n in nodes:
            if "fail-node" in n.uri:
                raise ConnectionRefusedError("node1 refused")
            n._status = NodeStatus.CONNECTED
            n._session_id = f"sess_{n.identifier}"
            pool_nodes[n.identifier] = n
        return dict(pool_nodes)

    music_mod.diagnose_lavalink_node = fake_diagnose
    wavelink.Pool.connect = partial_connect
    try:
        bot = FakeBot()
        cog = Music(bot)
        cfgs = [
            LavalinkNodeConfig(identifier="node1", uri="http://fail-node.test:2333", password="p1", slot="node1"),
            LavalinkNodeConfig(identifier="node2", uri="http://good-node.test:2333", password="p2", slot="node2"),
        ]
        cog._configured_nodes = {c.identifier: c for c in cfgs}
        await cog._connect_all_nodes(cfgs)
        check(
            "node2" in pool_nodes and "node1" not in pool_nodes and len(bot.notifications) == 1,
            "Node 1 連線失敗時不影響 Node 2 正常註冊進 Pool，且不崩潰",
        )
    finally:
        for leftover in list(pool_nodes.values()):
            sess = getattr(leftover, "_session", None)
            if sess is not None and not sess.closed:
                await sess.close()
        pool_nodes.clear()
        wavelink.Pool.connect = orig_connect
        music_mod.diagnose_lavalink_node = orig_diagnose


# ---------- 4. 新玩家建置選點與多節點搜尋容錯 ----------
async def test_selection_and_search_failover():
    print("\n--- 測試 4: 玩家選點與搜尋跨節點／來源容錯 ---")
    pool_nodes = getattr(wavelink.Pool, "_Pool__nodes")
    pool_nodes.clear()
    n1 = make_fake_node("node1", "http://n1.test:2333", total_players=4, cpu_lavalink=0.7)
    n2 = make_fake_node("node2", "http://n2.test:2333", total_players=1, cpu_lavalink=0.2)
    pool_nodes["node1"] = n1
    pool_nodes["node2"] = n2

    try:
        bot = FakeBot()
        cog = Music(bot)
        picked = cog._pick_best_node()
        check(picked is n2, "_pick_best_node() 正確選出玩家數與負載較低的 node2")

        # 4a. _connect_voice_channel 將 chosen_node (node2) 傳給 Player 工廠
        guild = SimpleNamespace(id=2001, voice_client=None, change_voice_state=AsyncMock())
        captured_nodes = []

        async def fake_vc_connect(*, cls=None):
            p = cls(bot, SimpleNamespace(id=9001, guild=guild))
            captured_nodes.append(p.node)
            return p

        voice_ch = SimpleNamespace(id=9001, guild=guild, connect=fake_vc_connect)
        player = await cog._connect_voice_channel(guild, voice_ch)
        check(captured_nodes and captured_nodes[0] is n2, "_connect_voice_channel 建立新 Player 時綁定至 pick_best_node() 所選節點")
        check(cog._guild_active_node.get(2001) == "node2", "成功連線後正確記錄伺服器目前使用節點")

        # 4b. 搜尋容錯：node2 ytsearch 失敗 -> node1 ytsearch 失敗 -> node2 scsearch 成功
        orig_search = wavelink.Playable.search
        search_calls: list[tuple[str, str | None, str | None]] = []

        async def multi_node_search(query, *, source=None, node=None):
            nid = getattr(node, "identifier", None)
            search_calls.append((query, source, nid))
            if source == "ytsearch":
                raise RuntimeError("YouTube blocked on public node")
            if source == "scsearch" and nid == "node2":
                return [make_fake_track(title="SoundCloud 備援曲")]
            return []

        wavelink.Playable.search = multi_node_search
        try:
            res = await cog._fetch_tracks_with_fallback("測試關鍵字")
            check(
                bool(res) and res[0].title == "SoundCloud 備援曲" and search_calls[0][1] == "ytsearch",
                "搜尋先嘗試 ytsearch（跨節點），失敗後自動 fallback 到 SoundCloud",
            )
        finally:
            wavelink.Playable.search = orig_search

        # 4c. 例外分類友善中文訊息（地區限制／無效網址／未知例外）
        region_err = wavelink.LavalinkLoadException(
            data={"message": "Video not available in your country", "severity": "common", "cause": "region blocked"}
        )
        msg_region, notify_region = cog._classify_search_exception(region_err)
        check("地區限制" in msg_region and notify_region is False, "地區限制回傳友善中文提示且不洗版通報開發者")

        msg_unknown, notify_unknown = cog._classify_search_exception(RuntimeError("boom"))
        check("發生錯誤" in msg_unknown and notify_unknown is True, "未知例外標記須通報開發者")
    finally:
        pool_nodes.clear()


# ---------- 5. 故障轉移、per-guild Lock 與全斷指數退避 ----------
async def test_failover_and_locks():
    print("\n--- 測試 5: 斷線故障轉移（含 /node_kill、per-guild Lock、全斷退避） ---")
    pool_nodes = getattr(wavelink.Pool, "_Pool__nodes")
    pool_nodes.clear()

    bot = FakeBot()
    cog = Music(bot)
    text_ch = MagicMock()
    text_ch.id = 8801
    fake_panel_msg = SimpleNamespace(id=9901, edit=AsyncMock())
    text_ch.send = AsyncMock(return_value=fake_panel_msg)
    text_ch.fetch_message = AsyncMock(return_value=fake_panel_msg)
    text_ch.get_partial_message = MagicMock(return_value=fake_panel_msg)
    bot.channels_map[8801] = text_ch

    track1 = make_fake_track(title="轉移測試曲", uri="https://yt.example/watch?v=stay", length=300_000, encoded="OLD_ENC")
    track2 = make_fake_track(title="佇列下一首", uri="https://yt.example/watch?v=next", length=180_000, encoded="NEXT_ENC")

    guild = SimpleNamespace(id=3001, voice_client=None, change_voice_state=AsyncMock(), me=None)
    bot.guilds_map[3001] = guild

    rebuilt_players: list = []

    class FakeVoiceChannel:
        def __init__(self, cid=7701):
            self.id = cid
            self.guild = guild
            self.mention = f"<#{cid}>"
            self.members = [SimpleNamespace(id=123, bot=False)]

        async def connect(self, *, cls=None):
            p = cls(bot, self)
            p._guild = guild
            p.channel = self
            p._connected = True
            p._volume = 100
            p._paused = False
            p.play_calls = []

            async def fake_play(trk, *, replace=True, start=0, end=None, volume=None, paused=None):
                p.play_calls.append({"track": trk, "start": start, "volume": volume, "paused": paused})
                p._current = trk
                p._last_raw_lavalink_pos = start
                return trk

            async def fake_disconnect(**kw):
                p._connected = False

            p.play = fake_play
            p.disconnect = fake_disconnect
            guild.voice_client = p
            rebuilt_players.append(p)
            return p

    vc_channel = FakeVoiceChannel()

    n1 = make_fake_node("node1", "http://n1.test:2333", total_players=1)
    n2 = make_fake_node("node2", "http://n2.test:2333", total_players=0)
    pool_nodes["node1"] = n1
    pool_nodes["node2"] = n2

    old_player = SimpleNamespace(
        guild=guild,
        channel=vc_channel,
        home_channel=text_ch,
        node=n1,
        connected=False,  # 模擬 Node.close() 已先將 _connected 設 False
        current=track1,
        _active_track=track1,
        _started_track=track1,
        position=0,       # 模擬斷線後 wavelink player.position 歸零
        _last_raw_lavalink_pos=45_200,
        _last_lavalink_update_mono=time.monotonic(),
        paused=False,
        volume=65,
        loop_mode="all",
        shuffle=True,
        song_queue=deque([(track2, "點歌人", 42)]),
        current_requester="點歌人",
        current_requester_id=42,
        disconnect=AsyncMock(),
    )
    n1._players[3001] = old_player
    guild.voice_client = old_player
    cog._guild_active_node[3001] = "node1"
    cog._configured_nodes = {
        "node1": LavalinkNodeConfig("node1", "http://n1.test:2333", "pw1", "node1"),
        "node2": LavalinkNodeConfig("node2", "http://n2.test:2333", "pw2", "node2"),
    }

    # 模擬 Node.close() 會把 old_player 斷線並清空 _players，然後同時觸發 disconnected 與 closed
    async def close_n1(eject=False):
        n1.status = NodeStatus.DISCONNECTED
        n1._players.clear()
        guild.voice_client = None

    n1.close = AsyncMock(side_effect=close_n1)

    orig_search = wavelink.Playable.search

    async def reload_on_n2(query, *, source=None, node=None):
        if node is n2 and "stay" in query:
            return [make_fake_track(title="轉移測試曲(重載)", uri=query, length=300_000, encoded="NEW_ENC_N2")]
        return []

    wavelink.Playable.search = reload_on_n2
    try:
        # 5a. 使用 /node_kill 測試故障轉移
        inter_owner = make_interaction(user_id=999001, guild_id=3001)
        await cog.node_kill.callback(cog, inter_owner, "node1")
        check(n1.status == NodeStatus.DISCONNECTED, "/node_kill 成功將 node1 切換為 DISCONNECTED")
        check("node1" in cog._manually_killed_nodes, "/node_kill 標記手動停用，避免健康檢查搶先自動復活")
        check(len(rebuilt_players) == 1, "故障轉移建立正好 1 個新 Player")

        new_p = rebuilt_players[0]
        check(new_p.node is n2, "新 Player 成功切換至 node2")
        check(new_p.channel is vc_channel, "新 Player 重新加入相同語音頻道")
        check(len(new_p.play_calls) == 1, "新 Player 成功重新開始播放")
        call0 = new_p.play_calls[0]
        check(call0["track"].encoded == "NEW_ENC_N2", "於目標節點重新載入曲目")
        check(45_000 <= call0["start"] <= 48_000, f"正確從原播放位置附近續播（實際 start={call0['start']} ms）")
        check(call0["volume"] == 65 and new_p.loop_mode == "all" and len(new_p.song_queue) == 1, "保留音量、循環模式與待播佇列")
        sent_texts = [c.args[0] for c in text_ch.send.await_args_list if c.args]
        check(any("node2" in t for t in sent_texts), "通知文字頻道已完成故障轉移")

        # 5b. 模擬重複觸發 on_wavelink_node_disconnected + on_wavelink_node_closed（per-guild Lock 去重）
        await asyncio.gather(
            cog.on_wavelink_node_disconnected(SimpleNamespace(node=n1)),
            cog.on_wavelink_node_closed(n1, [old_player]),
        )
        check(len(rebuilt_players) == 1, "per-guild asyncio.Lock 防止重複觸發故障轉移（仍維持 1 個新 Player）")

        # 5c. 測試 /node_revive 恢復 node1
        orig_pool_connect = wavelink.Pool.__dict__["connect"]

        async def revive_connect(*, nodes, client=None, cache_capacity=None):
            for n in nodes:
                n._status = NodeStatus.CONNECTED
                n._session_id = f"revived_{n.identifier}"
                n.fetch_stats = AsyncMock(side_effect=make_fake_node(n.identifier, n.uri).fetch_stats)
                pool_nodes[n.identifier] = n
            return dict(pool_nodes)

        wavelink.Pool.connect = revive_connect
        try:
            inter_revive = make_interaction(user_id=999001, guild_id=3001)
            await cog.node_revive.callback(cog, inter_revive, "node1")
            check("node1" not in cog._manually_killed_nodes and "node1" in pool_nodes, "/node_revive 成功清除手動停用旗標並重連 node1")
            check(
                getattr(getattr(pool_nodes["node1"], "status", None), "name", None) == "CONNECTED",
                "/node_revive 後 node1 狀態恢復 CONNECTED",
            )
        finally:
            for leftover in list(pool_nodes.values()):
                sess = getattr(leftover, "_session", None)
                if sess is not None and not sess.closed:
                    await sess.close()
            wavelink.Pool.connect = orig_pool_connect

        # 5d. 雙節點皆斷線情境：優雅結束並通知使用者無可用節點，並確認背景退避重連可執行
        pool_nodes["node1"] = make_fake_node("node1", "http://n1.test:2333", status=NodeStatus.DISCONNECTED)
        pool_nodes["node2"] = make_fake_node("node2", "http://n2.test:2333", status=NodeStatus.DISCONNECTED)
        text_ch.send.reset_mock()
        snap_before = cog._snapshot_player_state(new_p, dead_node_id="node2")
        res_none = await cog._failover_single_guild(3001, pool_nodes["node2"], "node2", snap_before)
        check(res_none["status"] == "no_node", "雙節點皆斷線時回傳 no_node 狀態不崩潰")
        sent_none_texts = [c.args[0] for c in text_ch.send.await_args_list if c.args]
        check(any("沒有其他可用節點" in t for t in sent_none_texts), "雙節點全斷時明確通知文字頻道")
        ok_ready, err_ready = cog._is_any_node_connected()
        check(not ok_ready and "指數退避" in err_ready, "_is_any_node_connected 回傳無可用節點與重連提示")
        if cog._all_nodes_reconnect_task and not cog._all_nodes_reconnect_task.done():
            cog._all_nodes_reconnect_task.cancel()
            try:
                await cog._all_nodes_reconnect_task
            except asyncio.CancelledError:
                pass

        # 5e. 故障轉移過程中目標節點連線失敗 -> 優雅清理並通知文字頻道
        pool_nodes["node1"] = make_fake_node("node1", "http://n1.test:2333", status=NodeStatus.DISCONNECTED)
        pool_nodes["node2"] = make_fake_node("node2", "http://n2.test:2333", status=NodeStatus.CONNECTED)
        text_ch.send.reset_mock()
        broken_vc = SimpleNamespace(id=7702, guild=guild, connect=AsyncMock(side_effect=RuntimeError("vc connect failed")))
        fail_snap = {
            "guild": guild,
            "guild_id": 3001,
            "channel": broken_vc,
            "home_channel": text_ch,
            "track": track1,
            "position_ms": 10_000,
            "paused": False,
            "volume": 80,
            "loop_mode": "off",
            "shuffle": False,
            "song_queue": [],
            "requester_name": "點歌人",
            "requester_id": 42,
            "node_id": "node1",
        }
        res_fail = await cog._failover_single_guild(3001, pool_nodes["node1"], "node1", fail_snap)
        check(res_fail["status"] == "failed", "故障轉移失敗時回傳 failed 狀態且不拋未處理例外")
        sent_fail_texts = [c.args[0] for c in text_ch.send.await_args_list if c.args]
        check(any("發生錯誤" in t for t in sent_fail_texts), "故障轉移失敗時通知伺服器文字頻道並優雅結束")
    finally:
        wavelink.Playable.search = orig_search
        pool_nodes.clear()


# ---------- 6. /music_node_status（含離線節點）與權限／低資源檢查 ----------
async def test_status_permissions_and_intervals():
    print("\n--- 測試 6: /music_node_status 離線節點顯示、擁有者權限與輪詢間隔 ---")
    pool_nodes = getattr(wavelink.Pool, "_Pool__nodes")
    pool_nodes.clear()

    bot = FakeBot(owner_id=999001)
    cog = Music(bot)
    cog._configured_nodes = {
        "node1": LavalinkNodeConfig("node1", "http://n1.test:2333", "SECRET_PW_1", "node1"),
        "node2": LavalinkNodeConfig("node2", "http://n2.test:2333", "SECRET_PW_2", "node2"),
    }
    # 故意只讓 node1 在 Pool 中且 CONNECTED；node2 不在 Pool（離線）
    pool_nodes["node1"] = make_fake_node("node1", "http://n1.test:2333", total_players=3, cpu_lavalink=0.15)
    cog._node_last_errors["node2"] = "連線逾時（測試用）"

    try:
        inter_status = make_interaction(user_id=111222, guild_id=None)  # 一般用戶於全域／DM 呼叫
        await cog.node_status.callback(cog, inter_status)
        check(inter_status.followup.send.await_count == 1, "/music_node_status 全域呼叫正常回應")
        embed = inter_status.followup.send.await_args.kwargs.get("embed")
        check(embed is not None and len(embed.fields) == 2, "/music_node_status Embed 同時列出在線與離線節點（共 2 個）")
        field_text = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
        check("node1" in field_text and "node2" in field_text, "Embed 含 node1 與離線的 node2")
        check("ms" in field_text and "CPU" in field_text and "記憶體" in field_text, "Embed 顯示延遲、CPU 與記憶體負載")
        check("SECRET_PW_1" not in field_text and "SECRET_PW_2" not in field_text, "Embed 絕不洩漏節點密碼")

        # 擁有者權限檢查
        check(_owner_only_check(make_interaction(user_id=999001, client=bot)) is True, "OWNER_ID 擁有者通過 _owner_only_check")
        check(_owner_only_check(make_interaction(user_id=555666, client=bot)) is False, "非擁有者遭 _owner_only_check 攔截")

        # 低資源輪詢間隔 >= 30 秒
        check(music_mod.NODE_HEALTH_CHECK_INTERVAL >= 30.0, f"NODE_HEALTH_CHECK_INTERVAL >= 30 秒（實際 {music_mod.NODE_HEALTH_CHECK_INTERVAL}s）")
    finally:
        pool_nodes.clear()


async def async_main():
    test_env_loading()
    test_pick_best_node_and_backoff()
    await test_startup_partial_failure()
    await test_selection_and_search_failover()
    await test_failover_and_locks()
    await test_status_permissions_and_intervals()


def main():
    print("=== 開始執行第 8 週雙 Lavalink 節點容錯單元測試 ===")
    asyncio.run(async_main())
    total = len(PASS) + len(FAIL)
    print(f"\n測試統計：共 {total} 項，{len(PASS)} 通過，{len(FAIL)} 失敗")
    if FAIL:
        print("失敗項目：")
        for name in FAIL:
            print(f"  - {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
