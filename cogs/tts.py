from __future__ import annotations

import asyncio
import io
import re
import traceback

import discord
from discord import app_commands
from discord.ext import commands
from gtts import gTTS


TTS_COOLDOWN_SECONDS = 15.0  # 每位使用者兩次 /tts 之間的冷卻秒數
DEFAULT_FILESIZE_LIMIT = 10 * 1024 * 1024  # Discord 未加成伺服器 / 私訊的預設上傳上限 (10 MiB)


def format_size(num_bytes: int) -> str:
    """把位元組數轉成好讀的 KB / MB 字串。"""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    return f"{num_bytes / 1024:.1f} KB"


class TTS(commands.Cog):
    """文字轉語音 (Text-to-Speech) 模組，使用 gTTS 將文字轉換為語音並輸出 MP3 檔案。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="tts", description="將文字轉換成語音，並儲存為 MP3 檔案傳送")
    # gTTS 每次都要打 Google 的 API，加上 per-user 冷卻避免被連續濫用；
    # 冷卻觸發時的 CommandOnCooldown 由 main.py 的 on_app_command_error 統一回覆。
    @app_commands.checks.cooldown(1, TTS_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    @app_commands.describe(
        text="輸入你想要轉換成語音的文字",
        file_name="輸入輸出檔案的名稱（選填，預設為文字前10個字）",
        language="輸入語言代碼（選填，預設為英文 en，例如：zh-tw、en、ja 等）"
    )
    async def tts(
        self,
        interaction: discord.Interaction,
        text: str,
        file_name: str | None = None,
        language: str | None = None,
    ):
        clean_text = text.strip() if text else ""
        if not clean_text:
            await interaction.response.send_message("❌ 沒有提供文字。正在退出程式。", ephemeral=True)
            return

        default_lang_used = False
        lang = language.strip().lower() if language and language.strip() else ""
        if not lang:
            lang = "en"
            default_lang_used = True

        target_name = file_name.strip() if file_name and file_name.strip() else ""
        if not target_name:
            target_name = clean_text[:10]

        target_name = re.sub(r'[\\/*?:"<>|\r\n]+', '_', target_name).strip()
        if not target_name:
            target_name = "tts_audio"

        if not target_name.lower().endswith(".mp3"):
            full_filename = f"{target_name}.mp3"
        else:
            full_filename = target_name

        await interaction.response.defer(thinking=True)

        def generate_audio() -> io.BytesIO:
            fp = io.BytesIO()
            tts_obj = gTTS(text=clean_text, lang=lang, slow=False)
            tts_obj.write_to_fp(fp)
            fp.seek(0)
            return fp

        try:
            audio_fp = await asyncio.to_thread(generate_audio)
        except ValueError as e:
            msg = f"❌ 語言代碼 `{lang}` 不受支援或無效！常見代碼例如：`zh-tw`（繁體中文）、`en`（英文）、`ja`（日文）等。"
            await interaction.followup.send(msg, ephemeral=True)
            return
        except Exception as e:
            print(f"❌ gTTS 轉換時發生錯誤: {e}")
            traceback.print_exc()
            await interaction.followup.send(f"❌ 語音轉換失敗：{e}", ephemeral=True)
            return

        # 檔案大小檢查：伺服器內用該伺服器的上傳上限（會隨加成等級提高），
        # 私訊或取不到 guild 時退回 Discord 預設上限。
        file_size = audio_fp.getbuffer().nbytes
        size_limit = interaction.guild.filesize_limit if interaction.guild else DEFAULT_FILESIZE_LIMIT
        if file_size > size_limit:
            await interaction.followup.send(
                f"❌ 產生的 MP3 檔案大小為 {format_size(file_size)}，超過此處的上傳上限 "
                f"{format_size(size_limit)}，無法傳送。請縮短文字後再試一次。",
                ephemeral=True,
            )
            return

        discord_file = discord.File(audio_fp, filename=full_filename)

        lines = []
        if default_lang_used:
            lines.append("未輸入語言代碼，已使用預設語言 (英文) 進行轉換。")
        lines.append(f"已成功將文字轉換成語音，並儲存為 {full_filename}")

        reply_content = "\n".join(lines)
        try:
            await interaction.followup.send(content=reply_content, file=discord_file)
        except discord.HTTPException as e:
            # 保險：若上方預檢沒擋到（例如上限資訊過時），Discord 會回 413 / 錯誤碼 40005。
            if e.status == 413 or e.code == 40005:
                await interaction.followup.send(
                    f"❌ MP3 檔案（{format_size(file_size)}）超過 Discord 上傳大小限制，無法傳送。請縮短文字後再試一次。",
                    ephemeral=True,
                )
                return
            raise


async def setup(bot: commands.Bot):
    await bot.add_cog(TTS(bot))
