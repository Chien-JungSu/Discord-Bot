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


def _validate_message_text(message: str, errors: list[str]):
    """驗證領取訊息文字：不可空白、不得超過 Discord 的 2000 字上限；問題訊息附加到 errors。"""
    if not message.strip():
        errors.append("• **訊息文字**不可空白")
    elif len(message) > 2000:
        errors.append(f"• **訊息文字**長度 {len(message)} 字，超過 Discord 的 2000 字上限")


# 編輯視窗的按鈕閒置逾時（秒）：與 cogs.auto_reply 相同慣例，每次操作都會重新計時
BUTTON_IDLE_TIMEOUT = 120.0


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
    ) -> tuple[list[dict], list[str]]:
        """機器人把所有指定的表符按一輪（已在訊息上的表符再按一次不會出錯，順便修補被手動刪掉的反應）。

        回傳 (saved_pairs, failed_emojis)。
        """
        saved_pairs: list[dict] = []
        failed_emojis: list[str] = []
        for emoji, role in assignable:
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
        for reaction in list(existing.reactions):
            if reaction.me and str(reaction.emoji) not in new_emoji_keys:
                try:
                    await reaction.clear()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    print(f'❌ reaction_roles：清除不再使用的舊表符 {reaction.emoji} 失敗: {e}')
                    failed_removes.append(str(reaction.emoji))

        saved_pairs, failed_adds = await self._press_emojis(existing, assignable)
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

    # ---------- /reaction_roles_edit：選單＋按鈕＋輸入框的互動式編輯 ----------
    @app_commands.command(name='reaction_roles_edit', description='編輯已發送的表符身份組訊息：選單選擇訊息後，用輸入框修改文字／表符配對，或切換「僅限有效表符」')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    async def reaction_roles_edit(self, interaction: discord.Interaction):
        if interaction.guild is None:  # guild_only 已擋，這裡只是型別上的保險
            return
        guild = interaction.guild

        guild_entry = self.settings.get(str(guild.id)) or {}
        messages = guild_entry.get('messages') or []
        if not messages:
            embed = discord.Embed(
                title='ℹ️ 目前沒有表符身份組訊息',
                description='此伺服器尚未發送任何表符身份組訊息，先用 `/reaction_roles_create` 發送一則吧！',
                color=discord.Color.light_grey(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        view = ReactionRolesEditView(self, guild, messages)
        view.initiator_id = interaction.user.id
        embed = discord.Embed(
            title='✏️ 編輯表符身份組訊息',
            description=(
                f'此伺服器目前有 **{len(messages)}/{MAX_MESSAGES_PER_GUILD}** 則表符身份組訊息。\n'
                '請從下方選單選擇要編輯的訊息（選項顯示該訊息的表符配對摘要），選取後：\n'
                '• 按「**編輯訊息文字**」或「**編輯表符配對**」開啟輸入框，欄位已預先帶入現有內容，直接修改後送出\n'
                '• 按「**🛡️ 僅限有效表符**」一下即可切換開／關\n'
                '• 所有操作都在同一則訊息上進行（連續編輯不會洗版）；連續 120 秒沒有操作按鈕會自動失效，再執行一次指令即可'
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    def _check_assignable(
        self,
        guild: discord.Guild,
        parsed: list[tuple[discord.PartialEmoji, discord.Role]],
        errors: list[str],
    ) -> list[tuple[discord.PartialEmoji, discord.Role]]:
        """檢查機器人能否發放這些身份組：@everyone、受整合管理、階層不足都會剔除並記錄錯誤。"""
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
        return assignable

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


class ReactionRolesMessageSelect(discord.ui.Select):
    """列出這個伺服器所有表符身份組訊息的下拉選單（選項顯示該訊息的表符配對摘要）。"""

    def __init__(self, cog: 'ReactionRoles', guild: discord.Guild, messages: list):
        options: list[discord.SelectOption] = []
        for index, mapping in enumerate(messages, start=1):
            channel = guild.get_channel(int(mapping.get('channel_id') or 0))
            channel_name = channel.name if channel is not None else f"ID {mapping.get('channel_id')}"
            options.append(discord.SelectOption(
                label=f'{index}. {cog._pairs_option_text(guild, mapping)}'[:100],
                description=f'#{channel_name}｜訊息 ID {mapping.get("message_id")}'[:100],
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
        self.add_item(ReactionRolesMessageSelect(cog, guild, messages))


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

    @discord.ui.button(label='編輯訊息文字', style=discord.ButtonStyle.primary, emoji='✏️')
    async def edit_text_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, message = await self._fetch_for_modal(interaction)
        if message is None:
            return
        await interaction.response.send_modal(ReactionRolesEditTextModal(self.cog, self.guild, self.mapping, message))

    @discord.ui.button(label='編輯表符配對', style=discord.ButtonStyle.success, emoji='🧩')
    async def edit_pairs_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, message = await self._fetch_for_modal(interaction)
        if message is None:
            return
        await interaction.response.send_modal(ReactionRolesPairsModal(self.cog, self.guild, self.mapping, channel, message))

    @discord.ui.button(label='🛡️ 關閉僅限有效表符', style=discord.ButtonStyle.secondary)
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

    @discord.ui.button(label='取消', style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_all()
        embed = discord.Embed(
            title='已離開編輯',
            description='表符身份組設定保持不變。',
            color=discord.Color.light_grey(),
        )
        await interaction.response.edit_message(embed=embed, view=self)


class ReactionRolesEditTextModal(discord.ui.Modal):
    """編輯訊息文字的彈出視窗：欄位已預先帶入現有文字，送出後就地更新編輯訊息。"""

    def __init__(self, cog: 'ReactionRoles', guild: discord.Guild, mapping: dict, message: discord.Message):
        super().__init__(title=f'編輯訊息文字（訊息 ID {mapping.get("message_id")}）'[:45])
        self.cog = cog
        self.guild = guild
        self.mapping = mapping
        self.message = message
        self.text_input = discord.ui.TextInput(
            label='訊息文字',
            placeholder='建議說明每個表符對應的身份組（最多 2000 字）',
            default=(message.content or '')[:2000],
            required=True,
            max_length=2000,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        errors: list[str] = []
        _validate_message_text(self.text_input.value, errors)
        if not errors:
            errors = await self.cog._edit_message_text(self.message, self.text_input.value)
        if errors:
            await interaction.response.send_message('❌ 無法儲存：\n\n' + '\n'.join(errors), ephemeral=True)
            return
        view = ReactionRolesEditActionView(self.cog, self.guild, self.mapping)
        view.initiator_id = interaction.user.id
        embed = self.cog._build_edit_embed(self.guild, self.mapping, status='✅ 訊息文字已更新')
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


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
