import os
import random
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
# 規則名稱長度上限（管理員自訂；舊規則沒有名稱時會以關鍵字代替顯示）
MAX_RULE_NAME_LENGTH = 40
# 單一規則最多可以有幾則回覆內容（超過一則時觸發會隨機挑選，即「隨機回覆模式」）
MAX_REPLIES_PER_RULE = 10
# 單一規則最多可以有幾個關鍵字（訊息命中「任一」關鍵字就會觸發該規則）
MAX_KEYWORDS_PER_RULE = 10
# 編輯用的彈出視窗每一頁放幾個輸入框。
# Discord 限制：每個彈出視窗最多只能放 5 個輸入框，所以關鍵字／回覆各 10 項要分兩頁（第 1–5 項、第 6–10 項）。
FIELDS_PER_PAGE = 5
# /auto_reply_list 總覽 embed 中，每條規則最多顯示幾則回覆（避免單一 embed 超過 Discord 的字數上限）
MAX_REPLIES_SHOWN_IN_LIST = 3

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
        data[str(guild_id)] = {'rules': [_normalize_rule(r) for r in rules if isinstance(r, dict)]}
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


def _normalize_rule(rule: dict) -> dict:
    """把規則補齊成新格式（載入時自動遷移，向後相容舊資料檔）：
    - name：規則名稱（舊格式沒有此欄位 → 空字串，顯示時退回關鍵字）
    - keywords：關鍵字清單（舊格式只有 keyword 單一關鍵字 → 自動遷移成 [keyword]）
    - keyword：維持為「第一個關鍵字」，讓舊的讀取方式依然可用
    - replies：回覆內容清單（舊格式只有 reply 單一回覆 → 自動遷移成 [reply]）
    - reply：維持為「第一則回覆」，讓舊的讀取方式依然可用
    """
    if not isinstance(rule.get('name'), str):
        rule['name'] = ''
    if not isinstance(rule.get('keywords'), list):
        legacy_kw = rule.get('keyword')
        rule['keywords'] = [legacy_kw] if isinstance(legacy_kw, str) and legacy_kw.strip() else []
    keywords = [k for k in rule['keywords'] if isinstance(k, str) and k.strip()]
    rule['keywords'] = keywords
    if keywords:
        rule['keyword'] = keywords[0]  # keyword 欄位永遠等於第一個關鍵字
    elif not isinstance(rule.get('keyword'), str):
        rule['keyword'] = ''
    if not isinstance(rule.get('replies'), list):
        legacy = rule.get('reply')
        rule['replies'] = [legacy] if isinstance(legacy, str) and legacy.strip() else []
    replies = [r for r in rule['replies'] if isinstance(r, str) and r.strip()]
    rule['replies'] = replies
    if replies:
        rule['reply'] = replies[0]  # reply 欄位永遠等於第一則回覆
    elif not isinstance(rule.get('reply'), str):
        rule['reply'] = ''
    return rule


def _normalize_whitespace(text: str) -> str:
    """去掉頭尾空白並壓縮連續空白（關鍵字與訊息內容比對前都會先過這個函式，兩邊一致）。"""
    return re.sub(r'\s+', ' ', text.strip())


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _rule_name(rule: dict) -> str:
    """規則的顯示名稱：優先使用管理員設定的規則名稱，沒有時退回偵測關鍵字（舊規則相容）。"""
    for key in ('name', 'keyword'):
        value = rule.get(key)
        if isinstance(value, str):
            cleaned = _normalize_whitespace(value)
            if cleaned:
                return _truncate(cleaned, MAX_RULE_NAME_LENGTH)
    return '(未命名規則)'


def _rule_keywords(rule: dict) -> list:
    """取得規則的所有關鍵字（相容舊格式：沒有 keywords 欄位時退回單一 keyword）。"""
    keywords = [k for k in (rule.get('keywords') or []) if isinstance(k, str) and k.strip()]
    if keywords:
        return keywords
    single = rule.get('keyword')
    if isinstance(single, str) and single.strip():
        return [single]
    return []


