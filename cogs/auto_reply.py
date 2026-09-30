import os
import re
import json
import time
import tempfile
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# 重用 welcome cog 的共用工具：頻道型別驗證與中文標籤（與 reaction_roles 相同做法）
from cogs.welcome import (
    _extract_text_channel,
    _channel_type_label,
)

AUTO_REPLY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'auto_reply.json')

# 每個伺服器最多同時存在的自動回覆規則數（與 reaction_roles 的每伺服器上限慣例一致）
MAX_RULES_PER_GUILD = 10
# 單一關鍵字長度上限（比對的是「壓縮空白後」的長度）
MAX_KEYWORD_LENGTH = 60
# 回覆內容長度上限（Discord 訊息上限 2000 字，這裡抓緊一點留餘裕）
MAX_REPLY_LENGTH = 1500

# 生效範圍的內部表示：'all'（整個伺服器）或頻道 ID 字串（僅限該文字頻道）
SCOPE_ALL = 'all'

# 回覆冷卻：同一個使用者對同一條規則在秒數內重複觸發不會重複回覆（防洗版）。
# 只存在記憶體中即可——冷卻本來就只有幾秒，重啟後清空沒有影響。
REPLY_COOLDOWN_SECONDS = 3.0
# on_message 是高頻事件，冷卻記錄不能無限成長；超過上限先清過期項目，仍滿了就放行。
REPLY_COOLDOWN_LIMIT = 500


def _load_auto_reply_settings() -> dict:
    if not os.path.exists(AUTO_REPLY_FILE):
        return {}
    try:
        with open(AUTO_REPLY_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"⚠️ 讀取自動回覆設定失敗，將以空設定啟動: {e}")
        return {}
    if not isinstance(raw, dict):
        return {}
    # 容錯：規則清單必須是 list、每筆規則必須是 dict，不合的就丟掉
    data: dict = {}
    for guild_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        rules = entry.get('rules')
        if not isinstance(rules, list):
            continue
        data[str(guild_id)] = {'rules': [r for r in rules if isinstance(r, dict)]}
    return data


def _save_auto_reply_settings(data: dict):
    dir_path = os.path.dirname(AUTO_REPLY_FILE)
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=dir_path, delete=False, suffix='.tmp') as tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, AUTO_REPLY_FILE)
    except Exception as e:
        print(f"❌ 儲存自動回覆設定失敗: {e}")
        raise


def _normalize_whitespace(text: str) -> str:
    """去掉頭尾空白並壓縮連續空白（關鍵字與訊息內容比對前都會先過這個函式，兩邊一致）。"""
    return re.sub(r'\s+', ' ', text.strip())


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _scope_text(scope, guild: Optional[discord.Guild]) -> str:
    """把 scope（'all' 或頻道 ID）轉成人類可讀的字串；傳入 guild 才能解析出頻道。"""
    if not scope or scope == SCOPE_ALL:
        return '🌐 整個伺服器'
    try:
        channel_id = int(scope)
    except (TypeError, ValueError):
        return f'❓ {scope}（範圍設定異常）'
    ch = guild.get_channel(channel_id) if guild else None
    if ch is None:
        return f'❓ {scope}（頻道已不存在）'
    return f'{ch.mention}（{ch.name}）'


def build_delete_confirm_embed(rule: dict, guild: discord.Guild) -> discord.Embed:
    """刪除確認 embed：完整顯示關鍵字、回覆內容、生效範圍，讓管理員確認後才真的刪。"""
    embed = discord.Embed(
        title='⚠️ 確認刪除自動回覆規則',
        description=(
            f"**關鍵字**：{rule.get('keyword') or ''}\n"
            f"**回覆內容**：{_truncate(rule.get('reply') or '', 500)}\n"
            f"**生效範圍**：{_scope_text(rule.get('scope'), guild)}\n\n"
            '刪除後此規則會立即停止偵測，且**無法復原**。'
        ),
        color=discord.Color.orange(),
    )
    return embed


