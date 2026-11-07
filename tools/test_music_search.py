"""測試 cogs/music.py 的搜尋演算法（YouTube 優先 + Fallback 機制）與失敗私人訊息處理。

涵蓋項目：
1. _fetch_tracks_with_fallback：
   - URL 網址直接搜尋，不加前綴
   - 使用者自帶前綴（例如 scsearch:、ytsearch:）直接搜尋
   - 純關鍵字優先使用 ytsearch
   - ytsearch 找不到（空清單）或例外時，自動 fallback 至 scsearch
   - 全數找不到時回傳 None
2. _search_one_track：
   - 找不到結果時，刪除公開 defer 思考訊息，並以 ephemeral=True 送出私人訊息
   - 搜尋拋出例外時，回報開發者、刪除公開 defer 並以 ephemeral=True 送出私人訊息
   - 搜尋成功時正確取出曲目（一般曲目或 Playlist 取首曲）

執行方式：python tools/test_music_search.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cogs.music as music_mod  # noqa: E402
from cogs.music import Music  # noqa: E402

PASS, FAIL = [], []


def check(cond, name):
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name)


def fake_track(title):
    return SimpleNamespace(title=title, uri=f"https://x/{title}", author="Singer", length=180000)


async def test_search_algorithm():
    print("\n--- 測試 1: _fetch_tracks_with_fallback 搜尋演算法 ---")
    bot = MagicMock()
    cog = Music(bot)

    called_args = []

    async def mock_search(query, source=None):
        called_args.append((query, source))
        if source == "ytsearch":
            return [fake_track("yt_result")]
        return []

    orig_search = getattr(music_mod.wavelink.Playable, "search", None)
    music_mod.wavelink.Playable.search = mock_search

    try:
        # 1-1 純關鍵字：優先使用 ytsearch
        called_args.clear()
        res = await cog._fetch_tracks_with_fallback("夜に駆ける")
        check(called_args == [("夜に駆ける", "ytsearch")], f"純關鍵字優先使用 ytsearch ({called_args})")
        check(res and res[0].title == "yt_result", "成功取得 ytsearch 結果")

        # 1-2 URL 搜尋：不加 source 前綴
        called_args.clear()
        await cog._fetch_tracks_with_fallback("https://www.youtube.com/watch?v=123")
        check(called_args == [("https://www.youtube.com/watch?v=123", None)], "URL 直接搜尋不帶前綴")

        # 1-3 自帶前綴：直接搜尋不重疊前綴
        called_args.clear()
        await cog._fetch_tracks_with_fallback("scsearch:some_song")
        check(called_args == [("scsearch:some_song", None)], "自帶前綴直接搜尋")

        # 1-4 Fallback 測試：ytsearch 為空時自動嘗試 scsearch
        called_args.clear()

        async def fallback_search(query, source=None):
            called_args.append((query, source))
            if source == "ytsearch":
                return []  # 模擬 YouTube 找不到
            if source == "scsearch":
                return [fake_track("sc_result")]  # 模擬 SoundCloud 找到
            return []

        music_mod.wavelink.Playable.search = fallback_search
        res_fb = await cog._fetch_tracks_with_fallback("冷門歌曲")
        check(len(called_args) == 2 and called_args[0][1] == "ytsearch" and called_args[1][1] == "scsearch",
              "ytsearch 查無結果時自動 fallback 至 scsearch")
        check(res_fb and res_fb[0].title == "sc_result", "成功取得 SoundCloud fallback 結果")

        # 1-5 全數找不到時回傳 None
        async def empty_search(query, source=None):
            return []

        music_mod.wavelink.Playable.search = empty_search
        check(await cog._fetch_tracks_with_fallback("完全不存在的歌曲") is None, "全來源找不到時回傳 None")

    finally:
        music_mod.wavelink.Playable.search = orig_search


async def test_search_ephemeral_error():
    print("\n--- 測試 2: 搜尋失敗訊息改為私人訊息 (ephemeral) ---")
    bot = MagicMock()
    bot.notify_owner_error = AsyncMock()
    cog = Music(bot)

    interaction = MagicMock()
    interaction.delete_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 2-1 查無結果
    cog._fetch_tracks_with_fallback = AsyncMock(return_value=None)
    res = await cog._search_one_track(interaction, "找不到的歌")
    check(res is None, "查無結果回傳 None")
    check(interaction.delete_original_response.await_count == 1, "刪除原公開 defer 思考訊息")
    check(interaction.followup.send.await_count == 1, "發送 followup 私人訊息")
    kwargs = interaction.followup.send.await_args.kwargs
    check(kwargs.get("ephemeral") is True, "訊息為 ephemeral=True（私人訊息）")
    check("找不到" in interaction.followup.send.await_args.args[0], "提示訊息包含找不到關鍵字說明")

    # 2-2 搜尋拋出例外
    interaction.delete_original_response.reset_mock()
    interaction.followup.send.reset_mock()
    cog._fetch_tracks_with_fallback = AsyncMock(side_effect=RuntimeError("網絡中斷"))

    res_err = await cog._search_one_track(interaction, "壞掉的歌")
    check(res_err is None, "搜尋例外回傳 None")
    check(bot.notify_owner_error.await_count == 1, "回報開發者 notify_owner_error")
    check(interaction.delete_original_response.await_count == 1, "發生錯誤時亦刪除原公開 defer 訊息")
    check(interaction.followup.send.await_args.kwargs.get("ephemeral") is True, "錯誤訊息為 ephemeral=True（私人訊息）")
    check("發生錯誤" in interaction.followup.send.await_args.args[0], "提示訊息說明發生錯誤")

    # 2-3 搜尋成功回傳曲目
    track1 = fake_track("Good Song")
    cog._fetch_tracks_with_fallback = AsyncMock(return_value=[track1])
    res_ok = await cog._search_one_track(interaction, "Good Song")
    check(res_ok is track1, "搜尋成功正確回傳第一首曲目")


async def main_async():
    await test_search_algorithm()
    await test_search_ephemeral_error()


def main():
    print("=== 開始執行音樂搜尋演算法與私人訊息單元測試 ===")
    asyncio.run(main_async())
    total = len(PASS) + len(FAIL)
    print(f"\n測試統計：共 {total} 項，{len(PASS)} 通過，{len(FAIL)} 失敗")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
