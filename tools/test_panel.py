from cogs.web_server import app


def test_home_page():
    client = app.test_client()
    res = client.get('/')
    assert res.status_code == 200
    html = res.data.decode('utf-8')
    assert 'hero-top-row' in html
    assert '登入' in html
    assert 'top-nav-link' in html
    assert '加入官方支援群' in html
    assert 'GitHub Repository' in html
    assert 'cta-links-row' not in html
    assert 'top-right-nav' not in html
    assert '⚙️' not in html
    assert '(Control Panel)' not in html


def test_docs_page():
    client = app.test_client()
    res = client.get('/docs')
    assert res.status_code == 200
    html = res.data.decode('utf-8')
    assert 'page-title' in html
    assert '指令文檔' in html
    assert 'back-link' in html
    assert '登入' in html
    assert 'header-top-right' not in html
    assert 'top-right-nav' not in html
    assert '⚙️' not in html
    assert '(Control Panel)' not in html


def test_login_oauth_redirect():
    client = app.test_client()
    res = client.get('/login')
    assert res.status_code == 302
    loc = res.headers.get('Location', '')
    assert 'https://discord.com/oauth2/authorize' in loc
    assert 'redirect_uri=https%3A%2F%2Fpatricksu.nkhost.dev%2Fapi%2Fauth' in loc
    assert 'client_id=1477930641944350952' in loc


def test_panel_unauthenticated():
    client = app.test_client()
    res = client.get('/panel')
    assert res.status_code == 200
    html = res.data.decode('utf-8')
    assert '登入控制面板' in html
    assert '透過 Discord 登入' in html
    assert 'tab-owner' not in html
    assert '擁有者專屬控制面板' not in html


def test_panel_non_owner_isolation():
    client = app.test_client()
    # 模擬一般管理員登入
    res_login = client.get('/api/auth/dev-login?role=admin')
    assert res_login.status_code == 302

    # 1. 前端頁面防護：確保不包含 owner_guild_only 與擁有者專區
    res_panel = client.get('/panel')
    html = res_panel.data.decode('utf-8')
    assert '伺服器管理員' in html
    assert 'BOT OWNER' not in html
    assert 'topbar-owner-crown' not in html
    assert 'tab-owner' not in html
    assert '擁有者專屬控制面板' not in html
    assert '管理員設定' not in html
    assert 'economy_admin' not in html

    # 2. 後端 API 防護：非擁有者調用所有 owner API 一律回傳 403 Forbidden
    res_econ = client.get('/api/panel/owner/economy-config')
    assert res_econ.status_code == 403

    res_user = client.get('/api/panel/owner/economy-user?user_id=123')
    assert res_user.status_code == 403

    res_cogs = client.get('/api/panel/owner/cogs')
    assert res_cogs.status_code == 403

    res_reload = client.post('/api/panel/owner/reload-cog', json={'module': 'general'})
    assert res_reload.status_code == 403

    res_sync = client.post('/api/panel/owner/sync-commands')
    assert res_sync.status_code == 403

    res_quota = client.get('/api/panel/owner/api-quota')
    assert res_quota.status_code == 403

    # 3. 伺服器指令清單防護：非擁有者絕對不會取得 owner_guild_only 指令
    res_guild = client.get('/api/panel/guild/222222222222222222/data')
    assert res_guild.status_code == 200
    commands = [c['name'] for c in res_guild.get_json().get('commands', [])]
    assert '/reload_cogs' not in commands
    assert '/economy_admin' not in commands


