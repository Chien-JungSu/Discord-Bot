from __future__ import annotations

import asyncio
import os
import re
import time
import traceback
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from cogs.database import (
    AI_PROVIDER_MODELS,
    DEFAULT_AI_INSTRUCTION,
    DEFAULT_AI_MAX_TOKENS,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_TEMPERATURE,
    build_panel_link_button,
    build_panel_link_view,
    calculate_ai_token_cost,
    get_guild_ai_settings,
    get_user_ai_points,
    is_guild_ai_authorized,
    load_ai_settings,
    save_guild_ai_settings,
    set_user_ai_points,
    update_user_ai_points,
)
from cogs.llm_router import router, strip_thinking_tags, strip_roleplay_prefix
from cogs.memory import memory_manager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, '.env'))


def split_message_chunks(text: str, max_chars: int = 1900) -> List[str]:
    """將長文本切割為不超過 max_chars 的區塊，若在程式碼區塊內斷開則自動補齊關閉與開啟 code fence。"""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk = []
    current_len = 0
    in_code_block = False
    code_lang = ""

    for line in lines:
        line_len = len(line) + 1  # 包含換行符
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_lang = stripped[3:].strip()
            else:
                in_code_block = False
                code_lang = ""

        if current_len + line_len > max_chars and current_chunk:
            if in_code_block:
                current_chunk.append("```")
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            if in_code_block:
                current_chunk.append(f"```{code_lang}")
                current_len = len(current_chunk[0]) + 1
            else:
                current_len = 0

        current_chunk.append(line)
        current_len += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


class AIChannelActionView(discord.ui.View):
    """回覆下方的互動按鈕：控制面板連結 + 一鍵清除對話記憶。"""

    def __init__(self, guild_id: Optional[int], channel_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        if guild_id:
            self.add_item(build_panel_link_button(guild_id, label="控制面板 AI 設定"))

    @discord.ui.button(label="清除頻道記憶", style=discord.ButtonStyle.secondary)
    async def clear_memory_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        count = memory_manager.clear_channel(self.channel_id)
        button.disabled = True
        button.label = "已清除記憶"
        button.style = discord.ButtonStyle.danger
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"已成功清除本頻道最近 {count} 則對話記憶！下一次對話將開啟全新上下文。",
            ephemeral=True,
        )


