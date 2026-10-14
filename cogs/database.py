import os
import json
import sqlite3
import threading
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
                role_channel_id INTEGER
            );
            """)

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
    """讀取歡迎訊息設定：{guild_id: {'welcome_channel_id', 'rules_channel_id', 'role_channel_id'}}"""
    init_db()
    with _lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT guild_id, welcome_channel_id, rules_channel_id, role_channel_id FROM welcome_settings;")
            res = {}
            for row in cursor.fetchall():
                res[str(row['guild_id'])] = {
                    'welcome_channel_id': row['welcome_channel_id'],
                    'rules_channel_id': row['rules_channel_id'],
                    'role_channel_id': row['role_channel_id'],
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
                    INSERT INTO welcome_settings (guild_id, welcome_channel_id, rules_channel_id, role_channel_id)
                    VALUES (?, ?, ?, ?)
                    """, (
                        str(gid),
                        val.get('welcome_channel_id'),
                        val.get('rules_channel_id'),
                        val.get('role_channel_id'),
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
                res[gid]['rules'].append({
                    'id': row['id'],
                    'name': row['name'] or '',
                    'keywords': kw,
                    'keyword': kw[0] if kw else '',
                    'replies': rep,
                    'reply': rep[0] if rep else '',
                    'channel_id': chan_id,
                    'scope': str(chan_id) if chan_id else 'all',
                    'enabled': not bool(row['is_paused']),
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
