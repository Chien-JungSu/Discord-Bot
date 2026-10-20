"""第 5 週單元測試：驗證 cogs/music.py 互動式 UI 控制面板與持久化 View。

涵蓋項目：
1. 持久化 View（MusicControlView）：
   - timeout 為 None。
   - 所有 5 顆按鈕具備固定且唯一的 custom_id。
2. refresh_buttons 動態按鈕狀態切換：
   - 無曲目/未播放時全部按鈕 disabled。
   - 播放中顯示「暫停」（藍色 primary），暫停中顯示「繼續」（綠色 success）。
   - 佇列為空時切歌按鈕 disabled，佇列有歌曲時 enabled。
   - 音量 0% 時「−」disabled；音量 100% 時「+」disabled；中間狀態皆 enabled。
3. interaction_check 權限防呆與重啟優雅處理：
   - Bot 重啟後（Player 不存在）：發送 ephemeral「目前沒有播放中的內容」，按鈕全 disabled，不拋出例外。
   - 使用者與 Bot 不在同一個語音頻道：以 ephemeral 提示拒絕。
   - 使用者在同語音頻道：通過檢查。
4. 多伺服器隔離：
   - panel_messages 字典按 guild_id 隔離，互不干擾。
5. on_error 錯誤回報：
   - 異常時正確調用 bot.notify_owner_error。

執行方式：.venv/Scripts/python.exe tools/test_music_panel.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import os
from collections import deque
from unittest.mock import AsyncMock, MagicMock

# 專案根目錄載入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import cogs.music as music
from cogs.music import MusicControlView, Music

PASS: list[str] = []
FAIL: list[str] = []


def check(cond: bool, name: str):
    (PASS if cond else FAIL).append(name)
    print(("✅" if cond else "❌") + f" {name}")


class FakePlayer:
    """模擬 wavelink.Player 的狀態。"""
    def __init__(self, current=None, song_queue=None, paused=False, volume=100, channel=None, guild=None):
        self.current = current
        self.song_queue = song_queue if song_queue is not None else deque()
        self.paused = paused
        self.volume = volume
        self.channel = channel
        self.guild = guild
        self.home_channel = None
        self.loop_mode = "off"
        self.current_requester = "測試者"


class FakeTrack:
    """模擬 wavelink.Playable。"""
    def __init__(self, title="測試歌曲", author="測試歌手", length=180000, uri="https://example.com"):
        self.title = title
        self.author = author
        self.length = length
        self.uri = uri
        self.artwork = None


async def test_view_persistence():
    """測試 1: View 持久化屬性與按鈕 custom_id。"""
    view = MusicControlView()
    check(view.timeout is None, "MusicControlView 的 timeout 為 None（持久化 View 必備）")

    btn_map = {item.custom_id: item for item in view.children if isinstance(item, discord.ui.Button)}
    expected_ids = {
        "music:toggle_pause",
        "music:skip",
        "music:stop",
        "music:vol_down",
        "music:vol_up",
    }
    check(set(btn_map.keys()) == expected_ids, "5 顆按鈕皆設定固定且唯一的 custom_id")
    check(len(btn_map) == 5, "按鈕數量剛好為 5 顆（單一 ActionRow 容納）")


async def test_refresh_buttons():
    """測試 2: refresh_buttons 動態狀態切換。"""
    view = MusicControlView()
    btn_map = {item.custom_id: item for item in view.children if isinstance(item, discord.ui.Button)}

    # 1. Player 為 None（未播放）
    view.refresh_buttons(None)
    all_disabled = all(btn.disabled for btn in btn_map.values())
    check(all_disabled, "Player 為 None 時，所有按鈕皆 disabled")

    # 2. 正常播放中、佇列為空、音量 100%
    player = FakePlayer(current=FakeTrack(), song_queue=deque(), paused=False, volume=100)
    view.refresh_buttons(player)
    btn_toggle = btn_map["music:toggle_pause"]
    btn_skip = btn_map["music:skip"]
    btn_stop = btn_map["music:stop"]
    btn_vol_down = btn_map["music:vol_down"]
    btn_vol_up = btn_map["music:vol_up"]

    check(not btn_toggle.disabled and btn_toggle.label == "暫停" and btn_toggle.emoji.name == "⏸️", "播放中：暫停鈕顯示「暫停」與 ⏸️")
    check(btn_skip.disabled, "佇列沒有下一首時：切歌鈕 disabled")
    check(not btn_stop.disabled, "播放中：停止鈕 enabled")
    check(not btn_vol_down.disabled, "音量 100%：音量 − enabled")
    check(btn_vol_up.disabled, "音量 100%：音量 + disabled（上限邊界）")

    # 3. 暫停中、佇列有 1 首、音量 50%
    player.paused = True
    player.song_queue.append((FakeTrack(title="第二首"), "點播者", 123))
    player.volume = 50
    view.refresh_buttons(player)

    check(not btn_toggle.disabled and btn_toggle.label == "繼續" and btn_toggle.emoji.name == "▶️", "暫停中：切換鈕顯示「繼續」與 ▶️")
    check(not btn_skip.disabled, "佇列有下一首時：切歌鈕 enabled")
    check(not btn_vol_down.disabled and not btn_vol_up.disabled, "音量 50%：音量 − 與 + 皆 enabled")

    # 4. 音量 0%
    player.volume = 0
    view.refresh_buttons(player)
    check(btn_vol_down.disabled, "音量 0%：音量 − disabled（下限邊界）")
    check(not btn_vol_up.disabled, "音量 0%：音量 + enabled")


async def test_interaction_check():
    """測試 3: interaction_check 權限與 Bot 重啟防呆。"""
    view = MusicControlView()

    # 模擬 Interaction
    fake_guild = MagicMock(spec=discord.Guild)
    fake_guild.id = 1111

    fake_vc_channel = MagicMock(spec=discord.VoiceChannel)
    fake_vc_channel.id = 2222
    fake_vc_channel.mention = "<#2222>"

    # 情境 1: Bot 重啟後舊面板被點擊（voice_client 為 None）
    interaction_restart = MagicMock(spec=discord.Interaction)
    interaction_restart.guild = fake_guild
    fake_guild.voice_client = None
    interaction_restart.response = MagicMock()
    interaction_restart.response.send_message = AsyncMock()
    interaction_restart.message = MagicMock()
    interaction_restart.message.edit = AsyncMock()

    res = await view.interaction_check(interaction_restart)
    check(res is False, "Bot 重啟後無 Player：interaction_check 回傳 False 攔截")
    interaction_restart.response.send_message.assert_called_once()
    call_args = interaction_restart.response.send_message.call_args
    check("目前沒有播放中的內容" in call_args[0][0] and call_args[1].get("ephemeral") is True, "無 Player 時以 ephemeral 提示「目前沒有播放中的內容」")
    interaction_restart.message.edit.assert_called_once()
    edit_args = interaction_restart.message.edit.call_args
    disabled_view = edit_args[1].get("view")
    check(all(b.disabled for b in disabled_view.children if isinstance(b, discord.ui.Button)), "無 Player 時將舊面板按鈕全部 disable")

    # 情境 2: 使用者與 Bot 不在同一個語音頻道
    player = FakePlayer(current=FakeTrack(), channel=fake_vc_channel, guild=fake_guild)
    fake_guild.voice_client = player
    # 讓 isinstance(player, music.wavelink.Player) 為 True
    music.wavelink.Player = FakePlayer

    interaction_diff_vc = MagicMock(spec=discord.Interaction)
    interaction_diff_vc.guild = fake_guild
    interaction_diff_vc.user = MagicMock()
    user_other_vc = MagicMock()
    user_other_vc.id = 3333
    interaction_diff_vc.user.voice = MagicMock()
    interaction_diff_vc.user.voice.channel = user_other_vc
    interaction_diff_vc.response = MagicMock()
    interaction_diff_vc.response.send_message = AsyncMock()

    res_diff = await view.interaction_check(interaction_diff_vc)
    check(res_diff is False, "非同頻道使用者：interaction_check 回傳 False 拒絕")
    interaction_diff_vc.response.send_message.assert_called_once()
    msg_arg = interaction_diff_vc.response.send_message.call_args[0][0]
    check("同一個語音頻道" in msg_arg and interaction_diff_vc.response.send_message.call_args[1].get("ephemeral") is True, "非同頻道以 ephemeral 提示拒絕")

    # 情境 3: 使用者在同一個語音頻道
    interaction_same_vc = MagicMock(spec=discord.Interaction)
    interaction_same_vc.guild = fake_guild
    interaction_same_vc.user = MagicMock()
    interaction_same_vc.user.voice = MagicMock()
    interaction_same_vc.user.voice.channel = fake_vc_channel

    res_same = await view.interaction_check(interaction_same_vc)
    check(res_same is True, "同頻道使用者：interaction_check 回傳 True 通過驗證")


async def test_multi_guild_isolation():
    """測試 4: 多伺服器面板狀態隔離。"""
    fake_bot = MagicMock(spec=discord.Client)
    cog = Music(fake_bot)

    guild_a_id = 1001
    guild_b_id = 2002

    guild_a = MagicMock(spec=discord.Guild)
    guild_a.id = guild_a_id
    guild_a.voice_client = None

    guild_b = MagicMock(spec=discord.Guild)
    guild_b.id = guild_b_id
    guild_b.voice_client = None

    channel_a = MagicMock(spec=discord.TextChannel)
    msg_a = MagicMock(spec=discord.Message)
    msg_a.edit = AsyncMock()
    channel_a.fetch_message = AsyncMock(return_value=msg_a)

    def get_guild(gid):
        return guild_a if gid == guild_a_id else (guild_b if gid == guild_b_id else None)

    fake_bot.get_guild.side_effect = get_guild
    fake_bot.get_channel.return_value = channel_a

    # 伺服器 A 記錄面板
    cog.panel_messages[guild_a_id] = (5001, 9001)
    # 伺服器 B 記錄面板
    cog.panel_messages[guild_b_id] = (5002, 9002)

    check(guild_a_id in cog.panel_messages and guild_b_id in cog.panel_messages, "字典各自保存不同伺服器的面板訊息")
    check(cog.panel_messages[guild_a_id] != cog.panel_messages[guild_b_id], "伺服器 A 與伺服器 B 面板 ID 相互獨立")

    # 模擬伺服器 A 播放結束更新
    await cog._update_panel_ended(guild_a_id)
    msg_a.edit.assert_called_once()
    check(guild_b_id in cog.panel_messages and cog.panel_messages[guild_b_id] == (5002, 9002), "操作伺服器 A 完全不影響伺服器 B 的面板狀態")


async def test_on_error_handling():
    """測試 5: on_error 異常捕獲與 notify_owner_error。"""
    view = MusicControlView()
    fake_bot = MagicMock()
    fake_bot.notify_owner_error = AsyncMock()

    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = fake_bot
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()

    fake_item = MagicMock(spec=discord.ui.Button)
    fake_item.custom_id = "music:toggle_pause"

    err = RuntimeError("模擬按鈕未預期例外")
    await view.on_error(interaction, err, fake_item)

    fake_bot.notify_owner_error.assert_called_once()
    check(fake_bot.notify_owner_error.call_args[0][0] == err, "按鈕異常時正確呼叫 bot.notify_owner_error")
    interaction.response.send_message.assert_called_once()
    check("發生錯誤" in interaction.response.send_message.call_args[0][0], "向使用者回覆友善的錯誤訊息")


async def main():
    print("=== 開始執行第 5 週音樂面板單元測試 ===")
    await test_view_persistence()
    await test_refresh_buttons()
    await test_interaction_check()
    await test_multi_guild_isolation()
    await test_on_error_handling()

    total = len(PASS) + len(FAIL)
    print(f"\n共 {total} 項測試：{len(PASS)} 通過，{len(FAIL)} 失敗")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
