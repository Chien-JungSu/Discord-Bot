import os
import json
import re
import time
import asyncio
import tempfile
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# 重用 welcome cog 的共用工具：讀取 welcome_settings.json、頻道型別驗證與中文標籤
from cogs.welcome import (
    _load_welcome_settings,
    _extract_text_channel,
    _channel_type_label,
)

REACTION_ROLES_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'reaction_roles.json')
COOLDOWN_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'reaction_roles_cooldowns.json')
MAX_PAIRS = 20  # Discord 單一訊息最多 20 個表符反應
MAX_MESSAGES_PER_GUILD = 10  # 每個伺服器最多同時保留的表符身份組訊息數

# ---------- 反應事件限速（防高頻點擊／取消洗身份組） ----------
# 規則：對「同一個表符」，使用者最快每 REACTION_COOLDOWN_SECONDS 秒觸發一次
# 發放／收回動作；冷卻期間內的其他點擊一律靜默忽略（不做事、也不洗版）。
# 例如 5 秒內狂按 ⇄ 取消同一個表符，實際上每 5 秒才會生效一次。
REACTION_COOLDOWN_SECONDS = 5.0
# 冷卻狀態記錄上線：每個 (guild, user, emoji) 的最近動作時間戳。超過就丟棄，
# 避免檔案無限成長；到達上限時直接關閉新的限速記錄（事件照樣處理）。
COOLDOWN_TRACK_LIMIT = 100
# 冷卻狀態定期寫回磁碟的間隔（秒）：反應事件可能很頻繁，不能每次都寫檔。
COOLDOWN_FLUSH_INTERVAL = 60.0


def _load_reaction_roles() -> dict:
    """載入表符身份組設定，並把舊的「單則訊息」格式自動遷移成多則訊息格式。

    舊格式：{guild_id: {channel_id, message_id, pairs}}
    新格式：{guild_id: {messages: [{channel_id, message_id, pairs}]}}
    """
    if not os.path.exists(REACTION_ROLES_FILE):
        return {}
    try:
        with open(REACTION_ROLES_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"⚠️ 讀取表符身份組設定失敗，將以空設定啟動: {e}")
        return {}

    migrated = False
    data: dict = {}
    for guild_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        if 'messages' in entry and isinstance(entry['messages'], list):
            data[guild_id] = entry  # 已是新格式
            continue
        if 'message_id' in entry:  # 舊格式 → 包成單元素清單
            data[guild_id] = {
                'messages': [{
                    'channel_id': entry.get('channel_id'),
                    'message_id': entry.get('message_id'),
                    'pairs': entry.get('pairs') or [],
                }],
            }
            migrated = True
    if migrated:
        # 遷移結果立刻寫回，之後就固定走新格式
        try:
            _save_reaction_roles(data)
            print("✅ reaction_roles.json 已自動遷移為多則訊息格式。")
        except Exception as e:
            print(f"⚠️ 遷移後的表符身份組設定寫回失敗（下次儲存時會再嘗試）: {e}")
    return data


def _save_reaction_roles(data: dict):
    dir_path = os.path.dirname(REACTION_ROLES_FILE)
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=dir_path, delete=False, suffix='.tmp') as tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, REACTION_ROLES_FILE)
    except Exception as e:
        print(f"❌ 儲存表符身份組設定失敗: {e}")
        raise


def _load_cooldowns() -> dict:
    if os.path.exists(COOLDOWN_FILE):
        try:
            with open(COOLDOWN_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                return {str(k): float(v) for k, v in raw.items()}
        except (json.JSONDecodeError, IOError, ValueError, TypeError) as e:
            print(f"⚠️ 讀取表符身份組冷卻狀態失敗（以空狀態啟動）: {e}")
    return {}


def _save_cooldowns(data: dict):
    dir_path = os.path.dirname(COOLDOWN_FILE)
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=dir_path, delete=False, suffix='.tmp') as tmp:
            json.dump(data, tmp, ensure_ascii=False)
            tmp_path = tmp.name
        os.replace(tmp_path, COOLDOWN_FILE)
    except Exception as e:
        print(f"❌ 儲存表符身份組冷卻狀態失敗: {e}")


