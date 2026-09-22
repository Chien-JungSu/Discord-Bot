from __future__ import annotations

import asyncio
import os
from collections import deque
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    import wavelink as wavelink_module

try:
    import wavelink
except ImportError:  # 尚未安裝 wavelink 時，讓其他 Cog 仍可正常運作
    wavelink = None

# Lavalink 連線逾時秒數，可用環境變數覆寫，避免連線卡住拖垮整個 bot 啟動流程
LAVALINK_CONNECT_TIMEOUT = float(os.getenv('LAVALINK_CONNECT_TIMEOUT', '15'))


def format_duration(length_ms: int | None) -> str:
    """將 wavelink Playable.length（毫秒）轉成 mm:ss 字串，供 Embed 顯示用。"""
    if not length_ms:
        return "直播 / 未知長度"
    total_seconds = length_ms // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """Cog 被 bot.load_extension() 載入時自動呼叫一次，建立節點連線池。

        修正: 連線 Lavalink 改成背景任務執行，不要在這裡 await 完成連線。
        wavelink.Pool.connect() 在節點連不上時可能會卡住重試很久，如果直接
        await 會讓 setup_hook() 整個卡死，導致後面的 tree.sync() 跟 on_ready
        都永遠跑不到（機器人網頁的 bot-stats 也會因此一直是 0）。改成
        create_task 丟到背景後，cog_load() 會立刻返回，其餘啟動流程不受影響；
        另外加上逾時保護，避免背景任務本身無限期卡住。
        """
        if wavelink is None:
            print("⚠️ 尚未安裝 wavelink，Music Cog 的 /join /leave /play 將無法運作。"
                  "請先執行 pip install wavelink 並設定 Lavalink 節點。")
            return

        lavalink_uri = os.getenv('LAVALINK_URI')
        lavalink_password = os.getenv('LAVALINK_PASSWORD')

        if not lavalink_uri or not lavalink_password:
            print("⚠️ 環境變數 LAVALINK_URI / LAVALINK_PASSWORD 未設定或為空，"
                  "已略過 Lavalink 節點連線。請檢查 .env 內容，例如：\n"
                  "   LAVALINK_URI=http://lavalink.jirayu.net:13592\n"
                  "   LAVALINK_PASSWORD=youshallnotpass")
            return

        # 不 await，丟到背景執行，讓 cog_load() 立刻返回
        asyncio.create_task(self._connect_lavalink(lavalink_uri, lavalink_password))
        print(f"⏳ 已在背景開始連線 Lavalink 節點：{lavalink_uri}（不會阻擋機器人其他功能啟動）")

    async def _connect_lavalink(self, lavalink_uri: str, lavalink_password: str):
        node = wavelink.Node(uri=lavalink_uri, password=lavalink_password)
        try:
            await asyncio.wait_for(
                wavelink.Pool.connect(nodes=[node], client=self.bot),
                timeout=LAVALINK_CONNECT_TIMEOUT,
            )
            print(f"🎧 已成功連線至 Lavalink 節點：{lavalink_uri}")
        except asyncio.TimeoutError as e:
            print(
                f"❌ 連線 Lavalink 節點逾時（超過 {LAVALINK_CONNECT_TIMEOUT:.0f} 秒）：{lavalink_uri}，"
                "語音功能（/join /leave /play）可能暫時無法使用，但不影響機器人其他功能。"
            )
            # 修正：先前只有 print，開發者不在電腦前看 log 就完全不會發現音樂功能掛了。
            await self.bot.notify_owner_error(
                e, extra_info=f"Lavalink 節點連線逾時：{lavalink_uri}（超過 {LAVALINK_CONNECT_TIMEOUT:.0f} 秒）"
            )
        except Exception as e:
            # Lavalink 節點若尚未啟動，這裡會失敗；先印出訊息，不讓整個 Bot 崩潰。
            print(f"❌ 連線 Lavalink 節點失敗：{e}")
            print("   請確認 Lavalink 是否已啟動，以及 LAVALINK_URI / LAVALINK_PASSWORD 是否正確。")
            await self.bot.notify_owner_error(e, extra_info=f"Lavalink 節點連線失敗：{lavalink_uri}")

    # ---------- wavelink 自訂事件 ----------
    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: Any):
        print(f"✅ Lavalink 節點已就緒：{payload.node.uri}（Session ID: {payload.session_id}）")

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: Any):
        """實測發現的問題：player.play(track) 呼叫成功不代表歌曲真的能播放。

        Lavalink 是先接受請求、才非同步去源站（YouTube 等）載入音訊，載入失敗時是用
        這個事件回報，不會讓 /play 裡的 try/except 抓到，所以之前失敗時 Discord 上
        完全沒有任何提示，只有後台 log 印出一大串 Java stacktrace。這裡統一攔截、
        簡單判斷常見原因（YouTube 反爬蟲要求登入 / 地區限制），回報到最近一次下
        指令的文字頻道。
        """
        player = getattr(payload, "player", None)
        track = getattr(payload, "track", None)
        exception = getattr(payload, "exception", None)

        # Lavalink v4 的 exception 物件可能是 dict，也可能是有屬性的物件，兩種都相容處理。
        if isinstance(exception, dict):
            message = exception.get("message")
            cause = exception.get("cause")
            severity = exception.get("severity")
        else:
            message = getattr(exception, "message", None)
            cause = getattr(exception, "cause", None)
            severity = getattr(exception, "severity", None)

        track_title = getattr(track, "title", None) or "未知曲目"
        print(f"❌ Lavalink TrackException｜track={track_title} severity={severity} message={message} cause={cause}")

        channel = getattr(player, "home_channel", None) if player else None
        if channel is None:
            return  # 沒有記錄到文字頻道（例如 Player 不是透過 /join 或 /play 建立），只留後台 log

        combined_text = f"{message or ''} {cause or ''}".lower()
        if "login" in combined_text or "sign in" in combined_text:
            reason_hint = (
                "\n🔍 研判原因：YouTube 判定這個請求需要登入驗證，疑似觸發了反爬蟲機制。"
                "這是已知的技術風險，規劃在第8週設定 poToken／OAuth2 來解決，目前這首歌先跳過。"
            )
        elif "not available" in combined_text or "region" in combined_text:
            reason_hint = "\n🔍 研判原因：這支影片可能有地區限制，換一個來源或影片試試看。"
        elif severity == "fault" or "invalid status code" in combined_text or " 404" in combined_text:
            # 實測發現：目前使用的是公開 Lavalink 節點，曾遇過 SoundCloud 串流連結
            # 直接回傳 404（節點端問題，不是我們程式碼的邏輯錯誤）。這正是計畫書裡
            # 「公開節點本質上不穩定」的實際案例，也是第8週要自架節點的理由之一。
            reason_hint = (
                "\n🔍 研判原因：這個音源的連結目前是壞的（來源站回傳錯誤），"
                "常見於目前使用的公開 Lavalink 節點不穩定，換一首歌或稍後再試看看。"
            )
        else:
            reason_hint = ""

        # 修正：先前只把錯誤印在後台跟發到文字頻道，開發者完全不會被 DM 通知。
        # severity 分三種：common（常見、預期內，例如格式不支援）、suspicious（可疑，
        # 通常是外部來源造成，例如 YouTube 反爬蟲）、fault（節點本身的問題）。
        # common 太常見也不算真的「未知錯誤」，故意不 DM 避免洗版；suspicious／fault
        # 才算是需要開發者留意的狀況，這兩種才會觸發 notify_owner_error。
        if severity in ("suspicious", "fault"):
            synthetic_error = RuntimeError(f"Lavalink TrackException（{severity}）：{message or cause or '未知錯誤'}")
            await self.bot.notify_owner_error(
                synthetic_error,
                extra_info=f"on_wavelink_track_exception track={track_title} severity={severity} cause={cause}",
            )

        try:
            await channel.send(f"⚠️ 播放「{track_title}」失敗，Lavalink 節點無法載入這首歌曲。{reason_hint}")
        except discord.HTTPException as e:
            print(f">>> 傳送 TrackException 通知訊息失敗: {e}")

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: Any):
        """第3週新增：串接播放佇列的核心事件。

        不論一首歌是正常播完、被跳過還是發生錯誤，Lavalink 都會在該首歌結束時
        觸發這個事件一次（細節寫在 payload.reason，這裡先不細分）。我們只要在
        這個時機點檢查「這個伺服器自己的佇列」還有沒有下一首：有的話就用
        popleft() 從 FIFO 佇列最前面取出並接著播放，藉此達成播完自動連播；
        佇列空了就發一次通知，不然使用者只會看到機器人靜靜停在語音頻道裡。

        這裡刻意把 player.autoplay 設成 disabled（見 _ensure_player / /join），
        是因為 wavelink 內建的 autoplay 也會在歌曲結束時嘗試自己接下一首（從
        wavelink 自己的 Queue 或推薦清單），如果不關掉，會跟這裡手動接管的邏輯
        搶著呼叫 player.play()，兩邊互相打架。關掉之後，「歌曲結束 → 接下一首」
        完全由我們自己的佇列與這個監聽器控制，這也是這週想練習的「非同步事件
        驅動」重點。
        """
        player = getattr(payload, "player", None)
        if player is None:
            return

        song_queue: deque = getattr(player, "song_queue", None)
        if song_queue is None:
            song_queue = deque()
            player.song_queue = song_queue

        if not song_queue:
            channel = getattr(player, "home_channel", None)
            if channel is not None:
                try:
                    await channel.send("📭 播放佇列已經全部播完囉，輸入 `/play` 繼續點歌吧！")
                except discord.HTTPException:
                    pass
            return

        next_track = song_queue.popleft()
        try:
            await player.play(next_track)
        except Exception as e:
            print(f">>> 自動播放下一首時發生錯誤：{e}")
            await self.bot.notify_owner_error(
                e, extra_info=f"on_wavelink_track_end 自動播放失敗 track={getattr(next_track, 'title', '?')}"
            )
            channel = getattr(player, "home_channel", None)
            if channel is not None:
                try:
                    await channel.send(
                        f"⚠️ 自動播放「{getattr(next_track, 'title', '未知曲目')}」時發生錯誤，已回報開發者。"
                    )
                except discord.HTTPException:
                    pass

    # ---------- 共用邏輯 ----------
    async def _ensure_player(
        self, interaction: discord.Interaction
    ) -> "wavelink_module.Player | None":
        """取得目前伺服器的 Player；若機器人還沒加入語音頻道，就直接幫使用者加入。

        回傳 None 代表已經送出錯誤訊息給使用者，呼叫端應該直接 return，不要繼續播放。
        這裡刻意沒有沿用 /join 指令本身，而是抽成共用函式，因為 /play 需要在
        「還沒 defer」跟「已經 defer」兩種情境下都能重複使用同一段邏輯。
        """
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.followup.send("❌ 你必須先加入一個語音頻道，我才能播放音樂！", ephemeral=True)
            return None

        user_channel = interaction.user.voice.channel
        player: "wavelink_module.Player | None" = interaction.guild.voice_client

        if player is None:
            try:
                player = await user_channel.connect(cls=wavelink.Player)
                # 第3週更新：改成 disabled，改由我們自己的 song_queue +
                # on_wavelink_track_end 監聽器接管「播完自動接下一首」的邏輯，
                # 避免跟 wavelink 內建的 autoplay 互相搶著呼叫 player.play()。
                player.autoplay = wavelink.AutoPlayMode.disabled
                player.song_queue = deque()
            except Exception as e:
                print(f">>> /play 加入語音頻道失敗：{e}")
                await self.bot.notify_owner_error(e, interaction, extra_info=f"/play 加入 {user_channel} 失敗")
                await interaction.followup.send("❌ 加入語音頻道時發生錯誤，已回報開發者。", ephemeral=True)
                return None
        elif player.channel.id != user_channel.id:
            await interaction.followup.send(
                f"❌ 你必須跟我在同一個語音頻道（{player.channel.mention}）才能點歌！", ephemeral=True
            )
            return None

        # 不論是新建立還是原本就存在的 Player，都更新 home_channel，
        # 讓 on_wavelink_track_exception 把錯誤訊息回報到「最近一次下指令」的文字頻道，
        # 而不是固定在第一次 /join 當下的頻道。
        player.home_channel = interaction.channel

        # 保險起見：萬一 player 是透過其他路徑建立（理論上不會發生），
        # 確保一定有 song_queue 可用，避免後面存取時 AttributeError。
        if getattr(player, "song_queue", None) is None:
            player.song_queue = deque()

        return player

    # ---------- 指令 ----------
    @app_commands.command(name="music_join", description="讓機器人加入你目前所在的語音頻道")
    async def join(self, interaction: discord.Interaction):
        if wavelink is None:
            await interaction.response.send_message("❌ 語音模組尚未安裝完成，請聯絡管理員。", ephemeral=True)
            return

        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message("❌ 你必須先加入一個語音頻道，我才能跟過去！", ephemeral=True)
            return

        channel = interaction.user.voice.channel

        if interaction.guild.voice_client is not None:
            await interaction.response.send_message(
                f"ℹ️ 我已經在 {interaction.guild.voice_client.channel.mention} 頻道中了。", ephemeral=True
            )
            return

        # 修正（實測發現）: channel.connect() 內部要完成語音閘道 handshake，
        # 距離 Lavalink 節點較遠或 Render 冷啟動時很容易超過 Discord 給的 3 秒 ack
        # 期限，沒有 defer() 時會直接炸 discord.errors.NotFound (10062 Unknown
        # interaction)。做法沿用 /play：先 defer(ephemeral=True)，之後一律用
        # followup.send 回覆，不再用 interaction.response.send_message。
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except (discord.NotFound, discord.HTTPException) as e:
            print(f">>> /join interaction.defer() 失敗: {e}")
            return

        try:
            player = await channel.connect(cls=wavelink.Player)
        except Exception as e:
            print(f">>> 加入語音頻道失敗：{e}")
            await self.bot.notify_owner_error(e, interaction, extra_info=f"/join 加入 {channel} 失敗")
            await interaction.followup.send("❌ 加入語音頻道時發生錯誤，已回報開發者。", ephemeral=True)
            return

        # 第3週更新：與 _ensure_player 一致，改成 disabled，
        # 交由自己的 song_queue + on_wavelink_track_end 決定下一首播什麼。
        player.autoplay = wavelink.AutoPlayMode.disabled
        player.song_queue = deque()
        # 記錄下指令的文字頻道，讓 on_wavelink_track_exception 之後能把播放失敗的
        # 通知送回這裡（Lavalink 端的錯誤是非同步事件，不會經過 /join 或 /play 本身）。
        player.home_channel = interaction.channel

        await interaction.followup.send(f"🔊 已加入 {channel.mention}！", ephemeral=True)

    @app_commands.command(name="music_leave", description="讓機器人離開目前所在的語音頻道")
    async def leave(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client

        if voice_client is None:
            await interaction.response.send_message("ℹ️ 我目前不在任何語音頻道中。", ephemeral=True)
            return

        channel_mention = voice_client.channel.mention
        # 第3週新增：離開語音頻道時順便清空這個伺服器的佇列，
        # 避免下次 /join 進來時還殘留上一次沒播完的歌曲清單造成混淆。
        song_queue = getattr(voice_client, "song_queue", None)
        if song_queue:
            song_queue.clear()
        await voice_client.disconnect()
        await interaction.response.send_message(f"👋 已離開 {channel_mention}。", ephemeral=True)

    async def _search_one_track(
        self, interaction: discord.Interaction, query: str
    ) -> "wavelink_module.Playable | None":
        """共用的搜尋邏輯，/play 與 /play_next（插播）都會用到。

        回傳 None 代表搜尋失敗或找不到結果，錯誤訊息已經透過 followup 送出，
        呼叫端只要直接 return 即可，不用重複處理錯誤訊息。
        """
        try:
            tracks = await wavelink.Playable.search(query)
        except Exception as e:
            # wavelink 搜尋失敗常見原因：來源網站暫時掛掉、網址格式不支援，
            # 或是 YouTube 端觸發了反爬蟲機制（poToken / OAuth2 相關）。
            # 第8週會處理 poToken/OAuth2 設定，這裡先統一攔截、回報開發者，
            # 避免整個指令直接噴未捕捉例外。
            print(f">>> wavelink 搜尋失敗：{e}")
            await self.bot.notify_owner_error(e, interaction, extra_info=f"音樂搜尋失敗 query={query}")
            await interaction.followup.send(
                "❌ 搜尋音樂時發生錯誤，可能是來源網站暫時無法連線，或觸發了反爬蟲封鎖，已回報開發者。",
                ephemeral=True,
            )
            return None

        if not tracks:
            await interaction.followup.send(
                f"😕 找不到「{query}」的搜尋結果，可能是關鍵字沒有對應結果、影片為地區限定，或網址無效，換個關鍵字試試看。",
                ephemeral=True,
            )
            return None

        # wavelink.Playable.search 對於歌單網址會回傳 Playlist，單曲/關鍵字則回傳 list[Playable]。
        # 本週先只取第一首，完整歌單匯入排到第7週（Spotify 批次解析）再實作。
        if isinstance(tracks, wavelink.Playlist):
            return tracks.tracks[0]
        return tracks[0]

    def _build_track_embed(
        self, title: str, track: "wavelink_module.Playable", requester: discord.abc.User, color: discord.Color
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=f"[{track.title}]({track.uri})" if getattr(track, "uri", None) else track.title,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        artwork = getattr(track, "artwork", None)
        if artwork:
            embed.set_thumbnail(url=artwork)
        embed.add_field(name="👤 作者", value=getattr(track, "author", None) or "未知", inline=True)
        embed.add_field(name="⏱️ 長度", value=format_duration(getattr(track, "length", None)), inline=True)
        embed.set_footer(text=f"由 {requester.display_name} 點播")
        return embed

    @app_commands.command(name="music_play", description="搜尋並播放音樂，若目前正在播放則排入佇列（可輸入關鍵字或直接貼網址）")
    @app_commands.describe(query="歌曲名稱 / 關鍵字，或是 YouTube、SoundCloud 網址")
    async def play(self, interaction: discord.Interaction, query: str):
        if wavelink is None:
            await interaction.response.send_message("❌ 語音模組尚未安裝完成，請聯絡管理員。", ephemeral=True)
            return

        # 搜尋 + 連線語音頻道都可能花超過 3 秒，先 defer 避免 Interaction 逾時（沿用 /weather 的慣例）。
        try:
            await interaction.response.defer(thinking=True)
        except (discord.NotFound, discord.HTTPException) as e:
            print(f">>> /play interaction.defer() 失敗: {e}")
            return

        player = await self._ensure_player(interaction)
        if player is None:
            return  # 錯誤訊息已經在 _ensure_player 內送出

        track = await self._search_one_track(interaction, query)
        if track is None:
            return  # 錯誤訊息已經在 _search_one_track 內送出

        # ---- 第3週更新：有佇列了，不再直接蓋掉正在播放的歌曲 ----
        # player.playing 代表現在有歌在播、player.paused 代表暫停中但還沒播完，
        # 這兩種狀態都視為「有東西在佔用播放器」，新點的歌一律排到佇列尾端（FIFO）。
        if player.playing or player.paused:
            player.song_queue.append(track)
            position = len(player.song_queue)
            embed = self._build_track_embed(
                title="➕ 已加入佇列", track=track, requester=interaction.user, color=discord.Color.blurple()
            )
            embed.add_field(name="📌 排隊位置", value=f"第 {position} 位", inline=False)
            await interaction.followup.send(embed=embed)
            return

        # ---- 佇列與播放器都是空的，直接開始播放 ----
        try:
            await player.play(track)
        except Exception as e:
            print(f">>> 播放音樂時發生錯誤：{e}")
            await self.bot.notify_owner_error(
                e, interaction, extra_info=f"/play 播放失敗 track={getattr(track, 'title', '?')}"
            )
            await interaction.followup.send("❌ 播放音樂時發生錯誤，已回報開發者。", ephemeral=True)
            return

        embed = self._build_track_embed(
            title="🎶 開始播放", track=track, requester=interaction.user, color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="music_play_next", description="（插播）搜尋一首歌曲並插入佇列最前面，下一首就會播放它")
    @app_commands.describe(query="歌曲名稱 / 關鍵字，或是 YouTube、SoundCloud 網址")
    async def play_next(self, interaction: discord.Interaction, query: str):
        if wavelink is None:
            await interaction.response.send_message("❌ 語音模組尚未安裝完成，請聯絡管理員。", ephemeral=True)
            return

        try:
            await interaction.response.defer(thinking=True)
        except (discord.NotFound, discord.HTTPException) as e:
            print(f">>> /play_next interaction.defer() 失敗: {e}")
            return

        player = await self._ensure_player(interaction)
        if player is None:
            return

        track = await self._search_one_track(interaction, query)
        if track is None:
            return

        if player.playing or player.paused:
            # 插播的重點：用 appendleft 塞到 FIFO 佇列的最前面，
            # 而不是照排隊順序 append 到最後面。
            player.song_queue.appendleft(track)
            embed = self._build_track_embed(
                title="⏭️ 已插播", track=track, requester=interaction.user, color=discord.Color.orange()
            )
            embed.add_field(name="📌 排隊位置", value="下一首", inline=False)
            await interaction.followup.send(embed=embed)
            return

        # 目前沒有東西在播，插播跟一般 /play 沒有差別，直接播放。
        try:
            await player.play(track)
        except Exception as e:
            print(f">>> 插播音樂時發生錯誤：{e}")
            await self.bot.notify_owner_error(
                e, interaction, extra_info=f"/play_next 播放失敗 track={getattr(track, 'title', '?')}"
            )
            await interaction.followup.send("❌ 播放音樂時發生錯誤，已回報開發者。", ephemeral=True)
            return

        embed = self._build_track_embed(
            title="🎶 開始播放", track=track, requester=interaction.user, color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="music_queue", description="顯示目前伺服器的播放佇列")
    async def queue_(self, interaction: discord.Interaction):
        player = interaction.guild.voice_client

        if player is None:
            await interaction.response.send_message("ℹ️ 我目前不在任何語音頻道中，也沒有播放佇列。", ephemeral=True)
            return

        current = getattr(player, "current", None)
        song_queue: deque = getattr(player, "song_queue", None) or deque()

        if current is None and not song_queue:
            await interaction.response.send_message("📭 目前沒有正在播放的歌曲，佇列也是空的。", ephemeral=True)
            return

        lines: list[str] = []
        if current is not None:
            status = "⏸️ 暫停中" if player.paused else "▶️ 正在播放"
            lines.append(f"**{status}：** {current.title}（{format_duration(getattr(current, 'length', None))}）")
        else:
            lines.append("**▶️ 正在播放：** （無）")

        if song_queue:
            lines.append("")
            lines.append(f"**📜 接下來（共 {len(song_queue)} 首）：**")
            queue_snapshot = list(song_queue)
            for idx, queued_track in enumerate(queue_snapshot[:10], start=1):
                lines.append(f"{idx}. {queued_track.title}（{format_duration(getattr(queued_track, 'length', None))}）")
            if len(queue_snapshot) > 10:
                lines.append(f"...還有 {len(queue_snapshot) - 10} 首沒有列出")
        else:
            lines.append("\n📭 佇列目前是空的，播完這首就結束囉，輸入 `/play` 繼續點歌吧！")

        embed = discord.Embed(
            title="🎵 播放佇列",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="music_queue_clear", description="清空目前的播放佇列（不影響正在播放的歌曲）")
    async def queue_clear(self, interaction: discord.Interaction):
        player = interaction.guild.voice_client
        song_queue: deque = getattr(player, "song_queue", None) if player else None

        if not song_queue:
            await interaction.response.send_message("ℹ️ 佇列目前是空的，沒有東西可以清除。", ephemeral=True)
            return

        removed_count = len(song_queue)
        song_queue.clear()
        await interaction.response.send_message(
            f"🗑️ 已清空佇列，移除了 {removed_count} 首歌曲（正在播放的歌曲不受影響）。", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