def test_panel_owner_access():
    client = app.test_client()
    # 模擬擁有者登入
    res_login = client.get('/api/auth/dev-login?role=owner')
    assert res_login.status_code == 302

    # 1. 前端頁面：擁有者正常載入管理員設定專區與分類子類別
    res_panel = client.get('/panel')
    html = res_panel.data.decode('utf-8')
    assert 'BOT OWNER' in html
    assert 'topbar-owner-crown' in html
    assert 'topbar-avatar-btn' in html
    assert 'topbar-user-dropdown' in html
    assert 'dropdown-owner-btn' in html
    assert 'owner-tab' not in html
    assert 'tab-owner' in html
    assert '管理員設定' in html
    assert '經濟系統設定' in html
    assert '系統維護設定' in html
    assert '擁有者專屬控制面板' not in html
    assert '儲存設定' in html
    assert '清除設定' in html
    assert 'btn-save-setting' in html
    assert 'btn-clear-setting' in html
    assert '<span>基礎與簽到設定</span>' in html
    assert '<span>小遊戲倍率設定</span>' in html
    assert '<span>玩家金幣帳戶</span>' in html
    assert '<span>模組熱重載</span>' in html
    assert '<span>指令同步與日誌</span>' in html
    assert '<span>外部 API 配額</span>' in html
    assert '<span>🪙 基礎與簽到設定</span>' not in html
    assert '<span>🔄 模組熱重載</span>' not in html

    # 2. 後端 API：擁有者可正常存取經濟參數、Cog 清單與 API 配額
    res_econ = client.get('/api/panel/owner/economy-config')
    assert res_econ.status_code == 200
    assert 'config' in res_econ.get_json()

    res_cogs = client.get('/api/panel/owner/cogs')
    assert res_cogs.status_code == 200
    assert 'cogs' in res_cogs.get_json()

    res_quota = client.get('/api/panel/owner/api-quota')
    assert res_quota.status_code == 200
    assert 'quotas' in res_quota.get_json()

    # 3. 伺服器指令清單：擁有者可查看到授權伺服器的指令
    res_guild = client.get('/api/panel/guild/1477930950288478262/data')
    assert res_guild.status_code == 200
    commands = [c['name'] for c in res_guild.get_json().get('commands', [])]
    assert '/reload_cogs' in commands
    assert '/economy_admin' in commands

    # 4. 嚴格權限隔離：即便是機器人擁有者，若在該伺服器沒有管理伺服器權限，也絕對不可查看或修改
    res_unauth_guild = client.get('/api/panel/guild/222222222222222222/data')
    assert res_unauth_guild.status_code == 403


def test_site_topbar_and_sidebar_ui():
    client = app.test_client()

    # 1. 檢驗首頁、指令頁、控制面板、服務條款、隱私權政策、登入提示頁均具備 site-topbar 與導覽選單
    for path in ['/', '/docs', '/panel', '/terms', '/privacy', '/api/auth']:
        res = client.get(path)
        assert res.status_code == 200
        html = res.data.decode('utf-8')
        assert 'site-topbar' in html
        assert 'site-menu-btn' in html
        assert 'hamburger-bar' in html
        assert 'topbar-brand' in html
        assert '炙彗JI器人' in html
        assert 'site-nav.js' in html
        assert 'href="/terms"' in html
        assert '<span>服務條款</span>' in html
        assert 'href="/privacy"' in html
        assert '<span>隱私權政策</span>' in html
        assert '<span>官方支援群</span>' not in html
        assert '<span>GitHub 原始碼</span>' not in html

    # 2. 檢驗服務條款與隱私權政策頁面內容
    res_terms = client.get('/terms')
    assert '服務條款' in res_terms.data.decode('utf-8')
    res_privacy = client.get('/privacy')
    assert '隱私權政策' in res_privacy.data.decode('utf-8')

    # 3. 檢驗首頁介紹下方三個按鈕樣式統一為 btn secondary
    res_home = client.get('/')
    home_html = res_home.data.decode('utf-8')
    assert 'btn primary cta-primary' not in home_html
    cta_section = home_html.split('<div class="cta-row">')[1].split('</div>')[0]
    assert cta_section.count('class="btn secondary"') == 3

    # 4. 檢驗 docs.html 專屬的側邊欄 UI 與邊緣熱區
    res_docs = client.get('/docs')
    docs_html = res_docs.data.decode('utf-8')
    assert 'docs-edge-hotspot' in docs_html
    assert 'docs-sidebar-trigger' in docs_html
    assert 'trigger-chevron' in docs_html
    assert 'docs-sidebar' in docs_html
    assert 'docs-sidebar-list' in docs_html


