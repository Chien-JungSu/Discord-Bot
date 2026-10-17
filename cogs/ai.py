from __future__ import annotations

import asyncio
import os
import re
import traceback
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from cogs.database import (
    DEFAULT_AI_INSTRUCTION,
    DEFAULT_AI_MAX_TOKENS,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_TEMPERATURE,
    build_panel_link_button,
    build_panel_link_view,
    get_guild_ai_settings,
    load_ai_settings,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, '.env'))

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


class AI(commands.Cog):
    """OpenRouter 免費 AI 模型整合模組，提供 /ai 指令與 @機器人 提及智慧對話功能。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._settings_cache: Dict[str, Dict[str, Any]] = {}
        self.reload_settings()

    def reload_settings(self) -> None:
        """重新載入所有伺服器的 AI 設定至記憶體快取（由控制面板儲存後調用同步）。"""
        try:
            load_dotenv(os.path.join(BASE_DIR, '.env'), override=True)
            self._settings_cache = load_ai_settings()
        except Exception as exc:
            print(f"⚠️ 載入 AI 設定失敗: {exc}")
            self._settings_cache = {}

    def _get_guild_cfg(self, guild_id: Optional[int]) -> Dict[str, Any]:
        """讀取指定伺服器的 AI 設定。"""
        if not guild_id:
            return {
                "is_enabled": True,
                "model": DEFAULT_AI_MODEL,
                "instruction": DEFAULT_AI_INSTRUCTION,
                "channel_id": None,
                "temperature": DEFAULT_AI_TEMPERATURE,
                "max_tokens": DEFAULT_AI_MAX_TOKENS,
            }
        gid_str = str(guild_id)
        if gid_str in self._settings_cache:
            return self._settings_cache[gid_str]
        cfg = get_guild_ai_settings(gid_str)
        self._settings_cache[gid_str] = cfg
        return cfg

    async def _get_session(self) -> aiohttp.ClientSession:
        """取得或建立 aiohttp.ClientSession。"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=45.0, connect=10.0)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def cog_unload(self) -> None:
        """Cog 卸載時安全關閉 aiohttp 連線池。"""
        if self._session and not self._session.closed:
            await self._session.close()

    async def call_openrouter(
        self,
        prompt: str,
        *,
        model: str,
        instruction: str,
        temperature: float,
        max_tokens: int,
    ) -> tuple[Optional[str], Optional[str]]:
        """向 OpenRouter API 發送 chat completion 請求。

        回傳：(ai_response_text, error_message)
        """
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            load_dotenv(os.path.join(BASE_DIR, '.env'), override=True)
            api_key = os.getenv("OPENROUTER_API_KEY", "").strip()

        if not api_key:
            return (
                None,
                "⚠️ **伺服器尚未設定 OpenRouter API Key**\n"
                "請伺服器擁有者在主機 `.env` 檔案中加入 `OPENROUTER_API_KEY=你的金鑰` 後重新啟動或重載機器人。\n"
                "可至 [OpenRouter Free Models](https://openrouter.ai/openrouter/free) 免費獲取與使用！",
            )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://patricksu.nkhost.dev",
            "X-Title": "炙彗JI器人",
        }

        messages: List[Dict[str, str]] = []
        if instruction and instruction.strip():
            messages.append({"role": "system", "content": instruction.strip()})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            session = await self._get_session()
            async with session.post(OPENROUTER_API_URL, headers=headers, json=payload) as resp:
                status_code = resp.status
                if status_code == 200:
                    data = await resp.json()
                    choices = data.get("choices") or []
                    if not choices:
                        return None, "⚠️ OpenRouter 未回傳任何文字內容，請稍候重試。"
                    message = choices[0].get("message") or {}
                    content = (message.get("content") or "").strip()
                    if not content and message.get("reasoning"):
                        content = str(message.get("reasoning")).strip()
                    if not content:
                        return None, "⚠️ AI 回應內容為空，請嘗試換個提問方式。"
                    return content, None

                elif status_code == 401:
                    return None, "❌ **OpenRouter 認證失敗 (401 Unauthorized)**\n請確認 `.env` 中的 `OPENROUTER_API_KEY` 是否有效正確。"
                elif status_code == 429:
                    return None, "⏳ **OpenRouter 請求頻率過高 (429 Rate Limit)**\n當前免費模型請求次數已達短期上限，請稍候 30 秒再試！"
                elif status_code == 404:
                    return None, f"❌ **找不到指定模型 (404 Not Found)**\n模型 `{model}` 可能已下線或名稱有誤，建議至控制面板切換為 `openrouter/auto` 或其他免費模型。"
                else:
                    err_body = await resp.text()
                    try:
                        err_json = await resp.json()
                        err_msg = err_json.get("error", {}).get("message") or err_body[:200]
                    except Exception:
                        err_msg = err_body[:200]
                    return None, f"⚠️ OpenRouter 回傳錯誤 (HTTP {status_code}):\n```{err_msg}```"

        except asyncio.TimeoutError:
            return None, "⏱️ **AI 回應逾時**：OpenRouter 伺服器忙碌中或正在排隊，請稍候再試。"
        except aiohttp.ClientError as exc:
            return None, f"🌐 **網路連線異常**：無法連線至 OpenRouter 伺服器 ({exc})。"
        except Exception as exc:
            traceback.print_exc()
            return None, f"💥 **發生未預期錯誤**：`{exc}`"

    # ==========================================================================
    # /ai 斜線指令
    # ==========================================================================

    @app_commands.command(name="ai", description="向 OpenRouter 免費 AI 模型發問與智慧對話")
    @app_commands.describe(
        prompt="你想詢問 AI 的問題或對話內容",
        model="臨時指定 OpenRouter 模型 (留空則使用本伺服器控制面板預設設定)",
        system_prompt="臨時覆蓋本次回答的 AI 人設指令 (留空則使用伺服器預設)",
    )
    @app_commands.choices(
        model=[
            app_commands.Choice(name="✨ openrouter/auto (自動推薦最佳免費模型)", value="openrouter/auto"),
            app_commands.Choice(name="🦙 meta-llama/llama-3.3-70b-instruct:free", value="meta-llama/llama-3.3-70b-instruct:free"),
            app_commands.Choice(name="⚡ google/gemini-2.0-flash-exp:free", value="google/gemini-2.0-flash-exp:free"),
            app_commands.Choice(name="🧠 deepseek/deepseek-r1:free (深度思考推理)", value="deepseek/deepseek-r1:free"),
            app_commands.Choice(name="💬 deepseek/deepseek-chat:free (DeepSeek V3)", value="deepseek/deepseek-chat:free"),
            app_commands.Choice(name="🌪️ mistralai/mistral-small-24b-instruct-2501:free", value="mistralai/mistral-small-24b-instruct-2501:free"),
            app_commands.Choice(name="🚀 meta-llama/llama-3.1-8b-instruct:free (輕量快速)", value="meta-llama/llama-3.1-8b-instruct:free"),
            app_commands.Choice(name="🌐 qwen/qwen-2.5-72b-instruct:free (通義千問)", value="qwen/qwen-2.5-72b-instruct:free"),
        ]
    )
    async def ai_command(
        self,
        interaction: discord.Interaction,
        prompt: str,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ):
        clean_prompt = prompt.strip()
        if not clean_prompt:
            await interaction.response.send_message("❌ 請輸入有效的提問內容！", ephemeral=True)
            return

        guild_id = interaction.guild_id
        cfg = self._get_guild_cfg(guild_id)

        # 1. 檢查伺服器是否停用 AI 功能
        if not cfg.get("is_enabled", True):
            view = build_panel_link_view(guild_id) if guild_id else None
            await interaction.response.send_message(
                "⚠️ **本伺服器目前已由管理員停用 AI 智慧問答功能。**\n若需啟用，請伺服器管理員前往網頁控制面板開啟。",
                ephemeral=True,
                view=view,
            )
            return

        # 2. 檢查專屬頻道限制
        allowed_channel_id = cfg.get("channel_id")
        if allowed_channel_id and interaction.channel_id != allowed_channel_id:
            ch_mention = f"<#{allowed_channel_id}>"
            await interaction.response.send_message(
                f"🔒 本伺服器已設定 AI 專屬點歌/對話頻道，請至 {ch_mention} 使用 `/ai` 指令！",
                ephemeral=True,
            )
            return

        # 3. 延遲回覆 (AI 生成需數秒)
        await interaction.response.defer(thinking=True)

        # 4. 決定模型與 System Instruction
        target_model = model or cfg.get("model") or DEFAULT_AI_MODEL
        target_instruction = system_prompt or cfg.get("instruction") or DEFAULT_AI_INSTRUCTION
        temperature = float(cfg.get("temperature", DEFAULT_AI_TEMPERATURE))
        max_tokens = int(cfg.get("max_tokens", DEFAULT_AI_MAX_TOKENS))

        # 5. 調用 OpenRouter
        response_text, error_msg = await self.call_openrouter(
            clean_prompt,
            model=target_model,
            instruction=target_instruction,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        panel_view = build_panel_link_view(guild_id, label="🌐 前往面板自訂 AI 人設") if guild_id else None

        if error_msg:
            await interaction.followup.send(error_msg, view=panel_view)
            return

        # 6. 組裝美觀 Embed 回覆
        # 若提問過長，截短顯示於 Embed 標題欄
        display_question = clean_prompt if len(clean_prompt) <= 250 else f"{clean_prompt[:247]}..."

        # Discord Embed 描述上限為 4096 字元
        if len(response_text) <= 3900:
            embed = discord.Embed(
                title="🤖 炙彗 AI 智慧回覆",
                description=response_text,
                color=0x7dd3fc,
            )
            embed.add_field(name="💬 提問", value=display_question, inline=False)
            embed.set_footer(
                text=f"模型: {target_model} · 串接自 OpenRouter Free",
                icon_url=self.bot.user.display_avatar.url if self.bot.user else None,
            )
            await interaction.followup.send(embed=embed, view=panel_view)
        else:
            # 超出長度時以分段發送，避免截斷
            first_chunk = response_text[:1900]
            remaining = response_text[1900:]
            await interaction.followup.send(
                f"**💬 提問：** {display_question}\n\n**🤖 AI 回覆 ({target_model})：**\n{first_chunk}"
            )
            while remaining:
                chunk = remaining[:1900]
                remaining = remaining[1900:]
                await interaction.followup.send(chunk)

    # ==========================================================================
    # /ai_config 斜線指令：快速檢視當前伺服器 AI 配置
    # ==========================================================================

    @app_commands.command(name="ai_config", description="查看當前伺服器的 AI 智慧助手與人設配置狀態")
    async def ai_config_command(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        cfg = self._get_guild_cfg(guild_id)

        status_text = "🟢 啟用中" if cfg.get("is_enabled", True) else "🔴 已停用"
        model_name = cfg.get("model", DEFAULT_AI_MODEL)
        inst_preview = cfg.get("instruction", DEFAULT_AI_INSTRUCTION)
        if len(inst_preview) > 120:
            inst_preview = inst_preview[:117] + "..."
        chan_id = cfg.get("channel_id")
        channel_text = f"<#{chan_id}>" if chan_id else "所有文字頻道皆可使用"
        temp = cfg.get("temperature", DEFAULT_AI_TEMPERATURE)
        tokens = cfg.get("max_tokens", DEFAULT_AI_MAX_TOKENS)

        embed = discord.Embed(
            title="⚙️ 伺服器 AI 智慧助手配置狀態",
            description="管理員可隨時透過下方按鈕前往「網頁控制面板」即時自訂 AI 人設指令、切換模型與設定頻道限制！",
            color=0xa78bfa,
        )
        embed.add_field(name="功能狀態", value=status_text, inline=True)
        embed.add_field(name="預設模型", value=f"`{model_name}`", inline=True)
        embed.add_field(name="限定頻道", value=channel_text, inline=True)
        embed.add_field(name="創意度 (Temperature)", value=f"`{temp}`", inline=True)
        embed.add_field(name="最大長度 (Max Tokens)", value=f"`{tokens}`", inline=True)
        embed.add_field(name="目前人設指令 (Instruction)", value=f"```\n{inst_preview}\n```", inline=False)
        embed.set_footer(text="OpenRouter 免費模型整合 · 炙彗JI器人")

        view = build_panel_link_view(guild_id, label="🌐 前往控制面板自訂 AI 人設") if guild_id else None
        await interaction.response.send_message(embed=embed, view=view)

    # ==========================================================================
    # @機器人 提及對話事件監聽器
    # ==========================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """當成員在文字頻道標記 @機器人 時，自動以設定好的 AI 人設進行回覆。"""
        # 忽略機器人自身訊息或來自其他 Bot 的訊息
        if message.author.bot or not message.guild:
            return

        bot_user = self.bot.user
        if not bot_user or bot_user not in message.mentions:
            return

        # 濾除 @機器人 標註後的實際提問文字
        content = message.content
        content = re.sub(rf'<@!?{bot_user.id}>', '', content).strip()

        guild_id = message.guild.id
        cfg = self._get_guild_cfg(guild_id)

        # 檢查是否啟用
        if not cfg.get("is_enabled", True):
            return

        # 檢查頻道限制
        allowed_channel_id = cfg.get("channel_id")
        if allowed_channel_id and message.channel.id != allowed_channel_id:
            # 專屬頻道限制：輕量提醒
            try:
                await message.reply(
                    f"🔒 本伺服器已設定 AI 專屬對話頻道，請至 <#{allowed_channel_id}> 與我聊聊天喔！",
                    mention_author=False,
                    delete_after=10.0,
                )
            except Exception:
                pass
            return

        # 若僅標註機器人卻無提問文字，送出親切問候
        if not content:
            greeting = (
                f"嗨 {message.author.mention}！有什麼我可以幫忙的嗎？✨\n"
                "你可以直接在訊息中提及我並輸入問題，或使用 `/ai` 指令開啟智慧對話！"
            )
            try:
                await message.reply(greeting, mention_author=False)
            except Exception:
                pass
            return

        target_model = cfg.get("model") or DEFAULT_AI_MODEL
        target_instruction = cfg.get("instruction") or DEFAULT_AI_INSTRUCTION
        temperature = float(cfg.get("temperature", DEFAULT_AI_TEMPERATURE))
        max_tokens = int(cfg.get("max_tokens", DEFAULT_AI_MAX_TOKENS))

        # 顯示輸入中打字動態 (Typing Indicator)
        try:
            async with message.channel.typing():
                response_text, error_msg = await self.call_openrouter(
                    content,
                    model=target_model,
                    instruction=target_instruction,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

                if error_msg:
                    await message.reply(error_msg, mention_author=False)
                    return

                if len(response_text) <= 2000:
                    await message.reply(response_text, mention_author=False)
                else:
                    first_chunk = response_text[:1900]
                    remaining = response_text[1900:]
                    await message.reply(first_chunk, mention_author=False)
                    while remaining:
                        chunk = remaining[:1900]
                        remaining = remaining[1900:]
                        await message.channel.send(chunk)
        except Exception as exc:
            print(f"⚠️ @機器人 AI 對話處理例外: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))
