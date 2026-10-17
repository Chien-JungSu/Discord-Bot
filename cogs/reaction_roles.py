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


from cogs.database import (
    load_reaction_roles as _load_reaction_roles,
    save_reaction_roles as _save_reaction_roles,
    load_reaction_cooldowns as _load_cooldowns,
    save_reaction_cooldowns as _save_cooldowns,
)


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


def _validate_message_text(message: str, errors: list[str]):
    """驗證領取訊息文字：不可空白、不得超過 Discord 的 2000 字上限；問題訊息附加到 errors。"""
    if not message.strip():
        errors.append("• **訊息文字**不可空白")
    elif len(message) > 2000:
        errors.append(f"• **訊息文字**長度 {len(message)} 字，超過 Discord 的 2000 字上限")


# 編輯視窗的按鈕閒置逾時（秒）：與 cogs.auto_reply 相同慣例，每次操作都會重新計時
BUTTON_IDLE_TIMEOUT = 120.0
# 「到訊息上按表符」：等待管理員在領取訊息上按下表符的時間（秒）
EMOJI_CAPTURE_TIMEOUT = 60.0
# 抓取結束後，仍忽略該管理員在領取訊息上的反應事件幾秒：避免抓取用的反應
# （以及機器人隨後收回它所觸發的 remove 事件）被當成一般領取／收回身份組
EMOJI_CAPTURE_GRACE = 5.0


