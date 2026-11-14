from __future__ import annotations

import importlib
import sys
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import cogs.ganhua_engine as _engine_mod

try:
    _engine_mod = importlib.reload(_engine_mod)
except Exception:
    pass

from cogs.ganhua_engine import LightweightGanhuaEngine, clean_input_text
from cogs.sanitize import redact_secrets

# 保留相容別名供外部或測試直接呼叫
clean_message_content = clean_input_text


def build_ganhua_embed(
    question: str,
    reply_text: str,
    author: Optional[discord.abc.User] = None,
) -> discord.Embed:
    """建立簡潔有質感的台灣幹話回覆 Discord Embed（含提問者問題與幹話回覆）。"""
    safe_question = question if len(question) <= 900 else f"{question[:897]}..."
    safe_reply = reply_text if len(reply_text) <= 1000 else f"{reply_text[:997]}..."

    embed = discord.Embed(
        title="🗣️ 台灣幹話產生器",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="💬 提問／話題",
        value=f"> {safe_question}",
        inline=False,
    )
    embed.add_field(
        name="🔥 幹話吐槽",
        value=safe_reply,
        inline=False,
    )

    if author is not None:
        display_name = getattr(author, "display_name", None) or getattr(author, "name", "使用者")
        avatar = getattr(author, "display_avatar", None)
        icon_url = getattr(avatar, "url", None) if avatar is not None else None
        embed.set_footer(text=f"由 {display_name} 發起吐槽", icon_url=icon_url)
    else:
        embed.set_footer(text="輕量級字元 TF-IDF 台灣幹話引擎")

    return embed


class Ganhua(commands.Cog):
    """輕量級台灣幹話產生器 Cog：支援 /幹話、/ganhua 與「用幹話回覆」右鍵選單。"""

    def __init__(
        self,
        bot: commands.Bot,
        engine: Optional[LightweightGanhuaEngine] = None,
    ) -> None:
        self.bot: commands.Bot = bot
        self.engine: LightweightGanhuaEngine = engine or LightweightGanhuaEngine(data_path="memes_data.json")

        # 註冊訊息右鍵選單指令：用幹話回覆
        self.ctx_menu: app_commands.ContextMenu = app_commands.ContextMenu(
            name="用幹話回覆",
            callback=self.ganhua_context_menu,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        """卸載模組時移除右鍵選單，避免重載時重複註冊。"""
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    async def _report_unexpected_error(
        self,
        exc: Exception,
        interaction: discord.Interaction,
        extra_info: str,
    ) -> None:
        """脫敏印出錯誤堆疊並通知機器人擁有者。"""
        safe_err = redact_secrets(str(exc))
        print(f"❌ [ganhua] {extra_info} 發生錯誤: {safe_err}", file=sys.stderr)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)

        if hasattr(self.bot, "notify_owner_error"):
            try:
                await self.bot.notify_owner_error(exc, interaction, extra_info=extra_info)
            except Exception as notify_exc:
                print(
                    f"⚠️ [ganhua] 回報錯誤給擁有者時失敗: {redact_secrets(str(notify_exc))}",
                    file=sys.stderr,
                )

    async def _handle_ganhua_slash(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """/幹話 與 /ganhua 共用之隨機幹話處理流程（無參數）。"""
        try:
            reply_text = self.engine.random_reply()
        except Exception as exc:
            await self._report_unexpected_error(exc, interaction, extra_info="Ganhua._handle_ganhua_slash")
            await interaction.response.send_message(
                "❌ 幹話檢索引擎發生異常，已回報給開發者，請稍後再試！",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            reply_text,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="幹話",
        description="隨機生成一句道地台灣幹話吐槽",
    )
    async def ganhua_zh_slash(self, interaction: discord.Interaction) -> None:
        """斜線指令 /幹話（無參數，直接隨機生成幹話）。"""
        await self._handle_ganhua_slash(interaction)

    @app_commands.command(
        name="ganhua",
        description="隨機生成一句道地台灣幹話吐槽",
    )
    async def ganhua_slash(self, interaction: discord.Interaction) -> None:
        """斜線指令 /ganhua（無參數，直接隨機生成幹話）。"""
        await self._handle_ganhua_slash(interaction)

    async def ganhua_context_menu(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        """訊息右鍵選單「用幹話回覆」：直接針對目標訊息於頻道回覆幹話，不留私人通知。"""
        raw_content = getattr(message, "content", "") or ""
        cleaned_text = clean_input_text(raw_content)

        # 例外處理：純圖片、貼圖或過濾 Mention 後無實際文字內容
        if not cleaned_text:
            await interaction.response.send_message(
                "ℹ️ 這則訊息沒有文字內容（可能是純圖片、貼圖或僅含標註），沒辦法針對無文字訊息吐槽喔！",
                ephemeral=True,
            )
            return

        try:
            reply_text = self.engine.reply(cleaned_text)
        except Exception as exc:
            await self._report_unexpected_error(exc, interaction, extra_info="Ganhua.ganhua_context_menu")
            await interaction.response.send_message(
                "❌ 幹話檢索引擎發生異常，已回報給開發者，請稍後再試！",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await message.reply(
                reply_text,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
        except (discord.Forbidden, discord.HTTPException):
            # 若頻道缺乏回覆權限或無法直接 reply 原訊息，退回使用 followup.send 發送幹話內容
            await interaction.followup.send(
                reply_text,
                allowed_mentions=discord.AllowedMentions.none(),
            )


async def setup(bot: commands.Bot) -> None:
    """載入 Ganhua Cog 模組。"""
    await bot.add_cog(Ganhua(bot))
