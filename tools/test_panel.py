from cogs.web_server import app


def test_home_page():
    client = app.test_client()
    res = client.get('/')
    assert res.status_code == 200
    html = res.data.decode('utf-8')
    assert 'hero-top-row' in html
    assert '登入' in html
    assert 'top-nav-link' in html
    assert 'cta-links-row' in html
    assert 'top-right-nav' not in html
    assert '⚙️' not in html
    assert '(Control Panel)' not in html


def test_docs_page():
    client = app.test_client()
    res = client.get('/docs')
    assert res.status_code == 200
    html = res.data.decode('utf-8')
    assert 'header-top-right' in html
    assert '登入' in html
    assert 'top-nav-link' in html
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
    assert 'SERVER ADMIN' in html
    assert 'tab-owner' not in html
    assert '擁有者專屬控制面板' not in html
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

    # 1. 前端頁面：擁有者正常載入擁有者專區
    res_panel = client.get('/panel')
    html = res_panel.data.decode('utf-8')
    assert 'BOT OWNER' in html
    assert 'tab-owner' in html
    assert '擁有者專屬控制面板' in html

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
    assert 'docs-sidebar' in docs_html
    assert 'docs-sidebar-list' in docs_html


