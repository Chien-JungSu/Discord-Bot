"""伺服器管理模組 (`cogs.manage`)。

提供一套 `/manage_*` 前綴的斜線指令，供具備對應權限的伺服器管理者進行：
1. 成員審核處置（踢出、封鎖／解封、禁言／解除禁言、警告與紀錄管理）
2. 訊息與頻道管理（批次刪訊、慢速模式、鎖定／解鎖、建立／編輯／刪除頻道、公告與固定訊息）
3. 身分組、暱稱、語音控制與成員權限檢視

安全原則：
- 所有指令皆啟用 `@app_commands.guild_only()`、`@app_commands.default_permissions(...)` 與執行期檢查雙重防護。
- 嚴格檢查執行者與機器人的伺服器權限、目標頻道覆寫權限，以及成員／身分組階層（禁止操作自己、機器人、伺服器擁有者或高於／等於自身與機器人的對象）。
- 高風險破壞性操作（踢出、封鎖、永久刪除頻道、清空全部警告、大量刪訊）皆強制顯示確認按鈕，且點擊確認時會再度驗證權限與階層。
"""
from __future__ import annotations

import datetime
import math
import re
import sys
import traceback
from typing import Any, Awaitable, Callable, Optional, Sequence, Union

import discord
from discord import app_commands
from discord.ext import commands

import cogs.database as _db


CONFIRM_VIEW_TIMEOUT = 60.0
PAGINATION_VIEW_TIMEOUT = 120.0
MODAL_TIMEOUT = 300.0
MAX_TIMEOUT_SECONDS = 28 * 86400  # Discord API 上限：28 天
MIN_TIMEOUT_SECONDS = 5
PURGE_CONFIRM_THRESHOLD = 25
WARNINGS_PER_PAGE = 8

_DURATION_PART_RE = re.compile(
    r"(\d+)\s*(weeks?|w|週|周|days?|d|天|日|hours?|hrs?|hr|h|小時|時|minutes?|mins?|min|m|分鐘|分|seconds?|secs?|sec|s|秒)",
    re.IGNORECASE,
)
_MSG_URL_RE = re.compile(
    r"^https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)/?$"
)

PERMISSION_LABELS: dict[str, str] = {
    "administrator": "管理員",
    "manage_guild": "管理伺服器",
    "manage_channels": "管理頻道",
    "manage_threads": "管理討論串",
    "manage_roles": "管理身分組",
    "manage_messages": "管理訊息",
    "read_message_history": "讀取訊息歷史",
    "send_messages": "發送訊息",
    "send_messages_in_threads": "在討論串中發送訊息",
    "embed_links": "嵌入連結",
    "view_channel": "檢視頻道",
    "mention_everyone": "提及 @everyone、@here 及所有身分組",
    "manage_nicknames": "管理暱稱",
    "kick_members": "踢出成員",
    "ban_members": "封鎖成員",
    "moderate_members": "禁言成員",
    "move_members": "移動成員",
    "mute_members": "將成員靜音",
    "deafen_members": "讓成員拒聽",
    "connect": "連線",
}

ANNOUNCE_COLORS: dict[str, discord.Color] = {
    "blurple": discord.Color.blurple(),
    "blue": discord.Color.blue(),
    "green": discord.Color.green(),
    "gold": discord.Color.gold(),
    "orange": discord.Color.orange(),
    "red": discord.Color.red(),
}

_DURATION_PRESETS: list[tuple[str, str]] = [
    ("5m", "5 分鐘 (5m)"),
    ("10m", "10 分鐘 (10m)"),
    ("30m", "30 分鐘 (30m)"),
    ("1h", "1 小時 (1h)"),
    ("6h", "6 小時 (6h)"),
    ("1d", "1 天 (1d)"),
    ("7d", "7 天 (7d)"),
    ("28d", "28 天上限 (28d)"),
]

_VOICE_ACTION_PERMS: dict[str, tuple[str, str]] = {
    "disconnect": ("move_members", "語音斷線"),
    "move": ("move_members", "移動語音頻道"),
    "mute": ("mute_members", "伺服器靜音"),
    "unmute": ("mute_members", "解除伺服器靜音"),
    "deafen": ("deafen_members", "伺服器拒聽"),
    "undeafen": ("deafen_members", "解除伺服器拒聽"),
}


# ==============================================================================
# 純函式解析與格式化工具
# ==============================================================================


def parse_duration_seconds(raw: str) -> tuple[Optional[int], Optional[str]]:
    """將持續時間字串（如 `10m`、`1h30m`、`2天`）解析為秒數。"""
    text = (raw or "").strip()
    if not text:
        return None, "❌ 請輸入禁言持續時間（例如 `10m`、`1h`、`1d`）。"

    matches = list(_DURATION_PART_RE.finditer(text))
    if not matches or "".join(m.group(0) for m in matches).replace(" ", "") != text.replace(" ", ""):
        return (
            None,
            "❌ 時間格式不正確。請在數字後方加上單位，例如 `30s`、`10m`、`1h30m`、`1d`、`1w`（支援中文 秒／分／小時／天／週）。",
        )

    unit_seconds = {
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "秒": 1,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "分": 60, "分鐘": 60,
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "時": 3600, "小時": 3600,
        "d": 86400, "day": 86400, "days": 86400, "天": 86400, "日": 86400,
        "w": 604800, "week": 604800, "weeks": 604800, "週": 604800, "周": 604800,
    }

    total = 0
    for match in matches:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        total += amount * unit_seconds[unit]

    if total < MIN_TIMEOUT_SECONDS:
        return None, f"❌ 禁言時間不可少於 {MIN_TIMEOUT_SECONDS} 秒。"
    if total > MAX_TIMEOUT_SECONDS:
        return None, "❌ Discord 禁言時間上限為 **28 天**（`28d`），請縮短時間。"
    return total, None


def format_duration_zh(seconds: int) -> str:
    """將秒數格式化為可讀的繁體中文持續時間。"""
    remaining = max(0, int(seconds))
    if remaining == 0:
        return "0 秒（已關閉）"
    units = ((86400, "天"), (3600, "小時"), (60, "分鐘"), (1, "秒"))
    parts: list[str] = []
    for unit_sec, label in units:
        if remaining >= unit_sec:
            qty, remaining = divmod(remaining, unit_sec)
            parts.append(f"{qty} {label}")
    return " ".join(parts)


def parse_message_reference(raw: str) -> tuple[Optional[int], Optional[int], Optional[int], Optional[str]]:
    """解析訊息 ID 或 Discord 訊息連結，回傳 `(guild_id, channel_id, message_id, error)`。"""
    text = (raw or "").strip()
    if not text:
        return None, None, None, "❌ 請輸入訊息 ID 或訊息連結。"
    if text.isdigit():
        return None, None, int(text), None

    match = _MSG_URL_RE.fullmatch(text)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3)), None

    return (
        None,
        None,
        None,
        "❌ 訊息識別格式不正確，請提供純數字的**訊息 ID**，或完整的 **Discord 訊息連結**。",
    )


def format_audit_reason(caller: Union[discord.Member, discord.User], reason: Optional[str]) -> str:
    """組合符合 Discord 512 字元上限的審核日誌原因字串。"""
    clean = (reason or "").strip() or "未填寫原因"
    prefix = f"操作者：{caller} ({caller.id}) | 原因："
    max_reason_len = max(16, 512 - len(prefix))
    if len(clean) > max_reason_len:
        clean = clean[: max_reason_len - 3] + "..."
    return f"{prefix}{clean}"


def parse_announce_color(color: Optional[str]) -> discord.Color:
    """依顏色代碼回傳對應的 Embed 顏色，預設為 Blurple。"""
    return ANNOUNCE_COLORS.get((color or "blurple").strip().lower(), discord.Color.blurple())


def _channel_type_display(ch: discord.abc.GuildChannel | discord.Thread) -> str:
    """取得頻道類型的繁體中文名稱。"""
    mapping = (
        (discord.TextChannel, "文字頻道"),
        (discord.VoiceChannel, "語音頻道"),
        (discord.StageChannel, "舞台頻道"),
        (discord.ForumChannel, "論壇頻道"),
        (discord.CategoryChannel, "分類頻道"),
        (discord.Thread, "討論串"),
    )
    for cls, label in mapping:
        if isinstance(ch, cls):
            return label
    return type(ch).__name__


# ==============================================================================
# 嚴格權限與階層檢查核心
# ==============================================================================


def _resolve_guild_context(
    interaction: discord.Interaction,
) -> tuple[Optional[discord.Guild], Optional[discord.Member], Optional[discord.Member], Optional[str]]:
    """確認指令在伺服器內執行，並解析執行者與機器人自身的 Member 物件。"""
    guild = interaction.guild
    if guild is None:
        return None, None, None, "❌ 此管理指令只能在伺服器內使用，無法於私訊執行。"

    caller: Optional[discord.Member] = None
    if isinstance(interaction.user, discord.Member):
        caller = interaction.user
    elif interaction.user is not None:
        caller = guild.get_member(interaction.user.id)

    if caller is None:
        return guild, None, None, "❌ 無法驗證你在本伺服器的成員身分與權限。"

    bot_member = guild.me
    client = getattr(interaction, "client", None)
    if bot_member is None and client is not None and getattr(client, "user", None) is not None:
        bot_member = guild.get_member(client.user.id)

    if bot_member is None:
        return guild, caller, None, "❌ 無法取得機器人在本伺服器的成員資料與權限狀態。"

    return guild, caller, bot_member, None


def _check_missing_permissions(perms: Any, required: Sequence[str]) -> list[str]:
    """檢查權限物件缺少哪些指定權限，回傳繁體中文權限名稱清單。"""
    if getattr(perms, "administrator", False):
        return []
    return [PERMISSION_LABELS.get(p, p) for p in required if not getattr(perms, p, False)]


def _check_actor_and_bot_guild_perms(
    caller: discord.Member,
    bot_member: discord.Member,
    required_caller_perms: Sequence[str],
    required_bot_perms: Sequence[str] = (),
) -> Optional[str]:
    """嚴格檢查執行者與機器人是否具備指定的伺服器層級權限。"""
    caller_missing = _check_missing_permissions(caller.guild_permissions, required_caller_perms)
    if caller_missing:
        return f"❌ 你沒有使用此指令所需的伺服器權限（**{'、'.join(caller_missing)}**）。"

    bot_missing = _check_missing_permissions(bot_member.guild_permissions, required_bot_perms)
    if bot_missing:
        return (
            f"❌ 機器人缺少必要伺服器權限（**{'、'.join(bot_missing)}**），"
            "請伺服器管理員調整機器人的身分組權限後再試。"
        )
    return None