def _dedupe_keywords(raw_keywords: list) -> tuple:
    """把輸入框的每行文字整理成關鍵字清單：壓縮空白 → 略過空白行 → 略過重複（不分大小寫）。
    回傳 (去重後的清單, 被略過的重複關鍵字清單)。
    """
    seen = {}
    duplicates = []
    for raw in raw_keywords:
        keyword = _normalize_whitespace(raw)
        if not keyword:
            continue
        key = keyword.lower()
        if key in seen:
            duplicates.append(keyword)
            continue
        seen[key] = keyword
    return list(seen.values()), duplicates


def _clean_field_value(value, is_keyword: bool) -> str:
    """整理編輯視窗「某一格」的輸入：關鍵字會壓縮連續空白、回覆內容只去頭尾空白。
    整理後是空字串就代表這一格「留空」＝刪除該項。
    """
    text = str(value or '')
    return _normalize_whitespace(text) if is_keyword else text.strip()


def _merge_page_items(page_items: list, tail_items: list, is_keyword: bool) -> tuple:
    """把「這一頁輸入框填的項目」和「沒編到的那一頁原項目」合併成完整清單。

    回傳 (合併後的清單, 被略過的重複關鍵字清單)。關鍵字不分大小寫去重；回覆內容允許重複。
    """
    combined = list(page_items) + list(tail_items)
    if is_keyword:
        return _dedupe_keywords(combined)
    return [x for x in combined if isinstance(x, str) and x.strip()], []


def _validate_item_list(items: list, item_max_length: int, max_items: int, label: str) -> list:
    """檢查整份清單的單項長度與項數上限，回傳錯誤訊息清單（空清單代表沒問題）。"""
    errors: list = []
    too_long = next((x for x in items if len(x) > item_max_length), None)
    if too_long:
        errors.append(f'• **{label}**「{_truncate(too_long, 20)}」長度超過上限 {item_max_length} 字')
    if len(items) > max_items:
        errors.append(f'• **{label}**最多 {max_items} 個（目前 {len(items)} 個）')
    return errors


def _rule_replies(rule: dict) -> list:
    """取得規則可用的回覆內容清單（相容舊格式：沒有 replies 欄位時退回單一 reply）。"""
    replies = [r for r in (rule.get('replies') or []) if isinstance(r, str) and r.strip()]
    if replies:
        return replies
    single = rule.get('reply')
    if isinstance(single, str) and single.strip():
        return [single]
    return []


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


def _keywords_text(rule: dict, limit: int = 200) -> str:
    """把關鍵字清單併成一行顯示文字（多個關鍵字以「、」區隔，任一命中即觸發）。"""
    keywords = _rule_keywords(rule)
    return _truncate('、'.join(keywords) or '（未設定關鍵字）', limit)


def _format_replies_block(rule: dict, max_show: int = 10, per_reply_limit: int = 120) -> str:
    """把回覆內容清單排成 embed 說明用的文字（超過 max_show 則只列前面幾則避免超過 embed 上限）。"""
    replies = _rule_replies(rule)
    if not replies:
        return '（未設定回覆內容）'
    lines = [f'{i}. {_truncate(r, per_reply_limit)}' for i, r in enumerate(replies[:max_show], start=1)]
    if len(replies) > max_show:
        lines.append(f'…（其餘 {len(replies) - max_show} 則略）')
    if len(replies) > 1:
        lines.append('🎲 已啟用隨機回覆：每次觸發從上面隨機挑選一則')
    return '\n'.join(lines)


def build_rule_summary_embed(rule: dict, guild: discord.Guild) -> discord.Embed:
    """規則詳情 embed：刪除確認與編輯選單共用同一份顯示格式。"""
    replies = _rule_replies(rule)
    keywords = _rule_keywords(rule)
    return discord.Embed(
        title=f'📋 自動回覆規則：{_rule_name(rule)}',
        description=(
            f'**規則名稱**：{_rule_name(rule)}\n'
            f'**關鍵字**（共 {len(keywords)} 個，任一命中即觸發）：{_keywords_text(rule)}\n'
            f'**生效範圍**：{_scope_text(rule.get("scope"), guild)}\n'
            f'**回覆內容**（共 {len(replies)} 則）：\n{_format_replies_block(rule)}'
        ),
        color=discord.Color.blue(),
    )


