import datetime
import json
import math
import os
import sqlite3
import threading
import time
from typing import Optional, Dict, List, Any

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'bot_database.db')

# 舊的 JSON 檔案路徑（用於初次自動遷移資料）
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
WELCOME_JSON = os.path.join(BASE_DIR, 'welcome_settings.json')
MUSIC_JSON = os.path.join(BASE_DIR, 'music_settings.json')
AUTO_REPLY_JSON = os.path.join(BASE_DIR, 'auto_reply.json')
REACTION_ROLES_JSON = os.path.join(BASE_DIR, 'reaction_roles.json')
COOLDOWNS_JSON = os.path.join(BASE_DIR, 'reaction_roles_cooldowns.json')

_lock = threading.RLock()


def get_connection() -> sqlite3.Connection:
    """取得 SQLite 連線並啟用 WAL 模式與外鍵限制。"""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    with _lock:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    """初始化 SQLite 資料表，並自動將現有的 .json 檔案資料遷移至資料庫（不遺失既有設定）。"""
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()

            # 1. 歡迎訊息設定表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS welcome_settings (
                guild_id TEXT PRIMARY KEY,
                welcome_channel_id INTEGER,
                rules_channel_id INTEGER,
                role_channel_id INTEGER,
                auto_role_id INTEGER
            );
            """)
            # 舊版資料庫沒有 auto_role_id 欄位：就地補上（既有設定不受影響）
            cursor.execute("PRAGMA table_info(welcome_settings);")
            if 'auto_role_id' not in {row[1] for row in cursor.fetchall()}:
                cursor.execute("ALTER TABLE welcome_settings ADD COLUMN auto_role_id INTEGER;")

            # 2. 音樂頻道設定表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS music_settings (
                guild_id TEXT PRIMARY KEY,
                music_channel_id INTEGER
            );
            """)

            # 3. 自動回覆規則表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS auto_reply_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                name TEXT NOT NULL,
                keywords TEXT NOT NULL,
                replies TEXT NOT NULL,
                channel_id INTEGER,
                is_paused INTEGER NOT NULL DEFAULT 0
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_auto_reply_guild ON auto_reply_rules (guild_id);")

            # 4. 表符身分組訊息表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS reaction_roles_messages (
                message_id INTEGER PRIMARY KEY,
                guild_id TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                strict INTEGER NOT NULL DEFAULT 1
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_reaction_roles_guild ON reaction_roles_messages (guild_id);")

            # 5. 表符配對表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS reaction_roles_pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                role_id INTEGER NOT NULL,
                FOREIGN KEY (message_id) REFERENCES reaction_roles_messages (message_id) ON DELETE CASCADE
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_reaction_roles_pairs_msg ON reaction_roles_pairs (message_id);")

            # 6. 表符限速冷卻表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS reaction_roles_cooldowns (
                key TEXT PRIMARY KEY,
                timestamp REAL NOT NULL
            );
            """)

            # 7. 外部 API 每日使用配額表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_api_usage (
                service TEXT PRIMARY KEY,
                usage_date TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0
            );
            """)

            # 8. 經濟系統全域使用者帳戶表 (含一般金幣與 AI 專屬貨幣)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS economy_users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0,
                last_daily_ts REAL NOT NULL DEFAULT 0,
                daily_streak INTEGER NOT NULL DEFAULT 0,
                last_hourly_ts REAL NOT NULL DEFAULT 0,
                total_earned INTEGER NOT NULL DEFAULT 0,
                total_wagered INTEGER NOT NULL DEFAULT 0,
                ai_points INTEGER NOT NULL DEFAULT 0
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_economy_balance ON economy_users (balance DESC);")

            # 9. 經濟系統後台參數設定表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS economy_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """)

            # 10. AI 智慧助手設定表 (OpenRouter 免費模型與個人化設定)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_settings (
                guild_id TEXT PRIMARY KEY,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                model TEXT NOT NULL DEFAULT 'openrouter/auto',
                instruction TEXT,
                channel_id INTEGER,
                temperature REAL NOT NULL DEFAULT 0.7,
                max_tokens INTEGER NOT NULL DEFAULT 3000,
                payer_user_id INTEGER
            );
            """)

            # 11. 伺服器 AI 功能授權名單表 (封測階段限定 Owner Guild 與獲授權之伺服器)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_authorized_guilds (
                guild_id TEXT PRIMARY KEY,
                guild_name TEXT DEFAULT '',
                authorized_at REAL NOT NULL,
                authorized_by INTEGER NOT NULL DEFAULT 0,
                notes TEXT DEFAULT ''
            );
            """)

            # ---------- 自動欄位安全遷移 (Migration) ----------
            cursor.execute("PRAGMA table_info(economy_users);")
            econ_cols = [r["name"] if isinstance(r, sqlite3.Row) else r[1] for r in cursor.fetchall()]
            if "ai_points" not in econ_cols:
                cursor.execute("ALTER TABLE economy_users ADD COLUMN ai_points INTEGER NOT NULL DEFAULT 0;")

            cursor.execute("PRAGMA table_info(ai_settings);")
            ai_cols = [r["name"] if isinstance(r, sqlite3.Row) else r[1] for r in cursor.fetchall()]
            if "payer_user_id" not in ai_cols:
                cursor.execute("ALTER TABLE ai_settings ADD COLUMN payer_user_id INTEGER;")

            conn.commit()

            # ---------- 自動遷移既有 JSON 資料 ----------
            _migrate_from_json(conn)
        finally:
            conn.close()