def _parse_emoji_token(token: str) -> Optional[discord.PartialEmoji]:
    """把單一 token 解析成表符（自訂表符或 Unicode 表符皆可），失敗回傳 None。

    純文字（含 ASCII 英文字母）不會被當成 Unicode 表符，避免把普通單字誤判為表符。
    """
    token = token.strip()
    if not token:
        return None
    try:
        emoji = discord.PartialEmoji.from_str(token)
    except Exception:
        return None
    if not emoji.name:
        return None
    if emoji.id is None and re.search(r'[A-Za-z]', emoji.name):
        return None
    return emoji


def _find_role(guild: discord.Guild, token: str) -> Optional[discord.Role]:
    """支援三種寫法找身份組：@提及（<@&ID>）、純數字 ID、名稱（不分大小寫）。"""
    token = token.strip()
    if not token:
        return None
    m = re.fullmatch(r'<@&(\d+)>', token)
    if m:
        return guild.get_role(int(m.group(1)))
    if token.isdigit():
        return guild.get_role(int(token))
    return discord.utils.find(lambda r: r.name.lower() == token.lower(), guild.roles)


def _parse_pairs(
    tokens: list[str],
    find_role,
) -> tuple[list[tuple[discord.PartialEmoji, discord.Role]], list[str]]:
    """把「表符 身份組 表符 身份組 …」的 token 序列自動拆成一對一配對。

    拆分規則：遇到可辨識的表符就開始一組，緊接的下一個 token 是身份組；
    身份組名稱本身不能再包含空白（含空白的名稱請改用 @提及或 ID）。
    回傳 (parsed, errors)，errors 為中文錯誤訊息；找到第一個無法解析的位置
    就停止，因為後續配對的切分點已經無法確定。
    """
    parsed: list[tuple[discord.PartialEmoji, discord.Role]] = []
    errors: list[str] = []
    seen_emojis: set[str] = set()
    i = 0
    idx = 0  # 目前解析到第幾組（供錯誤訊息顯示）
    while i < len(tokens):
        idx += 1
        if len(parsed) >= MAX_PAIRS:
            errors.append(f"• **表符配對**最多 {MAX_PAIRS} 組（Discord 單一訊息最多 20 個表符反應）")
            break
        emoji = _parse_emoji_token(tokens[i])
        if emoji is None:
            errors.append(f"• 第 {idx} 組 `{tokens[i]}`：無法辨識的表符，請直接輸入表符或自訂表符代碼（如 `<:name:123>`）")
            break
        emoji_key = str(emoji)
        if emoji_key in seen_emojis:
            errors.append(f"• 第 {idx} 組 `{tokens[i]}`：表符重複，每個表符只能對應一個身份組")
            break
        if i + 1 >= len(tokens):
            errors.append(f"• 第 {idx} 組：表符 `{tokens[i]}` 後面缺少對應的身份組（可用 @提及、ID 或名稱）")
            break
        role = find_role(tokens[i + 1])
        if role is None:
            errors.append(f"• 第 {idx} 組 `{tokens[i]}`：找不到身份組 `{tokens[i + 1]}`（可用 @提及、ID 或名稱）")
            break
        seen_emojis.add(emoji_key)
        parsed.append((emoji, role))
        i += 2
    return parsed, errors


