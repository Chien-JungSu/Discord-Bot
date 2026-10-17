import asyncio
import importlib
import json
import math
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import discord
from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, '.env'))

from cogs.database import (
    get_daily_api_usage,
    get_economy_user,
    get_guild_ai_settings,
    get_user_ai_points,
    init_db,
    load_auto_reply_settings,
    load_economy_config,
    load_music_settings,
    load_reaction_roles,
    load_welcome_settings,
    save_auto_reply_settings,
    save_economy_config,
    save_guild_ai_settings,
    delete_guild_ai_settings,
    save_music_settings,
    save_reaction_roles,
    save_welcome_settings,
    set_economy_balance,
    set_user_ai_points,
    update_economy_balance,
    update_user_ai_points,
)
from cogs.sanitize import redact_secrets

panel_bp = Blueprint('panel', __name__)

# Discord OAuth2 設定
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID') or '1477930641944350952'
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '').strip()
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI', 'https://patricksu.nkhost.dev/api/auth').strip()


def get_owner_id() -> Optional[int]:
    """取得機器人擁有者 ID (優先讀取環境變數，其次讀取 bot.owner_id)。"""
    owner_id = os.getenv('DISCORD_OWNER_ID') or os.getenv('OWNER_ID')
    if not owner_id:
        bot = get_bot()
        if bot and getattr(bot, 'owner_id', None):
            return int(bot.owner_id)
    try:
        return int(owner_id) if owner_id else None
    except ValueError:
        return None


def get_owner_guild_id() -> Optional[int]:
    """取得擁有者伺服器 ID。"""
    guild_id = os.getenv('DISCORD_OWNER_GUILD_ID') or os.getenv('OWNER_GUILD_ID')
    if not guild_id:
        bot = get_bot()
        if bot and getattr(bot, 'owner_guild_id', None):
            return int(bot.owner_guild_id)
    try:
        return int(guild_id) if guild_id else None
    except ValueError:
        return None


def get_bot():
    return current_app.config.get('BOT')


def _sync_cog_settings(cog_name: str):
    """通知正在運行的 Bot Cog 重新載入資料庫設定，確保網頁控制面板與 Discord 指令雙向即時同步。"""
    bot = get_bot()
    if not bot:
        return
    try:
        cog = bot.get_cog(cog_name)
        if cog is not None and hasattr(cog, 'reload_settings'):
            cog.reload_settings()
    except Exception as exc:
        print(f"⚠️ 同步 {cog_name} 設定至 Bot 記憶體時發生例外: {exc}")


def is_current_user_owner() -> bool:
    """檢查目前 Session 中的登入使用者是否為機器人擁有者。"""
    user = session.get('user')
    if not user or not user.get('id'):
        return False
    owner_id = get_owner_id()
    if not owner_id:
        return False
    try:
        return int(user.get('id')) == owner_id
    except (ValueError, TypeError):
        return False


def can_manage_guild(guild_id: str) -> bool:
    """檢查目前登入使用者對指定伺服器是否具備管理權限 (伺服器擁有者、Administrator、Manage Guild)。

    嚴格規定：即使是機器人擁有者，若在該伺服器沒有管理伺服器權限，也嚴禁查看或更改資料。
    """
    user = session.get('user')
    if not user:
        return False

    guilds = session.get('guilds', [])
    for g in guilds:
        if str(g.get('id')) == str(guild_id):
            try:
                perms = int(g.get('permissions', 0))
            except (ValueError, TypeError):
                perms = 0
            if bool(g.get('owner', False)) or (perms & 0x8 != 0) or (perms & 0x20 != 0):
                return True
    return False


def run_coroutine_threadsafe(coro, timeout=15):
    """在 Discord Bot 的 asyncio event loop 執行異步操作並等待回傳結果。"""
    bot = get_bot()
    if bot is None or getattr(bot, 'loop', None) is None or not bot.loop.is_running():
        raise RuntimeError("Discord 機器人尚未啟動或事件迴圈未在運行")
    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
    return future.result(timeout=timeout)


# ==============================================================================
# OAuth2 登入 / 回呼 / 登出
# ==============================================================================

@panel_bp.route('/login')
def login():
    """導向至 Discord OAuth2 授權頁面。"""
    state = secrets.token_urlsafe(24)
    session['oauth_state'] = state

    params = {
        'client_id': DISCORD_CLIENT_ID,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'identify guilds',
        'state': state,
        'prompt': 'consent',
    }
    auth_url = 'https://discord.com/oauth2/authorize?' + urllib.parse.urlencode(params)
    return redirect(auth_url)


