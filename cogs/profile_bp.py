import asyncio
import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)

from cogs.database import (
    get_profile_economy_data,
    get_user_profile_by_username,
    seed_default_profile,
    upsert_user_profile,
    has_profile_permission,
    is_profile_admin,
    create_support_ticket,
    get_support_tickets,
    is_user_blocked,
    get_profile_request_cooldown,
)

profile_bp = Blueprint('profile', __name__)


@profile_bp.after_request
def add_no_cache_headers(response):
    """確保個人檔案頁面與 API 響應不被瀏覽器快取，保證每次重新整理獲取最新視覺狀態。"""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# ==============================================================================
# 兩階段開發模式開關 (第一階段預設開啟本地免登入測試)
# ==============================================================================
# 當 PROFILE_DEV_MODE 為 True 時：
#   - 無需 Discord OAuth2 登入，直接視為本人擁有者 (is_owner = True)。
#   - 支援 URL 參數 ?role=visitor 或頂部測試列切換至一般訪客視角 (唯讀)。
# 當 PROFILE_DEV_MODE 為 False 時：
#   - 正式部署模式：嚴格檢查 session['user']['id'] == profile.user_id。
# ==============================================================================
PROFILE_DEV_MODE: bool = os.getenv("PROFILE_DEV_MODE", "false").strip().lower() in ("true", "1", "yes")

# 允許的網格卡片類型清單
ALLOWED_WIDGET_TYPES = {
    "profile_header",
    "bot_economy",
    "text_card",
    "social_link",
    "media_embed",
    "divider",
}

# 允許的社群平台
ALLOWED_SOCIAL_PLATFORMS = {
    "discord",
    "github",
    "youtube",
    "twitch",
    "instagram",
    "steam",
    "twitter",
    "threads",
    "spotify",
    "custom",
}


def is_safe_url(url: Optional[str]) -> bool:
    """檢查 URL 是否安全，嚴格防止 javascript: 或 data: 等偽協議 XSS。"""
    if not url:
        return True
    url_clean = url.strip()
    if url_clean.startswith('/') and not url_clean.startswith('//'):
        return True  # 允許相對站內路徑如 /static/logo.png
    lower = url_clean.lower()
    return lower.startswith('http://') or lower.startswith('https://')


def sanitize_text(text: Optional[str], max_len: int = 500) -> str:
    """清理文字並限制最大長度。"""
    if not text:
        return ""
    # 去除前後空白並限制長度，但不做 HTML entity encode，由前端或 Jinja 負責 safe 輸出
    return str(text).strip()[:max_len]


def get_current_profile_user(
    target_username: str,
    target_profile: Optional[dict[str, Any]] = None,
    role_override: Optional[str] = None,
) -> Tuple[Optional[dict[str, Any]], bool]:
    """統一的身分取得與權限判定輔助函式。

    回傳：
        (current_user, is_owner)
    """
    clean_target = target_username.strip().lower()

    if PROFILE_DEV_MODE:
        # 第一階段：本地免登入測試模式
        if role_override == 'visitor':
            # 模擬一般訪客（未登入/唯讀視角）
            return None, False

        # 預設：模擬當前使用者即為該個人檔案之擁有者
        mock_uid = target_profile["user_id"] if target_profile else f"dev_user_{clean_target}"
        user_info = {
            "id": mock_uid,
            "username": clean_target,
            "display_name": target_profile.get("display_name", clean_target) if target_profile else clean_target,
            "is_dev": True,
        }
        return user_info, True

    # 第二階段：正式部署模式（Discord OAuth2 驗證）
    user = session.get('user')
    if not user or not user.get('id'):
        return None, False

    session_user_id = str(user.get('id'))
    if target_profile and target_profile.get('user_id'):
        is_owner = (session_user_id == str(target_profile['user_id']))
    else:
        # 若尚未有 profile，比對使用者名稱
        session_uname = str(user.get('username', '')).lower()
        is_owner = (session_uname == clean_target)

    return user, is_owner


