"""獨立驗證腳本：驗證 cogs/auto_reply.py 的資料層功能（不需要連上 Discord）。

涵蓋項目：
- 規則名稱（name）、多關鍵字（keywords 清單）與多回覆（replies 清單）的新 schema
- 舊格式資料檔（只有 keyword／reply 單一值、沒有 name）的自動遷移與相容
- add_rule / remove_rule / set_rule_keywords / append_rule_keyword
- append_rule_reply / set_rule_replies，以及編輯視窗的輸入框處理（留空＝刪除該項、分頁合併）
- 每伺服器規則數上限、單一規則關鍵字數與回覆數上限
- 清單驗證（長度／數量／去重）與編輯介面的按鈕、輸入框結構
- on_message 的多關鍵字比對、範圍過濾、隨機回覆挑選、冷卻
- 壞檔容錯

執行方式：.venv/Scripts/python.exe tools/test_auto_reply.py
"""
import sys

sys.stdout.reconfigure(encoding='utf-8')  # Windows 主控台 cp950 印不出 emoji，先切到 utf-8

import asyncio
import contextlib
import os
import random
import tempfile

# AutoReply Cog 不是 context manager，包一層讓 with 寫法可用
_nullctx = contextlib.nullcontext

# 讓 import cogs.auto_reply 可運作（腳本位於 tools/，專案根目錄是上一層）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cogs.auto_reply as ar


class FakeChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id


class FakeAuthor:
    def __init__(self, user_id: int, bot: bool = False):
        self.id = user_id
        self.bot = bot


class FakeMessage:
    def __init__(self, content: str, channel_id: int, user_id: int, guild=None):
        self.content = content
        self.channel = FakeChannel(channel_id)
        self.author = FakeAuthor(user_id)
        self.sent = []  # 記錄被送出的回覆內容
        self.guild = guild


class FakeGuild:
    def __init__(self, guild_id: int):
        self.id = guild_id
        self.name = f'guild-{guild_id}'
        self.me = None
        self.messages = []  # 收到的訊息

    def get_channel(self, channel_id: int):
        return None  # 測試不需要真的頻道物件（顯示為「頻道已不存在」即可）


class FakeResponse:
    """假的 interaction.response：記錄送出的訊息，讓測試能檢查回覆的標題、內文與按鈕。"""

    def __init__(self, sent: list):
        self.sent = sent

    async def send_message(self, content=None, embed=None, view=None, ephemeral=False):
        self.sent.append({
            'content': content,
            'title': embed.title if embed is not None else None,
            'description': embed.description if embed is not None else None,
            'buttons': [b.label for b in view.children] if view is not None else [],
        })
        return 'MESSAGE'


class FakeInteraction:
    """假的 discord.Interaction，只提供 modal.on_submit 會用到的方法。"""

    def __init__(self, user_id: int = 1):
        self.user = FakeAuthor(user_id)
        self.sent: list = []
        self.response = FakeResponse(self.sent)

    async def original_response(self):
        return 'MESSAGE'


def fill(modal, values: list):
    """把輸入框的值設成給定清單（其餘留空），並回傳可餵給 on_submit 的假互動物件。"""
    for index, text_input in enumerate(modal.inputs):
        text_input._value = values[index] if index < len(values) else ''
    return FakeInteraction()


def make_cog(tmpdir: str) -> ar.AutoReply:
    """建立不跑 __init__ 的 AutoReply 實例，並把資料檔指到暫存目錄。"""
    cog = ar.AutoReply.__new__(ar.AutoReply)
    cog.settings = {}
    cog._reply_cooldowns = {}
    cog.bot = None
    ar.AUTO_REPLY_FILE = os.path.join(tmpdir, 'auto_reply.json')
    return cog


PASS = []
FAIL = []


def check(cond: bool, name: str):
    (PASS if cond else FAIL).append(name)
    print(('✅' if cond else '❌') + f' {name}')


