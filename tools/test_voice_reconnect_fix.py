"""驗證修復：解決機器人加入語音房時立刻退出導致 30 秒 ChannelTimeoutException 的問題。

涵蓋驗證：
1. cog_unload 時徹底關閉並清空 wavelink.Pool.nodes 中的節點，避免 reload 模組殘留幽靈節點
2. _connect_lavalink 遇到相同 URI 且已連線的節點時避免重複連線；遇到非連線節點時清理舊節點
3. _cleanup_guild_voice_and_player 能正確重置 Discord VoiceClient、發送 Gateway channel=None，並清理 node._players 與 Lavalink 端 player
4. _ensure_player 與 join 在 ChannelTimeoutException 時觸發徹底清理，並回傳友善錯誤提示
"""
import sys
import asyncio
import os
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wavelink
from wavelink import NodeStatus
import cogs.music as music_mod
from cogs.music import Music

PASS = []
FAIL = []

def check(cond: bool, name: str):
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name)


class FakeNode:
    def __init__(self, identifier="test_node", uri="http://test.node:2333", status=NodeStatus.CONNECTED):
        self.identifier = identifier
        self.uri = uri
        self.status = status
        self._players = {}
        self.closed = False
        self.ejected = False
        self.destroyed_players = []

    async def close(self, eject=False):
        self.closed = True
        self.ejected = eject

    async def _destroy_player(self, guild_id):
        self.destroyed_players.append(guild_id)


class FakeBot:
    def __init__(self):
        self.user = MagicMock(id=999999)
        self.notifications = []

    async def notify_owner_error(self, error, interaction=None, extra_info=""):
        self.notifications.append((error, extra_info))


async def test_cog_unload_cleans_pool_nodes():
    bot = FakeBot()
    cog = Music(bot)

    node1 = FakeNode("n1")
    node2 = FakeNode("n2")
    wavelink.Pool.nodes = {"n1": node1, "n2": node2}

    await cog.cog_unload()

    check(node1.closed and node1.ejected, "cog_unload 確實關閉並 eject 節點 1")
    check(node2.closed and node2.ejected, "cog_unload 確實關閉並 eject 節點 2")
    check(len(wavelink.Pool.nodes) == 0, "cog_unload 徹底清空 wavelink.Pool.nodes")


async def test_connect_lavalink_avoids_duplicate_and_cleans_stale():
    bot = FakeBot()
    cog = Music(bot)

    # 1. 已有 CONNECTED 節點時，略過重複連線
    active_node = FakeNode("active", "http://same.uri:2333", NodeStatus.CONNECTED)
    wavelink.Pool.nodes = {"active": active_node}

    with patch.object(wavelink.Pool, "connect", new_callable=AsyncMock) as mock_connect:
        await cog._connect_lavalink("http://same.uri:2333", "pwd")
        check(mock_connect.await_count == 0, "同 URI 且 CONNECTED 節點存在時不重複呼叫 Pool.connect")

    # 2. 舊節點處於離線狀態時，會先 discard 舊節點再連線
    dead_node = FakeNode("dead", "http://same.uri:2333", NodeStatus.DISCONNECTED)
    wavelink.Pool.nodes = {"dead": dead_node}

    with patch.object(wavelink.Pool, "connect", new_callable=AsyncMock) as mock_connect, \
         patch.object(cog, "_discard_failed_node", new_callable=AsyncMock) as mock_discard:
        # 讓新節點連線後出現在 nodes 裡模擬成功
        async def fake_connect(nodes, client):
            for n in nodes:
                wavelink.Pool.nodes[n.identifier] = n
        mock_connect.side_effect = fake_connect

        await cog._connect_lavalink("http://same.uri:2333", "pwd")
        check(mock_discard.await_count >= 1, "同 URI 但離線的舊節點會被 _discard_failed_node 清理")


async def test_cleanup_guild_voice_and_player():
    bot = FakeBot()
    cog = Music(bot)

    guild = MagicMock()
    guild.id = 12345
    vc = AsyncMock()
    guild.voice_client = vc
    guild.change_voice_state = AsyncMock()

    node = FakeNode("n1")
    old_player = AsyncMock()
    node._players[guild.id] = old_player
    wavelink.Pool.nodes = {"n1": node}

    await cog._cleanup_guild_voice_and_player(guild)

    check(vc.disconnect.await_count == 1, "VoiceClient 被 force disconnect")
    check(guild.change_voice_state.await_args.kwargs.get("channel") is None, "Gateway 語音狀態被強制設為 channel=None")
    check(guild.id not in node._players, "node._players 中的舊 player 已被移除")
    check(old_player.disconnect.await_count == 1, "舊 player 被 disconnect")
    check(12345 in node.destroyed_players, "節點的 _destroy_player 被調用清理 Lavalink 狀態")


async def test_join_handles_timeout_and_cleans():
    bot = FakeBot()
    cog = Music(bot)

    interaction = AsyncMock()
    guild = MagicMock()
    guild.id = 12345
    guild.voice_client = None
    interaction.guild = guild
    interaction.user.voice.channel = AsyncMock()
    channel = interaction.user.voice.channel

    # 模擬 channel.connect 拋出 ChannelTimeoutException
    channel.connect.side_effect = wavelink.exceptions.ChannelTimeoutException("Unable to connect exceeded timeout of 30.0 seconds")

    with patch.object(cog, "_check_music_channel", return_value=True), \
         patch.object(cog, "_is_any_node_connected", return_value=(True, "")), \
         patch.object(cog, "_cleanup_guild_voice_and_player", new_callable=AsyncMock) as mock_cleanup:
        
        await cog.join.callback(cog, interaction)

        check(mock_cleanup.await_count >= 1, "發生 ChannelTimeoutException 時觸發 _cleanup_guild_voice_and_player")
        check(interaction.followup.send.await_count == 1, "已透過 followup 送出訊息給使用者")
        sent_msg = interaction.followup.send.await_args.args[0]
        check("交握逾時" in sent_msg and "自動清理重置" in sent_msg, "回傳友善的中文交握逾時與重置提示")


async def main():
    print("=== 執行語音連線逾時與節點清理測試 ===")
    await test_cog_unload_cleans_pool_nodes()
    await test_connect_lavalink_avoids_duplicate_and_cleans_stale()
    await test_cleanup_guild_voice_and_player()
    await test_join_handles_timeout_and_cleans()

    print(f"\n測試結果：共 {len(PASS) + len(FAIL)} 項，{len(PASS)} 通過，{len(FAIL)} 失敗")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