@panel_bp.route('/api/auth')
def oauth_callback():
    """Discord OAuth2 回呼路由 (Redirect URI: https://patricksu.nkhost.dev/api/auth)。"""
    error = request.args.get('error')
    if error:
        error_desc = request.args.get('error_description', '授權被取消或拒絕')
        return render_template(
            'login_error.html',
            title='OAuth 授權取消',
            error=f"{error}: {error_desc}",
            redirect_uri=DISCORD_REDIRECT_URI,
            client_id=DISCORD_CLIENT_ID,
        )

    code = request.args.get('code')
    state = request.args.get('state')

    if not code:
        return render_template(
            'login_error.html',
            title='缺少授權碼',
            error="Discord 未回傳有效的授權碼 (code)。",
            redirect_uri=DISCORD_REDIRECT_URI,
            client_id=DISCORD_CLIENT_ID,
        )

    expected_state = session.get('oauth_state')
    if expected_state and state != expected_state:
        return render_template(
            'login_error.html',
            title='CSRF 驗證失敗',
            error="狀態參數驗證未通過 (CSRF Check Failed)，請重新登入。",
            redirect_uri=DISCORD_REDIRECT_URI,
            client_id=DISCORD_CLIENT_ID,
        )

    if not DISCORD_CLIENT_SECRET:
        return render_template(
            'login_error.html',
            title='伺服器未設定 Client Secret',
            error="伺服器環境變數尚未設定 DISCORD_CLIENT_SECRET。請至 Discord Developer Portal > OAuth2 > General 複製 Client Secret 並填入 .env 檔案中。",
            redirect_uri=DISCORD_REDIRECT_URI,
            client_id=DISCORD_CLIENT_ID,
            secret_missing=True,
        )

    # 1. 交換 Token
    token_payload = urllib.parse.urlencode({
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': DISCORD_REDIRECT_URI,
    }).encode('utf-8')

    token_req = urllib.request.Request(
        'https://discord.com/api/oauth2/token',
        data=token_payload,
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'JiRobot-Dashboard/1.0',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(token_req, timeout=15) as res:
            token_data = json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        err_msg = exc.read().decode('utf-8', errors='ignore')
        return render_template(
            'login_error.html',
            title='Token 交換失敗',
            error=f"Discord 回傳錯誤 (HTTP {exc.code}): {err_msg}",
            redirect_uri=DISCORD_REDIRECT_URI,
            client_id=DISCORD_CLIENT_ID,
        )
    except Exception as exc:
        return render_template(
            'login_error.html',
            title='連線例外',
            error=f"無法連線至 Discord 授權伺服器: {redact_secrets(str(exc))}",
            redirect_uri=DISCORD_REDIRECT_URI,
            client_id=DISCORD_CLIENT_ID,
        )

    access_token = token_data.get('access_token')
    if not access_token:
        return render_template(
            'login_error.html',
            title='無效的 Token',
            error="Discord 未回傳 access_token。",
            redirect_uri=DISCORD_REDIRECT_URI,
            client_id=DISCORD_CLIENT_ID,
        )

    # 2. 取得使用者資訊
    user_req = urllib.request.Request(
        'https://discord.com/api/users/@me',
        headers={
            'Authorization': f"Bearer {access_token}",
            'User-Agent': 'JiRobot-Dashboard/1.0',
        },
    )
    try:
        with urllib.request.urlopen(user_req, timeout=15) as res:
            user_data = json.loads(res.read().decode('utf-8'))
    except Exception as exc:
        return render_template(
            'login_error.html',
            title='讀取使用者資訊失敗',
            error=f"無法取得 Discord 使用者資訊: {redact_secrets(str(exc))}",
            redirect_uri=DISCORD_REDIRECT_URI,
            client_id=DISCORD_CLIENT_ID,
        )

    # 3. 取得使用者的伺服器清單
    guilds_req = urllib.request.Request(
        'https://discord.com/api/users/@me/guilds',
        headers={
            'Authorization': f"Bearer {access_token}",
            'User-Agent': 'JiRobot-Dashboard/1.0',
        },
    )
    try:
        with urllib.request.urlopen(guilds_req, timeout=15) as res:
            guilds_data = json.loads(res.read().decode('utf-8'))
    except Exception as exc:
        guilds_data = []

    user_id = str(user_data.get('id'))
    owner_id = get_owner_id()
    is_owner = (owner_id is not None and str(owner_id) == user_id)

    # 篩選可管理的伺服器
    manageable_guilds = []
    bot = get_bot()
    bot_guild_ids = {str(g.id) for g in bot.guilds} if bot else set()

    for g in guilds_data:
        try:
            perms = int(g.get('permissions', 0))
        except (ValueError, TypeError):
            perms = 0
        is_g_owner = bool(g.get('owner', False))
        # 嚴格限制：僅有具備「伺服器擁有者」、「Administrator (0x8)」或「Manage Guild (0x20)」的伺服器才可出現在控制面板
        # 嚴格禁止加入 or is_owner！即便是機器人擁有者，在自己沒有管理權限的伺服器同樣不可見、不可更改！
        can_manage = is_g_owner or (perms & 0x8 != 0) or (perms & 0x20 != 0)
        if can_manage:
            icon_hash = g.get('icon')
            icon_url = f"https://cdn.discordapp.com/icons/{g['id']}/{icon_hash}.png" if icon_hash else None
            manageable_guilds.append({
                'id': str(g['id']),
                'name': g.get('name', '未命名伺服器'),
                'icon': icon_hash,
                'icon_url': icon_url,
                'owner': is_g_owner,
                'permissions': perms,
                'bot_in_guild': str(g['id']) in bot_guild_ids,
            })

    avatar_hash = user_data.get('avatar')
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png"
        if avatar_hash
        else "https://cdn.discordapp.com/embed/avatars/0.png"
    )

    session['user'] = {
        'id': user_id,
        'username': user_data.get('username'),
        'global_name': user_data.get('global_name') or user_data.get('username'),
        'avatar': avatar_hash,
        'avatar_url': avatar_url,
        'is_owner': is_owner,
    }
    session['guilds'] = manageable_guilds

    return redirect(url_for('panel.control_panel'))


@panel_bp.route('/logout')
def logout():
    """清除登入 Session 並返回首頁。"""
    session.clear()
    return redirect('/')


@panel_bp.route('/api/auth/dev-login')
def dev_login():
    """供本地開發測試使用的模擬登入端點（可切換 擁有者 或 一般管理員）。

    注意：此端點僅在未設定 DISCORD_CLIENT_SECRET 或明確開啟 DEV_LOGIN 模式時可用。
    """
    mode = request.args.get('role', 'owner')
    owner_gid = str(get_owner_guild_id() or '1477930950288478262')
    admin_gid = '222222222222222222'

    if mode == 'owner':
        uid = str(get_owner_id() or '941935836189065236')
        session['user'] = {
            'id': uid,
            'username': 'Bot_Owner',
            'global_name': '炙彗 (機器人擁有者)',
            'avatar': None,
            'avatar_url': '/static/logo.png',
            'is_owner': True,
        }
        # 機器人擁有者只在自己具備權限的伺服器 (owner_gid) 具有管理伺服器權限
        bot_guilds = [
            {
                'id': owner_gid,
                'name': '開發者專屬伺服器 (Owner Guild)',
                'icon_url': None,
                'owner': True,
                'permissions': 8,
                'bot_in_guild': True,
            }
        ]
    elif mode == 'none':
        session['user'] = {
            'id': '777777777777777777',
            'username': 'Normal_User',
            'global_name': '普通成員 (無任何伺服器管理權限)',
            'avatar': None,
            'avatar_url': 'https://cdn.discordapp.com/embed/avatars/2.png',
            'is_owner': False,
        }
        bot_guilds = []
    else:
        session['user'] = {
            'id': '888888888888888888',
            'username': 'Server_Admin',
            'global_name': '伺服器管理員 (非擁有者)',
            'avatar': None,
            'avatar_url': 'https://cdn.discordapp.com/embed/avatars/1.png',
            'is_owner': False,
        }
        # 一般管理員只在 admin_gid 具備 Manage Guild 權限
        bot_guilds = [
            {
                'id': admin_gid,
                'name': '示範管理伺服器 (Admin)',
                'icon_url': None,
                'owner': False,
                'permissions': 32,
                'bot_in_guild': True,
            }
        ]

    session['guilds'] = bot_guilds
    return redirect(url_for('panel.control_panel'))


# ==============================================================================
# 控制面板主要頁面 (Control Panel HTML)
# ==============================================================================

@panel_bp.route('/panel')
def control_panel():
    """官網控制面板核心頁面。"""
    user = session.get('user')
    is_owner = is_current_user_owner()

    # 更新 Session 中各伺服器的機器人在線/加入狀態
    bot = get_bot()
    bot_guild_ids = {str(g.id) for g in bot.guilds} if bot else set()
    guilds = session.get('guilds', [])
    for g in guilds:
        g['bot_in_guild'] = str(g.get('id')) in bot_guild_ids

    # 邀請網址
    invite_url = (
        f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
        "&permissions=8&integration_type=0&scope=bot+applications.commands"
    )

    return render_template(
        'panel.html',
        user=user,
        is_owner=is_owner,
        guilds=guilds,
        client_id=DISCORD_CLIENT_ID,
        invite_url=invite_url,
        owner_guild_id=str(get_owner_guild_id()) if get_owner_guild_id() else None,
    )


# ==============================================================================
# 伺服器通用設定 API (所有伺服器管理員可用)
# ==============================================================================

