import datetime
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

# 確保直接執行此檔案 (如 python cogs/web_server.py 或 IDE 單獨除錯) 時，
# 正確將專案根目錄加入模組搜尋路徑，避免 ModuleNotFoundError: No module named 'cogs'
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import discord
from flask import Flask, jsonify, render_template, url_for as flask_url_for
from waitress import serve

try:
    from cogs.sanitize import redact_secrets
except ModuleNotFoundError:
    from sanitize import redact_secrets

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static'),
)
# 模板自動重載：production 模式下 Jinja2 預設會快取第一次渲染的模板、之後不再
# 讀檔，導致改 templates/*.html 後要重啟機器人才會生效。開啟後每次渲染都會
# 檢查檔案修改時間，改完 HTML 重新整理網頁即可看到新版，不用重啟機器人。
# 成本只是每次渲染多一次 os.stat，對這種小流量儀表板可忽略。
app.config['TEMPLATES_AUTO_RELOAD'] = True

# 靜態檔案自動快取更新（Cache-Busting）：
# 1. SEND_FILE_MAX_AGE_DEFAULT = 0：告知瀏覽器與反向代理不要對靜態檔案進行長久強快取。
# 2. override_url_for context processor：自動在 static url_for 附加檔案最後修改時間戳記 (?v=mtime)。
#    當 CSS、JS 等靜態檔案重新上傳覆蓋後，mtime 自動改變，HTML 產生的網址也隨之改變。
#    瀏覽器會立即判定為新資源並下載最新內容，無需手動清除快取（免 Ctrl+F5），更無需重啟機器人服務。
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# Session 密鑰與 Cookie 安全設定（供控制面板 OAuth2 登入與權限管理使用）
app.secret_key = os.getenv('FLASK_SECRET_KEY') or os.getenv('SECRET_KEY') or 'ji-robot-control-panel-secret-key-2026'
app.config['SESSION_COOKIE_NAME'] = 'ji_robot_session'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

from cogs.panel_bp import panel_bp
app.register_blueprint(panel_bp)



@app.context_processor
def override_url_for():
    """覆寫 Jinja 模板內的 url_for，自動為 static 檔案附加 ?v=<mtime> 快取標記"""
    return dict(url_for=_versioned_url_for)


def _versioned_url_for(endpoint: str, **values) -> str:
    if endpoint == 'static':
        filename = values.get('filename')
        if filename and app.static_folder:
            file_path = os.path.join(app.static_folder, filename)
            if os.path.exists(file_path):
                values['v'] = int(os.stat(file_path).st_mtime)
    return flask_url_for(endpoint, **values)

# ---------- 機器人實際上線時間（以 Discord 連線為準） ----------
# 為什麼不用「行程啟動時間」：行程活著不等於機器人活著。discord.py 掉線後會
# 自動重連，若以模組匯入的時間當基準，重連後儀表板會顯示「已連續運行好幾天」，
# 明明中間斷線過。儀表板要的是「機器人實際連線了多久」，所以基準點綁在
# on_connect / on_disconnect（由 main.py 的事件呼叫），重連就歸零。
# 這個值由 Discord 事件執行緒寫入、waitress 工作執行緒讀取；單純的 float 指派
# 與讀取在 CPython 的 GIL 下是原子的，不需要額外加鎖。
_bot_connected_since: Optional[float] = None


def mark_bot_connected() -> None:
    """由 main.py 的 on_connect 事件呼叫：從現在開始計算上線時間。"""
    global _bot_connected_since
    _bot_connected_since = time.monotonic()


def mark_bot_disconnected() -> None:
    """由 main.py 的 on_disconnect 事件呼叫：標記為離線，上線時間暫停計算。"""
    global _bot_connected_since
    _bot_connected_since = None


def get_bot_uptime() -> tuple[Optional[float], bool]:
    """回傳 (已連線秒數, 是否在線)。尚未連線或已斷線時回傳 (None, False)。"""
    if _bot_connected_since is None:
        return None, False
    return time.monotonic() - _bot_connected_since, True


def _format_uptime_seconds(total_seconds: float) -> str:
    total_seconds = max(0, int(total_seconds))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}天, {hours}小時\n{minutes}分, {seconds}秒"