def _check_actor_and_bot_channel_perms(
    guild: discord.Guild,
    channel: Any,
    caller: discord.Member,
    bot_member: discord.Member,
    required_caller_perms: Sequence[str],
    required_bot_perms: Sequence[str] = (),
) -> Optional[str]:
    """嚴格檢查執行者與機器人在目標頻道上的覆寫權限（防止跨頻道繞過權限）。"""
    ch_guild = getattr(channel, "guild", None)
    if ch_guild is None or ch_guild.id != guild.id:
        return "❌ 目標頻道不屬於此伺服器。"

    caller_perms = channel.permissions_for(caller)
    caller_reqs = ("view_channel", *required_caller_perms)
    caller_missing = _check_missing_permissions(caller_perms, caller_reqs)
    if caller_missing:
        mention = getattr(channel, "mention", f"#{getattr(channel, 'name', '未知頻道')}")
        return f"❌ 你在 {mention} 缺少必要頻道權限（**{'、'.join(caller_missing)}**）。"

    if required_bot_perms:
        bot_perms = channel.permissions_for(bot_member)
        bot_reqs = ("view_channel", *required_bot_perms)
        bot_missing = _check_missing_permissions(bot_perms, bot_reqs)
        if bot_missing:
            mention = getattr(channel, "mention", f"#{getattr(channel, 'name', '未知頻道')}")
            return f"❌ 機器人在 {mention} 缺少必要頻道權限（**{'、'.join(bot_missing)}**）。"

    return None


def _check_member_target_hierarchy(
    guild: discord.Guild,
    caller: discord.Member,
    bot_member: discord.Member,
    target: discord.Member,
    *,
    allow_self: bool = False,
    check_bot_hierarchy: bool = True,
    forbid_admin_target: bool = False,
) -> Optional[str]:
    """嚴格驗證目標成員的身分組階層、自我／擁有者／機器人保護與管理員限制。"""
    target_guild = getattr(target, "guild", None)
    if target_guild is None or target_guild.id != guild.id:
        return "❌ 目標成員不屬於此伺服器。"

    if not allow_self and target.id == caller.id:
        return "❌ 你無法對自己執行此管理操作。"

    if target.id == bot_member.id:
        return "❌ 你無法對機器人本身執行此管理操作。"

    if target.id == guild.owner_id and (not allow_self or caller.id != guild.owner_id):
        return "❌ 無法對伺服器擁有者執行此操作。"

    if target.id != caller.id and caller.id != guild.owner_id:
        if not (caller.top_role > target.top_role):
            return "❌ 目標成員的最高身分組高於或等於你，你無權對其執行此操作。"

    if check_bot_hierarchy and bot_member.id != guild.owner_id and target.id != guild.owner_id:
        if not (bot_member.top_role > target.top_role):
            return (
                f"❌ 目標成員的最高身分組高於或等於機器人（**{bot_member.top_role.name}**），"
                "請到伺服器設定將機器人的身分組往上調整。"
            )

    if forbid_admin_target and getattr(target.guild_permissions, "administrator", False):
        return "❌ 目標成員具備「管理員 (Administrator)」權限，依據 Discord 規範無法對其進行禁言。"

    return None


def _check_role_hierarchy(
    guild: discord.Guild,
    caller: discord.Member,
    bot_member: discord.Member,
    role: discord.Role,
) -> Optional[str]:
    """嚴格檢查目標身分組是否可被執行者與機器人安全指派或移除。"""
    role_guild = getattr(role, "guild", None)
    if role_guild is None or role_guild.id != guild.id:
        return "❌ 所選身分組不屬於此伺服器。"

    if role.is_default():
        return "❌ 無法操作 `@everyone` 預設身分組。"

    if role.managed:
        return "❌ 此身分組由系統或整合服務（如其他機器人、加成身分組等）自動管理，無法手動指派或移除。"

    if caller.id != guild.owner_id and not (caller.top_role > role):
        return "❌ 所選身分組高於或等於你的最高身分組，你無權操作此身分組。"

    if bot_member.id != guild.owner_id and not (bot_member.top_role > role):
        return (
            f"❌ 所選身分組高於或等於機器人的最高身分組（**{bot_member.top_role.name}**），"
            "請將機器人身分組調高後再試。"
        )

    return None


async def _has_any_voice_mod_permission(interaction: discord.Interaction) -> bool:
    """`/manage_voice` 頂層 check：必須至少具備一項語音管理權限。"""
    guild, caller, _, _ = _resolve_guild_context(interaction)
    if guild is None or caller is None:
        return False
    perms = caller.guild_permissions
    if getattr(perms, "administrator", False):
        return True
    if any(getattr(perms, p, False) for p in ("move_members", "mute_members", "deafen_members")):
        return True
    raise app_commands.MissingPermissions(["move_members / mute_members / deafen_members"])


async def _reply_ephemeral(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    *,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
) -> Optional[discord.Message]:
    """安全發送 ephemeral 回應（自動相容已 defer 與未回應狀態）。"""
    kwargs: dict[str, Any] = {"ephemeral": True}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view

    if interaction.response.is_done():
        return await interaction.followup.send(**kwargs)
    await interaction.response.send_message(**kwargs)
    try:
        return await interaction.original_response()
    except discord.HTTPException:
        return None


def _describe_action_error(exc: Exception) -> str:
    """將管理操作發生的例外轉為清楚的繁體中文說明文字。"""
    if isinstance(exc, discord.Forbidden):
        return "❌ 機器人權限或身分組階層不足，遭到 Discord 拒絕執行（Forbidden）。請確認機器人權限與身分組排序。"
    if isinstance(exc, discord.NotFound):
        return "❌ 找不到目標對象或資源（可能已被刪除或離開）。"
    if isinstance(exc, discord.HTTPException):
        return f"❌ Discord 伺服器拒絕了此請求（HTTP {exc.status}）：{exc.text}"
    return "❌ 發生未預期的系統錯誤，已自動回報給開發者。"


async def _report_unexpected_error(
    bot: Optional[commands.Bot],
    interaction: discord.Interaction,
    exc: Exception,
    context_label: str,
) -> None:
    """當發生非預期的系統例外時印出堆疊並通知開發者。"""
    if isinstance(exc, (discord.Forbidden, discord.NotFound, discord.HTTPException)):
        return
    print(f"❌ [cogs.manage] {context_label} 發生未預期錯誤: {exc}", file=sys.stderr)
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    if bot is not None and hasattr(bot, "notify_owner_error"):
        await bot.notify_owner_error(exc, interaction, extra_info=f"Manage.{context_label}")


async def _handle_discord_action_error(
    bot: Optional[commands.Bot],
    interaction: discord.Interaction,
    exc: Exception,
    context_label: str,
) -> None:
    """統一處理管理操作期間的 Discord API 錯誤與未預期例外並回覆使用者。"""
    await _report_unexpected_error(bot, interaction, exc, context_label)
    await _reply_ephemeral(interaction, _describe_action_error(exc))


async def _build_action_error_embed(
    bot: Optional[commands.Bot],
    interaction: discord.Interaction,
    exc: Exception,
    context_label: str,
    title: str = "❌ 操作失敗",
) -> discord.Embed:
    """統一處理例外並建構單一錯誤 Embed（避免確認面板重複發送兩次錯誤訊息）。"""
    await _report_unexpected_error(bot, interaction, exc, context_label)
    return discord.Embed(title=title, description=_describe_action_error(exc), color=discord.Color.red())


# ==============================================================================
# 互動式 UI 元件（二次確認、分頁、公告彈窗）
# ==============================================================================


