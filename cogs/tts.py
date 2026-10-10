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


# 常見的語言代碼 typo / 別名 → gTTS 實際支援的代碼。
# key 一律小寫、用 "-" 分隔（查詢前會先把使用者輸入正規化成這個格式）。
LANGUAGE_ALIASES: dict[str, str] = {
    # 日文
    "jp": "ja", "jpn": "ja", "japanese": "ja", "日文": "ja", "日語": "ja",
    # 韓文
    "kr": "ko", "kor": "ko", "korean": "ko", "韓文": "ko", "韓語": "ko",
    # 中文（未指定繁簡時預設繁體）
    "tw": "zh-TW", "zh-hant": "zh-TW", "cht": "zh-TW", "ch": "zh-TW", "chinese": "zh-TW",
    "中文": "zh-TW", "繁中": "zh-TW", "繁體中文": "zh-TW",
    "cn": "zh-CN", "zh-hans": "zh-CN", "chs": "zh-CN", "簡中": "zh-CN", "簡體中文": "zh-CN",
    "hk": "yue", "zh-hk": "yue", "cantonese": "yue", "粵語": "yue", "廣東話": "yue",
    # 英文
    "eng": "en", "english": "en", "en-us": "en", "en-gb": "en", "gb": "en", "英文": "en", "英語": "en",
    # 歐洲語系
    "ger": "de", "deu": "de", "german": "de", "德文": "de",
    "fra": "fr", "fre": "fr", "french": "fr", "法文": "fr",
    "sp": "es", "spa": "es", "spanish": "es", "西班牙文": "es",
    "ita": "it", "italian": "it",
    "rus": "ru", "russian": "ru", "俄文": "ru",
    "pt-br": "pt", "por": "pt", "portuguese": "pt",
    "gr": "el", "greek": "el",
    "dk": "da",
    "se": "sv",
    "ua": "uk",
    "cz": "cs",
    "nb": "no", "nn": "no",
    # 其他
    "he": "iw", "heb": "iw",
    "jv": "jw",
    "in": "id", "ind": "id",
    "vn": "vi", "vie": "vi", "越南文": "vi",
    "tha": "th", "泰文": "th",
    "fil": "tl",
}

try:
    from gtts.lang import tts_langs
    # 小寫 → gTTS 正確大小寫的代碼（例如 "fr-ca" → "fr-CA"）。gTTS 收到小寫的
    # "fr-ca"、"pt-pt" 時會默默退回 "fr"、"pt"，所以要先還原成正確大小寫。
    _CANONICAL_LANGS: dict[str, str] = {code.lower(): code for code in tts_langs()}
except Exception:
    _CANONICAL_LANGS = {}


def normalize_language(raw: str) -> str:
    """把使用者輸入的語言代碼正規化：統一小寫、"_" 換成 "-"，
    套用常見 typo / 別名對照，再還原成 gTTS 認得的大小寫。"""
    key = raw.strip().lower().replace("_", "-")
    key = LANGUAGE_ALIASES.get(key, key)
    return _CANONICAL_LANGS.get(key.lower(), key)


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
        raw_lang = language.strip() if language and language.strip() else ""
        lang = normalize_language(raw_lang) if raw_lang else ""
        if not lang:
            lang = "en"
            default_lang_used = True
        # 只有「實際換成不同代碼」才提示，單純大小寫差異（ZH-TW → zh-TW）不算。
        lang_converted = bool(raw_lang) and raw_lang.lower().replace("_", "-") != lang.lower()

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

        unsupported_msg = (
            f"❌ 語言代碼 `{raw_lang or lang}` 不受支援或無效！"
            "常見代碼例如：`zh-tw`（繁體中文）、`en`（英文）、`ja`（日文）等。"
        )

        # gTTS 2.5.x 的 _fallback_deprecated_lang() 會先把代碼轉小寫再比對棄用清單，
        # 導致「正確」的 zh-TW / zh-CN 也被當成棄用的 zh-tw / zh-cn 而印出警告，
        # fr-CA / pt-PT 甚至會被降級成 fr / pt。所以改成自己用 tts_langs() 驗證，
        # 再以 lang_check=False 呼叫 gTTS 跳過它的 fallback。
        # 若取不到語言清單（_CANONICAL_LANGS 為空），則退回讓 gTTS 自己檢查。
        use_own_check = bool(_CANONICAL_LANGS)
        if use_own_check and lang not in _CANONICAL_LANGS.values():
            await interaction.response.send_message(unsupported_msg, ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        def generate_audio() -> io.BytesIO:
            fp = io.BytesIO()
            tts_obj = gTTS(text=clean_text, lang=lang, slow=False, lang_check=not use_own_check)
            tts_obj.write_to_fp(fp)
            fp.seek(0)
            return fp

        try:
            audio_fp = await asyncio.to_thread(generate_audio)
        except ValueError as e:
            await interaction.followup.send(unsupported_msg, ephemeral=True)
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
        elif lang_converted:
            lines.append(f"已將語言代碼 `{raw_lang}` 自動轉換為 `{lang}`。")
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