def _fetch_uptime_data():
    api_key = os.getenv('UPTIMEROBOT_API_KEY')
    monitor_id = os.getenv('UPTIMEROBOT_MONITOR_ID')

    if not api_key or not monitor_id:
        return {
            'status': 'unconfigured',
            'message': '尚未設定 UPTIMEROBOT_API_KEY / UPTIMEROBOT_MONITOR_ID',
        }

    # 修正: logs 只能是 0 或 1（是否回傳事件記錄），不是筆數。
    # 另外要拿 24h/7d/30d/90d 的正常運行率，正確參數是 custom_uptime_ratios
    # （輸入天數，用 "-" 分隔），custom_uptime_ranges 是另一個參數，
    # 格式是時間戳記區間（start_end-start_end...），傳天數進去會驗證失敗。
    payload = urllib.parse.urlencode({
        'api_key': api_key,
        'format': 'json',
        'monitors': monitor_id,
        'logs': 0,
        'custom_uptime_ratios': '1-7-30-90',
        'all_time_uptime_ratio': '1',
    }).encode('utf-8')

    try:
        req = urllib.request.Request(
            'https://api.uptimerobot.com/v2/getMonitors',
            data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
    except Exception as exc:  # pragma: no cover - runtime dependency on external API
        # 修正（M2）：這個 message 會原封不動回傳給任何未認證的網站訪客，
        # 不能把原始例外字串（可能含 URL、api_key 等）直接外流。
        print(f">>> UptimeRobot API 查詢失敗: {redact_secrets(exc)}")
        return {
            'status': 'error',
            'message': '監控服務暫時無法連線，請稍後再試。',
        }

    if data.get('stat') != 'ok':
        error_message = data.get('error', {}).get('message', 'UptimeRobot API 查詢失敗')
        print(f">>> UptimeRobot API 回應錯誤: {redact_secrets(error_message)}")
        return {
            'status': 'error',
            'message': '監控服務查詢失敗，請稍後再試。',
        }

    monitors = data.get('monitors') or []
    if not monitors:
        return {
            'status': 'error',
            'message': '找不到對應的 UptimeRobot Monitor',
        }

    monitor = monitors[0]

    # 修正: 請求參數是 custom_uptime_ratios，但 UptimeRobot 回傳的欄位名稱是
    # 單數的 custom_uptime_ratio，值是用 "-" 分隔的字串（依照請求順序對應 1-7-30-90）
    custom_ranges_raw = monitor.get('custom_uptime_ratio') or ''
    ranges_list = custom_ranges_raw.split('-') if custom_ranges_raw else []

    def _range_value(index: int) -> str:
        if index < len(ranges_list) and ranges_list[index]:
            return ranges_list[index]
        return '0'

    uptime = {
        '24h': _range_value(0),
        '7d': _range_value(1),
        '30d': _range_value(2),
        '90d': _range_value(3),
        'all': monitor.get('all_time_uptime_ratio') or '0',
    }

    status_text = {
        0: '暫停',
        1: '待命',
        2: '在線',
        8: '維護',
        9: '離線',
    }.get(monitor.get('status'), '未知')

    return {
        'status': 'ok',
        'friendly_name': monitor.get('friendly_name', '機器人監控'),
        'status_code': monitor.get('status'),
        'status_text': status_text,
        'uptime': uptime,
        'logs': monitor.get('logs') or [],
    }


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/docs')
def docs_page():
    return render_template('docs.html')


@app.route('/terms')
def terms_page():
    return render_template('terms.html')


@app.route('/privacy')
def privacy_page():
    return render_template('privacy.html')


# ---------- GitHub 最新 Commit 紀錄快取與解析 ----------
COMMITS_CACHE_FILE = os.path.join(BASE_DIR, 'commits_cache.json')
_commits_cache: dict[str, Any] = {
    'timestamp': 0.0,
    'commits': [],
}
_COMMITS_CACHE_TTL = 60.0  # 記憶體快取 60 秒


def _format_commit_info(
    full_hash: str,
    short_hash: str,
    raw_subject: str,
    author: str,
    date_str: str,
    iso_date: str = '',
    body: str = '',
) -> dict[str, Any]:
    """格式化單則 Commit 資訊，支援 Conventional Commits 前綴解析與相對時間計算。"""
    match = re.match(r'^([a-zA-Z]+)(?:\(([^)]+)\))?!?:\s*(.*)$', raw_subject)
    if match:
        c_type = match.group(1).lower()
        scope = match.group(2)
        display_subject = match.group(3)
    else:
        c_type = 'commit'
        scope = None
        display_subject = raw_subject

    type_meta = {
        'feat': {'label': '新增功能', 'tag': 'FEAT', 'cls': 'badge-feat'},
        'fix': {'label': '問題修復', 'tag': 'FIX', 'cls': 'badge-fix'},
        'docs': {'label': '文檔更新', 'tag': 'DOCS', 'cls': 'badge-docs'},
        'style': {'label': '介面優化', 'tag': 'STYLE', 'cls': 'badge-style'},
        'refactor': {'label': '重構調整', 'tag': 'REFACTOR', 'cls': 'badge-refactor'},
        'perf': {'label': '效能優化', 'tag': 'PERF', 'cls': 'badge-perf'},
        'chore': {'label': '專案維護', 'tag': 'CHORE', 'cls': 'badge-chore'},
    }.get(c_type, {'label': '提交更新', 'tag': c_type.upper() if len(c_type) <= 6 else 'COMMIT', 'cls': 'badge-commit'})

    relative_time = date_str
    if iso_date:
        try:
            clean_iso = iso_date.replace('Z', '+00:00')
            commit_dt = datetime.datetime.fromisoformat(clean_iso)
            now_dt = datetime.datetime.now(commit_dt.tzinfo)
            diff = max(0.0, (now_dt - commit_dt).total_seconds())
            if diff < 60:
                relative_time = '剛剛'
            elif diff < 3600:
                relative_time = f'{int(diff // 60)} 分鐘前'
            elif diff < 86400:
                relative_time = f'{int(diff // 3600)} 小時前'
            elif diff < 86400 * 30:
                relative_time = f'{int(diff // 86400)} 天前'
            elif diff < 86400 * 365:
                relative_time = f'{int(diff // (86400 * 30))} 個月前'
            else:
                relative_time = f'{int(diff // (86400 * 365))} 年前'
        except Exception:
            relative_time = date_str

    return {
        'full_hash': full_hash,
        'short_hash': short_hash,
        'subject': raw_subject,
        'display_subject': display_subject,
        'author': author,
        'date': date_str,
        'iso_date': iso_date,
        'relative_time': relative_time,
        'body': body,
        'type': c_type,
        'scope': scope,
        'badge_label': type_meta['label'],
        'badge_tag': type_meta['tag'],
        'badge_cls': type_meta['cls'],
        'github_url': f'https://github.com/Chien-JungSu/Discord-Bot/commit/{full_hash}',
    }


def _parse_commit_record(record: str) -> Optional[dict[str, Any]]:
    """解析由本機 git log --format 輸出的單行 commit 紀錄。"""
    parts = record.strip().split('\x1f')
    if len(parts) < 5:
        return None
    full_hash = parts[0].strip()
    short_hash = parts[1].strip()
    raw_subject = parts[2].strip()
    author = parts[3].strip()
    date_str = parts[4].strip()
    iso_date = parts[5].strip() if len(parts) > 5 else ''
    body = parts[6].strip() if len(parts) > 6 else ''

    return _format_commit_info(
        full_hash=full_hash,
        short_hash=short_hash,
        raw_subject=raw_subject,
        author=author,
        date_str=date_str,
        iso_date=iso_date,
        body=body,
    )


def _parse_github_api_commit(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """解析由 GitHub REST API /commits 回傳的單筆 commit 紀錄。"""
    full_hash = item.get('sha', '').strip()
    if not full_hash:
        return None
    short_hash = full_hash[:7]
    commit_data = item.get('commit') or {}
    msg = commit_data.get('message') or ''
    lines = msg.splitlines()
    raw_subject = lines[0].strip() if lines else ''
    body = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ''

    author_obj = commit_data.get('author') or {}
    author = author_obj.get('name') or (item.get('author') or {}).get('login') or 'GitHub User'
    iso_date = author_obj.get('date', '')

    date_str = iso_date
    if iso_date:
        try:
            dt = datetime.datetime.fromisoformat(iso_date.replace('Z', '+00:00')).astimezone()
            date_str = dt.strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            pass

    return _format_commit_info(
        full_hash=full_hash,
        short_hash=short_hash,
        raw_subject=raw_subject,
        author=author,
        date_str=date_str,
        iso_date=iso_date,
        body=body,
    )


def _is_cache_commit(commit: dict[str, Any]) -> bool:
    """判斷該 commit 是否為維護 commits_cache.json 的提交，以在網頁 /commit 頁面中自動忽略。"""
    subject = str(commit.get('subject') or '').lower()
    display_subject = str(commit.get('display_subject') or '').lower()
    body = str(commit.get('body') or '').lower()
    return (
        'commits_cache' in subject
        or 'commits_cache' in display_subject
        or 'commits_cache' in body
    )


def _fetch_commits_from_local_git(limit: int = 10) -> list[dict[str, Any]]:
    """嘗試從本機 .git 目錄執行 git log 取得 commit 紀錄（自動忽略 commits_cache.json 相關提交）。"""
    git_dir = os.path.join(BASE_DIR, '.git')
    # 若本機非 git 儲存庫（如雲端主機/容器部屬未攜帶 .git），直接返回空清單，避免拋出 fatal 錯誤
    if not os.path.exists(git_dir):
        return []

    try:
        # 讀取較多筆數以確保過濾 commits_cache 後仍有足夠數量的有效 commit
        fetch_limit = max(50, limit * 4)
        res = subprocess.run(
            [
                'git',
                'log',
                '-n',
                str(fetch_limit),
                '--format=%H%x1f%h%x1f%s%x1f%an%x1f%ad%x1f%aI%x1f%b%x1e',
                '--date=format:%Y-%m-%d %H:%M:%S',
            ],
            cwd=BASE_DIR,
            capture_output=True,
            encoding='utf-8',
            errors='replace',
            timeout=5.0,
        )
        if res.returncode != 0:
            return []

        commits: list[dict[str, Any]] = []
        for record in res.stdout.strip('\x1e\n').split('\x1e'):
            if not record.strip():
                continue
            item = _parse_commit_record(record)
            if item and not _is_cache_commit(item):
                commits.append(item)
                if len(commits) >= limit:
                    break
        return commits
    except Exception:
        return []


def _fetch_commits_from_github_api(limit: int = 10) -> list[dict[str, Any]]:
    """當伺服器無本機 .git 目錄時，透過 GitHub 官方公開 REST API 取得最新 commit（自動忽略 commits_cache.json）。"""
    try:
        fetch_limit = max(50, limit * 4)
        url = f"https://api.github.com/repos/Chien-JungSu/Discord-Bot/commits?per_page={fetch_limit}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (compatible; DiscordBot-CommitFetcher/1.0)',
            'Accept': 'application/vnd.github.v3+json',
        }
        github_token = os.getenv('GITHUB_TOKEN', '').strip()
        if github_token:
            headers['Authorization'] = f"token {github_token}"

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode('utf-8'))
                commits: list[dict[str, Any]] = []
                for item in data:
                    parsed = _parse_github_api_commit(item)
                    if parsed and not _is_cache_commit(parsed):
                        commits.append(parsed)
                        if len(commits) >= limit:
                            break
                return commits
    except Exception:
        pass
    return []


