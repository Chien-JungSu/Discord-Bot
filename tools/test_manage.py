"""獨立離線單元測試：驗證 cogs/manage.py、cogs/database.py 警告資料表與主程式／面板整合。

涵蓋項目：
1. 純函式解析：parse_duration_seconds、format_duration_zh、parse_message_reference、format_audit_reason
2. SQLite 警告紀錄持久化：新增／查詢／計數／單筆刪除／清空、伺服器與成員隔離保護
3. 雙層權限與階層防護：
   - 缺少執行者伺服器權限／機器人伺服器權限攔截
   - 跨頻道覆寫繞過防護（頻道覆寫拒絕時即使有伺服器權限仍遭攔截）
   - 自我／機器人／伺服器擁有者／同級與更高階層成員保護（即使具備 Administrator 也受階層約束）
   - 擁有者可略過自身階層比較但仍受機器人身分組階層保護
   - 身分組階層、@everyone、系統整合身分組 (managed)、跨伺服器物件攔截
   - 禁言管理員攔截、公告 @everyone 與不可標記身分組攔截
   - 語音指令依子動作分別檢查 move_members / mute_members / deafen_members 與目標頻道權限
   - 解封自動完成依權限決定是否回傳封鎖名單
4. 高風險二次確認面板、警告分頁與公告彈窗：
   - 非發起人點擊遭拒
   - 點擊取消不執行操作
   - 點擊確認前若權限遭撤銷，revalidate 立即中止操作
   - 逾時自動停用按鈕
5. 20 個 /manage_* 指令完整流程測試、裝飾器屬性檢查與 main / panel_bp / docs 整合檢查

執行方式：python tools/test_manage.py
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import discord
from discord import app_commands
from discord.ext import commands

import cogs.database as db
import cogs.manage as manage_mod
from cogs.manage import (
    AnnounceModal,
    ConfirmActionView,
    Manage,
    WarningsPaginationView,
    format_audit_reason,
    format_duration_zh,
    parse_duration_seconds,
    parse_message_reference,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(cond: bool, name: str) -> None:
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name)


def run_coro(coro):
    """輕量執行無真實非同步 I/O 的測試 coroutine（相容受限沙箱）。"""
    try:
        while True:
            yielded = coro.send(None)
            if yielded is not None:
                raise RuntimeError(f"測試 coroutine 意外等待非同步物件: {yielded!r}")
    except StopIteration as exc:
        return exc.value


class FakeRole:
    """可比較階層的假 Discord Role。"""

    def __init__(
        self,
        role_id: int,
        name: str,
        position: int,
        guild,
        *,
        is_everyone: bool = False,
        managed: bool = False,
    ):
        self.id = role_id
        self.name = name
        self.position = position
        self.guild = guild
        self._is_everyone = is_everyone
        self.managed = managed
        self.mention = "@everyone" if is_everyone else f"<@&{role_id}>"
        self.mentionable = False

    def is_default(self) -> bool:
        return self._is_everyone

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, FakeRole):
            return NotImplemented
        if self.guild.id != other.guild.id:
            raise RuntimeError("cannot compare roles from two different guilds.")
        if self._is_everyone:
            return not other._is_everyone
        if other._is_everyone:
            return False
        if self.position != other.position:
            return self.position < other.position
        return self.id > other.id

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, FakeRole):
            return NotImplemented
        return other < self

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FakeRole):
            return False
        return self.id == other.id and self.guild.id == other.guild.id


def make_perms(**overrides) -> discord.Permissions:
    perms = discord.Permissions.none()
    for key, val in overrides.items():
        setattr(perms, key, bool(val))
    return perms


def make_fake_member(
    user_id: int,
    name: str,
    guild,
    top_role: FakeRole,
    *,
    perms: discord.Permissions | None = None,
    bot: bool = False,
    roles: list[FakeRole] | None = None,
    timed_out_until: datetime.datetime | None = None,
    voice_channel=None,
    voice_mute: bool = False,
    voice_deaf: bool = False,
) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = user_id
    m.name = name
    m.display_name = name
    m.mention = f"<@{user_id}>"
    m.guild = guild
    m.bot = bot
    m.top_role = top_role
    m.roles = roles if roles is not None else [guild.default_role, top_role]
    m.guild_permissions = perms if perms is not None else make_perms()
    m.display_avatar = SimpleNamespace(url=f"https://cdn.discordapp.com/avatars/{user_id}/a.png")
    m.created_at = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    m.joined_at = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    m.color = discord.Color.blurple()
    m.timed_out_until = timed_out_until
    m.is_timed_out = MagicMock(return_value=timed_out_until is not None)
    if voice_channel is not None:
        m.voice = SimpleNamespace(channel=voice_channel, mute=voice_mute, deaf=voice_deaf)
    else:
        m.voice = None

    m.send = AsyncMock()
    m.kick = AsyncMock()
    m.timeout = AsyncMock()
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    m.edit = AsyncMock()
    m.move_to = AsyncMock()
    m.__str__ = lambda self=m: name
    return m


def make_fake_guild(guild_id: int = 1001, owner_id: int = 1, features: tuple[str, ...] = ("COMMUNITY", "NEWS")) -> MagicMock:
    g = MagicMock(spec=discord.Guild)
    g.id = guild_id
    g.name = f"TestGuild-{guild_id}"
    g.owner_id = owner_id
    g.features = list(features)
    g.default_role = FakeRole(guild_id, "@everyone", 0, g, is_everyone=True)
    g.rules_channel = None
    g.public_updates_channel = None
    g._members: dict[int, MagicMock] = {}
    g._channels: dict[int, MagicMock] = {}
    g._bans: dict[int, SimpleNamespace] = {}

    g.get_member = MagicMock(side_effect=lambda uid: g._members.get(uid))
    g.get_channel_or_thread = MagicMock(side_effect=lambda cid: g._channels.get(cid))
    g.ban = AsyncMock()
    g.unban = AsyncMock()
    g.create_text_channel = AsyncMock()
    g.create_voice_channel = AsyncMock()
    g.create_category = AsyncMock()
    g.create_forum = AsyncMock()
    g.create_stage_channel = AsyncMock()

    async def _fetch_ban(user_obj):
        uid = user_obj.id
        if uid in g._bans:
            return g._bans[uid]
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Ban")

    g.fetch_ban = AsyncMock(side_effect=_fetch_ban)

    def _bans_iter(limit=200):
        async def _gen():
            count = 0
            for entry in g._bans.values():
                yield entry
                count += 1
                if limit and count >= limit:
                    break
        return _gen()

    g.bans = MagicMock(side_effect=_bans_iter)
    return g


def make_fake_channel(
    channel_id: int,
    name: str,
    guild,
    *,
    spec_cls=discord.TextChannel,
    channel_perms_override: dict[int, discord.Permissions] | None = None,
    locked: bool = False,
    everyone_send: bool | None = None,
) -> MagicMock:
    ch = MagicMock(spec=spec_cls)
    ch.id = channel_id
    ch.name = name
    ch.mention = f"<#{channel_id}>"
    ch.guild = guild
    ch.slowmode_delay = 0
    ch.locked = locked
    overrides = channel_perms_override or {}

    def _perms_for(member):
        if member.id in overrides:
            return overrides[member.id]
        return member.guild_permissions

    ch.permissions_for = MagicMock(side_effect=_perms_for)
    overwrite_obj = discord.PermissionOverwrite(send_messages=everyone_send, view_channel=True)
    ch.overwrites_for = MagicMock(return_value=overwrite_obj)
    ch.set_permissions = AsyncMock()
    ch.edit = AsyncMock()
    ch.delete = AsyncMock()
    ch.send = AsyncMock()
    ch.fetch_message = AsyncMock()
    ch.purge = AsyncMock(return_value=[])
    guild._channels[channel_id] = ch
    return ch


def make_fake_interaction(user, guild, channel, bot=None) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.user = user
    inter.guild = guild
    inter.channel = channel
    inter.channel_id = channel.id if channel is not None else None
    inter.client = bot

    state = {"done": False, "sent": [], "edited": [], "modals": []}
    orig_msg = MagicMock(spec=discord.Message)
    orig_msg.edit = AsyncMock()

    async def _defer(*, ephemeral=False, thinking=False):
        state["done"] = True

    async def _send_message(*args, **kwargs):
        state["done"] = True
        state["sent"].append(kwargs if kwargs else {"content": args[0] if args else None})

    async def _edit_message(*args, **kwargs):
        state["done"] = True
        state["edited"].append(kwargs)

    async def _send_modal(modal):
        state["done"] = True
        state["modals"].append(modal)

    async def _followup_send(*args, **kwargs):
        state["sent"].append(kwargs if kwargs else {"content": args[0] if args else None})
        return orig_msg

    async def _edit_orig(*args, **kwargs):
        state["edited"].append(kwargs)
        return orig_msg

    resp = MagicMock()
    resp.is_done = MagicMock(side_effect=lambda: state["done"])
    resp.defer = AsyncMock(side_effect=_defer)
    resp.send_message = AsyncMock(side_effect=_send_message)
    resp.edit_message = AsyncMock(side_effect=_edit_message)
    resp.send_modal = AsyncMock(side_effect=_send_modal)
    inter.response = resp

    followup = MagicMock()
    followup.send = AsyncMock(side_effect=_followup_send)
    inter.followup = followup
    inter.original_response = AsyncMock(return_value=orig_msg)
    inter.edit_original_response = AsyncMock(side_effect=_edit_orig)
    inter._state_log = state
    return inter


def make_fake_bot(owner_id: int = 99999) -> commands.Bot:
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True
    bot = commands.Bot(command_prefix=("!", "/"), intents=intents)
    bot.owner_id = owner_id
    bot.notify_owner_error = AsyncMock()  # type: ignore[attr-defined]
    return bot


def main() -> None:
    tmpdir = tempfile.mkdtemp()
    orig_db_file = db.DB_FILE
    orig_db_init = db._db_initialized
    orig_wal_init = db._wal_initialized
    try:
        db.DB_FILE = os.path.join(tmpdir, "test_manage.db")
        db._db_initialized = False
        db._wal_initialized = False

        # ======================================================================
        # 1. 純函式解析測試
        # ======================================================================
        print("\n--- [1] 純函式解析與格式化 ---")
        check(parse_duration_seconds("10m") == (600, None), "parse_duration_seconds: 10m -> 600")
        check(parse_duration_seconds("1h30m") == (5400, None), "parse_duration_seconds: 1h30m -> 5400")
        check(parse_duration_seconds("2天3小時") == (2 * 86400 + 3 * 3600, None), "parse_duration_seconds: 支援中文單位")
        check(parse_duration_seconds("28d") == (28 * 86400, None), "parse_duration_seconds: 接受 28 天上限")
        check(parse_duration_seconds("29d")[0] is None, "parse_duration_seconds: 拒絕超過 28 天")
        check(parse_duration_seconds("3s")[0] is None, "parse_duration_seconds: 拒絕低於 5 秒")
        check(parse_duration_seconds("10")[0] is None, "parse_duration_seconds: 拒絕無單位純數字")
        check(parse_duration_seconds("abc")[0] is None, "parse_duration_seconds: 拒絕無效字串")
        check(format_duration_zh(0) == "0 秒（已關閉）", "format_duration_zh: 0 秒顯示已關閉")
        check(format_duration_zh(90061) == "1 天 1 小時 1 分鐘 1 秒", "format_duration_zh: 完整格式化")

        check(parse_message_reference("123456789012345678") == (None, None, 123456789012345678, None), "parse_message_reference: 純 ID")
        check(
            parse_message_reference("https://discord.com/channels/111/222/333") == (111, 222, 333, None),
            "parse_message_reference: 完整 Discord 連結",
        )
        check(
            parse_message_reference("https://canary.discord.com/channels/11/22/33") == (11, 22, 33, None),
            "parse_message_reference: Canary 連結",
        )
        check(parse_message_reference("not_a_msg")[3] is not None, "parse_message_reference: 拒絕無效格式")

        long_reason = "x" * 600
        caller_dummy = SimpleNamespace(id=42, __str__=lambda s: "Admin#0001")
        formatted_reason = format_audit_reason(caller_dummy, long_reason)
        check(len(formatted_reason) <= 512, "format_audit_reason: 強制符合 Discord 512 字元上限")

        # ======================================================================
        # 2. SQLite 警告紀錄持久化與隔離測試
        # ======================================================================
        print("\n--- [2] SQLite 警告紀錄資料層 ---")
        w1 = db.add_moderation_warning(1001, 500, 10, "刷頻洗版", created_at=1000.0)
        w2 = db.add_moderation_warning(1001, 500, 10, "二次違規", created_at=2000.0)
        w_other_guild = db.add_moderation_warning(2002, 500, 10, "別的伺服器警告", created_at=3000.0)

        rows = db.get_moderation_warnings(1001, 500)
        check(len(rows) == 2 and rows[0]["id"] == w2["id"], "get_moderation_warnings 依時間新到舊排序")
        check(db.get_moderation_warning_count(1001, 500) == 2, "get_moderation_warning_count 計數正確")
        check(not db.remove_moderation_warning(1001, 500, w_other_guild["id"]), "跨伺服器無法刪除其他伺服器警告")
        check(not db.remove_moderation_warning(1001, 999, w1["id"]), "跨成員無法刪除其他成員警告")
        check(db.remove_moderation_warning(1001, 500, w1["id"]), "同伺服器同成員可刪除單筆警告")
        check(not db.remove_moderation_warning(1001, 500, w1["id"]), "重複刪除同一筆回傳 False")
        check(db.clear_moderation_warnings(1001, 500) == 1, "clear_moderation_warnings 回傳清除數量")
        check(db.get_moderation_warning_count(1001, 500) == 0, "清空後警告數歸零")
        check(db.get_moderation_warning_count(2002, 500) == 1, "其他伺服器警告不受清空影響")

        # ======================================================================
        # 3. 權限與階層測試準備
        # ======================================================================
        print("\n--- [3] 嚴格權限與階層驗證 ---")
        guild = make_fake_guild(guild_id=1001, owner_id=1)
        other_guild = make_fake_guild(guild_id=9999, owner_id=8)

        role_bot = FakeRole(10, "炙彗JI器人", 50, guild)
        role_admin = FakeRole(20, "Admin", 40, guild)
        role_mod = FakeRole(30, "Mod", 20, guild)
        role_vip = FakeRole(40, "VIP", 10, guild)
        role_managed = FakeRole(50, "BotManaged", 15, guild, managed=True)
        role_high = FakeRole(60, "SuperAdmin", 60, guild)
        role_other_guild = FakeRole(70, "Foreign", 5, other_guild)

        bot_user = SimpleNamespace(id=777, name="Bot")
        bot = make_fake_bot()
        bot._connection = SimpleNamespace(user=bot_user)

        bot_member = make_fake_member(
            777,
            "炙彗JI器人",
            guild,
            role_bot,
            perms=make_perms(
                kick_members=True,
                ban_members=True,
                moderate_members=True,
                manage_messages=True,
                read_message_history=True,
                send_messages=True,
                send_messages_in_threads=True,
                embed_links=True,
                view_channel=True,
                manage_channels=True,
                manage_threads=True,
                manage_roles=True,
                manage_nicknames=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
                connect=True,
            ),
            bot=True,
        )
        owner = make_fake_member(1, "GuildOwner", guild, guild.default_role, perms=make_perms(administrator=True))
        admin = make_fake_member(
            2,
            "AdminUser",
            guild,
            role_admin,
            perms=make_perms(administrator=True),
        )
        mod = make_fake_member(
            3,
            "ModUser",
            guild,
            role_mod,
            perms=make_perms(
                kick_members=True,
                ban_members=True,
                moderate_members=True,
                manage_messages=True,
                read_message_history=True,
                send_messages=True,
                send_messages_in_threads=True,
                embed_links=True,
                view_channel=True,
                manage_channels=True,
                manage_threads=True,
                manage_roles=True,
                manage_nicknames=True,
                move_members=True,
                connect=True,
            ),
        )
        low_user = make_fake_member(4, "RegularUser", guild, role_vip, perms=make_perms(view_channel=True, send_messages=True))
        higher_target = make_fake_member(5, "HigherMod", guild, role_high, perms=make_perms(kick_members=True))
        same_rank_mod = make_fake_member(6, "SameRankMod", guild, role_mod, perms=make_perms(kick_members=True))
        foreign_member = make_fake_member(7, "Foreign", other_guild, role_other_guild)

        for m_obj in (bot_member, owner, admin, mod, low_user, higher_target, same_rank_mod):
            guild._members[m_obj.id] = m_obj
        guild.me = bot_member
        other_guild.me = make_fake_member(777, "炙彗JI器人", other_guild, role_other_guild, bot=True)

        main_ch = make_fake_channel(100, "general", guild)
        restricted_ch = make_fake_channel(
            101,
            "mod-denied",
            guild,
            channel_perms_override={
                mod.id: make_perms(view_channel=True, send_messages=False, manage_messages=False, manage_channels=False),
            },
        )
        vc1 = make_fake_channel(201, "Voice-1", guild, spec_cls=discord.VoiceChannel)
        vc2 = make_fake_channel(202, "Voice-2", guild, spec_cls=discord.VoiceChannel)
        thread_ch = make_fake_channel(301, "Thread-1", guild, spec_cls=discord.Thread)

        cog = Manage(bot)

        # 3a. 權限不足／階層檢查測試
        inter = make_fake_interaction(low_user, guild, main_ch, bot)
        run_coro(cog.manage_kick.callback(cog, inter, low_user))
        check("❌ 你沒有使用此指令所需的伺服器權限" in inter._state_log["sent"][-1]["content"], "manage_kick: 無權限一般成員遭拒")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_kick.callback(cog, inter, mod))
        check("無法對自己" in inter._state_log["sent"][-1]["content"], "manage_kick: 禁止踢出自己")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_kick.callback(cog, inter, bot_member))
        check("無法對機器人本身" in inter._state_log["sent"][-1]["content"], "manage_kick: 禁止踢出機器人")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_kick.callback(cog, inter, owner))
        check("伺服器擁有者" in inter._state_log["sent"][-1]["content"], "manage_kick: 禁止踢出擁有者")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_kick.callback(cog, inter, same_rank_mod))
        check("最高身分組高於或等於你" in inter._state_log["sent"][-1]["content"], "manage_kick: 禁止踢出同級成員")

        inter = make_fake_interaction(admin, guild, main_ch, bot)
        run_coro(cog.manage_kick.callback(cog, inter, higher_target))
        check("最高身分組高於或等於你" in inter._state_log["sent"][-1]["content"], "manage_kick: 具 Administrator 亦受角色階層限制")

        inter = make_fake_interaction(owner, guild, main_ch, bot)
        run_coro(cog.manage_kick.callback(cog, inter, higher_target))
        check("高於或等於機器人" in inter._state_log["sent"][-1]["content"], "manage_kick: 擁有者可略過自身階層但仍受機器人階層保護")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_kick.callback(cog, inter, foreign_member))
        check("不屬於此伺服器" in inter._state_log["sent"][-1]["content"], "manage_kick: 跨伺服器成員安全攔截")

        # 3b. 跨頻道覆寫權限測試
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_lock.callback(cog, inter, channel=restricted_ch))
        check("缺少必要頻道權限" in inter._state_log["sent"][-1]["content"], "manage_lock: 跨頻道覆寫拒絕時即使具伺服器權限仍遭攔截")

        inter = make_fake_interaction(mod, guild, restricted_ch, bot)
        run_coro(cog.manage_purge.callback(cog, inter, amount=5))
        check("缺少必要頻道權限" in inter._state_log["sent"][-1]["content"], "manage_purge: 頻道覆寫無 manage_messages 遭攔截")

        # 3c. 禁言管理員攔截
        inter = make_fake_interaction(owner, guild, main_ch, bot)
        run_coro(cog.manage_timeout.callback(cog, inter, admin, "10m"))
        check("管理員 (Administrator)" in inter._state_log["sent"][-1]["content"], "manage_timeout: 阻擋禁言具備 Administrator 的成員")

        # 3d. 身分組管理階層與保護
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_role.callback(cog, inter, "add", low_user, guild.default_role))
        check("@everyone" in inter._state_log["sent"][-1]["content"], "manage_role: 禁止操作 @everyone")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_role.callback(cog, inter, "add", low_user, role_managed))
        check("整合服務" in inter._state_log["sent"][-1]["content"], "manage_role: 禁止操作 managed 身分組")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_role.callback(cog, inter, "add", low_user, role_admin))
        check("高於或等於你的最高身分組" in inter._state_log["sent"][-1]["content"], "manage_role: 禁止指派高於自身階層的身分組")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_role.callback(cog, inter, "add", low_user, role_other_guild))
        check("不屬於此伺服器" in inter._state_log["sent"][-1]["content"], "manage_role: 跨伺服器身分組安全攔截")

        # 3e. 公告 @everyone 與不可標記身分組攔截
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(
            cog.manage_announce.callback(
                cog, inter, channel=main_ch, title="公告", content="內容", mention_role=role_vip
            )
        )
        check("提及不可標記的身分組" in inter._state_log["sent"][-1]["content"], "manage_announce: 無 mention_everyone 時禁止提及不可標記身分組")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(
            cog.manage_announce.callback(
                cog,
                inter,
                channel=main_ch,
                title="公告",
                content="內容",
                edit_message_id="https://discord.com/channels/9999/100/555",
            )
        )
        check("不屬於本伺服器" in inter._state_log["sent"][-1]["content"], "manage_announce: 禁止編輯跨伺服器訊息連結")

        # 3f. 語音管理依子動作檢查權限
        low_user.voice = SimpleNamespace(channel=vc1, mute=False, deaf=False)
        inter = make_fake_interaction(mod, guild, main_ch, bot)  # mod 有 move_members，沒有 mute_members
        run_coro(cog.manage_voice.callback(cog, inter, "mute", low_user))
        check("將成員靜音" in inter._state_log["sent"][-1]["content"], "manage_voice: 有 move_members 但無 mute_members 不可靜音")

        # ======================================================================
        # 4. 確認面板安全性與分頁／彈窗測試
        # ======================================================================
        print("\n--- [4] 二次確認面板、權限撤銷中止、分頁與公告彈窗 ---")
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_kick.callback(cog, inter, low_user, reason="測試踢出"))
        kick_view = inter._state_log["sent"][-1]["view"]
        check(isinstance(kick_view, ConfirmActionView), "manage_kick 顯示 ConfirmActionView")

        # 非發起人點擊確認
        other_click = make_fake_interaction(admin, guild, main_ch, bot)
        allowed = run_coro(kick_view.interaction_check(other_click))
        check(not allowed, "ConfirmActionView 拒絕非發起人點擊")

        # 發起人點擊確認前權限遭撤銷
        orig_mod_perms = mod.guild_permissions
        mod.guild_permissions = make_perms(view_channel=True)
        confirm_click = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(kick_view._on_confirm(confirm_click))
        check(not low_user.kick.called, "點擊確認前若權限遭撤銷，立即中止踢出")
        check("權限或狀態已變更" in confirm_click._state_log["edited"][-1]["embed"].title, "權限失效時回報操作已中止")
        mod.guild_permissions = orig_mod_perms

        # 重新產生確認面板並正常確認踢出
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_kick.callback(cog, inter, low_user, reason="確認踢出"))
        kick_view2 = inter._state_log["sent"][-1]["view"]
        confirm_click2 = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(kick_view2._on_confirm(confirm_click2))
        check(low_user.kick.called, "ConfirmActionView 確認後成功執行 kick")

        # 取消按鈕測試
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_kick.callback(cog, inter, low_user, reason="取消測試"))
        kick_view3 = inter._state_log["sent"][-1]["view"]
        cancel_click = make_fake_interaction(mod, guild, main_ch, bot)
        low_user.kick.reset_mock()
        run_coro(kick_view3._on_cancel(cancel_click))
        check(not low_user.kick.called and "取消" in cancel_click._state_log["edited"][-1]["embed"].title, "ConfirmActionView 取消不執行操作")

        # 逾時停用按鈕測試
        run_coro(kick_view3.on_timeout())
        check(all(item.disabled for item in kick_view3.children), "ConfirmActionView on_timeout 停用全部按鈕")

        # ======================================================================
        # 5. 所有 20 個指令正常執行路徑測試
        # ======================================================================
        print("\n--- [5] 20 個 /manage_* 指令功能驗證 ---")
        # 5.1 ban & unban + autocomplete
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_ban.callback(cog, inter, low_user, reason="違規封鎖", delete_message_days=2))
        ban_view = inter._state_log["sent"][-1]["view"]
        run_coro(ban_view._on_confirm(make_fake_interaction(mod, guild, main_ch, bot)))
        check(guild.ban.called and guild.ban.call_args.kwargs["delete_message_seconds"] == 2 * 86400, "manage_ban 確認後執行封鎖並傳入 delete_message_seconds")

        banned_u = MagicMock(spec=discord.User)
        banned_u.id = low_user.id
        banned_u.name = "RegularUser"
        banned_u.global_name = "Regular"
        banned_u.__str__ = lambda s=banned_u: "RegularUser"
        guild._bans[low_user.id] = SimpleNamespace(user=banned_u, reason="bad")

        unauth_ac = run_coro(cog._unban_user_autocomplete(make_fake_interaction(low_user, guild, main_ch, bot), ""))
        auth_ac = run_coro(cog._unban_user_autocomplete(make_fake_interaction(mod, guild, main_ch, bot), "reg"))
        check(unauth_ac == [] and len(auth_ac) == 1 and auth_ac[0].value == str(low_user.id), "manage_unban autocomplete 嚴格保護封鎖名單")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_unban.callback(cog, inter, str(low_user.id), reason="申訴通過"))
        check(guild.unban.called, "manage_unban 成功解除封鎖")

        # 5.2 timeout & untimeout
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_timeout.callback(cog, inter, low_user, "15m", reason="冷靜一下"))
        check(low_user.timeout.called, "manage_timeout 成功禁言")

        low_user.timed_out_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
        low_user.is_timed_out = MagicMock(return_value=True)
        low_user.timeout.reset_mock()
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_untimeout.callback(cog, inter, low_user, reason="提前解禁"))
        check(low_user.timeout.called and low_user.timeout.call_args.args[0] is None, "manage_untimeout 成功解除禁言")
        low_user.is_timed_out = MagicMock(return_value=False)

        # 5.3 warn & warnings (pagination + remove + clear)
        for i in range(10):
            inter = make_fake_interaction(mod, guild, main_ch, bot)
            run_coro(cog.manage_warn.callback(cog, inter, low_user, f"違規事項 #{i + 1}"))
        check(db.get_moderation_warning_count(guild.id, low_user.id) == 10, "manage_warn 成功持久化 10 筆警告")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_warnings.callback(cog, inter, low_user, action="view"))
        page_view = inter._state_log["sent"][-1]["view"]
        check(isinstance(page_view, WarningsPaginationView) and page_view.total_pages == 2, "manage_warnings 超過 8 筆啟用分頁 View")
        next_click = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(page_view._on_next(next_click))
        check(page_view.page == 1, "WarningsPaginationView 成功翻頁至第 2 頁")

        first_wid = db.get_moderation_warnings(guild.id, low_user.id)[0]["id"]
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_warnings.callback(cog, inter, low_user, action="remove", warning_id=first_wid))
        check(db.get_moderation_warning_count(guild.id, low_user.id) == 9, "manage_warnings action=remove 成功刪除單筆")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_warnings.callback(cog, inter, low_user, action="clear"))
        clear_view = inter._state_log["sent"][-1]["view"]
        run_coro(clear_view._on_confirm(make_fake_interaction(mod, guild, main_ch, bot)))
        check(db.get_moderation_warning_count(guild.id, low_user.id) == 0, "manage_warnings action=clear 確認後清空全部")

        # 5.4 purge (<=25 直接執行，>25 二次確認)
        main_ch.purge = AsyncMock(return_value=[1, 2, 3])
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_purge.callback(cog, inter, amount=10, member=low_user, contains="spam"))
        check(main_ch.purge.called, "manage_purge <= 25 直接執行清理")

        main_ch.purge.reset_mock()
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_purge.callback(cog, inter, amount=50))
        purge_view = inter._state_log["sent"][-1]["view"]
        check(isinstance(purge_view, ConfirmActionView) and not main_ch.purge.called, "manage_purge > 25 顯示二次確認面板")
        run_coro(purge_view._on_confirm(make_fake_interaction(mod, guild, main_ch, bot)))
        check(main_ch.purge.called, "manage_purge > 25 確認後執行清理")

        # 5.5 slowmode, lock, unlock (頻道與討論串)
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_slowmode.callback(cog, inter, seconds=30, channel=main_ch))
        check(main_ch.edit.call_args.kwargs["slowmode_delay"] == 30, "manage_slowmode 設定頻道慢速模式")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_lock.callback(cog, inter, channel=main_ch, reason="維護"))
        check(main_ch.set_permissions.called and main_ch.set_permissions.call_args.kwargs["overwrite"].send_messages is False, "manage_lock 設定 @everyone send_messages=False")

        main_ch.overwrites_for = MagicMock(return_value=discord.PermissionOverwrite(send_messages=False, view_channel=True))
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_unlock.callback(cog, inter, channel=main_ch, reason="恢復"))
        check(main_ch.set_permissions.call_args.kwargs["overwrite"].send_messages is None, "manage_unlock 還原 send_messages=None 並保留 view_channel")

        inter = make_fake_interaction(mod, guild, thread_ch, bot)
        run_coro(cog.manage_lock.callback(cog, inter, channel=thread_ch))
        check(thread_ch.edit.call_args.kwargs["locked"] is True, "manage_lock 支援討論串鎖定")

        # 5.6 channel_create, channel_edit, channel_delete
        new_ch_mock = make_fake_channel(555, "announcements-new", guild)
        guild.create_text_channel = AsyncMock(return_value=new_ch_mock)
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_channel_create.callback(cog, inter, name="announcements-new", channel_type="text", topic="測試主題"))
        check(guild.create_text_channel.called, "manage_channel_create 成功建立頻道")

        cat_ch = make_fake_channel(600, "Cat", guild, spec_cls=discord.CategoryChannel)
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_channel_edit.callback(cog, inter, channel=main_ch, name="general-renamed", category=cat_ch))
        check(main_ch.edit.call_args.kwargs["name"] == "general-renamed" and main_ch.edit.call_args.kwargs["category"] == cat_ch, "manage_channel_edit 成功編輯頻道")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_channel_delete.callback(cog, inter, channel=new_ch_mock, reason="清理"))
        del_view = inter._state_log["sent"][-1]["view"]
        check(isinstance(del_view, ConfirmActionView) and not new_ch_mock.delete.called, "manage_channel_delete 強制二次確認")
        run_coro(del_view._on_confirm(make_fake_interaction(mod, guild, main_ch, bot)))
        check(new_ch_mock.delete.called, "manage_channel_delete 確認後刪除頻道")

        # 刪除「目前執行指令所在頻道」分支（pre_edit_text + NotFound 容錯）
        cur_ch = make_fake_channel(556, "current-victim", guild)
        inter = make_fake_interaction(mod, guild, cur_ch, bot)
        run_coro(cog.manage_channel_delete.callback(cog, inter, channel=cur_ch, reason="自刪頻道"))
        cur_del_view = inter._state_log["sent"][-1]["view"]
        check(bool(cur_del_view.pre_edit_text), "manage_channel_delete 刪除目前頻道時啟用 pre_edit_text")
        cur_confirm = make_fake_interaction(mod, guild, cur_ch, bot)
        cur_confirm.edit_original_response = AsyncMock(
            side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")
        )
        run_coro(cur_del_view._on_confirm(cur_confirm))
        check(cur_ch.delete.called, "manage_channel_delete 刪除目前頻道並安全忽略 edit_original_response NotFound")

        # 5.7 announce (inline + Modal) & pin
        sent_ann = SimpleNamespace(jump_url="https://discord.com/channels/1001/100/888")
        main_ch.send = AsyncMock(return_value=sent_ann)
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_announce.callback(cog, inter, channel=main_ch, title="重要", content="第一行\\n第二行", color="gold"))
        check(
            main_ch.send.called and main_ch.send.call_args.kwargs["embed"].description == "第一行\n第二行",
            "manage_announce 成功發布公告並解析換行",
        )

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_announce.callback(cog, inter, channel=main_ch))
        modal = inter._state_log["modals"][-1]
        check(isinstance(modal, AnnounceModal), "manage_announce 留空標題與內文時開啟 AnnounceModal")
        modal.title_input._value = "彈窗公告"
        modal.content_input._value = "多行內容"
        main_ch.send.reset_mock()
        run_coro(modal.on_submit(make_fake_interaction(mod, guild, main_ch, bot)))
        check(main_ch.send.called and main_ch.send.call_args.kwargs["embed"].title == "彈窗公告", "AnnounceModal 送出後成功發送公告")

        pin_msg = MagicMock(spec=discord.Message)
        pin_msg.pinned = False
        pin_msg.jump_url = "https://discord.com/channels/1001/100/901"
        pin_msg.pin = AsyncMock()
        main_ch.fetch_message = AsyncMock(return_value=pin_msg)
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_pin.callback(cog, inter, action="pin", message_id="https://discord.com/channels/1001/100/901"))
        check(pin_msg.pin.called, "manage_pin 透過訊息連結成功固定訊息")

        # 5.8 role, nick, voice, member_info
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_role.callback(cog, inter, action="add", member=low_user, role=role_vip))
        check("已經擁有身分組" in inter._state_log["sent"][-1]["content"], "manage_role 已擁有身分組時友善提示")

        low_user.roles = [guild.default_role]
        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_role.callback(cog, inter, action="add", member=low_user, role=role_vip))
        check(low_user.add_roles.called, "manage_role 成功指派符合階層的身分組")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_nick.callback(cog, inter, member=low_user, nickname="新暱稱"))
        check(low_user.edit.call_args.kwargs["nick"] == "新暱稱", "manage_nick 成功修改暱稱")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_voice.callback(cog, inter, action="move", member=low_user, target_channel=vc2))
        check(low_user.move_to.called and low_user.move_to.call_args.args[0] == vc2, "manage_voice 成功移動語音成員")

        # 語音靜音冪等保護測試
        low_user.voice = SimpleNamespace(channel=vc1, mute=True, deaf=False)
        inter = make_fake_interaction(admin, guild, main_ch, bot)
        run_coro(cog.manage_voice.callback(cog, inter, action="mute", member=low_user))
        check("已經處於伺服器靜音狀態" in inter._state_log["sent"][-1]["content"], "manage_voice 已靜音成員給予冪等友善提示")

        inter = make_fake_interaction(mod, guild, main_ch, bot)
        run_coro(cog.manage_member_info.callback(cog, inter, member=low_user))
        info_embed = inter._state_log["sent"][-1]["embed"]
        check(info_embed is not None and "成員管理資訊" in info_embed.title, "manage_member_info 回傳成員管理資訊 Embed")

        # ======================================================================
        # 6. 指令樹、裝飾器、main.py、panel_bp 與文件同步檢查
        # ======================================================================
        print("\n--- [6] 系統整合與文件同步檢查 ---")
        expected_cmds = {
            "manage_kick",
            "manage_ban",
            "manage_unban",
            "manage_timeout",
            "manage_untimeout",
            "manage_warn",
            "manage_warnings",
            "manage_purge",
            "manage_slowmode",
            "manage_lock",
            "manage_unlock",
            "manage_channel_create",
            "manage_channel_edit",
            "manage_channel_delete",
            "manage_announce",
            "manage_pin",
            "manage_role",
            "manage_nick",
            "manage_voice",
            "manage_member_info",
        }
        cog_cmds = {cmd.name: cmd for cmd in cog.get_app_commands()}
        check(set(cog_cmds.keys()) == expected_cmds, f"Manage Cog 正確註冊全部 {len(expected_cmds)} 個 /manage_* 指令")
        check(
            all(cmd.guild_only and cmd.default_permissions is not None and len(cmd.checks) >= 1 for cmd in cog_cmds.values()),
            "所有 /manage_* 指令皆具備 guild_only、default_permissions 與執行期 checks",
        )
        check(
            all(len(cmd.description) <= 12 for cmd in cog_cmds.values())
            and all(len(p.description) <= 30 for cmd in cog_cmds.values() for p in cmd.parameters),
            "所有 /manage_* 指令與參數說明皆保持精簡（指令 <= 12 字、參數 <= 30 字）",
        )

        # 載入 cogs.manage 至 Bot 並以 AST 統計所有擴充模組的總全域斜線指令數（<= 100）
        import ast
        import importlib
        import main as main_mod
        check("cogs.manage" in main_mod.INITIAL_EXTENSIONS, "main.INITIAL_EXTENSIONS 包含 cogs.manage")

        test_bot = make_fake_bot()
        run_coro(test_bot.load_extension("cogs.manage"))
        loaded_cmds = test_bot.tree.get_commands()
        check(expected_cmds.issubset({c.name for c in loaded_cmds}), "cogs.manage 成功載入 Bot 指令樹")

        total_slash_names: set[str] = set()
        for ext in main_mod.INITIAL_EXTENSIONS:
            rel_path = os.path.join(PROJECT_ROOT, *ext.split(".")) + ".py"
            src = open(rel_path, encoding="utf-8").read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call) and getattr(dec.func, "attr", None) == "command":
                            for kw in dec.keywords:
                                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                                    total_slash_names.add(str(kw.value.value))
        check(len(total_slash_names) <= 100, f"全機器人斜線指令總數 ({len(total_slash_names)}) 符合 Discord 100 個上限")
        check(expected_cmds.issubset(total_slash_names), "全指令清點包含所有 /manage_* 指令")

        # 檢查 panel_bp 與文件同步
        panel_src = open(os.path.join(PROJECT_ROOT, "cogs", "panel_bp.py"), encoding="utf-8").read()
        readme_src = open(os.path.join(PROJECT_ROOT, "README.md"), encoding="utf-8").read()
        readme_en_src = open(os.path.join(PROJECT_ROOT, "README.en.md"), encoding="utf-8").read()
        docs_src = open(os.path.join(PROJECT_ROOT, "templates", "docs.html"), encoding="utf-8").read()

        check(all(f"/{name}" in panel_src for name in expected_cmds), "panel_bp.py 指令清單已包含全部 /manage_* 指令")
        check(all(f"/{name}" in readme_src for name in expected_cmds), "README.md 已同步記錄全部 /manage_* 指令")
        check(all(f"/{name}" in readme_en_src for name in expected_cmds), "README.en.md 已同步記錄全部 /manage_* 指令")
        check(all(f"/{name}" in docs_src for name in expected_cmds), "templates/docs.html 已同步記錄全部 /manage_* 指令")

    finally:
        db.DB_FILE = orig_db_file
        db._db_initialized = orig_db_init
        db._wal_initialized = orig_wal_init
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n共 {len(PASS) + len(FAIL)} 項測試：{len(PASS)} 通過，{len(FAIL)} 失敗")
    if FAIL:
        print("失敗項目：", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