class ConfirmActionView(discord.ui.View):
    """高風險管理操作的二次確認面板，點擊確認時會再次驗證執行者權限。"""

    def __init__(
        self,
        *,
        initiator_id: int,
        confirm_label: str,
        confirm_style: discord.ButtonStyle,
        revalidate_fn: Callable[[discord.Interaction], Optional[str]],
        execute_fn: Callable[[discord.Interaction], Awaitable[discord.Embed]],
        defer_before_execute: bool = True,
        pre_edit_text: Optional[str] = None,
    ):
        super().__init__(timeout=CONFIRM_VIEW_TIMEOUT)
        self.initiator_id = initiator_id
        self.revalidate_fn = revalidate_fn
        self.execute_fn = execute_fn
        self.defer_before_execute = defer_before_execute
        self.pre_edit_text = pre_edit_text
        self.message: Optional[discord.Message] = None

        confirm_btn = discord.ui.Button(label=confirm_label, style=confirm_style)
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

        cancel_btn = discord.ui.Button(label="取消", style=discord.ButtonStyle.secondary)
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.initiator_id:
            await interaction.response.send_message(
                "❌ 只有原本執行指令的管理員可以操作此確認面板。",
                ephemeral=True,
            )
            return False
        return True

    def _disable_all(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        self._disable_all()
        self.stop()

        err = self.revalidate_fn(interaction)
        if err:
            err_embed = discord.Embed(
                title="❌ 權限或狀態已變更，操作已中止",
                description=err,
                color=discord.Color.red(),
            )
            await interaction.response.edit_message(embed=err_embed, view=None)
            return

        if self.pre_edit_text:
            await interaction.response.edit_message(content=self.pre_edit_text, embed=None, view=None)
            result_embed = await self.execute_fn(interaction)
            try:
                await interaction.edit_original_response(content=None, embed=result_embed, view=None)
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        if self.defer_before_execute:
            await interaction.response.defer()
            result_embed = await self.execute_fn(interaction)
            await interaction.edit_original_response(embed=result_embed, view=None)
        else:
            result_embed = await self.execute_fn(interaction)
            await interaction.response.edit_message(embed=result_embed, view=None)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self._disable_all()
        self.stop()
        cancel_embed = discord.Embed(
            title="ℹ️ 操作已取消",
            description="未對目標執行任何變更。",
            color=discord.Color.light_grey(),
        )
        await interaction.response.edit_message(embed=cancel_embed, view=None)


def build_warnings_page_embed(
    guild: discord.Guild,
    member: discord.Member,
    warnings_list: list[dict[str, Any]],
    page: int,
) -> discord.Embed:
    """建構指定頁碼的成員警告紀錄 Embed。"""
    total = len(warnings_list)
    total_pages = max(1, math.ceil(total / WARNINGS_PER_PAGE))
    safe_page = max(0, min(page, total_pages - 1))
    start = safe_page * WARNINGS_PER_PAGE
    chunk = warnings_list[start : start + WARNINGS_PER_PAGE]

    lines: list[str] = []
    for item in chunk:
        wid = item["id"]
        mod_id = item["moderator_id"]
        ts = int(item["created_at"])
        reason = item["reason"]
        lines.append(
            f"**#{wid}**｜<t:{ts}:f>（<t:{ts}:R>）\n"
            f"執行者：<@{mod_id}> (`{mod_id}`)\n"
            f"原因：{reason}"
        )

    embed = discord.Embed(
        title=f"⚠️ {member.display_name} 的警告紀錄",
        description="\n\n".join(lines),
        color=discord.Color.orange(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(
        text=f"{guild.name} • 第 {safe_page + 1} / {total_pages} 頁 • 共 {total} 筆警告（可使用 /manage_warnings action:刪除單筆警告 移除）"
    )
    return embed


class WarningsPaginationView(discord.ui.View):
    """成員警告紀錄分頁檢視。"""

    def __init__(
        self,
        *,
        initiator_id: int,
        guild: discord.Guild,
        member: discord.Member,
        warnings_list: list[dict[str, Any]],
        page: int = 0,
    ):
        super().__init__(timeout=PAGINATION_VIEW_TIMEOUT)
        self.initiator_id = initiator_id
        self.guild = guild
        self.member = member
        self.warnings_list = warnings_list
        self.page = page
        self.total_pages = max(1, math.ceil(len(warnings_list) / WARNINGS_PER_PAGE))
        self.message: Optional[discord.Message] = None
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.initiator_id:
            await interaction.response.send_message(
                "❌ 只有執行查詢指令的管理員可以翻頁。",
                ephemeral=True,
            )
            return False
        return True

    def _sync_buttons(self) -> None:
        self.clear_items()
        prev_btn = discord.ui.Button(
            label="◀️ 上一頁",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page <= 0),
        )
        prev_btn.callback = self._on_prev
        self.add_item(prev_btn)

        indicator = discord.ui.Button(
            label=f"第 {self.page + 1} / {self.total_pages} 頁",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        self.add_item(indicator)

        next_btn = discord.ui.Button(
            label="下一頁 ▶️",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page >= self.total_pages - 1),
        )
        next_btn.callback = self._on_next
        self.add_item(next_btn)

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        embed = build_warnings_page_embed(self.guild, self.member, self.warnings_list, self.page)
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        self._sync_buttons()
        embed = build_warnings_page_embed(self.guild, self.member, self.warnings_list, self.page)
        await interaction.response.edit_message(embed=embed, view=self)


class AnnounceModal(discord.ui.Modal, title="發佈／編輯伺服器公告"):
    """多行公告輸入視窗（當 /manage_announce 未直接填寫文字時開啟）。"""

    def __init__(
        self,
        cog: "Manage",
        target_channel: Union[discord.TextChannel, discord.Thread],
        color_key: Optional[str],
        mention_role: Optional[discord.Role],
        edit_message_id: Optional[str],
    ):
        super().__init__(timeout=MODAL_TIMEOUT)
        self.cog = cog
        self.target_channel = target_channel
        self.color_key = color_key
        self.mention_role = mention_role
        self.edit_message_id = edit_message_id

        self.title_input = discord.ui.TextInput(
            placeholder="例如：伺服器重要公告（可留空）",
            max_length=256,
            required=False,
        )
        self.content_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="請輸入公告詳細內文（支援多行與 Markdown 語法）...",
            max_length=4000,
            required=True,
        )
        self.add_item(discord.ui.Label(text="公告標題（選填）", component=self.title_input))
        self.add_item(discord.ui.Label(text="公告內容", component=self.content_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._execute_announce(
            interaction=interaction,
            target_channel=self.target_channel,
            title=self.title_input.value,
            content=self.content_input.value,
            color_key=self.color_key,
            mention_role=self.mention_role,
            edit_message_id=self.edit_message_id,
        )


# ==============================================================================
# Cog 主體
# ==============================================================================


class Manage(commands.Cog):
    """伺服器管理功能模組（/manage_* 系列斜線指令）。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --------------------------------------------------------------------------
    # 內部共用輔助
    # --------------------------------------------------------------------------

    @staticmethod
    def _validate_announce_request(
        interaction: discord.Interaction,
        target_channel: Any,
        mention_role: Optional[discord.Role],
        edit_message_id: Optional[str],
    ) -> tuple[
        Optional[discord.Guild],
        Optional[discord.Member],
        Optional[discord.Member],
        Optional[Union[discord.TextChannel, discord.Thread]],
        Optional[int],
        Optional[str],
    ]:
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            return None, None, None, None, None, err

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("manage_messages",))
        if err:
            return None, None, None, None, None, err

        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            return None, None, None, None, None, "❌ 公告只能發送或編輯於**文字頻道**或**討論串**中。"

        send_perm = "send_messages_in_threads" if isinstance(target_channel, discord.Thread) else "send_messages"
        err = _check_actor_and_bot_channel_perms(
            guild,
            target_channel,
            caller,
            bot_member,
            required_caller_perms=("manage_messages", send_perm),
            required_bot_perms=(send_perm, "embed_links"),
        )
        if err:
            return None, None, None, None, None, err

        caller_ch_perms = target_channel.permissions_for(caller)
        if mention_role is not None:
            role_guild = getattr(mention_role, "guild", None)
            if role_guild is None or role_guild.id != guild.id:
                return None, None, None, None, None, "❌ 所選提及身分組不屬於此伺服器。"
            if mention_role.is_default() and not getattr(caller_ch_perms, "mention_everyone", False):
                return (
                    None,
                    None,
                    None,
                    None,
                    None,
                    f"❌ 你在 {target_channel.mention} 缺少「提及 @everyone、@here 及所有身分組」權限。",
                )
            if not mention_role.mentionable and not getattr(caller_ch_perms, "mention_everyone", False):
                return (
                    None,
                    None,
                    None,
                    None,
                    None,
                    f"❌ 你沒有權限在 {target_channel.mention} 提及不可標記的身分組 {mention_role.mention}"
                    "（需具備「提及 @everyone、@here 及所有身分組」權限）。",
                )

        parsed_msg_id: Optional[int] = None
        if edit_message_id is not None and edit_message_id.strip():
            url_gid, url_cid, msg_id, parse_err = parse_message_reference(edit_message_id)
            if parse_err or msg_id is None:
                return None, None, None, None, None, parse_err
            if url_gid is not None and url_gid != guild.id:
                return None, None, None, None, None, "❌ 要編輯的訊息連結不屬於本伺服器。"
            if url_cid is not None and url_cid != target_channel.id:
                return None, None, None, None, None, "❌ 要編輯的訊息連結與指定的公告頻道不一致。"
            parsed_msg_id = msg_id

        return guild, caller, bot_member, target_channel, parsed_msg_id, None

    async def _execute_announce(
        self,
        *,
        interaction: discord.Interaction,
        target_channel: Any,
        title: Optional[str],
        content: Optional[str],
        color_key: Optional[str],
        mention_role: Optional[discord.Role],
        edit_message_id: Optional[str],
    ) -> None:
        guild, caller, _, resolved_ch, parsed_msg_id, err = self._validate_announce_request(
            interaction, target_channel, mention_role, edit_message_id
        )
        if err or guild is None or caller is None or resolved_ch is None:
            await _reply_ephemeral(interaction, err or "❌ 權限驗證失敗。")
            return

        clean_title = (title or "").strip()[:256]
        clean_content = (content or "").replace("\\n", "\n").strip()
        if not clean_title and not clean_content:
            await _reply_ephemeral(interaction, "❌ 公告標題與內容不可同時為空。")
            return
        if len(clean_content) > 4000:
            await _reply_ephemeral(interaction, "❌ 公告內容不可超過 4000 字元。")
            return

        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title=clean_title or None,
            description=clean_content or None,
            color=parse_announce_color(color_key),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"由 {caller.display_name} 發布", icon_url=caller.display_avatar.url)

        caller_ch_perms = resolved_ch.permissions_for(caller)
        allow_everyone = bool(getattr(caller_ch_perms, "mention_everyone", False))
        allowed_mentions = discord.AllowedMentions(
            everyone=allow_everyone,
            users=True,
            roles=[mention_role] if (mention_role is not None and not mention_role.is_default()) else allow_everyone,
        )
        content_ping = mention_role.mention if mention_role is not None else None

        try:
            if parsed_msg_id is not None:
                target_msg = await resolved_ch.fetch_message(parsed_msg_id)
                bot_user = getattr(self.bot, "user", None)
                if bot_user is None or target_msg.author.id != bot_user.id:
                    await _reply_ephemeral(interaction, "❌ 我只能編輯由本機器人發送的公告訊息。")
                    return
                await target_msg.edit(content=content_ping, embed=embed, allowed_mentions=allowed_mentions)
                await _reply_ephemeral(
                    interaction,
                    embed=discord.Embed(
                        title="✅ 公告已更新",
                        description=f"已在 {resolved_ch.mention} 更新公告訊息：[點此前往]({target_msg.jump_url})",
                        color=discord.Color.green(),
                    ),
                )
            else:
                sent_msg = await resolved_ch.send(
                    content=content_ping,
                    embed=embed,
                    allowed_mentions=allowed_mentions,
                )
                await _reply_ephemeral(
                    interaction,
                    embed=discord.Embed(
                        title="✅ 公告已發布",
                        description=f"已於 {resolved_ch.mention} 發布公告：[點此前往]({sent_msg.jump_url})",
                        color=discord.Color.green(),
                    ),
                )
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_announce")

    # ==========================================================================
    # A. 成員審核處置（Moderation）
    # ==========================================================================

    @app_commands.command(name="manage_kick", description="踢出成員")
    @app_commands.describe(member="目標成員", reason="踢出原因")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    async def manage_kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: Optional[str] = None,
    ):
        def _validate(inter: discord.Interaction) -> Optional[str]:
            guild, caller, bot_member, err = _resolve_guild_context(inter)
            if err or guild is None or caller is None or bot_member is None:
                return err
            err = _check_actor_and_bot_guild_perms(caller, bot_member, ("kick_members",), ("kick_members",))
            if err:
                return err
            target = guild.get_member(member.id) or member
            return _check_member_target_hierarchy(guild, caller, bot_member, target, allow_self=False)

        err = _validate(interaction)
        if err:
            await _reply_ephemeral(interaction, err)
            return

        clean_reason = (reason or "").strip() or "未填寫原因"

        async def _execute(inter: discord.Interaction) -> discord.Embed:
            guild, caller, _, _ = _resolve_guild_context(inter)
            assert guild is not None and caller is not None
            target = guild.get_member(member.id) or member
            dm_note = "（已嘗試私訊通知對方）"
            try:
                await target.send(f"⚠️ 你已被移出伺服器 **{guild.name}**。\n原因：{clean_reason}")
            except (discord.Forbidden, discord.HTTPException):
                dm_note = "（對方關閉私訊或無法傳送通知）"

            try:
                await target.kick(reason=format_audit_reason(caller, clean_reason))
            except Exception as exc:
                return await _build_action_error_embed(self.bot, inter, exc, "manage_kick", title="❌ 踢出失敗")

            return discord.Embed(
                title="✅ 已踢出成員",
                description=(
                    f"**成員**：{target.mention} (`{target.id}`)\n"
                    f"**原因**：{clean_reason}\n"
                    f"**執行者**：{caller.mention}\n"
                    f"ℹ️ {dm_note}"
                ),
                color=discord.Color.green(),
            )

        confirm_embed = discord.Embed(
            title="⚠️ 確認踢出成員",
            description=(
                f"即將把 **{member}** ({member.mention} / `{member.id}`) 踢出伺服器。\n"
                f"**原因**：{clean_reason}\n\n"
                "請點擊下方按鈕確認是否執行。"
            ),
            color=discord.Color.orange(),
        )
        view = ConfirmActionView(
            initiator_id=interaction.user.id,
            confirm_label="確認踢出",
            confirm_style=discord.ButtonStyle.danger,
            revalidate_fn=_validate,
            execute_fn=_execute,
        )
        view.message = await _reply_ephemeral(interaction, embed=confirm_embed, view=view)

    @app_commands.command(name="manage_ban", description="封鎖成員或使用者")
    @app_commands.describe(
        user="目標成員或使用者 ID",
        reason="封鎖原因",
        delete_message_days="刪除近幾天訊息（0~7）",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    async def manage_ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: Optional[str] = None,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ):
        def _validate(inter: discord.Interaction) -> Optional[str]:
            guild, caller, bot_member, err = _resolve_guild_context(inter)
            if err or guild is None or caller is None or bot_member is None:
                return err
            err = _check_actor_and_bot_guild_perms(caller, bot_member, ("ban_members",), ("ban_members",))
            if err:
                return err
            if user.id == caller.id:
                return "❌ 你無法封鎖自己。"
            if user.id == bot_member.id:
                return "❌ 你無法透過機器人封鎖機器人自己。"
            if user.id == guild.owner_id:
                return "❌ 無法封鎖伺服器擁有者。"

            member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
            if member is not None:
                return _check_member_target_hierarchy(guild, caller, bot_member, member, allow_self=False)
            return None

        err = _validate(interaction)
        if err:
            await _reply_ephemeral(interaction, err)
            return

        guild = interaction.guild
        assert guild is not None
        try:
            await guild.fetch_ban(user)
            await _reply_ephemeral(interaction, f"ℹ️ **{user}** (`{user.id}`) 目前已經在伺服器封鎖名單中。")
            return
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass

        clean_reason = (reason or "").strip() or "未填寫原因"
        del_days = int(delete_message_days)

        async def _execute(inter: discord.Interaction) -> discord.Embed:
            g, caller, _, _ = _resolve_guild_context(inter)
            assert g is not None and caller is not None
            dm_note = "（已嘗試私訊通知對方）"
            if g.get_member(user.id) is not None:
                try:
                    await user.send(f"🔨 你已被伺服器 **{g.name}** 永久封鎖。\n原因：{clean_reason}")
                except (discord.Forbidden, discord.HTTPException):
                    dm_note = "（對方關閉私訊或無法傳送通知）"
            else:
                dm_note = "（對象不在伺服器內，略過私訊通知）"

            try:
                await g.ban(
                    user,
                    delete_message_seconds=del_days * 86400,
                    reason=format_audit_reason(caller, clean_reason),
                )
            except Exception as exc:
                return await _build_action_error_embed(self.bot, inter, exc, "manage_ban", title="❌ 封鎖失敗")

            return discord.Embed(
                title="🔨 已封鎖使用者",
                description=(
                    f"**使用者**：{user} (`{user.id}`)\n"
                    f"**清除歷史訊息**：近 {del_days} 天\n"
                    f"**原因**：{clean_reason}\n"
                    f"**執行者**：{caller.mention}\n"
                    f"ℹ️ {dm_note}"
                ),
                color=discord.Color.green(),
            )

        confirm_embed = discord.Embed(
            title="⚠️ 確認封鎖使用者",
            description=(
                f"即將永久封鎖 **{user}** (`{user.id}`)。\n"
                f"**清除歷史訊息**：近 {del_days} 天\n"
                f"**原因**：{clean_reason}\n\n"
                "請點擊下方按鈕確認是否執行。"
            ),
            color=discord.Color.red(),
        )
        view = ConfirmActionView(
            initiator_id=interaction.user.id,
            confirm_label="確認封鎖",
            confirm_style=discord.ButtonStyle.danger,
            revalidate_fn=_validate,
            execute_fn=_execute,
        )
        view.message = await _reply_ephemeral(interaction, embed=confirm_embed, view=view)

    @app_commands.command(name="manage_unban", description="解除使用者封鎖")
    @app_commands.describe(user="使用者 ID 或名稱", reason="解封原因")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    async def manage_unban(
        self,
        interaction: discord.Interaction,
        user: str,
        reason: Optional[str] = None,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("ban_members",), ("ban_members",))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        query = user.strip()
        if not query:
            await _reply_ephemeral(interaction, "❌ 請輸入要解除封鎖的使用者 ID 或名稱。")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            target_user: Optional[discord.User] = None
            if query.isdigit():
                try:
                    ban_entry = await guild.fetch_ban(discord.Object(id=int(query)))
                    target_user = ban_entry.user
                except discord.NotFound:
                    target_user = None
            else:
                lower_q = query.lower()
                async for ban_entry in guild.bans(limit=200):
                    u = ban_entry.user
                    candidates = {
                        str(u.id),
                        u.name.lower(),
                        str(u).lower(),
                        (getattr(u, "global_name", None) or "").lower(),
                    }
                    if lower_q in candidates:
                        target_user = u
                        break

            if target_user is None:
                await _reply_ephemeral(interaction, f"❌ 在封鎖名單中找不到符合 `{query}` 的使用者。")
                return

            clean_reason = (reason or "").strip() or "未填寫原因"
            await guild.unban(target_user, reason=format_audit_reason(caller, clean_reason))
            embed = discord.Embed(
                title="✅ 已解除封鎖",
                description=(
                    f"**使用者**：{target_user} (`{target_user.id}`)\n"
                    f"**原因**：{clean_reason}\n"
                    f"**執行者**：{caller.mention}"
                ),
                color=discord.Color.green(),
            )
            await _reply_ephemeral(interaction, embed=embed)
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_unban")

    @manage_unban.autocomplete("user")
    async def _unban_user_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            return []
        if _check_actor_and_bot_guild_perms(caller, bot_member, ("ban_members",), ("ban_members",)):
            return []

        needle = (current or "").strip().lower()
        choices: list[app_commands.Choice[str]] = []
        try:
            async for entry in guild.bans(limit=25):
                u = entry.user
                display = getattr(u, "global_name", None) or u.name
                label = f"{display} (@{u.name} / {u.id})"[:100]
                if not needle or needle in label.lower() or needle in str(u.id):
                    choices.append(app_commands.Choice(name=label, value=str(u.id)))
                if len(choices) >= 25:
                    break
        except discord.HTTPException:
            return []
        return choices

    @app_commands.command(name="manage_timeout", description="禁言成員")
    @app_commands.describe(
        member="目標成員",
        duration="持續時間（如 10m、1h、1d，上限 28 天）",
        reason="禁言原因",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def manage_timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
        reason: Optional[str] = None,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("moderate_members",), ("moderate_members",))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        target = guild.get_member(member.id) or member
        err = _check_member_target_hierarchy(
            guild, caller, bot_member, target, allow_self=False, forbid_admin_target=True
        )
        if err:
            await _reply_ephemeral(interaction, err)
            return

        seconds, dur_err = parse_duration_seconds(duration)
        if dur_err or seconds is None:
            await _reply_ephemeral(interaction, dur_err)
            return

        await interaction.response.defer(ephemeral=True)
        clean_reason = (reason or "").strip() or "未填寫原因"
        until_dt = discord.utils.utcnow() + datetime.timedelta(seconds=seconds)
        try:
            await target.timeout(until_dt, reason=format_audit_reason(caller, clean_reason))
            embed = discord.Embed(
                title="🔇 已禁言成員",
                description=(
                    f"**成員**：{target.mention} (`{target.id}`)\n"
                    f"**時長**：{format_duration_zh(seconds)}\n"
                    f"**解除時間**：{discord.utils.format_dt(until_dt, style='F')}（{discord.utils.format_dt(until_dt, style='R')}）\n"
                    f"**原因**：{clean_reason}\n"
                    f"**執行者**：{caller.mention}"
                ),
                color=discord.Color.green(),
            )
            await _reply_ephemeral(interaction, embed=embed)
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_timeout")

    @manage_timeout.autocomplete("duration")
    async def _timeout_duration_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        needle = (current or "").strip().lower()
        return [
            app_commands.Choice(name=label, value=val)
            for val, label in _DURATION_PRESETS
            if not needle or needle in val.lower() or needle in label.lower()
        ][:25]

    @app_commands.command(name="manage_untimeout", description="解除成員禁言")
    @app_commands.describe(member="目標成員", reason="解除原因")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def manage_untimeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: Optional[str] = None,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("moderate_members",), ("moderate_members",))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        target = guild.get_member(member.id) or member
        err = _check_member_target_hierarchy(guild, caller, bot_member, target, allow_self=False)
        if err:
            await _reply_ephemeral(interaction, err)
            return

        if not target.is_timed_out():
            await _reply_ephemeral(interaction, f"ℹ️ {target.mention} 目前並沒有被禁言。")
            return

        await interaction.response.defer(ephemeral=True)
        clean_reason = (reason or "").strip() or "未填寫原因"
        try:
            await target.timeout(None, reason=format_audit_reason(caller, clean_reason))
            embed = discord.Embed(
                title="🔊 已解除禁言",
                description=(
                    f"**成員**：{target.mention} (`{target.id}`)\n"
                    f"**原因**：{clean_reason}\n"
                    f"**執行者**：{caller.mention}"
                ),
                color=discord.Color.green(),
            )
            await _reply_ephemeral(interaction, embed=embed)
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_untimeout")

    @app_commands.command(name="manage_warn", description="給予成員警告")
    @app_commands.describe(member="目標成員", reason="警告原因（最多 500 字）")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def manage_warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("moderate_members",))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        target = guild.get_member(member.id) or member
        err = _check_member_target_hierarchy(
            guild, caller, bot_member, target, allow_self=False, check_bot_hierarchy=False
        )
        if err:
            await _reply_ephemeral(interaction, err)
            return

        clean_reason = (reason or "").strip()
        if not clean_reason:
            await _reply_ephemeral(interaction, "❌ 警告原因不可為空白。")
            return
        if len(clean_reason) > 500:
            await _reply_ephemeral(interaction, "❌ 警告原因不可超過 500 字元。")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            record = _db.add_moderation_warning(guild.id, target.id, caller.id, clean_reason)
            total = _db.get_moderation_warning_count(guild.id, target.id)
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_warn.db")
            return

        dm_note = "（已私訊通知對方）"
        try:
            await target.send(
                f"⚠️ 你在伺服器 **{guild.name}** 收到了一筆管理員警告（編號 `#{record['id']}`）。\n"
                f"**原因**：{clean_reason}\n"
                f"**目前累計警告數**：{total} 筆"
            )
        except (discord.Forbidden, discord.HTTPException):
            dm_note = "（對方關閉私訊，未送出通知）"

        embed = discord.Embed(
            title="⚠️ 已記錄成員警告",
            description=(
                f"**警告編號**：`#{record['id']}`\n"
                f"**成員**：{target.mention} (`{target.id}`)\n"
                f"**原因**：{clean_reason}\n"
                f"**累計警告**：**{total}** 筆\n"
                f"**執行者**：{caller.mention}\n"
                f"ℹ️ {dm_note}"
            ),
            color=discord.Color.gold(),
        )
        await _reply_ephemeral(interaction, embed=embed)

    @app_commands.command(name="manage_warnings", description="管理成員警告紀錄")
    @app_commands.describe(
        member="目標成員",
        action="操作類型",
        warning_id="警告編號（刪除單筆時填寫）",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="查看警告紀錄", value="view"),
            app_commands.Choice(name="刪除單筆警告", value="remove"),
            app_commands.Choice(name="清空全部警告", value="clear"),
        ]
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def manage_warnings(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        action: str = "view",
        warning_id: Optional[int] = None,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("moderate_members",))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        target = guild.get_member(member.id) or member
        if getattr(target, "guild", None) is None or target.guild.id != guild.id:
            await _reply_ephemeral(interaction, "❌ 目標成員不屬於此伺服器。")
            return

        act = (action or "view").strip().lower()
        if act == "view":
            records = _db.get_moderation_warnings(guild.id, target.id)
            if not records:
                embed = discord.Embed(
                    title=f"ℹ️ {target.display_name} 的警告紀錄",
                    description=f"{target.mention} 目前在此伺服器沒有任何警告紀錄。",
                    color=discord.Color.light_grey(),
                )
                embed.set_thumbnail(url=target.display_avatar.url)
                await _reply_ephemeral(interaction, embed=embed)
                return

            embed = build_warnings_page_embed(guild, target, records, page=0)
            if len(records) > WARNINGS_PER_PAGE:
                view = WarningsPaginationView(
                    initiator_id=caller.id,
                    guild=guild,
                    member=target,
                    warnings_list=records,
                )
                view.message = await _reply_ephemeral(interaction, embed=embed, view=view)
            else:
                await _reply_ephemeral(interaction, embed=embed)
            return

        # 刪除／清空警告屬異動操作，嚴格禁止操作自己、擁有者或同級／更高階層成員
        def _validate_mutate(inter: discord.Interaction) -> Optional[str]:
            g, c, bm, e = _resolve_guild_context(inter)
            if e or g is None or c is None or bm is None:
                return e
            e = _check_actor_and_bot_guild_perms(c, bm, ("moderate_members",))
            if e:
                return e
            t = g.get_member(member.id) or member
            return _check_member_target_hierarchy(g, c, bm, t, allow_self=False, check_bot_hierarchy=False)

        err = _validate_mutate(interaction)
        if err:
            await _reply_ephemeral(interaction, err)
            return

        if act == "remove":
            if warning_id is None or warning_id <= 0:
                await _reply_ephemeral(interaction, "❌ 請在 `warning_id` 填入要刪除的正整數警告編號（如 `12`）。")
                return
            removed = _db.remove_moderation_warning(guild.id, target.id, int(warning_id))
            if not removed:
                await _reply_ephemeral(
                    interaction,
                    f"❌ 在 {target.mention} 的紀錄中找不到編號為 `#{warning_id}` 的警告。",
                )
                return
            remaining = _db.get_moderation_warning_count(guild.id, target.id)
            embed = discord.Embed(
                title="🗑️ 已刪除警告紀錄",
                description=(
                    f"已移除 {target.mention} 的警告 **`#{warning_id}`**。\n"
                    f"**目前剩餘警告**：{remaining} 筆\n"
                    f"**操作者**：{caller.mention}"
                ),
                color=discord.Color.green(),
            )
            await _reply_ephemeral(interaction, embed=embed)
            return

        if act == "clear":
            count = _db.get_moderation_warning_count(guild.id, target.id)
            if count == 0:
                await _reply_ephemeral(interaction, f"ℹ️ {target.mention} 目前沒有任何警告紀錄可清空。")
                return

            async def _execute_clear(inter: discord.Interaction) -> discord.Embed:
                g, c, _, _ = _resolve_guild_context(inter)
                assert g is not None and c is not None
                cleared = _db.clear_moderation_warnings(g.id, target.id)
                return discord.Embed(
                    title="🧹 已清空警告紀錄",
                    description=(
                        f"已清除 {target.mention} 在本伺服器的全部 **{cleared}** 筆警告紀錄。\n"
                        f"**操作者**：{c.mention}"
                    ),
                    color=discord.Color.green(),
                )

            confirm_embed = discord.Embed(
                title="⚠️ 確認清空全部警告紀錄",
                description=(
                    f"即將永久刪除 {target.mention} (`{target.id}`) 在本伺服器的全部 **{count}** 筆警告紀錄。\n"
                    "此動作無法復原，請確認是否繼續。"
                ),
                color=discord.Color.red(),
            )
            view = ConfirmActionView(
                initiator_id=caller.id,
                confirm_label="確認清空全部",
                confirm_style=discord.ButtonStyle.danger,
                revalidate_fn=_validate_mutate,
                execute_fn=_execute_clear,
            )
            view.message = await _reply_ephemeral(interaction, embed=confirm_embed, view=view)
            return

        await _reply_ephemeral(interaction, "❌ 不支援的操作類型。")

    # ==========================================================================
    # B. 訊息與頻道管理（Message & Channel Management）
    # ==========================================================================

    @app_commands.command(name="manage_purge", description="批次清理頻道訊息")
    @app_commands.describe(
        amount="掃描訊息數（1~100）",
        member="只刪指定成員訊息",
        contains="只刪包含此文字的訊息",
        bots_only="僅刪除機器人訊息",
        ignore_pinned="略過固定訊息",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def manage_purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
        member: Optional[discord.Member] = None,
        contains: Optional[str] = None,
        bots_only: bool = False,
        ignore_pinned: bool = True,
    ):
        channel = interaction.channel

        def _validate(inter: discord.Interaction) -> Optional[str]:
            guild, caller, bot_member, err = _resolve_guild_context(inter)
            if err or guild is None or caller is None or bot_member is None:
                return err
            err = _check_actor_and_bot_guild_perms(caller, bot_member, ("manage_messages",), ("manage_messages",))
            if err:
                return err
            if channel is None or not hasattr(channel, "purge"):
                return "❌ 此頻道類型不支援批次刪除訊息。"
            return _check_actor_and_bot_channel_perms(
                guild,
                channel,
                caller,
                bot_member,
                required_caller_perms=("manage_messages", "read_message_history"),
                required_bot_perms=("manage_messages", "read_message_history"),
            )

        err = _validate(interaction)
        if err:
            await _reply_ephemeral(interaction, err)
            return

        clean_contains = (contains or "").strip().lower()
        scan_limit = int(amount)

        def _match_msg(msg: discord.Message) -> bool:
            if ignore_pinned and getattr(msg, "pinned", False):
                return False
            if member is not None and msg.author.id != member.id:
                return False
            if bots_only and not getattr(msg.author, "bot", False):
                return False
            if clean_contains and clean_contains not in (msg.content or "").lower():
                return False
            return True

        async def _execute_purge(inter: discord.Interaction) -> discord.Embed:
            _, caller, _, _ = _resolve_guild_context(inter)
            assert caller is not None and channel is not None
            try:
                deleted = await channel.purge(
                    limit=scan_limit,
                    check=_match_msg,
                    bulk=True,
                    reason=format_audit_reason(caller, f"批次清理訊息（掃描 {scan_limit} 則）"),
                )
            except Exception as exc:
                return await _build_action_error_embed(self.bot, inter, exc, "manage_purge", title="❌ 清理失敗")

            filters: list[str] = []
            if member is not None:
                filters.append(f"成員：{member.mention}")
            if clean_contains:
                filters.append(f"包含關鍵字：`{clean_contains}`")
            if bots_only:
                filters.append("僅限機器人")
            filters.append("略過固定訊息" if ignore_pinned else "包含固定訊息")

            return discord.Embed(
                title="🧹 訊息清理完成",
                description=(
                    f"已於 {getattr(channel, 'mention', '此頻道')} 刪除 **{len(deleted)}** 則符合條件的訊息（掃描上限 {scan_limit} 則）。\n"
                    f"**篩選條件**：{'｜'.join(filters)}"
                ),
                color=discord.Color.green(),
            )

        if scan_limit > PURGE_CONFIRM_THRESHOLD:
            confirm_embed = discord.Embed(
                title="⚠️ 確認大量清理訊息",
                description=(
                    f"即將於 {getattr(channel, 'mention', '此頻道')} 掃描並刪除最多 **{scan_limit}** 則訊息。\n"
                    "此操作無法復原，請點擊下方按鈕確認是否執行。"
                ),
                color=discord.Color.orange(),
            )
            view = ConfirmActionView(
                initiator_id=interaction.user.id,
                confirm_label=f"確認清理（最多 {scan_limit} 則）",
                confirm_style=discord.ButtonStyle.danger,
                revalidate_fn=_validate,
                execute_fn=_execute_purge,
            )
            view.message = await _reply_ephemeral(interaction, embed=confirm_embed, view=view)
            return

        await interaction.response.defer(ephemeral=True)
        result_embed = await _execute_purge(interaction)
        await _reply_ephemeral(interaction, embed=result_embed)

    @app_commands.command(name="manage_slowmode", description="設定頻道慢速模式")
    @app_commands.describe(
        seconds="間隔秒數（0＝關閉，上限 21600）",
        channel="目標頻道（預設當前頻道）",
        reason="變更原因",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def manage_slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        channel: Optional[
            Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel, discord.Thread]
        ] = None,
        reason: Optional[str] = None,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        target = channel or interaction.channel
        if target is None or not hasattr(target, "slowmode_delay"):
            await _reply_ephemeral(interaction, "❌ 目標頻道類型不支援慢速模式設定。")
            return

        req_perm = "manage_threads" if isinstance(target, discord.Thread) else "manage_channels"
        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("manage_channels",), (req_perm,))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_channel_perms(
            guild, target, caller, bot_member, required_caller_perms=(req_perm,), required_bot_perms=(req_perm,)
        )
        if err:
            await _reply_ephemeral(interaction, err)
            return

        await interaction.response.defer(ephemeral=True)
        sec = int(seconds)
        try:
            await target.edit(slowmode_delay=sec, reason=format_audit_reason(caller, reason))
            embed = discord.Embed(
                title="⏱️ 已更新頻道慢速模式",
                description=(
                    f"**頻道**：{target.mention}\n"
                    f"**慢速間隔**：{format_duration_zh(sec)}\n"
                    f"**執行者**：{caller.mention}"
                ),
                color=discord.Color.green(),
            )
            await _reply_ephemeral(interaction, embed=embed)
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_slowmode")

    async def _toggle_channel_lock(
        self,
        interaction: discord.Interaction,
        channel: Optional[
            Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel, discord.Thread]
        ],
        reason: Optional[str],
        *,
        lock: bool,
    ) -> None:
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        target = channel or interaction.channel
        if target is None or not isinstance(
            target,
            (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel, discord.Thread),
        ):
            await _reply_ephemeral(interaction, "❌ 目標頻道類型不支援鎖定／解鎖操作。")
            return

        req_perm = "manage_threads" if isinstance(target, discord.Thread) else "manage_channels"
        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("manage_channels",), (req_perm,))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_channel_perms(
            guild, target, caller, bot_member, required_caller_perms=(req_perm,), required_bot_perms=(req_perm,)
        )
        if err:
            await _reply_ephemeral(interaction, err)
            return

        await interaction.response.defer(ephemeral=True)
        clean_reason = (reason or "").strip() or "未填寫原因"
        try:
            if isinstance(target, discord.Thread):
                if lock and target.locked:
                    await _reply_ephemeral(interaction, f"ℹ️ 討論串 {target.mention} 目前已處於鎖定狀態。")
                    return
                if not lock and not target.locked:
                    await _reply_ephemeral(interaction, f"ℹ️ 討論串 {target.mention} 目前未處於鎖定狀態。")
                    return
                await target.edit(locked=lock, reason=format_audit_reason(caller, clean_reason))
            else:
                overwrite = target.overwrites_for(guild.default_role)
                if lock and overwrite.send_messages is False:
                    await _reply_ephemeral(interaction, f"ℹ️ 頻道 {target.mention} 目前已處於鎖定狀態。")
                    return
                if not lock and overwrite.send_messages is not False:
                    await _reply_ephemeral(
                        interaction,
                        f"ℹ️ 頻道 {target.mention} 目前並未被鎖定（`@everyone` 發言權限未設為強制拒絕）。",
                    )
                    return
                overwrite.send_messages = False if lock else None
                await target.set_permissions(
                    guild.default_role,
                    overwrite=overwrite,
                    reason=format_audit_reason(caller, clean_reason),
                )

            notice_title = "🔒 此頻道已被管理員鎖定" if lock else "🔓 此頻道已解除鎖定"
            notice_color = discord.Color.red() if lock else discord.Color.green()
            if isinstance(target, (discord.TextChannel, discord.Thread)):
                bot_perms = target.permissions_for(bot_member)
                can_send = (
                    getattr(bot_perms, "send_messages_in_threads", False)
                    if isinstance(target, discord.Thread)
                    else getattr(bot_perms, "send_messages", False)
                )
                if getattr(bot_perms, "administrator", False) or (can_send and getattr(bot_perms, "embed_links", False)):
                    try:
                        await target.send(
                            embed=discord.Embed(
                                title=notice_title,
                                description=f"**原因**：{clean_reason}\n**執行者**：{caller.mention}",
                                color=notice_color,
                            )
                        )
                    except discord.HTTPException:
                        pass

            action_label = "鎖定" if lock else "解除鎖定"
            await _reply_ephemeral(
                interaction,
                embed=discord.Embed(
                    title=f"✅ 已{action_label}頻道",
                    description=f"**目標**：{target.mention}\n**原因**：{clean_reason}",
                    color=discord.Color.green(),
                ),
            )
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, f"manage_{'lock' if lock else 'unlock'}")

    @app_commands.command(name="manage_lock", description="鎖定頻道發言")
    @app_commands.describe(
        channel="目標頻道（預設當前頻道）",
        reason="鎖定原因",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def manage_lock(
        self,
        interaction: discord.Interaction,
        channel: Optional[
            Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel, discord.Thread]
        ] = None,
        reason: Optional[str] = None,
    ):
        await self._toggle_channel_lock(interaction, channel, reason, lock=True)

    @app_commands.command(name="manage_unlock", description="解除頻道鎖定")
    @app_commands.describe(
        channel="目標頻道（預設當前頻道）",
        reason="解鎖原因",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def manage_unlock(
        self,
        interaction: discord.Interaction,
        channel: Optional[
            Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel, discord.Thread]
        ] = None,
        reason: Optional[str] = None,
    ):
        await self._toggle_channel_lock(interaction, channel, reason, lock=False)

    @app_commands.command(name="manage_channel_create", description="建立頻道")
    @app_commands.describe(
        name="頻道名稱",
        channel_type="頻道類型",
        category="所屬分類",
        topic="頻道主題",
        nsfw="設為 NSFW",
    )
    @app_commands.choices(
        channel_type=[
            app_commands.Choice(name="文字頻道", value="text"),
            app_commands.Choice(name="語音頻道", value="voice"),
            app_commands.Choice(name="分類", value="category"),
            app_commands.Choice(name="公告頻道", value="news"),
            app_commands.Choice(name="論壇頻道", value="forum"),
            app_commands.Choice(name="舞台頻道", value="stage"),
        ]
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def manage_channel_create(
        self,
        interaction: discord.Interaction,
        name: str,
        channel_type: str = "text",
        category: Optional[discord.CategoryChannel] = None,
        topic: Optional[str] = None,
        nsfw: Optional[bool] = None,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("manage_channels",), ("manage_channels",))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        clean_name = (name or "").strip()
        if not clean_name or len(clean_name) > 100:
            await _reply_ephemeral(interaction, "❌ 頻道名稱長度必須介於 1 ~ 100 字元之間。")
            return

        if topic is not None and len(topic) > 1024:
            await _reply_ephemeral(interaction, "❌ 頻道主題不可超過 1024 字元。")
            return

        ctype = (channel_type or "text").strip().lower()
        if ctype == "category" and category is not None:
            await _reply_ephemeral(interaction, "❌ 分類本身不能再被放入另一個分類中。")
            return

        if category is not None:
            err = _check_actor_and_bot_channel_perms(
                guild, category, caller, bot_member, ("manage_channels",), ("manage_channels",)
            )
            if err:
                await _reply_ephemeral(interaction, err)
                return

        features = set(getattr(guild, "features", ()) or ())
        if ctype == "news" and "NEWS" not in features:
            await _reply_ephemeral(interaction, "❌ 此伺服器尚未啟用「社群 (Community)」功能，無法建立公告頻道。")
            return
        if ctype in ("forum", "stage") and "COMMUNITY" not in features:
            await _reply_ephemeral(interaction, "❌ 此伺服器尚未啟用「社群 (Community)」功能，無法建立論壇或舞台頻道。")
            return

        await interaction.response.defer(ephemeral=True)
        audit_reason = format_audit_reason(caller, f"建立頻道 {clean_name}")
        try:
            kwargs: dict[str, Any] = {"name": clean_name, "reason": audit_reason}
            if category is not None and ctype != "category":
                kwargs["category"] = category

            if ctype == "text":
                if topic is not None:
                    kwargs["topic"] = topic
                if nsfw is not None:
                    kwargs["nsfw"] = bool(nsfw)
                created = await guild.create_text_channel(**kwargs)
            elif ctype == "news":
                kwargs["news"] = True
                if topic is not None:
                    kwargs["topic"] = topic
                if nsfw is not None:
                    kwargs["nsfw"] = bool(nsfw)
                created = await guild.create_text_channel(**kwargs)
            elif ctype == "voice":
                if nsfw is not None:
                    kwargs["nsfw"] = bool(nsfw)
                created = await guild.create_voice_channel(**kwargs)
            elif ctype == "stage":
                if topic is not None:
                    kwargs["topic"] = topic
                created = await guild.create_stage_channel(**kwargs)
            elif ctype == "forum":
                if topic is not None:
                    kwargs["topic"] = topic
                if nsfw is not None:
                    kwargs["nsfw"] = bool(nsfw)
                created = await guild.create_forum(**kwargs)
            elif ctype == "category":
                created = await guild.create_category(**kwargs)
            else:
                await _reply_ephemeral(interaction, "❌ 不支援的頻道類型。")
                return

            embed = discord.Embed(
                title="✅ 已建立新頻道",
                description=(
                    f"**頻道**：{created.mention} (`{created.name}`)\n"
                    f"**類型**：{_channel_type_display(created)}\n"
                    f"**ID**：`{created.id}`"
                ),
                color=discord.Color.green(),
            )
            await _reply_ephemeral(interaction, embed=embed)
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_channel_create")

    @app_commands.command(name="manage_channel_edit", description="編輯頻道設定")
    @app_commands.describe(
        channel="目標頻道（預設當前頻道）",
        name="新名稱",
        topic="新主題",
        clear_topic="清除頻道主題",
        nsfw="設為 NSFW",
        category="移至指定分類",
        clear_category="移出所屬分類",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def manage_channel_edit(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.abc.GuildChannel] = None,
        name: Optional[str] = None,
        topic: Optional[str] = None,
        clear_topic: bool = False,
        nsfw: Optional[bool] = None,
        category: Optional[discord.CategoryChannel] = None,
        clear_category: bool = False,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("manage_channels",), ("manage_channels",))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        target = channel or interaction.channel
        if target is None or not isinstance(target, discord.abc.GuildChannel):
            await _reply_ephemeral(interaction, "❌ 目標不是可編輯的伺服器頻道（討論串請直接使用討論串設定）。")
            return

        err = _check_actor_and_bot_channel_perms(
            guild, target, caller, bot_member, ("manage_channels",), ("manage_channels",)
        )
        if err:
            await _reply_ephemeral(interaction, err)
            return

        edit_kwargs: dict[str, Any] = {}
        changes: list[str] = []

        if name is not None:
            clean_name = name.strip()
            if not clean_name or len(clean_name) > 100:
                await _reply_ephemeral(interaction, "❌ 頻道名稱長度必須介於 1 ~ 100 字元之間。")
                return
            edit_kwargs["name"] = clean_name
            changes.append(f"• **名稱**：`{clean_name}`")

        if clear_topic or topic is not None:
            if not isinstance(target, (discord.TextChannel, discord.ForumChannel, discord.StageChannel)):
                await _reply_ephemeral(interaction, f"❌ {_channel_type_display(target)}不支援設定頻道主題。")
                return
            if clear_topic:
                edit_kwargs["topic"] = None
                changes.append("• **主題**：已清除")
            elif topic is not None:
                if len(topic) > 1024:
                    await _reply_ephemeral(interaction, "❌ 頻道主題不可超過 1024 字元。")
                    return
                edit_kwargs["topic"] = topic
                changes.append("• **主題**：已更新")

        if nsfw is not None:
            if not isinstance(target, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)):
                await _reply_ephemeral(interaction, f"❌ {_channel_type_display(target)}不支援設定 NSFW 標記。")
                return
            edit_kwargs["nsfw"] = bool(nsfw)
            changes.append(f"• **NSFW**：{'開啟' if nsfw else '關閉'}")

        if clear_category or category is not None:
            if isinstance(target, discord.CategoryChannel):
                await _reply_ephemeral(interaction, "❌ 分類頻道不能被移動至其他分類中。")
                return
            if clear_category:
                edit_kwargs["category"] = None
                changes.append("• **所屬分類**：已移出")
            elif category is not None:
                err = _check_actor_and_bot_channel_perms(
                    guild, category, caller, bot_member, ("manage_channels",), ("manage_channels",)
                )
                if err:
                    await _reply_ephemeral(interaction, err)
                    return
                edit_kwargs["category"] = category
                changes.append(f"• **所屬分類**：`{category.name}`")

        if not edit_kwargs:
            await _reply_ephemeral(interaction, "❌ 請至少提供一個要修改的項目（名稱、主題、NSFW 或所屬分類）。")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await target.edit(**edit_kwargs, reason=format_audit_reason(caller, "編輯頻道設定"))
            embed = discord.Embed(
                title="✅ 頻道設定已更新",
                description=f"**目標頻道**：{target.mention}\n\n" + "\n".join(changes),
                color=discord.Color.green(),
            )
            await _reply_ephemeral(interaction, embed=embed)
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_channel_edit")

    @app_commands.command(name="manage_channel_delete", description="刪除頻道")
    @app_commands.describe(channel="要刪除的頻道", reason="刪除原因")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def manage_channel_delete(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel,
        reason: Optional[str] = None,
    ):
        def _validate(inter: discord.Interaction) -> Optional[str]:
            guild, caller, bot_member, err = _resolve_guild_context(inter)
            if err or guild is None or caller is None or bot_member is None:
                return err
            err = _check_actor_and_bot_guild_perms(caller, bot_member, ("manage_channels",), ("manage_channels",))
            if err:
                return err
            err = _check_actor_and_bot_channel_perms(
                guild, channel, caller, bot_member, ("manage_channels",), ("manage_channels",)
            )
            if err:
                return err
            protected_ids = {
                getattr(guild.rules_channel, "id", None),
                getattr(guild.public_updates_channel, "id", None),
            }
            if channel.id in protected_ids:
                return "❌ 此頻道為伺服器社群規則或官方更新專用頻道，Discord 不允許直接刪除。"
            return None

        err = _validate(interaction)
        if err:
            await _reply_ephemeral(interaction, err)
            return

        clean_reason = (reason or "").strip() or "未填寫原因"
        target_name = channel.name
        target_id = channel.id
        is_current_channel = interaction.channel_id == channel.id

        async def _execute(inter: discord.Interaction) -> discord.Embed:
            _, caller, _, _ = _resolve_guild_context(inter)
            assert caller is not None
            try:
                await channel.delete(reason=format_audit_reason(caller, clean_reason))
            except Exception as exc:
                return await _build_action_error_embed(self.bot, inter, exc, "manage_channel_delete", title="❌ 刪除頻道失敗")

            return discord.Embed(
                title="🗑️ 已永久刪除頻道",
                description=(
                    f"**已刪除頻道**：`{target_name}` (`{target_id}`)\n"
                    f"**原因**：{clean_reason}\n"
                    f"**執行者**：{caller.mention}"
                ),
                color=discord.Color.green(),
            )

        extra_warn = (
            "\n⚠️ **注意**：此頻道正是你目前執行指令的頻道，刪除後該頻道與對話視窗將立即消失！"
            if is_current_channel
            else ""
        )
        confirm_embed = discord.Embed(
            title="⚠️ 確認永久刪除頻道",
            description=(
                f"即將永久刪除{_channel_type_display(channel)} **{channel.mention}** (`{target_name}` / `{target_id}`)。\n"
                f"**原因**：{clean_reason}\n"
                f"❗ **此操作無法復原，頻道內所有訊息將永久消失！**{extra_warn}"
            ),
            color=discord.Color.red(),
        )
        view = ConfirmActionView(
            initiator_id=interaction.user.id,
            confirm_label="確認永久刪除",
            confirm_style=discord.ButtonStyle.danger,
            revalidate_fn=_validate,
            execute_fn=_execute,
            pre_edit_text=f"⏳ 正在永久刪除頻道 `{target_name}`..." if is_current_channel else None,
        )
        view.message = await _reply_ephemeral(interaction, embed=confirm_embed, view=view)

    @app_commands.command(name="manage_announce", description="發布或編輯公告")
    @app_commands.describe(
        channel="目標頻道（預設當前頻道）",
        title="公告標題",
        content="公告內容（可用 \\n 換行，留空則開視窗）",
        color="卡片顏色",
        mention_role="標記身分組",
        edit_message_id="要編輯的訊息 ID 或連結",
    )
    @app_commands.choices(
        color=[
            app_commands.Choice(name="藍紫 (Blurple)", value="blurple"),
            app_commands.Choice(name="天藍 (Blue)", value="blue"),
            app_commands.Choice(name="翠綠 (Green)", value="green"),
            app_commands.Choice(name="金黃 (Gold)", value="gold"),
            app_commands.Choice(name="橘紅 (Orange)", value="orange"),
            app_commands.Choice(name="緋紅 (Red)", value="red"),
        ]
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def manage_announce(
        self,
        interaction: discord.Interaction,
        channel: Optional[Union[discord.TextChannel, discord.Thread]] = None,
        title: Optional[str] = None,
        content: Optional[str] = None,
        color: Optional[str] = None,
        mention_role: Optional[discord.Role] = None,
        edit_message_id: Optional[str] = None,
    ):
        target_channel = channel or interaction.channel
        _, _, _, resolved_ch, _, err = self._validate_announce_request(
            interaction, target_channel, mention_role, edit_message_id
        )
        if err or resolved_ch is None:
            await _reply_ephemeral(interaction, err or "❌ 權限驗證失敗。")
            return

        if title is None and content is None:
            modal = AnnounceModal(
                cog=self,
                target_channel=resolved_ch,
                color_key=color,
                mention_role=mention_role,
                edit_message_id=edit_message_id,
            )
            await interaction.response.send_modal(modal)
            return

        await self._execute_announce(
            interaction=interaction,
            target_channel=resolved_ch,
            title=title,
            content=content,
            color_key=color,
            mention_role=mention_role,
            edit_message_id=edit_message_id,
        )

    @app_commands.command(name="manage_pin", description="固定或取消固定訊息")
    @app_commands.describe(
        action="操作類型",
        message_id="訊息 ID 或連結",
        channel="目標頻道（預設當前頻道）",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="固定訊息", value="pin"),
            app_commands.Choice(name="取消固定", value="unpin"),
        ]
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def manage_pin(
        self,
        interaction: discord.Interaction,
        action: str,
        message_id: str,
        channel: Optional[Union[discord.TextChannel, discord.Thread]] = None,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("manage_messages",), ("manage_messages",))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        url_gid, url_cid, parsed_mid, parse_err = parse_message_reference(message_id)
        if parse_err or parsed_mid is None:
            await _reply_ephemeral(interaction, parse_err)
            return
        if url_gid is not None and url_gid != guild.id:
            await _reply_ephemeral(interaction, "❌ 該訊息連結不屬於本伺服器。")
            return

        target_channel: Any = channel
        if url_cid is not None:
            if target_channel is not None and target_channel.id != url_cid:
                await _reply_ephemeral(interaction, "❌ 訊息連結中的頻道與你指定的頻道不一致。")
                return
            if target_channel is None:
                target_channel = guild.get_channel_or_thread(url_cid)
        elif target_channel is None:
            target_channel = interaction.channel

        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await _reply_ephemeral(interaction, "❌ 找不到有效的目標文字頻道或討論串。")
            return

        err = _check_actor_and_bot_channel_perms(
            guild,
            target_channel,
            caller,
            bot_member,
            required_caller_perms=("manage_messages", "read_message_history"),
            required_bot_perms=("manage_messages", "read_message_history"),
        )
        if err:
            await _reply_ephemeral(interaction, err)
            return

        await interaction.response.defer(ephemeral=True)
        act = (action or "").strip().lower()
        try:
            msg = await target_channel.fetch_message(parsed_mid)
            if act == "pin":
                if msg.pinned:
                    await _reply_ephemeral(interaction, f"ℹ️ 該訊息已經固定於 {target_channel.mention}。")
                    return
                await msg.pin(reason=format_audit_reason(caller, "固定訊息"))
                await _reply_ephemeral(
                    interaction,
                    embed=discord.Embed(
                        title="📌 已固定訊息",
                        description=f"已固定 {target_channel.mention} 的訊息：[點此前往]({msg.jump_url})",
                        color=discord.Color.green(),
                    ),
                )
            elif act == "unpin":
                if not msg.pinned:
                    await _reply_ephemeral(interaction, f"ℹ️ 該訊息目前並未固定於 {target_channel.mention}。")
                    return
                await msg.unpin(reason=format_audit_reason(caller, "取消固定訊息"))
                await _reply_ephemeral(
                    interaction,
                    embed=discord.Embed(
                        title="📍 已取消固定訊息",
                        description=f"已取消固定 {target_channel.mention} 的訊息：[點此前往]({msg.jump_url})",
                        color=discord.Color.green(),
                    ),
                )
            else:
                await _reply_ephemeral(interaction, "❌ 不支援的操作類型。")
        except discord.HTTPException as exc:
            if getattr(exc, "code", None) == 30003:
                await _reply_ephemeral(interaction, "❌ 此頻道的固定訊息數量已達 Discord 上限（50 則），請先取消固定舊訊息。")
                return
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_pin")
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_pin")

    # ==========================================================================
    # C. 身分組、暱稱、語音與成員資訊管理
    # ==========================================================================

    @app_commands.command(name="manage_role", description="指派或移除身分組")
    @app_commands.describe(
        action="操作類型",
        member="目標成員",
        role="目標身分組",
        reason="變更原因",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="指派身分組", value="add"),
            app_commands.Choice(name="移除身分組", value="remove"),
        ]
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def manage_role(
        self,
        interaction: discord.Interaction,
        action: str,
        member: discord.Member,
        role: discord.Role,
        reason: Optional[str] = None,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("manage_roles",), ("manage_roles",))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_role_hierarchy(guild, caller, bot_member, role)
        if err:
            await _reply_ephemeral(interaction, err)
            return

        target = guild.get_member(member.id) or member
        err = _check_member_target_hierarchy(
            guild, caller, bot_member, target, allow_self=True, check_bot_hierarchy=False
        )
        if err:
            await _reply_ephemeral(interaction, err)
            return

        await interaction.response.defer(ephemeral=True)
        act = (action or "").strip().lower()
        audit_reason = format_audit_reason(caller, reason)
        try:
            if act == "add":
                if role in target.roles:
                    await _reply_ephemeral(interaction, f"ℹ️ {target.mention} 已經擁有身分組 {role.mention}。")
                    return
                await target.add_roles(role, reason=audit_reason)
                embed = discord.Embed(
                    title="🏷️ 已指派身分組",
                    description=f"已為 {target.mention} 加上身分組 {role.mention}。\n**執行者**：{caller.mention}",
                    color=discord.Color.green(),
                )
                await _reply_ephemeral(interaction, embed=embed)
            elif act == "remove":
                if role not in target.roles:
                    await _reply_ephemeral(interaction, f"ℹ️ {target.mention} 並未擁有身分組 {role.mention}。")
                    return
                await target.remove_roles(role, reason=audit_reason)
                embed = discord.Embed(
                    title="🏷️ 已移除身分組",
                    description=f"已從 {target.mention} 移除身分組 {role.mention}。\n**執行者**：{caller.mention}",
                    color=discord.Color.green(),
                )
                await _reply_ephemeral(interaction, embed=embed)
            else:
                await _reply_ephemeral(interaction, "❌ 不支援的操作類型。")
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_role")

    @app_commands.command(name="manage_nick", description="修改或重設成員暱稱")
    @app_commands.describe(
        member="目標成員",
        nickname="新暱稱（留空則重設）",
        reason="變更原因",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_nicknames=True)
    @app_commands.checks.has_permissions(manage_nicknames=True)
    async def manage_nick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        nickname: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("manage_nicknames",), ("manage_nicknames",))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        target = guild.get_member(member.id) or member
        err = _check_member_target_hierarchy(guild, caller, bot_member, target, allow_self=False)
        if err:
            await _reply_ephemeral(interaction, err)
            return

        clean_nick = (nickname or "").strip() or None
        if clean_nick is not None and len(clean_nick) > 32:
            await _reply_ephemeral(interaction, "❌ Discord 暱稱長度上限為 32 字元。")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            old_display = target.display_name
            await target.edit(nick=clean_nick, reason=format_audit_reason(caller, reason))
            new_display = clean_nick if clean_nick is not None else f"{target.name}（已重設為原名稱）"
            embed = discord.Embed(
                title="✏️ 已更新成員暱稱",
                description=(
                    f"**成員**：{target.mention}\n"
                    f"**原顯示名稱**：{old_display}\n"
                    f"**新暱稱**：{new_display}\n"
                    f"**執行者**：{caller.mention}"
                ),
                color=discord.Color.green(),
            )
            await _reply_ephemeral(interaction, embed=embed)
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, "manage_nick")

    @app_commands.command(name="manage_voice", description="管理語音頻道成員")
    @app_commands.describe(
        action="操作類型",
        member="目標成員",
        target_channel="目標語音頻道（移動時必填）",
        reason="操作原因",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="踢出語音頻道 (斷線)", value="disconnect"),
            app_commands.Choice(name="移動語音頻道", value="move"),
            app_commands.Choice(name="伺服器靜音", value="mute"),
            app_commands.Choice(name="解除伺服器靜音", value="unmute"),
            app_commands.Choice(name="伺服器拒聽", value="deafen"),
            app_commands.Choice(name="解除伺服器拒聽", value="undeafen"),
        ]
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(move_members=True)
    @app_commands.check(_has_any_voice_mod_permission)
    async def manage_voice(
        self,
        interaction: discord.Interaction,
        action: str,
        member: discord.Member,
        target_channel: Optional[Union[discord.VoiceChannel, discord.StageChannel]] = None,
        reason: Optional[str] = None,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        act = (action or "").strip().lower()
        if act not in _VOICE_ACTION_PERMS:
            await _reply_ephemeral(interaction, "❌ 不支援的語音管理動作。")
            return

        req_perm, action_label = _VOICE_ACTION_PERMS[act]
        err = _check_actor_and_bot_guild_perms(caller, bot_member, (req_perm,), (req_perm,))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        target = guild.get_member(member.id) or member
        err = _check_member_target_hierarchy(guild, caller, bot_member, target, allow_self=False)
        if err:
            await _reply_ephemeral(interaction, err)
            return

        voice_state = getattr(target, "voice", None)
        current_vc = getattr(voice_state, "channel", None)
        if current_vc is None:
            await _reply_ephemeral(interaction, f"❌ {target.mention} 目前不在任何語音頻道中。")
            return

        if act == "move":
            if target_channel is None:
                await _reply_ephemeral(interaction, "❌ 執行「移動語音頻道」時，必須在 `target_channel` 指定目的地語音頻道。")
                return
            if current_vc.id == target_channel.id:
                await _reply_ephemeral(interaction, f"ℹ️ {target.mention} 目前已經在 {target_channel.mention} 中。")
                return
            err = _check_actor_and_bot_channel_perms(
                guild,
                target_channel,
                caller,
                bot_member,
                required_caller_perms=("move_members",),
                required_bot_perms=("connect", "move_members"),
            )
            if err:
                await _reply_ephemeral(interaction, err)
                return

        if act == "mute" and getattr(voice_state, "mute", False):
            await _reply_ephemeral(interaction, f"ℹ️ {target.mention} 目前已經處於伺服器靜音狀態。")
            return
        if act == "unmute" and not getattr(voice_state, "mute", True):
            await _reply_ephemeral(interaction, f"ℹ️ {target.mention} 目前並未被伺服器靜音。")
            return
        if act == "deafen" and getattr(voice_state, "deaf", False):
            await _reply_ephemeral(interaction, f"ℹ️ {target.mention} 目前已經處於伺服器拒聽狀態。")
            return
        if act == "undeafen" and not getattr(voice_state, "deaf", True):
            await _reply_ephemeral(interaction, f"ℹ️ {target.mention} 目前並未被伺服器拒聽。")
            return

        await interaction.response.defer(ephemeral=True)
        audit_reason = format_audit_reason(caller, reason)
        try:
            if act == "disconnect":
                await target.move_to(None, reason=audit_reason)
                detail = f"已將 {target.mention} 從 {current_vc.mention} 斷線移出。"
            elif act == "move":
                assert target_channel is not None
                await target.move_to(target_channel, reason=audit_reason)
                detail = f"已將 {target.mention} 從 {current_vc.mention} 移動至 {target_channel.mention}。"
            elif act == "mute":
                await target.edit(mute=True, reason=audit_reason)
                detail = f"已對 {target.mention} 啟用伺服器靜音。"
            elif act == "unmute":
                await target.edit(mute=False, reason=audit_reason)
                detail = f"已解除 {target.mention} 的伺服器靜音。"
            elif act == "deafen":
                await target.edit(deafen=True, reason=audit_reason)
                detail = f"已對 {target.mention} 啟用伺服器拒聽。"
            else:
                await target.edit(deafen=False, reason=audit_reason)
                detail = f"已解除 {target.mention} 的伺服器拒聽。"

            embed = discord.Embed(
                title=f"🎙️ 語音管理：{action_label}",
                description=f"{detail}\n**執行者**：{caller.mention}",
                color=discord.Color.green(),
            )
            await _reply_ephemeral(interaction, embed=embed)
        except Exception as exc:
            await _handle_discord_action_error(self.bot, interaction, exc, f"manage_voice.{act}")

    @app_commands.command(name="manage_member_info", description="查看成員管理資訊")
    @app_commands.describe(member="目標成員")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def manage_member_info(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        guild, caller, bot_member, err = _resolve_guild_context(interaction)
        if err or guild is None or caller is None or bot_member is None:
            await _reply_ephemeral(interaction, err)
            return

        err = _check_actor_and_bot_guild_perms(caller, bot_member, ("moderate_members",))
        if err:
            await _reply_ephemeral(interaction, err)
            return

        target = guild.get_member(member.id) or member
        if getattr(target, "guild", None) is None or target.guild.id != guild.id:
            await _reply_ephemeral(interaction, "❌ 目標成員不屬於此伺服器。")
            return

        warn_count = _db.get_moderation_warning_count(guild.id, target.id)
        roles = [r.mention for r in reversed(target.roles) if not r.is_default()]
        if len(roles) > 20:
            role_text = " ".join(roles[:20]) + f" …（共 {len(roles)} 個）"
        elif roles:
            role_text = " ".join(roles)
        else:
            role_text = "（僅 @everyone）"

        key_perm_names = (
            "administrator",
            "manage_guild",
            "ban_members",
            "kick_members",
            "moderate_members",
            "manage_channels",
            "manage_roles",
            "manage_messages",
            "manage_nicknames",
            "move_members",
        )
        perms = target.guild_permissions
        granted_perms = [PERMISSION_LABELS[p] for p in key_perm_names if getattr(perms, p, False)]
        perm_text = "、".join(granted_perms) if granted_perms else "無特殊管理權限"

        timeout_text = "未禁言"
        if target.is_timed_out() and getattr(target, "timed_out_until", None) is not None:
            timeout_text = (
                f"禁言中（至 {discord.utils.format_dt(target.timed_out_until, style='F')} / "
                f"{discord.utils.format_dt(target.timed_out_until, style='R')}）"
            )

        voice_ch = getattr(getattr(target, "voice", None), "channel", None)
        voice_text = voice_ch.mention if voice_ch else "未在語音頻道"
        joined_text = (
            f"{discord.utils.format_dt(target.joined_at, style='F')} ({discord.utils.format_dt(target.joined_at, style='R')})"
            if target.joined_at
            else "未知"
        )

        embed = discord.Embed(
            title=f"🛡️ 成員管理資訊：{target.display_name}",
            color=target.color if target.color.value else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="基本資料", value=f"{target.mention} (`{target.id}`)\n帳號類型：{'🤖 機器人' if target.bot else '👤 真人成員'}", inline=True)
        embed.add_field(name="最高身分組", value=target.top_role.mention if not target.top_role.is_default() else "@everyone", inline=True)
        embed.add_field(name="累計警告", value=f"**{warn_count}** 筆", inline=True)
        embed.add_field(name="帳號建立時間", value=f"{discord.utils.format_dt(target.created_at, style='F')} ({discord.utils.format_dt(target.created_at, style='R')})", inline=False)
        embed.add_field(name="加入伺服器時間", value=joined_text, inline=False)
        embed.add_field(name="禁言狀態", value=timeout_text, inline=True)
        embed.add_field(name="語音狀態", value=voice_text, inline=True)
        embed.add_field(name=f"身分組 ({len(roles)})", value=role_text[:1024], inline=False)
        embed.add_field(name="關鍵管理權限", value=perm_text[:1024], inline=False)
        embed.set_footer(text=f"{guild.name} • 僅指令執行者可見")
        await _reply_ephemeral(interaction, embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Manage(bot))