def _migrate_from_json(conn: sqlite3.Connection):
    """如果資料庫為空且存在既有的 JSON 檔案，自動將資料轉入 SQLite。"""
    cursor = conn.cursor()

    # 1. 遷移 welcome_settings.json
    cursor.execute("SELECT COUNT(*) FROM welcome_settings;")
    if cursor.fetchone()[0] == 0 and os.path.exists(WELCOME_JSON):
        try:
            with open(WELCOME_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for gid, val in data.items():
                    if isinstance(val, dict):
                        cursor.execute("""
                        INSERT OR REPLACE INTO welcome_settings (guild_id, welcome_channel_id, rules_channel_id, role_channel_id)
                        VALUES (?, ?, ?, ?)
                        """, (
                            str(gid),
                            val.get('welcome_channel_id'),
                            val.get('rules_channel_id'),
                            val.get('role_channel_id'),
                        ))
                conn.commit()
                print("✅ 已成功將 welcome_settings.json 遷移至 SQLite。")
        except Exception as e:
            print(f"⚠️ 遷移 welcome_settings.json 失敗: {e}")

    # 2. 遷移 music_settings.json
    cursor.execute("SELECT COUNT(*) FROM music_settings;")
    if cursor.fetchone()[0] == 0 and os.path.exists(MUSIC_JSON):
        try:
            with open(MUSIC_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for gid, val in data.items():
                    if isinstance(val, dict):
                        cursor.execute("""
                        INSERT OR REPLACE INTO music_settings (guild_id, music_channel_id)
                        VALUES (?, ?)
                        """, (str(gid), val.get('music_channel_id')))
                conn.commit()
                print("✅ 已成功將 music_settings.json 遷移至 SQLite。")
        except Exception as e:
            print(f"⚠️ 遷移 music_settings.json 失敗: {e}")

    # 3. 遷移 auto_reply.json
    cursor.execute("SELECT COUNT(*) FROM auto_reply_rules;")
    if cursor.fetchone()[0] == 0 and os.path.exists(AUTO_REPLY_JSON):
        try:
            with open(AUTO_REPLY_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for gid, entry in data.items():
                    rules = entry.get('rules') if isinstance(entry, dict) else entry
                    if isinstance(rules, list):
                        for r in rules:
                            if isinstance(r, dict):
                                kw = r.get('keywords') or ([r.get('keyword')] if r.get('keyword') else [])
                                rep = r.get('replies') or ([r.get('reply')] if r.get('reply') else [])
                                is_paused = 0 if r.get('enabled', True) else 1
                                chan_id = r.get('channel_id')
                                if chan_id is None and r.get('scope') and str(r.get('scope')) != 'all':
                                    try:
                                        chan_id = int(r.get('scope'))
                                    except (ValueError, TypeError):
                                        chan_id = None
                                rule_id = r.get('id')
                                if rule_id is not None:
                                    cursor.execute("""
                                    INSERT INTO auto_reply_rules (id, guild_id, name, keywords, replies, channel_id, is_paused)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                    """, (
                                        int(rule_id),
                                        str(gid),
                                        r.get('name') or '',
                                        json.dumps(kw, ensure_ascii=False),
                                        json.dumps(rep, ensure_ascii=False),
                                        chan_id,
                                        is_paused,
                                    ))
                                else:
                                    cursor.execute("""
                                    INSERT INTO auto_reply_rules (guild_id, name, keywords, replies, channel_id, is_paused)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                    """, (
                                        str(gid),
                                        r.get('name') or '',
                                        json.dumps(kw, ensure_ascii=False),
                                        json.dumps(rep, ensure_ascii=False),
                                        chan_id,
                                        is_paused,
                                    ))
                conn.commit()
                print("✅ 已成功將 auto_reply.json 遷移至 SQLite。")
        except Exception as e:
            print(f"⚠️ 遷移 auto_reply.json 失敗: {e}")

    # 4. 遷移 reaction_roles.json
    cursor.execute("SELECT COUNT(*) FROM reaction_roles_messages;")
    if cursor.fetchone()[0] == 0 and os.path.exists(REACTION_ROLES_JSON):
        try:
            with open(REACTION_ROLES_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for gid, entry in data.items():
                    if not isinstance(entry, dict):
                        continue
                    msgs = entry.get('messages')
                    if msgs is None and 'message_id' in entry:
                        msgs = [entry]
                    if isinstance(msgs, list):
                        for m in msgs:
                            msg_id = m.get('message_id')
                            chan_id = m.get('channel_id')
                            if not msg_id or not chan_id:
                                continue
                            strict_val = 1 if m.get('strict', True) else 0
                            cursor.execute("""
                            INSERT OR REPLACE INTO reaction_roles_messages (message_id, guild_id, channel_id, strict)
                            VALUES (?, ?, ?, ?)
                            """, (msg_id, str(gid), chan_id, strict_val))
                            for p in (m.get('pairs') or []):
                                cursor.execute("""
                                INSERT INTO reaction_roles_pairs (message_id, emoji, role_id)
                                VALUES (?, ?, ?)
                                """, (msg_id, str(p.get('emoji')), int(p.get('role_id'))))
                conn.commit()
                print("✅ 已成功將 reaction_roles.json 遷移至 SQLite。")
        except Exception as e:
            print(f"⚠️ 遷移 reaction_roles.json 失敗: {e}")

    # 5. 遷移 reaction_roles_cooldowns.json
    cursor.execute("SELECT COUNT(*) FROM reaction_roles_cooldowns;")
    if cursor.fetchone()[0] == 0 and os.path.exists(COOLDOWNS_JSON):
        try:
            with open(COOLDOWNS_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, ts in data.items():
                    cursor.execute("""
                    INSERT OR REPLACE INTO reaction_roles_cooldowns (key, timestamp)
                    VALUES (?, ?)
                    """, (str(k), float(ts)))
                conn.commit()
                print("✅ 已成功將 reaction_roles_cooldowns.json 遷移至 SQLite。")
        except Exception as e:
            print(f"⚠️ 遷移 reaction_roles_cooldowns.json 失敗: {e}")


# ==============================================================================
# Welcome Settings API
# ==============================================================================

def load_welcome_settings() -> dict:
    """讀取歡迎訊息設定：{guild_id: {'welcome_channel_id', 'rules_channel_id', 'role_channel_id', 'auto_role_id'}}"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT guild_id, welcome_channel_id, rules_channel_id, role_channel_id, auto_role_id FROM welcome_settings;")
            res = {}
            for row in cursor.fetchall():
                res[str(row['guild_id'])] = {
                    'welcome_channel_id': row['welcome_channel_id'],
                    'rules_channel_id': row['rules_channel_id'],
                    'role_channel_id': row['role_channel_id'],
                    'auto_role_id': row['auto_role_id'],
                }
            return res
        finally:
            conn.close()


def save_welcome_settings(data: dict):
    """完整儲存歡迎訊息設定。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM welcome_settings;")
            for gid, val in data.items():
                if isinstance(val, dict):
                    cursor.execute("""
                    INSERT INTO welcome_settings (guild_id, welcome_channel_id, rules_channel_id, role_channel_id, auto_role_id)
                    VALUES (?, ?, ?, ?, ?)
                    """, (
                        str(gid),
                        val.get('welcome_channel_id'),
                        val.get('rules_channel_id'),
                        val.get('role_channel_id'),
                        val.get('auto_role_id'),
                    ))
            conn.commit()
        finally:
            conn.close()


# ==============================================================================
# Music Settings API
# ==============================================================================

def load_music_settings() -> dict:
    """讀取點歌頻道設定：{guild_id: {'music_channel_id': int}}"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT guild_id, music_channel_id FROM music_settings;")
            res = {}
            for row in cursor.fetchall():
                res[str(row['guild_id'])] = {'music_channel_id': row['music_channel_id']}
            return res
        finally:
            conn.close()


def save_music_settings(data: dict):
    """完整儲存點歌頻道設定。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM music_settings;")
            for gid, val in data.items():
                if isinstance(val, dict):
                    cursor.execute("""
                    INSERT INTO music_settings (guild_id, music_channel_id)
                    VALUES (?, ?)
                    """, (str(gid), val.get('music_channel_id')))
            conn.commit()
        finally:
            conn.close()


# ==============================================================================
# AI Settings API (Multi-Provider Free LLM Architecture: Gemini / NVIDIA / Mistral)
# ==============================================================================

DEFAULT_AI_MODEL = "auto"
DEFAULT_AI_INSTRUCTION = (
    "你是 Discord 智慧助手「炙彗JI器人」（由「炙彗」開發）。"
    "請使用親切、禮貌且帶有一點幽默感的繁體中文直接回答問題，排版清晰美觀，善用條列式或粗體突顯重點。\n"
    "【對話準則】直接開始自然回答，嚴禁在開頭添加「xxx的我」等動作或心理前綴，亦切勿主動背誦自身身份設定。"
)
DEFAULT_AI_TEMPERATURE = 0.7
DEFAULT_AI_MAX_TOKENS = 4000

# 多供應商免費模型與容錯容災配置清單
AI_PROVIDER_MODELS = [
    {
        "id": "auto",
        "provider": "auto",
        "name": "auto (text, vision)",
        "desc": "自動容錯容災鏈",
    },
    {
        "id": "gemini:gemini-flash-latest",
        "provider": "gemini",
        "name": "gemini-flash-latest (text, vision)",
        "desc": "Google Gemini (預設)",
    },
    {
        "id": "nvidia:meta/llama-3.2-11b-vision-instruct",
        "provider": "nvidia",
        "name": "meta/llama-3.2-11b-vision-instruct (text, vision)",
        "desc": "NVIDIA NIM",
    },
    {
        "id": "mistral:ministral-8b-latest",
        "provider": "mistral",
        "name": "ministral-8b-latest (text, vision)",
        "desc": "Mistral AI",
    },
]

# 向下相容別名
OPENROUTER_FREE_MODELS = AI_PROVIDER_MODELS

# 各模型對話計費費率倍數（基準：ai_cost_per_1k_tokens / 1,000）
MODEL_TOKEN_RATES = {
    "auto": 1.0,
    "gemini:gemini-flash-latest": 1.0,
    "gemini-flash-latest": 1.0,
    "gemini:gemini-3.5-flash-lite": 1.0,
    "gemini-3.5-flash-lite": 1.0,
    "gemini:gemma-4-31b-it": 1.0,
    "gemma-4-31b-it": 1.0,
    "gemini:gemini-2.5-flash-lite": 1.0,
    "gemini-2.5-flash-lite": 1.0,
    "gemini:gemini-2.5-flash": 1.0,
    "gemini-2.5-flash": 1.0,
    "nvidia:meta/llama-3.2-11b-vision-instruct": 1.0,
    "meta/llama-3.2-11b-vision-instruct": 1.0,
    "mistral:ministral-8b-latest": 1.0,
    "ministral-8b-latest": 1.0,
    # 相容舊版模型名稱
    "groq:llama-3.3-70b-versatile": 1.0,
    "llama-3.3-70b-versatile": 1.0,
    "gemini:gemini-2.0-flash": 1.0,
    "gemini-2.0-flash": 1.0,
    "nvidia:meta/llama-3.3-70b-instruct": 1.2,
    "meta/llama-3.3-70b-instruct": 1.2,
    "mistral:mistral-small-latest": 1.0,
    "mistral-small-latest": 1.0,
    "openrouter/auto": 1.0,
}


def calculate_ai_token_cost(model: str, total_tokens: int) -> int:
    """根據模型與總消耗 Token 計算扣除之 AI 貨幣數量。"""
    try:
        cfg = load_economy_config()
        cost_per_1k = max(1, int(cfg.get("ai_cost_per_1k_tokens", 100)))
    except Exception:
        cost_per_1k = 100

    rate = MODEL_TOKEN_RATES.get(model, 1.0)
    # 依 Token 比例計算，至少扣除 1 點
    cost = max(1, math.ceil(total_tokens * (cost_per_1k / 1000.0) * rate))
    return int(cost)


def load_ai_settings() -> dict:
    """讀取所有伺服器的 AI 設定。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT guild_id, is_enabled, model, instruction, channel_id, temperature, max_tokens, payer_user_id FROM ai_settings;"
            )
            res = {}
            for row in cursor.fetchall():
                res[str(row['guild_id'])] = {
                    'is_enabled': bool(row['is_enabled']),
                    'model': row['model'] or DEFAULT_AI_MODEL,
                    'instruction': row['instruction'] if row['instruction'] is not None else DEFAULT_AI_INSTRUCTION,
                    'channel_id': row['channel_id'],
                    'temperature': float(row['temperature']) if row['temperature'] is not None else DEFAULT_AI_TEMPERATURE,
                    'max_tokens': DEFAULT_AI_MAX_TOKENS if (row['max_tokens'] is None or int(row['max_tokens']) <= 1000) else int(row['max_tokens']),
                    'payer_user_id': row['payer_user_id'],
                }
            return res
        finally:
            conn.close()


def get_guild_ai_settings(guild_id: str) -> dict:
    """取得特定伺服器的 AI 設定（若未特別設定則套用系統預設）。"""
    all_settings = load_ai_settings()
    if str(guild_id) in all_settings:
        return all_settings[str(guild_id)]
    return {
        'is_enabled': True,
        'model': DEFAULT_AI_MODEL,
        'instruction': DEFAULT_AI_INSTRUCTION,
        'channel_id': None,
        'temperature': DEFAULT_AI_TEMPERATURE,
        'max_tokens': DEFAULT_AI_MAX_TOKENS,
        'payer_user_id': None,
    }


def save_guild_ai_settings(guild_id: str, settings: dict) -> None:
    """儲存或更新指定伺服器的 AI 設定。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            is_enabled = 1 if settings.get('is_enabled', True) else 0
            model = str(settings.get('model') or DEFAULT_AI_MODEL).strip()
            instruction = settings.get('instruction')
            if instruction is None:
                instruction = DEFAULT_AI_INSTRUCTION
            else:
                instruction = str(instruction).strip()

            channel_id = settings.get('channel_id')
            if channel_id:
                try:
                    channel_id = int(channel_id)
                except (ValueError, TypeError):
                    channel_id = None
            else:
                channel_id = None

            try:
                temperature = float(settings.get('temperature', DEFAULT_AI_TEMPERATURE))
                temperature = max(0.0, min(1.0, temperature))
            except (ValueError, TypeError):
                temperature = DEFAULT_AI_TEMPERATURE

            try:
                max_tokens = int(settings.get('max_tokens', DEFAULT_AI_MAX_TOKENS))
                max_tokens = max(100, min(4000, max_tokens))
            except (ValueError, TypeError):
                max_tokens = DEFAULT_AI_MAX_TOKENS

            payer_user_id = settings.get('payer_user_id')
            if payer_user_id:
                try:
                    payer_user_id = int(payer_user_id)
                except (ValueError, TypeError):
                    payer_user_id = None
            else:
                payer_user_id = None

            cursor.execute(
                """
                INSERT OR REPLACE INTO ai_settings (guild_id, is_enabled, model, instruction, channel_id, temperature, max_tokens, payer_user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(guild_id), is_enabled, model, instruction, channel_id, temperature, max_tokens, payer_user_id),
            )
            conn.commit()
        finally:
            conn.close()


def delete_guild_ai_settings(guild_id: str) -> None:
    """清除伺服器 AI 設定（還原為預設值）。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM ai_settings WHERE guild_id = ?;", (str(guild_id),))
            conn.commit()
        finally:
            conn.close()


def is_guild_ai_authorized(guild_id: Any, owner_guild_id: Optional[Any] = None) -> bool:
    """檢查指定伺服器是否具備使用 AI 智慧助手的封測授權。

    擁有者伺服器（Owner Guild）永遠預設為授權狀態。
    其他伺服器需在 ai_authorized_guilds 名單中方可使用。
    """
    if not guild_id:
        return False
    gid_str = str(guild_id).strip()
    if not gid_str:
        return False

    # 1. 檢查是否為擁有者伺服器 (永遠開通)
    resolved_owner_guild = owner_guild_id
    if not resolved_owner_guild:
        resolved_owner_guild = (
            os.getenv("DISCORD_OWNER_GUILD_ID")
            or os.getenv("OWNER_GUILD_ID")
            or "1477930950288478262"
        )
    if gid_str == str(resolved_owner_guild).strip():
        return True

    # 2. 查詢資料庫授權名單
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM ai_authorized_guilds WHERE guild_id = ? LIMIT 1;", (gid_str,))
            return cursor.fetchone() is not None
        finally:
            conn.close()


def get_ai_authorized_guilds() -> List[Dict[str, Any]]:
    """讀取所有已獲得 AI 功能封測授權的伺服器名單。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT guild_id, guild_name, authorized_at, authorized_by, notes FROM ai_authorized_guilds ORDER BY authorized_at DESC;"
            )
            res = []
            for row in cursor.fetchall():
                res.append({
                    "guild_id": str(row["guild_id"]),
                    "guild_name": row["guild_name"] or "",
                    "authorized_at": float(row["authorized_at"]),
                    "authorized_by": int(row["authorized_by"] or 0),
                    "notes": row["notes"] or "",
                })
            return res
        finally:
            conn.close()


def authorize_guild_ai(
    guild_id: Any,
    guild_name: str = "",
    authorized_by: int = 0,
    notes: str = "",
) -> None:
    """發放特定伺服器的 AI 功能封測授權。"""
    init_db()
    gid_str = str(guild_id).strip()
    if not gid_str:
        return
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO ai_authorized_guilds (guild_id, guild_name, authorized_at, authorized_by, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (gid_str, str(guild_name or "").strip(), time.time(), int(authorized_by or 0), str(notes or "").strip()),
            )
            conn.commit()
        finally:
            conn.close()


def revoke_guild_ai(guild_id: Any) -> bool:
    """撤銷特定伺服器的 AI 功能封測授權。"""
    init_db()
    gid_str = str(guild_id).strip()
    if not gid_str:
        return False
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM ai_authorized_guilds WHERE guild_id = ?;", (gid_str,))
            affected = cursor.rowcount > 0
            conn.commit()
            return affected
        finally:
            conn.close()


# ==============================================================================
# Auto Reply API
# ==============================================================================

def load_auto_reply_settings() -> dict:
    """讀取自動回覆設定：{guild_id: {'rules': [rule1, rule2, ...]}}"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, guild_id, name, keywords, replies, channel_id, is_paused FROM auto_reply_rules ORDER BY id ASC;")
            res: dict[str, dict] = {}
            for row in cursor.fetchall():
                gid = str(row['guild_id'])
                if gid not in res:
                    res[gid] = {'rules': []}
                try:
                    kw = json.loads(row['keywords'])
                except Exception:
                    kw = []
                try:
                    rep = json.loads(row['replies'])
                except Exception:
                    rep = []
                chan_id = row['channel_id']
                paused_flag = 1 if row['is_paused'] else 0
                res[gid]['rules'].append({
                    'id': row['id'],
                    'name': row['name'] or '',
                    'keywords': kw,
                    'keyword': kw[0] if kw else '',
                    'replies': rep,
                    'reply': rep[0] if rep else '',
                    'channel_id': chan_id,
                    'scope': str(chan_id) if chan_id else 'all',
                    'enabled': not bool(paused_flag),
                    'is_paused': paused_flag,
                })
            return res
        finally:
            conn.close()


def save_auto_reply_settings(data: dict):
    """完整儲存自動回覆設定。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM auto_reply_rules;")
            for gid, entry in data.items():
                rules = entry.get('rules') if isinstance(entry, dict) else entry
                if isinstance(rules, list):
                    for r in rules:
                        if isinstance(r, dict):
                            kw = r.get('keywords') or ([r.get('keyword')] if r.get('keyword') else [])
                            rep = r.get('replies') or ([r.get('reply')] if r.get('reply') else [])
                            if 'enabled' in r:
                                is_paused = 0 if r.get('enabled', True) else 1
                            else:
                                is_paused = 1 if r.get('is_paused') else 0
                            scope = r.get('scope')
                            if scope == 'all':
                                chan_id = None
                            elif scope:
                                try:
                                    chan_id = int(scope)
                                except (ValueError, TypeError):
                                    chan_id = r.get('channel_id')
                            else:
                                chan_id = r.get('channel_id')
                            rule_id = r.get('id')
                            if rule_id is not None:
                                cursor.execute("""
                                INSERT INTO auto_reply_rules (id, guild_id, name, keywords, replies, channel_id, is_paused)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                """, (
                                    int(rule_id),
                                    str(gid),
                                    r.get('name') or '',
                                    json.dumps(kw, ensure_ascii=False),
                                    json.dumps(rep, ensure_ascii=False),
                                    chan_id,
                                    is_paused,
                                ))
                            else:
                                cursor.execute("""
                                INSERT INTO auto_reply_rules (guild_id, name, keywords, replies, channel_id, is_paused)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """, (
                                    str(gid),
                                    r.get('name') or '',
                                    json.dumps(kw, ensure_ascii=False),
                                    json.dumps(rep, ensure_ascii=False),
                                    chan_id,
                                    is_paused,
                                ))
            conn.commit()
        finally:
            conn.close()


# ==============================================================================
# Reaction Roles API
# ==============================================================================

def load_reaction_roles() -> dict:
    """讀取表符身分組設定：{guild_id: {'messages': [...]}}"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT message_id, guild_id, channel_id, strict FROM reaction_roles_messages;")
            messages_by_guild: dict[str, list[dict]] = {}
            msg_map: dict[int, dict] = {}

            for row in cursor.fetchall():
                gid = str(row['guild_id'])
                msg_entry = {
                    'channel_id': row['channel_id'],
                    'message_id': row['message_id'],
                    'pairs': [],
                    'strict': bool(row['strict']),
                }
                messages_by_guild.setdefault(gid, []).append(msg_entry)
                msg_map[row['message_id']] = msg_entry

            cursor.execute("SELECT message_id, emoji, role_id FROM reaction_roles_pairs ORDER BY id ASC;")
            for row in cursor.fetchall():
                mid = row['message_id']
                if mid in msg_map:
                    msg_map[mid]['pairs'].append({
                        'emoji': row['emoji'],
                        'role_id': row['role_id'],
                    })

            return {gid: {'messages': msgs} for gid, msgs in messages_by_guild.items()}
        finally:
            conn.close()


def save_reaction_roles(data: dict):
    """完整儲存表符身分組設定。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM reaction_roles_pairs;")
            cursor.execute("DELETE FROM reaction_roles_messages;")

            for gid, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                msgs = entry.get('messages') or []
                for m in msgs:
                    msg_id = m.get('message_id')
                    chan_id = m.get('channel_id')
                    if not msg_id or not chan_id:
                        continue
                    strict_val = 1 if m.get('strict', True) else 0
                    cursor.execute("""
                    INSERT OR REPLACE INTO reaction_roles_messages (message_id, guild_id, channel_id, strict)
                    VALUES (?, ?, ?, ?)
                    """, (msg_id, str(gid), chan_id, strict_val))

                    for p in (m.get('pairs') or []):
                        cursor.execute("""
                        INSERT INTO reaction_roles_pairs (message_id, emoji, role_id)
                        VALUES (?, ?, ?)
                        """, (msg_id, str(p.get('emoji')), int(p.get('role_id'))))

            conn.commit()
        finally:
            conn.close()


def load_reaction_cooldowns() -> dict:
    """讀取表符限速冷卻狀態。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT key, timestamp FROM reaction_roles_cooldowns;")
            return {row['key']: float(row['timestamp']) for row in cursor.fetchall()}
        finally:
            conn.close()


def save_reaction_cooldowns(data: dict):
    """儲存表符限速冷卻狀態。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM reaction_roles_cooldowns;")
            for k, ts in data.items():
                cursor.execute("""
                INSERT OR REPLACE INTO reaction_roles_cooldowns (key, timestamp)
                VALUES (?, ?)
                """, (str(k), float(ts)))
            conn.commit()
        finally:
            conn.close()


def get_daily_api_usage(service: str) -> tuple[int, str]:
    """取得特定服務今日的累計呼叫次數及當前日期（若日期已跨日則自動回傳 0）。"""
    init_db()
    today_str = datetime.date.today().isoformat()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT usage_date, count FROM daily_api_usage WHERE service = ?;", (service,))
            row = cursor.fetchone()
            if row and row['usage_date'] == today_str:
                return int(row['count']), today_str
            return 0, today_str
        finally:
            conn.close()


def record_api_call_success(service: str) -> int:
    """呼叫成功後遞增該服務的今日計數，回傳遞增後的次數。"""
    init_db()
    today_str = datetime.date.today().isoformat()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT usage_date, count FROM daily_api_usage WHERE service = ?;", (service,))
            row = cursor.fetchone()
            if row and row['usage_date'] == today_str:
                new_count = int(row['count']) + 1
            else:
                new_count = 1
            cursor.execute("""
            INSERT OR REPLACE INTO daily_api_usage (service, usage_date, count)
            VALUES (?, ?, ?);
            """, (service, today_str, new_count))
            conn.commit()
            return new_count
        finally:
            conn.close()


def mark_api_quota_exhausted(service: str, limit: int = 100):
    """當外部 API 回傳 429 或配額用盡時，直接將今日計數標記為已達上限。"""
    init_db()
    today_str = datetime.date.today().isoformat()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT OR REPLACE INTO daily_api_usage (service, usage_date, count)
            VALUES (?, ?, ?);
            """, (service, today_str, limit))
            conn.commit()
        finally:
            conn.close()


# ==============================================================================
# Economy System API
# ==============================================================================

TZ_UTC8 = datetime.timezone(datetime.timedelta(hours=8))

DEFAULT_ECONOMY_CONFIG: dict[str, Any] = {
    # 貨幣與轉帳設定
    "currency_name": "金幣",
    "currency_emoji": "🪙",
    "transfer_enabled": True,
    "transfer_tax_percent": 5,
    "transfer_min": 10,

    # 初始資金與每日簽到設定（固定於每日 00:00 UTC+8 刷新）
    "initial_balance": 200,
    "daily_min": 100,
    "daily_max": 300,
    "daily_streak_bonus": 20,
    "daily_streak_max": 200,

    # 每小時簽到設定（固定於每小時整點 XX:00 UTC+8 刷新）
    "hourly_min": 20,
    "hourly_max": 60,

    # 小遊戲通用與賠率設定（擲硬幣 / 骰子 / 21點）
    "bet_min": 10,
    "bet_max": 50000,
    "coinflip_multiplier": 2.0,
    "dice_multiplier": 2.0,
    "blackjack_multiplier": 2.0,
    "blackjack_natural_multiplier": 2.5,

    # 拉霸機與刮刮樂設定
    "slots_jackpot_multiplier": 15.0,
    "slots_triple_multiplier": 5.0,
    "slots_pair_multiplier": 1.5,
    "scratch_cost": 100,
    "scratch_jackpot_multiplier": 10.0,
    "scratch_second_multiplier": 3.0,
    "scratch_third_multiplier": 1.5,

    # AI 專屬貨幣設定 (AI Points)
    "ai_currency_name": "AI幣",
    "ai_currency_emoji": "⚡",
    "ai_initial_balance": 1000,
    "ai_daily_reward": 1000,
    "ai_daily_streak_bonus": 100,
    "ai_daily_streak_max": 1000,
    "ai_cost_per_1k_tokens": 100,
}


def get_daily_window_utc8(now_ts: Optional[float] = None) -> tuple[float, float, float]:
    """計算 UTC+8 時區下的每日簽到時間視窗：
    回傳 (今日 00:00 時間戳, 明日 00:00 下次刷新時間戳, 昨日 00:00 時間戳)。
    """
    ts = time.time() if now_ts is None else float(now_ts)
    now_dt = datetime.datetime.fromtimestamp(ts, tz=TZ_UTC8)
    today_start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight_dt = today_start_dt + datetime.timedelta(days=1)
    yesterday_start_dt = today_start_dt - datetime.timedelta(days=1)
    return today_start_dt.timestamp(), next_midnight_dt.timestamp(), yesterday_start_dt.timestamp()


def get_hourly_window_utc8(now_ts: Optional[float] = None) -> tuple[float, float]:
    """計算 UTC+8 時區下的每小時整點簽到時間視窗：
    回傳 (本小時整點 XX:00 時間戳, 下個整點刷新時間戳)。
    """
    ts = time.time() if now_ts is None else float(now_ts)
    now_dt = datetime.datetime.fromtimestamp(ts, tz=TZ_UTC8)
    hour_start_dt = now_dt.replace(minute=0, second=0, microsecond=0)
    next_hour_dt = hour_start_dt + datetime.timedelta(hours=1)
    return hour_start_dt.timestamp(), next_hour_dt.timestamp()


def load_economy_config() -> dict[str, Any]:
    """讀取經濟系統後台參數設定（自動合併預設值、轉型，並執行上下限防呆校正）。"""
    init_db()
    config = dict(DEFAULT_ECONOMY_CONFIG)
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM economy_config;")
            for row in cursor.fetchall():
                k = row["key"]
                if k not in DEFAULT_ECONOMY_CONFIG:
                    continue
                default_val = DEFAULT_ECONOMY_CONFIG[k]
                try:
                    loaded_val = json.loads(row["value"])
                    if isinstance(default_val, bool):
                        config[k] = bool(loaded_val)
                    elif isinstance(default_val, int):
                        config[k] = int(float(loaded_val))
                    elif isinstance(default_val, float):
                        config[k] = float(loaded_val)
                    elif isinstance(default_val, str):
                        config[k] = str(loaded_val)
                    else:
                        config[k] = loaded_val
                except Exception:
                    pass

            # 防呆保護：避免資料庫中因手動修改出現「最高上限低於初始值」導致執行期例外
            if config["daily_max"] < config["daily_min"]:
                config["daily_max"] = config["daily_min"]
            if config["daily_streak_bonus"] > 0 and config["daily_streak_max"] < config["daily_streak_bonus"]:
                config["daily_streak_max"] = config["daily_streak_bonus"]
            if config["hourly_max"] < config["hourly_min"]:
                config["hourly_max"] = config["hourly_min"]
            if config["bet_max"] < config["bet_min"]:
                config["bet_max"] = config["bet_min"]

            return config
        finally:
            conn.close()


def save_economy_config(config: dict[str, Any]):
    """儲存經濟系統後台參數設定至 SQLite。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM economy_config;")
            for k, default_val in DEFAULT_ECONOMY_CONFIG.items():
                val = config.get(k, default_val)
                cursor.execute(
                    "INSERT OR REPLACE INTO economy_config (key, value) VALUES (?, ?);",
                    (str(k), json.dumps(val, ensure_ascii=False)),
                )
            conn.commit()
        finally:
            conn.close()


def get_economy_user(user_id: int) -> dict[str, Any]:
    """取得指定使用者的經濟帳戶資訊（若尚未建立則回傳預設 0 值結構）。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT user_id, balance, last_daily_ts, daily_streak, last_hourly_ts, total_earned, total_wagered, ai_points
                FROM economy_users
                WHERE user_id = ?;
                """,
                (int(user_id),),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "user_id": int(row["user_id"]),
                    "balance": int(row["balance"]),
                    "last_daily_ts": float(row["last_daily_ts"]),
                    "daily_streak": int(row["daily_streak"]),
                    "last_hourly_ts": float(row["last_hourly_ts"]),
                    "total_earned": int(row["total_earned"]),
                    "total_wagered": int(row["total_wagered"]),
                    "ai_points": int(row["ai_points"]) if "ai_points" in row.keys() else 0,
                }
            return {
                "user_id": int(user_id),
                "balance": 0,
                "last_daily_ts": 0.0,
                "daily_streak": 0,
                "last_hourly_ts": 0.0,
                "total_earned": 0,
                "total_wagered": 0,
                "ai_points": 0,
            }
        finally:
            conn.close()


def _ensure_user_row(cursor: sqlite3.Cursor, user_id: int):
    cursor.execute(
        """
        INSERT OR IGNORE INTO economy_users (user_id, balance, last_daily_ts, daily_streak, last_hourly_ts, total_earned, total_wagered, ai_points)
        VALUES (?, 0, 0, 0, 0, 0, 0, 0);
        """,
        (int(user_id),),
    )


def get_user_ai_points(user_id: int) -> int:
    """取得指定使用者的 AI 貨幣餘額。"""
    init_db()
    uid = int(user_id)
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            _ensure_user_row(cursor, uid)
            cursor.execute("SELECT ai_points FROM economy_users WHERE user_id = ?;", (uid,))
            row = cursor.fetchone()
            return int(row["ai_points"]) if row and "ai_points" in row.keys() else 0
        finally:
            conn.close()


def update_user_ai_points(
    user_id: int,
    delta: int,
    allow_negative: bool = False,
) -> tuple[bool, int]:
    """增減使用者的 AI 專屬貨幣 (ai_points)。
    當 delta < 0 且 allow_negative=False 時，若餘額不足則不扣款並回傳 (False, 目前餘額)。
    成功時回傳 (True, 更新後餘額)。
    """
    init_db()
    uid = int(user_id)
    delta_int = int(delta)
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            _ensure_user_row(cursor, uid)
            cursor.execute("SELECT ai_points FROM economy_users WHERE user_id = ?;", (uid,))
            curr = int(cursor.fetchone()["ai_points"])
            if delta_int < 0 and not allow_negative and curr + delta_int < 0:
                return False, curr

            new_pts = max(0, curr + delta_int) if not allow_negative else (curr + delta_int)
            cursor.execute("UPDATE economy_users SET ai_points = ? WHERE user_id = ?;", (new_pts, uid))
            conn.commit()
            return True, new_pts
        finally:
            conn.close()


def set_user_ai_points(user_id: int, new_points: int) -> int:
    """直接設定使用者的 AI 貨幣餘額（供管理員後台發放/調整使用）。"""
    init_db()
    uid = int(user_id)
    pts = max(0, int(new_points))
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            _ensure_user_row(cursor, uid)
            cursor.execute("UPDATE economy_users SET ai_points = ? WHERE user_id = ?;", (pts, uid))
            conn.commit()
            return pts
        finally:
            conn.close()


def update_economy_balance(
    user_id: int,
    delta: int,
    *,
    add_earned: int = 0,
    add_wagered: int = 0,
    allow_negative: bool = False,
) -> tuple[bool, int]:
    """原子化增減使用者餘額。

    當 delta < 0 且 allow_negative=False 時，若餘額不足則不會扣款並回傳 (False, 目前餘額)；
    成功時回傳 (True, 更新後餘額)。
    """
    init_db()
    uid = int(user_id)
    delta_int = int(delta)
    earned_int = max(0, int(add_earned))
    wagered_int = max(0, int(add_wagered))
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            _ensure_user_row(cursor, uid)
            cursor.execute("SELECT balance FROM economy_users WHERE user_id = ?;", (uid,))
            current_balance = int(cursor.fetchone()["balance"])
            if delta_int < 0 and not allow_negative and current_balance + delta_int < 0:
                return False, current_balance

            new_balance = max(0, current_balance + delta_int) if not allow_negative else (current_balance + delta_int)
            cursor.execute(
                """
                UPDATE economy_users
                SET balance = ?,
                    total_earned = total_earned + ?,
                    total_wagered = total_wagered + ?
                WHERE user_id = ?;
                """,
                (new_balance, earned_int, wagered_int, uid),
            )
            conn.commit()
            return True, new_balance
        finally:
            conn.close()


def set_economy_balance(user_id: int, new_balance: int) -> int:
    """直接設定指定使用者的餘額（供後台管理指令使用），回傳設定後的餘額。"""
    init_db()
    uid = int(user_id)
    target = max(0, int(new_balance))
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            _ensure_user_row(cursor, uid)
            cursor.execute(
                "UPDATE economy_users SET balance = ? WHERE user_id = ?;",
                (target, uid),
            )
            conn.commit()
            return target
        finally:
            conn.close()


def claim_economy_daily(
    user_id: int,
    now_ts: float,
    base_reward: int,
    streak_bonus_per_day: int,
    streak_bonus_max: int,
    initial_balance: int = 200,
    ai_daily_reward: Optional[int] = None,
    ai_streak_bonus_per_day: Optional[int] = None,
    ai_streak_bonus_max: Optional[int] = None,
    ai_initial_balance: Optional[int] = None,
) -> dict[str, Any]:
    """原子化執行每日簽到：以每日 00:00 (UTC+8) 為刷新基準，檢查當日是否已簽到、計算連簽天數與金幣/AI專屬貨幣獎勵並寫入資料庫。
    若為第一次使用簽到指令（last_daily_ts == 0 且 last_hourly_ts == 0），額外發放初始資金與初始 AI 幣。
    """
    init_db()
    uid = int(user_id)
    today_start_ts, next_midnight_ts, yesterday_start_ts = get_daily_window_utc8(now_ts)

    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            _ensure_user_row(cursor, uid)
            cursor.execute(
                "SELECT balance, last_daily_ts, daily_streak, last_hourly_ts, ai_points FROM economy_users WHERE user_id = ?;",
                (uid,),
            )
            row = cursor.fetchone()
            balance = int(row["balance"])
            last_ts = float(row["last_daily_ts"])
            old_streak = int(row["daily_streak"])
            last_hourly_ts = float(row["last_hourly_ts"])
            curr_ai_points = int(row["ai_points"]) if "ai_points" in row.keys() else 0

            # 若今日 00:00 (UTC+8) 之後已經簽到過，則需等到明日 00:00 (UTC+8)
            if last_ts >= today_start_ts:
                return {
                    "ok": False,
                    "next_available_ts": next_midnight_ts,
                    "balance": balance,
                    "streak": old_streak,
                    "ai_points": curr_ai_points,
                }

            # 首次使用任何簽到指令時，發放初始資金（預設 200 金幣）與初始 AI 貨幣
            is_first_checkin = (last_ts == 0.0 and last_hourly_ts == 0.0)
            init_bonus = max(0, int(initial_balance)) if is_first_checkin else 0

            # 判斷是否維持連簽：上次簽到落在「昨日 00:00 ~ 今日 00:00 (UTC+8)」之間即為連續簽到
            if last_ts >= yesterday_start_ts:
                new_streak = old_streak + 1
                streak_broken = False
            else:
                new_streak = 1
                streak_broken = old_streak > 1

            # 例外處理：若連簽加成上限設定低於單日加成初始值，以安全上限（不低於單日加成值）或 0 處理
            clean_per_day = max(0, int(streak_bonus_per_day))
            clean_max = max(0, int(streak_bonus_max))
            if clean_per_day > 0 and clean_max < clean_per_day:
                clean_max = clean_per_day

            bonus = min(max(0, (new_streak - 1) * clean_per_day), clean_max)
            total_reward = max(0, int(base_reward) + bonus + init_bonus)
            new_balance = balance + total_reward

            # 計算 AI 專屬貨幣發放（獲取方式依規則僅能從簽到或管理員後台獲得）
            econ_cfg = load_economy_config()
            ai_base = max(0, int(ai_daily_reward if ai_daily_reward is not None else econ_cfg.get("ai_daily_reward", 1000)))
            ai_s_bon = max(0, int(ai_streak_bonus_per_day if ai_streak_bonus_per_day is not None else econ_cfg.get("ai_daily_streak_bonus", 100)))
            ai_s_max = max(0, int(ai_streak_bonus_max if ai_streak_bonus_max is not None else econ_cfg.get("ai_daily_streak_max", 1000)))
            ai_init = max(0, int(ai_initial_balance if ai_initial_balance is not None else econ_cfg.get("ai_initial_balance", 1000))) if is_first_checkin else 0

            ai_bonus = min(max(0, (new_streak - 1) * ai_s_bon), ai_s_max)
            total_ai_reward = ai_base + ai_bonus + ai_init
            new_ai_points = curr_ai_points + total_ai_reward

            cursor.execute(
                """
                UPDATE economy_users
                SET balance = ?,
                    ai_points = ?,
                    last_daily_ts = ?,
                    daily_streak = ?,
                    total_earned = total_earned + ?
                WHERE user_id = ?;
                """,
                (new_balance, new_ai_points, float(now_ts), new_streak, total_reward, uid),
            )
            conn.commit()
            return {
                "ok": True,
                "base_reward": int(base_reward),
                "streak_bonus": bonus,
                "initial_bonus": init_bonus,
                "total_reward": total_reward,
                "new_balance": new_balance,
                "streak": new_streak,
                "streak_broken": streak_broken,
                "old_streak": old_streak,
                "next_available_ts": next_midnight_ts,
                "ai_reward": ai_base,
                "ai_streak_bonus": ai_bonus,
                "ai_initial_bonus": ai_init,
                "total_ai_reward": total_ai_reward,
                "new_ai_points": new_ai_points,
            }
        finally:
            conn.close()


def claim_economy_hourly(
    user_id: int,
    now_ts: float,
    reward: int,
    initial_balance: int = 200,
) -> dict[str, Any]:
    """原子化執行每小時簽到：以每小時整點 XX:00 (UTC+8) 為刷新基準，檢查本小時是否已領取並發放獎勵。
    若為第一次使用簽到指令（last_daily_ts == 0 且 last_hourly_ts == 0），額外發放初始資金 initial_balance。
    """
    init_db()
    uid = int(user_id)
    hour_start_ts, next_hour_ts = get_hourly_window_utc8(now_ts)

    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            _ensure_user_row(cursor, uid)
            cursor.execute(
                "SELECT balance, last_daily_ts, last_hourly_ts FROM economy_users WHERE user_id = ?;",
                (uid,),
            )
            row = cursor.fetchone()
            balance = int(row["balance"])
            last_daily_ts = float(row["last_daily_ts"])
            last_ts = float(row["last_hourly_ts"])

            if last_ts >= hour_start_ts:
                return {
                    "ok": False,
                    "next_available_ts": next_hour_ts,
                    "balance": balance,
                }

            is_first_checkin = (last_daily_ts == 0.0 and last_ts == 0.0)
            init_bonus = max(0, int(initial_balance)) if is_first_checkin else 0

            clean_reward = max(0, int(reward))
            total_reward = clean_reward + init_bonus
            new_balance = balance + total_reward
            cursor.execute(
                """
                UPDATE economy_users
                SET balance = ?,
                    last_hourly_ts = ?,
                    total_earned = total_earned + ?
                WHERE user_id = ?;
                """,
                (new_balance, float(now_ts), total_reward, uid),
            )
            conn.commit()
            return {
                "ok": True,
                "reward": clean_reward,
                "initial_bonus": init_bonus,
                "total_reward": total_reward,
                "new_balance": new_balance,
                "next_available_ts": next_hour_ts,
            }
        finally:
            conn.close()


def transfer_economy_balance(
    sender_id: int,
    receiver_id: int,
    amount: int,
    tax_percent: int,
) -> dict[str, Any]:
    """原子化轉帳：從發送者扣除 amount，接收者獲得扣除手續費後的淨額。"""
    init_db()
    s_uid = int(sender_id)
    r_uid = int(receiver_id)
    amt = int(amount)
    tax = max(0, int(amt * max(0, min(100, int(tax_percent))) // 100))
    net_amount = max(0, amt - tax)

    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            _ensure_user_row(cursor, s_uid)
            _ensure_user_row(cursor, r_uid)
            cursor.execute("SELECT balance FROM economy_users WHERE user_id = ?;", (s_uid,))
            s_bal = int(cursor.fetchone()["balance"])
            if s_bal < amt:
                return {"ok": False, "sender_balance": s_bal}

            cursor.execute("SELECT balance FROM economy_users WHERE user_id = ?;", (r_uid,))
            r_bal = int(cursor.fetchone()["balance"])

            new_s_bal = s_bal - amt
            new_r_bal = r_bal + net_amount

            cursor.execute(
                "UPDATE economy_users SET balance = ? WHERE user_id = ?;",
                (new_s_bal, s_uid),
            )
            cursor.execute(
                "UPDATE economy_users SET balance = ? WHERE user_id = ?;",
                (new_r_bal, r_uid),
            )
            conn.commit()
            return {
                "ok": True,
                "amount": amt,
                "tax": tax,
                "net_amount": net_amount,
                "sender_balance": new_s_bal,
                "receiver_balance": new_r_bal,
            }
        finally:
            conn.close()


def get_economy_leaderboard(
    limit: int = 10,
    user_id: Optional[int] = None,
    exclude_user_id: Optional[int] = None,
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """取得富豪排行榜前 limit 名（自動忽略 exclude_user_id 如機器人擁有者），以及可選的指定使用者排名資訊。"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            if exclude_user_id is not None:
                cursor.execute(
                    """
                    SELECT user_id, balance, daily_streak, total_earned
                    FROM economy_users
                    WHERE balance > 0 AND user_id != ?
                    ORDER BY balance DESC, total_earned DESC, user_id ASC
                    LIMIT ?;
                    """,
                    (int(exclude_user_id), int(limit)),
                )
            else:
                cursor.execute(
                    """
                    SELECT user_id, balance, daily_streak, total_earned
                    FROM economy_users
                    WHERE balance > 0
                    ORDER BY balance DESC, total_earned DESC, user_id ASC
                    LIMIT ?;
                    """,
                    (int(limit),),
                )
            top_list = [
                {
                    "rank": idx,
                    "user_id": int(r["user_id"]),
                    "balance": int(r["balance"]),
                    "daily_streak": int(r["daily_streak"]),
                    "total_earned": int(r["total_earned"]),
                }
                for idx, r in enumerate(cursor.fetchall(), start=1)
            ]

            user_rank_info = None
            if user_id is not None:
                uid = int(user_id)
                cursor.execute(
                    "SELECT balance, daily_streak, total_earned FROM economy_users WHERE user_id = ?;",
                    (uid,),
                )
                u_row = cursor.fetchone()
                u_bal = int(u_row["balance"]) if u_row else 0
                u_streak = int(u_row["daily_streak"]) if u_row else 0
                if exclude_user_id is not None and uid == int(exclude_user_id):
                    rank = None
                elif u_bal > 0:
                    if exclude_user_id is not None:
                        cursor.execute(
                            "SELECT COUNT(*) FROM economy_users WHERE balance > ? AND user_id != ?;",
                            (u_bal, int(exclude_user_id)),
                        )
                    else:
                        cursor.execute(
                            "SELECT COUNT(*) FROM economy_users WHERE balance > ?;",
                            (u_bal,),
                        )
                    rank = int(cursor.fetchone()[0]) + 1
                else:
                    rank = None
                user_rank_info = {
                    "user_id": uid,
                    "balance": u_bal,
                    "daily_streak": u_streak,
                    "rank": rank,
                }
            return top_list, user_rank_info
        finally:
            conn.close()


# ==============================================================================
# 網頁控制面板連結按鈕共用工具 (供各 Cog 指令回覆附加按鈕使用)
# ==============================================================================

def get_panel_url(guild_id: Optional[Any] = None, *, owner: bool = False) -> str:
    """取得網頁控制面板的完整網址（若提供 guild_id 則附上 #guild-<id>，若 owner=True 則附上 #owner）。"""
    base = (os.getenv("PANEL_URL") or os.getenv("PUBLIC_BASE_URL") or "").strip()
    if not base:
        redirect_uri = os.getenv("DISCORD_REDIRECT_URI", "https://patricksu.nkhost.dev/api/auth").strip()
        if "/api/auth" in redirect_uri:
            base = redirect_uri.split("/api/auth")[0].rstrip("/") + "/panel"
        else:
            base = "https://patricksu.nkhost.dev/panel"
    elif not base.rstrip("/").endswith("/panel"):
        base = base.rstrip("/") + "/panel"

    if owner:
        return f"{base}#owner"
    if guild_id is not None and str(guild_id).strip():
        return f"{base}#guild-{guild_id}"
    return base


def build_panel_link_button(
    guild_id: Optional[Any] = None,
    *,
    owner: bool = False,
    label: str = "🌐 前往控制面板",
    row: Optional[int] = None,
):
    """建立指向網頁控制面板的 Discord Link Button。"""
    import discord

    return discord.ui.Button(
        label=label,
        style=discord.ButtonStyle.link,
        url=get_panel_url(guild_id, owner=owner),
        row=row,
    )


def build_panel_link_view(
    guild_id: Optional[Any] = None,
    *,
    owner: bool = False,
    label: str = "🌐 前往控制面板",
):
    """建立僅包含一顆網頁控制面板連結按鈕的 Discord View。"""
    import discord

    view = discord.ui.View()
    view.add_item(build_panel_link_button(guild_id, owner=owner, label=label))
    return view