class ReactionRoles(commands.Cog):
    """表符身份組：發送／修改領取訊息，並依成員的反應自動發放／收回身份組。

    提供 /reaction_roles_create（建立）與 /reaction_roles_edit（修改）兩個指令；
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
        # 表符抓取中：(message_id, user_id) → 忽略反應事件直到這個 monotonic 時間
        self._emoji_capture_ignore: dict[tuple[int, int], float] = {}
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

    @app_commands.command(name="reaction_roles_create", description="發送「按表符領身份組」訊息（表符＋身份組一對一，可多組）")
    @app_commands.describe(
        message="要發送的訊息文字（建議說明每個表符對應的身份組）",
        pairs="表符＋身份組配對，以空白分隔：`表符 身份組 表符 身份組 …`，例如：`🎉 @帥 🎮 @打電動`（最多 20 組）",
        channel="要發送到的頻道（選填；未填時自動使用 /welcome_active 設定的身份組領取頻道）",
        strict="是否只保留有效表符（預設開啟）：成員按了不對應任何身份組的表符時，機器人會自動移除該反應（需機器人在該頻道有「管理訊息」權限）",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    async def reaction_roles_create(
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
        _validate_message_text(message, errors)

        # ---------- 2. 解析表符＋身份組配對（以空白分隔自動拆分） ----------
        tokens = pairs.split()
        if not tokens:
            errors.append("• **表符配對**不可空白，格式：`表符 身份組 表符 身份組 …`，例如：`🎉 @帥 🎮 @打電動`")
        parsed, pair_errors = _parse_pairs(tokens, lambda token: _find_role(guild, token))
        errors.extend(pair_errors)

        # ---------- 3. 檢查機器人能不能發放這些身份組 ----------
        assignable = self._check_assignable(guild, parsed, errors)

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

        # ---------- 7. 檢查權限、發送訊息、按下表符、儲存設定（與 edit 指令共用流程） ----------
        assert target_channel is not None
        result = await self._apply_reaction_roles(
            interaction,
            target_channel,
            assignable,
            message,
            strict,
        )
        if result is None:
            return

        # ---------- 8. 回報結果（僅指令者可見） ----------
        pair_lines = [f"{p['emoji']} → <@&{p['role_id']}>" for p in result['saved_pairs']]
        desc = (
            f"📢 頻道：{target_channel.mention}\n"
            f"🔗 [前往訊息]({result['message'].jump_url})\n\n"
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
        if result['failed_emojis']:
            embed.add_field(
                name="⚠️ 部分表符按下失敗",
                value="、".join(result['failed_emojis']) + "\n這幾個表符不會發放身份組。",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- /reaction_roles_edit 的共用邏輯（互動 UI 在檔案末尾） ----------

    @staticmethod
    def _check_channel_perms(
        channel: discord.TextChannel,
        me,
        *,
        strict: bool,
        need_send: bool,
    ) -> list[str]:
        """檢查機器人在頻道的權限，回傳缺少的權限名稱（空清單＝權限足夠）。"""
        perms = channel.permissions_for(me) if me else None
        if perms is None:
            return []
        missing: list[str] = []
        if not perms.view_channel:
            missing.append("檢視頻道")
        if need_send and not perms.send_messages:
            missing.append("發送訊息")
        if not perms.add_reactions:
            missing.append("新增反應")
        if strict and not perms.manage_messages:
            missing.append("管理訊息（僅限有效表符模式需要，用來移除無效反應）")
        return missing

    async def _fetch_editable_message(
        self,
        guild: discord.Guild,
        mapping: dict,
    ) -> tuple[Optional[discord.TextChannel], Optional[discord.Message], list[str]]:
        """抓取要編輯的頻道與原訊息。回傳 (頻道, 訊息, 錯誤清單)；失敗時前兩項為 None。"""
        channel_id = int(mapping.get('channel_id') or 0)
        message_id = int(mapping.get('message_id') or 0)
        target_channel = guild.get_channel(channel_id)
        if not isinstance(target_channel, discord.TextChannel):
            try:
                fetched = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                fetched = None
            target_channel = fetched if isinstance(fetched, discord.TextChannel) else None
        if target_channel is None:
            return None, None, ['找不到原訊息所在的頻道（可能已被刪除，或機器人看不到它）。']
        try:
            message = await target_channel.fetch_message(message_id)
        except discord.NotFound:
            return None, None, ['找不到原訊息（可能已被刪除）。']
        except discord.Forbidden:
            return None, None, [f'機器人在 {target_channel.mention} 缺少「檢視頻道」或「讀取訊息歷史」權限。']
        except discord.HTTPException as e:
            print(f'❌ reaction_roles_edit：讀取原訊息 {message_id} 失敗: {e}')
            return None, None, ['讀取原訊息時發生未知錯誤，請稍後再試。']
        return target_channel, message, []

    async def _edit_message_text(self, message: discord.Message, new_text: str) -> list[str]:
        """修改訊息文字。回傳錯誤清單（空清單＝成功）。"""
        try:
            await message.edit(content=new_text)
        except discord.Forbidden:
            return ['機器人沒有修改這則訊息的權限。']
        except discord.NotFound:
            return ['找不到原訊息（可能已被刪除）。']
        except discord.HTTPException as e:
            print(f'❌ reaction_roles_edit：修改訊息文字失敗: {e}')
            return ['修改訊息文字時發生未知錯誤，請稍後再試。']
        return []

    async def _press_emojis(
        self,
        message: discord.Message,
        assignable: list[tuple[discord.PartialEmoji, discord.Role]],
        skip: frozenset[str] | set[str] = frozenset(),
    ) -> tuple[list[dict], list[str]]:
        """機器人把所有指定的表符按一輪（已在訊息上的表符再按一次不會出錯，順便修補被手動刪掉的反應）。

        skip：機器人已經按在訊息上的表符字串，這些直接視為成功、不再重按（減少 API 請求）。
        回傳 (saved_pairs, failed_emojis)。
        """
        saved_pairs: list[dict] = []
        failed_emojis: list[str] = []
        for emoji, role in assignable:
            if str(emoji) in skip:
                saved_pairs.append({'emoji': str(emoji), 'role_id': role.id})
                continue
            try:
                await message.add_reaction(str(emoji))
                saved_pairs.append({'emoji': str(emoji), 'role_id': role.id})
            except discord.HTTPException as e:
                # 表符不存在（例如從其他伺服器複製但機器人不在該伺服器）等情況會走到這裡
                print(f'❌ reaction_roles：按下表符 {emoji} 失敗: {e}')
                failed_emojis.append(str(emoji))
        return saved_pairs, failed_emojis

    async def _sync_message_pairs(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        channel: discord.TextChannel,
        mapping: dict,
        assignable: list[tuple[discord.PartialEmoji, discord.Role]],
        *,
        existing: discord.Message,
    ) -> tuple[Optional[dict], list[str]]:
        """把訊息上的表符反應同步成新的配對，並更新設定（strict 不在此更動）。

        成功時回傳 ({'saved_pairs': [...], 'failed_adds': [...], 'failed_removes': [...]}, [])；
        失敗時回傳 (None, 錯誤清單)，呼叫端負責回覆使用者。
        """
        me = guild.me
        strict = bool(mapping.get('strict', True))
        missing_perms = self._check_channel_perms(channel, me, strict=strict, need_send=False)
        if missing_perms:
            return None, [f'機器人在 {channel.mention} 缺少權限：**{"、".join(missing_perms)}**。']

        old_pairs: list[dict] = list(mapping.get('pairs') or [])
        # 清掉「新配對不再使用」的舊表符（連同成員對它的反應）；仍在使用的表符保留原樣
        failed_removes: list[str] = []
        new_emoji_keys = {str(emoji) for emoji, _role in assignable}
        # 機器人已經按著、且新配對仍會使用的表符：不必重按
        already_pressed = {str(r.emoji) for r in existing.reactions if r.me and str(r.emoji) in new_emoji_keys}
        for reaction in list(existing.reactions):
            if reaction.me and str(reaction.emoji) not in new_emoji_keys:
                try:
                    await reaction.clear()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    print(f'❌ reaction_roles：清除不再使用的舊表符 {reaction.emoji} 失敗: {e}')
                    failed_removes.append(str(reaction.emoji))

        saved_pairs, failed_adds = await self._press_emojis(existing, assignable, skip=already_pressed)
        if not saved_pairs:
            # 一個表符都沒按上去：把舊配對的表符盡量按回去，讓訊息反應和未變更的舊設定一致
            for pair in old_pairs:
                try:
                    await existing.add_reaction(pair.get('emoji'))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    print(f'❌ reaction_roles：還原舊表符 {pair.get("emoji")} 失敗: {e}')
            return None, ['所有表符都無法按下，請確認自訂表符來自本伺服器（或機器人所在的伺服器）。']

        # 更新設定並儲存（失敗時回滾記憶體狀態，並把舊表符盡量按回去）
        snapshot: list[dict] = list(old_pairs)
        mapping['pairs'] = saved_pairs
        try:
            _save_reaction_roles(self.settings)
        except Exception as e:
            mapping['pairs'] = snapshot
            for pair in snapshot:
                try:
                    await existing.add_reaction(pair.get('emoji'))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e2:
                    print(f'❌ reaction_roles：還原舊表符 {pair.get("emoji")} 失敗: {e2}')
            traceback.print_exc()
            await self.bot.notify_owner_error(e, interaction, extra_info='reaction_roles_edit: 儲存設定失敗')
            return None, ['訊息已修改，但儲存設定時發生錯誤，已回滾變更；已回報開發者。']
        return {'saved_pairs': saved_pairs, 'failed_adds': failed_adds, 'failed_removes': failed_removes}, []

    async def _toggle_message_strict(self, interaction: discord.Interaction, mapping: dict) -> bool:
        """切換這則訊息的「僅限有效表符」開關並儲存；失敗時回滾、回報開發者並回傳 False。"""
        old = mapping.get('strict')
        mapping['strict'] = not bool(mapping.get('strict', True))
        try:
            _save_reaction_roles(self.settings)
        except Exception as e:
            if old is None:
                mapping.pop('strict', None)
            else:
                mapping['strict'] = old
            traceback.print_exc()
            await self.bot.notify_owner_error(e, interaction, extra_info='reaction_roles_edit: 儲存設定失敗')
            return False
        return True

    def _pairs_prefill(self, guild: discord.Guild, mapping: dict) -> str:
        """把目前的配對組成可重新解析的輸入框預填文字：`表符 身份組 表符 身份組 …`。"""
        parts: list[str] = []
        for pair in mapping.get('pairs') or []:
            role = guild.get_role(int(pair.get('role_id') or 0))
            if role is not None and ' ' not in role.name and not role.name.isdigit() and not role.is_default():
                role_token = role.name  # 沒有空白的名稱可以直接解析
            elif role is not None:
                role_token = role.mention  # <@&ID> 提及代碼（名稱含空白等情況用）
            else:
                role_token = str(pair.get('role_id'))  # 身份組已被刪除，退回純數字 ID
            parts.append(f"{pair.get('emoji')} {role_token}")
        return ' '.join(parts)

    def _pairs_option_text(self, guild: discord.Guild, mapping: dict) -> str:
        """選單選項用的配對摘要，例如：`🎉→帥、🎮→打電動…等 5 組`。"""
        pairs = mapping.get('pairs') or []
        parts: list[str] = []
        for pair in pairs[:3]:
            role = guild.get_role(int(pair.get('role_id') or 0))
            role_name = role.name if role is not None else f"ID {pair.get('role_id')}"
            parts.append(f"{pair.get('emoji')}→{role_name}")
        text = '、'.join(parts)
        if len(pairs) > 3:
            text += f'…等 {len(pairs)} 組'
        return text or '（尚無配對）'

    def _build_edit_embed(self, guild: discord.Guild, mapping: dict, status: Optional[str] = None) -> discord.Embed:
        """編輯視窗的 embed：頻道、前往訊息連結、目前的配對與 strict 狀態（可附最新狀態）。"""
        channel_id = int(mapping.get('channel_id') or 0)
        message_id = int(mapping.get('message_id') or 0)
        channel = guild.get_channel(channel_id)
        channel_text = channel.mention if channel is not None else f'（ID {channel_id}）'
        jump_url = f'https://discord.com/channels/{guild.id}/{channel_id}/{message_id}'
        lines = [
            f'📢 頻道：{channel_text}',
            f'🔗 [前往訊息]({jump_url})',
            '',
            '🧩 目前的表符配對：',
        ]
        pairs = mapping.get('pairs') or []
        if pairs:
            for pair in pairs:
                role = guild.get_role(int(pair.get('role_id') or 0))
                role_text = role.mention if role is not None else f"<@&{pair.get('role_id')}>"
                lines.append(f"{pair.get('emoji')} → {role_text}")
        else:
            lines.append('（沒有任何配對）')
        embed = discord.Embed(title='✏️ 編輯表符身份組訊息', description='\n'.join(lines), color=discord.Color.blurple())
        embed.add_field(
            name='🛡️ 僅限有效表符',
            value='開啟' if bool(mapping.get('strict', True)) else '關閉',
            inline=False,
        )
        if status:
            embed.add_field(name='ℹ️ 最新狀態', value=status[:1024], inline=False)
        return embed

    def reload_settings(self):
        """從資料庫重新載入表符身份組設定（供網頁控制面板變更後同步記憶體狀態）。"""
        self.settings = _load_reaction_roles()

    async def _delete_mapping(
        self,
        guild_key: str,
        message_id: int,
        interaction: Optional[discord.Interaction] = None,
    ) -> bool:
        """從伺服器設定中移除指定的領取訊息紀錄並寫入資料庫；失敗時回滾記憶體狀態並回傳 False。"""
        guild_entry = self.settings.get(str(guild_key))
        if not guild_entry:
            return False
        messages = guild_entry.get('messages') or []
        removed_items = []
        new_messages = []
        for idx, msg_entry in enumerate(messages):
            if int(msg_entry.get('message_id') or 0) == int(message_id):
                removed_items.append((idx, msg_entry))
            else:
                new_messages.append(msg_entry)
        if not removed_items:
            return False

        old_messages = list(messages)
        if new_messages:
            guild_entry['messages'] = new_messages
        else:
            self.settings.pop(str(guild_key), None)

        try:
            _save_reaction_roles(self.settings)
        except Exception as e:
            # 回滾記憶體狀態
            self.settings[str(guild_key)] = {'messages': old_messages}
            traceback.print_exc()
            if interaction is not None and hasattr(self.bot, 'notify_owner_error'):
                await self.bot.notify_owner_error(e, interaction, extra_info='reaction_roles_edit: 刪除領取訊息設定失敗')
            return False
        return True

    # ---------- /reaction_roles_edit：選單＋按鈕＋輸入框的互動式編輯 ----------
    @app_commands.command(name='reaction_roles_edit', description='編輯或刪除已發送的表符身份組訊息：修改文字／表符配對、切換僅限有效表符，或刪除領取訊息')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    async def reaction_roles_edit(self, interaction: discord.Interaction):
        if interaction.guild is None:  # guild_only 已擋，這裡只是型別上的保險
            return
        guild = interaction.guild

        # 先延遲回應（僅指令者可見），避免伺服器或網路延遲導致超過 Discord 3 秒限制 (10062 Unknown interaction)
        await interaction.response.defer(ephemeral=True)

        guild_entry = self.settings.get(str(guild.id)) or {}
        messages = guild_entry.get('messages') or []
        if not messages:
            embed = discord.Embed(
                title='ℹ️ 目前沒有表符身份組訊息',
                description='此伺服器尚未發送任何表符身份組訊息，先用 `/reaction_roles_create` 發送一則吧！',
                color=discord.Color.light_grey(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        view = ReactionRolesEditView(self, guild, messages)
        view.initiator_id = interaction.user.id
        embed = discord.Embed(
            title='✏️ 編輯表符身份組訊息',
            description=(
                f'此伺服器目前有 **{len(messages)}/{MAX_MESSAGES_PER_GUILD}** 則表符身份組訊息。\n'
                '請從下方選單選擇要編輯的訊息（選項顯示該訊息的表符配對摘要），選取後：\n'
                '• 按「**編輯訊息文字**」開啟輸入框，欄位已預先帶入現有內容，直接修改後送出\n'
                '• 按「**編輯表符配對**」進入配對編輯器：用**身份組選單**直接搜尋身份組、'
                '到**領取訊息上按表符**即可選表符，不必再複製身份組 ID 或表符代碼\n'
                '• 按「**🛡️ 僅限有效表符**」一下即可切換開／關\n'
                '• 按「**🗑️ 刪除領取訊息**」可在確認後刪除該則領取訊息與配對設定\n'
                '• 所有操作都在同一則訊息上進行（連續編輯不會洗版）；連續 120 秒沒有操作按鈕會自動失效，再執行一次指令即可'
            ),
            color=discord.Color.blurple(),
        )
        msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = msg

    def _check_assignable(
        self,
        guild: discord.Guild,
        parsed: list[tuple[discord.PartialEmoji, discord.Role]],
        errors: list[str],
    ) -> list[tuple[discord.PartialEmoji, discord.Role]]:
        """檢查機器人能否發放這些身份組：@everyone、受整合管理、階層不足都會剔除並記錄錯誤。"""
        assignable: list[tuple[discord.PartialEmoji, discord.Role]] = []
        for emoji, role in parsed:
            emoji_key = str(emoji)
            problems = self._role_problems(guild, role)
            if problems:
                errors.append(f"• {emoji_key} → {role.mention}：{'；'.join(problems)}")
            else:
                assignable.append((emoji, role))
        return assignable

    @staticmethod
    def _role_problems(guild: discord.Guild, role: discord.Role) -> list[str]:
        """單一身份組能否由機器人發放；回傳問題描述（空清單＝可以發放）。"""
        me = guild.me
        problems: list[str] = []
        if role.is_default():
            problems.append("@everyone 不能用表符發放")
        elif role.managed:
            problems.append("此身份組由整合服務管理，無法手動發放")
        elif me is not None and me.top_role <= role:
            problems.append(f"機器人的最高身份組（{me.top_role.name}）沒有高於它")
        return problems

    async def _apply_reaction_roles(
        self,
        interaction: discord.Interaction,
        target_channel: discord.TextChannel,
        assignable: list[tuple[discord.PartialEmoji, discord.Role]],
        message_text: str,
        strict: bool,
    ) -> Optional[dict]:
        """建立流程：檢查頻道權限 → 發送訊息 → 按下表符 → 儲存設定。

        成功時回傳 {'message': 訊息, 'saved_pairs': [...], 'failed_emojis': [...]}；
        失敗時已向指令者回報錯誤並回傳 None。
        """
        guild = interaction.guild
        assert guild is not None
        me = guild.me

        # ---------- 檢查機器人在目標頻道的權限 ----------
        missing_perms = self._check_channel_perms(target_channel, me, strict=strict, need_send=True)
        if missing_perms:
            embed = discord.Embed(
                title="❌ 機器人權限不足",
                description=f"機器人在 {target_channel.mention} 缺少權限：**{'、'.join(missing_perms)}**。\n請調整頻道權限後再試。",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return None

        # ---------- 發送訊息 ----------
        try:
            sent = await target_channel.send(content=message_text)
        except discord.Forbidden:
            embed = discord.Embed(
                title="❌ 發送失敗",
                description=f"機器人沒有在 {target_channel.mention} 發言的權限。",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return None
        except Exception as e:
            print(f"❌ reaction_roles：發送訊息失敗: {e}")
            traceback.print_exc()
            await self.bot.notify_owner_error(e, interaction, extra_info="reaction_roles: 發送訊息失敗")
            await interaction.followup.send("❌ 發送訊息時發生未知錯誤，已回報開發者。", ephemeral=True)
            return None

        # ---------- 機器人自己把所有指定的表符按一輪 ----------
        saved_pairs, failed_emojis = await self._press_emojis(sent, assignable)
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
            return None

        # ---------- 儲存設定（每個伺服器可保留多則訊息；失敗時回滾記憶體狀態） ----------
        guild_entry = self.settings.setdefault(str(guild.id), {'messages': []})
        messages = guild_entry.setdefault('messages', [])
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
            await interaction.followup.send(
                "❌ 訊息已發送，但儲存設定時發生錯誤（此訊息不會發放身份組），已回報開發者。",
                ephemeral=True,
            )
            return None

        return {'message': sent, 'saved_pairs': saved_pairs, 'failed_emojis': failed_emojis}

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

    def _find_mapping_by_ids(self, guild_key: str, channel_id: Optional[int], message_id: int) -> Optional[dict]:
        """以訊息 ID（可選頻道 ID）在伺服器設定中找出對應的領取訊息紀錄。"""
        guild_entry = self.settings.get(guild_key)
        if not guild_entry:
            return None
        for msg_entry in guild_entry.get('messages') or []:
            if msg_entry.get('message_id') != message_id:
                continue
            if channel_id is not None and msg_entry.get('channel_id') != channel_id:
                continue
            return msg_entry
        return None

    async def _handle_role_reaction(self, payload: discord.RawReactionActionEvent, *, granting: bool):
        """raw reaction 事件共用處理：查表後發放／收回身份組（含限速）。"""
        if payload.guild_id is None:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return  # 機器人自己按下／收回的表符不處理

        # 管理員正在用「到訊息上按表符」挑選表符：這段期間的反應只用來抓表符
        ignore_until = self._emoji_capture_ignore.get((payload.message_id, payload.user_id))
        if ignore_until is not None:
            if time.monotonic() < ignore_until:
                return
            self._emoji_capture_ignore.pop((payload.message_id, payload.user_id), None)

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


# ---------- /reaction_roles_edit 的互動 UI（下拉選單／按鈕／輸入框，寫法與 cogs.auto_reply 相同） ----------


async def _respond_in_place(
    interaction: discord.Interaction,
    *,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
) -> Optional[discord.Message]:
    """回應元件互動：優先「就地編輯」/reaction_roles_edit 的那則訊息（連續編輯不洗版）。

    原訊息不能編輯時（例如 ephemeral 訊息超過 15 分鐘生命週期），退回另外開一則
    臨時訊息，功能不受影響。回傳回應後的訊息物件（拿不到就是 None）。
    """
    if not interaction.response.is_done():
        try:
            await interaction.response.edit_message(content=content, embed=embed, view=view)
        except discord.HTTPException:
            try:
                await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
            except discord.HTTPException:
                return None
    else:
        try:
            await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        except discord.HTTPException:
            return None
    try:
        return await interaction.original_response()
    except discord.HTTPException:
        return None  # 拿不到訊息物件只是沒辦法在逾時時把按鈕變灰，功能不受影響


def _raw_to_display_text(guild: discord.Guild, text: str) -> str:
    """將 Discord 原始訊息內容中的 ID 標籤轉換為人類可讀的文字（@身分組名、#頻道名、:表符名:）。"""
    if not text:
        return ""

    # 1. 轉換身份組提及 <@&role_id> -> @RoleName
    def _replace_role(m):
        try:
            rid = int(m.group(1))
            role = guild.get_role(rid)
            return f"@{role.name}" if role else m.group(0)
        except Exception:
            return m.group(0)

    text = re.sub(r'<@&(\d+)>', _replace_role, text)

    # 2. 轉換頻道提及 <#channel_id> -> #ChannelName
    def _replace_channel(m):
        try:
            cid = int(m.group(1))
            ch = guild.get_channel(cid)
            return f"#{ch.name}" if ch else m.group(0)
        except Exception:
            return m.group(0)

    text = re.sub(r'<#(\d+)>', _replace_channel, text)

    # 3. 轉換自訂表情符號 <:name:id> 或 <a:name:id> -> :name:
    text = re.sub(r'<a?:([a-zA-Z0-9_]+):\d+>', r':\1:', text)

    return text