class ReactionRoles(commands.Cog):
    """表符身份組：發送領取訊息並依成員的反應自動發放／收回身份組。

    每個伺服器可同時保留多則領取訊息（上限 MAX_MESSAGES_PER_GUILD），
    反應事件則依 (guild, user, emoji) 限速，防止高頻點擊／取消洗身份組；
    發送時預設開啟「僅限有效表符」，成員按了不對應身份組的表符會被自動移除。
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: dict = _load_reaction_roles()
        # 冷卻狀態：key = f"{guild_id}:{user_id}:{emoji_str}" → 最近一次動作的 monotonic 時間
        self._cooldowns: dict[str, float] = _load_cooldowns()
        self._cooldown_dirty = False
        self._cooldown_flush_task: asyncio.Task | None = None
        self._start_cooldown_flusher()

    def _start_cooldown_flusher(self):
        """背景任務：定期把變動過的冷卻狀態寫回磁碟（批次寫入，避免高頻 IO）。"""
        async def _flusher():
            try:
                while True:
                    await asyncio.sleep(COOLDOWN_FLUSH_INTERVAL)
                    self._flush_cooldowns()
            except asyncio.CancelledError:
                self._flush_cooldowns()  # 關閉前把變動過的狀態寫完
                raise

        self._cooldown_flush_task = asyncio.create_task(_flusher())

    def _flush_cooldowns(self):
        if not self._cooldown_dirty:
            return
        self._cooldown_dirty = False
        _save_cooldowns(self._cooldowns)

    async def cog_unload(self):
        if self._cooldown_flush_task:
            self._cooldown_flush_task.cancel()
            try:
                await self._cooldown_flush_task  # 等任務把狀態寫完
            except asyncio.CancelledError:
                pass
            self._cooldown_flush_task = None

    @app_commands.command(name="reaction_roles", description="發送「按表符領身份組」訊息（表符＋身份組一對一，可多組）")
    @app_commands.describe(
        message="要發送的訊息文字（建議說明每個表符對應的身份組）",
        pairs="表符＋身份組配對，以空白分隔：`表符 身份組 表符 身份組 …`，例如：`🎉 @帥 🎮 @打電動`（最多 20 組）",
        channel="要發送到的頻道（選填；未填時自動使用 /welcome_active 設定的身份組領取頻道）",
        strict="是否只保留有效表符（預設開啟）：成員按了不對應任何身份組的表符時，機器人會自動移除該反應（需機器人在該頻道有「管理訊息」權限）",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    async def reaction_roles(
        self,
        interaction: discord.Interaction,
        message: str,
        pairs: str,
        channel: Optional[discord.abc.GuildChannel] = None,
        strict: bool = True,
    ):
        if interaction.guild is None:  # guild_only 已擋，這裡只是型別上的保險
            return
        guild = interaction.guild

        # 回應可能需要一點時間（送訊息＋逐一加反應），先延遲回應（僅指令者可見）。
        await interaction.response.defer(ephemeral=True, thinking=True)

        errors: list[str] = []

        # ---------- 1. 驗證訊息文字 ----------
        if not message.strip():
            errors.append("• **訊息文字**不可空白")
        elif len(message) > 2000:
            errors.append(f"• **訊息文字**長度 {len(message)} 字，超過 Discord 的 2000 字上限")

        # ---------- 2. 解析表符＋身份組配對（以空白分隔自動拆分） ----------
        tokens = pairs.split()
        if not tokens:
            errors.append("• **表符配對**不可空白，格式：`表符 身份組 表符 身份組 …`，例如：`🎉 @帥 🎮 @打電動`")
        parsed, pair_errors = _parse_pairs(tokens, lambda token: _find_role(guild, token))
        errors.extend(pair_errors)

        # ---------- 3. 檢查機器人能不能發放這些身份組 ----------
        me = guild.me
        assignable: list[tuple[discord.PartialEmoji, discord.Role]] = []
        for emoji, role in parsed:
            emoji_key = str(emoji)
            problems: list[str] = []
            if role.is_default():
                problems.append("@everyone 不能用表符發放")
            elif role.managed:
                problems.append("此身份組由整合服務管理，無法手動發放")
            elif me is not None and me.top_role <= role:
                problems.append(f"機器人的最高身份組（{me.top_role.name}）沒有高於它")
            if problems:
                errors.append(f"• {emoji_key} → {role.mention}：{'；'.join(problems)}")
            else:
                assignable.append((emoji, role))

        # ---------- 4. 決定目標頻道（參數優先，其次 welcome 設定的身份組領取頻道） ----------
        target_channel: Optional[discord.TextChannel] = None
        if channel is not None:
            tc = _extract_text_channel(channel)
            if tc is None:
                errors.append(f"• **指定頻道** 是{_channel_type_label(channel)} {channel.mention}，請改選**文字頻道**")
            else:
                target_channel = tc
        else:
            welcome_settings = _load_welcome_settings().get(str(guild.id)) or {}
            role_channel_id = welcome_settings.get('role_channel_id')
            if role_channel_id:
                ch = guild.get_channel(int(role_channel_id))
                if isinstance(ch, discord.TextChannel):
                    target_channel = ch
                else:
                    # welcome 設定了身份組頻道，但該頻道已不存在（被刪除等）
                    errors.append(
                        "• 未指定 `channel`，且 `/welcome_active` 設定的「身份組領取頻道」已不存在（可能已被刪除）；"
                        "請重新執行 `/welcome_active` 設定，或在這個指令中直接指定頻道"
                    )
            else:
                errors.append(
                    "• 未指定 `channel`，且 `/welcome_active` 尚未設定「身份組領取頻道」；"
                    "請先用 `/welcome_active` 設定，或在這個指令中直接指定頻道"
                )

        # ---------- 5. 有任何錯誤就一次列出，不發送 ----------
        if errors:
            embed = discord.Embed(
                title="❌ 無法發送身份組領取訊息",
                description="請修正以下問題後再試一次：\n\n" + "\n".join(errors),
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # ---------- 6. 檢查訊息數量上限 ----------
        guild_entry = self.settings.setdefault(str(guild.id), {'messages': []})
        messages = guild_entry.setdefault('messages', [])
        if len(messages) >= MAX_MESSAGES_PER_GUILD:
            embed = discord.Embed(
                title="❌ 表符身份組訊息已達上限",
                description=(
                    f"此伺服器已有 **{len(messages)}** 則表符身份組訊息（上限 {MAX_MESSAGES_PER_GUILD}）。"
                    "\n請先刪除舊的領取訊息後再發送新的。"
                ),
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # ---------- 7. 檢查機器人在目標頻道的權限 ----------
        assert target_channel is not None
        perms = target_channel.permissions_for(me) if me else None
        missing_perms: list[str] = []
        if perms is not None:
            if not perms.view_channel:
                missing_perms.append("檢視頻道")
            if not perms.send_messages:
                missing_perms.append("發送訊息")
            if not perms.add_reactions:
                missing_perms.append("新增反應")
            if strict and not perms.manage_messages:
                missing_perms.append("管理訊息（僅限有效表符模式需要，用來移除無效反應）")
        if missing_perms:
            embed = discord.Embed(
                title="❌ 機器人權限不足",
                description=f"機器人在 {target_channel.mention} 缺少權限：**{'、'.join(missing_perms)}**。\n請調整頻道權限後再試。",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # ---------- 8. 發送訊息 ----------
        try:
            sent = await target_channel.send(content=message)
        except discord.Forbidden:
            embed = discord.Embed(
                title="❌ 發送失敗",
                description=f"機器人沒有在 {target_channel.mention} 發言的權限。",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        except Exception as e:
            print(f"❌ reaction_roles：發送訊息失敗: {e}")
            traceback.print_exc()
            await self.bot.notify_owner_error(e, interaction, extra_info="reaction_roles: 發送訊息失敗")
            await interaction.followup.send("❌ 發送訊息時發生未知錯誤，已回報開發者。", ephemeral=True)
            return

        # ---------- 9. 機器人自己先把所有指定的表符按一輪 ----------
        saved_pairs: list[dict] = []
        failed_emojis: list[str] = []
        for emoji, role in assignable:
            try:
                await sent.add_reaction(str(emoji))
                saved_pairs.append({'emoji': str(emoji), 'role_id': role.id})
            except discord.HTTPException as e:
                # 表符不存在（例如從其他伺服器複製但機器人不在該伺服器）等情況會走到這裡
                print(f"❌ reaction_roles：按下表符 {emoji} 失敗: {e}")
                failed_emojis.append(str(emoji))

        if not saved_pairs:
            embed = discord.Embed(
                title="❌ 沒有任何表符成功按下",
                description=(
                    f"訊息已發送到 {target_channel.mention}（{sent.jump_url}），但所有表符都無法按下，"
                    "因此不會啟用身份組發放。請確認自訂表符來自本伺服器（或機器人所在的伺服器）。"
                ),
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # ---------- 10. 儲存設定（每個伺服器可保留多則訊息） ----------
        messages.append({
            'channel_id': target_channel.id,
            'message_id': sent.id,
            'pairs': saved_pairs,
            # 防止亂按：True = 移除不對應任何身份組的表符反應（舊設定沒有此欄位時視為開啟）
            'strict': bool(strict),
        })
        try:
            _save_reaction_roles(self.settings)
        except Exception as e:
            # 回滾記憶體狀態，避免「檔案儲存失敗但記憶體卻多了紀錄」的不一致
            messages.pop()
            await self.bot.notify_owner_error(e, interaction, extra_info="reaction_roles: 儲存設定失敗")
            await interaction.followup.send("❌ 訊息已發送，但儲存設定時發生錯誤（此訊息不會發放身份組），已回報開發者。", ephemeral=True)
            return

        # ---------- 11. 回報結果（僅指令者可見） ----------
        pair_lines = [f"{p['emoji']} → <@&{p['role_id']}>" for p in saved_pairs]
        desc = (
            f"📢 頻道：{target_channel.mention}\n"
            f"🔗 [前往訊息]({sent.jump_url})\n\n"
            + "\n".join(pair_lines)
        )
        embed = discord.Embed(
            title="✅ 身份組領取訊息已發送",
            description=desc,
            color=discord.Color.green(),
        )
        embed.set_footer(
            text=f"成員按下表符可獲得身份組，取消表符則自動收回。（{len(messages)}/{MAX_MESSAGES_PER_GUILD} 則）"
        )
        if strict:
            embed.add_field(
                name="🛡️ 僅限有效表符",
                value="成員按下不對應身份組的其他表符時，機器人會自動移除該反應（需「管理訊息」權限）。",
                inline=False,
            )
        if failed_emojis:
            embed.add_field(
                name="⚠️ 部分表符按下失敗",
                value="、".join(failed_emojis) + "\n這幾個表符不會發放身份組。",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    def _check_cooldown(self, guild_id: int, user_id: int, emoji_str: str) -> bool:
        """限速檢查：冷卻中回傳 True（應忽略這次事件）。

        以 (guild, user, emoji) 為單位記錄最近一次生效動作的時間；同一個人在
        冷卻時間內重複按／取消**同一個表符**不會再次觸發身份組變動。
        記錄數超過 COOLDOWN_TRACK_LIMIT 時，先清掉已過期的項目再嘗試；仍然
        滿了就放行（不記錄新的限速狀態），寧可漏限速也不要擋正常使用者。
        """
        now = time.monotonic()
        key = f"{guild_id}:{user_id}:{emoji_str}"
        last = self._cooldowns.get(key)
        if last is not None and now - last < REACTION_COOLDOWN_SECONDS:
            return True
        if len(self._cooldowns) >= COOLDOWN_TRACK_LIMIT and key not in self._cooldowns:
            # 只清「已過期」的記錄（正常情況下很夠用），避免清掉還在冷卻的
            expired = [k for k, ts in self._cooldowns.items() if now - ts >= REACTION_COOLDOWN_SECONDS]
            for k in expired:
                del self._cooldowns[k]
            if len(self._cooldowns) >= COOLDOWN_TRACK_LIMIT:
                return False
        self._cooldowns[key] = now
        self._cooldown_dirty = True
        return False

    def _lookup_mapping(self, payload: discord.RawReactionActionEvent) -> Optional[dict]:
        """在這個伺服器的所有領取訊息中，找出符合頻道＋訊息 ID 的那則。"""
        guild_entry = self.settings.get(str(payload.guild_id))
        if not guild_entry:
            return None
        for msg_entry in guild_entry.get('messages') or []:
            if payload.message_id == msg_entry.get('message_id') and payload.channel_id == msg_entry.get('channel_id'):
                return msg_entry
        return None

    async def _handle_role_reaction(self, payload: discord.RawReactionActionEvent, *, granting: bool):
        """raw reaction 事件共用處理：查表後發放／收回身份組（含限速）。"""
        if payload.guild_id is None:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return  # 機器人自己按下／收回的表符不處理

        mapping = self._lookup_mapping(payload)
        if mapping is None:
            return

        emoji_str = str(payload.emoji)
        role_id = next(
            (p.get('role_id') for p in mapping.get('pairs') or [] if p.get('emoji') == emoji_str),
            None,
        )
        if role_id is None:
            # 防止亂按：這則訊息設為「僅限有效表符」時，把不對應身份組的反應移除掉
            if granting and mapping.get('strict', True):
                await self._remove_invalid_reaction(payload)
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(int(role_id))
        if role is None:
            return  # 身份組已被刪除，靜默略過

        member = payload.member  # on_raw_reaction_add 才會附帶 member
        if member is None or not isinstance(member, discord.Member):
            member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except (discord.NotFound, discord.HTTPException):
                return  # 使用者已離開伺服器等情況
        if member.bot:
            return

        # ---------- 限速：高頻點擊／取消同一個表符的冷卻 ----------
        if self._check_cooldown(payload.guild_id, payload.user_id, emoji_str):
            return

        action = '發放' if granting else '收回'
        try:
            if granting:
                await member.add_roles(role, reason=f"reaction_roles：按下表符領取 {role.name}")
            else:
                await member.remove_roles(role, reason=f"reaction_roles：取消表符收回 {role.name}")
        except discord.Forbidden:
            print(f"❌ reaction_roles：機器人無法{action}身份組 {role.name} 給 {member}（權限或階層不足）。")
            await self.bot.notify_owner_error(
                discord.Forbidden(
                    discord.http.Route('PATCH', '/guilds/{guild_id}/members/{user_id}', guild_id=guild.id, user_id=member.id),
                    f'無法{action}身份組 {role.name}',
                ),
                extra_info=f"reaction_roles: guild={guild.name} ({guild.id}) role={role.name} member={member}",
            )
        except discord.HTTPException as e:
            print(f"❌ reaction_roles：{action}身份組 {role.name} 時發生錯誤: {e}")

    async def _remove_invalid_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        """移除「不對應任何身份組」的表符反應（防止亂按領取訊息）。

        機器人要移除別人的反應，必須在該頻道擁有 manage_messages 權限；沒有
        權限或訊息已不存在時就靜默略過（只在主控台留下紀錄），不會打擾使用者。
        """
        emoji_str = str(payload.emoji)
        # 連續狂按同一個無效表符時只移除一次，避免反應被拿來洗 API 請求
        if self._check_cooldown(payload.guild_id, payload.user_id, f'invalid:{emoji_str}'):
            return
        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return  # 快取中沒有這個頻道（機器人可能已看不到它）
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return  # 訊息已被刪除
        except discord.HTTPException as e:
            print(f"❌ reaction_roles：讀取訊息 {payload.message_id} 以移除無效表符失敗: {e}")
            return
        try:
            # 只需要 user id，不必另外把成員物件抓出來
            await message.remove_reaction(payload.emoji, discord.Object(payload.user_id))
        except discord.NotFound:
            return  # 使用者已自行移除，或該反應已不存在
        except discord.Forbidden:
            print(
                f"❌ reaction_roles：無法移除無效表符 {emoji_str}，"
                f"機器人在頻道 {payload.channel_id} 缺少「管理訊息」權限。"
            )
        except discord.HTTPException as e:
            print(f"❌ reaction_roles：移除無效表符 {emoji_str} 失敗: {e}")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_role_reaction(payload, granting=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle_role_reaction(payload, granting=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