def main():
    random.seed(42)  # 讓「隨機回覆」的測試可重現
    tmpdir = tempfile.mkdtemp()

    with _nullctx(make_cog(tmpdir)) as cog:
        gid = 100

        # ---------- 1. add_rule（含規則名稱） ----------
        rule_id = cog.add_rule(gid, '打招呼', '你好', '嗨！', ar.SCOPE_ALL)
        rule = cog._get_rules(gid)[0]
        check(rule['name'] == '打招呼', 'add_rule 儲存規則名稱')
        check(rule['keywords'] == ['你好'] and rule['keyword'] == '你好', 'add_rule 建立 keywords 清單且 keyword 同步')
        check(rule['replies'] == ['嗨！'] and rule['reply'] == '嗨！', 'add_rule 建立 replies 清單且 reply 同步')

        # name 為空時 _rule_name 退回關鍵字
        rule_id2 = cog.add_rule(gid, '', '再見', '拜拜', ar.SCOPE_ALL)
        rule2 = cog._get_rules(gid)[1]
        check(ar._rule_name(rule2) == '再見', '未填名稱的規則以關鍵字當顯示名稱')

        # ---------- 2. 關鍵字：追加 / 整批編輯（新增關鍵字功能） ----------
        check(cog.append_rule_keyword(gid, rule_id, '哈囉'), 'append_rule_keyword 成功')
        check(cog._get_rules(gid)[0]['keywords'] == ['你好', '哈囉'], 'append_rule_keyword 追加到 keywords 尾端')
        check(cog._get_rules(gid)[0]['keyword'] == '你好', 'append_rule_keyword 後 keyword 仍等於第一個關鍵字')
        check(not cog.append_rule_keyword(gid, 999, 'X'), 'append_rule_keyword 找不到規則回傳 False')

        # 舊格式規則（只有 keyword 欄位）追加關鍵字時要先把舊的併入清單
        legacy_kw_id = cog.add_rule(gid, '', '舊關鍵字', '回覆', ar.SCOPE_ALL)
        legacy_kw_rule = cog._get_rules(gid)[-1]
        legacy_kw_rule.pop('keywords')  # 模擬舊格式：只有 keyword 欄位
        check(cog.append_rule_keyword(gid, legacy_kw_id, '新關鍵字'), 'append_rule_keyword 對舊格式規則成功')
        check(legacy_kw_rule['keywords'] == ['舊關鍵字', '新關鍵字'], 'append_rule_keyword 併入舊的單一關鍵字')
        check(legacy_kw_rule['keyword'] == '舊關鍵字', 'append_rule_keyword 後 keyword 仍等於第一個關鍵字')

        check(cog.set_rule_keywords(gid, rule_id, ['A', 'B', 'C']), 'set_rule_keywords 整批覆寫成功')
        check(cog._get_rules(gid)[0]['keywords'] == ['A', 'B', 'C'], 'set_rule_keywords 實際更新 keywords')
        check(cog._get_rules(gid)[0]['keyword'] == 'A', 'set_rule_keywords 同步 keyword 欄位')
        check(not cog.set_rule_keywords(gid, 999, ['X']), 'set_rule_keywords 找不到規則回傳 False')

        # ---------- 3. append_rule_reply（新增回覆 → 隨機回覆模式） ----------
        legacy_rule_id = cog.add_rule(gid, '', '舊格式', '唯一回覆', ar.SCOPE_ALL)
        legacy_rule = cog._get_rules(gid)[-1]
        legacy_rule.pop('replies')  # 模擬舊格式：只有 reply 欄位

        check(cog.append_rule_reply(gid, legacy_rule_id, '第二則回覆'), 'append_rule_reply 成功')
        check(legacy_rule['replies'] == ['唯一回覆', '第二則回覆'], 'append_rule_reply 併入舊的單一回覆')
        check(legacy_rule['reply'] == '唯一回覆', 'append_rule_reply 後 reply 仍等於第一則回覆')

        check(cog.append_rule_reply(gid, rule_id, '第二種回法'), 'append_rule_reply 對新格式規則成功')
        rule = cog._get_rules(gid)[0]
        check(len(rule['replies']) == 2, 'append_rule_reply 追加到 replies 尾端')

        # ---------- 4. set_rule_replies（回滾用） ----------
        cog.set_rule_replies(gid, rule_id, ['A', 'B'])
        check(cog._get_rules(gid)[0]['replies'] == ['A', 'B'], 'set_rule_replies 整批覆寫')
        check(not cog.set_rule_replies(gid, 999, ['X']), 'set_rule_replies 找不到規則回傳 False')

        # ---------- 4b. 編輯視窗的輸入框處理（留空＝刪除該項、分頁合併） ----------
        check(ar._clean_field_value('  你好  ', True) == '你好', '關鍵字格會壓縮頭尾空白')
        check(ar._clean_field_value('   ', True) == '', '全空白的關鍵字格＝空字串（代表刪除該項）')
        check(ar._clean_field_value('  回覆  ', False) == '回覆', '回覆內容格只去頭尾空白（保留內部換行）')
        check(ar._clean_field_value(None, True) == '', 'None 也視為留空')

        merged, dup = ar._merge_page_items(['A', '', 'B'], ['C'], True)
        check(merged == ['A', 'B', 'C'], '合併這一頁與沒編到的那一頁')
        check(dup == [], '沒有重複時不報重複項')
        check(ar._merge_page_items(['A'], ['A'], True)[1] == ['A'], '合併時關鍵字去重並回報重複項')
        check(ar._merge_page_items([], ['  '], False) == ([], []), '回覆內容全空＝空清單（呼叫端會追問是否刪規則）')

        # 只編第一頁時，第 6–10 項要原封不動
        cog.set_rule_replies(gid, rule_id, ['A', 'B', 'C', 'D', 'E', 'F', 'G'])
        kept = ar._merge_page_items(['AA'], ['F', 'G'], False)[0]
        cog.set_rule_replies(gid, rule_id, kept)
        check(cog._get_rules(gid)[0]['replies'] == ['AA', 'F', 'G'], '編輯第一頁不會動到第 6–10 項')
        check(cog._get_rules(gid)[0]['reply'] == 'AA', '編輯後 reply 欄位同步為第一則')

        # ---------- 5. 規則數上限 ----------
        while len(cog._get_rules(gid)) < ar.MAX_RULES_PER_GUILD:
            n = len(cog._get_rules(gid))
            cog.add_rule(gid, f'r{n}', f'kw{n}', f'reply{n}', ar.SCOPE_ALL)
        check(len(cog._get_rules(gid)) == ar.MAX_RULES_PER_GUILD, '規則數達上限')
        ar._save_auto_reply_settings(cog.settings)

        cog2 = ar.AutoReply.__new__(ar.AutoReply)
        cog2.settings = ar._load_auto_reply_settings()
        cog2._reply_cooldowns = {}
        cog2.bot = None
        check(len(cog2._get_rules(gid)) == ar.MAX_RULES_PER_GUILD, '存取來回後規則數不變')

        # ---------- 6. 回覆數上限（MAX_REPLIES_PER_RULE） ----------
        # 選一條只有 1 則回覆的規則，把它補滿到上限
        target_id = cog2._get_rules(gid)[1]['id']
        current = list(cog2._get_rules(gid)[1]['replies'])
        ok = True
        for i in range(len(current), ar.MAX_REPLIES_PER_RULE):
            ok = ok and cog2.append_rule_reply(gid, target_id, f'回覆{i}')
        check(ok, '回覆數可加到上限')
        replies_now = cog2._get_rules(gid)[1]['replies']
        check(len(replies_now) == ar.MAX_REPLIES_PER_RULE, '回覆數達上限')

        # ---------- 7. 隨機回覆挑選（on_message 資料層） ----------
        rule_dict = cog2._get_rules(gid)[1]
        picked = {ar.random.choice(ar._rule_replies(rule_dict)) for _ in range(50)}
        check(picked.issubset(set(replies_now)), '隨機挑選只會選出清單內的回覆')
        check(len(picked) >= 2, '隨機挑選真的會選到不同則回覆')

        # ---------- 7b. 關鍵字數上限（MAX_KEYWORDS_PER_RULE） ----------
        kw_current = list(ar._rule_keywords(cog2._get_rules(gid)[1]))
        ok = True
        for i in range(len(kw_current), ar.MAX_KEYWORDS_PER_RULE):
            ok = ok and cog2.append_rule_keyword(gid, target_id, f'kw{i}')
        check(ok, '關鍵字可加到上限')
        keywords_now = ar._rule_keywords(cog2._get_rules(gid)[1])
        check(len(keywords_now) == ar.MAX_KEYWORDS_PER_RULE, '關鍵字數達上限')
        check(keywords_now[0] == '再見', '關鍵字清單第一個仍是原關鍵字')

        # ---------- 7c. 關鍵字驗證（空白／長度／數量／去重） ----------
        keywords, duplicates = ar._dedupe_keywords(['你好', '  你好  ', '', '哈囉'])
        check(keywords == ['你好', '哈囉'], '關鍵字驗證：壓縮空白並略過重複項')
        check(duplicates == ['你好'], '關鍵字驗證：回報被略過的重複關鍵字')
        kw_args = (ar.MAX_KEYWORD_LENGTH, ar.MAX_KEYWORDS_PER_RULE, '關鍵字')
        check(ar._validate_item_list(keywords, *kw_args) == [], '關鍵字驗證：正常輸入沒有錯誤')
        check(ar._validate_item_list(['   '], *kw_args) == [], '關鍵字驗證：全空不會直接報錯（改由呼叫端追問是否刪規則）')
        check(bool(ar._validate_item_list(['x' * (ar.MAX_KEYWORD_LENGTH + 1)], *kw_args)), '關鍵字驗證：超過長度上限視為錯誤')
        check(bool(ar._validate_item_list([f'k{i}' for i in range(ar.MAX_KEYWORDS_PER_RULE + 1)], *kw_args)),
              '關鍵字驗證：超過數量上限視為錯誤')
        rp_args = (ar.MAX_REPLY_LENGTH, ar.MAX_REPLIES_PER_RULE, '回覆內容')
        check(ar._validate_item_list(['正常回覆'], *rp_args) == [], '回覆內容驗證：正常輸入沒有錯誤')
        check(bool(ar._validate_item_list(['x' * (ar.MAX_REPLY_LENGTH + 1)], *rp_args)), '回覆內容驗證：超過長度上限視為錯誤')
        check(bool(ar._validate_item_list([f'r{i}' for i in range(ar.MAX_REPLIES_PER_RULE + 1)], *rp_args)),
              '回覆內容驗證：超過數量上限視為錯誤')

        # ---------- 8. 舊格式資料檔自動遷移 ----------
        legacy_file = {
            str(gid): {'rules': [{'id': 1, 'keyword': '你好', 'reply': '嗨', 'scope': ar.SCOPE_ALL}]},
        }
        with open(ar.AUTO_REPLY_FILE, 'w', encoding='utf-8') as f:
            import json
            json.dump(legacy_file, f)
        migrated = ar._load_auto_reply_settings()
        migrated_rule = migrated[str(gid)]['rules'][0]
        check(migrated_rule['name'] == '', '舊格式遷移：name 補空字串')
        check(migrated_rule['keywords'] == ['你好'], '舊格式遷移：keyword 自動併入 keywords')
        check(ar._rule_keywords(migrated_rule) == ['你好'], '舊格式規則的關鍵字清單可讀')
        check(migrated_rule['replies'] == ['嗨'], '舊格式遷移：reply 自動併入 replies')
        check(migrated_rule['reply'] == '嗨', '舊格式遷移：reply 欄位保留')
        check(ar._rule_name(migrated_rule) == '你好', '舊格式規則顯示名稱退回關鍵字')
        legacy_items = ar._rule_replies(migrated_rule)
        check(ar._merge_page_items([], legacy_items[ar.FIELDS_PER_PAGE:], False)[0] == [],
              '舊格式單一回覆規則：輸入框全留空＝整份清單變空（呼叫端會追問是否刪規則）')

        # ---------- 9. 壞檔容錯 ----------
        with open(ar.AUTO_REPLY_FILE, 'w', encoding='utf-8') as f:
            f.write('{ not json')
        check(ar._load_auto_reply_settings() == {}, '壞 JSON 容錯回空設定')

    # ---------- 10. 範圍過濾、冷卻與多關鍵字比對（模擬 on_message 流程） ----------
    class _HookCog:
        """模擬 on_message 的偵測流程（不真的發送 Discord 訊息）。"""
        def __init__(self, settings):
            self.settings = settings
            self._reply_cooldowns = {}

        def _get_rules(self, guild_id):
            return ar.AutoReply._get_rules(self, guild_id)

        def detect(self, message):
            gid = message.guild.id
            content = ar._normalize_whitespace(message.content).lower()
            if not content:
                return None
            channel_id_str = str(message.channel.id)
            for rule in self._get_rules(gid):
                scope = rule.get('scope')
                if scope and scope != ar.SCOPE_ALL and scope != channel_id_str:
                    continue
                keywords = [k.lower() for k in ar._rule_keywords(rule)]
                if not keywords or not any(k in content for k in keywords):
                    continue
                key = f'{gid}:{message.channel.id}:{rule.get("id")}:{message.author.id}'
                if ar.AutoReply._is_reply_on_cooldown(self, key):
                    continue
                replies = ar._rule_replies(rule)
                if not replies:
                    continue
                return ar.random.choice(replies)
            return None

    with _nullctx(make_cog(tempfile.mkdtemp())) as cog3:
        gid = 200
        cog3.add_rule(gid, '範圍規則', '測試', '範圍回覆', '555')  # 只在頻道 555 生效
        cog3.add_rule(gid, '全服規則', '測試', '全服回覆', ar.SCOPE_ALL)
        multi_id = cog3.add_rule(gid, '多關鍵字', '喵喵', '貓咪回覆', ar.SCOPE_ALL)
        cog3.append_rule_keyword(gid, multi_id, '汪汪')
        cog3.append_rule_keyword(gid, multi_id, '喵星人')

        hook = _HookCog(cog3.settings)
        guild = FakeGuild(gid)
        # 頻道 555 命中範圍規則（規則順序在前面優先）
        msg = FakeMessage('來個測試一下吧', 555, 1, guild=guild)
        guild.messages.append(msg)
        check(hook.detect(msg) == '範圍回覆', '指定頻道內命中範圍規則')
        # 頻道 666 不在範圍 → 落到全服規則
        msg = FakeMessage('來個測試一下吧', 666, 2, guild=guild)
        guild.messages.append(msg)
        check(hook.detect(msg) == '全服回覆', '範圍外頻道落到全服規則')
        # 空白壓縮與大小寫：'  來個   測試 吧 ' 比對 keyword '測試'
        msg = FakeMessage('  來個   測試一下吧 ', 555, 3, guild=guild)
        guild.messages.append(msg)
        check(hook.detect(msg) is not None, '空白壓縮後仍能命中關鍵字')

        # ---------- 11. 多關鍵字：同一條規則的不同關鍵字都能觸發 ----------
        check(ar._rule_keywords(cog3._get_rules(gid)[2]) == ['喵喵', '汪汪', '喵星人'], '多關鍵字清單已建立')
        msg = FakeMessage('大家喵喵叫', 777, 4, guild=guild)
        check(hook.detect(msg) == '貓咪回覆', '多關鍵字：命中第一個關鍵字')
        msg = FakeMessage('汪汪大聲叫', 777, 5, guild=guild)
        check(hook.detect(msg) == '貓咪回覆', '多關鍵字：命中第二個關鍵字')
        msg = FakeMessage('喵喵   汪汪   同時出現', 777, 6, guild=guild)
        check(hook.detect(msg) == '貓咪回覆', '多關鍵字：同時命中多個關鍵字')
        msg = FakeMessage('喵星人報到', 777, 7, guild=guild)
        check(hook.detect(msg) == '貓咪回覆', '多關鍵字：命中第三個關鍵字')
        msg = FakeMessage('狗狗叫', 777, 8, guild=guild)
        check(hook.detect(msg) is None, '多關鍵字：都沒命中則不觸發')

        # ---------- 12. 用編輯視窗的方式刪掉一則回覆後仍可正常觸發 ----------
        cog3.append_rule_reply(gid, multi_id, '貓咪回覆二號')
        check(ar._rule_replies(cog3._get_rules(gid)[2]) == ['貓咪回覆', '貓咪回覆二號'], '刪除前有兩則回覆（隨機模式）')
        cog3.set_rule_replies(gid, multi_id, ['貓咪回覆'])  # 編輯視窗把「第二則」那格留空＝刪除該則
        check(ar._rule_replies(cog3._get_rules(gid)[2]) == ['貓咪回覆'], '該格留空＝刪除那一則回覆')
        check(cog3._get_rules(gid)[2]['reply'] == '貓咪回覆', '刪除後 reply 欄位同步為第一則')
        check(ar._rule_keywords(cog3._get_rules(gid)[2]) == ['喵喵', '汪汪', '喵星人'], '刪除回覆不影響關鍵字')
        hook2 = _HookCog(cog3.settings)
        msg = FakeMessage('喵喵叫', 888, 9, guild=guild)
        check(hook2.detect(msg) == '貓咪回覆', '刪掉一則回覆後剩下的仍會被觸發')

        # ---------- 13. 編輯介面的按鈕與輸入框結構 ----------
        rule_ui = cog3._get_rules(gid)[2]
        check([b.label for b in ar.AutoReplyEditActionView(cog3, None, rule_ui).children] == ['編輯關鍵字', '編輯回覆內容', '取消'],
              '編輯介面只有三顆按鈕')

        kw_modal = ar.AutoReplyEditKeywordsModal(cog3, None, rule_ui, page=1)
        check(len(kw_modal.inputs) == ar.FIELDS_PER_PAGE, '關鍵字視窗每頁 5 個輸入框')
        check([t.label for t in kw_modal.inputs] == [f'關鍵字 {i}' for i in range(1, 6)], '輸入框對應第 1–5 個關鍵字')
        check([t.default for t in kw_modal.inputs[:3]] == ['喵喵', '汪汪', '喵星人'], '輸入框預先帶入現有關鍵字')
        check(all(t.required is False for t in kw_modal.inputs), '關鍵字輸入框不是必填（留空＝刪除該項）')

        rep_modal = ar.AutoReplyEditRepliesModal(cog3, None, rule_ui, page=1)
        check([t.label for t in rep_modal.inputs] == [f'回覆內容 {i}' for i in range(1, 6)], '回覆輸入框對應第 1–5 則')
        check(rep_modal.inputs[0].default == '貓咪回覆', '回覆輸入框預先帶入現有回覆')
        check(len(rep_modal.title) <= 45, '視窗標題不超過 Discord 上限')

        # 超過一頁的項目要另外開第二頁視窗
        many = {'id': 'x', 'name': '很多項', 'keyword': 'k0', 'keywords': [f'k{i}' for i in range(8)],
                'reply': 'r0', 'replies': [f'r{i}' for i in range(8)], 'scope': ar.SCOPE_ALL}
        page2 = ar.AutoReplyEditKeywordsModal(cog3, None, many, page=2)
        check([t.label for t in page2.inputs] == [f'關鍵字 {i}' for i in range(6, 11)], '第二頁是第 6–10 格')
        check([t.default for t in page2.inputs[:3]] == ['k5', 'k6', 'k7'], '第二頁預先帶入第 6–8 個關鍵字')
        check(page2.inputs[3].default == '', '沒有的項目該格留空（使用者可自行補上）')
        more = ar.AutoReplyEditSecondPageView(cog3, None, many, ar.AutoReplyEditKeywordsModal, '編輯第 6-10 個關鍵字')
        check([b.label for b in more.children] == ['編輯第 6-10 個關鍵字'], '超過 5 項時附「繼續編輯下一頁」按鈕')

        # ---------- 14. 實際送出編輯視窗：留空＝刪除該項、全部留空＝追問是否刪規則 ----------
        rule_edit = cog3._get_rules(gid)[0]
        ui_guild = FakeGuild(gid)
        modal = ar.AutoReplyEditKeywordsModal(cog3, ui_guild, rule_edit, page=1)
        inter = fill(modal, ['測試', '   ', '  第二個關鍵字  '])
        asyncio.run(modal.on_submit(inter))
        check(len(inter.sent) == 1 and inter.sent[0]['title'] == '✅ 已更新關鍵字', '編輯關鍵字：送出後回報成功')
        check(ar._rule_keywords(rule_edit) == ['測試', '第二個關鍵字'], '留空格＝刪除該項、其他空格可自行補上')
        check('**新增**：第二個關鍵字' in inter.sent[0]['description'], '回報中會列出新增的關鍵字')

        modal_empty = ar.AutoReplyEditKeywordsModal(cog3, ui_guild, rule_edit, page=1)
        inter_empty = fill(modal_empty, [])
        asyncio.run(modal_empty.on_submit(inter_empty))
        check(len(inter_empty.sent) == 1 and '是否要刪除整條規則' in inter_empty.sent[0]['content'], '關鍵字全部留空＝追問是否刪除整條規則')
        check(inter_empty.sent[0]['buttons'] == ['確認刪除', '取消'], '追問時附「確認刪除／取消」按鈕')
        check(ar._rule_keywords(rule_edit) == ['測試', '第二個關鍵字'], '追問期間不會先把規則清空')

        reply_modal = ar.AutoReplyEditRepliesModal(cog3, ui_guild, rule_edit, page=1)
        inter_reply = fill(reply_modal, ['  改過的回覆  '])
        asyncio.run(reply_modal.on_submit(inter_reply))
        check(ar._rule_replies(rule_edit) == ['改過的回覆'], '編輯回覆：留空的格子＝刪除該則回覆')
        check('固定回覆模式' in inter_reply.sent[0]['description'], '只剩一則回覆時提示已回到固定回覆模式')

        reply_empty = ar.AutoReplyEditRepliesModal(cog3, ui_guild, rule_edit, page=1)
        inter_reply_empty = fill(reply_empty, [])
        asyncio.run(reply_empty.on_submit(inter_reply_empty))
        check(inter_reply_empty.sent[0]['buttons'] == ['確認刪除', '取消'], '回覆內容全部留空＝追問是否刪除整條規則')
        check(ar._rule_replies(rule_edit) == ['改過的回覆'], '回覆追問期間不會先把規則清空')

        big_rule = cog3._get_rules(gid)[2]
        for i in range(6):
            cog3.append_rule_keyword(gid, big_rule['id'], f'k{i}')
        big_tail = list(ar._rule_keywords(big_rule))[ar.FIELDS_PER_PAGE:]  # 第 6–10 項應該完全不動
        big_modal = ar.AutoReplyEditKeywordsModal(cog3, ui_guild, big_rule, page=1)
        inter_big = fill(big_modal, ['kk1', 'kk2', 'kk3', 'kk4', 'kk5'])
        asyncio.run(big_modal.on_submit(inter_big))
        check(ar._rule_keywords(big_rule) == ['kk1', 'kk2', 'kk3', 'kk4', 'kk5'] + big_tail,
              '只填前 5 格時，第 6–10 個關鍵字保留不動')
        check(inter_big.sent[0]['buttons'] == ['編輯第 6-10 個關鍵字'], '超過 5 項時附「繼續編輯第 6-10 個」按鈕')

        for i in range(6, 8):
            cog3.append_rule_keyword(gid, big_rule['id'], f'k{i}')
        before_over = list(ar._rule_keywords(big_rule))
        over_modal = ar.AutoReplyEditKeywordsModal(cog3, ui_guild, big_rule, page=1)
        inter_over = fill(over_modal, ['a1', 'a2', 'a3', 'a4', 'a5'])
        asyncio.run(over_modal.on_submit(inter_over))
        check('最多' in (inter_over.sent[0]['content'] or ''), '填滿兩頁超過上限時擋下並說明上限')
        check(ar._rule_keywords(big_rule) == before_over, '超過上限時不會存檔（原本的清單不動）')

        # ---------- 15. /auto_reply_list 的總覽 embed ----------
        big_rule['replies'] = [f'回覆{i}' * 60 for i in range(ar.MAX_REPLIES_PER_RULE)]
        big_rule['reply'] = big_rule['replies'][0]
        overview = ar.build_rules_overview_embed(cog3._get_rules(gid), FakeGuild(gid))
        check(len(overview.fields) == len(cog3._get_rules(gid)), '總覽：一條規則一個欄位')
        check(str(len(cog3._get_rules(gid))) in overview.title, '總覽：標題帶上規則數')
        check(ar._rule_keywords(big_rule)[0] in overview.fields[2].value, '總覽：欄位內含關鍵字')
        check('還有' in overview.fields[2].value, f'總覽：回覆超過 {ar.MAX_REPLIES_SHOWN_IN_LIST} 則時只顯示前幾則')
        check(len(overview) <= 6000, f'總覽：embed 總長度在 Discord 上限內（{len(overview)}）')
        check(all(len(f.value) <= 1024 for f in overview.fields), '總覽：每個欄位在 Discord 上限內')

    print()
    print(f'共 {len(PASS) + len(FAIL)} 項測試：{len(PASS)} 通過，{len(FAIL)} 失敗')
    if FAIL:
        print('失敗項目：')
        for name in FAIL:
            print(f'  - {name}')
        sys.exit(1)


if __name__ == '__main__':
    main()