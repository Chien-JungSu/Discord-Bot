from __future__ import annotations

import asyncio
import collections
import datetime
import importlib
import re
import sys
import time
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import cogs.database as _db_mod

try:
    _db_mod = importlib.reload(_db_mod)
except Exception:
    pass

from cogs.database import (
    add_temp_message,
    load_temp_messages,
    remove_temp_message,
)

# 時效選項定義：(顯示名稱, 秒數, 中文說明)
DURATION_SPECS: list[tuple[str, int, str]] = [
    ("1min", 60, "1 分鐘後自動刪除"),
    ("30min", 1800, "30 分鐘後自動刪除"),
    ("1hr", 3600, "1 小時後自動刪除"),
    ("6hr", 21600, "6 小時後自動刪除"),
    ("12hr", 43200, "12 小時後自動刪除"),
    ("24hr", 86400, "24 小時後自動刪除"),
]

DURATION_LABELS: dict[int, str] = {sec: label for label, sec, _ in DURATION_SPECS}
DURATION_CHOICES: list[app_commands.Choice[int]] = [
    app_commands.Choice(name=label, value=sec) for label, sec, _ in DURATION_SPECS
]

DEFAULT_FILESIZE_LIMIT = 10 * 1024 * 1024  # 未加成伺服器 / 私訊預設 10 MiB
SUPPORTED_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
SELECT_IDLE_TIMEOUT = 120.0  # 右鍵選單下拉選單閒置逾時秒數
TEMP_MESSAGE_FOOTER_RE = re.compile(r"(?:^|\n)-#\s*由\s+.+\s+傳送\s*•\s*<t:\d+:R>\s*自動刪除\s*$")


def _format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    return f"{num_bytes / 1024:.1f} KB"


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    filename = (attachment.filename or "").lower()
    return content_type.startswith("image/") or filename.endswith(SUPPORTED_IMAGE_EXTENSIONS)


def _get_display_username(user: discord.abc.User) -> str:
    return getattr(user, "display_name", None) or user.name


def _build_temp_message_content(
    text: Optional[str],
    username: str,
    expire_at: float,
    *,
    truncate_if_needed: bool = False,
) -> tuple[str, Optional[str]]:
    """組合限時訊息內文與底部 footer `-# 由 {username} 傳送 • <t:...:R> 自動刪除`。

    回傳 (組合後內容, 錯誤訊息)；若無錯誤則第二項為 None。
    """
    expire_dt = datetime.datetime.fromtimestamp(expire_at, tz=datetime.timezone.utc)
    relative_ts = discord.utils.format_dt(expire_dt, style="R")
    footer = f"-# 由 {username} 傳送 • {relative_ts} 自動刪除"
    clean_text = text.strip() if text else ""
    if not clean_text:
        return footer, None

    max_text_len = 2000 - len(footer) - 1
    if len(clean_text) > max_text_len:
        if truncate_if_needed:
            clean_text = clean_text[: max(0, max_text_len - 1)] + "…"
        else:
            return (
                "",
                f"❌ 文字內容過長（目前 {len(clean_text)} 字，加上底部標註後上限為 {max_text_len} 字），請縮短後再試！",
            )

    return f"{clean_text}\n{footer}", None


def _build_allowed_mentions(
    channel: Optional[discord.abc.Messageable],
    user: discord.abc.User,
) -> discord.AllowedMentions:
    """根據使用者在該頻道的權限決定是否允許 @everyone / @here 與身分組提及，防止越權洗頻。"""
    can_mention_everyone = False
    if isinstance(channel, discord.abc.GuildChannel) and isinstance(user, discord.Member):
        perms = channel.permissions_for(user)
        can_mention_everyone = perms.mention_everyone
    return discord.AllowedMentions(
        everyone=can_mention_everyone,
        roles=can_mention_everyone,
        users=True,
    )