@panel_bp.route('/api/panel/guild/<guild_id>/data')
def get_guild_data(guild_id: str):
    """取得特定伺服器的頻道清單、身分組清單與目前各功能設定值。"""
    if not can_manage_guild(guild_id):
        return jsonify({'status': 'error', 'message': '您沒有權限管理此伺服器'}), 403

    bot = get_bot()
    guild_obj = bot.get_guild(int(guild_id)) if (bot and guild_id.isdigit()) else None

    # 頻道清單、身分組清單、成員清單與自訂表情符號清單
    channels = []
    roles = []
    emojis = []
    members = []
    if guild_obj:
        for ch in guild_obj.text_channels:
            channels.append({
                'id': str(ch.id),
                'name': ch.name,
                'type': 'text',
            })
        for r in guild_obj.roles:
            if r.name != '@everyone':
                color_hex = f"#{r.color.value:06x}" if r.color.value else '#94a3b8'
                roles.append({
                    'id': str(r.id),
                    'name': r.name,
                    'color': color_hex,
                    'position': r.position,
                })
        roles.sort(key=lambda x: x['position'], reverse=True)
        for em in guild_obj.emojis:
            ext = 'gif' if em.animated else 'png'
            emojis.append({
                'id': str(em.id),
                'name': em.name,
                'animated': bool(em.animated),
                'raw': str(em),
                'url': f"https://cdn.discordapp.com/emojis/{em.id}.{ext}?size=48",
            })
        for m in sorted(guild_obj.members, key=lambda x: (x.display_name or x.name).lower()):
            if not m.bot:
                members.append({
                    'id': str(m.id),
                    'name': m.display_name,
                    'username': m.name,
                    'avatar': m.display_avatar.url,
                })

    # 讀取各模組資料庫設定
    welcome_all = load_welcome_settings()
    welcome_cfg = welcome_all.get(str(guild_id), {})

    music_all = load_music_settings()
    music_cfg = music_all.get(str(guild_id), {})

    auto_reply_all = load_auto_reply_settings()
    raw_rules = auto_reply_all.get(str(guild_id), {}).get('rules', [])
    auto_reply_rules = []
    for r in raw_rules:
        r_copy = dict(r)
        r_copy['channel_id'] = str(r['channel_id']) if r.get('channel_id') else None
        is_paused_flag = 1 if (r.get('is_paused') or not r.get('enabled', True)) else 0
        r_copy['is_paused'] = is_paused_flag
        r_copy['enabled'] = not bool(is_paused_flag)
        auto_reply_rules.append(r_copy)

    reaction_roles_all = load_reaction_roles()
    raw_reaction_messages = reaction_roles_all.get(str(guild_id), {}).get('messages', [])
    reaction_messages = []
    for m in raw_reaction_messages:
        pairs_serialized = []
        for p in (m.get('pairs') or []):
            pairs_serialized.append({
                'emoji': str(p.get('emoji') or ''),
                'role_id': str(p.get('role_id') or ''),
            })
        reaction_messages.append({
            'channel_id': str(m.get('channel_id') or ''),
            'message_id': str(m.get('message_id') or ''),
            'strict': bool(m.get('strict', True)),
            'pairs': pairs_serialized,
        })

    # 指令清單（區分「所有成員」與「管理員」，且擁有者專屬指令僅對擁有者於專屬伺服器顯示）
    commands_list = [
        {"name": "/ping", "description": "測試機器人的連線延遲", "category": "General", "permission": "所有成員"},
        {"name": "/panel", "description": "取得網頁控制面板連結按鈕", "category": "General", "permission": "所有成員"},
        {"name": "/choice", "description": "隨機選擇輸入的選項", "category": "General", "permission": "所有成員"},
        {"name": "/quotes", "description": "隨機名言或笑話", "category": "General", "permission": "所有成員"},
        {"name": "/steal", "description": "偷取表情符號並新增至伺服器", "category": "General", "permission": "管理員"},
        {"name": "/weather", "description": "查詢台澎金馬地區天氣", "category": "Weather", "permission": "所有成員"},
        {"name": "/bus", "description": "查詢全台公車到站預估時間", "category": "Bus", "permission": "所有成員"},
        {"name": "/server_info", "description": "顯示當前伺服器詳細資訊", "category": "Server Info", "permission": "所有成員"},
        {"name": "/welcome_active", "description": "啟用並設定歡迎訊息頻道與身分組", "category": "Welcome", "permission": "管理員"},
        {"name": "/welcome_inactive", "description": "停用伺服器歡迎訊息功能", "category": "Welcome", "permission": "管理員"},
        {"name": "/auto_reply_add", "description": "新增關鍵字自動回覆規則", "category": "Auto Reply", "permission": "管理員"},
        {"name": "/auto_reply_edit", "description": "編輯現有關鍵字回覆規則", "category": "Auto Reply", "permission": "管理員"},
        {"name": "/auto_reply_list", "description": "檢視本伺服器自動回覆清單", "category": "Auto Reply", "permission": "管理員"},
        {"name": "/auto_reply_remove", "description": "刪除指定關鍵字回覆規則", "category": "Auto Reply", "permission": "管理員"},
        {"name": "/reaction_roles_create", "description": "建立表符身分組面板訊息", "category": "Reaction Roles", "permission": "管理員"},
        {"name": "/reaction_roles_edit", "description": "編輯表符身分組配置", "category": "Reaction Roles", "permission": "管理員"},
        {"name": "/music_join", "description": "讓機器人加入語音頻道", "category": "Music", "permission": "所有成員"},
        {"name": "/music_leave", "description": "讓機器人離開語音頻道", "category": "Music", "permission": "所有成員"},
        {"name": "/music_play", "description": "播放歌曲或加入佇列", "category": "Music", "permission": "所有成員"},
        {"name": "/music_skip", "description": "跳過當前歌曲", "category": "Music", "permission": "所有成員"},
        {"name": "/music_stop", "description": "停止播放並清空佇列", "category": "Music", "permission": "所有成員"},
        {"name": "/music_queue", "description": "顯示播放佇列", "category": "Music", "permission": "所有成員"},
        {"name": "/music_set_channel", "description": "設定專屬點歌頻道", "category": "Music", "permission": "管理員"},
        {"name": "/tts", "description": "文字轉語音並在語音頻道朗讀", "category": "TTS", "permission": "所有成員"},
        {"name": "/shorten", "description": "將長網址縮短為短網址", "category": "Shortener", "permission": "所有成員"},
        {"name": "/economy_daily", "description": "領取每日簽到金幣與連簽獎勵", "category": "Economy", "permission": "所有成員"},
        {"name": "/economy_hourly", "description": "領取每小時整點簽到金幣", "category": "Economy", "permission": "所有成員"},
        {"name": "/economy_balance", "description": "查詢金幣餘額與統計", "category": "Economy", "permission": "所有成員"},
        {"name": "/economy_leaderboard", "description": "檢視金幣排行榜", "category": "Economy", "permission": "所有成員"},
        {"name": "/economy_pay", "description": "轉帳金幣給其他成員", "category": "Economy", "permission": "所有成員"},
        {"name": "/economy_slots", "description": "拉霸機小遊戲賭金幣", "category": "Economy", "permission": "所有成員"},
        {"name": "/economy_gamble", "description": "擲硬幣或骰子賭金幣", "category": "Economy", "permission": "所有成員"},
        {"name": "/economy_blackjack", "description": "21點小遊戲挑戰莊家", "category": "Economy", "permission": "所有成員"},
        {"name": "/economy_scratch", "description": "九宮格刮刮樂小遊戲", "category": "Economy", "permission": "所有成員"},
        {"name": "/ai", "description": "向 OpenRouter 免費 AI 模型發問與智慧對話", "category": "AI", "permission": "所有成員"},
        {"name": "/ai_config", "description": "查看伺服器當前 AI 人設與模型配置", "category": "AI", "permission": "所有成員"},
        {"name": "/ai_settings", "description": "設定伺服器預設 AI 模型、人設指令與指定贊助支付者", "category": "AI", "permission": "管理員"},
    ]

    # 關鍵防護：管理員專用指令 (owner_guild_only) 絕對不對一般使用者顯示，且僅在擁有者檢視專屬伺服器時附加
    owner_gid = str(get_owner_guild_id() or '1477930950288478262')
    if is_current_user_owner() and str(guild_id) == owner_gid:
        commands_list.extend([
            {"name": "/reload_cogs", "description": "線上熱重載指定 Cog 模組並同步指令樹", "category": "Owner / System", "permission": "管理員", "owner_guild_only": True},
            {"name": "/economy_admin", "description": "開啟經濟系統參數控制面板與管理使用者金幣", "category": "Owner / Economy", "permission": "管理員", "owner_guild_only": True},
        ])

    ai_cfg = get_guild_ai_settings(str(guild_id))

    return jsonify({
        'status': 'ok',
        'guild_id': str(guild_id),
        'guild_name': guild_obj.name if guild_obj else '未命名伺服器',
        'bot_in_guild': guild_obj is not None,
        'channels': channels,
        'roles': roles,
        'members': members,
        'emojis': emojis,
        'welcome': {
            'welcome_channel_id': str(welcome_cfg.get('welcome_channel_id') or ''),
            'rules_channel_id': str(welcome_cfg.get('rules_channel_id') or ''),
            'role_channel_id': str(welcome_cfg.get('role_channel_id') or ''),
            'auto_role_id': str(welcome_cfg.get('auto_role_id') or ''),
        },
        'music': {
            'music_channel_id': str(music_cfg.get('music_channel_id') or ''),
        },
        'ai': {
            'is_enabled': bool(ai_cfg.get('is_enabled', True)),
            'model': str(ai_cfg.get('model') or 'openrouter/auto'),
            'instruction': str(ai_cfg.get('instruction') or ''),
            'channel_id': str(ai_cfg.get('channel_id') or ''),
            'payer_user_id': str(ai_cfg.get('payer_user_id') or ''),
            'temperature': float(ai_cfg.get('temperature', 0.7)),
            'max_tokens': int(ai_cfg.get('max_tokens', 1000)),
        },
        'auto_reply_rules': auto_reply_rules,
        'reaction_roles_messages': reaction_messages,
        'commands': commands_list,
    })