def send_support_dm_to_owner(
    user_id: str,
    username: str,
    display_name: str,
    category: str,
    reason: str,
    ticket_id: int,
):
    """異步發送申請或支援單據通報至 Bot Owner 的 Discord 私訊。"""
    try:
        bot = current_app.config.get('BOT')
    except Exception:
        bot = None

    if not bot or getattr(bot, 'loop', None) is None or not bot.loop.is_running():
        print("[WARN] Bot 尚未啟動或 event loop 未在運行，略過 Owner DM 發送。")
        return

    owner_id = os.getenv('DISCORD_OWNER_ID') or os.getenv('OWNER_ID')
    if not owner_id and getattr(bot, 'owner_id', None):
        owner_id = str(bot.owner_id)

    if not owner_id:
        print("[WARN] 無法獲取 Bot Owner ID，無法發送 DM。")
        return

    async def _send_dm():
        try:
            owner = bot.get_user(int(owner_id))
            if owner is None:
                owner = await bot.fetch_user(int(owner_id))
            if not owner:
                return
            time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cat_display = "申請個人檔案" if category == "profile_request" else category
            dm_text = (
                f"📝 **【個人檔案建立申請】新提交通知**\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"**單據編號**: `#{ticket_id}`\n"
                f"**申請類別**: {cat_display}\n"
                f"**申請者名稱**: {display_name} (`@{username}`)\n"
                f"**Discord ID**: `{user_id}`\n"
                f"**申請理由 / 內容**:\n{reason}\n"
                f"**提交時間**: `{time_str}`\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"-# 💡 提示：前往網頁管理員設定（Bot Owner 後台）可查看申請詳情並點擊「快捷授權」完成審核！"
            )
            await owner.send(dm_text)
            print(f"[INFO] 已成功發送單據 #{ticket_id} 之通報至 Bot Owner DM。")
        except Exception as exc:
            print(f"[WARN] 發送 Owner DM 失敗: {exc}")

    try:
        asyncio.run_coroutine_threadsafe(_send_dm(), bot.loop)
    except Exception as exc:
        print(f"[WARN] run_coroutine_threadsafe 發送 Owner DM 失敗: {exc}")


# ==============================================================================
# 路由定義
# ==============================================================================

@profile_bp.route('/profile')
def profile_root():
    """快捷路由：
    - 未登入用戶：跳出「登入編輯個人檔案」提示頁面
    - 已登入但未授權用戶：跳出「尚未申請個人檔案頁面」提示頁面並附申請按鈕
    - 已授權或已有主頁用戶：自動導向至其專屬個人主頁 /profile/@<username>
    """
    user = session.get('user')
    if not user:
        return render_template(
            'profile_prompt.html',
            state='login_required',
            title='登入編輯個人檔案',
            user=None,
        )

    user_id = str(user.get('id', ''))
    uname = str(user.get('username', '')).strip().lower()

    # 檢查是否擁有編輯權限或已有 profile
    existing = get_user_profile_by_username(uname)
    has_perm = has_profile_permission(user_id)

    if has_perm or existing:
        return redirect(f'/profile/@{uname}')

    return render_template(
        'profile_prompt.html',
        state='not_applied',
        title='尚未申請個人檔案頁面',
        user=user,
    )


@profile_bp.route('/profile/@<username>')
def profile_view(username: str):
    """Bento Grid 網格個人 Profile 頁面。"""
    clean_username = username.strip().lstrip('@').lower()
    logged_user = session.get('user')

    if not clean_username or len(clean_username) > 32:
        return render_template('profile_404.html', username=clean_username or 'unknown', user=logged_user), 404

    role_param = request.args.get('role', '').strip().lower()
    logged_user_id = str(logged_user.get('id', '')) if logged_user else ''
    logged_uname = str(logged_user.get('username', '')).lower() if logged_user else ''

    # 1. 查詢 SQLite 個人檔案
    profile = get_user_profile_by_username(clean_username)

    if not profile:
        # 正式上線規則：若該 profile 不存在於資料庫中，
        # 僅當當前登入者剛好是該使用者，且擁有授權/管理員編輯權限時，才為其初次初始化
        if logged_user and logged_uname == clean_username and has_profile_permission(logged_user_id):
            profile = seed_default_profile(
                username=clean_username,
                user_id=logged_user_id,
                display_name=logged_user.get('global_name') or logged_user.get('username'),
                avatar_url=logged_user.get('avatar_url'),
            )
        else:
            # 使用者無編輯權限且不存在於資料庫中，自動轉向到無此頁面的畫面 (404)
            return render_template('profile_404.html', username=clean_username, user=logged_user), 404

    # 2. 判定當前瀏覽者權限 (is_owner)
    current_user, is_owner = get_current_profile_user(
        target_username=clean_username,
        target_profile=profile,
        role_override=role_param,
    )

    # 嚴格規則：只有擁有 profile 編輯權限的使用者才能開啟編輯工具列
    if is_owner:
        uid = current_user.get('id') if current_user else None
        if not has_profile_permission(uid):
            is_owner = False

    # 3. 取得機器人連動之經濟數據
    economy_data = get_profile_economy_data(profile.get('user_id'))

    # 4. 組裝標準公開 URL (Canonical URL，去除 ?role= 測試參數)
    host_url = request.host_url.rstrip('/')
    canonical_url = f"{host_url}/profile/@{clean_username}"

    # 當前視角模式（供狀態顯示）
    current_role_view = "owner" if is_owner else "visitor"

    return render_template(
        'profile.html',
        profile=profile,
        economy=economy_data,
        is_owner=is_owner,
        dev_mode=PROFILE_DEV_MODE,
        current_role=current_role_view,
        canonical_url=canonical_url,
    )


