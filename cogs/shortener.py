import asyncio
import os
import re
import urllib.parse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from cogs.database import (
    get_daily_api_usage,
    record_api_call_success,
    mark_api_quota_exhausted,
)
from cogs.sanitize import redact_secrets
from cogs.tls import create_verified_ssl_context

REURL_API_URL = "https://api.reurl.cc/shorten"
SHORTEN_COOLDOWN_SECONDS = 5.0
REURL_DAILY_LIMIT = 100  # Reurl 免費 API 官方每日限制 100 次

# 網址正規表示式，用於從聊天訊息中擷取第一個合法的網址
URL_REGEX = re.compile(
    r'(?:https?://|www\.)[^\s<>"\'`]+',
    re.IGNORECASE,
)


def normalize_url(raw_url: str | None) -> str | None:
    """清理並驗證使用者輸入的網址。

    - 移除首尾空白。
    - 若使用者未輸入協定（例如只打 www.example.com 或 example.com/path），
      只要包含小數點且不以特殊符號開頭，自動補上 https://。
    - 驗證 scheme 必須為 http 或 https，且 netloc 不得為空。
    """
    if not raw_url:
        return None

    cleaned = raw_url.strip()
    if not cleaned:
        return None

    # 檢查是否已有 scheme
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', cleaned):
        if '.' in cleaned and not cleaned.startswith(('.', '/', '?')):
            cleaned = f"https://{cleaned}"
        else:
            return None

    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme.lower() not in ('http', 'https'):
        return None
    if not parsed.netloc:
        return None

    return cleaned


def extract_first_url(text: str | None) -> str | None:
    """從一段文字中擷取第一個網址並正規化。"""
    if not text:
        return None
    match = URL_REGEX.search(text)
    if not match:
        return None
    return normalize_url(match.group(0))


def is_quota_exceeded(status: int, data: dict | str) -> bool:
    """判斷 Reurl API 回應是否為達到請求上限 / 配額耗盡。"""
    if status == 429:
        return True

    msg = ""
    code = ""
    if isinstance(data, dict):
        # 依 OpenAPI 規格書，失敗訊息可能在 msg 或 message
        msg = str(data.get("msg") or data.get("message") or "").lower()
        code = str(data.get("code") or "").lower()
    elif isinstance(data, str):
        msg = data.lower()

    if code.startswith("429"):
        return True

    keywords = (
        "quota", "limit", "exceed", "too many requests", "rate limit",
        "100", "次數", "上限", "超額", "額度", "頻率",
    )
    return any(k in msg for k in keywords)


class ShortenerView(discord.ui.View):
    """附帶一鍵前往按鈕的 UI View。"""

    def __init__(self, short_url: str, original_url: str):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="🔗 前往短網址",
                url=short_url,
                style=discord.ButtonStyle.link,
            )
        )
        if len(original_url) <= 512:
            self.add_item(
                discord.ui.Button(
                    label="🌐 原始網址",
                    url=original_url,
                    style=discord.ButtonStyle.link,
                )
            )


class QuotaExceededView(discord.ui.View):
    """額度用盡時提供快速前往原始網址的按鈕。"""

    def __init__(self, original_url: str):
        super().__init__(timeout=None)
        if len(original_url) <= 512:
            self.add_item(
                discord.ui.Button(
                    label="🌐 前往原始網址",
                    url=original_url,
                    style=discord.ButtonStyle.link,
                )
            )


def build_result_embed(
    short_url: str,
    original_url: str,
    utm_info: dict[str, str] | None = None,
    current_usage: int | None = None,
) -> discord.Embed:
    """建立美觀的短網址結果 Embed。"""
    embed = discord.Embed(
        title="🔗 網址縮短成功",
        description="已透過 **reurl.cc** 成功產生短網址！",
        color=0x00A8FC,  # Reurl 品牌藍色調
        timestamp=discord.utils.utcnow(),
    )

    # 短網址：粗體呈現標準 URL（Discord 會自動轉為藍色超連結，絕不破版）
    embed.add_field(
        name="✨ 短網址",
        value=f"**{short_url}**",
        inline=False,
    )

    # 原始網址：正常長度直接顯示為乾淨網址；過長時提供中文錨點連結與截斷預覽
    if len(original_url) <= 120:
        embed.add_field(
            name="📎 原始網址",
            value=original_url,
            inline=False,
        )
    else:
        embed.add_field(
            name="📎 原始網址",
            value=f"[點此開啟完整原始連結]({original_url})\n`{original_url[:100]}...`",
            inline=False,
        )

    # 顯示 UTM 參數
    if utm_info:
        valid_utms = [f"• **{k}**: `{v}`" for k, v in utm_info.items() if v]
        if valid_utms:
            embed.add_field(
                name="🏷️ UTM 追蹤標籤",
                value="\n".join(valid_utms),
                inline=False,
            )

    footer_text = "縮網址服務由 reurl.cc 提供"
    if current_usage is not None:
        footer_text += f" | 今日已使用: {current_usage}/{REURL_DAILY_LIMIT} 次"
    embed.set_footer(text=footer_text)
    return embed


