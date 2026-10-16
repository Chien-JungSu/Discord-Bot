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

    # 1. 檢驗首頁、指令頁、控制面板、登入提示頁均具備 site-topbar 與漢堡符號動畫結構
    for path in ['/', '/docs', '/panel']:
        res = client.get(path)
        assert res.status_code == 200
        html = res.data.decode('utf-8')
        assert 'site-topbar' in html
        assert 'site-menu-btn' in html
        assert 'hamburger-bar' in html
        assert 'topbar-brand' in html
        assert '炙彗JI器人' in html
        assert 'site-nav.js' in html

    # 檢驗 login_error.html
    res_err = client.get('/api/auth')
    assert res_err.status_code == 200
    err_html = res_err.data.decode('utf-8')
    assert 'site-topbar' in err_html
    assert 'site-menu-btn' in err_html
    assert 'hamburger-bar' in err_html
    assert 'site-nav.js' in err_html

    # 2. 檢驗 docs.html 專屬的側邊欄 UI 與邊緣熱區
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
    for path in ['/', '/docs', '/panel', '/api/auth']:
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

    # 1. 一般管理員：擁有伺服器選擇畫面、設定畫面包含返回按鈕與清除設定確認 Modal，但沒有管理員設定入口
    client.get('/api/auth/dev-login?role=admin')
    res_admin = client.get('/panel')
    assert res_admin.status_code == 200
    admin_html = res_admin.data.decode('utf-8')
    assert 'view-server-selection' in admin_html
    assert 'server-cards-grid' in admin_html
    assert 'view-server-settings' in admin_html
    assert 'btn-back-to-servers' in admin_html
    assert 'btn-sidebar-back' in admin_html
    assert 'modal-confirm-clear' in admin_html
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
    assert 'modal-confirm-clear' in owner_html