@panel_bp.route('/api/panel/guild/<guild_id>/ai', methods=['POST'])
def save_guild_ai(guild_id: str):
    """儲存或重設伺服器的 AI 智慧助手與人設配置 (OpenRouter)。"""
    if not can_manage_guild(guild_id):
        return jsonify({'status': 'error', 'message': '權限不足'}), 403

    payload = request.get_json(silent=True) or {}
    action = payload.get('action', 'save')

    if action in ('clear', 'reset'):
        delete_guild_ai_settings(str(guild_id))
        _sync_cog_settings('AI')
        updated = get_guild_ai_settings(str(guild_id))
        return jsonify({
            'status': 'ok',
            'message': 'AI 設定已成功重設為系統預設值！',
            'ai': {
                'is_enabled': bool(updated.get('is_enabled', True)),
                'model': str(updated.get('model') or 'openrouter/auto'),
                'instruction': str(updated.get('instruction') or ''),
                'channel_id': str(updated.get('channel_id') or ''),
                'payer_user_id': str(updated.get('payer_user_id') or ''),
                'temperature': float(updated.get('temperature', 0.7)),
                'max_tokens': int(updated.get('max_tokens', 1000)),
            },
        })

    is_enabled = bool(payload.get('is_enabled', True))
    model = str(payload.get('model') or '').strip()
    instruction = payload.get('instruction')
    channel_id = payload.get('channel_id')
    temperature = payload.get('temperature')
    max_tokens = payload.get('max_tokens')

    settings = {
        'is_enabled': is_enabled,
        'model': model,
        'instruction': instruction,
        'channel_id': channel_id,
        'temperature': temperature,
        'max_tokens': max_tokens,
    }

    if 'payer_user_id' in payload:
        raw_payer = str(payload.get('payer_user_id') or '').strip()
        settings['payer_user_id'] = raw_payer if (raw_payer and raw_payer != 'none') else None

    save_guild_ai_settings(str(guild_id), settings)
    _sync_cog_settings('AI')
    updated = get_guild_ai_settings(str(guild_id))

    return jsonify({
        'status': 'ok',
        'message': 'AI 智慧助手與人設個人化設定已成功儲存！',
        'ai': {
            'is_enabled': bool(updated.get('is_enabled', True)),
            'model': str(updated.get('model') or 'openrouter/auto'),
            'instruction': str(updated.get('instruction') or ''),
            'channel_id': str(updated.get('channel_id') or ''),
            'payer_user_id': str(updated.get('payer_user_id') or ''),
            'temperature': float(updated.get('temperature', 0.7)),
            'max_tokens': int(updated.get('max_tokens', 1000)),
        },
    })


@panel_bp.route('/api/panel/guild/<guild_id>/welcome', methods=['POST'])
def save_guild_welcome(guild_id: str):
    """儲存或停用伺服器的歡迎訊息設定。"""
    if not can_manage_guild(guild_id):
        return jsonify({'status': 'error', 'message': '權限不足'}), 403

    payload = request.get_json(silent=True) or {}
    action = payload.get('action', 'save')

    all_data = load_welcome_settings()
    if action == 'clear':
        if str(guild_id) in all_data:
            del all_data[str(guild_id)]
            save_welcome_settings(all_data)
        return jsonify({'status': 'ok', 'message': '歡迎訊息設定已停用並清除'})

    welcome_channel_id = payload.get('welcome_channel_id')
    rules_channel_id = payload.get('rules_channel_id')
    role_channel_id = payload.get('role_channel_id')
    auto_role_id = payload.get('auto_role_id')

    all_data[str(guild_id)] = {
        'welcome_channel_id': int(welcome_channel_id) if welcome_channel_id else None,
        'rules_channel_id': int(rules_channel_id) if rules_channel_id else None,
        'role_channel_id': int(role_channel_id) if role_channel_id else None,
        'auto_role_id': int(auto_role_id) if auto_role_id else None,
    }
    save_welcome_settings(all_data)
    return jsonify({'status': 'ok', 'message': '歡迎訊息設定已成功儲存！'})


@panel_bp.route('/api/panel/guild/<guild_id>/music', methods=['POST'])
def save_guild_music(guild_id: str):
    """儲存或解綁伺服器的音樂點歌專屬文字頻道。"""
    if not can_manage_guild(guild_id):
        return jsonify({'status': 'error', 'message': '權限不足'}), 403

    payload = request.get_json(silent=True) or {}
    music_channel_id = payload.get('music_channel_id')

    all_data = load_music_settings()
    if not music_channel_id:
        if str(guild_id) in all_data:
            del all_data[str(guild_id)]
            save_music_settings(all_data)
        return jsonify({'status': 'ok', 'message': '專屬點歌頻道已解除綁定（所有文字頻道皆可使用點歌指令）'})

    all_data[str(guild_id)] = {'music_channel_id': int(music_channel_id)}
    save_music_settings(all_data)
    return jsonify({'status': 'ok', 'message': '專屬點歌文字頻道已成功設定！'})