def test_local_logo_asset():
    client = app.test_client()
    res = client.get('/logo.png')
    assert res.status_code == 200
    assert 'image/png' in res.content_type

    res_static = client.get('/static/logo.png')
    assert res_static.status_code == 200

    # 確保所有頁面皆使用本地 logo.png 且不包含 dropbox 外部連結
    for path in ['/', '/docs', '/panel', '/terms', '/privacy', '/api/auth']:
        page_res = client.get(path)
        assert page_res.status_code == 200
        page_html = page_res.data.decode('utf-8')
        assert 'dropbox.com' not in page_html
        assert 'logo.png' in page_html


def test_panel_user_without_manageable_guilds():
    client = app.test_client()
    # 模擬登入無任何可管理伺服器的普通成員帳號
    res_login = client.get('/api/auth/dev-login?role=none')
    assert res_login.status_code == 302

    res_panel = client.get('/panel')
    assert res_panel.status_code == 200
    html = res_panel.data.decode('utf-8')

    # 1. 確保有顯示溫馨提示卡片與邀請連結
    assert 'no-guilds-card' in html
    assert '您目前沒有可管理的伺服器' in html
    assert '邀請機器人加入新伺服器' in html

    # 2. 確保絕對不顯示任何伺服器設定選項、工作區與側邊欄 Tabs
    assert 'server-selector-section' not in html
    assert '選擇管理伺服器或控制專區' not in html
    assert 'panel-sidebar' not in html
    assert 'tab-welcome' not in html
    assert 'tab-music' not in html
    assert 'tab-auto-reply' not in html
    assert 'tab-reaction-roles' not in html
    assert 'tab-commands' not in html
    assert 'tab-owner' not in html
    assert 'btn-save-setting' not in html
    assert 'btn-clear-setting' not in html
    assert 'modal-auto-reply' not in html


def test_panel_server_selection_navigation_and_confirm_modal():
    client = app.test_client()

    # 1. 一般管理員：擁有伺服器選擇畫面、設定畫面僅有單一返回按鈕與清除設定確認 Modal，且無管理員設定入口
    client.get('/api/auth/dev-login?role=admin')
    res_admin = client.get('/panel')
    assert res_admin.status_code == 200
    admin_html = res_admin.data.decode('utf-8')
    assert 'view-server-selection' in admin_html
    assert 'server-cards-grid' in admin_html
    assert 'view-server-settings' in admin_html
    assert 'btn-back-to-servers' in admin_html
    assert 'btn-sidebar-back' not in admin_html
    assert admin_html.count('<span>返回伺服器選擇</span>') == 1
    assert 'modal-confirm-clear' in admin_html
    assert 'modal-reaction-role' in admin_html
    assert 'modal-confirm-ok' in admin_html
    assert 'btn-confirm-danger' in admin_html
    assert 'btn-dropdown-owner' not in admin_html

    # 2. 機器人擁有者：頭像下拉選單中包含管理員設定入口 (btn-dropdown-owner)
    client.get('/api/auth/dev-login?role=owner')
    res_owner = client.get('/panel')
    assert res_owner.status_code == 200
    owner_html = res_owner.data.decode('utf-8')
    assert 'btn-dropdown-owner' in owner_html
    assert 'view-server-selection' in owner_html
    assert 'view-server-settings' in owner_html
    assert 'btn-back-to-servers' in owner_html
    assert 'btn-sidebar-back' not in owner_html
    assert owner_html.count('<span>返回伺服器選擇</span>') == 1
    assert 'modal-confirm-clear' in owner_html
    assert 'modal-reaction-role' in owner_html


