"""第 7 週：大型歌單匯入的記憶體／穩定度壓力測試（離線，用假的 Spotify 與搜尋）。

用途：驗證匯入 N 首（預設 200）時，Python 端的記憶體峰值與任務清理是否符合 512MB 環境的需求。
用 tracemalloc 量「匯入前 → 匯入中峰值 → 匯入後」，並確認背景任務結束後沒有殘留。

執行方式：
    python tools/bench_import_memory.py            # 200 首
    python tools/bench_import_memory.py 1000 0.005 # 自訂首數與單首模擬延遲（秒）

注意：這裡量的是「程式端物件」的記憶體，不含 Lavalink 節點與 discord.py 本身；
實機請搭配 Render／雲端面板的 Metrics 觀察整個行程的 RSS。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import os
import time
import tracemalloc
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cogs.music as music_mod  # noqa: E402
from cogs.music import Music  # noqa: E402
from services.spotify import SpotifyFetchResult, SpotifyTrack  # noqa: E402

GID = 1


class FakePlayer:
    def __init__(self):
        self.song_queue = deque()
        self.current = None
        self.paused = False
        self.connected = True
        self.loop_mode = "off"
        self.guild = SimpleNamespace(id=GID)

    @property
    def playing(self):
        return self.current is not None and not self.paused

    async def play(self, track):
        self.current = track


async def run(n: int, delay: float):
    player = FakePlayer()
    bot = MagicMock()
    bot.get_guild = lambda gid: SimpleNamespace(id=GID, voice_client=player)
    bot.notify_owner_error = AsyncMock()
    cog = Music(bot)
    cog._sync_panel = AsyncMock()
    cog.panel_messages[GID] = (1, 2)
    cog.spotify = MagicMock()
    cog.spotify.enabled = True
    cog.spotify.fetch_tracks = AsyncMock(
        return_value=SpotifyFetchResult(
            "playlist", [SpotifyTrack(f"歌{i}", f"手{i}", 200000) for i in range(n)], False, 0
        )
    )

    async def fake_search(sem, st):
        async with sem:
            await asyncio.sleep(delay)  # 模擬 Lavalink 搜尋延遲
        return SimpleNamespace(title=st.title, uri=f"https://x/{st.title}", author=st.artist, length=200000)

    cog._search_spotify_track = fake_search
    it = MagicMock()
    it.guild = SimpleNamespace(id=GID)
    it.user = SimpleNamespace(id=7, display_name="bench")
    it.delete_original_response = AsyncMock()

    # 同時量測事件迴圈延遲：每 50ms 排一次心跳，看實際間隔有沒有被拉長（代表迴圈有沒有被阻塞）
    max_lag = 0.0
    stop = False

    async def heartbeat():
        nonlocal max_lag
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.05)
            now = time.perf_counter()
            max_lag = max(max_lag, now - last - 0.05)
            last = now

    hb = asyncio.create_task(heartbeat())
    tracemalloc.start()
    base, _ = tracemalloc.get_traced_memory()
    t0 = time.perf_counter()
    await cog._start_spotify_import(it, player, "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    task = cog._import_tasks[GID]
    await task
    elapsed = time.perf_counter() - t0
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    stop = True
    await hb

    queued = len(player.song_queue) + (1 if player.current else 0)
    print(f"匯入首數：{n}，實際進入播放／佇列：{queued}，耗時 {elapsed:.2f}s")
    print(f"記憶體（tracemalloc）：起始 {base/1024:.0f} KiB → 峰值 +{(peak-base)/1024:.0f} KiB → 結束 +{(cur-base)/1024:.0f} KiB")
    print(f"事件迴圈最大額外延遲：{max_lag*1000:.1f} ms（越小代表越沒有阻塞）")
    print(f"殘留匯入任務：{len(cog._import_tasks)}（應為 0）")
    assert queued == n and not cog._import_tasks


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    lag = float(sys.argv[2]) if len(sys.argv) > 2 else 0.02
    asyncio.run(run(count, lag))