def build_missing_key_embed() -> discord.Embed:
    """當 REURL_API_KEY 未設定時的引導 Embed。"""
    embed = discord.Embed(
        title="❌ 尚未設定 Reurl API 金鑰",
        description=(
            "本指令需要連接 **reurl.cc** 的 API，伺服器管理員尚未設定 API 金鑰。\n\n"
            "**設定步驟：**\n"
            "1. 前往 [reurl.cc](https://reurl.cc/) 註冊或登入會員。\n"
            "2. 進入會員專區的「API 說明 / 開發者專區」取得您的專屬 **API Key**。\n"
            "3. 在專案根目錄的 `.env` 檔案中新增：\n"
            "```env\nREURL_API_KEY=你的金鑰\n```\n"
            "4. 重新啟動機器人即可使用！"
        ),
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    return embed


def build_quota_exceeded_embed(original_url: str, current_usage: int = REURL_DAILY_LIMIT) -> discord.Embed:
    """當每日 100 次額度已用盡時呈現的專屬 Embed。"""
    embed = discord.Embed(
        title="⚠️ Reurl 每日縮網址額度已達上限",
        description=(
            "很抱歉，**reurl.cc** 官方免費 API 設有 **每日 100 次** 的請求額度上限。\n"
            f"目前今日的額度已全數用盡（**{current_usage} / {REURL_DAILY_LIMIT} 次**），"
            "將於 **每日午夜（台灣時間 00:00）** 自動重置計數！"
        ),
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow(),
    )
    if len(original_url) <= 120:
        embed.add_field(
            name="📎 您想縮短的原始網址",
            value=original_url,
            inline=False,
        )
    else:
        embed.add_field(
            name="📎 您想縮短的原始網址",
            value=f"[點此開啟完整原始連結]({original_url})\n`{original_url[:100]}...`",
            inline=False,
        )
    embed.add_field(
        name="💡 建議替代方案",
        value=(
            "• 您仍可以直接使用下方的「🌐 前往原始網址」按鈕開啟或複製原連結。\n"
            "• 請於午夜重置後再試，或至 [reurl.cc](https://reurl.cc/) 升級方案獲得更高額度。"
        ),
        inline=False,
    )
    embed.set_footer(text=f"每日額度：{REURL_DAILY_LIMIT} 次 | 狀態：今日已額滿")
    return embed


async def request_reurl_shorten(
    api_key: str,
    target_url: str,
    utm_source: str | None = None,
    utm_medium: str | None = None,
    utm_campaign: str | None = None,
    utm_term: str | None = None,
    utm_content: str | None = None,
) -> tuple[bool, str, dict]:
    """向 reurl.cc API 發送縮網址請求（完全符合 OpenAPI 3.0 規範）。

    回傳：
        (success: bool, short_url_or_error_msg: str, raw_response: dict)
    """
    # 根據 OpenAPI 規範：header 為 reurl-api-key（去除頭尾引號與空白）
    clean_key = api_key.strip().strip('"').strip("'")
    headers = {
        "Content-Type": "application/json",
        "reurl-api-key": clean_key,
    }

    # 根據 OpenAPI 規範：支援 url 與各 GA UTM 參數
    payload: dict[str, str] = {
        "url": target_url,
    }
    if utm_source and utm_source.strip():
        payload["utm_source"] = utm_source.strip()
    if utm_medium and utm_medium.strip():
        payload["utm_medium"] = utm_medium.strip()
    if utm_campaign and utm_campaign.strip():
        payload["utm_campaign"] = utm_campaign.strip()
    if utm_term and utm_term.strip():
        payload["utm_term"] = utm_term.strip()
    if utm_content and utm_content.strip():
        payload["utm_content"] = utm_content.strip()

    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=create_verified_ssl_context())
        ) as session:
            async with session.post(
                REURL_API_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                status = resp.status
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    data = {"raw_text": text}

                # 優先檢查是否觸發配額超額限制 (HTTP 429 或含有配額關鍵字)
                if is_quota_exceeded(status, data):
                    return False, "QUOTA_EXCEEDED", data

                # 依規格書 200 回應結構：{"res": "success", "short_url": "...", "original_url": "..."}
                if status == 200:
                    short_url = data.get("short_url")
                    if short_url:
                        return True, short_url, data
                    msg = data.get("msg") or data.get("message") or "未知錯誤（未取得 short_url）"
                    return False, f"縮網址失敗：{msg}", data

                # 401 / 403 授權失敗
                if status in (401, 403):
                    msg = data.get("msg") or data.get("message") or "未授權"
                    return (
                        False,
                        f"❌ Reurl API 金鑰無效或未授權（HTTP {status}：{msg}），請確認 `.env` 中的 `REURL_API_KEY` 是否正確。",
                        data,
                    )

                # 依規格書 400 回應結構：{"code": "...", "msg": "..."}
                if status == 400:
                    code = data.get("code")
                    msg = data.get("msg") or data.get("message") or "輸入的網址格式不正確或不被支援"
                    code_str = f"（代碼：{code}）" if code else ""
                    return False, f"❌ 縮網址請求失敗{code_str}：{msg}", data

                msg = data.get("msg") or data.get("message") or str(data)
                return False, f"⚠️ Reurl 伺服器回應異常（HTTP {status}：{msg}），請稍後再試！", data

    except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError) as ssl_err:
        return False, f"⚠️ 與 Reurl 伺服器建立安全連線失敗（SSL 憑證問題）：{redact_secrets(ssl_err)}", {}
    except asyncio.TimeoutError:
        return False, "⚠️ 連線至 Reurl 伺服器逾時，請稍後再試！", {}
    except aiohttp.ClientError as client_err:
        return False, f"⚠️ 連線至 Reurl 伺服器失敗：{redact_secrets(client_err)}", {}
    except Exception as exc:
        return False, f"⚠️ 發生未預期的錯誤：{redact_secrets(exc)}", {}