def test_reaction_roles_panel_crud_and_snowflake_precision():
    from cogs.database import load_reaction_roles, save_reaction_roles

    client = app.test_client()
    client.get('/api/auth/dev-login?role=admin')
    guild_id = '222222222222222222'
    msg_id = '1484534824910852137'
    chan_id = '1483075031796813946'
    role_a = '1477940768537444462'
    role_b = '1477940839828033567'

    all_rr = load_reaction_roles()
    orig_guild_rr = all_rr.get(guild_id)

    try:
        all_rr[guild_id] = {
            'messages': [
                {
                    'message_id': int(msg_id),
                    'channel_id': int(chan_id),
                    'strict': False,
                    'pairs': [
                        {'emoji': '🔥', 'role_id': int(role_a)},
                    ],
                }
            ]
        }
        save_reaction_roles(all_rr)

        # 1. 讀取伺服器設定，驗證 64-bit ID 轉為字串不失真
        res_data = client.get(f'/api/panel/guild/{guild_id}/data')
        assert res_data.status_code == 200
        rr_list = res_data.get_json().get('reaction_roles_messages', [])
        target = next((m for m in rr_list if m['message_id'] == msg_id), None)
        assert target is not None
        assert target['channel_id'] == chan_id
        assert target['strict'] is False
        assert target['pairs'] == [{'emoji': '🔥', 'role_id': role_a}]

        # 2. 透過控制面板 API 更新表符身分組配對與嚴格模式
        res_upd = client.post(
            f'/api/panel/guild/{guild_id}/reaction-roles/update',
            json={
                'message_id': msg_id,
                'strict': True,
                'pairs': [
                    {'emoji': '🔥', 'role_id': role_a},
                    {'emoji': '<:custom:1483078986119479377>', 'role_id': role_b},
                ],
            },
        )
        assert res_upd.status_code == 200
        updated_msgs = load_reaction_roles().get(guild_id, {}).get('messages', [])
        updated_rr = next((m for m in updated_msgs if str(m.get('message_id')) == msg_id), None)
        assert updated_rr is not None
        assert updated_rr['strict'] is True
        assert updated_rr['pairs'] == [
            {'emoji': '🔥', 'role_id': int(role_a)},
            {'emoji': '<:custom:1483078986119479377>', 'role_id': int(role_b)},
        ]

        # 3. 透過控制面板 API 刪除表符身分組訊息設定
        res_del = client.post(
            f'/api/panel/guild/{guild_id}/reaction-roles/delete',
            json={'message_id': msg_id, 'delete_discord_message': True},
        )
        assert res_del.status_code == 200
        after_msgs = load_reaction_roles().get(guild_id, {}).get('messages', [])
        assert all(str(m.get('message_id')) != msg_id for m in after_msgs)

        # 4. 透過控制面板 API 新增表符身分組訊息設定
        created_msg_id = '1484534824910899999'
        res_create = client.post(
            f'/api/panel/guild/{guild_id}/reaction-roles/create',
            json={
                'channel_id': chan_id,
                'mock_message_id': created_msg_id,
                'strict': True,
                'pairs': [
                    {'emoji': '🎉', 'role_id': role_a},
                    {'emoji': '<:custom:1483078986119479377>', 'role_id': role_b},
                ],
            },
        )
        assert res_create.status_code == 200
        assert res_create.get_json().get('message_id') == created_msg_id
        created_msgs = load_reaction_roles().get(guild_id, {}).get('messages', [])
        created_rr = next((m for m in created_msgs if str(m.get('message_id')) == created_msg_id), None)
        assert created_rr is not None
        assert str(created_rr['channel_id']) == chan_id
        assert created_rr['strict'] is True
        assert created_rr['pairs'] == [
            {'emoji': '🎉', 'role_id': int(role_a)},
            {'emoji': '<:custom:1483078986119479377>', 'role_id': int(role_b)},
        ]
    finally:
        all_rr = load_reaction_roles()
        if orig_guild_rr is None:
            all_rr.pop(guild_id, None)
        else:
            all_rr[guild_id] = orig_guild_rr
        save_reaction_roles(all_rr)