class TempMessageDurationSelect(discord.ui.Select):
    """右鍵訊息選單「限時訊息」的時效下拉選單。"""

    def __init__(self, parent_view: "TempMessageDurationView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=label,
                value=str(sec),
                description=f"自使用指令當下起算，{desc}",
                emoji="⏳",
            )
            for label, sec, desc in DURATION_SPECS
        ]
        super().__init__(
            placeholder="請選擇限時訊息的有效時間…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.handle_duration_selected(interaction, int(self.values[0]))


class TempMessageDurationView(discord.ui.View):
    """右鍵訊息選單觸發後的私密互動介面。"""

    def __init__(
        self,
        cog: "TempMessage",
        target_message: discord.Message,
        initiator: discord.abc.User,
        invoked_at: float,
    ):
        super().__init__(timeout=SELECT_IDLE_TIMEOUT)
        self.cog = cog
        self.target_message = target_message
        self.initiator = initiator
        self.invoked_at = invoked_at
        self.select = TempMessageDurationSelect(self)
        self.add_item(self.select)
        self._handled = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.initiator.id:
            await interaction.response.send_message("❌ 只有執行此指令的使用者才能選擇時效。", ephemeral=True)
            return False
        return True

    def _disable_all(self):
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

    async def handle_duration_selected(self, interaction: discord.Interaction, duration_seconds: int):
        if self._handled:
            await interaction.response.send_message("ℹ️ 此限時訊息已經處理完成。", ephemeral=True)
            return
        self._handled = True
        self._disable_all()
        self.stop()

        await interaction.response.defer(ephemeral=True)

        duration_label = DURATION_LABELS.get(duration_seconds, f"{duration_seconds}s")
        # 有效時間從使用指令的當下開始計算
        expire_at = self.invoked_at + duration_seconds
        channel = self.target_message.channel
        guild = self.target_message.guild

        # 檢查機器人權限
        if isinstance(channel, discord.abc.GuildChannel) and guild and guild.me:
            perms = channel.permissions_for(guild.me)
            missing = []
            if not perms.view_channel:
                missing.append("檢視頻道")
            if not perms.send_messages:
                missing.append("發送訊息")
            if not perms.manage_messages:
                missing.append("管理訊息（刪除原訊息需要）")
            if self.target_message.attachments and not perms.attach_files:
                missing.append("附加檔案")
            if missing:
                await interaction.edit_original_response(
                    content=f"❌ 機器人在該頻道缺少以下權限：**{'、'.join(missing)}**，無法轉換為限時訊息。",
                    embed=None,
                    view=None,
                )
                return

        # 檢查附件總大小是否超過頻道上傳上限
        size_limit = guild.filesize_limit if guild else DEFAULT_FILESIZE_LIMIT
        total_size = sum(att.size for att in self.target_message.attachments)
        if total_size > size_limit:
            await interaction.edit_original_response(
                content=(
                    f"❌ 原訊息的附件總大小（{_format_size(total_size)}）超過目前頻道的上傳上限"
                    f"（{_format_size(size_limit)}），機器人無法代為重發。"
                ),
                embed=None,
                view=None,
            )
            return

        # 先將原訊息的附件下載成 discord.File（必須在刪除原訊息前讀取，避免刪除後 CDN 失效）
        files: list[discord.File] = []
        try:
            for att in self.target_message.attachments:
                files.append(await att.to_file(spoiler=att.is_spoiler()))
        except discord.HTTPException:
            await interaction.edit_original_response(
                content="❌ 無法讀取原訊息的附件檔案（原訊息可能已被刪除），操作已取消。",
                embed=None,
                view=None,
            )
            return

        # 刪除原訊息
        try:
            await self.target_message.delete()
        except discord.NotFound:
            await interaction.edit_original_response(
                content="❌ 找不到原訊息（可能已被刪除），操作已取消。",
                embed=None,
                view=None,
            )
            return
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="❌ 機器人缺少「管理訊息」權限，無法刪除原訊息。",
                embed=None,
                view=None,
            )
            return
        except discord.HTTPException as exc:
            await interaction.edit_original_response(
                content=f"❌ 刪除原訊息時發生錯誤：{exc}",
                embed=None,
                view=None,
            )
            return

        # 若選擇時距離當初觸發指令的時間已經超過所選時效，直接完成刪除不再重發
        if expire_at <= time.time():
            await interaction.edit_original_response(
                content=(
                    f"⏱️ 已刪除原訊息。由於有效時間從使用指令當下開始計算，"
                    f"所選的 **{duration_label}** 時效已到期，因此不再重發訊息。"
                ),
                embed=None,
                view=None,
            )
            return

        username = _get_display_username(interaction.user)
        full_content, _ = _build_temp_message_content(
            self.target_message.content,
            username,
            expire_at,
            truncate_if_needed=True,
        )
        allowed_mentions = _build_allowed_mentions(channel, interaction.user)

        try:
            if files:
                sent_msg = await channel.send(
                    content=full_content,
                    files=files,
                    allowed_mentions=allowed_mentions,
                )
            else:
                sent_msg = await channel.send(
                    content=full_content,
                    allowed_mentions=allowed_mentions,
                )
        except discord.HTTPException as exc:
            await interaction.edit_original_response(
                content=f"❌ 機器人代為傳送限時訊息時失敗：{exc}",
                embed=None,
                view=None,
            )
            return

        self.cog.schedule_message_deletion(
            message_id=sent_msg.id,
            channel_id=sent_msg.channel.id,
            guild_id=guild.id if guild else None,
            author_id=interaction.user.id,
            expire_at=expire_at,
            created_at=self.invoked_at,
        )

        expire_dt = datetime.datetime.fromtimestamp(expire_at, tz=datetime.timezone.utc)
        await interaction.edit_original_response(
            content=(
                f"✅ 已將訊息轉換為限時訊息（時效：**{duration_label}**），"
                f"將於 {discord.utils.format_dt(expire_dt, style='R')}（{discord.utils.format_dt(expire_dt, style='T')}）自動刪除。"
            ),
            embed=None,
            view=None,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        print(f"❌ [temp_message] View 互動發生錯誤: {error}", file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        if hasattr(self.cog.bot, "notify_owner_error"):
            await self.cog.bot.notify_owner_error(error, interaction, extra_info="TempMessageDurationView.on_error")
        msg = "❌ 處理限時訊息時發生未知錯誤，已回報開發者。"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class TempMessage(commands.Cog):
    """限時訊息模組：支援 /temp_message 斜線指令與「限時訊息」右鍵訊息選單。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._tasks: dict[int, asyncio.Task] = {}
        self._known_temp_message_ids: collections.deque[int] = collections.deque(maxlen=2000)

        # 註冊右鍵訊息選單：限時訊息
        self.ctx_menu = app_commands.ContextMenu(
            name="限時訊息",
            callback=self.temp_message_context,
        )
        self.bot.tree.add_command(self.ctx_menu)

    def is_temp_message(self, msg_or_id: discord.Message | int | None) -> bool:
        """檢查指定訊息物件或訊息 ID 是否為限時訊息。"""
        if msg_or_id is None:
            return False
        if isinstance(msg_or_id, int):
            return msg_or_id in self._tasks or msg_or_id in self._known_temp_message_ids
        msg_id = getattr(msg_or_id, "id", None)
        if msg_id and (int(msg_id) in self._tasks or int(msg_id) in self._known_temp_message_ids):
            return True
        content = getattr(msg_or_id, "content", None) or ""
        return bool(content and TEMP_MESSAGE_FOOTER_RE.search(content))

    async def cog_load(self):
        """模組載入時從資料庫恢復所有尚未到期的限時訊息排程。"""
        asyncio.create_task(self._restore_pending_deletions())

    async def cog_unload(self):
        """卸載模組時移除右鍵選單並取消進行中的背景計時任務（重載後會由資料庫自動恢復）。"""
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()

    async def _restore_pending_deletions(self):
        await self.bot.wait_until_ready()
        try:
            records = load_temp_messages()
        except Exception as exc:
            print(f"⚠️ [temp_message] 讀取未到期之限時訊息排程失敗: {exc}", file=sys.stderr)
            return

        for rec in records:
            msg_id = int(rec["message_id"])
            chan_id = int(rec["channel_id"])
            expire_at = float(rec["expire_at"])
            self._spawn_deletion_task(msg_id, chan_id, expire_at)

    def schedule_message_deletion(
        self,
        message_id: int,
        channel_id: int,
        guild_id: Optional[int | str],
        author_id: int,
        expire_at: float,
        created_at: Optional[float] = None,
    ) -> None:
        """將限時訊息寫入 SQLite 並啟動背景倒數刪除任務。"""
        try:
            add_temp_message(
                message_id=message_id,
                channel_id=channel_id,
                guild_id=guild_id,
                author_id=author_id,
                expire_at=expire_at,
                created_at=created_at,
            )
        except Exception as exc:
            print(f"⚠️ [temp_message] 儲存限時訊息排程失敗 (message_id={message_id}): {exc}", file=sys.stderr)

        self._spawn_deletion_task(message_id, channel_id, expire_at)

    def _spawn_deletion_task(self, message_id: int, channel_id: int, expire_at: float) -> None:
        self._known_temp_message_ids.append(int(message_id))
        existing = self._tasks.pop(message_id, None)
        if existing and not existing.done():
            existing.cancel()

        task = asyncio.create_task(self._delete_when_expired(message_id, channel_id, expire_at))
        self._tasks[message_id] = task

    async def _delete_when_expired(self, message_id: int, channel_id: int, expire_at: float) -> None:
        try:
            delay = max(0.0, expire_at - time.time())
            if delay > 0:
                await asyncio.sleep(delay)

            try:
                await self.bot.http.delete_message(channel_id, message_id)
            except (discord.NotFound, discord.Forbidden):
                # 訊息已被手動刪除，或機器人已無權限存取該頻道
                pass
            except discord.HTTPException as exc:
                print(
                    f"⚠️ [temp_message] 自動刪除限時訊息失敗 (channel={channel_id}, message={message_id}): {exc}",
                    file=sys.stderr,
                )

            try:
                remove_temp_message(message_id)
            except Exception as exc:
                print(f"⚠️ [temp_message] 移除資料庫限時訊息紀錄失敗: {exc}", file=sys.stderr)
        except asyncio.CancelledError:
            raise
        finally:
            if self._tasks.get(message_id) is asyncio.current_task():
                self._tasks.pop(message_id, None)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """若限時訊息在到期前已被手動刪除，立即取消計時器並清理資料庫紀錄。"""
        task = self._tasks.pop(payload.message_id, None)
        if task is not None:
            task.cancel()
            try:
                remove_temp_message(payload.message_id)
            except Exception:
                pass

    # ==========================================================================
    # /temp_message 斜線指令
    # ==========================================================================

    @app_commands.command(
        name="temp_message",
        description="發送限時訊息（可輸入文字或上傳圖片，由機器人代為傳送並在指定時間後自動刪除）",
    )
    @app_commands.describe(
        duration="選擇訊息有效時間（從使用指令當下開始計算）",
        text="要傳送的文字內容（與圖片至少需填寫一項）",
        image="要上傳的圖片檔案（與文字至少需填寫一項）",
    )
    @app_commands.choices(duration=DURATION_CHOICES)
    async def temp_message(
        self,
        interaction: discord.Interaction,
        duration: app_commands.Choice[int],
        text: Optional[str] = None,
        image: Optional[discord.Attachment] = None,
    ):
        # 有效時間從使用指令的當下開始計算
        invoked_at = time.time()
        duration_seconds = int(duration.value)
        duration_label = duration.name
        expire_at = invoked_at + duration_seconds

        channel = interaction.channel
        if channel is None or not hasattr(channel, "send"):
            await interaction.response.send_message("❌ 此處無法發送限時訊息！", ephemeral=True)
            return

        clean_text = text.strip() if text else ""
        if not clean_text and image is None:
            await interaction.response.send_message(
                "❌ 請至少輸入**文字內容**或上傳一張**圖片**！",
                ephemeral=True,
            )
            return

        # 檢查機器人在頻道的發言與附加檔案權限
        guild = interaction.guild
        if isinstance(channel, discord.abc.GuildChannel) and guild and guild.me:
            perms = channel.permissions_for(guild.me)
            missing = []
            if not perms.view_channel:
                missing.append("檢視頻道")
            if not perms.send_messages:
                missing.append("發送訊息")
            if image is not None and not perms.attach_files:
                missing.append("附加檔案")
            if missing:
                await interaction.response.send_message(
                    f"❌ 機器人在本頻道缺少以下權限：**{'、'.join(missing)}**，無法代為傳送限時訊息。",
                    ephemeral=True,
                )
                return

        # 驗證圖片格式與檔案大小
        if image is not None:
            if not _is_image_attachment(image):
                await interaction.response.send_message(
                    "❌ 上傳的檔案不是支援的圖片格式（僅支援 PNG、JPG、WebP、GIF、BMP）！",
                    ephemeral=True,
                )
                return

            size_limit = guild.filesize_limit if guild else DEFAULT_FILESIZE_LIMIT
            if image.size > size_limit:
                await interaction.response.send_message(
                    f"❌ 圖片大小（{_format_size(image.size)}）超過此處的上傳上限（{_format_size(size_limit)}）！",
                    ephemeral=True,
                )
                return

        username = _get_display_username(interaction.user)
        full_content, err_msg = _build_temp_message_content(clean_text, username, expire_at)
        if err_msg:
            await interaction.response.send_message(err_msg, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        discord_file: Optional[discord.File] = None
        if image is not None:
            try:
                discord_file = await image.to_file(spoiler=image.is_spoiler())
            except discord.HTTPException:
                await interaction.followup.send("❌ 讀取上傳的圖片失敗，請稍後再試！", ephemeral=True)
                return

        allowed_mentions = _build_allowed_mentions(channel, interaction.user)

        try:
            if discord_file is not None:
                sent_msg = await channel.send(
                    content=full_content,
                    file=discord_file,
                    allowed_mentions=allowed_mentions,
                )
            else:
                sent_msg = await channel.send(
                    content=full_content,
                    allowed_mentions=allowed_mentions,
                )
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ 傳送限時訊息失敗：{exc}", ephemeral=True)
            return

        self.schedule_message_deletion(
            message_id=sent_msg.id,
            channel_id=sent_msg.channel.id,
            guild_id=guild.id if guild else None,
            author_id=interaction.user.id,
            expire_at=expire_at,
            created_at=invoked_at,
        )

        expire_dt = datetime.datetime.fromtimestamp(expire_at, tz=datetime.timezone.utc)
        await interaction.followup.send(
            f"✅ 限時訊息已傳送（時效：**{duration_label}**），"
            f"將於 {discord.utils.format_dt(expire_dt, style='R')}（{discord.utils.format_dt(expire_dt, style='T')}）自動刪除。",
            ephemeral=True,
        )

    # ==========================================================================
    # 右鍵訊息選單：限時訊息
    # ==========================================================================

    async def temp_message_context(self, interaction: discord.Interaction, message: discord.Message):
        """右鍵訊息選單「限時訊息」：私密訊息提供下拉選單選擇有效時間，時效自使用指令當下起算。"""
        invoked_at = time.time()

        if message.author.id != interaction.user.id:
            await interaction.response.send_message(
                "❌ 你只能對**自己傳送的訊息**使用「限時訊息」功能！",
                ephemeral=True,
            )
            return

        has_text = bool(message.content and message.content.strip())
        has_attachments = bool(message.attachments)
        if not has_text and not has_attachments:
            await interaction.response.send_message(
                "❌ 此訊息不包含任何文字或圖片附件，無法轉換為限時訊息！",
                ephemeral=True,
            )
            return

        channel = message.channel
        guild = message.guild
        if isinstance(channel, discord.abc.GuildChannel) and guild and guild.me:
            perms = channel.permissions_for(guild.me)
            missing = []
            if not perms.view_channel:
                missing.append("檢視頻道")
            if not perms.send_messages:
                missing.append("發送訊息")
            if not perms.manage_messages:
                missing.append("管理訊息（刪除原訊息需要）")
            if has_attachments and not perms.attach_files:
                missing.append("附加檔案")
            if missing:
                await interaction.response.send_message(
                    f"❌ 機器人在本頻道缺少以下權限：**{'、'.join(missing)}**，無法將此訊息轉換為限時訊息。",
                    ephemeral=True,
                )
                return

        invoked_dt = datetime.datetime.fromtimestamp(invoked_at, tz=datetime.timezone.utc)
        view = TempMessageDurationView(
            cog=self,
            target_message=message,
            initiator=interaction.user,
            invoked_at=invoked_at,
        )
        await interaction.response.send_message(
            content=(
                f"⏳ 請從下方選單選擇此訊息的有效時間（有效時間自使用指令當下 "
                f"{discord.utils.format_dt(invoked_dt, style='T')} 開始計算）："
            ),
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(TempMessage(bot))