@profile_bp.route('/support')
def support_page():
    """服務支援與回報分頁 (支援個人檔案申請、問題回報)。"""
    cat = request.args.get('category', '').strip() or request.args.get('type', '').strip()
    user = session.get('user')
    is_blocked = is_user_blocked(user.get('id')) if user else False
    return render_template('support.html', user=user, initial_category=cat, is_blocked=is_blocked)


@profile_bp.route('/api/support/submit', methods=['POST'])
def submit_support_ticket():
    """提交服務支援或個人檔案申請 API。"""
    user = session.get('user')
    if not user:
        return jsonify({"success": False, "error": "請先登入 Discord 帳號後再提交申請。"}), 401

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"success": False, "error": "無效的 JSON 酬載"}), 400

    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "請求格式不正確"}), 400

    category = str(data.get('category', 'profile_request')).strip()
    reason = str(data.get('reason', '')).strip()

    if len(reason) < 10:
        return jsonify({"success": False, "error": "請至少填寫 10 個字元之說明理由。"}), 400
    if len(reason) > 3000:
        return jsonify({"success": False, "error": "內容長度超出限制 (上限 3000 字)。"}), 400

    user_id = str(user.get('id', ''))
    username = str(user.get('username', ''))
    display_name = str(user.get('global_name') or username)

    # 檢查是否為被封鎖用戶
    if is_user_blocked(user_id):
        return jsonify({"success": False, "error": "您的帳號因違反服務條款或濫用服務已被系統封鎖，無法提交支援單據。"}), 403

    # 個人檔案申請限制每 30 天僅能申請一次
    if category == 'profile_request':
        can_apply, remaining_days = get_profile_request_cooldown(user_id)
        if not can_apply:
            return jsonify({
                "success": False,
                "error": f"個人檔案申請每 30 天限制申請一次。您近期已提交過申請，請於 {remaining_days} 天後再試。"
            }), 429

    ticket_id = create_support_ticket(
        user_id=user_id,
        username=username,
        display_name=display_name,
        category=category,
        reason=reason,
    )

    if not ticket_id:
        return jsonify({"success": False, "error": "資料庫寫入失敗，請稍後再試。"}), 500

    # 同步發送 Bot Owner DM 通報
    try:
        send_support_dm_to_owner(
            user_id=user_id,
            username=username,
            display_name=display_name,
            category=category,
            reason=reason,
            ticket_id=ticket_id,
        )
    except Exception as exc:
        print(f"[WARN] 通報 Owner DM 發生錯誤: {exc}")

    success_msg = (
        "個人檔案申請已成功送出！已同步發送通報至管理團隊審核，審核通過後您即可在用戶中心開啟個人主頁。"
        if category == "profile_request"
        else "您的單據已成功送出，管理團隊將盡速為您處理。"
    )

    return jsonify({
        "success": True,
        "ticket_id": ticket_id,
        "message": success_msg,
    })