def _load_disk_cached_commits() -> list[dict[str, Any]]:
    """從硬碟備援檔案讀取最後一次成功的 commit 快取（自動過濾 commits_cache.json 相關項目）。"""
    if os.path.isfile(COMMITS_CACHE_FILE):
        try:
            with open(COMMITS_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [c for c in data if not _is_cache_commit(c)]
        except Exception:
            pass
    return []


def _save_disk_cached_commits(commits: list[dict[str, Any]]) -> None:
    """將成功的 commit 快取持久化至硬碟。"""
    if not commits:
        return
    try:
        with open(COMMITS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(commits, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_latest_commits(limit: int = 10) -> list[dict[str, Any]]:
    """讀取最新 commit 紀錄：三層容錯機制 (記憶體快取 -> 本機 Git -> GitHub API 備援 -> 硬碟持久快取)。"""
    global _commits_cache
    now = time.monotonic()
    if _commits_cache['commits'] and (now - _commits_cache['timestamp'] < _COMMITS_CACHE_TTL):
        return _commits_cache['commits'][:limit]

    commits: list[dict[str, Any]] = []

    # 1. 優先嘗試本機 git log（若存在 .git 目錄）
    commits = _fetch_commits_from_local_git(limit)

    # 2. 本機非 git 儲存庫或讀取失敗時，無縫切換至 GitHub REST API
    if not commits:
        commits = _fetch_commits_from_github_api(limit)

    # 3. 網路異常或受限時，讀取硬碟快取備援
    if not commits:
        commits = _load_disk_cached_commits()

    # 4. 更新快取
    if commits:
        _commits_cache['timestamp'] = now
        _commits_cache['commits'] = commits
        _save_disk_cached_commits(commits)
        return commits[:limit]

    # 若皆無資料，設定冷卻時間避免頻繁重複嘗試
    _commits_cache['timestamp'] = now
    return _commits_cache['commits'][:limit]


@app.route('/commit')
@app.route('/commits')
def commits_page():
    commits = [c for c in get_latest_commits(10) if not _is_cache_commit(c)]
    return render_template('commit.html', commits=commits)


@app.route('/logo.png')
def root_logo():
    return app.send_static_file('logo.png')

@app.route('/health')
def health_check():
    bot = app.config.get('BOT')
    if bot and bot.is_ready() and not bot.is_closed():
        return jsonify({
            "status": "online",
            "bot_user": str(bot.user),
            "latency_ms": round(bot.latency * 1000) if bot.latency else None,
            "guilds": len(bot.guilds)
        }), 200
    else:
        return jsonify({
            "status": "offline",
            "error": "Discord bot is disconnected or not ready"
        }), 503


@app.route('/api/bot-stats')
def bot_stats_api():
    bot = app.config.get('BOT')
    if bot is None:
        return jsonify({
            'status': 'unavailable',
            'message': 'Discord bot 尚未註冊到 Web 伺服器',
        })

    latency_value = getattr(bot, 'latency', None)
    if latency_value is None or not math.isfinite(latency_value):
        latency_ms = 0
    else:
        latency_ms = int(round(latency_value * 1000))

    total_servers = len(bot.guilds)
    # 修正: guild.member_count 在缺少 Server Members Intent 或快取尚未就緒時可能是 None，
    # 直接 sum() 會丟 TypeError 導致整支 API 回傳 500，前端因此全部顯示 "--"
    total_users = sum((guild.member_count or 0) for guild in bot.guilds)
    # 上線時間 = 這次 Discord 連線持續了多久（不是行程跑了多久）
    uptime_seconds, connected = get_bot_uptime()
    uptime_text = _format_uptime_seconds(uptime_seconds) if connected else '離線中'
    version = os.getenv('APP_VERSION') or getattr(bot, 'version', None) or discord.__version__

    return jsonify({
        'status': 'ok',
        'guilds': total_servers,
        'users': total_users,
        'latency_ms': latency_ms,
        'connected': connected,
        'uptime_seconds': uptime_seconds if connected else 0,
        'uptime_text': uptime_text,
        'version': version,
    })


@app.route('/api/uptime')
def uptime_api():
    return jsonify(_fetch_uptime_data())


def run():
    """以 production WSGI server（waitress）啟動網頁服務。

    修正（H1）：原本用 Flask 內建的 Werkzeug 開發伺服器直接對外服務，
    官方明確警告不適合生產環境（無 slowloris 防護、連線管理脆弱）。
    改用 waitress 後監聽行為不變：照樣綁 0.0.0.0 與 PORT（託管面板
    綁定 0.0.0.0:20198 的部署方式不受影響）。
    """
    port = int(os.environ.get('PORT', 20198))
    print(f"[Web] 伺服器已啟動，請開啟瀏覽器造訪: http://127.0.0.1:{port} 或 http://localhost:{port}", flush=True)
    serve(app, host='0.0.0.0', port=port, threads=8)


def start_web_server():
    """Start the Flask web server in a background thread for local bot use."""
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


if __name__ == '__main__':
    run()