def build_delete_confirm_embed(rule: dict, guild: discord.Guild) -> discord.Embed:
    """刪除確認 embed：完整顯示規則名稱、關鍵字、回覆內容、生效範圍，讓管理員確認後才真的刪。"""
    embed = build_rule_summary_embed(rule, guild)
    embed.title = '⚠️ 確認刪除自動回覆規則'
    embed.color = discord.Color.orange()
    embed.description += '\n\n刪除後此規則會立即停止偵測，且**無法復原**。'
    return embed


def build_rules_overview_embed(rules: list, guild: discord.Guild) -> discord.Embed:
    """所有規則的總覽 embed（/auto_reply_list 用）：一條規則一個欄位，顯示關鍵字、回覆內容與生效範圍。

    Discord 單一 embed 的總字數有上限，所以每條規則的回覆只顯示前 MAX_REPLIES_SHOWN_IN_LIST 則，
    其餘用「還有 N 則」帶過，並把每個欄位再截斷到安全長度。
    """
    embed = discord.Embed(
        title=f'📋 自動回覆規則總覽（{len(rules)}/{MAX_RULES_PER_GUILD}）',
        description='以下為此伺服器目前**已生效**的自動回覆規則。',
        color=discord.Color.blue(),
    )
    for index, rule in enumerate(rules, start=1):
        keywords = _rule_keywords(rule)
        replies = _rule_replies(rule)
        shown = replies[:MAX_REPLIES_SHOWN_IN_LIST]
        lines = [f'{i}. {_truncate(reply, 60)}' for i, reply in enumerate(shown, start=1)]
        if len(replies) > len(shown):
            lines.append(f'…還有 {len(replies) - len(shown)} 則')
        value = (
            f'**關鍵字**（共 {len(keywords)} 個）：{_keywords_text(rule, 80)}\n'
            f'**回覆內容**（共 {len(replies)} 則）：\n' + '\n'.join(lines) + '\n'
            f'**生效範圍**：{_scope_text(rule.get("scope"), guild)}'
        )
        embed.add_field(name=f'{index}. {_truncate(_rule_name(rule), 40)}', value=_truncate(value, 1000), inline=False)
    embed.set_footer(text='要修改規則用 /auto_reply_edit；要刪除整條規則用 /auto_reply_remove。')
    return embed


