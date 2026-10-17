from __future__ import annotations

import asyncio
import os
import re
import time
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
    calculate_ai_token_cost,
    get_guild_ai_settings,
    get_user_ai_points,
    load_ai_settings,
    save_guild_ai_settings,
    set_user_ai_points,
    update_user_ai_points,
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
                "payer_user_id": None,
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

    def _build_ai_embed(
        self,
        response_text: str,
        prompt: str,
        model: str,
        stats: dict[str, Any],
        cost: int,
        payer_id: int,
        is_guild_sponsored: bool,
        new_balance: int,
    ) -> discord.Embed:
        """依需求建立包含模型名稱、Token消耗、思考時間與支付者的美觀 Embed 回覆。"""
        display_question = prompt if len(prompt) <= 250 else f"{prompt[:247]}..."
        embed = discord.Embed(
            title="🤖 炙彗 AI 智慧回覆",
            description=response_text if len(response_text) <= 3900 else response_text[:3900],
            color=0x7dd3fc,
        )
        embed.add_field(name="💬 提問", value=display_question, inline=False)

        payer_text = f"<@{payer_id}> (伺服器贊助)" if is_guild_sponsored else f"<@{payer_id}>"
        meta_lines = [
            f"• **模型名稱**：`{model}`",
            f"• **使用 Token 數量**：`{stats.get('total_tokens', 0):,}` (提示: {stats.get('prompt_tokens', 0):,} / 回覆: {stats.get('completion_tokens', 0):,})",
            f"• **思考時間**：`{stats.get('thinking_time', 0.0):.2f} 秒`",
            f"• **扣除費用**：`{cost:,}` ⚡AI幣 (支付者: {payer_text} · 剩餘: `{new_balance:,}` 點)",
        ]
        embed.add_field(name="📊 運算統計與計費", value="\n".join(meta_lines), inline=False)
        embed.set_footer(
            text=f"模型: {model} · 串接自 OpenRouter",
            icon_url=self.bot.user.display_avatar.url if self.bot.user else None,
        )
        return embed

    async def call_openrouter(
        self,
        prompt: str,
        *,
        model: str,
        instruction: str,
        temperature: float,
        max_tokens: int,
    ) -> tuple[Optional[str], Optional[str], dict[str, Any]]:
        """向 OpenRouter API 發送 chat completion 請求並記錄思考時間與 Token 統計。

        回傳：(ai_response_text, error_message, stats_dict)
        """
        start_time = time.perf_counter()
        default_stats = {
            "prompt_tokens": max(1, len(prompt) // 2),
            "completion_tokens": 0,
            "total_tokens": max(1, len(prompt) // 2),
            "thinking_time": 0.0,
        }

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
                default_stats,
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
                elapsed = time.perf_counter() - start_time
                status_code = resp.status
                if status_code == 200:
                    data = await resp.json()
                    choices = data.get("choices") or []
                    if not choices:
                        return None, "⚠️ OpenRouter 未回傳任何文字內容，請稍候重試。", default_stats
                    message = choices[0].get("message") or {}
                    content = (message.get("content") or "").strip()
                    if not content and message.get("reasoning"):
                        content = str(message.get("reasoning")).strip()
                    if not content:
                        return None, "⚠️ AI 回應內容為空，請嘗試換個提問方式。", default_stats

                    usage = data.get("usage") or {}
                    prompt_tokens = usage.get("prompt_tokens")
                    completion_tokens = usage.get("completion_tokens")
                    total_tokens = usage.get("total_tokens")

                    if total_tokens is None:
                        prompt_tokens = max(1, len(prompt) // 2)
                        completion_tokens = max(1, len(content) // 2)
                        total_tokens = prompt_tokens + completion_tokens

                    stats = {
                        "prompt_tokens": int(prompt_tokens or 0),
                        "completion_tokens": int(completion_tokens or 0),
                        "total_tokens": int(total_tokens or 0),
                        "thinking_time": elapsed,
                    }
                    return content, None, stats

                default_stats["thinking_time"] = elapsed
                if status_code == 401:
                    return None, "❌ **OpenRouter 認證失敗 (401 Unauthorized)**\n請確認 `.env` 中的 `OPENROUTER_API_KEY` 是否有效正確。", default_stats
                elif status_code == 429:
                    return None, "⏳ **OpenRouter 請求頻率過高 (429 Rate Limit)**\n當前免費模型請求次數已達短期上限，請稍候 30 秒再試！", default_stats
                elif status_code == 404:
                    return None, f"❌ **找不到指定模型 (404 Not Found)**\n模型 `{model}` 可能已下線或名稱有誤，建議切換為其他模型。", default_stats
                else:
                    err_body = await resp.text()
                    try:
                        err_json = await resp.json()
                        err_msg = err_json.get("error", {}).get("message") or err_body[:200]
                    except Exception:
                        err_msg = err_body[:200]
                    return None, f"⚠️ OpenRouter 回傳錯誤 (HTTP {status_code}):\n```{err_msg}```", default_stats

        except asyncio.TimeoutError:
            default_stats["thinking_time"] = time.perf_counter() - start_time
            return None, "⏱️ **AI 回應逾時**：OpenRouter 伺服器忙碌中或正在排隊，請稍候再試。", default_stats
        except aiohttp.ClientError as exc:
            default_stats["thinking_time"] = time.perf_counter() - start_time
            return None, f"🌐 **網路連線異常**：無法連線至 OpenRouter 伺服器 ({exc})。", default_stats
        except Exception as exc:
            traceback.print_exc()
            default_stats["thinking_time"] = time.perf_counter() - start_time
            return None, f"💥 **發生未預期錯誤**：`{exc}`", default_stats

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
            app_commands.Choice(name="⚡ deepseek/deepseek-v4.1-flash-1017 (DeepSeek Flash)", value="deepseek/deepseek-v4.1-flash-1017"),
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

        # 3. 檢查 AI 專屬貨幣餘額 (根據伺服器是否有指定支付者)
        payer_id = int(cfg.get("payer_user_id") or interaction.user.id)
        is_guild_sponsored = bool(cfg.get("payer_user_id"))
        current_points = get_user_ai_points(payer_id)

        if current_points <= 0:
            payer_mention = f"伺服器指定贊助者 (<@{payer_id}>)" if is_guild_sponsored else f"你 (<@{payer_id}>)"
            panel_view = build_panel_link_view(guild_id, label="🌐 前往面板查看 AI 設定") if guild_id else None
            embed_no_balance = discord.Embed(
                title="⚡ AI 專屬貨幣餘額不足",
                description=(
                    f"⚠️ **{payer_mention} 目前的 AI 專屬貨幣餘額不足（目前餘額：`0` 點）。**\n\n"
                    f"💡 **如何獲取 AI 貨幣？**\n"
                    f"• 每日使用 `/economy_daily` 簽到免費領取 **1,000+** 點 AI 貨幣（連續簽到享額外加成！）。\n"
                    f"• 聯繫伺服器擁有者或管理員從後台發放 AI 點數。"
                ),
                color=discord.Color.red(),
            )
            if is_guild_sponsored and interaction.user.id != payer_id:
                embed_no_balance.set_footer(text="提示：本伺服器設定了指定支付者，請提醒該成員簽到補足 AI 貨幣！")
            await interaction.response.send_message(embed=embed_no_balance, view=panel_view, ephemeral=True)
            return

        # 4. 延遲回覆 (AI 生成需數秒)
        await interaction.response.defer(thinking=True)

        # 5. 決定模型與 System Instruction
        target_model = model or cfg.get("model") or DEFAULT_AI_MODEL
        target_instruction = system_prompt or cfg.get("instruction") or DEFAULT_AI_INSTRUCTION
        temperature = float(cfg.get("temperature", DEFAULT_AI_TEMPERATURE))
        max_tokens = int(cfg.get("max_tokens", DEFAULT_AI_MAX_TOKENS))

        # 6. 調用 OpenRouter
        response_text, error_msg, stats = await self.call_openrouter(
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

        # 7. 計算消耗並扣除 AI 專屬貨幣
        cost = calculate_ai_token_cost(target_model, stats["total_tokens"])
        ok, new_balance = update_user_ai_points(payer_id, -cost, allow_negative=False)
        if not ok:
            set_user_ai_points(payer_id, 0)
            new_balance = 0

        # 8. 組裝美觀 Embed 回覆 (附帶模型名稱、Token數量、思考時間、支付者)
        embed = self._build_ai_embed(
            response_text=response_text,
            prompt=clean_prompt,
            model=target_model,
            stats=stats,
            cost=cost,
            payer_id=payer_id,
            is_guild_sponsored=is_guild_sponsored,
            new_balance=new_balance,
        )

        if len(response_text) <= 3900:
            await interaction.followup.send(embed=embed, view=panel_view)
        else:
            await interaction.followup.send(embed=embed, view=panel_view)
            # 超出字元以分段追加發送
            remaining = response_text[3900:]
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

        payer_id = cfg.get("payer_user_id")
        if payer_id:
            pts = get_user_ai_points(payer_id)
            payer_text = f"<@{payer_id}> (餘額: `{pts:,}` ⚡AI幣)"
        else:
            payer_text = "未指定（由每位提問者自行支付）"

        embed = discord.Embed(
            title="⚙️ 伺服器 AI 智慧助手配置狀態",
            description="管理員可隨時透過 `/ai_settings` 指令或點擊下方按鈕前往「網頁控制面板」即時自訂 AI 人設、預設模型與指定支付者！",
            color=0xa78bfa,
        )
        embed.add_field(name="功能狀態", value=status_text, inline=True)
        embed.add_field(name="預設模型", value=f"`{model_name}`", inline=True)
        embed.add_field(name="限定頻道", value=channel_text, inline=True)
        embed.add_field(name="💳 指定支付者", value=payer_text, inline=True)
        embed.add_field(name="創意度 (Temperature)", value=f"`{temp}`", inline=True)
        embed.add_field(name="最大長度 (Max Tokens)", value=f"`{tokens}`", inline=True)
        embed.add_field(name="目前人設指令 (Instruction)", value=f"```\n{inst_preview}\n```", inline=False)
        embed.set_footer(text="OpenRouter 免費模型整合 · 炙彗JI器人")

        view = build_panel_link_view(guild_id, label="🌐 前往控制面板自訂 AI 人設") if guild_id else None
        await interaction.response.send_message(embed=embed, view=view)

    # ==========================================================================
    # /ai_settings 斜線指令：伺服器管理員自訂模型、人設與指定 AI 貨幣支付者
    # ==========================================================================

    @app_commands.command(
        name="ai_settings",
        description="⚙️ [管理員] 設定本伺服器的預設 AI 模型、人設指令與指定 AI 貨幣支付者",
    )
    @app_commands.describe(
        model="選擇本伺服器預設使用的 AI 模型",
        instruction="自訂本伺服器的 AI 人設指令 (System Instruction)",
        payer="指定承擔本伺服器所有 AI 對話費用的成員 (選填，不填則保持現狀)",
        clear_payer="設為 True 則解除指定支付者（恢復由每位提問者自行支付）",
        channel="限定 AI 對話的專屬文字頻道 (留空則不限制頻道)",
        clear_channel="設為 True 則解除頻道限制（所有文字頻道皆可使用）",
        temperature="設定創意度 (0.0 ~ 1.0，預設 0.7)",
        max_tokens="設定最大回覆 Token 限制 (100 ~ 2000，預設 1000)",
        enabled="是否啟用本伺服器的 AI 智慧問答功能",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(
        model=[
            app_commands.Choice(name="✨ openrouter/auto (自動推薦最佳免費模型)", value="openrouter/auto"),
            app_commands.Choice(name="⚡ deepseek/deepseek-v4.1-flash-1017 (DeepSeek Flash)", value="deepseek/deepseek-v4.1-flash-1017"),
            app_commands.Choice(name="🦙 meta-llama/llama-3.3-70b-instruct:free", value="meta-llama/llama-3.3-70b-instruct:free"),
            app_commands.Choice(name="⚡ google/gemini-2.0-flash-exp:free", value="google/gemini-2.0-flash-exp:free"),
            app_commands.Choice(name="🧠 deepseek/deepseek-r1:free (深度思考推理)", value="deepseek/deepseek-r1:free"),
            app_commands.Choice(name="💬 deepseek/deepseek-chat:free (DeepSeek V3)", value="deepseek/deepseek-chat:free"),
            app_commands.Choice(name="🌪️ mistralai/mistral-small-24b-instruct-2501:free", value="mistralai/mistral-small-24b-instruct-2501:free"),
            app_commands.Choice(name="🚀 meta-llama/llama-3.1-8b-instruct:free (輕量快速)", value="meta-llama/llama-3.1-8b-instruct:free"),
            app_commands.Choice(name="🌐 qwen/qwen-2.5-72b-instruct:free (通義千問)", value="qwen/qwen-2.5-72b-instruct:free"),
        ]
    )
    async def ai_settings_command(
        self,
        interaction: discord.Interaction,
        model: Optional[str] = None,
        instruction: Optional[str] = None,
        payer: Optional[discord.Member] = None,
        clear_payer: Optional[bool] = False,
        channel: Optional[discord.TextChannel] = None,
        clear_channel: Optional[bool] = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        enabled: Optional[bool] = None,
    ):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ 此指令僅限在伺服器中使用！", ephemeral=True)
            return

        if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 你需要具備「管理伺服器」權限才能使用此設定指令！", ephemeral=True)
            return

        guild_id = interaction.guild_id
        cfg = dict(self._get_guild_cfg(guild_id))

        updates = []
        if model is not None:
            cfg["model"] = model.strip()
            updates.append(f"• **預設模型**：`{cfg['model']}`")

        if instruction is not None:
            cfg["instruction"] = instruction.strip()
            updates.append(f"• **人設指令**：已更新（長度 {len(cfg['instruction'])} 字元）")

        if clear_payer:
            cfg["payer_user_id"] = None
            updates.append("• **指定支付者**：已清除（恢復由每位提問者自行支付）")
        elif payer is not None:
            cfg["payer_user_id"] = payer.id
            pts = get_user_ai_points(payer.id)
            updates.append(f"• **指定支付者**：{payer.mention}（目前餘額 `{pts:,}` ⚡AI幣）")

        if clear_channel:
            cfg["channel_id"] = None
            updates.append("• **頻道限制**：已解除（所有文字頻道皆可使用）")
        elif channel is not None:
            cfg["channel_id"] = channel.id
            updates.append(f"• **限定頻道**：{channel.mention}")

        if temperature is not None:
            cfg["temperature"] = max(0.0, min(1.0, float(temperature)))
            updates.append(f"• **創意度**：`{cfg['temperature']}`")

        if max_tokens is not None:
            cfg["max_tokens"] = max(100, min(2000, int(max_tokens)))
            updates.append(f"• **最大長度**：`{cfg['max_tokens']}` Tokens")

        if enabled is not None:
            cfg["is_enabled"] = bool(enabled)
            updates.append(f"• **AI 功能**：{'🟢 已啟用' if cfg['is_enabled'] else '🔴 已停用'}")

        if not updates:
            await interaction.response.send_message(
                "ℹ️ 未提供任何需要變更的設定參數。可使用 `/ai_config` 檢視當前設定狀態。",
                ephemeral=True,
            )
            return

        save_guild_ai_settings(str(guild_id), cfg)
        self.reload_settings()

        embed = discord.Embed(
            title="✅ 伺服器 AI 設定已成功更新！",
            description="\n".join(updates),
            color=discord.Color.green(),
        )
        embed.set_footer(text="設定已即時同步至資料庫與網頁控制面板")
        view = build_panel_link_view(guild_id, label="🌐 前往控制面板查看")
        await interaction.response.send_message(embed=embed, view=view)

    # ==========================================================================
    # @機器人 提及對話事件監聽器
    # ==========================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """當成員在文字頻道標記 @機器人 時，自動以設定好的 AI 人設進行回覆並以 Embed 包裝呈現。"""
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

        # 檢查 AI 專屬貨幣餘額
        payer_id = int(cfg.get("payer_user_id") or message.author.id)
        is_guild_sponsored = bool(cfg.get("payer_user_id"))
        current_points = get_user_ai_points(payer_id)

        if current_points <= 0:
            payer_mention = f"伺服器指定贊助者 (<@{payer_id}>)" if is_guild_sponsored else f"{message.author.mention}"
            panel_view = build_panel_link_view(guild_id) if guild_id else None
            embed_no_balance = discord.Embed(
                title="⚡ AI 專屬貨幣餘額不足",
                description=(
                    f"⚠️ **{payer_mention} 目前的 AI 專屬貨幣餘額不足（目前餘額：`0` 點）。**\n\n"
                    f"💡 **如何獲取 AI 貨幣？**\n"
                    f"• 每日使用 `/economy_daily` 簽到免費領取 **1,000+** 點 AI 貨幣（連續簽到享額外加成！）。\n"
                    f"• 聯繫伺服器擁有者或管理員從後台發放 AI 點數。"
                ),
                color=discord.Color.red(),
            )
            try:
                await message.reply(embed=embed_no_balance, view=panel_view, mention_author=False)
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
                response_text, error_msg, stats = await self.call_openrouter(
                    content,
                    model=target_model,
                    instruction=target_instruction,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

                if error_msg:
                    await message.reply(error_msg, mention_author=False)
                    return

                # 計費扣款
                cost = calculate_ai_token_cost(target_model, stats["total_tokens"])
                ok, new_balance = update_user_ai_points(payer_id, -cost, allow_negative=False)
                if not ok:
                    set_user_ai_points(payer_id, 0)
                    new_balance = 0

                embed = self._build_ai_embed(
                    response_text=response_text,
                    prompt=content,
                    model=target_model,
                    stats=stats,
                    cost=cost,
                    payer_id=payer_id,
                    is_guild_sponsored=is_guild_sponsored,
                    new_balance=new_balance,
                )
                panel_view = build_panel_link_view(guild_id, label="🌐 前往面板自訂 AI 人設") if guild_id else None

                if len(response_text) <= 3900:
                    await message.reply(embed=embed, view=panel_view, mention_author=False)
                else:
                    await message.reply(embed=embed, view=panel_view, mention_author=False)
                    remaining = response_text[3900:]
                    while remaining:
                        chunk = remaining[:1900]
                        remaining = remaining[1900:]
                        await message.channel.send(chunk)
        except Exception as exc:
            print(f"⚠️ @機器人 AI 對話處理例外: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))