def test_auto_reply_multiline_blocks_and_command_permissions():
    from cogs.database import load_auto_reply_settings, save_auto_reply_settings

    client = app.test_client()
    client.get('/api/auth/dev-login?role=admin')
    guild_id = '222222222222222222'

    # 1. 驗證面板 HTML 包含新增身分組訊息、新增回覆區塊按鈕與統一的設定元素
    res_panel = client.get('/panel')
    assert res_panel.status_code == 200
    html = res_panel.data.decode('utf-8')
    assert 'btn-add-reaction-role' in html
    assert 'btn-add-reply-block' in html
    assert 'modal-rule-replies-list' in html
    assert 'modal-rr-channel' in html

    # 2. 驗證指令清單僅有「所有成員」與「管理員」兩種權限分類，且一般使用者看不到 owner_guild_only 指令
    res_data = client.get(f'/api/panel/guild/{guild_id}/data')
    assert res_data.status_code == 200
    commands = res_data.get_json().get('commands', [])
    assert len(commands) > 0
    for c in commands:
        assert c.get('permission') in ('所有成員', '管理員')
        assert not c.get('owner_guild_only')

    # 3. 驗證自動回覆多行回覆區塊儲存與讀取不會被拆成多則
    all_ar = load_auto_reply_settings()
    orig_guild_ar = all_ar.get(guild_id)
    try:
        multiline_reply_1 = '第一行文字\n第二行文字\n第三行文字'
        multiline_reply_2 = '另一則回覆的第一行\n另一則回覆的第二行'
        res_save = client.post(
            f'/api/panel/guild/{guild_id}/auto-reply/save',
            json={
                'name': '多行回覆測試規則',
                'keywords': ['早安', '你好'],
                'replies': [multiline_reply_1, multiline_reply_2],
            },
        )
        assert res_save.status_code == 200
        saved_rules = load_auto_reply_settings().get(guild_id, {}).get('rules', [])
        assert any(r.get('replies') == [multiline_reply_1, multiline_reply_2] for r in saved_rules)
    finally:
        all_ar = load_auto_reply_settings()
        if orig_guild_ar is None:
            all_ar.pop(guild_id, None)
        else:
            all_ar[guild_id] = orig_guild_ar
        save_auto_reply_settings(all_ar)


def test_bot_default_settings_api_and_ui():
    """驗證機器人管理員後台面板新增之『機器人預設設定』區域與後端 API。"""
    client = app.test_client()

    # 1. 未登入或非擁有者禁止存取
    res_unauth = client.get('/api/panel/owner/bot-defaults')
    assert res_unauth.status_code == 403

    # 2. 模擬機器人擁有者登入
    res_login = client.get('/api/auth/dev-login?role=owner')
    assert res_login.status_code == 302

    # 3. 驗證後台面板 HTML 包含機器人預設設定卡片與子分頁
    res_page = client.get('/panel')
    assert res_page.status_code == 200
    html = res_page.data.decode('utf-8')
    assert 'owner-sub-bot-defaults' in html
    assert '機器人預設設定' in html
    assert '所有伺服器預設值' in html
    assert 'bot-default-ai-model' in html
    assert 'bot-default-ai-instruction' in html
    assert 'btn-apply-defaults-all' in html

    # 4. 讀取全域預設值 API
    res_get = client.get('/api/panel/owner/bot-defaults')
    assert res_get.status_code == 200
    data = res_get.get_json()
    assert data['status'] == 'ok'
    assert 'defaults' in data
    assert 'models' in data

    # 5. 儲存全域預設值 API
    res_post = client.post(
        '/api/panel/owner/bot-defaults',
        json={
            'ai_is_enabled': True,
            'ai_model': 'gemini:gemini-flash-latest',
            'ai_temperature': 0.8,
            'ai_max_tokens': 3500,
            'ai_instruction': '測試專用全域預設人設提示詞',
            'apply_to_all': False,
        },
    )
    assert res_post.status_code == 200
    res_data = res_post.get_json()
    assert res_data['status'] == 'ok'
    assert res_data['defaults']['ai_model'] == 'gemini:gemini-flash-latest'
    assert res_data['defaults']['ai_instruction'] == '測試專用全域預設人設提示詞'

    # 6. 驗證 cogs.database.get_bot_default_settings 回傳最新設定
    from cogs.database import get_bot_default_settings
    current_defaults = get_bot_default_settings()
    assert current_defaults['ai_model'] == 'gemini:gemini-flash-latest'
    assert current_defaults['ai_instruction'] == '測試專用全域預設人設提示詞'