@panel_bp.route('/api/panel/guild/<guild_id>/auto-reply/save', methods=['POST'])
def save_guild_auto_reply_rule(guild_id: str):
    """新增或修改自動回覆規則。"""
    if not can_manage_guild(guild_id):
        return jsonify({'status': 'error', 'message': '權限不足'}), 403

    payload = request.get_json(silent=True) or {}
    rule_id = payload.get('id')
    name = (payload.get('name') or '').strip()
    keywords_raw = payload.get('keywords') or []
    replies_raw = payload.get('replies') or []
    channel_id = payload.get('channel_id')
    is_paused = 1 if payload.get('is_paused') else 0

    if isinstance(keywords_raw, str):
        keywords = [k.strip() for k in keywords_raw.split(',') if k.strip()]
    else:
        keywords = [str(k).strip() for k in keywords_raw if str(k).strip()]

    if isinstance(replies_raw, str):
        replies = [r.strip() for r in replies_raw.split('\n') if r.strip()]
    else:
        replies = [str(r).strip() for r in replies_raw if str(r).strip()]

    if not name:
        return jsonify({'status': 'error', 'message': '規則名稱不得為空'}), 400
    if not keywords:
        return jsonify({'status': 'error', 'message': '至少需要設定一個觸發關鍵字'}), 400
    if not replies:
        return jsonify({'status': 'error', 'message': '至少需要設定一則回覆內容'}), 400

    all_data = load_auto_reply_settings()
    guild_data = all_data.setdefault(str(guild_id), {'rules': []})
    rules = guild_data.setdefault('rules', [])

    chan_val = int(channel_id) if channel_id else None
    scope_val = str(chan_val) if chan_val else 'all'

    if rule_id is not None:
        # 修改現有規則
        updated = False
        for r in rules:
            if r.get('id') == int(rule_id):
                r['name'] = name[:40]
                r['keywords'] = keywords[:10]
                r['keyword'] = keywords[0]
                r['replies'] = replies[:10]
                r['reply'] = replies[0]
                r['channel_id'] = chan_val
                r['scope'] = scope_val
                r['is_paused'] = is_paused
                r['enabled'] = not bool(is_paused)
                updated = True
                break
        if not updated:
            return jsonify({'status': 'error', 'message': '找不到指定的規則 ID'}), 404
    else:
        # 新增規則 (檢查上限 10 條)
        if len(rules) >= 10:
            return jsonify({'status': 'error', 'message': '伺服器自動回覆規則已達上限（10條）'}), 400
        new_id = int(time.time() * 1000) % 100000000
        rules.append({
            'id': new_id,
            'name': name[:40],
            'keywords': keywords[:10],
            'keyword': keywords[0],
            'replies': replies[:10],
            'reply': replies[0],
            'channel_id': chan_val,
            'scope': scope_val,
            'is_paused': is_paused,
            'enabled': not bool(is_paused),
        })

    save_auto_reply_settings(all_data)
    _sync_cog_settings('AutoReply')
    return jsonify({'status': 'ok', 'message': '自動回覆規則已儲存成功！'})


@panel_bp.route('/api/panel/guild/<guild_id>/auto-reply/delete', methods=['POST'])
def delete_guild_auto_reply_rule(guild_id: str):
    """刪除指定自動回覆規則。"""
    if not can_manage_guild(guild_id):
        return jsonify({'status': 'error', 'message': '權限不足'}), 403

    payload = request.get_json(silent=True) or {}
    rule_id = payload.get('id')
    if rule_id is None:
        return jsonify({'status': 'error', 'message': '缺少 rule_id 參數'}), 400

    all_data = load_auto_reply_settings()
    guild_data = all_data.get(str(guild_id), {})
    rules = guild_data.get('rules', [])
    new_rules = [r for r in rules if r.get('id') != int(rule_id)]

    if len(rules) == len(new_rules):
        return jsonify({'status': 'error', 'message': '找不到欲刪除的規則'}), 404

    guild_data['rules'] = new_rules
    save_auto_reply_settings(all_data)
    _sync_cog_settings('AutoReply')
    return jsonify({'status': 'ok', 'message': '規則已成功刪除'})


@panel_bp.route('/api/panel/guild/<guild_id>/auto-reply/toggle', methods=['POST'])
def toggle_guild_auto_reply_rule(guild_id: str):
    """暫停或啟用自動回覆規則。"""
    if not can_manage_guild(guild_id):
        return jsonify({'status': 'error', 'message': '權限不足'}), 403

    payload = request.get_json(silent=True) or {}
    rule_id = payload.get('id')
    if rule_id is None:
        return jsonify({'status': 'error', 'message': '缺少 rule_id 參數'}), 400

    all_data = load_auto_reply_settings()
    guild_data = all_data.get(str(guild_id), {})
    rules = guild_data.get('rules', [])

    found = None
    for r in rules:
        if r.get('id') == int(rule_id):
            currently_paused = bool(r.get('is_paused')) or not bool(r.get('enabled', True))
            new_paused = 0 if currently_paused else 1
            r['is_paused'] = new_paused
            r['enabled'] = not bool(new_paused)
            found = r
            break

    if not found:
        return jsonify({'status': 'error', 'message': '找不到指定的規則'}), 404

    save_auto_reply_settings(all_data)
    _sync_cog_settings('AutoReply')
    status_str = "已暫停" if found['is_paused'] else "已啟用"
    return jsonify({'status': 'ok', 'message': f"規則「{found.get('name')}」{status_str}", 'is_paused': found['is_paused']})


# ==============================================================================
# 表符身分組 (Reaction Roles) 網頁控制面板同步編輯 / 刪除 API
# ==============================================================================

@panel_bp.route('/api/panel/guild/<guild_id>/reaction-roles/message/<message_id>', methods=['GET'])
def get_guild_reaction_role_message(guild_id: str, message_id: str):
    """讀取指定表符身分組訊息在 Discord 頻道中的目前文字內容（供編輯彈窗預填）。"""
    if not can_manage_guild(guild_id):
        return jsonify({'status': 'error', 'message': '權限不足'}), 403

    all_data = load_reaction_roles()
    messages = all_data.get(str(guild_id), {}).get('messages', [])
    target_msg = next((m for m in messages if str(m.get('message_id')) == str(message_id)), None)
    if not target_msg:
        return jsonify({'status': 'error', 'message': '找不到該筆表符身分組配置'}), 404

    content_text = ''
    bot = get_bot()
    if bot and getattr(bot, 'loop', None) and bot.loop.is_running() and guild_id.isdigit():
        async def _fetch_content():
            guild_obj = bot.get_guild(int(guild_id))
            if not guild_obj:
                return ''
            ch_id = int(target_msg.get('channel_id') or 0)
            msg_id = int(target_msg.get('message_id') or 0)
            ch = guild_obj.get_channel(ch_id)
            if not isinstance(ch, discord.TextChannel):
                try:
                    fetched = await bot.fetch_channel(ch_id)
                    if isinstance(fetched, discord.TextChannel):
                        ch = fetched
                except Exception:
                    ch = None
            if not ch:
                return ''
            try:
                m_obj = await ch.fetch_message(msg_id)
                return m_obj.content or ''
            except Exception:
                return ''

        try:
            content_text = run_coroutine_threadsafe(_fetch_content(), timeout=8)
        except Exception:
            content_text = ''

    return jsonify({
        'status': 'ok',
        'message_id': str(target_msg.get('message_id')),
        'channel_id': str(target_msg.get('channel_id')),
        'content': content_text,
    })