class AutoReplyRuleSelect(discord.ui.Select):
    """刪除用的下拉選單：選項就是目前「已生效」的自動回覆規則（規則名稱，舊規則退回關鍵字）。"""

    def __init__(self, cog: 'AutoReply', guild: discord.Guild, rules: list):
        self.cog = cog
        self.guild = guild
        self.rules = rules
        options = []
        for index, rule in enumerate(rules, start=1):
            name = _truncate(_rule_name(rule), 80)
            desc = f'{_scope_text(rule.get("scope"), guild)}｜{_keywords_text(rule, 40)}'
            options.append(discord.SelectOption(
                label=f'{index}. {name}'[:100],
                description=_truncate(desc, 100),
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
    def __init__(self, cog: 'AutoReply', guild: discord.Guild, rules: list):
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
                    f'**規則名稱**：{_rule_name(self.rule)}\n'
                    f'**關鍵字**：{_keywords_text(self.rule)}\n'
                    f'**生效範圍**：{_scope_text(self.rule.get("scope"), self.guild)}\n\n'
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


class AutoReplyEditSelect(discord.ui.Select):
    """編輯用的下拉選單：選項是目前所有自動回覆規則（規則名稱，舊規則退回關鍵字）。"""

    def __init__(self, cog: 'AutoReply', guild: discord.Guild, rules: list):
        self.cog = cog
        self.guild = guild
        self.rules = rules
        options = []
        for index, rule in enumerate(rules, start=1):
            name = _truncate(_rule_name(rule), 80)
            desc = f'{_scope_text(rule.get("scope"), guild)}｜{_keywords_text(rule, 40)}'
            options.append(discord.SelectOption(
                label=f'{index}. {name}'[:100],
                description=_truncate(desc, 100),
                value=str(rule.get('id')),
            ))
        super().__init__(
            placeholder='選擇要編輯的自動回覆規則…',
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        rule = next((r for r in self.rules if str(r.get('id')) == self.values[0]), None)
        if rule is None:
            await interaction.response.send_message('❌ 找不到這條規則，它可能已被其他管理員刪除。', ephemeral=True)
            return
        # 選完在同一則訊息上顯示規則詳情＋編輯按鈕
        view = AutoReplyEditActionView(self.cog, self.guild, rule)
        view.initiator_id = interaction.user.id
        await interaction.response.edit_message(
            embed=build_rule_summary_embed(rule, self.guild),
            view=view,
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass


class AutoReplyEditView(discord.ui.View):
    def __init__(self, cog: 'AutoReply', guild: discord.Guild, rules: list):
        super().__init__(timeout=180)
        self.add_item(AutoReplyEditSelect(cog, guild, rules))


class AutoReplyEditActionView(discord.ui.View):
    """選取規則後的編輯按鈕：編輯關鍵字／編輯回覆內容／取消，只有執行指令的人能按。
    按鈕按下後不會停用，方便連續編輯；180 秒沒動作才自動失效。
    """

    def __init__(self, cog: 'AutoReply', guild: discord.Guild, rule: dict):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild
        self.rule = rule
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

    @discord.ui.button(label='編輯關鍵字', style=discord.ButtonStyle.primary, emoji='✏️')
    async def edit_keywords_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AutoReplyEditKeywordsModal(self.cog, self.guild, self.rule))

    @discord.ui.button(label='編輯回覆內容', style=discord.ButtonStyle.success, emoji='💬')
    async def edit_replies_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AutoReplyEditRepliesModal(self.cog, self.guild, self.rule))

    @discord.ui.button(label='取消', style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_all()
        embed = discord.Embed(
            title='已離開編輯',
            description='自動回覆規則保持不變。',
            color=discord.Color.light_grey(),
        )
        await interaction.response.edit_message(embed=embed, view=self)


class AutoReplyListEditModal(discord.ui.Modal):
    """編輯「關鍵字清單」或「回覆內容清單」的彈出視窗基底類別（兩種清單共用同一套流程）。

    互動方式：
    - 每頁 FIELDS_PER_PAGE（5）個輸入框，預先帶入目前清單的內容，可直接改、可留空、也可往空格補
    - **留空＝刪除該項**；沒編到的那一頁（第 6–10 項）保持原樣，不會被動到
    - 送出後若整份清單都空了，會再問是否要刪除整條規則（不會安靜地存成空清單）
    """

    IS_KEYWORD = True  # True＝編輯關鍵字、False＝編輯回覆內容
    LABEL = '項目'
    ITEM_MAX_LENGTH = MAX_KEYWORD_LENGTH
    MAX_ITEMS = MAX_KEYWORDS_PER_RULE
    SUCCESS_TITLE = '✅ 已更新'
    MORE_BUTTON_LABEL = '編輯下一頁'
    MODAL_KIND = 'item'

    def __init__(self, cog: 'AutoReply', guild: discord.Guild, rule: dict, page: int = 1):
        super().__init__(title=self._build_title(rule, page))
        self.cog = cog
        self.guild = guild
        self.rule = rule
        self.page = page
        self.start = (page - 1) * FIELDS_PER_PAGE  # 這一頁是清單的第幾項開始
        items = self._current_items()
        self.inputs: list = []
        for offset in range(FIELDS_PER_PAGE):
            number = self.start + offset + 1
            text_input = discord.ui.TextInput(
                label=f'{self.LABEL} {number}',
                placeholder=f'{self.LABEL} {number}（最多 {self.ITEM_MAX_LENGTH} 字，留空＝刪除）',
                default=items[number - 1][: self.ITEM_MAX_LENGTH] if number <= len(items) else '',
                required=False,  # 留空＝刪除該項，所以不是必填
                max_length=self.ITEM_MAX_LENGTH,
                custom_id=f'auto_reply_{self.MODAL_KIND}_p{page}_{offset}',
                style=discord.TextStyle.short if self.IS_KEYWORD else discord.TextStyle.paragraph,
            )
            self.inputs.append(text_input)
            self.add_item(text_input)

    def _build_title(self, rule: dict, page: int) -> str:
        """視窗標題（Discord 上限 45 字）：標明這一頁負責第幾項到第幾項。"""
        start = (page - 1) * FIELDS_PER_PAGE + 1
        end = start + FIELDS_PER_PAGE - 1
        return _truncate(f'編輯{self.LABEL} {start}-{end}：{_rule_name(rule)}', 45)

    def _current_items(self) -> list:
        """規則目前的這一份清單（關鍵字／回覆內容）。"""
        return _rule_keywords(self.rule) if self.IS_KEYWORD else _rule_replies(self.rule)

    def _summary(self) -> str:
        return _keywords_text(self.rule) if self.IS_KEYWORD else _format_replies_block(self.rule)

    def _apply(self, items: list) -> bool:
        if self.IS_KEYWORD:
            return self.cog.set_rule_keywords(self.guild.id, self.rule.get('id'), items)
        return self.cog.set_rule_replies(self.guild.id, self.rule.get('id'), items)

    async def _ask_delete_rule(self, interaction: discord.Interaction):
        """整份清單都留空時：這條規則已經沒有可用的項目了，先問要不要整條刪掉。"""
        view = ConfirmDeleteView(self.cog, self.guild, self.rule)
        view.initiator_id = interaction.user.id
        await interaction.response.send_message(
            content=(
                f'⚠️ 你把**{self.LABEL}**的每一格都留空了（留空＝刪除該項），'
                f'這條規則就沒有{self.LABEL}可以用了。\n是否要刪除整條規則？'
            ),
            embed=build_delete_confirm_embed(self.rule, self.guild),
            view=view,
            ephemeral=True,
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass  # 拿不到訊息物件只是沒辦法在逾時時把按鈕變灰，功能不受影響

    async def on_submit(self, interaction: discord.Interaction):
        # 這一頁輸入框填的項目（留空的格子自動跳過＝刪除該項）
        page_items = [clean for clean in (_clean_field_value(t.value, self.IS_KEYWORD) for t in self.inputs) if clean]
        old_items = self._current_items()
        tail_items = old_items[self.start + FIELDS_PER_PAGE:]  # 沒編到的那一頁保持原樣
        items, duplicates = _merge_page_items(page_items, tail_items, self.IS_KEYWORD)

        errors = _validate_item_list(items, self.ITEM_MAX_LENGTH, self.MAX_ITEMS, self.LABEL)
        if errors:
            await interaction.response.send_message(
                f'❌ 無法儲存{self.LABEL}：\n\n' + '\n'.join(errors),
                ephemeral=True,
            )
            return

        if not items:
            await self._ask_delete_rule(interaction)  # 全空＝這條規則已經沒用了，問要不要刪掉
            return

        if not self._apply(items):
            await interaction.response.send_message('❌ 找不到這條規則，它可能已被其他管理員刪除。', ephemeral=True)
            return
        try:
            _save_auto_reply_settings(self.cog.settings)
        except Exception as e:
            self._apply(old_items)  # 回滾記憶體狀態
            traceback.print_exc()
            await self.cog.bot.notify_owner_error(e, interaction, extra_info='auto_reply_edit: 儲存設定失敗')
            await interaction.response.send_message('❌ 儲存設定時發生錯誤，變更未生效，已回報開發者。', ephemeral=True)
            return

        await self._send_success(interaction, items, old_items, duplicates)

    async def _send_success(self, interaction, items: list, old_items: list, duplicates: list):
        added = [x for x in items if x not in old_items]
        removed = [x for x in old_items if x not in items]
        lines = [
            f'**規則名稱**：{_rule_name(self.rule)}',
            f'**{self.LABEL}**（共 {len(items)} 項）：\n{self._summary()}',
        ]
        if added:
            lines.append('**新增**：' + '、'.join(_truncate(x, 40) for x in added))
        if removed:
            lines.append('**已刪除**：' + '、'.join(_truncate(x, 40) for x in removed))
        if duplicates:
            lines.append('（已略過重複關鍵字：' + '、'.join(duplicates) + '）')
        if not self.IS_KEYWORD:
            lines.append('🎲 隨機回覆模式：每次觸發從上面隨機挑選一則。' if len(items) > 1
                         else 'ℹ️ 只剩一則回覆，已是固定回覆模式（不再隨機挑選）。')
        embed = discord.Embed(title=self.SUCCESS_TITLE, description='\n'.join(lines), color=discord.Color.green())

        # 項目超過一頁時，第 6–10 項要再開下一頁視窗來編輯
        view = None
        if len(items) > FIELDS_PER_PAGE:
            view = AutoReplyEditSecondPageView(self.cog, self.guild, self.rule, type(self), self.MORE_BUTTON_LABEL)
            embed.set_footer(text=f'第 {FIELDS_PER_PAGE + 1}-{len(items)} 項在下一頁，可按下方按鈕繼續編輯。')
        message = await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        if view is not None:
            view.message = message


class AutoReplyEditSecondPageView(discord.ui.View):
    """存檔後想繼續編輯第 6–10 項時的按鈕（每個彈出視窗最多 5 個輸入框，所以要分頁）。"""

    def __init__(self, cog: 'AutoReply', guild: discord.Guild, rule: dict, modal_cls, label: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild
        self.rule = rule
        self.modal_cls = modal_cls
        self.initiator_id: Optional[int] = None
        self.message: Optional[discord.Message] = None
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, emoji='✏️')
        button.callback = self._open_next_page  # discord.ui.Button 的 callback 要另外指派
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.initiator_id is not None and interaction.user.id != self.initiator_id:
            await interaction.response.send_message('❌ 只有執行編輯指令的人才能操作這些按鈕。', ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _open_next_page(self, interaction: discord.Interaction):
        await interaction.response.send_modal(self.modal_cls(self.cog, self.guild, self.rule, page=2))


class AutoReplyEditKeywordsModal(AutoReplyListEditModal):
    """編輯關鍵字清單：每頁 5 格、最多 10 個關鍵字（訊息命中任一個即觸發）。"""

    IS_KEYWORD = True
    LABEL = '關鍵字'
    ITEM_MAX_LENGTH = MAX_KEYWORD_LENGTH
    MAX_ITEMS = MAX_KEYWORDS_PER_RULE
    SUCCESS_TITLE = '✅ 已更新關鍵字'
    MORE_BUTTON_LABEL = '編輯第 6-10 個關鍵字'
    MODAL_KIND = 'keywords'


class AutoReplyEditRepliesModal(AutoReplyListEditModal):
    """編輯回覆內容清單：每頁 5 格、最多 10 則回覆（超過一則時觸發會隨機挑選）。"""

    IS_KEYWORD = False
    LABEL = '回覆內容'
    ITEM_MAX_LENGTH = MAX_REPLY_LENGTH
    MAX_ITEMS = MAX_REPLIES_PER_RULE
    SUCCESS_TITLE = '✅ 已更新回覆內容'
    MORE_BUTTON_LABEL = '編輯第 6-10 則回覆'
    MODAL_KIND = 'replies'


class AutoReply(commands.Cog):
    """關鍵字自動回覆：管理員新增「規則名稱、偵測關鍵字 → 機器人回覆內容 → 生效範圍」規則，
    成員訊息包含關鍵字時機器人自動回覆；一條規則可以有多個關鍵字（命中任一即觸發）與多則回覆內容
    （多於一則時每次觸發隨機挑選，即隨機回覆模式）。每個伺服器上限 MAX_RULES_PER_GUILD 條規則。
    提供四個指令：/auto_reply_add 新增、/auto_reply_edit 編輯、/auto_reply_list 檢視、/auto_reply_remove 刪除。
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: dict = _load_auto_reply_settings()
        # key = f"{guild_id}:{channel_id}:{rule_id}:{user_id}" → 最近一次回覆的 monotonic 時間
        self._reply_cooldowns: dict = {}

    # ---------- 資料操作 ----------

    def _get_rules(self, guild_id: int) -> list:
        return (self.settings.get(str(guild_id)) or {}).get('rules') or []

    def add_rule(self, guild_id: int, name: str, keyword: str, reply: str, scope: str) -> int:
        entry = self.settings.setdefault(str(guild_id), {'rules': []})
        rules = entry.setdefault('rules', [])
        new_id = max((int(r.get('id') or 0) for r in rules), default=0) + 1
        rules.append({
            'id': new_id,
            'name': name,        # 可能為空字串（未填）→ 顯示時以關鍵字代替
            'keyword': keyword,
            'keywords': [keyword],  # 完整關鍵字清單；命中任一即觸發
            'reply': reply,      # 相容舊欄位：永遠等同第一則回覆
            'replies': [reply],  # 完整回覆清單；超過一則時觸發會隨機挑選
            'scope': scope,
        })
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

    def set_rule_keywords(self, guild_id: int, rule_id, keywords: list) -> bool:
        """整批覆寫規則的關鍵字清單（編輯關鍵字、編輯失敗回滾時使用）。"""
        for r in self._get_rules(guild_id):
            if str(r.get('id')) == str(rule_id):
                cleaned = [k for k in keywords if isinstance(k, str) and k.strip()]
                r['keywords'] = cleaned
                r['keyword'] = cleaned[0] if cleaned else (r.get('keyword') or '')  # keyword 欄位永遠等於第一個關鍵字
                return True
        return False

    def append_rule_keyword(self, guild_id: int, rule_id, keyword: str) -> bool:
        """為規則追加一個關鍵字（原有關鍵字保留，之後命中任一關鍵字都會觸發）。
        舊格式規則（只有 keyword 欄位）會先把原本的單一關鍵字併入清單，保持資料一致。
        """
        for r in self._get_rules(guild_id):
            if str(r.get('id')) == str(rule_id):
                keywords = list(r.get('keywords') or [])
                if not keywords:
                    legacy = r.get('keyword')
                    if isinstance(legacy, str) and legacy.strip():
                        keywords.append(legacy)
                keywords.append(keyword)
                r['keywords'] = keywords
                r['keyword'] = keywords[0]
                return True
        return False

    def append_rule_reply(self, guild_id: int, rule_id, reply: str) -> bool:
        """為規則新增一則回覆內容（追加到 replies 清單尾端）。
        舊格式規則（只有 reply 欄位）會先把原本的單一回覆併入清單，保持資料一致。
        """
        for r in self._get_rules(guild_id):
            if str(r.get('id')) == str(rule_id):
                replies = list(r.get('replies') or [])
                if not replies:
                    legacy = r.get('reply')
                    if isinstance(legacy, str) and legacy.strip():
                        replies.append(legacy)
                replies.append(reply)
                r['replies'] = replies
                r['reply'] = replies[0]  # reply 欄位永遠等於第一則回覆
                return True
        return False

    def set_rule_replies(self, guild_id: int, rule_id, replies: list) -> bool:
        """整批覆寫規則的回覆清單（編輯回覆內容、編輯失敗回滾時使用）。"""
        for r in self._get_rules(guild_id):
            if str(r.get('id')) == str(rule_id):
                cleaned = [x for x in replies if isinstance(x, str) and x.strip()]
                r['replies'] = cleaned
                r['reply'] = cleaned[0] if cleaned else (r.get('reply') or '')
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

    @app_commands.command(name='auto_reply_add', description='新增自動回覆規則：規則名稱、偵測關鍵字→機器人回覆內容→生效範圍（每伺服器上限 10 條，之後可再新增其他關鍵字）')
    @app_commands.describe(
        keyword='要偵測的關鍵字（訊息內容「包含」此關鍵字就會觸發，不分大小寫，最多 60 字；之後可用 /auto_reply_edit 再加最多 10 個關鍵字）',
        reply='機器人偵測到關鍵字時要自動回覆的內容',
        name='規則名稱（選填；之後用 /auto_reply_edit 的選單辨識用，不填則以關鍵字當名稱）',
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
        name: Optional[str] = None,
        channel: Optional[discord.abc.GuildChannel] = None,
    ):
        if interaction.guild is None:  # guild_only 已擋，這裡只是型別上的保險
            return
        guild = interaction.guild

        errors: list = []

        # ---------- 1. 驗證關鍵字與回覆內容 ----------
        clean_keyword = _normalize_whitespace(keyword)
        clean_reply = reply.strip()
        clean_name = _normalize_whitespace(name) if name else ''
        if not clean_keyword:
            errors.append('• **關鍵字**不可空白')
        elif len(clean_keyword) > MAX_KEYWORD_LENGTH:
            errors.append(f'• **關鍵字**長度 {len(clean_keyword)} 字，超過上限 {MAX_KEYWORD_LENGTH} 字')
        if not clean_reply:
            errors.append('• **回覆內容**不可空白')
        elif len(clean_reply) > MAX_REPLY_LENGTH:
            errors.append(f'• **回覆內容**長度 {len(clean_reply)} 字，超過上限 {MAX_REPLY_LENGTH} 字')
        if clean_name and len(clean_name) > MAX_RULE_NAME_LENGTH:
            errors.append(f'• **規則名稱**長度 {len(clean_name)} 字，超過上限 {MAX_RULE_NAME_LENGTH} 字')

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
        new_id = self.add_rule(guild.id, clean_name, clean_keyword, clean_reply, scope)
        try:
            _save_auto_reply_settings(self.settings)
        except Exception as e:
            self.remove_rule(guild.id, new_id)
            traceback.print_exc()
            await self.bot.notify_owner_error(e, interaction, extra_info='auto_reply_add: 儲存設定失敗')
            await interaction.response.send_message('❌ 儲存設定時發生錯誤，規則未生效，已回報開發者。', ephemeral=True)
            return

        scope_display = '🌐 整個伺服器' if scope == SCOPE_ALL else guild.get_channel(int(scope)).mention
        name_display = clean_name if clean_name else f'（未設定，以關鍵字「{clean_keyword}」作為名稱）'
        embed = discord.Embed(
            title='✅ 自動回覆規則已新增',
            description=(
                f'**規則名稱**：{name_display}\n'
                f'**關鍵字**：{clean_keyword}\n'
                f'**回覆內容**：{_truncate(clean_reply, 300)}\n'
                f'**生效範圍**：{scope_display}\n\n'
                f'目前共有 **{len(self._get_rules(guild.id))}/{MAX_RULES_PER_GUILD}** 條規則。'
            ),
            color=discord.Color.green(),
        )
        if warning:
            embed.add_field(name='⚠️ 權限提醒', value=warning, inline=False)
        embed.set_footer(text='訊息內容「包含」關鍵字即觸發（不分大小寫、忽略多餘空白）；同一人對同一條規則 3 秒內不重複回覆。之後可用 /auto_reply_edit 的輸入框自行增修關鍵字與回覆內容（留空＝刪除該項；超過一則回覆時會隨機挑選）。')
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='auto_reply_edit', description='編輯自動回覆規則：在輸入框中自行編輯關鍵字與回覆內容（留空＝刪除該項；超過一則回覆時自動開啟隨機回覆模式）')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    async def auto_reply_edit(self, interaction: discord.Interaction):
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

        view = AutoReplyEditView(self, guild, rules)
        embed = discord.Embed(
            title='✏️ 編輯自動回覆規則',
            description=(
                f'此伺服器目前有 **{len(rules)}/{MAX_RULES_PER_GUILD}** 條自動回覆規則。\n'
                '請從下方選單選擇要編輯的規則（選項即為**規則名稱**，舊規則會顯示偵測關鍵字），'
                '選取後可按「**編輯關鍵字**」或「**編輯回覆內容**」開啟輸入框自行填寫：\n'
                '• 每個輸入框對應清單中的一項，並已預先帶入現有內容，可直接修改\n'
                '• **留空＝刪除該項**；每頁 5 格、最多 10 項，第 6–10 項可按存檔訊息上的按鈕開下一頁\n'
                '• 關鍵字或回覆內容**全部留空**時，會再問你要不要把整條規則刪掉'
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name='auto_reply_list', description='列出目前所有自動回覆規則（規則名稱、關鍵字、回覆內容、生效範圍）')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    async def auto_reply_list(self, interaction: discord.Interaction):
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

        await interaction.response.send_message(
            embed=build_rules_overview_embed(rules, guild),
            ephemeral=True,
        )

    @app_commands.command(name='auto_reply_remove', description='刪除自動回覆規則（從目前「已生效」的規則中選取，刪除前需確認）')
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
                '請從下方選單選擇要刪除的規則（選項即為目前**已生效**的規則名稱／偵測關鍵字），'
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

            # 一條規則可以有多個關鍵字，訊息命中「任一」即觸發
            keywords = [k.lower() for k in _rule_keywords(rule)]
            if not keywords or not any(k in content for k in keywords):
                continue

            # 命中關鍵字：同一人對同一條規則限流，避免灌水洗版
            key = f'{guild_id}:{message.channel.id}:{rule.get("id")}:{message.author.id}'
            if self._is_reply_on_cooldown(key):
                continue

            # 多則回覆時隨機挑選一則（隨機回覆模式）；單則回覆就是原本的行為
            replies = _rule_replies(rule)
            if not replies:
                continue  # 沒有任何回覆內容的規則無法回覆（防呆）
            reply_text = random.choice(replies)

            try:
                await message.channel.send(
                    reply_text,
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