def test_table_to_image_extractor():
    """驗證 Markdown 表格抓取與轉製為圖片功能。"""
    from cogs.table_renderer import extract_and_render_markdown_tables

    sample_response = (
        "沒問題！既然你說「隨便」，那我就為你量身打造一張分析表：\n\n"
        "| 飲品名稱 | 主要功效 | 精神狀態 | 副作用 | 炙彗評分 |\n"
        "| :--- | :--- | :--- | :--- | :---: |\n"
        "| 美式黑咖啡 | 靈魂強制開機 | 表面冷靜，內心已靈魂出竅 | 心悸、突然看破紅塵 | ★★★★☆ |\n"
        "| 全糖珍奶 | 瞬間分泌多巴胺 | 充滿希望，覺得世界很美好 | 半小時後血糖驟降想睡覺 | ★★★★★ |\n\n"
        "如果需要其他表格隨時跟我說！"
    )

    clean_text, files = extract_and_render_markdown_tables(sample_response)
    assert "[[TABLE_IMAGE:table_1.png]]" in clean_text
    assert "📊" not in clean_text
    assert len(files) == 1
    assert files[0].filename == "table_1.png"
    # 確保圖片 bytes 非空
    assert len(files[0].fp.getvalue()) > 1000

    # 驗證 Components V2 Container 會將圖片嵌入 MediaGallery 組件且無多餘提示文字
    from cogs.ai import AI
    from discord import ui
    ai_cog = AI(None)
    view = ai_cog._build_ai_components_v2_view(clean_response=clean_text, footer_text="footer | stats")
    assert len(view.children) == 1
    container = view.children[0]
    assert isinstance(container, ui.Container)
    # 確保 Container 內部含有 MediaGallery 組件
    has_gallery = any(isinstance(c, ui.MediaGallery) for c in container.children)
    assert has_gallery, "Container 內部應包含 ui.MediaGallery 組件"

    # 無表格的一般文字應保持原樣且不產生圖片
    normal_text = "這是一段沒有表格的普通回覆。"
    clean_normal, normal_files = extract_and_render_markdown_tables(normal_text)
    assert clean_normal == normal_text
    assert len(normal_files) == 0


def test_chinese_font_loading():
    """驗證繁體中文字型載入與字元寬度計算，確保無豆腐塊。"""
    from cogs.table_renderer import get_font
    font = get_font(16, bold=False)
    assert font is not None
    # 4 個繁體中文字元在 16px 下寬度應約為 64px 左右 (非缺少字型時的 20px 豆腐塊)
    bbox = font.getbbox("繁體中文")
    width = bbox[2] - bbox[0]
    assert width >= 55, f"中文字串寬度異常 ({width}px)，可能缺字型造成豆腐塊"


def test_kaomoji_rendering():
    """驗證各國罕見符號與顏文字 (如 ٩(•̀ㅂ•́)و✧、( ⸝⸝> ᴗ <⸝⸝ )) 支援度。"""
    from cogs.table_renderer import extract_and_render_markdown_tables

    kaomoji_table = (
        "| 序號 | 狀態 | 代表顏文字 | 說明 |\n"
        "| :--- | :--- | :--- | :--- |\n"
        "| 1 | 開心 | ( ≧ ∇ ≦ )/ | 充滿元氣 |\n"
        "| 2 | 眼神死 | ( ¬_¬ ) | 神秘Bug |\n"
        "| 3 | 害羞 | ( ⸝⸝> ᴗ <⸝⸝ ) | 小扭呢 |\n"
        "| 4 | 思考 | ٩(•̀ㅂ•́)و✧ | 解決難題 |\n"
        "| 5 | 睏倦 | ( ¯ ꒳ ¯ ) ノ³ | 靈魂出竅 |\n"
    )

    clean_text, files = extract_and_render_markdown_tables(kaomoji_table)
    assert len(files) == 1
    assert files[0].filename == "table_1.png"
    assert len(files[0].fp.getvalue()) > 5000


def test_resolve_error_view_and_deduplication():
    """驗證 Fallback 逾時/失敗通報去重與已解決按鈕狀態。"""
    from cogs.ai import _unresolved_ai_error_keys, ResolveErrorView

    test_key = "測試異常類型_Timeout"
    _unresolved_ai_error_keys.discard(test_key)

    # 尚未解決時加入未解決集合
    _unresolved_ai_error_keys.add(test_key)
    assert test_key in _unresolved_ai_error_keys

    # 模擬按下『已解決』
    view = ResolveErrorView(test_key)
    assert view.timeout is None  # 不設 disable 時間

    # 觸發標記已解決
    _unresolved_ai_error_keys.discard(test_key)
    assert test_key not in _unresolved_ai_error_keys