class Shortener(commands.Cog):
    """短網址產生器模組，連接 reurl.cc API 將長網址縮短。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 註冊右鍵訊息 Context Menu：縮短訊息中的網址
        self.ctx_menu = app_commands.ContextMenu(
            name="縮短訊息中的網址",
            callback=self.shorten_message_context,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self):
        """卸載模組時移除 Context Menu，避免重複註冊錯誤。"""
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    async def _handle_shorten_logic(
        self,
        interaction: discord.Interaction,
        raw_url: str,
        utm_source: str | None = None,
        utm_medium: str | None = None,
        utm_campaign: str | None = None,
        ephemeral: bool = False,
    ):
        """共用的縮網址處理邏輯（供 /shorten、/reurl 與 Context Menu 共同呼叫）。"""
        deferred = False
        try:
            await interaction.response.defer(thinking=True, ephemeral=ephemeral)
            deferred = True
        except (discord.NotFound, discord.HTTPException) as exc:
            print(f">>> [shortener] interaction.defer() 失敗: {exc}")

        async def send_response(content=None, embed=None, view=None, is_ephemeral=ephemeral):
            nonlocal deferred
            kwargs = {}
            if content is not None:
                kwargs["content"] = content
            if embed is not None:
                kwargs["embed"] = embed
            if view is not None:
                kwargs["view"] = view
            kwargs["ephemeral"] = is_ephemeral

            if deferred:
                try:
                    return await interaction.followup.send(**kwargs)
                except discord.HTTPException as exc:
                    print(f">>> [shortener] followup.send 失敗: {exc}")
                    if getattr(exc, "code", None) in (10062, 40060):
                        return None
                    raise
            try:
                return await interaction.response.send_message(**kwargs)
            except discord.HTTPException as exc:
                print(f">>> [shortener] send_message 失敗: {exc}")
                if getattr(exc, "code", None) not in (40060, 10062):
                    raise
                deferred = True
                return await interaction.followup.send(**kwargs)

        # 1. 檢查 API Key（若未載入則即時重新讀取 .env，讓使用者無須重啟即可生效）
        api_key = os.getenv("REURL_API_KEY")
        if not api_key:
            dotenv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
            load_dotenv(dotenv_path=dotenv_path, override=True)
            api_key = os.getenv("REURL_API_KEY")

        if not api_key or not api_key.strip():
            embed = build_missing_key_embed()
            await send_response(embed=embed, is_ephemeral=True)
            return

        api_key = api_key.strip().strip('"').strip("'")

        # 2. 驗證與正規化網址
        normalized = normalize_url(raw_url)
        if not normalized:
            await send_response(
                content="❌ 請輸入有效的網址！（例如：`https://example.com` 或 `www.example.com`）",
                is_ephemeral=True,
            )
            return

        # 3. 檢查本地每日使用配額（預先防護，若已達 100 次直接提示，免去無效的 API 請求）
        current_usage, _ = get_daily_api_usage("reurl")
        if current_usage >= REURL_DAILY_LIMIT:
            embed = build_quota_exceeded_embed(normalized, current_usage)
            view = QuotaExceededView(normalized)
            await send_response(embed=embed, view=view, is_ephemeral=True)
            return

        # 4. 發送請求縮網址
        success, result, raw_data = await request_reurl_shorten(
            api_key=api_key,
            target_url=normalized,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
        )

        if not success:
            # 配額耗盡例外處理 (HTTP 429 或含有配額超額關鍵字)
            if result == "QUOTA_EXCEEDED" or is_quota_exceeded(0, raw_data):
                mark_api_quota_exhausted("reurl", REURL_DAILY_LIMIT)
                await self.bot.notify_owner_error(
                    Exception(f"Reurl API 每日請求上限（{REURL_DAILY_LIMIT} 次）已達上限"),
                    interaction,
                    extra_info=f"shorten command reached daily quota ({REURL_DAILY_LIMIT})",
                )
                embed = build_quota_exceeded_embed(normalized, REURL_DAILY_LIMIT)
                view = QuotaExceededView(normalized)
                await send_response(embed=embed, view=view, is_ephemeral=True)
                return

            # 其他失敗情境
            if "REURL_API_KEY" in result or "未授權" in result or "憑證" in result:
                await self.bot.notify_owner_error(
                    Exception(result),
                    interaction,
                    extra_info=f"shorten command failed for target={normalized}",
                )
            await send_response(content=result, is_ephemeral=True)
            return

        # 5. 成功：依 API 回傳的 original_url（含 UTM）或原始輸入作為目標
        dest_url = (raw_data.get("original_url") or "").strip() or normalized
        utm_info = {}
        if utm_source:
            utm_info["utm_source"] = utm_source
        if utm_medium:
            utm_info["utm_medium"] = utm_medium
        if utm_campaign:
            utm_info["utm_campaign"] = utm_campaign

        new_usage = record_api_call_success("reurl")
        embed = build_result_embed(result, dest_url, utm_info=utm_info, current_usage=new_usage)
        view = ShortenerView(result, dest_url)
        await send_response(embed=embed, view=view, is_ephemeral=ephemeral)

    @app_commands.command(name="shorten", description="使用 reurl.cc 將長網址縮短為短網址")
    @app_commands.describe(
        url="請輸入要縮短的原始網址（例如：https://example.com）",
        utm_source="GA utm_source 流量來源標籤（選填，例如：FB_AD、discord 等）",
        utm_medium="GA utm_medium 行銷媒介（選填，例如：cpc、social 等）",
        utm_campaign="GA utm_campaign 行銷活動名稱（選填，例如：summer_sale 等）",
        ephemeral="是否僅自己可見（選填，預設為 False，即公開分享）",
    )
    @app_commands.checks.cooldown(1, SHORTEN_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def shorten(
        self,
        interaction: discord.Interaction,
        url: str,
        utm_source: str | None = None,
        utm_medium: str | None = None,
        utm_campaign: str | None = None,
        ephemeral: bool = False,
    ):
        await self._handle_shorten_logic(
            interaction,
            url,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            ephemeral=ephemeral,
        )

    @app_commands.command(name="reurl", description="使用 reurl.cc 將長網址縮短為短網址（同 /shorten）")
    @app_commands.describe(
        url="請輸入要縮短的原始網址（例如：https://example.com）",
        utm_source="GA utm_source 流量來源標籤（選填，例如：FB_AD、discord 等）",
        utm_medium="GA utm_medium 行銷媒介（選填，例如：cpc、social 等）",
        utm_campaign="GA utm_campaign 行銷活動名稱（選填，例如：summer_sale 等）",
        ephemeral="是否僅自己可見（選填，預設為 False，即公開分享）",
    )
    @app_commands.checks.cooldown(1, SHORTEN_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def reurl(
        self,
        interaction: discord.Interaction,
        url: str,
        utm_source: str | None = None,
        utm_medium: str | None = None,
        utm_campaign: str | None = None,
        ephemeral: bool = False,
    ):
        await self._handle_shorten_logic(
            interaction,
            url,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            ephemeral=ephemeral,
        )

    async def shorten_message_context(self, interaction: discord.Interaction, message: discord.Message):
        """右鍵訊息快捷縮網址：自動擷取該訊息內的第一個網址並縮短。"""
        url = extract_first_url(message.content)
        if not url:
            await interaction.response.send_message(
                "❌ 此訊息中沒有找到任何可縮短的網址！",
                ephemeral=True,
            )
            return

        # Context Menu 預設為私密回覆 (ephemeral=True)，避免打擾頻道其他成員
        await self._handle_shorten_logic(interaction, url, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Shortener(bot))