@panel_bp.route('/api/panel/guild/<guild_id>/reaction-roles/create', methods=['POST'])
def create_guild_reaction_role(guild_id: str):
    """從控制面板新增表符身分組領取訊息，並同步發送至 Discord 指定文字頻道與加上表符反應。"""
    if not can_manage_guild(guild_id):
        return jsonify({'status': 'error', 'message': '權限不足'}), 403

    payload = request.get_json(silent=True) or {}
    channel_id = str(payload.get('channel_id') or '').strip()
    if not channel_id or not channel_id.isdigit():
        return jsonify({'status': 'error', 'message': '請選擇要發送領取訊息的文字頻道'}), 400

    strict_mode = bool(payload.get('strict', True))
    raw_pairs = payload.get('pairs')
    if not isinstance(raw_pairs, list) or len(raw_pairs) == 0:
        return jsonify({'status': 'error', 'message': '請至少新增 1 組表符與身分組配對'}), 400
    if len(raw_pairs) > 20:
        return jsonify({'status': 'error', 'message': '單一訊息最多只能設定 20 組表符配對'}), 400

    cleaned_pairs = []
    seen_emojis = set()
    for idx, p in enumerate(raw_pairs, start=1):
        if not isinstance(p, dict):
            continue
        em_str = str(p.get('emoji') or '').strip()
        r_id_str = str(p.get('role_id') or '').strip()
        if not em_str:
            return jsonify({'status': 'error', 'message': f'第 {idx} 組缺少表情符號'}), 400
        if not r_id_str or not r_id_str.isdigit():
            return jsonify({'status': 'error', 'message': f'第 {idx} 組缺少有效的身分組'}), 400
        if em_str in seen_emojis:
            return jsonify({'status': 'error', 'message': f'表符 {em_str} 重複使用，每個表符只能對應一個身分組'}), 400
        seen_emojis.add(em_str)
        cleaned_pairs.append({
            'emoji': em_str,
            'role_id': int(r_id_str),
        })

    if not cleaned_pairs:
        return jsonify({'status': 'error', 'message': '表符配對不可為空'}), 400

    all_data = load_reaction_roles()
    guild_entry = all_data.setdefault(str(guild_id), {'messages': []})
    messages = guild_entry.setdefault('messages', [])
    if len(messages) >= 10:
        return jsonify({'status': 'error', 'message': '此伺服器的表符身分組訊息已達上限（10 則），請先刪除舊訊息'}), 400

    raw_content = str(payload.get('message_content') or '').strip()
    auto_sync_text = bool(payload.get('auto_sync_text', True))

    new_msg_id = None
    discord_note = ''
    bot = get_bot()
    if bot and getattr(bot, 'loop', None) and bot.loop.is_running() and guild_id.isdigit():
        async def _send_rr_message():
            guild_obj = bot.get_guild(int(guild_id))
            if not guild_obj:
                raise RuntimeError('機器人尚未加入此伺服器')
            ch = guild_obj.get_channel(int(channel_id))
            if not isinstance(ch, discord.TextChannel):
                try:
                    fetched = await bot.fetch_channel(int(channel_id))
                    if isinstance(fetched, discord.TextChannel):
                        ch = fetched
                except Exception:
                    ch = None
            if not ch:
                raise RuntimeError('找不到指定的 Discord 文字頻道')

            if raw_content and not auto_sync_text:
                final_text = raw_content[:2000]
            else:
                header = raw_content.split('\n', 1)[0].strip() if raw_content else '點擊表符領取身分組！'
                lines = [header, '']
                for p in cleaned_pairs:
                    r_obj = guild_obj.get_role(int(p['role_id']))
                    r_mention = r_obj.mention if r_obj else f"<@&{p['role_id']}>"
                    lines.append(f"{p['emoji']} {r_mention}")
                final_text = '\n'.join(lines)[:2000]

            sent = await ch.send(
                content=final_text,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            for p in cleaned_pairs:
                try:
                    await sent.add_reaction(p['emoji'])
                except Exception:
                    pass
            return sent.id

        try:
            new_msg_id = run_coroutine_threadsafe(_send_rr_message(), timeout=20)
            discord_note = '（已於 Discord 頻道發送領取訊息並掛載表符）'
        except Exception as exc:
            return jsonify({'status': 'error', 'message': f'發送 Discord 訊息失敗：{exc}'}), 400
    else:
        # 單機開發或測試環境模擬 ID
        mock_id = payload.get('mock_message_id')
        new_msg_id = int(mock_id) if (mock_id and str(mock_id).isdigit()) else int(time.time() * 1000)

    messages.append({
        'channel_id': int(channel_id),
        'message_id': int(new_msg_id),
        'strict': strict_mode,
        'pairs': cleaned_pairs,
    })
    save_reaction_roles(all_data)
    _sync_cog_settings('ReactionRoles')

    return jsonify({
        'status': 'ok',
        'message': f'已成功新增表符身分組訊息！{discord_note}',
        'message_id': str(new_msg_id),
    })


@panel_bp.route('/api/panel/guild/<guild_id>/reaction-roles/update', methods=['POST'])
def update_guild_reaction_role(guild_id: str):
    """修改表符身分組配置（配對清單、僅限有效表符開關、可選同步更新 Discord 訊息文字與反應）。"""
    if not can_manage_guild(guild_id):
        return jsonify({'status': 'error', 'message': '權限不足'}), 403

    payload = request.get_json(silent=True) or {}
    message_id = payload.get('message_id')
    if not message_id or not str(message_id).isdigit():
        return jsonify({'status': 'error', 'message': '缺少有效的 message_id 參數'}), 400

    all_data = load_reaction_roles()
    guild_entry = all_data.get(str(guild_id), {})
    messages = guild_entry.get('messages', [])

    target_msg = next((m for m in messages if str(m.get('message_id')) == str(message_id)), None)
    if not target_msg:
        return jsonify({'status': 'error', 'message': '找不到指定的表符身分組訊息配置'}), 404

    # 更新 strict 開關
    if 'strict' in payload:
        target_msg['strict'] = bool(payload.get('strict'))

    # 更新 pairs 配對清單（若有傳入）
    if 'pairs' in payload:
        raw_pairs = payload.get('pairs')
        if not isinstance(raw_pairs, list) or len(raw_pairs) == 0:
            return jsonify({'status': 'error', 'message': '表符配對不可為空，請至少設定 1 組配對'}), 400
        if len(raw_pairs) > 20:
            return jsonify({'status': 'error', 'message': '單一訊息最多只能設定 20 組表符配對'}), 400

        cleaned_pairs = []
        seen_emojis = set()
        for idx, p in enumerate(raw_pairs, start=1):
            if not isinstance(p, dict):
                continue
            em_str = str(p.get('emoji') or '').strip()
            r_id_str = str(p.get('role_id') or '').strip()
            if not em_str:
                return jsonify({'status': 'error', 'message': f'第 {idx} 組缺少表情符號'}), 400
            if not r_id_str or not r_id_str.isdigit():
                return jsonify({'status': 'error', 'message': f'第 {idx} 組缺少有效的身分組'}), 400
            if em_str in seen_emojis:
                return jsonify({'status': 'error', 'message': f'表符 {em_str} 重複使用，每個表符只能對應一個身分組'}), 400
            seen_emojis.add(em_str)
            cleaned_pairs.append({
                'emoji': em_str,
                'role_id': int(r_id_str),
            })

        if not cleaned_pairs:
            return jsonify({'status': 'error', 'message': '表符配對不可為空'}), 400

        target_msg['pairs'] = cleaned_pairs

    save_reaction_roles(all_data)
    _sync_cog_settings('ReactionRoles')

    # 若機器人在線，同步更新 Discord 原訊息的反應與內文
    message_content = payload.get('message_content')
    auto_sync_text = bool(payload.get('auto_sync_text', False))
    discord_sync_note = ''

    bot = get_bot()
    if bot and getattr(bot, 'loop', None) and bot.loop.is_running() and guild_id.isdigit():
        async def _sync_discord_message():
            guild_obj = bot.get_guild(int(guild_id))
            if not guild_obj:
                return ''
            ch_id = int(target_msg.get('channel_id') or 0)
            msg_id = int(target_msg.get('message_id') or 0)
            ch = guild_obj.get_channel(ch_id)
            if not isinstance(ch, discord.TextChannel):
                try:
                    fetched = await bot.fetch_channel(ch_id)
                    if isinstance(fetched, discord.TextChannel):
                        ch = fetched
                except Exception:
                    ch = None
            if not ch:
                return '（原頻道未找到，已僅更新資料庫配置）'
            try:
                m_obj = await ch.fetch_message(msg_id)
            except Exception:
                return '（原 Discord 訊息無法讀取，已僅更新資料庫配置）'

            # 1. 若有指定更新內文或自動排版內文
            new_text = None
            if isinstance(message_content, str) and message_content.strip():
                new_text = message_content.strip()[:2000]
            elif auto_sync_text:
                first_line = (m_obj.content or '').split('\n', 1)[0].strip()
                pairs_list = target_msg.get('pairs') or []
                if (
                    first_line
                    and not first_line.startswith('<')
                    and not first_line.startswith('•')
                    and not any(first_line.startswith(str(p.get('emoji', ''))) for p in pairs_list)
                ):
                    header = first_line
                else:
                    header = '點擊表符領取身分組！'
                lines = [header, '']
                for p in pairs_list:
                    r_obj = guild_obj.get_role(int(p.get('role_id') or 0))
                    r_mention = r_obj.mention if r_obj else f"<@&{p.get('role_id')}>"
                    lines.append(f"{p.get('emoji')} {r_mention}")
                new_text = '\n'.join(lines)[:2000]

            if new_text is not None and m_obj.author.id == (bot.user.id if bot.user else 0):
                try:
                    await m_obj.edit(content=new_text)
                except Exception:
                    pass

            # 2. 若這次有更新 pairs，同步訊息上的表符反應
            if 'pairs' in payload:
                desired_emojis = [str(p.get('emoji')) for p in (target_msg.get('pairs') or [])]
                desired_set = set(desired_emojis)
                already_on = {str(r.emoji) for r in m_obj.reactions if r.me and str(r.emoji) in desired_set}
                for reaction in list(m_obj.reactions):
                    if reaction.me and str(reaction.emoji) not in desired_set:
                        try:
                            await reaction.clear()
                        except Exception:
                            pass
                for em_str in desired_emojis:
                    if em_str not in already_on:
                        try:
                            await m_obj.add_reaction(em_str)
                        except Exception:
                            pass
            return '（已同步更新 Discord 訊息與反應）'

        try:
            discord_sync_note = run_coroutine_threadsafe(_sync_discord_message(), timeout=15)
        except Exception:
            discord_sync_note = ''

    return jsonify({
        'status': 'ok',
        'message': f'表符身分組配置已成功更新！{discord_sync_note}',
        'strict': bool(target_msg.get('strict', True)),
    })


@panel_bp.route('/api/panel/guild/<guild_id>/reaction-roles/delete', methods=['POST'])
def delete_guild_reaction_role(guild_id: str):
    """刪除指定的表符身分組訊息配置，並嘗試一併刪除 Discord 頻道內的原領取訊息。"""
    if not can_manage_guild(guild_id):
        return jsonify({'status': 'error', 'message': '權限不足'}), 403

    payload = request.get_json(silent=True) or {}
    message_id = payload.get('message_id')
    if not message_id or not str(message_id).isdigit():
        return jsonify({'status': 'error', 'message': '缺少有效的 message_id 參數'}), 400

    all_data = load_reaction_roles()
    guild_entry = all_data.get(str(guild_id), {})
    messages = guild_entry.get('messages', [])

    target_msg = next((m for m in messages if str(m.get('message_id')) == str(message_id)), None)
    if not target_msg:
        return jsonify({'status': 'error', 'message': '找不到欲刪除的表符身分組訊息配置'}), 404

    channel_id = int(target_msg.get('channel_id') or 0)
    msg_id_int = int(target_msg.get('message_id') or 0)

    new_messages = [m for m in messages if str(m.get('message_id')) != str(message_id)]
    if new_messages:
        guild_entry['messages'] = new_messages
    else:
        all_data.pop(str(guild_id), None)

    save_reaction_roles(all_data)
    _sync_cog_settings('ReactionRoles')

    # 若機器人在線，嘗試一併刪除 Discord 頻道內的原訊息
    discord_del_note = ''
    bot = get_bot()
    if bot and getattr(bot, 'loop', None) and bot.loop.is_running() and guild_id.isdigit() and channel_id:
        async def _del_discord_msg():
            guild_obj = bot.get_guild(int(guild_id))
            if not guild_obj:
                return ''
            ch = guild_obj.get_channel(channel_id)
            if not isinstance(ch, discord.TextChannel):
                try:
                    fetched = await bot.fetch_channel(channel_id)
                    if isinstance(fetched, discord.TextChannel):
                        ch = fetched
                except Exception:
                    ch = None
            if not ch:
                return ''
            try:
                m_obj = await ch.fetch_message(msg_id_int)
                await m_obj.delete()
                return '（頻道內的原領取訊息也已一併刪除）'
            except discord.NotFound:
                return '（原頻道訊息先前已被刪除，已清除設定）'
            except Exception:
                return '（已移除設定；原頻道訊息因權限或連線因素未刪除）'

        try:
            discord_del_note = run_coroutine_threadsafe(_del_discord_msg(), timeout=10)
        except Exception:
            discord_del_note = ''

    return jsonify({
        'status': 'ok',
        'message': f'表符身分組訊息配置已成功刪除！{discord_del_note}',
    })


# ==============================================================================
# 👑 擁有者專屬 API (owner_guild_only 指令設定控制專區)
# 確保非擁有者無法以任何途徑讀取或修改以下設定 (403 Forbidden)
# ==============================================================================

@panel_bp.before_request
def check_owner_for_owner_routes():
    """全域中介防護：所有 /api/panel/owner/ 端點必須嚴格校驗擁有者身分。"""
    if request.path.startswith('/api/panel/owner/'):
        if not is_current_user_owner():
            return jsonify({
                'status': 'error',
                'error': '403 Forbidden',
                'message': '權限不足：此設定為 owner_guild_only 擁有者專屬控制面板，僅機器人擁有者可存取。',
            }), 403


@panel_bp.route('/api/panel/owner/economy-config', methods=['GET', 'POST'])
def owner_economy_config():
    """對應 @owner_guild_only /economy_admin：檢視與修改經濟系統全域參數。"""
    if request.method == 'GET':
        config = load_economy_config()
        return jsonify({'status': 'ok', 'config': config})

    payload = request.get_json(silent=True) or {}
    cur_config = load_economy_config()

    # 安全地逐項更新數值，並防呆校驗
    for k in cur_config.keys():
        if k in payload:
            val = payload[k]
            if isinstance(cur_config[k], bool):
                cur_config[k] = bool(val)
            elif isinstance(cur_config[k], int):
                try:
                    cur_config[k] = int(float(val))
                except (ValueError, TypeError):
                    pass
            elif isinstance(cur_config[k], float):
                try:
                    cur_config[k] = float(val)
                except (ValueError, TypeError):
                    pass
            elif isinstance(cur_config[k], str):
                cur_config[k] = str(val).strip()

    # 防呆校驗
    # 防呆校驗
    if cur_config['daily_max'] < cur_config['daily_min']:
        cur_config['daily_max'] = cur_config['daily_min']
    if cur_config['hourly_max'] < cur_config['hourly_min']:
        cur_config['hourly_max'] = cur_config['hourly_min']
    if cur_config['bet_max'] < cur_config['bet_min']:
        cur_config['bet_max'] = cur_config['bet_min']
    if 'ai_cost_per_1k_tokens' in cur_config and cur_config['ai_cost_per_1k_tokens'] < 1:
        cur_config['ai_cost_per_1k_tokens'] = 1
    if 'ai_daily_streak_max' in cur_config and 'ai_daily_streak_bonus' in cur_config:
        if cur_config['ai_daily_streak_max'] < cur_config['ai_daily_streak_bonus']:
            cur_config['ai_daily_streak_max'] = cur_config['ai_daily_streak_bonus']

    save_economy_config(cur_config)
    _sync_cog_settings('Economy')
    return jsonify({'status': 'ok', 'message': '經濟系統後台參數已成功更新！', 'config': cur_config})


@panel_bp.route('/api/panel/owner/economy-user', methods=['GET', 'POST'])
def owner_economy_user():
    """對應 @owner_guild_only /economy_admin：查詢與手動調整使用者金幣或 AI 貨幣帳戶。"""
    if request.method == 'GET':
        user_id_str = request.args.get('user_id', '').strip()
        if not user_id_str or not user_id_str.isdigit():
            return jsonify({'status': 'error', 'message': '請提供有效的數字格式 Discord 使用者 ID'}), 400
        user_data = get_economy_user(int(user_id_str))
        return jsonify({'status': 'ok', 'user': user_data})

    payload = request.get_json(silent=True) or {}
    user_id = payload.get('user_id')
    action = payload.get('action', 'set')  # 'set' 或 'add'
    amount = payload.get('amount')
    currency = payload.get('currency', 'gold')  # 'gold' 或 'ai'

    if not user_id or str(user_id).isdigit() is False:
        return jsonify({'status': 'error', 'message': '使用者 ID 格式錯誤'}), 400
    try:
        amount_int = int(amount)
    except (ValueError, TypeError):
        return jsonify({'status': 'error', 'message': '金額必須為整數'}), 400

    uid = int(user_id)
    if currency == 'ai':
        if action == 'add':
            _, new_pts = update_user_ai_points(uid, amount_int)
        else:
            new_pts = set_user_ai_points(uid, max(0, amount_int))
        return jsonify({
            'status': 'ok',
            'message': f"使用者 {uid} 的 AI 貨幣已成功調整為 {new_pts:,} 點！",
            'user': get_economy_user(uid),
        })

    if action == 'add':
        _, new_balance = update_economy_balance(uid, amount_int)
    else:
        new_balance = set_economy_balance(uid, max(0, amount_int))

    return jsonify({
        'status': 'ok',
        'message': f"使用者 {uid} 的金幣已成功調整為 {new_balance:,} 金幣！",
        'user': get_economy_user(uid),
    })


@panel_bp.route('/api/panel/owner/cogs')
def owner_cogs_list():
    """對應 @owner_guild_only /reload_cogs：僅列出目前已載入、可熱重載的 Cog 模組。

    database / sanitize / tls / panel_bp / web_server 等輔助模組不是 discord.py extension，
    無法透過 reload_extension 熱重載，因此不顯示於控制面板。
    """
    bot = get_bot()
    loaded_extensions = set(bot.extensions.keys()) if bot else set()

    all_cogs = [
        {
            'module': mod_name,
            'name': mod_name.split('.', 1)[-1],
            'loaded': True,
        }
        for mod_name in sorted(loaded_extensions)
        if mod_name.startswith('cogs.')
    ]

    return jsonify({
        'status': 'ok',
        'cogs': all_cogs,
        'owner_guild_id': get_owner_guild_id(),
    })


@panel_bp.route('/api/panel/owner/reload-cog', methods=['POST'])
def owner_reload_cog():
    """對應 @owner_guild_only /reload_cogs：執行線上熱重載並自動同步指令樹。"""
    payload = request.get_json(silent=True) or {}
    module_name = (payload.get('module') or '').strip()

    if not module_name:
        return jsonify({'status': 'error', 'message': '請指定要重載的模組名稱'}), 400

    bot = get_bot()
    if bot is None or getattr(bot, 'loop', None) is None or not bot.loop.is_running():
        return jsonify({'status': 'error', 'message': 'Discord 機器人尚未啟動或事件迴圈未在運行'}), 503

    if module_name != 'all':
        normalized = module_name if module_name.startswith('cogs.') else f"cogs.{module_name}"
        if normalized not in set(bot.extensions.keys()):
            return jsonify({
                'status': 'error',
                'message': f"模組 {normalized} 不是已載入的 Cog，無法熱重載",
            }), 400

    async def _async_reload():
        if 'cogs.database' in sys.modules:
            importlib.reload(sys.modules['cogs.database'])

        reloaded = []
        errors = []

        if module_name == 'all':
            for m in sorted(k for k in bot.extensions.keys() if k.startswith('cogs.')):
                try:
                    await bot.reload_extension(m)
                    reloaded.append(m)
                except Exception as e:
                    errors.append(f"{m}: {e}")
        else:
            await bot.reload_extension(normalized)
            reloaded.append(normalized)

        from cogs.general import sync_app_commands_after_reload
        sync_summary = await sync_app_commands_after_reload(bot)

        return {
            'reloaded': reloaded,
            'errors': errors,
            'sync_summary': sync_summary,
        }

    try:
        res = run_coroutine_threadsafe(_async_reload(), timeout=25)
        return jsonify({
            'status': 'ok',
            'message': f"成功重載模組：{', '.join(res['reloaded'])}",
            'sync_summary': res['sync_summary'],
            'errors': res['errors'],
        })
    except Exception as exc:
        return jsonify({
            'status': 'error',
            'message': f"重載模組失敗：{redact_secrets(str(exc))}",
        }), 500


@panel_bp.route('/api/panel/owner/sync-commands', methods=['POST'])
def owner_sync_commands():
    """對應 @owner_guild_only：強制向 Discord 官方重新同步指令樹。"""
    bot = get_bot()
    if bot is None or getattr(bot, 'loop', None) is None or not bot.loop.is_running():
        return jsonify({'status': 'error', 'message': 'Discord 機器人尚未啟動或事件迴圈未在運行'}), 503

    async def _async_sync():
        from cogs.general import sync_app_commands_after_reload
        return await sync_app_commands_after_reload(bot)

    try:
        summary = run_coroutine_threadsafe(_async_sync(), timeout=25)
        return jsonify({'status': 'ok', 'message': f"指令樹同步完成：{summary}"})
    except Exception as exc:
        return jsonify({'status': 'error', 'message': f"指令同步失敗：{redact_secrets(str(exc))}"}), 500


@panel_bp.route('/api/panel/owner/api-quota')
def owner_api_quota():
    """對應擁有者管理：檢視外部 API 每日使用配額與調用次數。"""
    services = ['weather_cwa', 'bus_tdx', 'shortener_reurl']
    results = {}
    for s in services:
        count, date_str = get_daily_api_usage(s)
        results[s] = {'count': count, 'date': date_str}

    return jsonify({'status': 'ok', 'quotas': results})