class AutoReplyRuleSelect(discord.ui.Select):
    """刪除用的下拉選單：選項就是目前「已生效」的自動回覆規則（偵測關鍵字）。"""

    def __init__(self, cog: 'AutoReply', guild: discord.Guild, rules: list[dict]):
        self.cog = cog
        self.guild = guild
        self.rules = rules
        options = []
        for index, rule in enumerate(rules, start=1):
            keyword = _truncate(_normalize_whitespace(rule.get('keyword') or '') or '(空白關鍵字)', 80)
            options.append(discord.SelectOption(
                label=f'{index}. {keyword}'[:100],
                description=_truncate(_scope_text(rule.get('scope'), guild), 100),
                value=str(rule.get('id')),
            ))
        super().__init__(
            placeholder='選擇要刪除的自動回覆規則…',
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        rule = next((r for r in self.rules if str(r.get('id')) == self.values[0]), None)
        if rule is None:
            await interaction.response.send_message('❌ 找不到這條規則，它可能已被其他管理員刪除。', ephemeral=True)
            return
        # 選完先跳出「確認／取消」按鈕，按確認才會真的刪除
        view = ConfirmDeleteView(self.cog, self.guild, rule)
        view.initiator_id = interaction.user.id
        await interaction.response.send_message(
            embed=build_delete_confirm_embed(rule, self.guild),
            view=view,
            ephemeral=True,
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass  # 拿不到訊息物件只是沒辦法在逾時時把按鈕變灰，功能不受影響


class AutoReplyRemoveView(discord.ui.View):
    def __init__(self, cog: 'AutoReply', guild: discord.Guild, rules: list[dict]):
        super().__init__(timeout=180)
        self.add_item(AutoReplyRuleSelect(cog, guild, rules))


class ConfirmDeleteView(discord.ui.View):
    """刪除前的「確認／取消」按鈕，只有當初操作的成員能按，避免其他人誤刪。"""

    def __init__(self, cog: 'AutoReply', guild: discord.Guild, rule: dict):
        super().__init__(timeout=60)
        self.cog = cog
        self.guild = guild
        self.rule = rule
        self.initiator_id: Optional[int] = None
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.initiator_id is not None and interaction.user.id != self.initiator_id:
            await interaction.response.send_message('❌ 只有執行刪除指令的人才能操作這些按鈕。', ephemeral=True)
            return False
        return True

    def _disable_buttons(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label='確認刪除', style=discord.ButtonStyle.danger, emoji='🗑️')
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_buttons()
        removed = self.cog.remove_rule(self.guild.id, self.rule.get('id'))
        if not removed:
            embed = discord.Embed(
                title='ℹ️ 規則已不存在',
                description='這條自動回覆規則可能已被其他管理員刪除。',
                color=discord.Color.light_grey(),
            )
        else:
            embed = discord.Embed(
                title='🗑️ 已刪除自動回覆規則',
                description=(
                    f"**關鍵字**：{self.rule.get('keyword') or ''}\n"
                    f"**生效範圍**：{_scope_text(self.rule.get('scope'), self.guild)}\n\n"
                    '此規則已停止偵測關鍵字。'
                ),
                color=discord.Color.green(),
            )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label='取消', style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_buttons()
        embed = discord.Embed(
            title='已取消刪除',
            description='自動回覆規則保持不變。',
            color=discord.Color.light_grey(),
        )
        await interaction.response.edit_message(embed=embed, view=self)


class AutoReply(commands.Cog):
    """關鍵字自動回覆：管理員新增「偵測關鍵字 → 機器人回覆內容 → 生效範圍」規則，
    成員訊息包含關鍵字時機器人自動回覆。每個伺服器上限 MAX_RULES_PER_GUILD 條。
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: dict = _load_auto_reply_settings()
        # key = f"{guild_id}:{channel_id}:{rule_id}:{user_id}" → 最近一次回覆的 monotonic 時間
        self._reply_cooldowns: dict[str, float] = {}

    # ---------- 資料操作 ----------

    def _get_rules(self, guild_id: int) -> list[dict]:
        return (self.settings.get(str(guild_id)) or {}).get('rules') or []

    def add_rule(self, guild_id: int, keyword: str, reply: str, scope: str) -> int:
        entry = self.settings.setdefault(str(guild_id), {'rules': []})
        rules = entry.setdefault('rules', [])
        new_id = max((int(r.get('id') or 0) for r in rules), default=0) + 1
        rules.append({'id': new_id, 'keyword': keyword, 'reply': reply, 'scope': scope})
        return new_id

    def remove_rule(self, guild_id: int, rule_id) -> bool:
        entry = self.settings.get(str(guild_id))
        if not entry:
            return False
        rules = entry.get('rules') or []
        for i, r in enumerate(rules):
            if str(r.get('id')) == str(rule_id):
                rules.pop(i)
                if not rules:
                    self.settings.pop(str(guild_id), None)  # 最後一條也刪掉時清掉整個伺服器節點
                return True
        return False

    def _is_reply_on_cooldown(self, key: str) -> bool:
        """限速檢查：冷卻中回傳 True（應忽略這次觸發）。寫法與 reaction_roles 的限速一致。"""
        now = time.monotonic()
        last = self._reply_cooldowns.get(key)
        if last is not None and now - last < REPLY_COOLDOWN_SECONDS:
            return True
        if len(self._reply_cooldowns) >= REPLY_COOLDOWN_LIMIT and key not in self._reply_cooldowns:
            expired = [k for k, ts in self._reply_cooldowns.items() if now - ts >= REPLY_COOLDOWN_SECONDS]
            for k in expired:
                del self._reply_cooldowns[k]
            if len(self._reply_cooldowns) >= REPLY_COOLDOWN_LIMIT:
                return False  # 寧可漏限速也不要擋正常使用者
        self._reply_cooldowns[key] = now
        return False

    # ---------- 指令 ----------

    @app_commands.command(name='auto_reply_add', description='新增自動回覆規則：偵測關鍵字→機器人回覆內容→生效範圍（每伺服器上限 10 條）')
    @app_commands.describe(
        keyword='要偵測的關鍵字（訊息內容「包含」此關鍵字就會觸發，不分大小寫，最多 60 字）',
        reply='機器人偵測到關鍵字時要自動回覆的內容',
        channel='生效範圍：只在此文字頻道生效（選填；不填則整個伺服器都會生效）',
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    async def auto_reply_add(
        self,
        interaction: discord.Interaction,
        keyword: str,
        reply: str,
        channel: Optional[discord.abc.GuildChannel] = None,
    ):
        if interaction.guild is None:  # guild_only 已擋，這裡只是型別上的保險
            return
        guild = interaction.guild

        errors: list[str] = []

        # ---------- 1. 驗證關鍵字與回覆內容 ----------
        clean_keyword = _normalize_whitespace(keyword)
        clean_reply = reply.strip()
        if not clean_keyword:
            errors.append('• **關鍵字**不可空白')
        elif len(clean_keyword) > MAX_KEYWORD_LENGTH:
            errors.append(f'• **關鍵字**長度 {len(clean_keyword)} 字，超過上限 {MAX_KEYWORD_LENGTH} 字')
        if not clean_reply:
            errors.append('• **回覆內容**不可空白')
        elif len(clean_reply) > MAX_REPLY_LENGTH:
            errors.append(f'• **回覆內容**長度 {len(clean_reply)} 字，超過上限 {MAX_REPLY_LENGTH} 字')

        # ---------- 2. 驗證生效範圍（自動回覆只能發生在文字頻道） ----------
        scope = SCOPE_ALL
        if channel is not None:
            tc = _extract_text_channel(channel)
            if tc is None:
                errors.append(f'• **生效範圍** 是{_channel_type_label(channel)} {channel.mention}，請改選**文字頻道**')
            else:
                scope = str(tc.id)

        if errors:
            embed = discord.Embed(
                title='❌ 無法新增自動回覆規則',
                description='請修正以下問題後再試一次：\n\n' + '\n'.join(errors),
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # ---------- 3. 檢查規則數量上限 ----------
        rules = self._get_rules(guild.id)
        if len(rules) >= MAX_RULES_PER_GUILD:
            embed = discord.Embed(
                title='❌ 自動回覆規則已達上限',
                description=(
                    f'此伺服器已有 **{len(rules)}** 條自動回覆規則（上限 {MAX_RULES_PER_GUILD} 條）。'
                    '\n請先用 `/auto_reply_remove` 刪除舊規則後再新增。'
                ),
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # ---------- 4. 權限預檢查（僅提醒，不阻擋：管理員之後仍可調整頻道權限） ----------
        warning: Optional[str] = None
        if scope != SCOPE_ALL:
            target = guild.get_channel(int(scope))
            me = guild.me
            if target is not None and me is not None:
                perms = target.permissions_for(me)
                if not (perms.view_channel and perms.send_messages):
                    warning = f'機器人在 {target.mention} 缺少「檢視頻道／發送訊息」權限，這條規則可能無法正常回覆。'

        # ---------- 5. 儲存規則（儲存失敗時回滾記憶體狀態） ----------
        new_id = self.add_rule(guild.id, clean_keyword, clean_reply, scope)
        try:
            _save_auto_reply_settings(self.settings)
        except Exception as e:
            self.remove_rule(guild.id, new_id)
            traceback.print_exc()
            await self.bot.notify_owner_error(e, interaction, extra_info='auto_reply_add: 儲存設定失敗')
            await interaction.response.send_message('❌ 儲存設定時發生錯誤，規則未生效，已回報開發者。', ephemeral=True)
            return

        scope_display = '🌐 整個伺服器' if scope == SCOPE_ALL else guild.get_channel(int(scope)).mention
        embed = discord.Embed(
            title='✅ 自動回覆規則已新增',
            description=(
                f'**關鍵字**：{clean_keyword}\n'
                f'**回覆內容**：{_truncate(clean_reply, 300)}\n'
                f'**生效範圍**：{scope_display}\n\n'
                f'目前共有 **{len(self._get_rules(guild.id))}/{MAX_RULES_PER_GUILD}** 條規則。'
            ),
            color=discord.Color.green(),
        )
        if warning:
            embed.add_field(name='⚠️ 權限提醒', value=warning, inline=False)
        embed.set_footer(text='訊息內容「包含」關鍵字即觸發（不分大小寫、忽略多餘空白）；同一人對同一條規則 3 秒內不重複回覆。')
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='auto_reply_remove', description='刪除自動回覆規則（從目前「已生效」的偵測關鍵字中選取，刪除前需確認）')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    async def auto_reply_remove(self, interaction: discord.Interaction):
        if interaction.guild is None:  # guild_only 已擋，這裡只是型別上的保險
            return
        guild = interaction.guild

        rules = self._get_rules(guild.id)
        if not rules:
            embed = discord.Embed(
                title='ℹ️ 目前沒有任何自動回覆規則',
                description='此伺服器尚未新增任何自動回覆規則，先用 `/auto_reply_add` 新增吧！',
                color=discord.Color.light_grey(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        view = AutoReplyRemoveView(self, guild, rules)
        embed = discord.Embed(
            title='🗑️ 刪除自動回覆規則',
            description=(
                f'此伺服器目前有 **{len(rules)}/{MAX_RULES_PER_GUILD}** 條自動回覆規則。\n'
                '請從下方選單選擇要刪除的規則（選項即為目前**已生效**的偵測關鍵字），'
                '選取後會再跳出確認訊息。'
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ---------- 訊息偵測 ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        content = _normalize_whitespace(message.content).lower()
        if not content:
            return  # 純圖片／附件等沒有文字的訊息

        guild_id = message.guild.id
        rules = self._get_rules(guild_id)
        if not rules:
            return

        channel_id_str = str(message.channel.id)

        for rule in rules:
            # 生效範圍過濾：'all'（含空值，與 _scope_text 一致）或指定頻道（其他頻道／討論串不觸發）
            scope = rule.get('scope')
            if scope and scope != SCOPE_ALL and scope != channel_id_str:
                continue

            keyword = _normalize_whitespace(rule.get('keyword') or '').lower()
            if not keyword or keyword not in content:
                continue

            # 命中關鍵字：同一人對同一條規則限流，避免灌水洗版
            key = f'{guild_id}:{message.channel.id}:{rule.get("id")}:{message.author.id}'
            if self._is_reply_on_cooldown(key):
                continue

            try:
                await message.channel.send(
                    rule.get('reply') or '',
                    reference=message,  # 引用觸發訊息，看得出來是回覆哪一句
                    allowed_mentions=discord.AllowedMentions.none(),  # 自動回覆一律不 ping 人
                )
            except discord.Forbidden:
                print(f'❌ auto_reply：機器人沒有在 #{message.channel}（{message.guild.name}）發言的權限。')
                return
            except discord.HTTPException as e:
                print(f'❌ auto_reply：發送自動回覆失敗: {e}')
                return
            except Exception as e:
                print(f'❌ auto_reply：發送自動回覆時發生未知錯誤: {e}')
                traceback.print_exc()
                await self.bot.notify_owner_error(
                    e,
                    extra_info=f'auto_reply.on_message guild={message.guild.name} ({guild_id})',
                )
                return

            # 一則訊息最多回覆第一條命中的規則，避免多條規則同時命中時洗版
            return


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoReply(bot))