class AI(commands.Cog):
    """多供應商免費 LLM 智慧對話模組 (Gemini / NVIDIA / Mistral)。

    支援文字/視覺圖片雙模態、自動限速 429 容錯容災輪詢、每頻道 15 則滑動視窗記憶與經濟系統 AI 貨幣計費。
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._settings_cache: Dict[str, Dict[str, Any]] = {}
        self.reload_settings()

    def reload_settings(self) -> None:
        """重新載入所有伺服器的 AI 設定至記憶體快取。"""
        try:
            load_dotenv(os.path.join(BASE_DIR, '.env'), override=True)
            self._settings_cache = load_ai_settings()
            router.reload_env_keys()
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

    async def cog_unload(self) -> None:
        """Cog 卸載時安全關閉 router 連線資源。"""
        await router.close()

    def _build_ai_embed(
        self,
        response_text: str,
        prompt: str,
        stats: dict[str, Any],
        cost: int,
        payer_id: int,
        is_guild_sponsored: bool,
        new_balance: int,
        payer_name: str = "User",
        image_url: Optional[str] = None,
    ) -> discord.Embed:
        """依照簡潔優雅風格 (圖二) 建立 AI 回覆 Embed：
        無贅餘標題與欄位，僅保留純淨 AI 回覆，底部以分隔線與 -# 子文字呈現計費統計、模型與耗時。
        格式範例：
        [AI 回覆內容]

        ---
        -# 0.00/C | I 33C/0.00 | O 120C/0.00 | TC 0.00 | Pay Nicko | deepseek-v4.1-flash | 4.91s
        """
        # 提取模型名稱 (去除 gemini: 或 nvidia: 前綴，精簡顯示如 gemini-2.5-flash-lite / llama-3.2-11b-vision-instruct)
        model_raw = str(stats.get("model") or "auto")
        if ":" in model_raw:
            model_raw = model_raw.split(":", 1)[1]
        clean_model = model_raw.split("/")[-1] if "/" in model_raw else model_raw

        # 提取 Token 統計與耗時
        prompt_tokens = stats.get("prompt_tokens", 0) or 0
        completion_tokens = stats.get("completion_tokens", 0) or 0
        total_tokens = stats.get("total_tokens", 0) or (prompt_tokens + completion_tokens)
        thinking_time = float(stats.get("thinking_time", 0.0) or 0.0)

        # 格式化單位費率、輸入/輸出費用與總費用 (完全對齊圖二 0.00/C 格式)
        if total_tokens > 0 and cost > 0:
            unit_rate = (cost / total_tokens) * 1000
            p_ratio = prompt_tokens / total_tokens if total_tokens > 0 else 0.5
            input_cost = cost * p_ratio
            output_cost = cost - input_cost
            unit_rate_str = f"{unit_rate:.2f}/C"
            input_cost_str = f"{input_cost:.2f}"
            output_cost_str = f"{output_cost:.2f}"
            total_cost_str = f"{float(cost):.2f}"
        else:
            unit_rate_str = "0.00/C"
            input_cost_str = "0.00"
            output_cost_str = "0.00"
            total_cost_str = f"{float(cost):.2f}"

        clean_payer = payer_name.replace("|", "").strip() or "User"
        footer_text = (
            f"{unit_rate_str} | I {prompt_tokens}C/{input_cost_str} | "
            f"O {completion_tokens}C/{output_cost_str} | TC {total_cost_str} | "
            f"Pay {clean_payer} | {clean_model} | {thinking_time:.2f}s"
        )
        divider = "─" * 36
        footer_block = f"\n\n-# {divider}\n-# {footer_text}"

        # 內容保護 (Embed description 上限 4096 字元，預留分隔線與 footer 空間)
        max_content_len = 4096 - len(footer_block)
        clean_response = strip_roleplay_prefix(strip_thinking_tags(response_text or "")).rstrip()
        if not clean_response:
            clean_response = "（無回覆內容）"
        elif len(clean_response) > max_content_len:
            clean_response = f"{clean_response[:max_content_len - 3]}..."

        desc = f"{clean_response}{footer_block}"

        # 乾淨高質感的 Discord Blurple 邊框 (與圖二相同)
        embed = discord.Embed(
            description=desc,
            color=0x5865F2,
        )

        if image_url:
            embed.set_thumbnail(url=image_url)

        return embed

    # ==========================================================================
    # /ai 斜線指令 (支援文字與圖片附件)
    # ==========================================================================

    @app_commands.command(name="ai", description="向免費 AI 智慧多供應商閘道發問（支援文字與圖片雙模態）")
    @app_commands.describe(
        prompt="你想詢問 AI 的問題或對話內容",
        image="可選附帶圖片（支援 PNG/JPG/WebP/GIF，自動啟用視覺多模態分析）",
        provider="臨時指定優先使用的 AI 提供商 (預設 auto 自動容錯容災鏈)",
        system_prompt="臨時覆蓋本次回答的 AI 人設指令 (留空則使用伺服器預設)",
    )
    @app_commands.choices(
        provider=[
            app_commands.Choice(name="auto (text, vision)", value="auto"),
            app_commands.Choice(name="gemini-flash-latest (text, vision)", value="gemini"),
            app_commands.Choice(name="meta/llama-3.2-11b-vision-instruct (text, vision)", value="nvidia"),
            app_commands.Choice(name="ministral-8b-latest (text, vision)", value="mistral"),
        ]
    )
    async def ai_command(
        self,
        interaction: discord.Interaction,
        prompt: str,
        image: Optional[discord.Attachment] = None,
        provider: Optional[str] = "auto",
        system_prompt: Optional[str] = None,
    ):
        clean_prompt = prompt.strip()
        if not clean_prompt and not image:
            await interaction.response.send_message("請輸入提問內容或上傳圖片！", ephemeral=True)
            return

        guild_id = interaction.guild_id

        # 0. 封測階段伺服器授權檢查
        if not is_guild_ai_authorized(guild_id):
            panel_view = build_panel_link_view(guild_id) if guild_id else None
            embed_unauth = discord.Embed(
                title="AI 智慧助手內部封測中",
                description=(
                    "**本伺服器尚未獲得 AI 智慧問答功能的封測使用授權。**\n\n"
                    "**封測說明**：\n"
                    "• 因目前處於初階測試階段且各免費 API 具備頻率上限，目前預設僅開放給機器人擁有者伺服器。\n"
                    "• 如需開通本伺服器權限，請聯繫機器人擁有者至後台控制面板發放封測授權！"
                ),
                color=0xf59e0b,
            )
            embed_unauth.set_footer(text="待測試穩定後將全面開放所有伺服器使用！")
            await interaction.response.send_message(embed=embed_unauth, view=panel_view, ephemeral=True)
            return

        cfg = self._get_guild_cfg(guild_id)

        # 1. 檢查伺服器是否停用 AI 功能
        if not cfg.get("is_enabled", True):
            view = build_panel_link_view(guild_id) if guild_id else None
            await interaction.response.send_message(
                "**本伺服器目前已由管理員停用 AI 智慧問答功能。**\n若需啟用，請伺服器管理員前往網頁控制面板開啟。",
                ephemeral=True,
                view=view,
            )
            return

        # 2. 檢查專屬頻道限制
        allowed_channel_id = cfg.get("channel_id")
        if allowed_channel_id and interaction.channel_id != allowed_channel_id:
            await interaction.response.send_message(
                f"本伺服器已設定 AI 專屬點歌/對話頻道，請至 <#{allowed_channel_id}> 使用 `/ai` 指令！",
                ephemeral=True,
            )
            return

        # 3. 檢查 AI 專屬貨幣餘額
        payer_id = int(cfg.get("payer_user_id") or interaction.user.id)
        is_guild_sponsored = bool(cfg.get("payer_user_id"))
        current_points = get_user_ai_points(payer_id)

        if current_points <= 0:
            payer_mention = f"伺服器指定贊助者 (<@{payer_id}>)" if is_guild_sponsored else f"你 (<@{payer_id}>)"
            panel_view = build_panel_link_view(guild_id, label="查看 AI 設定") if guild_id else None
            embed_no_balance = discord.Embed(
                title="AI 專屬貨幣餘額不足",
                description=(
                    f"**{payer_mention} 目前的 AI 專屬貨幣餘額不足（目前餘額：`0` 點）。**\n\n"
                    f"**如何獲取 AI 貨幣？**\n"
                    f"• 每日使用 `/economy_daily` 簽到免費領取 **1,000+** 點 AI 貨幣（連續簽到享額外加成！）。\n"
                    f"• 聯繫伺服器擁有者或管理員從後台發放 AI 點數。"
                ),
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed_no_balance, view=panel_view, ephemeral=True)
            return

        # 4. 延遲回覆
        await interaction.response.defer(thinking=True)

        # 5. 圖片 URL 提取
        img_url = None
        if image:
            content_type = image.content_type or ""
            filename = image.filename.lower()
            if content_type.startswith("image/") or any(filename.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]):
                img_url = image.url
            else:
                await interaction.followup.send("⚠️ 上傳的附件非支援的圖片格式 (僅支援 PNG/JPG/WebP/GIF)。", ephemeral=True)
                return

        # 6. 人設指令與對話記憶
        channel_id = interaction.channel_id
        instruction = system_prompt or memory_manager.get_channel_persona(
            channel_id,
            fallback_default=cfg.get("instruction") or DEFAULT_AI_INSTRUCTION,
        )
        temperature = float(cfg.get("temperature", DEFAULT_AI_TEMPERATURE))
        max_tokens = int(cfg.get("max_tokens", DEFAULT_AI_MAX_TOKENS))

        # 取得歷史上下文並加入當前提問
        context_messages = memory_manager.get_context_messages(channel_id)
        user_name = interaction.user.display_name or interaction.user.name
        context_messages.append({"role": "user", "content": clean_prompt or "請分析圖片"})

        # 7. 呼叫 Multi-Provider Router 執行生成
        preferred_p = provider if (provider and provider != "auto") else None
        configured_model = str(cfg.get("model") or "auto")
        preferred_m = None
        if not preferred_p and configured_model != "auto":
            if ":" in configured_model:
                parts = configured_model.split(":", 1)
                preferred_p = parts[0]
                preferred_m = parts[1]
            else:
                preferred_m = configured_model
        elif preferred_p and configured_model != "auto":
            if ":" in configured_model:
                parts = configured_model.split(":", 1)
                if parts[0] == preferred_p:
                    preferred_m = parts[1]

        response_text, error_msg, stats = await router.generate_chat_completion(
            context_messages,
            system_instruction=instruction,
            image_url=img_url,
            preferred_provider_id=preferred_p,
            preferred_model=preferred_m,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        if error_msg or not response_text:
            final_err = "請求逾時，請稍後再試" if (not error_msg or "逾時" in error_msg or "Timeout" in error_msg or "timeout" in error_msg) else error_msg
            await interaction.followup.send(final_err)
            return

        # 8. 寫入頻道記憶
        memory_manager.add_user_message(channel_id, user_name, clean_prompt or "[圖片提問]")
        memory_manager.add_assistant_message(channel_id, response_text)

        # 9. 經濟系統計費扣款
        cost = calculate_ai_token_cost(stats.get("model", "auto"), stats.get("total_tokens", 0))
        ok, new_balance = update_user_ai_points(payer_id, -cost, allow_negative=False)
        if not ok:
            set_user_ai_points(payer_id, 0)
            new_balance = 0

        payer_member = interaction.guild.get_member(payer_id) if interaction.guild else None
        payer_name = payer_member.display_name if payer_member else (interaction.user.display_name or interaction.user.name)

        # 10. 組裝簡約 Embed (如圖二排版)
        chunks = split_message_chunks(response_text, max_chars=3800)
        first_chunk = chunks[0] if chunks else response_text

        embed = self._build_ai_embed(
            response_text=first_chunk,
            prompt=clean_prompt,
            stats=stats,
            cost=cost,
            payer_id=payer_id,
            is_guild_sponsored=is_guild_sponsored,
            new_balance=new_balance,
            payer_name=payer_name,
            image_url=img_url,
        )

        await interaction.followup.send(embed=embed)
        if len(chunks) > 1:
            for extra_chunk in chunks[1:]:
                sub_chunks = split_message_chunks(extra_chunk, max_chars=1900)
                for sc in sub_chunks:
                    await interaction.followup.send(sc)

    # ==========================================================================
    # /reset 與 /ai_reset: 清除頻道對話記憶
    # ==========================================================================

    @app_commands.command(name="ai_reset", description="清除當前文字頻道或討論串的最近 15 則對話上下文記憶")
    async def ai_reset_command(self, interaction: discord.Interaction):
        count = memory_manager.clear_channel(interaction.channel_id)
        embed = discord.Embed(
            title="頻道記憶已清除",
            description=f"已成功清除本頻道最近 **{count}** 則對話記憶！下次發問將作為全新對話起始。",
            color=0x38bdf8,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="reset", description="[快捷] 清除當前頻道的 AI 對話記憶")
    async def reset_shortcut_command(self, interaction: discord.Interaction):
        await self.ai_reset_command(interaction)

    # ==========================================================================
    # /provider 與 /ai_provider: 檢視提供商在線狀態與容災鏈
    # ==========================================================================

    @app_commands.command(name="ai_provider", description="查看多供應商免費 AI 狀態、容錯容災鏈與即時冷卻倒數")
    async def ai_provider_command(self, interaction: discord.Interaction):
        statuses = router.get_providers_status()
        embed = discord.Embed(
            title="多供應商免費 AI 網關狀態監控",
            description=(
                "本系統採用動態多供應商容錯容災架構（Failover Gateway），調用時遇 `429 速率限制` 或服務端錯誤時，"
                "**0 秒自動無縫切換**至下一順位提供商！\n\n"
                "**文字請求鏈**：Google AI Studio ➔ NVIDIA NIM ➔ Mistral AI\n"
                "**圖片視覺鏈**：Google AI Studio ➔ NVIDIA NIM ➔ Mistral AI"
            ),
            color=0x818cf8,
        )

        for s in statuses:
            lines = [
                f"• **狀態**：{s['status_label']}",
                f"• **速率上限**：`{s['rpm_limit']}`",
                f"• **免費額度**：{s['quota']}",
                f"• **預設模型**：`{s['default_text_model']}`",
            ]
            if s['supports_vision']:
                lines.append(f"• **視覺模型**：`{s['default_vision_model']}` (支援圖片分析)")
            if s['last_error']:
                lines.append(f"• **最近事件**：`{s['last_error'][:60]}`")
            embed.add_field(name=s['name'], value="\n".join(lines), inline=False)

        embed.set_footer(text="動態容災智慧輪詢架構 · 炙彗JI器人")
        view = build_panel_link_view(interaction.guild_id, label="前往後台檢視 AI 配置") if interaction.guild_id else None
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="provider", description="[快捷] 查看多供應商免費 AI 狀態與冷卻資訊")
    async def provider_shortcut_command(self, interaction: discord.Interaction):
        await self.ai_provider_command(interaction)

    # ==========================================================================
    # /persona 與 /ai_persona: 設定或清除頻道自訂人設
    # ==========================================================================

    @app_commands.command(name="ai_persona", description="設定或重設當前頻道的專屬 AI 人設指令 (System Instruction)")
    @app_commands.describe(
        instruction="為當前頻道自訂的人設指令 (留空且 clear=True 則還原為伺服器預設)",
        clear="設為 True 則清除頻道專屬人設，恢復使用本伺服器控制面板預設人設",
    )
    async def ai_persona_command(
        self,
        interaction: discord.Interaction,
        instruction: Optional[str] = None,
        clear: Optional[bool] = False,
    ):
        channel_id = interaction.channel_id
        if clear:
            memory_manager.reset_channel_persona(channel_id)
            await interaction.response.send_message("已清除本頻道的專屬人設，恢復套用伺服器預設人設！")
            return

        if instruction and instruction.strip():
            memory_manager.set_channel_persona(channel_id, instruction.strip())
            embed = discord.Embed(
                title="頻道專屬人設已更新",
                description=f"本頻道後續對話將使用以下自訂人設：\n```\n{instruction.strip()}\n```",
                color=0xa855f7,
            )
            embed.set_footer(text="提示：可使用 /ai_persona clear:True 隨時還原")
            await interaction.response.send_message(embed=embed)
        else:
            cur_persona = memory_manager.get_channel_persona(channel_id, fallback_default="（未設定，套用伺服器預設）")
            await interaction.response.send_message(f"**當前頻道自訂人設**：\n```{cur_persona}```")

    @app_commands.command(name="persona", description="[快捷] 查看或設定當前頻道 AI 人設")
    @app_commands.describe(instruction="為當前頻道自訂的人設指令", clear="設為 True 則清除頻道專屬人設")
    async def persona_shortcut_command(self, interaction: discord.Interaction, instruction: Optional[str] = None, clear: Optional[bool] = False):
        await self.ai_persona_command(interaction, instruction, clear)

    # ==========================================================================
    # /ai_config 斜線指令：快速檢視當前伺服器配置
    # ==========================================================================

    @app_commands.command(name="ai_config", description="查看當前伺服器的 AI 智慧助手與多供應商配置狀態")
    async def ai_config_command(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        cfg = self._get_guild_cfg(guild_id)

        status_text = "啟用中" if cfg.get("is_enabled", True) else "已停用"
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
            payer_text = f"<@{payer_id}> (餘額: `{pts:,}` AI幣)"
        else:
            payer_text = "未指定（由每位提問者自行支付）"

        is_auth = is_guild_ai_authorized(guild_id)
        auth_text = "已開通封測授權" if is_auth else "未授權 (內部封測中)"

        embed = discord.Embed(
            title="伺服器 AI 智慧助手配置狀態",
            description="管理員可隨時透過 `/ai_settings` 指令或點擊下方按鈕前往「網頁控制面板」即時自訂 AI 人設、預設模式與指定支付者！",
            color=0xa78bfa,
        )
        embed.add_field(name="封測授權狀態", value=auth_text, inline=True)
        embed.add_field(name="功能狀態", value=status_text, inline=True)
        embed.add_field(name="預設引擎", value=f"`{model_name}`", inline=True)
        embed.add_field(name="限定頻道", value=channel_text, inline=True)
        embed.add_field(name="指定支付者", value=payer_text, inline=True)
        embed.add_field(name="創意度 (Temperature)", value=f"`{temp}`", inline=True)
        embed.add_field(name="最大長度 (Max Tokens)", value=f"`{tokens}`", inline=True)
        embed.add_field(name="目前人設指令 (Instruction)", value=f"```\n{inst_preview}\n```", inline=False)
        embed.set_footer(text="Multi-Provider Failover Gateway · 炙彗JI器人")

        view = build_panel_link_view(guild_id, label="前往控制面板自訂 AI 人設") if guild_id else None
        await interaction.response.send_message(embed=embed, view=view)

    # ==========================================================================
    # /ai_settings 斜線指令：伺服器管理員自訂
    # ==========================================================================

    @app_commands.command(
        name="ai_settings",
        description="[管理員] 設定本伺服器的預設 AI 提供商/模型、人設指令與指定 AI 貨幣支付者",
    )
    @app_commands.describe(
        model="選擇本伺服器預設使用的 AI 提供商與模型",
        instruction="自訂本伺服器的 AI 人設指令 (System Instruction)",
        payer="指定承擔本伺服器所有 AI 對話費用的成員 (選填，不填則保持現狀)",
        clear_payer="設為 True 則解除指定支付者（恢復由每位提問者自行支付）",
        channel="限定 AI 對話的專屬文字頻道 (留空則不限制頻道)",
        clear_channel="設為 True 則解除頻道限制（所有文字頻道皆可使用）",
        temperature="設定創意度 (0.0 ~ 1.0，預設 0.7)",
        max_tokens="設定最大回覆 Token 限制 (100 ~ 4000，預設 3000)",
        enabled="是否啟用本伺服器的 AI 智慧問答功能",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(
        model=[
            app_commands.Choice(name="auto (text, vision)", value="auto"),
            app_commands.Choice(name="gemini-flash-latest (text, vision)", value="gemini:gemini-flash-latest"),
            app_commands.Choice(name="meta/llama-3.2-11b-vision-instruct (text, vision)", value="nvidia:meta/llama-3.2-11b-vision-instruct"),
            app_commands.Choice(name="ministral-8b-latest (text, vision)", value="mistral:ministral-8b-latest"),
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

        guild_id = interaction.guild_id

        # 封測階段伺服器授權檢查
        if not is_guild_ai_authorized(guild_id):
            panel_view = build_panel_link_view(guild_id) if guild_id else None
            embed_unauth = discord.Embed(
                title="AI 智慧助手內部封測中",
                description=(
                    "**本伺服器尚未獲得 AI 智慧助手封測授權，無法修改相關設定。**\n\n"
                    "請聯繫機器人擁有者/管理員在後台控制面板發放本伺服器的 AI 封測授權後再進行設定！"
                ),
                color=0xf59e0b,
            )
            await interaction.response.send_message(embed=embed_unauth, view=panel_view, ephemeral=True)
            return

        if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("你需要具備「管理伺服器」權限才能使用此設定指令！", ephemeral=True)
            return

        cfg = dict(self._get_guild_cfg(guild_id))

        updates = []
        if model is not None:
            cfg["model"] = model.strip()
            updates.append(f"• **預設引擎**：`{cfg['model']}`")

        if instruction is not None:
            cfg["instruction"] = instruction.strip()
            updates.append(f"• **人設指令**：已更新（長度 {len(cfg['instruction'])} 字元）")

        if clear_payer:
            cfg["payer_user_id"] = None
            updates.append("• **指定支付者**：已清除（恢復由每位提問者自行支付）")
        elif payer is not None:
            cfg["payer_user_id"] = payer.id
            pts = get_user_ai_points(payer.id)
            updates.append(f"• **指定支付者**：{payer.mention}（目前餘額 `{pts:,}` AI幣）")

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
            cfg["max_tokens"] = max(100, min(4000, int(max_tokens)))
            updates.append(f"• **最大長度**：`{cfg['max_tokens']}` Tokens")

        if enabled is not None:
            cfg["is_enabled"] = bool(enabled)
            updates.append(f"• **AI 功能**：{'已啟用' if cfg['is_enabled'] else '已停用'}")

        if not updates:
            await interaction.response.send_message(
                "未提供任何需要變更的設定參數。可使用 `/ai_config` 檢視當前設定狀態。",
                ephemeral=True,
            )
            return

        save_guild_ai_settings(str(guild_id), cfg)
        self.reload_settings()

        embed = discord.Embed(
            title="伺服器 AI 設定已成功更新！",
            description="\n".join(updates),
            color=discord.Color.green(),
        )
        embed.set_footer(text="設定已即時同步至資料庫與網頁控制面板")
        view = build_panel_link_view(guild_id, label="前往控制面板查看")
        await interaction.response.send_message(embed=embed, view=view)

    # ==========================================================================
    # @機器人 提及 / 直接回覆 / 專屬頻道對話事件監聽器
    # ==========================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """當成員在文字頻道標記 @機器人、直接回覆機器人訊息或在專屬 AI 頻道發言時觸發自然對話。"""
        # 忽略機器人自身訊息或來自其他 Bot 的訊息
        if message.author.bot:
            return

        bot_user = self.bot.user
        if not bot_user:
            return

        guild = message.guild
        guild_id = guild.id if guild else None

        # 封測階段授權檢查 (未獲授權之伺服器不觸發)
        if guild_id and not is_guild_ai_authorized(guild_id):
            return

        cfg = self._get_guild_cfg(guild_id)
        if not cfg.get("is_enabled", True):
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = bot_user in message.mentions
        is_reply_to_bot = False

        if message.reference and message.reference.message_id:
            resolved = message.reference.resolved
            if isinstance(resolved, discord.Message) and resolved.author.id == bot_user.id:
                is_reply_to_bot = True

        dedicated_channel_id = cfg.get("channel_id")
        is_in_dedicated_channel = dedicated_channel_id and (message.channel.id == dedicated_channel_id)

        # 僅在提及機器人、回覆機器人、DM 或在專屬 AI 頻道時觸發
        should_respond = is_mentioned or is_reply_to_bot or is_dm or is_in_dedicated_channel
        if not should_respond:
            return

        # 濾除 @機器人 標註後的實際文字
        clean_content = message.content
        if is_mentioned:
            clean_content = re.sub(rf'<@!?{bot_user.id}>', '', clean_content).strip()

        # 檢查是否有圖片附件
        img_url = None
        for att in message.attachments:
            ct = att.content_type or ""
            fn = att.filename.lower()
            if ct.startswith("image/") or any(fn.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]):
                img_url = att.url
                break

        # 若僅標註卻無文字亦無圖片，發送親切引導問候
        if not clean_content and not img_url:
            greeting = (
                f"嗨 {message.author.mention}！有什麼我可以幫忙的嗎？\n"
                "你可以直接在訊息中提及我並輸入問題、上傳圖片，或使用 `/ai` 指令開始智慧對話！\n"
                "提示：使用 `/ai_provider` 可即時檢視 Gemini、NVIDIA、Mistral 等免費 AI 引擎的在線狀態。"
            )
            try:
                await message.reply(greeting, mention_author=False)
            except Exception:
                pass
            return

        # 檢查 AI 貨幣餘額
        payer_id = int(cfg.get("payer_user_id") or message.author.id)
        is_guild_sponsored = bool(cfg.get("payer_user_id"))
        current_points = get_user_ai_points(payer_id)

        if current_points <= 0:
            payer_mention = f"伺服器指定贊助者 (<@{payer_id}>)" if is_guild_sponsored else f"{message.author.mention}"
            panel_view = build_panel_link_view(guild_id) if guild_id else None
            embed_no_balance = discord.Embed(
                title="AI 專屬貨幣餘額不足",
                description=(
                    f"**{payer_mention} 目前的 AI 專屬貨幣餘額不足（目前餘額：`0` 點）。**\n\n"
                    f"**如何獲取 AI 貨幣？**\n"
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

        channel_id = message.channel.id
        instruction = memory_manager.get_channel_persona(
            channel_id,
            fallback_default=cfg.get("instruction") or DEFAULT_AI_INSTRUCTION,
        )
        temperature = float(cfg.get("temperature", DEFAULT_AI_TEMPERATURE))
        max_tokens = int(cfg.get("max_tokens", DEFAULT_AI_MAX_TOKENS))

        # 構建上下文列表
        context_messages = memory_manager.get_context_messages(channel_id)
        user_name = message.author.display_name or message.author.name
        context_messages.append({"role": "user", "content": clean_content or "請分析這張圖片"})

        # 解析伺服器預設模型與提供商
        configured_model = str(cfg.get("model") or "auto")
        configured_p = None
        configured_m = None
        if configured_model != "auto":
            if ":" in configured_model:
                parts = configured_model.split(":", 1)
                configured_p = parts[0]
                configured_m = parts[1]
            else:
                configured_m = configured_model

        # 打字動態與調用 Router
        try:
            async with message.channel.typing():
                response_text, error_msg, stats = await router.generate_chat_completion(
                    context_messages,
                    system_instruction=instruction,
                    image_url=img_url,
                    preferred_provider_id=configured_p,
                    preferred_model=configured_m,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

                if error_msg or not response_text:
                    final_err = "請求逾時，請稍後再試" if (not error_msg or "逾時" in error_msg or "Timeout" in error_msg or "timeout" in error_msg) else error_msg
                    await message.reply(final_err, mention_author=False)
                    return

                # 記錄對話歷史
                memory_manager.add_user_message(channel_id, user_name, clean_content or "[圖片提問]")
                memory_manager.add_assistant_message(channel_id, response_text)

                # 扣款計費
                cost = calculate_ai_token_cost(stats.get("model", "auto"), stats.get("total_tokens", 0))
                ok, new_balance = update_user_ai_points(payer_id, -cost, allow_negative=False)
                if not ok:
                    set_user_ai_points(payer_id, 0)
                    new_balance = 0

                payer_member = message.guild.get_member(payer_id) if message.guild else None
                payer_name = payer_member.display_name if payer_member else (message.author.display_name or message.author.name)

                chunks = split_message_chunks(response_text, max_chars=3800)
                first_chunk = chunks[0] if chunks else response_text

                embed = self._build_ai_embed(
                    response_text=first_chunk,
                    prompt=clean_content,
                    stats=stats,
                    cost=cost,
                    payer_id=payer_id,
                    is_guild_sponsored=is_guild_sponsored,
                    new_balance=new_balance,
                    payer_name=payer_name,
                    image_url=img_url,
                )

                await message.reply(embed=embed, mention_author=False)
                if len(chunks) > 1:
                    for extra_chunk in chunks[1:]:
                        sub_chunks = split_message_chunks(extra_chunk, max_chars=1900)
                        for sc in sub_chunks:
                            await message.channel.send(sc)

        except Exception as exc:
            traceback.print_exc()
            print(f"⚠️ @機器人 AI 對話處理例外: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))