def _display_to_raw_text(guild: discord.Guild, text: str, mapping_pairs: Optional[list] = None) -> str:
    """將管理員在輸入框輸入的可讀文字（@身分組名、:表符名:）轉回 Discord 原始格式（<@&id>、<:name:id>）。"""
    if not text:
        return ""

    # 1. 將 @RoleName 轉為 <@&role_id>（按名稱長度遞減排序，優先匹配最長名稱）
    for role in sorted([r for r in guild.roles if not r.is_default()], key=lambda x: len(x.name), reverse=True):
        pattern = rf'(?<!<@&)(?<!\w)@{re.escape(role.name)}(?!\w)'
        text = re.sub(pattern, role.mention, text)

    # 2. 將 :emoji_name: 轉為自訂表符 <:name:id> 或 <a:name:id>
    emoji_map = {}
    if mapping_pairs:
        for p in mapping_pairs:
            raw = str(p.get('emoji') or '')
            m = re.match(r'<(a?):([a-zA-Z0-9_]+):(\d+)>', raw)
            if m:
                emoji_map[m.group(2)] = raw
    for em in guild.emojis:
        emoji_map[em.name] = str(em)

    def _sub_emoji(m):
        name = m.group(1)
        return emoji_map.get(name, m.group(0))

    text = re.sub(r'(?<!<)(?<!<a):([a-zA-Z0-9_]+):(?!\d+>)', _sub_emoji, text)
    return text