@profile_bp.route('/api/profile/save', methods=['POST'])
def save_profile():
    """儲存個人檔案排版、卡片與外觀設定 API。"""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"success": False, "error": "無效的 JSON 酬載"}), 400

    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "請求格式必須是 JSON 物件"}), 400

    target_username = str(data.get('username', '')).strip().lstrip('@')
    if not target_username:
        return jsonify({"success": False, "error": "缺少目標使用者名稱"}), 400

    # 1. 查詢目標 Profile
    profile = get_user_profile_by_username(target_username)
    if not profile:
        return jsonify({"success": False, "error": "找不到該使用者個人檔案"}), 404

    # 2. 權限檢查
    role_param = request.args.get('role', '').strip().lower()
    current_user, is_owner = get_current_profile_user(
        target_username=target_username,
        target_profile=profile,
        role_override=role_param,
    )

    if not is_owner:
        return jsonify({
            "success": False,
            "error": "權限不足：您並非此個人檔案的擁有者，無法儲存變更。",
        }), 403

    # 檢查是否具備授權編輯權限或是否已被封鎖
    uid = current_user.get('id') if current_user else None
    if is_user_blocked(uid):
        return jsonify({
            "success": False,
            "error": "帳號已被封鎖：您的帳號已被系統管理員封鎖，無法編輯或儲存個人主頁。",
        }), 403

    if not has_profile_permission(uid):
        return jsonify({
            "success": False,
            "error": "權限不足：您的帳號尚未取得個人主頁編輯權限，請先前往支援頁面申請。",
        }), 403

    # 3. 資料驗證與清理
    # 3.1 驗證 layout_widgets
    raw_widgets = data.get('layout_widgets')
    if not isinstance(raw_widgets, list):
        return jsonify({"success": False, "error": "layout_widgets 必須是陣列"}), 400

    if len(raw_widgets) > 20:
        return jsonify({"success": False, "error": "網格卡片數量不能超過 20 張"}), 400

    # 嚴格卡片類型上限設定 (依規範：文字區塊、社群連結、圖片或YouTube嵌入上限為 5 個；經濟面板與名片上限為 1 個)
    CARD_LIMITS: Dict[str, int] = {
        'profile_header': 1,
        'bot_economy': 1,
        'text_card': 5,
        'social_link': 5,
        'media_embed': 5,
    }

    cleaned_widgets: List[Dict[str, Any]] = []
    widget_counts: Dict[str, int] = {}
    for idx, widget in enumerate(raw_widgets):
        if not isinstance(widget, dict):
            continue

        w_id = sanitize_text(widget.get('id', f'card-{idx}'), max_len=60)
        w_type = str(widget.get('type', '')).strip()
        if w_type not in ALLOWED_WIDGET_TYPES:
            continue

        # 檢查卡片類型數量上限
        current_type_count = widget_counts.get(w_type, 0)
        max_type_limit = CARD_LIMITS.get(w_type, 20)
        if current_type_count >= max_type_limit:
            continue
        widget_counts[w_type] = current_type_count + 1

        try:
            x = max(0, min(11, int(widget.get('x', 0))))
            y = max(0, int(widget.get('y', 0)))
            w = max(1, min(12, int(widget.get('w', 1))))
            h = max(1, min(12, int(widget.get('h', 1))))
        except (ValueError, TypeError):
            continue

        raw_content = widget.get('content', {})
        if not isinstance(raw_content, dict):
            raw_content = {}

        cleaned_content: Dict[str, Any] = {}
        for k, v in raw_content.items():
            k_clean = str(k).strip()[:40]
            if isinstance(v, str):
                # 支援較長之 Markdown 內容，提升至 10000 字元
                cleaned_val = sanitize_text(v, max_len=10000)
                # 檢查 URL 欄位安全性
                if 'url' in k_clean.lower() or 'link' in k_clean.lower():
                    if not is_safe_url(cleaned_val):
                        return jsonify({"success": False, "error": f"偵測到不安全之網址格式：{k_clean}"}), 400
                cleaned_content[k_clean] = cleaned_val
            elif isinstance(v, (int, float, bool)):
                cleaned_content[k_clean] = v
            elif isinstance(v, list):
                if k_clean == 'links':
                    cleaned_links = []
                    for item in v[:30]:
                        if isinstance(item, dict):
                            l_url = sanitize_text(item.get("url", ""), max_len=500)
                            if l_url and not is_safe_url(l_url):
                                l_url = "#"
                            cleaned_links.append({
                                "id": sanitize_text(item.get("id", ""), max_len=30),
                                "platform": sanitize_text(item.get("platform", "custom"), max_len=30).lower(),
                                "username": sanitize_text(item.get("username", ""), max_len=60),
                                "url": l_url,
                                "style": sanitize_text(item.get("style", "inline"), max_len=20),
                                "avatar_url": sanitize_text(item.get("avatar_url", ""), max_len=500) if is_safe_url(item.get("avatar_url", "")) else "",
                                "use_platform_icon": bool(item.get("use_platform_icon", False)),
                            })
                    cleaned_content['links'] = cleaned_links
                else:
                    # 支援標籤陣列 (Hashtags)，最多 5 個標籤，每個標籤最長 40 字元
                    cleaned_content[k_clean] = [
                        sanitize_text(str(item).lstrip('#'), max_len=40)
                        for item in v if isinstance(item, (str, int, float))
                    ][:5]
            elif v is None:
                cleaned_content[k_clean] = None

        cleaned_widgets.append({
            "id": w_id,
            "type": w_type,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "content": cleaned_content,
        })

    # 個人基礎資訊卡片禁用刪除：若提交中遺失 profile_header，自動補回舊有 header
    has_header = any(w.get('type') == 'profile_header' for w in cleaned_widgets)
    if not has_header:
        orig_widgets = profile.get('layout_widgets', [])
        orig_header = next((w for w in orig_widgets if w.get('type') == 'profile_header'), None)
        if orig_header:
            cleaned_widgets.insert(0, orig_header)
        else:
            cleaned_widgets.insert(0, {
                "id": "card-header",
                "type": "profile_header",
                "x": 0, "y": 0, "w": 8, "h": 2,
                "content": {
                    "display_name": profile.get("display_name", target_username),
                    "username": target_username,
                    "hashtags": ["炙彗社群", "次世代機器人"],
                    "bio": profile.get("bio", ""),
                    "avatar_url": profile.get("avatar_url", "/static/logo.png")
                }
            })

    # 3.2 驗證 theme_config
    raw_theme = data.get('theme_config')
    if not isinstance(raw_theme, dict):
        raw_theme = profile.get('theme_config', {})

    cleaned_theme = {
        "theme_id": sanitize_text(raw_theme.get('theme_id', 'sky'), max_len=30),
        "glow_color": sanitize_text(raw_theme.get('glow_color', '#38bdf8'), max_len=20),
        "card_radius": sanitize_text(raw_theme.get('card_radius', '16px'), max_len=15),
        "bg_style": sanitize_text(raw_theme.get('bg_style', 'default'), max_len=30),
        "section_splits": [
            int(s) for s in raw_theme.get('section_splits', [])
            if isinstance(s, (int, float))
        ][:20],
        "section_splits_x": [
            sanitize_text(str(s), max_len=30)
            for s in raw_theme.get('section_splits_x', [])
            if isinstance(s, (str, int, float))
        ][:30],
    }

    # 3.3 驗證基本資料 (display_name, bio, avatar_url)
    display_name = sanitize_text(data.get('display_name', profile.get('display_name', '')), max_len=50)
    bio = sanitize_text(data.get('bio', profile.get('bio', '')), max_len=300)
    avatar_url = sanitize_text(data.get('avatar_url', profile.get('avatar_url', '')), max_len=500)
    if avatar_url and not is_safe_url(avatar_url):
        return jsonify({"success": False, "error": "頭像網址必須以 http:// 或 https:// 開頭"}), 400

    # 4. 寫入 SQLite
    success = upsert_user_profile(
        user_id=profile['user_id'],
        username=profile['username'],
        display_name=display_name or profile['username'],
        avatar_url=avatar_url or profile.get('avatar_url', '/static/logo.png'),
        bio=bio,
        theme_config=cleaned_theme,
        layout_widgets=cleaned_widgets,
    )

    if success:
        return jsonify({
            "success": True,
            "message": "個人檔案已成功儲存",
        })
    else:
        return jsonify({
            "success": False,
            "error": "資料庫寫入失敗，請稍後再試",
        }), 500