def _generate_pairs_message_content(guild: discord.Guild, mapping: dict, header: Optional[str] = None) -> str:
    """根據現有配對自動產生排版美觀的領取訊息內文。"""
    pairs = mapping.get('pairs') or []
    if not header:
        header = '點擊表符領取身分組！'
    lines = [header, '']
    for p in pairs:
        emoji = p.get('emoji')
        role = guild.get_role(int(p.get('role_id') or 0))
        role_mention = role.mention if role else '(已刪除身分組)'
        lines.append(f"{emoji} {role_mention}")
    return '\n'.join(lines)


class ReactionRolesMessageSelect(discord.ui.Select):
    """列出這個伺服器所有表符身份組訊息的下拉選單（選項顯示該訊息的表符配對摘要）。"""

    def __init__(self, cog: 'ReactionRoles', guild: discord.Guild, messages: list):
        options: list[discord.SelectOption] = []
        for index, mapping in enumerate(messages, start=1):
            channel = guild.get_channel(int(mapping.get('channel_id') or 0))
            channel_name = channel.name if channel is not None else f"ID {mapping.get('channel_id')}"
            pair_count = len(mapping.get('pairs') or [])
            options.append(discord.SelectOption(
                label=f'{index}. {cog._pairs_option_text(guild, mapping)}'[:100],
                description=f'#{channel_name}｜共 {pair_count} 組表符配對'[:100],
                value=str(mapping.get('message_id')),
            ))
        super().__init__(
            placeholder='選擇要編輯的表符身份組訊息…',
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog
        self.guild = guild

    async def callback(self, interaction: discord.Interaction):
        mapping = self.cog._find_mapping_by_ids(str(self.guild.id), None, int(self.values[0]))
        if mapping is None:
            await interaction.response.send_message('❌ 找不到這則領取訊息，它可能已被其他管理員刪除。', ephemeral=True)
            return
        view = ReactionRolesEditActionView(self.cog, self.guild, mapping)
        view.initiator_id = interaction.user.id  # 只有執行編輯指令的人能按後續按鈕
        embed = self.cog._build_edit_embed(self.guild, mapping)
        await interaction.response.edit_message(embed=embed, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass  # 拿不到訊息物件只是沒辦法在逾時時把按鈕變灰，功能不受影響


class ReactionRolesEditView(discord.ui.View):
    """/reaction_roles_edit 的第一層：下拉選單選擇要編輯的訊息。"""

    def __init__(self, cog: 'ReactionRoles', guild: discord.Guild, messages: list):
        super().__init__(timeout=BUTTON_IDLE_TIMEOUT)
        self.initiator_id: Optional[int] = None
        self.message: Optional[discord.Message] = None
        self.add_item(ReactionRolesMessageSelect(cog, guild, messages))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.initiator_id is not None and interaction.user.id != self.initiator_id:
            await interaction.response.send_message('❌ 只有執行編輯指令的人才能操作這個選單。', ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            if hasattr(item, 'disabled'):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ReactionRolesEditActionView(discord.ui.View):
    """選取訊息後的編輯按鈕：編輯訊息文字／編輯表符配對／切換僅限有效表符／取消，只有執行指令的人能按。

    按鈕按下後不會停用，方便連續編輯；每次操作都會重新計時，連續 120 秒沒動作才自動失效。
    """

    def __init__(self, cog: 'ReactionRoles', guild: discord.Guild, mapping: dict):
        super().__init__(timeout=BUTTON_IDLE_TIMEOUT)
        self.cog = cog
        self.guild = guild
        self.mapping = mapping
        self.initiator_id: Optional[int] = None
        self.message: Optional[discord.Message] = None
        self._sync_toggle_label()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.initiator_id is not None and interaction.user.id != self.initiator_id:
            await interaction.response.send_message('❌ 只有執行編輯指令的人才能操作這些按鈕。', ephemeral=True)
            return False
        return True

    def _disable_all(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    def _sync_toggle_label(self):
        """切換鈕的文字要反映目前狀態：開啟中＝顯示「關閉」、關閉中＝顯示「開啟」。"""
        if bool(self.mapping.get('strict', True)):
            self.toggle_strict_button.label = '🛡️ 關閉僅限有效表符'
        else:
            self.toggle_strict_button.label = '🛡️ 開啟僅限有效表符'

    async def on_timeout(self):
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _fetch_for_modal(self, interaction: discord.Interaction):
        """按編輯鈕時抓原訊息；成功回傳 (頻道, 訊息)，失敗已回覆錯誤並回傳 (None, None)。"""
        channel, message, errors = await self.cog._fetch_editable_message(self.guild, self.mapping)
        if errors:
            await interaction.response.send_message('❌ ' + '\n'.join(errors), ephemeral=True)
            return None, None
        if self.cog.bot.user is None or message.author.id != self.cog.bot.user.id:
            await interaction.response.send_message('❌ 只能編輯機器人自己發送的領取訊息。', ephemeral=True)
            return None, None
        return channel, message

    @discord.ui.button(label='編輯訊息文字', style=discord.ButtonStyle.primary, emoji='✏️', row=0)
    async def edit_text_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, message = await self._fetch_for_modal(interaction)
        if message is None:
            return
        await interaction.response.send_modal(ReactionRolesEditTextModal(self.cog, self.guild, self.mapping, message))

    @discord.ui.button(label='依配對同步文字', style=discord.ButtonStyle.secondary, emoji='✨', row=0)
    async def auto_format_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """根據目前設定的表符配對，自動重新排版並更新訊息內文，完全免手動編輯或複製 ID。"""
        channel, message = await self._fetch_for_modal(interaction)
        if message is None:
            return

        pairs = self.mapping.get('pairs') or []
        if not pairs:
            await interaction.response.send_message('❌ 目前尚未設定任何表符配對，無法自動排版。', ephemeral=True)
            return

        # 嘗試保留原訊息第 1 行的自訂標題（如果不是以表符或提及開頭）
        first_line = (message.content or '').split('\n', 1)[0].strip()
        if (
            first_line
            and not first_line.startswith('<')
            and not first_line.startswith('•')
            and not any(first_line.startswith(p.get('emoji', '')) for p in pairs)
        ):
            custom_header = first_line
        else:
            custom_header = '點擊表符領取身分組！'

        new_text = _generate_pairs_message_content(self.guild, self.mapping, header=custom_header)
        errors = await self.cog._edit_message_text(message, new_text)
        if errors:
            await interaction.response.send_message('❌ 自動排版更新失敗：\n\n' + '\n'.join(errors), ephemeral=True)
            return

        embed = self.cog._build_edit_embed(self.guild, self.mapping, status='✨ 訊息內文已依目前設定的表符配對自動排版更新完成！')
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label='編輯表符配對', style=discord.ButtonStyle.success, emoji='🧩', row=0)
    async def edit_pairs_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 就地切換成互動式配對編輯器（身份組選單＋到訊息上按表符），不必再手動複製 ID
        self.stop()
        editor = ReactionRolesPairsEditorView(self.cog, self.guild, self.mapping)
        editor.initiator_id = interaction.user.id
        await interaction.response.edit_message(embed=editor.build_embed(), view=editor)
        try:
            editor.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @discord.ui.button(label='🛡️ 關閉僅限有效表符', style=discord.ButtonStyle.secondary, row=1)
    async def toggle_strict_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """切換「僅限有效表符」：按一下即切換並儲存，設定即時生效。"""
        if not await self.cog._toggle_message_strict(interaction, self.mapping):
            await interaction.response.send_message('❌ 儲存設定時發生錯誤，變更未生效，已回報開發者。', ephemeral=True)
            return
        self._sync_toggle_label()
        if bool(self.mapping.get('strict', True)):
            status = '🛡️ 僅限有效表符已**開啟**：成員按下不對應身份組的表符時，機器人會自動移除該反應。'
        else:
            status = '🛡️ 僅限有效表符已**關閉**：其他表符的反應會保留在訊息上（不會發放身份組）。'
        embed = self.cog._build_edit_embed(self.guild, self.mapping, status=status)
        # 就地把這則訊息換成結果＋同排按鈕（不另外開一則，避免連續操作時洗版）
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label='刪除領取訊息', style=discord.ButtonStyle.danger, emoji='🗑️', row=1)
    async def delete_message_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """開啟二次確認畫面，確認後刪除領取訊息與對應設定。"""
        self.stop()
        confirm_view = ReactionRolesDeleteConfirmView(self.cog, self.guild, self.mapping)
        confirm_view.initiator_id = interaction.user.id
        embed = confirm_view.build_confirm_embed()
        await interaction.response.edit_message(embed=embed, view=confirm_view)
        try:
            confirm_view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @discord.ui.button(label='取消', style=discord.ButtonStyle.secondary, row=1)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_all()
        embed = discord.Embed(
            title='已離開編輯',
            description='表符身份組設定保持不變。',
            color=discord.Color.light_grey(),
        )
        await interaction.response.edit_message(embed=embed, view=self)


class ReactionRolesDeleteConfirmView(discord.ui.View):
    """刪除表符身份組領取訊息的二次確認視窗：確認後會嘗試刪除頻道內的原訊息並移除資料庫設定。"""

    def __init__(self, cog: 'ReactionRoles', guild: discord.Guild, mapping: dict):
        super().__init__(timeout=BUTTON_IDLE_TIMEOUT)
        self.cog = cog
        self.guild = guild
        self.mapping = mapping
        self.initiator_id: Optional[int] = None
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.initiator_id is not None and interaction.user.id != self.initiator_id:
            await interaction.response.send_message('❌ 只有執行編輯指令的人才能操作這些按鈕。', ephemeral=True)
            return False
        return True

    def _disable_all(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def on_timeout(self):
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def build_confirm_embed(self) -> discord.Embed:
        channel_id = int(self.mapping.get('channel_id') or 0)
        message_id = int(self.mapping.get('message_id') or 0)
        channel = self.guild.get_channel(channel_id)
        channel_text = channel.mention if channel is not None else f'（ID {channel_id}）'
        jump_url = f'https://discord.com/channels/{self.guild.id}/{channel_id}/{message_id}'
        pairs = self.mapping.get('pairs') or []

        lines = [
            f'📢 頻道：{channel_text}',
            f'🔗 [前往訊息]({jump_url})',
            '',
            f'🧩 即將移除的表符配對（共 {len(pairs)} 組）：',
        ]
        if pairs:
            for pair in pairs:
                role = self.guild.get_role(int(pair.get('role_id') or 0))
                role_text = role.mention if role is not None else f"<@&{pair.get('role_id')}>"
                lines.append(f"• {pair.get('emoji')} → {role_text}")
        else:
            lines.append('（沒有任何配對）')

        lines.extend([
            '',
            '⚠️ **確定要刪除這則表符身份組領取訊息嗎？**',
            '• 機器人將一併刪除頻道內的原領取訊息與所有綁定設定',
            '• 刪除後無法復原（先前已領取身份組的成員不會被收回身份組）',
        ])

        return discord.Embed(
            title='⚠️ 確認刪除表符身份組訊息',
            description='\n'.join(lines),
            color=discord.Color.red(),
        )

    @discord.ui.button(label='確認刪除', style=discord.ButtonStyle.danger, emoji='🗑️', row=0)
    async def confirm_delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = int(self.mapping.get('channel_id') or 0)
        message_id = int(self.mapping.get('message_id') or 0)
        pairs_count = len(self.mapping.get('pairs') or [])
        channel = self.guild.get_channel(channel_id)
        channel_text = channel.mention if channel is not None else f'（ID {channel_id}）'

        # 1. 先移除設定並儲存（若儲存失敗則不刪除原訊息）
        deleted_ok = await self.cog._delete_mapping(str(self.guild.id), message_id, interaction=interaction)
        if not deleted_ok:
            await interaction.response.send_message(
                '❌ 刪除失敗：找不到這則領取訊息設定，或儲存時發生錯誤。',
                ephemeral=True,
            )
            return

        # 2. 嘗試刪除 Discord 頻道內的原領取訊息
        target_channel = channel if isinstance(channel, discord.TextChannel) else None
        if target_channel is None:
            try:
                fetched = await self.cog.bot.fetch_channel(channel_id)
                if isinstance(fetched, discord.TextChannel):
                    target_channel = fetched
            except discord.HTTPException:
                target_channel = None

        msg_note = '頻道內的原領取訊息也已一併刪除。'
        if target_channel is not None:
            try:
                msg_obj = await target_channel.fetch_message(message_id)
                await msg_obj.delete()
            except discord.NotFound:
                msg_note = '（原頻道訊息先前已被刪除，已清除殘留設定）'
            except discord.Forbidden:
                msg_note = '（⚠️ 機器人無權限刪除頻道內的原訊息，但已解除並移除此表符身份組設定）'
            except discord.HTTPException:
                msg_note = '（⚠️ 無法刪除頻道內的原訊息，但已解除並移除此表符身份組設定）'
        else:
            msg_note = '（原頻道已不存在，已清除殘留設定）'

        self.stop()
        self._disable_all()
        embed = discord.Embed(
            title='🗑️ 已刪除表符身份組訊息',
            description=(
                f'已成功移除 {channel_text} 的表符身份組配置（共 {pairs_count} 組配對）。\n'
                f'{msg_note}'
            ),
            color=discord.Color.green(),
        )
        await _respond_in_place(interaction, embed=embed, view=None)

    @discord.ui.button(label='取消返回', style=discord.ButtonStyle.secondary, emoji='↩️', row=0)
    async def cancel_delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        view = ReactionRolesEditActionView(self.cog, self.guild, self.mapping)
        view.initiator_id = interaction.user.id
        embed = self.cog._build_edit_embed(self.guild, self.mapping, status='已取消刪除，表符身份組設定保持不變。')
        view.message = await _respond_in_place(interaction, embed=embed, view=view)


class ReactionRolesEditTextModal(discord.ui.Modal):
    """編輯訊息文字的彈出視窗：欄位已預先帶入現有文字（轉為人讀名稱），送出後自動轉換回標籤並就地更新。"""

    def __init__(self, cog: 'ReactionRoles', guild: discord.Guild, mapping: dict, message: discord.Message):
        channel = guild.get_channel(int(mapping.get('channel_id') or 0))
        ch_label = f"#{channel.name}" if channel else ""
        modal_title = f"編輯訊息文字（{ch_label}）" if ch_label else "編輯領取訊息文字"
        super().__init__(title=modal_title[:45])
        self.cog = cog
        self.guild = guild
        self.mapping = mapping
        self.message = message

        display_text = _raw_to_display_text(guild, message.content or '')
        self.text_input = discord.ui.TextInput(
            label='訊息文字（支援 @身分組 與 :表符:）',
            placeholder='可直接輸入 @身分組名稱 或 :表符:，送出後自動轉換為標籤',
            default=display_text[:2000],
            required=True,
            max_length=2000,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        # 將使用者輸入的人讀文字轉回 Discord 原始標籤（<@&id>、<:name:id>）
        raw_text = _display_to_raw_text(self.guild, self.text_input.value, self.mapping.get('pairs'))
        errors: list[str] = []
        _validate_message_text(raw_text, errors)
        if not errors:
            errors = await self.cog._edit_message_text(self.message, raw_text)
        if errors:
            await interaction.response.send_message('❌ 無法儲存：\n\n' + '\n'.join(errors), ephemeral=True)
            return
        view = ReactionRolesEditActionView(self.cog, self.guild, self.mapping)
        view.initiator_id = interaction.user.id
        embed = self.cog._build_edit_embed(self.guild, self.mapping, status='✅ 訊息文字已更新完成（已自動轉換身分組與表符標籤）')
        view.message = await _respond_in_place(interaction, embed=embed, view=view)


class ReactionRolesPairsModal(discord.ui.Modal):
    """編輯表符配對的彈出視窗：欄位已預先帶入現有配對，送出後整組取代並同步訊息上的反應。

    配對格式：`表符 身份組 表符 身份組 …`（一對一、以空白分隔，最多 20 組）；
    身份組可填名稱（不含空白）、數字 ID 或 <@&ID> 提及代碼。
    """

    def __init__(self, cog: 'ReactionRoles', guild: discord.Guild, mapping: dict,
                 channel: discord.TextChannel, message: discord.Message):
        pairs = mapping.get('pairs') or []
        super().__init__(title=f'編輯表符配對（目前 {len(pairs)} 組）'[:45])
        self.cog = cog
        self.guild = guild
        self.mapping = mapping
        self.channel = channel
        self.message = message
        self.pairs_input = discord.ui.TextInput(
            label='表符 身份組（一對一、以空白分隔）',
            placeholder='例如：🎉 帥 🎮 打電動（最多 20 組）',
            default=cog._pairs_prefill(guild, mapping),
            required=True,
            max_length=1000,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.pairs_input)

    async def on_submit(self, interaction: discord.Interaction):
        errors: list[str] = []
        tokens = self.pairs_input.value.split()
        if not tokens:
            errors.append('• **表符配對**不可空白，格式：`表符 身份組 表符 身份組 …`，例如：`🎉 帥 🎮 打電動`')
        parsed, pair_errors = _parse_pairs(tokens, lambda token: _find_role(self.guild, token))
        errors.extend(pair_errors)
        assignable = self.cog._check_assignable(self.guild, parsed, errors) if not pair_errors else []
        if errors:
            await interaction.response.send_message('❌ 無法儲存：\n\n' + '\n'.join(errors), ephemeral=True)
            return

        result, apply_errors = await self.cog._sync_message_pairs(
            interaction, self.guild, self.channel, self.mapping, assignable, existing=self.message,
        )
        if result is None:
            await interaction.response.send_message('❌ 無法儲存：\n\n' + '\n'.join(apply_errors), ephemeral=True)
            return

        status = '✅ 表符配對已更新（整組取代）'
        if result['failed_adds']:
            status += f"\n⚠️ 有 {len(result['failed_adds'])} 個表符按下失敗：{'、'.join(result['failed_adds'])}（不會發放身份組）"
        if result['failed_removes']:
            status += f"\n⚠️ 有 {len(result['failed_removes'])} 個舊表符移除失敗：{'、'.join(result['failed_removes'])}（反應還在訊息上，但已不對應任何身份組）"
        view = ReactionRolesEditActionView(self.cog, self.guild, self.mapping)
        view.initiator_id = interaction.user.id
        embed = self.cog._build_edit_embed(self.guild, self.mapping, status=status)
        view.message = await _respond_in_place(interaction, embed=embed, view=view)


class ReactionRolesSingleEmojiModal(discord.ui.Modal):
    """輸入或修改單一表情符號的小視窗。"""

    def __init__(self, editor: 'ReactionRolesPairsEditorView', *, is_edit: bool = False):
        title = '更換表情符號' if is_edit else '為身份組輸入表情符號'
        super().__init__(title=title[:45])
        self.editor = editor
        self.is_edit = is_edit

        default_val = ''
        if is_edit and editor.selected_index is not None and 0 <= editor.selected_index < len(editor.draft_pairs):
            default_val = editor.draft_pairs[editor.selected_index].get('emoji', '')

        self.emoji_input = discord.ui.TextInput(
            label='表情符號',
            placeholder='直接貼上或輸入表情符號，例如：🎉、🎮 或自訂表情',
            default=default_val,
            required=True,
            max_length=100,
        )
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction):
        token = self.emoji_input.value.strip()
        emoji = _parse_emoji_token(token)
        if emoji is None:
            await interaction.response.send_message(
                f'❌ `{token}` 無法辨識為有效的表情符號，請確認後再試。',
                ephemeral=True,
            )
            return

        emoji_str = str(emoji)
        exists = any(
            p.get('emoji') == emoji_str and (not self.is_edit or idx != self.editor.selected_index)
            for idx, p in enumerate(self.editor.draft_pairs)
        )
        if exists:
            await interaction.response.send_message(
                f'❌ 表符 {emoji_str} 已在其他配對中使用，每個表符只能對應一個身份組！',
                ephemeral=True,
            )
            return

        if self.is_edit:
            if self.editor.selected_index is not None and 0 <= self.editor.selected_index < len(self.editor.draft_pairs):
                old_emoji = self.editor.draft_pairs[self.editor.selected_index].get('emoji')
                self.editor.draft_pairs[self.editor.selected_index]['emoji'] = emoji_str
                self.editor.has_unsaved_changes = True
                self.editor.status = f'✅ 已將第 {self.editor.selected_index + 1} 組的表符由 {old_emoji} 更換為 {emoji_str}（尚未儲存，完成後請點擊儲存）'
        else:
            if self.editor.pending_role is not None:
                self.editor.draft_pairs.append({'emoji': emoji_str, 'role_id': self.editor.pending_role.id})
                self.editor.has_unsaved_changes = True
                self.editor.status = f'✅ 已新增配對：{emoji_str} → {self.editor.pending_role.mention}（尚未儲存，完成後請點擊儲存）'
                self.editor.pending_role = None
            else:
                self.editor.pending_emoji = emoji_str
                self.editor.status = f'已設定表情符號 {emoji_str}！請從下方「身份組選單」挑選要搭配的身份組。'

        self.editor._refresh_components()
        self.editor.message = await _respond_in_place(interaction, embed=self.editor.build_embed(), view=self.editor)


class ReactionRolesAddPairModal(discord.ui.Modal):
    """新增配對視窗（表符必填，身份組可選填）。"""

    def __init__(self, editor: 'ReactionRolesPairsEditorView'):
        super().__init__(title='新增表符配對'[:45])
        self.editor = editor
        self.emoji_input = discord.ui.TextInput(
            label='表情符號',
            placeholder='直接貼上或輸入表情符號，例如：🎉、🎮 或自訂表情',
            required=True,
            max_length=100,
        )
        self.role_input = discord.ui.TextInput(
            label='身份組名稱或 @提及（選填）',
            placeholder='可輸入身份組名稱、@提及，或留空稍後由身份組選單挑選',
            required=False,
            max_length=100,
        )
        self.add_item(self.emoji_input)
        self.add_item(self.role_input)

    async def on_submit(self, interaction: discord.Interaction):
        emoji_token = self.emoji_input.value.strip()
        emoji = _parse_emoji_token(emoji_token)
        if emoji is None:
            await interaction.response.send_message(
                f'❌ `{emoji_token}` 無法辨識為有效的表情符號。',
                ephemeral=True,
            )
            return

        emoji_str = str(emoji)
        if any(p.get('emoji') == emoji_str for p in self.editor.draft_pairs):
            await interaction.response.send_message(
                f'❌ 表符 {emoji_str} 已在配對清單中，不能重複使用！',
                ephemeral=True,
            )
            return

        role_token = self.role_input.value.strip()
        if role_token:
            role = _find_role(self.editor.guild, role_token)
            if role is None:
                await interaction.response.send_message(
                    f'❌ 找不到身份組 `{role_token}`。若身份組名稱特殊，建議留空此欄位直接從身份組選單挑選。',
                    ephemeral=True,
                )
                return
            problems = self.editor.cog._role_problems(self.editor.guild, role)
            if problems:
                await interaction.response.send_message(
                    f"❌ 身份組 {role.mention} 無法發放：{'；'.join(problems)}",
                    ephemeral=True,
                )
                return
            self.editor.draft_pairs.append({'emoji': emoji_str, 'role_id': role.id})
            self.editor.has_unsaved_changes = True
            self.editor.pending_role = None
            self.editor.status = f'✅ 已新增配對：{emoji_str} → {role.mention}（尚未儲存，完成後請點擊儲存）'
        else:
            self.editor.pending_emoji = emoji_str
            self.editor.status = f'已設定表情符號 {emoji_str}！請從下方「身份組選單」挑選要搭配的身份組以完成新增。'

        self.editor._refresh_components()
        self.editor.message = await _respond_in_place(interaction, embed=self.editor.build_embed(), view=self.editor)


class ReactionRolesPairSelect(discord.ui.Select):
    """讓管理員點選要查看/修改/刪除的現有配對項目。"""

    def __init__(self, editor: 'ReactionRolesPairsEditorView'):
        options: list[discord.SelectOption] = [
            discord.SelectOption(
                label='➕ 新增模式（取消選取現有配對）',
                value='-1',
                description='選取後可搭配下方身份組選單與按鈕新增新配對',
                default=(editor.selected_index is None),
            )
        ]
        for idx, pair in enumerate(editor.draft_pairs):
            role = editor.guild.get_role(int(pair.get('role_id') or 0))
            role_name = role.name if role is not None else f"ID {pair.get('role_id')}"
            options.append(discord.SelectOption(
                label=f"{idx + 1}. {pair.get('emoji')} → {role_name}"[:100],
                value=str(idx),
                description=f"表符：{pair.get('emoji')} ｜ 身份組：{role_name}"[:100],
                default=(editor.selected_index == idx),
            ))
        super().__init__(
            placeholder='選擇要修改/更換表符/刪除的配對項目…',
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )
        self.editor = editor

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        if idx == -1:
            self.editor.selected_index = None
            self.editor.status = '已切換至「新增模式」。請從「身份組選單」挑選身份組，再輸入或到訊息上按表符。'
        else:
            self.editor.selected_index = idx
            selected_pair = self.editor.draft_pairs[idx]
            role = self.editor.guild.get_role(int(selected_pair.get('role_id') or 0))
            role_text = role.mention if role else f"ID {selected_pair.get('role_id')}"
            self.editor.status = (
                f"已選取第 {idx + 1} 組配對（{selected_pair.get('emoji')} → {role_text}）：\n"
                "• **換身份組**：直接從下方「身份組選單」挑選新身份組\n"
                "• **換表符**：點擊「✏️ 更換表符」或「👆 到訊息按表符」\n"
                "• **刪除**：點擊「🗑️ 刪除選取配對」"
            )
        self.editor._refresh_components()
        await interaction.response.edit_message(embed=self.editor.build_embed(), view=self.editor)


class ReactionRolesEditorRoleSelect(discord.ui.RoleSelect):
    """原生身份組選單，支援搜尋名稱與直接點選，免複製任何身份組 ID。"""

    def __init__(self, editor: 'ReactionRolesPairsEditorView'):
        super().__init__(
            placeholder='選擇身份組（支援搜尋名稱／免複製 ID）...',
            min_values=1,
            max_values=1,
            row=1,
        )
        self.editor = editor

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        role = val if isinstance(val, discord.Role) else self.editor.guild.get_role(int(val))
        if role is None:
            self.editor.status = '❌ 找不到所選取的身份組。'
            self.editor._refresh_components()
            await interaction.response.edit_message(embed=self.editor.build_embed(), view=self.editor)
            return

        problems = self.editor.cog._role_problems(self.editor.guild, role)
        if problems:
            self.editor.status = f"❌ 身份組 {role.mention} 無法發放：{'；'.join(problems)}"
            self.editor._refresh_components()
            await interaction.response.edit_message(embed=self.editor.build_embed(), view=self.editor)
            return

        # 情境 1：已在待定表符狀態，直接完成新增
        if self.editor.pending_emoji:
            emoji_str = self.editor.pending_emoji
            self.editor.pending_emoji = None
            self.editor.draft_pairs.append({'emoji': emoji_str, 'role_id': role.id})
            self.editor.has_unsaved_changes = True
            self.editor.status = f'✅ 已新增配對：{emoji_str} → {role.mention}（尚未儲存，完成後請點擊儲存）'
        # 情境 2：已選取某組配對，直接更換該組的身份組
        elif self.editor.selected_index is not None and 0 <= self.editor.selected_index < len(self.editor.draft_pairs):
            old_role_id = self.editor.draft_pairs[self.editor.selected_index].get('role_id')
            old_role = self.editor.guild.get_role(int(old_role_id or 0))
            old_name = old_role.name if old_role else f"ID {old_role_id}"
            self.editor.draft_pairs[self.editor.selected_index]['role_id'] = role.id
            self.editor.has_unsaved_changes = True
            self.editor.status = f'✅ 已將第 {self.editor.selected_index + 1} 組的身份組由 @{old_name} 更換為 {role.mention}（尚未儲存）'
        # 情境 3：未選取配對，暫存為待配對身份組
        else:
            self.editor.pending_role = role
            self.editor.status = (
                f"已選擇身份組：{role.mention}！\n"
                "• 按「➕ 輸入表符」輸入表情符號\n"
                "• 或按「👆 到訊息按表符」直接在領取訊息上按一下表情"
            )

        self.editor._refresh_components()
        await interaction.response.edit_message(embed=self.editor.build_embed(), view=self.editor)


class ReactionRolesPairsEditorView(discord.ui.View):
    """互動式表符配對編輯器：免手動複製身份組 ID 與表符 ID。"""

    def __init__(self, cog: 'ReactionRoles', guild: discord.Guild, mapping: dict):
        super().__init__(timeout=BUTTON_IDLE_TIMEOUT)
        self.cog = cog
        self.guild = guild
        self.mapping = mapping
        self.draft_pairs: list[dict] = [dict(p) for p in (mapping.get('pairs') or [])]
        self.selected_index: Optional[int] = None
        self.pending_role: Optional[discord.Role] = None
        self.pending_emoji: Optional[str] = None
        self.status: Optional[str] = None
        self.has_unsaved_changes: bool = False
        self.initiator_id: Optional[int] = None
        self.message: Optional[discord.Message] = None
        self.is_capturing: bool = False
        self._refresh_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.initiator_id is not None and interaction.user.id != self.initiator_id:
            await interaction.response.send_message('❌ 只有執行編輯指令的人才能操作這些選單與按鈕。', ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            if hasattr(item, 'disabled'):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def build_embed(self) -> discord.Embed:
        channel_id = int(self.mapping.get('channel_id') or 0)
        message_id = int(self.mapping.get('message_id') or 0)
        channel = self.guild.get_channel(channel_id)
        channel_text = channel.mention if channel is not None else f'（ID {channel_id}）'
        jump_url = f'https://discord.com/channels/{self.guild.id}/{channel_id}/{message_id}'

        lines = [
            f'📢 頻道：{channel_text} ｜ 🔗 [前往原訊息]({jump_url})',
            '',
            f'**📋 目前暫存配對清單（{len(self.draft_pairs)}/{MAX_PAIRS} 組）**：',
        ]

        if self.draft_pairs:
            for idx, pair in enumerate(self.draft_pairs):
                role = self.guild.get_role(int(pair.get('role_id') or 0))
                role_text = role.mention if role is not None else f"<@&{pair.get('role_id')}> (已刪除)"
                prefix = f"👉 **第 {idx + 1} 組**：" if (self.selected_index == idx) else f"{idx + 1}. "
                lines.append(f"{prefix}{pair.get('emoji')} → {role_text}")
        else:
            lines.append('（目前無任何配對）')

        lines.extend([
            '',
            '💡 **便利操作指引**：',
            '• **新增配對**：從「身份組選單」挑選身份組，再按「➕ 輸入表符」或「👆 到訊息按表符」',
            '• **修改身份組**：從上方「配對選單」選取項目，再直接於「身份組選單」挑選新身份組',
            '• **更換表情**：從上方「配對選單」選取項目，再按「✏️ 更換表符」或「👆 到訊息按表符」',
            '• **刪除配對**：從上方「配對選單」選取項目後，按「🗑️ 刪除選取配對」',
            '• **套用生效**：所有變更在按下「💾 儲存並套用」後才會寫入原訊息',
        ])

        embed = discord.Embed(
            title='🧩 視覺化編輯表符配對',
            description='\n'.join(lines),
            color=discord.Color.gold() if self.has_unsaved_changes else discord.Color.blurple(),
        )

        if self.pending_role is not None:
            embed.add_field(
                name='🎯 待配對身份組',
                value=f"{self.pending_role.mention}\n請點擊「➕ 輸入表符」輸入表情，或點擊「👆 到訊息按表符」直接在原訊息點擊反應！",
                inline=False,
            )
        elif self.pending_emoji is not None:
            embed.add_field(
                name='🎯 待配對表情符號',
                value=f"{self.pending_emoji}\n請直接從下方「身份組選單」挑選要搭配的身份組！",
                inline=False,
            )

        if self.status:
            embed.add_field(name='ℹ️ 提示與狀態', value=self.status[:1024], inline=False)

        if self.has_unsaved_changes:
            embed.set_footer(text='⚠️ 目前有尚未儲存的變更，請務必點擊「💾 儲存並套用」寫入設定！')
        else:
            embed.set_footer(text='所有操作皆在同一畫面就地更新；連續 120 秒無動作自動逾時。')

        return embed

    def _refresh_components(self):
        self.clear_items()
        # Row 0: 現有配對下拉選單
        self.add_item(ReactionRolesPairSelect(self))
        # Row 1: 原生身份組選單
        self.add_item(ReactionRolesEditorRoleSelect(self))

        # Row 2: 針對配對的按鈕群
        has_selection = (self.selected_index is not None and 0 <= self.selected_index < len(self.draft_pairs))
        at_limit = (len(self.draft_pairs) >= MAX_PAIRS)

        btn_add = discord.ui.Button(
            label='➕ 輸入表符',
            style=discord.ButtonStyle.success,
            row=2,
            disabled=at_limit and not self.pending_role,
        )
        btn_add.callback = self._on_add_button
        self.add_item(btn_add)

        btn_capture = discord.ui.Button(
            label='👆 到訊息按表符',
            style=discord.ButtonStyle.primary,
            row=2,
            disabled=self.is_capturing or (at_limit and not has_selection and not self.pending_role),
        )
        btn_capture.callback = self._on_capture_button
        self.add_item(btn_capture)

        btn_change_emoji = discord.ui.Button(
            label='✏️ 更換表符',
            style=discord.ButtonStyle.secondary,
            row=2,
            disabled=not has_selection,
        )
        btn_change_emoji.callback = self._on_change_emoji_button
        self.add_item(btn_change_emoji)

        btn_delete = discord.ui.Button(
            label='🗑️ 刪除選取配對',
            style=discord.ButtonStyle.danger,
            row=2,
            disabled=not has_selection,
        )
        btn_delete.callback = self._on_delete_button
        self.add_item(btn_delete)

        # Row 3: 儲存 / 返回 / 批次文字按鈕群
        btn_save = discord.ui.Button(
            label='💾 儲存並套用',
            style=discord.ButtonStyle.success if self.has_unsaved_changes else discord.ButtonStyle.secondary,
            row=3,
        )
        btn_save.callback = self._on_save_button
        self.add_item(btn_save)

        btn_back = discord.ui.Button(
            label='↩️ 返回',
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        btn_back.callback = self._on_back_button
        self.add_item(btn_back)

        btn_text = discord.ui.Button(
            label='📝 純文字批次編輯',
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        btn_text.callback = self._on_text_modal_button
        self.add_item(btn_text)

    async def _on_add_button(self, interaction: discord.Interaction):
        if len(self.draft_pairs) >= MAX_PAIRS:
            await interaction.response.send_message(
                f'❌ 表符配對已達上限（{MAX_PAIRS} 組），請先刪除部分配對再新增。',
                ephemeral=True,
            )
            return
        if self.pending_role is not None:
            await interaction.response.send_modal(ReactionRolesSingleEmojiModal(self, is_edit=False))
        else:
            await interaction.response.send_modal(ReactionRolesAddPairModal(self))

    async def _on_change_emoji_button(self, interaction: discord.Interaction):
        if self.selected_index is None or not (0 <= self.selected_index < len(self.draft_pairs)):
            await interaction.response.send_message('❌ 請先從「配對選單」選取要更換表符的項目。', ephemeral=True)
            return
        await interaction.response.send_modal(ReactionRolesSingleEmojiModal(self, is_edit=True))

    async def _on_delete_button(self, interaction: discord.Interaction):
        if self.selected_index is None or not (0 <= self.selected_index < len(self.draft_pairs)):
            await interaction.response.send_message('❌ 請先從「配對選單」選取要刪除的項目。', ephemeral=True)
            return
        del_pair = self.draft_pairs.pop(self.selected_index)
        role = self.guild.get_role(int(del_pair.get('role_id') or 0))
        role_text = role.mention if role else f"ID {del_pair.get('role_id')}"
        self.status = f"🗑️ 已刪除第 {self.selected_index + 1} 組配對：{del_pair.get('emoji')} → {role_text}（尚未儲存，完成後請點擊儲存）"
        self.selected_index = None
        self.has_unsaved_changes = True
        self._refresh_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_capture_button(self, interaction: discord.Interaction):
        has_selection = (self.selected_index is not None and 0 <= self.selected_index < len(self.draft_pairs))
        if not has_selection and self.pending_role is None:
            self.status = (
                "⚠️ 請先從下方「身份組選單」挑選要新增的身份組，\n"
                "或從上方「配對選單」選取要更換表符的配對項目，再按此按鈕！"
            )
            self._refresh_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        channel, message, errors = await self.cog._fetch_editable_message(self.guild, self.mapping)
        if errors:
            self.status = '❌ ' + '\n'.join(errors)
            self._refresh_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        if has_selection:
            pair = self.draft_pairs[self.selected_index]
            role = self.guild.get_role(int(pair.get('role_id') or 0))
            role_text = role.mention if role else f"ID {pair.get('role_id')}"
            target_desc = f"第 {self.selected_index + 1} 組（{role_text}）"
        else:
            target_desc = f"身份組 {self.pending_role.mention}"

        self.status = (
            f"⏳ **等待捕捉表符中**（限時 {int(EMOJI_CAPTURE_TIMEOUT)} 秒）：\n"
            f"請前往原訊息 🔗 [點此前往訊息]({message.jump_url}) 並**按一下**要綁定給 {target_desc} 的表情符號！\n"
            "（機器人捕捉後會自動移除該反應並填入設定，不會干擾訊息）"
        )
        self.is_capturing = True
        self.cog._emoji_capture_ignore[(message.id, interaction.user.id)] = (
            time.monotonic() + EMOJI_CAPTURE_TIMEOUT + EMOJI_CAPTURE_GRACE
        )
        self._refresh_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

        def check(p: discord.RawReactionActionEvent):
            return (
                p.message_id == message.id
                and p.user_id == interaction.user.id
                and p.guild_id == self.guild.id
            )

        try:
            payload = await self.cog.bot.wait_for('raw_reaction_add', timeout=EMOJI_CAPTURE_TIMEOUT, check=check)
        except asyncio.TimeoutError:
            self.status = '⏳ 等待表符超時（超過 60 秒未按反應），已取消捕捉。'
        else:
            emoji_str = str(payload.emoji)
            # 移除管理員點擊的暫時性反應
            try:
                await message.remove_reaction(payload.emoji, discord.Object(interaction.user.id))
            except Exception:
                pass

            exists = any(
                p.get('emoji') == emoji_str and (not has_selection or idx != self.selected_index)
                for idx, p in enumerate(self.draft_pairs)
            )
            if exists:
                self.status = f'❌ 捕捉到的表符 {emoji_str} 已在其他配對中使用，不能重複配對！'
            else:
                if has_selection:
                    old_emoji = self.draft_pairs[self.selected_index].get('emoji')
                    self.draft_pairs[self.selected_index]['emoji'] = emoji_str
                    self.status = f'✅ 已成功捕捉表符！第 {self.selected_index + 1} 組表符由 {old_emoji} 更換為 {emoji_str}（尚未儲存，完成後請點擊儲存）'
                    self.has_unsaved_changes = True
                elif self.pending_role is not None:
                    self.draft_pairs.append({'emoji': emoji_str, 'role_id': self.pending_role.id})
                    self.status = f'✅ 已成功捕捉表符！已新增配對：{emoji_str} → {self.pending_role.mention}（尚未儲存，完成後請點擊儲存）'
                    self.pending_role = None
                    self.has_unsaved_changes = True
        finally:
            self.is_capturing = False
            self._refresh_components()
            try:
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
            except Exception:
                if self.message:
                    try:
                        await self.message.edit(embed=self.build_embed(), view=self)
                    except Exception:
                        pass

    async def _on_save_button(self, interaction: discord.Interaction):
        if not self.draft_pairs:
            self.status = '❌ 表符配對不可為空，請至少保留或新增 1 組配對。'
            self._refresh_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        channel, message, errors = await self.cog._fetch_editable_message(self.guild, self.mapping)
        if errors:
            self.status = '❌ 無法讀取訊息：' + '、'.join(errors)
            self._refresh_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        assignable: list[tuple[discord.PartialEmoji, discord.Role]] = []
        pair_errors: list[str] = []
        for idx, p in enumerate(self.draft_pairs, start=1):
            emoji = _parse_emoji_token(p.get('emoji', ''))
            if emoji is None:
                pair_errors.append(f"第 {idx} 組表符 `{p.get('emoji')}` 無法辨識")
                continue
            role = self.guild.get_role(int(p.get('role_id') or 0))
            if role is None:
                pair_errors.append(f"第 {idx} 組身份組已不存在")
                continue
            problems = self.cog._role_problems(self.guild, role)
            if problems:
                pair_errors.append(f"第 {idx} 組 {role.mention}：{'；'.join(problems)}")
                continue
            assignable.append((emoji, role))

        if pair_errors:
            self.status = '❌ 無法儲存，請修正以下問題：\n' + '\n'.join(f'• {e}' for e in pair_errors)
            self._refresh_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        result, apply_errors = await self.cog._sync_message_pairs(
            interaction, self.guild, channel, self.mapping, assignable, existing=message
        )
        if result is None:
            self.status = '❌ 儲存失敗：\n' + '\n'.join(f'• {e}' for e in apply_errors)
            self._refresh_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        self.stop()
        status = f"✅ 表符配對已成功儲存並同步至訊息反應（共 {len(result['saved_pairs'])} 組）\n💡 **提示**：若想讓訊息內文也列出這些配對，可點擊下方「✨ 依配對同步文字」一鍵自動排版！"
        if result.get('failed_adds'):
            status += f"\n⚠️ 有 {len(result['failed_adds'])} 個表符按下失敗：{'、'.join(result['failed_adds'])}"
        if result.get('failed_removes'):
            status += f"\n⚠️ 有 {len(result['failed_removes'])} 個舊表符移除失敗：{'、'.join(result['failed_removes'])}"

        view = ReactionRolesEditActionView(self.cog, self.guild, self.mapping)
        view.initiator_id = interaction.user.id
        embed = self.cog._build_edit_embed(self.guild, self.mapping, status=status)
        view.message = await _respond_in_place(interaction, embed=embed, view=view)

    async def _on_back_button(self, interaction: discord.Interaction):
        self.stop()
        view = ReactionRolesEditActionView(self.cog, self.guild, self.mapping)
        view.initiator_id = interaction.user.id
        embed = self.cog._build_edit_embed(self.guild, self.mapping, status='已返回主選單，未儲存的變更已捨棄。')
        view.message = await _respond_in_place(interaction, embed=embed, view=view)

    async def _on_text_modal_button(self, interaction: discord.Interaction):
        channel, message, errors = await self.cog._fetch_editable_message(self.guild, self.mapping)
        if errors:
            await interaction.response.send_message('❌ ' + '\n'.join(errors), ephemeral=True)
            return
        await interaction.response.send_modal(ReactionRolesPairsModal(self.cog, self.guild, self.mapping, channel, message))


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
