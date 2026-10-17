import asyncio
import datetime
import importlib
import inspect
import math
import os
import random
import re
import sys
import time
import traceback
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import cogs.database as _db_mod

# 當透過 /reload_module 熱重載 cogs.economy 時，一併重新載入 cogs.database，
# 避免舊版 sys.modules['cogs.database'] 快取缺少新增的函式或新簽章。
try:
    _db_mod = importlib.reload(_db_mod)
except Exception:
    pass

from cogs.database import (
    DEFAULT_ECONOMY_CONFIG as _DB_DEFAULT_ECONOMY_CONFIG,
    claim_economy_daily as _db_claim_economy_daily,
    claim_economy_hourly as _db_claim_economy_hourly,
    get_economy_leaderboard as _db_get_economy_leaderboard,
    get_economy_user,
    get_user_ai_points,
    load_economy_config as _db_load_economy_config,
    save_economy_config,
    set_economy_balance,
    set_user_ai_points,
    transfer_economy_balance,
    update_economy_balance,
    update_user_ai_points,
    build_panel_link_button,
)

TZ_UTC8 = datetime.timezone(datetime.timedelta(hours=8))


def _fallback_get_daily_window_utc8(now_ts: Optional[float] = None) -> tuple[float, float, float]:
    """回傳 (今日 00:00 UTC+8 時間戳, 明日 00:00 UTC+8 時間戳, 昨日 00:00 UTC+8 時間戳)。"""
    ts = float(now_ts if now_ts is not None else time.time())
    dt_utc8 = datetime.datetime.fromtimestamp(ts, tz=TZ_UTC8)
    today_midnight = dt_utc8.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_midnight = today_midnight + datetime.timedelta(days=1)
    yesterday_midnight = today_midnight - datetime.timedelta(days=1)
    return (
        today_midnight.timestamp(),
        tomorrow_midnight.timestamp(),
        yesterday_midnight.timestamp(),
    )


def _fallback_get_hourly_window_utc8(now_ts: Optional[float] = None) -> tuple[float, float]:
    """回傳 (本小時整點 XX:00 UTC+8 時間戳, 下個小時整點 UTC+8 時間戳)。"""
    ts = float(now_ts if now_ts is not None else time.time())
    dt_utc8 = datetime.datetime.fromtimestamp(ts, tz=TZ_UTC8)
    current_hour = dt_utc8.replace(minute=0, second=0, microsecond=0)
    next_hour = current_hour + datetime.timedelta(hours=1)
    return current_hour.timestamp(), next_hour.timestamp()


get_daily_window_utc8 = getattr(_db_mod, "get_daily_window_utc8", _fallback_get_daily_window_utc8)
get_hourly_window_utc8 = getattr(_db_mod, "get_hourly_window_utc8", _fallback_get_hourly_window_utc8)

DEFAULT_ECONOMY_CONFIG = {
    k: v
    for k, v in _DB_DEFAULT_ECONOMY_CONFIG.items()
    if k not in ("daily_cooldown_hours", "hourly_cooldown_minutes")
}
DEFAULT_ECONOMY_CONFIG.setdefault("initial_balance", 200)


def load_economy_config() -> dict[str, Any]:
    cfg = _db_load_economy_config()
    cfg.pop("daily_cooldown_hours", None)
    cfg.pop("hourly_cooldown_minutes", None)
    cfg.setdefault("initial_balance", 200)
    if cfg.get("daily_max", 300) < cfg.get("daily_min", 100):
        cfg["daily_max"] = cfg["daily_min"]
    if cfg.get("daily_streak_bonus", 20) > 0 and cfg.get("daily_streak_max", 200) < cfg.get("daily_streak_bonus", 20):
        cfg["daily_streak_max"] = cfg["daily_streak_bonus"]
    if cfg.get("hourly_max", 60) < cfg.get("hourly_min", 20):
        cfg["hourly_max"] = cfg["hourly_min"]
    if cfg.get("bet_max", 50000) < cfg.get("bet_min", 10):
        cfg["bet_max"] = cfg["bet_min"]
    return cfg


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
    """相容新舊版 cogs.database.claim_economy_daily，確保每日 00:00 (UTC+8) 刷新、首次簽到初始資金與 AI 專屬貨幣發放。"""
    sig = inspect.signature(_db_claim_economy_daily)
    if "ai_daily_reward" in sig.parameters:
        return _db_claim_economy_daily(
            user_id=user_id,
            now_ts=now_ts,
            base_reward=base_reward,
            streak_bonus_per_day=streak_bonus_per_day,
            streak_bonus_max=streak_bonus_max,
            initial_balance=initial_balance,
            ai_daily_reward=ai_daily_reward,
            ai_streak_bonus_per_day=ai_streak_bonus_per_day,
            ai_streak_bonus_max=ai_streak_bonus_max,
            ai_initial_balance=ai_initial_balance,
        )
    if "initial_balance" in sig.parameters:
        return _db_claim_economy_daily(
            user_id=user_id,
            now_ts=now_ts,
            base_reward=base_reward,
            streak_bonus_per_day=streak_bonus_per_day,
            streak_bonus_max=streak_bonus_max,
            initial_balance=initial_balance,
        )
    # 若伺服器端 cogs/database.py 尚未同步更新，直接以 00:00 (UTC+8) + 初始資金規則操作資料庫
    _db_mod.init_db()
    uid = int(user_id)
    today_start_ts, next_midnight_ts, yesterday_start_ts = get_daily_window_utc8(now_ts)
    with _db_mod._lock:
        conn = _db_mod.get_connection()
        try:
            cursor = conn.cursor()
            _db_mod._ensure_user_row(cursor, uid)
            cursor.execute(
                "SELECT balance, last_daily_ts, daily_streak, last_hourly_ts, ai_points FROM economy_users WHERE user_id = ?;",
                (uid,),
            )
            row = cursor.fetchone()
            balance = int(row["balance"])
            last_ts = float(row["last_daily_ts"])
            old_streak = int(row["daily_streak"])
            last_hourly_ts = float(row["last_hourly_ts"])
            curr_ai_pts = int(row["ai_points"]) if "ai_points" in row.keys() else 0
            if last_ts >= today_start_ts:
                return {
                    "ok": False,
                    "next_available_ts": next_midnight_ts,
                    "balance": balance,
                    "streak": old_streak,
                    "ai_points": curr_ai_pts,
                }
            is_first_checkin = (last_ts == 0.0 and last_hourly_ts == 0.0)
            init_bonus = max(0, int(initial_balance)) if is_first_checkin else 0
            if last_ts >= yesterday_start_ts:
                new_streak = old_streak + 1
                streak_broken = False
            else:
                new_streak = 1
                streak_broken = old_streak > 1
            clean_per_day = max(0, int(streak_bonus_per_day))
            clean_max = max(0, int(streak_bonus_max))
            if clean_per_day > 0 and clean_max < clean_per_day:
                clean_max = clean_per_day
            bonus = min(max(0, (new_streak - 1) * clean_per_day), clean_max)
            total_reward = max(0, int(base_reward) + bonus + init_bonus)
            new_balance = balance + total_reward

            # AI 幣
            ai_base = 1000 if ai_daily_reward is None else int(ai_daily_reward)
            ai_s_bon = 100 if ai_streak_bonus_per_day is None else int(ai_streak_bonus_per_day)
            ai_s_max = 1000 if ai_streak_bonus_max is None else int(ai_streak_bonus_max)
            ai_init = (1000 if ai_initial_balance is None else int(ai_initial_balance)) if is_first_checkin else 0
            ai_bon = min(max(0, (new_streak - 1) * ai_s_bon), ai_s_max)
            tot_ai = ai_base + ai_bon + ai_init
            new_ai = curr_ai_pts + tot_ai

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
                (new_balance, new_ai, float(now_ts), new_streak, total_reward, uid),
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
                "ai_streak_bonus": ai_bon,
                "ai_initial_bonus": ai_init,
                "total_ai_reward": tot_ai,
                "new_ai_points": new_ai,
            }
        finally:
            conn.close()


def claim_economy_hourly(
    user_id: int,
    now_ts: float,
    reward: int,
    initial_balance: int = 200,
) -> dict[str, Any]:
    """相容新舊版 cogs.database.claim_economy_hourly，確保每小時整點 XX:00 刷新與首次簽到初始資金。"""
    sig = inspect.signature(_db_claim_economy_hourly)
    if "initial_balance" in sig.parameters:
        return _db_claim_economy_hourly(
            user_id=user_id,
            now_ts=now_ts,
            reward=reward,
            initial_balance=initial_balance,
        )
    _db_mod.init_db()
    uid = int(user_id)
    hour_start_ts, next_hour_ts = get_hourly_window_utc8(now_ts)
    with _db_mod._lock:
        conn = _db_mod.get_connection()
        try:
            cursor = conn.cursor()
            _db_mod._ensure_user_row(cursor, uid)
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


def get_economy_leaderboard(
    limit: int = 10,
    user_id: Optional[int] = None,
    exclude_user_id: Optional[int] = None,
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """相容新舊版 cogs.database.get_economy_leaderboard，支援自動忽略 exclude_user_id (owner_id)。"""
    sig = inspect.signature(_db_get_economy_leaderboard)
    if "exclude_user_id" in sig.parameters:
        return _db_get_economy_leaderboard(
            limit=limit,
            user_id=user_id,
            exclude_user_id=exclude_user_id,
        )
    _db_mod.init_db()
    with _db_mod._lock:
        conn = _db_mod.get_connection()
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


load_dotenv()

OWNER_ID_ENV = os.getenv("DISCORD_OWNER_ID") or os.getenv("OWNER_ID")
try:
    OWNER_ID_ENV = int(OWNER_ID_ENV) if OWNER_ID_ENV else None
except ValueError:
    OWNER_ID_ENV = None

OWNER_GUILD_ID = os.getenv("DISCORD_OWNER_GUILD_ID") or os.getenv("OWNER_GUILD_ID")
try:
    OWNER_GUILD_ID = int(OWNER_GUILD_ID) if OWNER_GUILD_ID else None
except ValueError:
    OWNER_GUILD_ID = None


def get_bot_owner_id(bot: Any) -> Optional[int]:
    """取得機器人擁有者 ID（優先讀取 bot.owner_id，若未設定則退回環境變數 DISCORD_OWNER_ID / OWNER_ID）。"""
    oid = getattr(bot, "owner_id", None)
    if oid is not None:
        return int(oid)
    return OWNER_ID_ENV


def owner_guild_only(command_func):
    """將斜線指令限制在開發者專屬伺服器 (OWNER_GUILD_ID) 中同步與顯示。"""
    if OWNER_GUILD_ID is None:
        return command_func
    return app_commands.guilds(OWNER_GUILD_ID)(command_func)


def owner_only(interaction: discord.Interaction) -> bool:
    """驗證互動者是否為機器人擁有者 (owner_id)。"""
    owner_id = get_bot_owner_id(interaction.client)
    return owner_id is not None and interaction.user.id == owner_id


# 互動式按鈕的閒置逾時秒數
VIEW_TIMEOUT = 120.0
# 斜線指令與互動按鈕冷卻秒數（防止大量刷指令與連點按鈕洗版）
ECONOMY_CMD_COOLDOWN_SECONDS = 5.0
ECONOMY_LEADERBOARD_COOLDOWN_SECONDS = 10.0
ECONOMY_GAME_COOLDOWN_SECONDS = 5.0
ECONOMY_BUTTON_COOLDOWN_SECONDS = 3.0


# ==============================================================================
# 撲克牌與 21 點輔助函式
# ==============================================================================

SUITS = ["♠️", "♥️", "♦️", "♣️"]
RANKS = [
    ("A", 11),
    ("2", 2),
    ("3", 3),
    ("4", 4),
    ("5", 5),
    ("6", 6),
    ("7", 7),
    ("8", 8),
    ("9", 9),
    ("10", 10),
    ("J", 10),
    ("Q", 10),
    ("K", 10),
]


def build_shoe(num_decks: int = 2) -> list[tuple[str, str, int]]:
    """建立洗好的牌靴，每張牌為 (花色, 點數文字, 基礎點數)。"""
    deck = [(suit, rank, val) for suit in SUITS for rank, val in RANKS] * num_decks
    random.shuffle(deck)
    return deck


def calculate_hand_value(hand: list[tuple[str, str, int]]) -> int:
    """計算 21 點手牌總點數（A 可自動在 11 與 1 之間切換避免爆牌）。"""
    total = sum(card[2] for card in hand)
    aces = sum(1 for card in hand if card[1] == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def format_hand(hand: list[tuple[str, str, int]], hide_second: bool = False) -> str:
    """格式化手牌顯示字串。若 hide_second=True 則將莊家第二張牌蓋牌。"""
    if not hand:
        return "（無）"
    if hide_second and len(hand) >= 2:
        first = f"`{hand[0][0]}{hand[0][1]}`"
        hidden = " ".join("`🂠`" for _ in hand[1:])
        return f"{first} {hidden}"
    return " ".join(f"`{suit}{rank}`" for suit, rank, _ in hand)


# ==============================================================================
# 拉霸機與刮刮樂輔助函式
# ==============================================================================

SLOT_SYMBOLS = [
    ("🍒", 30, "櫻桃"),
    ("🍋", 25, "檸檬"),
    ("🍇", 20, "葡萄"),
    ("🔔", 14, "金鐘"),
    ("💎", 7, "鑽石"),
    ("7️⃣", 4, "幸運七"),
]


def spin_slot_reels() -> list[str]:
    """依權重隨機轉出三個拉霸機圖案。"""
    symbols = [s[0] for s in SLOT_SYMBOLS]
    weights = [s[1] for s in SLOT_SYMBOLS]
    return random.choices(symbols, weights=weights, k=3)


def evaluate_slots(reels: list[str], config: dict[str, Any]) -> tuple[float, str]:
    """評估拉霸機結果，回傳 (賠率倍數, 結果描述)。"""
    a, b, c = reels[0], reels[1], reels[2]
    if a == b == c:
        if a in ("7️⃣", "💎"):
            mult = float(config.get("slots_jackpot_multiplier", 15.0))
            return mult, f"🎉 **超級頭獎 ({a}{b}{c})！獲得 {mult:g} 倍獎金！**"
        mult = float(config.get("slots_triple_multiplier", 5.0))
        return mult, f"🎊 **三連線大獎 ({a}{b}{c})！獲得 {mult:g} 倍獎金！**"
    if a == b or b == c or a == c:
        mult = float(config.get("slots_pair_multiplier", 1.5))
        return mult, f"✨ **雙連線中獎！獲得 {mult:g} 倍獎金！**"
    return 0.0, "💨 **差一點點！沒有連線，再接再厲！**"


def generate_scratch_card(config: dict[str, Any]) -> dict[str, Any]:
    """產生一張 3x3（9 格）的刮刮樂盤面與對應獎項。

    規則：9 格中只要刮出 3 個相同的中獎圖案，即可獲得該圖案對應的最高獎項！
    - 💎 湊滿 3 個：頭獎 (`scratch_jackpot_multiplier` 倍)
    - 🌟 湊滿 3 個：貳獎 (`scratch_second_multiplier` 倍)
    - 🍀 湊滿 3 個：參獎 (`scratch_third_multiplier` 倍)
    - 🍒 湊滿 3 個：安慰獎 (1.0 倍，退還票價)
    - 未湊滿 3 個相同圖案：銘謝惠顧 (0 倍)
    """
    roll = random.random()
    # 機率分配：頭獎 3%、貳獎 10%、參獎 22%、安慰獎 20%、未中獎 45%
    if roll < 0.03:
        tier = "jackpot"
        win_symbol = "💎"
        mult = float(config.get("scratch_jackpot_multiplier", 10.0))
        prize_title = f"🏆 恭喜刮中【頭獎 💎×3】（{mult:g} 倍）！"
    elif roll < 0.13:
        tier = "second"
        win_symbol = "🌟"
        mult = float(config.get("scratch_second_multiplier", 3.0))
        prize_title = f"🎉 恭喜刮中【貳獎 🌟×3】（{mult:g} 倍）！"
    elif roll < 0.35:
        tier = "third"
        win_symbol = "🍀"
        mult = float(config.get("scratch_third_multiplier", 1.5))
        prize_title = f"✨ 恭喜刮中【參獎 🍀×3】（{mult:g} 倍）！"
    elif roll < 0.55:
        tier = "consolation"
        win_symbol = "🍒"
        mult = 1.0
        prize_title = "🍒 刮中【回本獎 🍒×3】（1 倍回本）！"
    else:
        tier = "none"
        win_symbol = None
        mult = 0.0
        prize_title = "🍂 銘謝惠顧！沒有湊滿 3 個相同圖案。"

    all_symbols = ["💎", "🌟", "🍀", "🍒", "🍋", "🔔", "🍉", "🪙"]
    board: list[str] = []
    counts: dict[str, int] = {s: 0 for s in all_symbols}

    if win_symbol is not None:
        for _ in range(3):
            board.append(win_symbol)
        counts[win_symbol] = 3

    # 填滿剩餘格子，確保其他非中獎圖案不會出現 >= 3 次
    remaining_slots = 9 - len(board)
    candidates = [s for s in all_symbols if s != win_symbol]
    for _ in range(remaining_slots):
        avail = [s for s in candidates if counts[s] < 2]
        chosen = random.choice(avail)
        board.append(chosen)
        counts[chosen] += 1

    random.shuffle(board)
    return {
        "tier": tier,
        "win_symbol": win_symbol,
        "multiplier": mult,
        "prize_title": prize_title,
        "board": board,
    }


# ==============================================================================
# 小遊戲互動介面 (Views)
# ==============================================================================

class BaseEconomyView(discord.ui.View):
    """經濟系統互動按鈕共用基底類別：限制僅發起者可操作、逾時自動停用按鈕、統一錯誤回報。"""

    def __init__(self, cog: "Economy", user_id: int, timeout: float = VIEW_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.user_id = user_id
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ 這是其他玩家的遊戲或面板，請自己輸入斜線指令遊玩喔！",
                ephemeral=True,
            )
            return False
        return True

    def disable_all_items(self):
        for item in self.children:
            if getattr(item, "style", None) == discord.ButtonStyle.link:
                continue
            if hasattr(item, "disabled"):
                item.disabled = True

    def enable_all_items(self):
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = False

    async def on_timeout(self):
        # 若使用者目前正在跑開獎動畫，不介入鎖定訊息（動畫結束時會自動替換為全新 View 並重新計算 120 秒）
        if self.user_id in self.cog._animating_users:
            return
        self.disable_all_items()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        print(f"❌ [EconomyView] 按鈕發生例外: {error}", file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        await self.cog.bot.notify_owner_error(error, interaction, extra_info=f"EconomyView item={item}")
        msg = "❌ 發生未知錯誤，已回報給開發者。"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class CustomBetModal(discord.ui.Modal, title="✏️ 自訂下注金額"):
    """讓玩家自由輸入下注金額或輸入 all 全押的彈出視窗。"""

    def __init__(self, panel_view: "BaseBettingPanelView"):
        super().__init__()
        self.panel_view = panel_view
        cfg = panel_view.cog.config
        b_min = int(cfg.get("bet_min", 10))
        b_max = int(cfg.get("bet_max", 50000))

        self.bet_input = discord.ui.TextInput(
            label=f"下注金額 ({b_min:,} ~ {b_max:,}，或輸入 all 全押)",
            placeholder=f"最低 {b_min:,} ~ 最高 {b_max:,}（高於自身餘額將自動改為全押）",
            required=True,
            max_length=15,
        )
        self.add_item(self.bet_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.bet_input.value.strip().lower().replace(",", "")
        cfg = self.panel_view.cog.config
        b_min = int(cfg.get("bet_min", 10))
        b_max = int(cfg.get("bet_max", 50000))
        cur = self.panel_view.cog.currency_str()
        bal = get_economy_user(interaction.user.id)["balance"]

        # Modal 提交在 discord.py 中屬於獨立 Interaction，不會自動刷新原 View 的 timeout，
        # 因此這裡建立一個相同狀態的全新面板實例取代舊 View，讓 120 秒計時器重新起算！
        old_view = self.panel_view
        fresh_view: BaseBettingPanelView = type(old_view)(
            old_view.cog,
            old_view.user,
            old_view.bet,
            is_all_in=old_view.is_all_in,
            played_once=old_view.played_once,
        )
        fresh_view.message = old_view.message or interaction.message
        old_view.message = None
        old_view.stop()

        if raw in ("all", "allin", "all in", "全押", "梭哈", "max"):
            await fresh_view.prompt_all_in_confirm(interaction)
            return

        try:
            val = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "❌ 請輸入有效的正整數金額，或輸入 `all` 代表全押！",
                ephemeral=True,
            )
            return

        if bal < b_min:
            fresh_view.sync_bet_buttons(balance=bal)
            await interaction.response.edit_message(embed=fresh_view.build_lobby_embed(balance=bal), view=fresh_view)
            await interaction.followup.send(
                f"❌ 餘額不足！最低下注需要 **{b_min:,}** {cur}，你目前只有 **{bal:,}** {cur}。",
                ephemeral=True,
            )
            return

        if val < b_min:
            await interaction.response.send_message(
                f"❌ 下注金額不能低於最低限制 **{b_min:,}** {cur}！",
                ephemeral=True,
            )
            return

        # 數字高於自身所有金額時，自動修改成自身擁有的所有金額
        if val > bal:
            val = bal

        # 當偵測到賭注 = 自身所有金額時，一樣跳出 All In 確認
        if val == bal:
            fresh_view.bet = min(bal, b_max)
            await fresh_view.prompt_all_in_confirm(interaction)
            return

        if val > b_max:
            await interaction.response.send_message(
                f"❌ 下注金額不能超過單局上限 **{b_max:,}** {cur}！",
                ephemeral=True,
            )
            return

        fresh_view.bet = val
        fresh_view.is_all_in = False
        fresh_view.confirming_all_in = False
        fresh_view.sync_bet_buttons(balance=bal)
        embed = fresh_view.build_lobby_embed(balance=bal)
        await interaction.response.edit_message(embed=embed, view=fresh_view)


class BaseBettingPanelView(BaseEconomyView):
    """支援互動式調整賭注（最低、減半、加倍、自訂金額、All In 二次確認）的遊戲面板基底類別。"""

    def __init__(
        self,
        cog: "Economy",
        user: discord.abc.User,
        initial_bet: Optional[int] = None,
        *,
        is_all_in: bool = False,
        played_once: bool = False,
    ):
        super().__init__(cog, user.id, timeout=VIEW_TIMEOUT)
        self.user = user
        self.is_all_in = is_all_in
        self.played_once = played_once
        self.confirming_all_in = False
        b_min = int(cog.config.get("bet_min", 10))
        b_max = int(cog.config.get("bet_max", 50000))
        bal = get_economy_user(user.id)["balance"]
        if bal < b_min:
            self.is_all_in = False
            self.bet = max(0, bal)
        elif self.is_all_in:
            self.bet = min(bal, b_max)
        elif initial_bet is not None:
            self.bet = max(b_min, min(b_max, bal, int(initial_bet)))
        else:
            default_target = min(max(b_min, 100), b_max)
            self.bet = max(b_min, min(bal, default_target))

    def sync_bet_buttons(self, balance: Optional[int] = None):
        """由子類別覆寫以動態更新按鈕上的下注金額文字與 All In 確認狀態。"""
        pass

    def build_lobby_embed(self, balance: Optional[int] = None) -> discord.Embed:
        """由子類別覆寫以建立該遊戲的準備面板 Embed。"""
        raise NotImplementedError

    async def prompt_all_in_confirm(self, interaction: discord.Interaction):
        """顯示 All In 二次確認訊息並透過編輯原訊息切換為「確認 All In / 取消」按鈕。"""
        bal = get_economy_user(interaction.user.id)["balance"]
        b_min = int(self.cog.config.get("bet_min", 10))
        b_max = int(self.cog.config.get("bet_max", 50000))
        cur = self.cog.currency_str()
        if bal < b_min:
            self.confirming_all_in = False
            self.is_all_in = False
            self.bet = max(0, bal)
            self.sync_bet_buttons(balance=bal)
            await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)
            await interaction.followup.send(
                f"❌ 餘額不足以 All In！最低下注需要 **{b_min:,}** {cur}，你目前只有 **{bal:,}** {cur}。",
                ephemeral=True,
            )
            return
        self.bet = min(bal, b_max)
        self.confirming_all_in = True
        self.sync_bet_buttons(balance=bal)
        await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)

    async def _adjust_bet_min(self, interaction: discord.Interaction):
        bal = get_economy_user(interaction.user.id)["balance"]
        b_min = int(self.cog.config.get("bet_min", 10))
        if bal < b_min:
            self.confirming_all_in = False
            self.is_all_in = False
            self.bet = max(0, bal)
            self.sync_bet_buttons(balance=bal)
            await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)
            return
        self.bet = b_min
        if self.bet == bal:
            await self.prompt_all_in_confirm(interaction)
            return
        self.is_all_in = False
        self.confirming_all_in = False
        self.sync_bet_buttons(balance=bal)
        await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)

    async def _adjust_bet_half(self, interaction: discord.Interaction):
        bal = get_economy_user(interaction.user.id)["balance"]
        b_min = int(self.cog.config.get("bet_min", 10))
        if bal < b_min:
            self.confirming_all_in = False
            self.is_all_in = False
            self.bet = max(0, bal)
            self.sync_bet_buttons(balance=bal)
            await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)
            return
        self.bet = max(b_min, min(bal, self.bet // 2))
        if self.bet == bal:
            await self.prompt_all_in_confirm(interaction)
            return
        self.is_all_in = False
        self.confirming_all_in = False
        self.sync_bet_buttons(balance=bal)
        await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)

    async def _adjust_bet_double(self, interaction: discord.Interaction):
        bal = get_economy_user(interaction.user.id)["balance"]
        b_min = int(self.cog.config.get("bet_min", 10))
        b_max = int(self.cog.config.get("bet_max", 50000))
        if bal < b_min:
            self.confirming_all_in = False
            self.is_all_in = False
            self.bet = max(0, bal)
            self.sync_bet_buttons(balance=bal)
            await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)
            return
        # 若加倍後高於自身所有金額，自動修改成自身擁有的所有金額
        new_bet = self.bet * 2
        if new_bet > bal:
            new_bet = bal
        if new_bet == bal:
            self.bet = min(bal, b_max)
            await self.prompt_all_in_confirm(interaction)
            return
        self.bet = max(b_min, min(b_max, new_bet))
        self.is_all_in = False
        self.confirming_all_in = False
        self.sync_bet_buttons(balance=bal)
        await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)


class SlotsPanelView(BaseBettingPanelView):
    """拉霸機互動下注與遊玩面板（支援調整賭注、自訂金額、All In 確認與再轉一次）。"""

    def __init__(
        self,
        cog: "Economy",
        user: discord.abc.User,
        initial_bet: Optional[int] = None,
        *,
        is_all_in: bool = False,
        played_once: bool = False,
    ):
        super().__init__(cog, user, initial_bet, is_all_in=is_all_in, played_once=played_once)
        self.sync_bet_buttons()

    def sync_bet_buttons(self, balance: Optional[int] = None):
        b_min = int(self.cog.config.get("bet_min", 10))
        b_max = int(self.cog.config.get("bet_max", 50000))
        bal = balance if balance is not None else get_economy_user(self.user_id)["balance"]
        self.min_btn.label = f"💵 最低 ({b_min:,})"

        # 若沒錢（低於最低下注額），直接將按鈕 disable
        if bal < b_min:
            self.confirming_all_in = False
            self.is_all_in = False
            self.bet = max(0, bal)
            action_prefix = "🔄 再轉一次" if self.played_once else "🎰 開始旋轉"
            self.spin_btn.label = f"{action_prefix} (下注: {self.bet:,})"
            self.spin_btn.style = discord.ButtonStyle.success
            self.all_in_spin_btn.label = "🔥 All In"
            self.all_in_spin_btn.style = discord.ButtonStyle.danger
            self.disable_all_items()
            return

        if self.is_all_in or self.bet > bal:
            self.bet = min(bal, b_max)

        if self.confirming_all_in:
            all_in_amt = min(bal, b_max)
            self.spin_btn.label = f"⚠️ 確認 All In ({all_in_amt:,})"
            self.spin_btn.style = discord.ButtonStyle.danger
            self.spin_btn.disabled = False
            self.all_in_spin_btn.label = "↩️ 取消 All In"
            self.all_in_spin_btn.style = discord.ButtonStyle.secondary
            self.all_in_spin_btn.disabled = False
            self.min_btn.disabled = True
            self.half_btn.disabled = True
            self.double_btn.disabled = True
            self.custom_btn.disabled = True
        else:
            action_prefix = "🔄 再轉一次" if self.played_once else "🎰 開始旋轉"
            self.spin_btn.label = f"{action_prefix} (下注: {self.bet:,})"
            self.spin_btn.style = discord.ButtonStyle.success
            self.spin_btn.disabled = False
            self.all_in_spin_btn.label = "🔥 All In"
            self.all_in_spin_btn.style = discord.ButtonStyle.danger
            self.all_in_spin_btn.disabled = False
            self.min_btn.disabled = False
            self.half_btn.disabled = False
            self.double_btn.disabled = False
            self.custom_btn.disabled = False

    def build_lobby_embed(self, balance: Optional[int] = None) -> discord.Embed:
        cfg = self.cog.config
        cur = self.cog.currency_str()
        bal = balance if balance is not None else get_economy_user(self.user_id)["balance"]
        b_min = int(cfg.get("bet_min", 10))
        b_max = int(cfg.get("bet_max", 50000))
        j_mult = float(cfg.get("slots_jackpot_multiplier", 15.0))
        t_mult = float(cfg.get("slots_triple_multiplier", 5.0))
        p_mult = float(cfg.get("slots_pair_multiplier", 1.5))

        if self.confirming_all_in:
            all_in_amt = min(bal, b_max)
            embed = discord.Embed(
                title="⚠️ 確認 All In 全押旋轉？",
                description=(
                    f"**玩家**：{self.user.mention} ｜ **目前餘額**：**{bal:,}** {cur}\n"
                    f"**即將全押投入**：🔥 **{all_in_amt:,}** {cur}（單局上限：`{b_max:,}`）\n\n"
                    "> ⚠️ **注意：All In 全押一旦未中獎將損失全部投入賭注！**\n"
                    "> 請點擊下方 **「⚠️ 確認 All In」** 立即啟動，或點擊 **「↩️ 取消 All In」** 返回。"
                ),
                color=discord.Color.orange(),
            )
            embed.set_footer(text="請確認是否要執行 All In 全押旋轉。")
            return embed

        bet_tag = f"**{self.bet:,}** {cur}" + (" 🔥 *(All In)*" if self.is_all_in else "")
        embed = discord.Embed(
            title="🎰 皇家拉霸機 (Slots)",
            description=(
                f"**玩家**：{self.user.mention} ｜ **目前餘額**：**{bal:,}** {cur}\n"
                f"**目前設定賭注**：{bet_tag}（單局範圍：`{b_min:,}` ~ `{b_max:,}`）\n\n"
                "> 🎰 **準備就緒，請點擊下方按鈕開始旋轉！** 🎰\n"
                "> # 【 🍒 ｜ 💎 ｜ 7️⃣ 】\n\n"
                f"🏆 **頭獎 (`7️⃣7️⃣7️⃣` / `💎💎💎`)**：`{j_mult:g}x` 倍\n"
                f"🎊 **一般三連線**：`{t_mult:g}x` 倍 ｜ ✨ **任意雙連線**：`{p_mult:g}x` 倍"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="可使用下方按鈕調整賭注、自訂金額，或點擊「🔥 All In」梭哈！")
        return embed

    @discord.ui.button(label="🎰 開始旋轉", style=discord.ButtonStyle.success, row=0)
    async def spin_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        bal = get_economy_user(interaction.user.id)["balance"]
        b_min = int(self.cog.config.get("bet_min", 10))
        b_max = int(self.cog.config.get("bet_max", 50000))
        cur = self.cog.currency_str()
        if bal < b_min:
            self.confirming_all_in = False
            self.is_all_in = False
            self.bet = max(0, bal)
            self.sync_bet_buttons(balance=bal)
            await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)
            await interaction.followup.send(
                f"❌ 餘額不足！最低下注需要 **{b_min:,}** {cur}，你目前只有 **{bal:,}** {cur}。",
                ephemeral=True,
            )
            return

        if self.confirming_all_in:
            self.confirming_all_in = False
            all_in_bet = min(bal, b_max)
            self.bet = all_in_bet
            self.is_all_in = True
            self.sync_bet_buttons(balance=bal)
            await self.cog._execute_slots(interaction, all_in_bet, panel_view=self, is_all_in=True)
            return

        if self.bet > bal:
            self.bet = min(bal, b_max)

        # 當偵測到賭注 = 自身所有金額時，一樣跳出 All In 確認
        if self.bet == bal:
            await self.prompt_all_in_confirm(interaction)
            return

        self.is_all_in = False
        await self.cog._execute_slots(interaction, self.bet, panel_view=self, is_all_in=False)

    @discord.ui.button(label="🔥 All In", style=discord.ButtonStyle.danger, row=0)
    async def all_in_spin_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.confirming_all_in:
            self.confirming_all_in = False
            self.is_all_in = False
            self.sync_bet_buttons()
            await interaction.response.edit_message(embed=self.build_lobby_embed(), view=self)
            return
        await self.prompt_all_in_confirm(interaction)

    @discord.ui.button(label="💵 最低", style=discord.ButtonStyle.secondary, row=1)
    async def min_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._adjust_bet_min(interaction)

    @discord.ui.button(label="➗ 減半", style=discord.ButtonStyle.secondary, row=1)
    async def half_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._adjust_bet_half(interaction)

    @discord.ui.button(label="✖️ 加倍", style=discord.ButtonStyle.secondary, row=1)
    async def double_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._adjust_bet_double(interaction)

    @discord.ui.button(label="✏️ 自訂賭注", style=discord.ButtonStyle.primary, row=1)
    async def custom_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CustomBetModal(self))


class GamblePanelView(BaseBettingPanelView):
    """擲硬幣 / 擲骰子比大小的互動下注與選擇面板（含 All In 二次確認模式、自訂金額與快速調整）。"""

    def __init__(
        self,
        cog: "Economy",
        user: discord.abc.User,
        initial_bet: Optional[int] = None,
        *,
        is_all_in: bool = False,
        played_once: bool = False,
    ):
        self._pending_gamble: Optional[tuple[str, str]] = None
        super().__init__(cog, user, initial_bet, is_all_in=is_all_in, played_once=played_once)
        self.sync_bet_buttons()

    def sync_bet_buttons(self, balance: Optional[int] = None):
        b_min = int(self.cog.config.get("bet_min", 10))
        b_max = int(self.cog.config.get("bet_max", 50000))
        bal = balance if balance is not None else get_economy_user(self.user_id)["balance"]
        choice_btns = (self.coin_heads, self.coin_tails, self.dice_big, self.dice_small, self.dice_odd, self.dice_even)

        # 若沒錢（低於最低下注額），直接將所有按鈕 disable
        if bal < b_min:
            self.confirming_all_in = False
            self.is_all_in = False
            self.bet = max(0, bal)
            self.min_btn.label = f"💵 最低 ({b_min:,})"
            self.min_btn.style = discord.ButtonStyle.secondary
            self.all_in_btn.label = "🔥 All In"
            self.all_in_btn.style = discord.ButtonStyle.danger
            self.disable_all_items()
            return

        if self.is_all_in or self.bet > bal:
            self.bet = min(bal, b_max)

        if self.confirming_all_in:
            all_in_amt = min(bal, b_max)
            for btn in choice_btns:
                btn.disabled = True
            self.all_in_btn.label = f"⚠️ 確認 All In ({all_in_amt:,})"
            self.all_in_btn.style = discord.ButtonStyle.danger
            self.all_in_btn.disabled = False
            self.min_btn.label = "↩️ 取消 All In"
            self.min_btn.style = discord.ButtonStyle.secondary
            self.min_btn.disabled = False
            self.half_btn.disabled = True
            self.double_btn.disabled = True
            self.custom_btn.disabled = True
        else:
            for btn in choice_btns:
                btn.disabled = False
            self.min_btn.label = f"💵 最低 ({b_min:,})"
            self.min_btn.style = discord.ButtonStyle.secondary
            self.min_btn.disabled = False
            self.half_btn.disabled = False
            self.double_btn.disabled = False
            self.custom_btn.disabled = False
            self.all_in_btn.disabled = False
            if self.is_all_in:
                self.all_in_btn.label = f"🔥 All In ({self.bet:,})"
                self.all_in_btn.style = discord.ButtonStyle.success
            else:
                self.all_in_btn.label = "🔥 All In"
                self.all_in_btn.style = discord.ButtonStyle.danger

    def build_lobby_embed(self, balance: Optional[int] = None) -> discord.Embed:
        cfg = self.cog.config
        cur = self.cog.currency_str()
        bal = balance if balance is not None else get_economy_user(self.user_id)["balance"]
        b_min = int(cfg.get("bet_min", 10))
        b_max = int(cfg.get("bet_max", 50000))
        c_mult = float(cfg.get("coinflip_multiplier", 2.0))
        d_mult = float(cfg.get("dice_multiplier", 2.0))

        if self.confirming_all_in:
            all_in_amt = min(bal, b_max)
            embed = discord.Embed(
                title="⚠️ 確認啟用 All In 全押模式？",
                description=(
                    f"**玩家**：{self.user.mention} ｜ **目前餘額**：**{bal:,}** {cur}\n"
                    f"**即將設定全押賭注**：🔥 **{all_in_amt:,}** {cur}（單局上限：`{b_max:,}`）\n\n"
                    "> ⚠️ **注意：確認啟用後，點選上方任一硬幣或骰子選項將直接全押開賭！**\n"
                    "> 請點擊下方 **「⚠️ 確認 All In」** 鎖定全押賭注，或點擊 **「↩️ 取消 All In」** 返回。"
                ),
                color=discord.Color.orange(),
            )
            embed.set_footer(text="請確認是否要將賭注切換為 All In 全押模式。")
            return embed

        if self.is_all_in and bal >= b_min:
            self.bet = min(bal, b_max)
            self.sync_bet_buttons(balance=bal)
        bet_tag = f"**{self.bet:,}** {cur}" + (" 🔥 *(All In)*" if self.is_all_in else "")

        embed = discord.Embed(
            title="🎲 擲硬幣 & 骰子比大小",
            description=(
                f"**玩家**：{self.user.mention} ｜ **目前餘額**：**{bal:,}** {cur}\n"
                f"**目前設定賭注**：{bet_tag}（單局範圍：`{b_min:,}` ~ `{b_max:,}`）\n"
                f"**獲勝賠率**：🪙 擲硬幣 `{c_mult:g}x` ｜ 🎲 擲骰子 `{d_mult:g}x`\n\n"
                "> 🎯 **請先確認下方賭注（或開啟 All In），再點擊要押注的硬幣／骰子項目立即開獎！**"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="點擊第 1、2 排的押注按鈕即可直接開賭；點擊第 3 排按鈕可調整賭注或點擊「🔥 All In」。")
        return embed

    async def _trigger_gamble(self, interaction: discord.Interaction, mode: str, choice: str):
        bal = get_economy_user(interaction.user.id)["balance"]
        b_min = int(self.cog.config.get("bet_min", 10))
        b_max = int(self.cog.config.get("bet_max", 50000))
        cur = self.cog.currency_str()
        if bal < b_min:
            self.confirming_all_in = False
            self.is_all_in = False
            self.bet = max(0, bal)
            self.sync_bet_buttons(balance=bal)
            await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)
            await interaction.followup.send(
                f"❌ 餘額不足！最低下注需要 **{b_min:,}** {cur}，你目前只有 **{bal:,}** {cur}。",
                ephemeral=True,
            )
            return

        if self.is_all_in:
            self.bet = min(bal, b_max)
            self.sync_bet_buttons(balance=bal)
            await self.cog._execute_gamble(
                interaction,
                self.bet,
                mode,
                choice,
                panel_view=self,
                is_all_in=True,
            )
            return

        if self.bet > bal:
            self.bet = min(bal, b_max)

        # 當偵測到賭注 = 自身所有金額時，一樣跳出 All In 確認
        if self.bet == bal:
            self._pending_gamble = (mode, choice)
            await self.prompt_all_in_confirm(interaction)
            return

        await self.cog._execute_gamble(
            interaction,
            self.bet,
            mode,
            choice,
            panel_view=self,
            is_all_in=False,
        )

    @discord.ui.button(label="🪙 硬幣：正面", style=discord.ButtonStyle.primary, row=0)
    async def coin_heads(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trigger_gamble(interaction, "coinflip", "正面")

    @discord.ui.button(label="🪙 硬幣：反面", style=discord.ButtonStyle.primary, row=0)
    async def coin_tails(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trigger_gamble(interaction, "coinflip", "反面")

    @discord.ui.button(label="🎲 骰子：大 (4~6)", style=discord.ButtonStyle.success, row=1)
    async def dice_big(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trigger_gamble(interaction, "dice_hl", "大")

    @discord.ui.button(label="🎲 骰子：小 (1~3)", style=discord.ButtonStyle.success, row=1)
    async def dice_small(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trigger_gamble(interaction, "dice_hl", "小")

    @discord.ui.button(label="🎲 骰子：單數", style=discord.ButtonStyle.secondary, row=1)
    async def dice_odd(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trigger_gamble(interaction, "dice_oe", "單")

    @discord.ui.button(label="🎲 骰子：雙數", style=discord.ButtonStyle.secondary, row=1)
    async def dice_even(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trigger_gamble(interaction, "dice_oe", "雙")

    @discord.ui.button(label="🔥 All In", style=discord.ButtonStyle.danger, row=2)
    async def all_in_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.confirming_all_in:
            self.confirming_all_in = False
            bal = get_economy_user(interaction.user.id)["balance"]
            b_min = int(self.cog.config.get("bet_min", 10))
            b_max = int(self.cog.config.get("bet_max", 50000))
            cur = self.cog.currency_str()
            if bal < b_min:
                self._pending_gamble = None
                self.sync_bet_buttons(balance=bal)
                await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)
                await interaction.followup.send(
                    f"❌ 餘額不足以 All In！最低下注需要 **{b_min:,}** {cur}，你目前只有 **{bal:,}** {cur}。",
                    ephemeral=True,
                )
                return
            self.bet = min(bal, b_max)
            self.is_all_in = True
            pending = self._pending_gamble
            self._pending_gamble = None
            if pending is not None:
                mode, choice = pending
                self.sync_bet_buttons(balance=bal)
                await self.cog._execute_gamble(
                    interaction,
                    self.bet,
                    mode,
                    choice,
                    panel_view=self,
                    is_all_in=True,
                )
                return
            self.sync_bet_buttons(balance=bal)
            await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)
            return
        self._pending_gamble = None
        await self.prompt_all_in_confirm(interaction)

    @discord.ui.button(label="💵 最低", style=discord.ButtonStyle.secondary, row=2)
    async def min_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.confirming_all_in:
            self.confirming_all_in = False
            self.is_all_in = False
            self._pending_gamble = None
            self.sync_bet_buttons()
            await interaction.response.edit_message(embed=self.build_lobby_embed(), view=self)
            return
        await self._adjust_bet_min(interaction)

    @discord.ui.button(label="➗ 減半", style=discord.ButtonStyle.secondary, row=2)
    async def half_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._adjust_bet_half(interaction)

    @discord.ui.button(label="✖️ 加倍", style=discord.ButtonStyle.secondary, row=2)
    async def double_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._adjust_bet_double(interaction)

    @discord.ui.button(label="✏️ 自訂賭注", style=discord.ButtonStyle.primary, row=2)
    async def custom_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CustomBetModal(self))


class BlackjackLobbyView(BaseBettingPanelView):
    """21 點互動下注與開局／重玩面板（支援調整賭注、自訂金額、All In 確認發牌）。"""

    def __init__(
        self,
        cog: "Economy",
        user: discord.abc.User,
        initial_bet: Optional[int] = None,
        *,
        is_all_in: bool = False,
        played_once: bool = False,
    ):
        super().__init__(cog, user, initial_bet, is_all_in=is_all_in, played_once=played_once)
        self.sync_bet_buttons()

    def sync_bet_buttons(self, balance: Optional[int] = None):
        b_min = int(self.cog.config.get("bet_min", 10))
        b_max = int(self.cog.config.get("bet_max", 50000))
        bal = balance if balance is not None else get_economy_user(self.user_id)["balance"]
        self.min_btn.label = f"💵 最低 ({b_min:,})"

        # 若沒錢（低於最低下注額），直接將按鈕 disable
        if bal < b_min:
            self.confirming_all_in = False
            self.is_all_in = False
            self.bet = max(0, bal)
            action_prefix = "🃏 再玩一局" if self.played_once else "🃏 開始發牌"
            self.deal_btn.label = f"{action_prefix} (下注: {self.bet:,})"
            self.deal_btn.style = discord.ButtonStyle.success
            self.all_in_deal_btn.label = "🔥 All In"
            self.all_in_deal_btn.style = discord.ButtonStyle.danger
            self.disable_all_items()
            return

        if self.is_all_in or self.bet > bal:
            self.bet = min(bal, b_max)

        if self.confirming_all_in:
            all_in_amt = min(bal, b_max)
            self.deal_btn.label = f"⚠️ 確認 All In ({all_in_amt:,})"
            self.deal_btn.style = discord.ButtonStyle.danger
            self.deal_btn.disabled = False
            self.all_in_deal_btn.label = "↩️ 取消 All In"
            self.all_in_deal_btn.style = discord.ButtonStyle.secondary
            self.all_in_deal_btn.disabled = False
            self.min_btn.disabled = True
            self.half_btn.disabled = True
            self.double_btn.disabled = True
            self.custom_btn.disabled = True
        else:
            action_prefix = "🃏 再玩一局" if self.played_once else "🃏 開始發牌"
            self.deal_btn.label = f"{action_prefix} (下注: {self.bet:,})"
            self.deal_btn.style = discord.ButtonStyle.success
            self.deal_btn.disabled = False
            self.all_in_deal_btn.label = "🔥 All In"
            self.all_in_deal_btn.style = discord.ButtonStyle.danger
            self.all_in_deal_btn.disabled = False
            self.min_btn.disabled = False
            self.half_btn.disabled = False
            self.double_btn.disabled = False
            self.custom_btn.disabled = False

    def build_lobby_embed(self, balance: Optional[int] = None) -> discord.Embed:
        cfg = self.cog.config
        cur = self.cog.currency_str()
        bal = balance if balance is not None else get_economy_user(self.user_id)["balance"]
        b_min = int(cfg.get("bet_min", 10))
        b_max = int(cfg.get("bet_max", 50000))
        bj_mult = float(cfg.get("blackjack_multiplier", 2.0))
        nat_mult = float(cfg.get("blackjack_natural_multiplier", 2.5))

        if self.confirming_all_in:
            all_in_amt = min(bal, b_max)
            embed = discord.Embed(
                title="⚠️ 確認 All In 全押發牌？",
                description=(
                    f"**玩家**：{self.user.mention} ｜ **目前餘額**：**{bal:,}** {cur}\n"
                    f"**即將全押投入**：🔥 **{all_in_amt:,}** {cur}（單局上限：`{b_max:,}`）\n\n"
                    "> ⚠️ **注意：All In 全押一旦落敗將損失全部投入賭注！**\n"
                    "> 請點擊下方 **「⚠️ 確認 All In」** 立即開局，或點擊 **「↩️ 取消 All In」** 返回。"
                ),
                color=discord.Color.orange(),
            )
            embed.set_footer(text="請確認是否要執行 All In 全押發牌。")
            return embed

        bet_tag = f"**{self.bet:,}** {cur}" + (" 🔥 *(All In)*" if self.is_all_in else "")

        embed = discord.Embed(
            title="🃏 21 點對決 (Blackjack)",
            description=(
                f"**玩家**：{self.user.mention} ｜ **目前餘額**：**{bal:,}** {cur}\n"
                f"**目前設定賭注**：{bet_tag}（單局範圍：`{b_min:,}` ~ `{b_max:,}`）\n\n"
                f"👑 **天生 Blackjack (起手 21 點)**：`{nat_mult:g}x` 倍\n"
                f"🎊 **一般點數獲勝 / 莊家爆牌**：`{bj_mult:g}x` 倍\n"
                f"🤝 **平手 (Push)**：全額退還賭注"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="請調整賭注後點擊「🃏 開始發牌」，或點擊「🔥 All In」梭哈！")
        return embed

    @discord.ui.button(label="🃏 開始發牌", style=discord.ButtonStyle.success, row=0)
    async def deal_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        bal = get_economy_user(interaction.user.id)["balance"]
        b_min = int(self.cog.config.get("bet_min", 10))
        b_max = int(self.cog.config.get("bet_max", 50000))
        cur = self.cog.currency_str()
        if bal < b_min:
            self.confirming_all_in = False
            self.is_all_in = False
            self.bet = max(0, bal)
            self.sync_bet_buttons(balance=bal)
            await interaction.response.edit_message(embed=self.build_lobby_embed(balance=bal), view=self)
            await interaction.followup.send(
                f"❌ 餘額不足！最低下注需要 **{b_min:,}** {cur}，你目前只有 **{bal:,}** {cur}。",
                ephemeral=True,
            )
            return

        if self.confirming_all_in:
            self.confirming_all_in = False
            all_in_bet = min(bal, b_max)
            self.bet = all_in_bet
            self.is_all_in = True
            self.sync_bet_buttons(balance=bal)
            await self.cog._start_blackjack(interaction, all_in_bet, is_all_in=True)
            return

        if self.bet > bal:
            self.bet = min(bal, b_max)

        # 當偵測到賭注 = 自身所有金額時，一樣跳出 All In 確認
        if self.bet == bal:
            await self.prompt_all_in_confirm(interaction)
            return

        self.is_all_in = False
        await self.cog._start_blackjack(interaction, self.bet, is_all_in=False)

    @discord.ui.button(label="🔥 All In", style=discord.ButtonStyle.danger, row=0)
    async def all_in_deal_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.confirming_all_in:
            self.confirming_all_in = False
            self.is_all_in = False
            self.sync_bet_buttons()
            await interaction.response.edit_message(embed=self.build_lobby_embed(), view=self)
            return
        await self.prompt_all_in_confirm(interaction)

    @discord.ui.button(label="💵 最低", style=discord.ButtonStyle.secondary, row=1)
    async def min_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._adjust_bet_min(interaction)

    @discord.ui.button(label="➗ 減半", style=discord.ButtonStyle.secondary, row=1)
    async def half_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._adjust_bet_half(interaction)

    @discord.ui.button(label="✖️ 加倍", style=discord.ButtonStyle.secondary, row=1)
    async def double_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._adjust_bet_double(interaction)

    @discord.ui.button(label="✏️ 自訂賭注", style=discord.ButtonStyle.primary, row=1)
    async def custom_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CustomBetModal(self))


class BlackjackView(BaseEconomyView):
    """21 點進行中的互動操作介面（抽牌 / 停牌 / 雙倍下注）。"""

    def __init__(
        self,
        cog: "Economy",
        user: discord.abc.User,
        bet: int,
        shoe: list[tuple[str, str, int]],
        player_hand: list[tuple[str, str, int]],
        dealer_hand: list[tuple[str, str, int]],
        *,
        is_all_in: bool = False,
    ):
        super().__init__(cog, user.id, timeout=120.0)
        self.user = user
        self.bet = bet
        self.is_all_in = is_all_in
        self.shoe = shoe
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand
        self.finished = False
        if get_economy_user(user.id)["balance"] < bet:
            self.double_button.disabled = True

    def build_embed(self, reveal_dealer: bool = False, result_text: Optional[str] = None, balance: Optional[int] = None, color: Optional[discord.Color] = None) -> discord.Embed:
        cur = self.cog.currency_str()
        p_val = calculate_hand_value(self.player_hand)
        d_val = calculate_hand_value(self.dealer_hand) if reveal_dealer else self.dealer_hand[0][2]
        d_val_str = f"**{d_val}** 點" if reveal_dealer else f"**{d_val}+ ?** 點"
        bet_str = f"**{self.bet:,}** {cur}" + (" 🔥 *(All In)*" if self.is_all_in else "")

        embed = discord.Embed(
            title="🃏 21 點對決 (Blackjack)",
            description=f"**玩家**：{self.user.mention} ｜ **本局賭注**：{bet_str}",
            color=color or discord.Color.blurple(),
        )
        embed.add_field(
            name=f"🧑 你的手牌（{p_val} 點）",
            value=format_hand(self.player_hand),
            inline=False,
        )
        embed.add_field(
            name=f"🎩 莊家手牌（{d_val_str}）",
            value=format_hand(self.dealer_hand, hide_second=not reveal_dealer),
            inline=False,
        )
        if result_text:
            embed.add_field(name="📊 對局結果", value=result_text, inline=False)
        if balance is not None:
            embed.set_footer(text=f"目前餘額：{balance:,} {self.cog.config['currency_name']}（可使用下方按鈕調整賭注、All In 或再玩一局）")
        else:
            embed.set_footer(text="請選擇「抽牌」、「停牌」或「雙倍下注」（120 秒未操作將自動停牌結算）")
        return embed

    async def _finish_game(self, interaction: Optional[discord.Interaction], reason_prefix: str = ""):
        if self.finished:
            return
        self.finished = True
        self.cog._active_blackjack_users.discard(self.user_id)
        self.disable_all_items()

        p_val = calculate_hand_value(self.player_hand)
        # 若玩家未爆牌，莊家依標準規則小於 17 點必須補牌
        if p_val <= 21:
            while calculate_hand_value(self.dealer_hand) < 17:
                self.dealer_hand.append(self.shoe.pop())
        d_val = calculate_hand_value(self.dealer_hand)

        cur = self.cog.currency_str()
        win_mult = float(self.cog.config.get("blackjack_multiplier", 2.0))

        if p_val > 21:
            payout = 0
            color = discord.Color.red()
            res_msg = f"💥 **你爆牌了（{p_val} 點）！** 莊家獲勝，損失 **{self.bet:,}** {cur}。"
        elif d_val > 21:
            payout = int(round(self.bet * win_mult))
            color = discord.Color.green()
            res_msg = f"🎉 **莊家爆牌（{d_val} 點）！你獲勝了！** 贏得 **{payout:,}** {cur}（淨賺 **+{payout - self.bet:,}**）！"
        elif p_val > d_val:
            payout = int(round(self.bet * win_mult))
            color = discord.Color.green()
            res_msg = f"🎊 **點數勝出（{p_val} vs {d_val}）！** 贏得 **{payout:,}** {cur}（淨賺 **+{payout - self.bet:,}**）！"
        elif p_val < d_val:
            payout = 0
            color = discord.Color.red()
            res_msg = f"😢 **莊家點數較高（{d_val} vs {p_val}）！** 損失 **{self.bet:,}** {cur}。"
        else:
            payout = self.bet
            color = discord.Color.gold()
            res_msg = f"🤝 **平手 Push（{p_val} 點）！** 已全額退還賭注 **{self.bet:,}** {cur}。"

        if reason_prefix:
            res_msg = f"{reason_prefix}\n{res_msg}"

        if payout > 0:
            net_earned = max(0, payout - self.bet)
            _, new_bal = update_economy_balance(self.user_id, payout, add_earned=net_earned)
        else:
            new_bal = get_economy_user(self.user_id)["balance"]

        embed = self.build_embed(reveal_dealer=True, result_text=res_msg, balance=new_bal, color=color)
        replay_view = BlackjackLobbyView(self.cog, self.user, self.bet, is_all_in=self.is_all_in, played_once=True)
        replay_view.message = self.message

        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=replay_view)
        elif self.message is not None:
            try:
                await self.message.edit(embed=embed, view=replay_view)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        if not self.finished:
            await self._finish_game(None, reason_prefix="⏰ **操作逾時，系統已自動為您停牌結算：**")

    @discord.ui.button(label="🃏 抽牌 (Hit)", style=discord.ButtonStyle.primary)
    async def hit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return
        # 抽一張牌後即不可再雙倍下注
        self.double_button.disabled = True
        self.player_hand.append(self.shoe.pop())
        p_val = calculate_hand_value(self.player_hand)
        if p_val >= 21:
            await self._finish_game(interaction)
        else:
            embed = self.build_embed(reveal_dealer=False)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🛑 停牌 (Stand)", style=discord.ButtonStyle.secondary)
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return
        await self._finish_game(interaction)

    @discord.ui.button(label="💰 雙倍下注 (Double)", style=discord.ButtonStyle.success)
    async def double_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return
        # 再扣除一次原賭注，抽一張牌後強制停牌結算
        ok, current_bal = update_economy_balance(self.user_id, -self.bet, add_wagered=self.bet)
        if not ok:
            cur = self.cog.currency_str()
            await interaction.response.send_message(
                f"❌ 餘額不足以雙倍下注！還需要 **{self.bet:,}** {cur}（目前餘額：**{current_bal:,}** {cur}）。",
                ephemeral=True,
            )
            return
        self.bet *= 2
        self.player_hand.append(self.shoe.pop())
        await self._finish_game(interaction, reason_prefix=f"🔥 **已雙倍下注！總賭注提升至 {self.bet:,} {self.cog.currency_str()}！**")


class ScratchSlotButton(discord.ui.Button):
    """刮刮樂 3x3 盤面上的單格刮開按鈕。"""

    def __init__(self, index: int):
        super().__init__(
            label="❓ 刮開",
            style=discord.ButtonStyle.secondary,
            row=index // 3,
            custom_id=f"scratch_cell_{index}",
        )
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        view: "ScratchCardView" = self.view  # type: ignore
        await view.reveal_cell(interaction, self.index)


class ScratchCardView(BaseEconomyView):
    """3x3 互動式刮刮樂介面：支援逐格刮開、一鍵全刮、刮完自動派彩與再買一張。"""

    def __init__(self, cog: "Economy", user: discord.abc.User, cost: int, card_data: dict[str, Any]):
        super().__init__(cog, user.id, timeout=180.0)
        self.user = user
        self.cost = cost
        self.card_data = card_data
        self.revealed: list[bool] = [False] * 9
        self.settled = False

        self.cell_buttons: list[ScratchSlotButton] = []
        for idx in range(9):
            btn = ScratchSlotButton(idx)
            self.cell_buttons.append(btn)
            self.add_item(btn)

        self.reveal_all_btn = discord.ui.Button(
            label="✨ 全部刮開",
            style=discord.ButtonStyle.primary,
            row=3,
        )
        self.reveal_all_btn.callback = self.reveal_all_callback
        self.add_item(self.reveal_all_btn)

    def build_embed(self, final_balance: Optional[int] = None) -> discord.Embed:
        cur = self.cog.currency_str()
        cfg = self.cog.config
        j_mult = float(cfg.get("scratch_jackpot_multiplier", 10.0))
        s_mult = float(cfg.get("scratch_second_multiplier", 3.0))
        t_mult = float(cfg.get("scratch_third_multiplier", 1.5))

        embed = discord.Embed(
            title="🎫 幸運九宮格刮刮樂",
            description=(
                f"**購買者**：{self.user.mention} ｜ **單張票價**：**{self.cost:,}** {cur}\n"
                f"**中獎規則**：在 9 格中刮出 **3 個相同圖案** 即可贏得對應獎金！\n"
                f"• 💎×3 頭獎：**{int(round(self.cost * j_mult)):,}** {cur}（{j_mult:g}x）\n"
                f"• 🌟×3 貳獎：**{int(round(self.cost * s_mult)):,}** {cur}（{s_mult:g}x）\n"
                f"• 🍀×3 參獎：**{int(round(self.cost * t_mult)):,}** {cur}（{t_mult:g}x）\n"
                f"• 🍒×3 回本獎：**{self.cost:,}** {cur}（1x）"
            ),
            color=discord.Color.gold() if not self.settled else (
                discord.Color.green() if self.card_data["multiplier"] > 0 else discord.Color.light_grey()
            ),
        )
        if self.settled:
            payout = int(round(self.cost * float(self.card_data["multiplier"])))
            prize_line = f"{self.card_data['prize_title']}\n"
            if payout > 0:
                prize_line += f"💰 **派彩金額**：獲得 **{payout:,}** {cur}！"
            else:
                prize_line += "💨 下次手氣一定更好，再試一張吧！"
            embed.add_field(name="🎊 開獎結果", value=prize_line, inline=False)
            if final_balance is not None:
                embed.set_footer(text=f"目前餘額：{final_balance:,} {cfg['currency_name']}")
        else:
            rem = sum(1 for r in self.revealed if not r)
            embed.set_footer(text=f"請點擊下方按鈕逐格刮開，或按「✨ 全部刮開」（剩餘 {rem} 格未刮）")
        return embed

    def _update_button_visuals(self):
        win_sym = self.card_data["win_symbol"]
        for idx, btn in enumerate(self.cell_buttons):
            if self.revealed[idx]:
                sym = self.card_data["board"][idx]
                btn.label = sym
                btn.disabled = True
                if self.settled and win_sym and sym == win_sym:
                    btn.style = discord.ButtonStyle.success
                else:
                    btn.style = discord.ButtonStyle.secondary

    async def _settle_card(self, interaction: Optional[discord.Interaction]):
        if self.settled:
            return
        self.settled = True
        for i in range(9):
            self.revealed[i] = True
        self._update_button_visuals()

        payout = int(round(self.cost * float(self.card_data["multiplier"])))
        if payout > 0:
            net_earned = max(0, payout - self.cost)
            _, new_bal = update_economy_balance(self.user_id, payout, add_earned=net_earned)
        else:
            new_bal = get_economy_user(self.user_id)["balance"]

        # 移除「全部刮開」按鈕，改放「🎫 再買一張」按鈕（若餘額不足則直接 disable）
        self.remove_item(self.reveal_all_btn)
        buy_again_btn = discord.ui.Button(
            label=f"🎫 再買一張 ({self.cost:,} {self.cog.config['currency_name']})",
            style=discord.ButtonStyle.primary,
            row=3,
            disabled=(new_bal < self.cost),
        )

        async def _buy_again_cb(inter: discord.Interaction):
            await self.cog._start_scratch(inter, edit_existing=True)

        buy_again_btn.callback = _buy_again_cb
        self.add_item(buy_again_btn)

        embed = self.build_embed(final_balance=new_bal)
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message is not None:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    async def reveal_cell(self, interaction: discord.Interaction, index: int):
        if self.settled:
            return
        self.revealed[index] = True
        if all(self.revealed):
            await self._settle_card(interaction)
        else:
            self._update_button_visuals()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def reveal_all_callback(self, interaction: discord.Interaction):
        await self._settle_card(interaction)

    async def on_timeout(self):
        if not self.settled:
            await self._settle_card(None)
        else:
            self.disable_all_items()
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass


# ==============================================================================
# 後台管理面板 (Modals & Views for @owner_guild_only)
# ==============================================================================

def _parse_int_field(raw: str, label: str, min_val: int = 0, max_val: int = 100_000_000) -> tuple[Optional[int], Optional[str]]:
    try:
        val = int(raw.strip())
    except ValueError:
        return None, f"• **{label}** 必須為整數（收到：`{raw}`）"
    if val < min_val or val > max_val:
        return None, f"• **{label}** 必須介於 `{min_val}` ~ `{max_val}` 之間（收到：`{val}`）"
    return val, None


def _parse_float_field(raw: str, label: str, min_val: float = 0.0, max_val: float = 10000.0) -> tuple[Optional[float], Optional[str]]:
    try:
        val = float(raw.strip())
    except ValueError:
        return None, f"• **{label}** 必須為數字（收到：`{raw}`）"
    if not math.isfinite(val) or val < min_val or val > max_val:
        return None, f"• **{label}** 必須介於 `{min_val}` ~ `{max_val}` 之間（收到：`{val}`）"
    return round(val, 2), None


class AdminDailyModal(discord.ui.Modal, title="📅 修改每日簽到參數"):
    def __init__(self, panel_view: "EconomyAdminView"):
        super().__init__()
        self.panel_view = panel_view
        cfg = panel_view.cog.config

        self.daily_min = discord.ui.TextInput(
            label="每日簽到最低獎勵 (整數)",
            default=str(cfg["daily_min"]),
            required=True,
            max_length=10,
        )
        self.daily_max = discord.ui.TextInput(
            label="每日簽到最高獎勵 (整數)",
            default=str(cfg["daily_max"]),
            required=True,
            max_length=10,
        )
        self.daily_streak_bonus = discord.ui.TextInput(
            label="連續簽到每多一天額外加成 (整數，初始加成)",
            default=str(cfg["daily_streak_bonus"]),
            required=True,
            max_length=10,
        )
        self.daily_streak_max = discord.ui.TextInput(
            label="連續簽到加成最高上限 (整數，0 為關閉連簽加成)",
            default=str(cfg["daily_streak_max"]),
            required=True,
            max_length=10,
        )
        for item in (self.daily_min, self.daily_max, self.daily_streak_bonus, self.daily_streak_max):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        errors: list[str] = []
        d_min, e1 = _parse_int_field(self.daily_min.value, "每日最低獎勵", 0)
        d_max, e2 = _parse_int_field(self.daily_max.value, "每日最高獎勵", 0)
        s_bon, e3 = _parse_int_field(self.daily_streak_bonus.value, "連簽每日加成", 0)
        s_max, e4 = _parse_int_field(self.daily_streak_max.value, "連簽加成上限", 0)
        for err in (e1, e2, e3, e4):
            if err:
                errors.append(err)
        if d_min is not None and d_max is not None and d_min > d_max:
            errors.append("• **每日最低獎勵** (`daily_min`) 不可大於 **每日最高獎勵** (`daily_max`)")
        if s_bon is not None and s_max is not None and s_bon > 0 and s_max < s_bon:
            errors.append(
                f"• **連簽加成最高上限** (`{s_max}`) 不可低於 **連簽每日初始加成** (`{s_bon}`)！（若欲關閉連簽加成，請將「連簽每日加成」設為 `0`）"
            )

        if errors:
            await interaction.response.send_message("❌ 參數驗證失敗：\n" + "\n".join(errors), ephemeral=True)
            return

        await self.panel_view.apply_config_updates(
            interaction,
            {
                "daily_min": d_min,
                "daily_max": d_max,
                "daily_streak_bonus": s_bon,
                "daily_streak_max": s_max,
            },
            "✅ 已更新「每日簽到」參數設定！（每日固定於 00:00 UTC+8 刷新）",
        )


class AdminHourlyModal(discord.ui.Modal, title="⏰ 修改每小時簽到參數"):
    def __init__(self, panel_view: "EconomyAdminView"):
        super().__init__()
        self.panel_view = panel_view
        cfg = panel_view.cog.config

        self.hourly_min = discord.ui.TextInput(
            label="每小時簽到最低獎勵 (整數)",
            default=str(cfg["hourly_min"]),
            required=True,
            max_length=10,
        )
        self.hourly_max = discord.ui.TextInput(
            label="每小時簽到最高獎勵 (整數)",
            default=str(cfg["hourly_max"]),
            required=True,
            max_length=10,
        )
        for item in (self.hourly_min, self.hourly_max):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        errors: list[str] = []
        h_min, e1 = _parse_int_field(self.hourly_min.value, "每小時最低獎勵", 0)
        h_max, e2 = _parse_int_field(self.hourly_max.value, "每小時最高獎勵", 0)
        for err in (e1, e2):
            if err:
                errors.append(err)
        if h_min is not None and h_max is not None and h_min > h_max:
            errors.append("• **每小時最低獎勵** (`hourly_min`) 不可大於 **每小時最高獎勵** (`hourly_max`)")

        if errors:
            await interaction.response.send_message("❌ 參數驗證失敗：\n" + "\n".join(errors), ephemeral=True)
            return

        await self.panel_view.apply_config_updates(
            interaction,
            {
                "hourly_min": h_min,
                "hourly_max": h_max,
            },
            "✅ 已更新「每小時簽到」參數設定！（每小時固定於整點 XX:00 刷新）",
        )


class AdminBetAndGambleModal(discord.ui.Modal, title="🎲 修改賭注上下限與硬幣/骰子賠率"):
    def __init__(self, panel_view: "EconomyAdminView"):
        super().__init__()
        self.panel_view = panel_view
        cfg = panel_view.cog.config

        self.bet_min = discord.ui.TextInput(
            label="小遊戲單局最低下注金額 (整數)",
            default=str(cfg["bet_min"]),
            required=True,
            max_length=10,
        )
        self.bet_max = discord.ui.TextInput(
            label="小遊戲單局最高下注金額 (整數)",
            default=str(cfg["bet_max"]),
            required=True,
            max_length=12,
        )
        self.coinflip_multiplier = discord.ui.TextInput(
            label="擲硬幣猜正反獲勝賠率 (如 2.0)",
            default=str(cfg["coinflip_multiplier"]),
            required=True,
            max_length=10,
        )
        self.dice_multiplier = discord.ui.TextInput(
            label="擲骰子猜大小/單雙獲勝賠率 (如 2.0)",
            default=str(cfg["dice_multiplier"]),
            required=True,
            max_length=10,
        )
        for item in (self.bet_min, self.bet_max, self.coinflip_multiplier, self.dice_multiplier):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        errors: list[str] = []
        b_min, e1 = _parse_int_field(self.bet_min.value, "最低下注金額", 1)
        b_max, e2 = _parse_int_field(self.bet_max.value, "最高下注金額", 1)
        c_mult, e3 = _parse_float_field(self.coinflip_multiplier.value, "擲硬幣賠率", 1.0, 1000.0)
        d_mult, e4 = _parse_float_field(self.dice_multiplier.value, "擲骰子賠率", 1.0, 1000.0)
        for err in (e1, e2, e3, e4):
            if err:
                errors.append(err)
        if b_min is not None and b_max is not None and b_min > b_max:
            errors.append("• **最低下注金額** 不可大於 **最高下注金額**")

        if errors:
            await interaction.response.send_message("❌ 參數驗證失敗：\n" + "\n".join(errors), ephemeral=True)
            return

        await self.panel_view.apply_config_updates(
            interaction,
            {
                "bet_min": b_min,
                "bet_max": b_max,
                "coinflip_multiplier": c_mult,
                "dice_multiplier": d_mult,
            },
            "✅ 已更新「小遊戲賭注上下限與硬幣／骰子賠率」！",
        )


class AdminBlackjackAndSlotsModal(discord.ui.Modal, title="🃏 修改 21點 與 🎰 拉霸機賠率"):
    def __init__(self, panel_view: "EconomyAdminView"):
        super().__init__()
        self.panel_view = panel_view
        cfg = panel_view.cog.config

        self.bj_mult = discord.ui.TextInput(
            label="21點一般獲勝賠率 (含本金，如 2.0)",
            default=str(cfg["blackjack_multiplier"]),
            required=True,
            max_length=10,
        )
        self.bj_nat_mult = discord.ui.TextInput(
            label="21點天生 Blackjack 賠率 (含本金，如 2.5)",
            default=str(cfg["blackjack_natural_multiplier"]),
            required=True,
            max_length=10,
        )
        self.slots_jackpot = discord.ui.TextInput(
            label="拉霸機頭獎 (777 / 💎💎💎) 賠率 (如 15.0)",
            default=str(cfg["slots_jackpot_multiplier"]),
            required=True,
            max_length=10,
        )
        self.slots_triple = discord.ui.TextInput(
            label="拉霸機一般三連線賠率 (如 5.0)",
            default=str(cfg["slots_triple_multiplier"]),
            required=True,
            max_length=10,
        )
        self.slots_pair = discord.ui.TextInput(
            label="拉霸機雙連線賠率 (如 1.5)",
            default=str(cfg["slots_pair_multiplier"]),
            required=True,
            max_length=10,
        )
        for item in (self.bj_mult, self.bj_nat_mult, self.slots_jackpot, self.slots_triple, self.slots_pair):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        errors: list[str] = []
        bj_m, e1 = _parse_float_field(self.bj_mult.value, "21點一般賠率", 1.0, 1000.0)
        bj_n, e2 = _parse_float_field(self.bj_nat_mult.value, "21點 Blackjack 賠率", 1.0, 1000.0)
        s_j, e3 = _parse_float_field(self.slots_jackpot.value, "拉霸機頭獎賠率", 1.0, 10000.0)
        s_t, e4 = _parse_float_field(self.slots_triple.value, "拉霸機三連線賠率", 1.0, 10000.0)
        s_p, e5 = _parse_float_field(self.slots_pair.value, "拉霸機雙連線賠率", 0.0, 1000.0)
        for err in (e1, e2, e3, e4, e5):
            if err:
                errors.append(err)

        if errors:
            await interaction.response.send_message("❌ 參數驗證失敗：\n" + "\n".join(errors), ephemeral=True)
            return

        await self.panel_view.apply_config_updates(
            interaction,
            {
                "blackjack_multiplier": bj_m,
                "blackjack_natural_multiplier": bj_n,
                "slots_jackpot_multiplier": s_j,
                "slots_triple_multiplier": s_t,
                "slots_pair_multiplier": s_p,
            },
            "✅ 已更新「21 點與拉霸機賠率」設定！",
        )


class AdminScratchModal(discord.ui.Modal, title="🎫 修改刮刮樂票價與倍率"):
    def __init__(self, panel_view: "EconomyAdminView"):
        super().__init__()
        self.panel_view = panel_view
        cfg = panel_view.cog.config

        self.scratch_cost = discord.ui.TextInput(
            label="刮刮樂單張購買價格 (整數)",
            default=str(cfg["scratch_cost"]),
            required=True,
            max_length=10,
        )
        self.scratch_jackpot = discord.ui.TextInput(
            label="頭獎 (💎×3) 倍率 (如 10.0)",
            default=str(cfg["scratch_jackpot_multiplier"]),
            required=True,
            max_length=10,
        )
        self.scratch_second = discord.ui.TextInput(
            label="貳獎 (🌟×3) 倍率 (如 3.0)",
            default=str(cfg["scratch_second_multiplier"]),
            required=True,
            max_length=10,
        )
        self.scratch_third = discord.ui.TextInput(
            label="參獎 (🍀×3) 倍率 (如 1.5)",
            default=str(cfg["scratch_third_multiplier"]),
            required=True,
            max_length=10,
        )
        for item in (self.scratch_cost, self.scratch_jackpot, self.scratch_second, self.scratch_third):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        errors: list[str] = []
        cost, e1 = _parse_int_field(self.scratch_cost.value, "刮刮樂單張價格", 1)
        j_m, e2 = _parse_float_field(self.scratch_jackpot.value, "刮刮樂頭獎倍率", 1.0, 10000.0)
        s_m, e3 = _parse_float_field(self.scratch_second.value, "刮刮樂貳獎倍率", 1.0, 10000.0)
        t_m, e4 = _parse_float_field(self.scratch_third.value, "刮刮樂參獎倍率", 0.5, 1000.0)
        for err in (e1, e2, e3, e4):
            if err:
                errors.append(err)

        if errors:
            await interaction.response.send_message("❌ 參數驗證失敗：\n" + "\n".join(errors), ephemeral=True)
            return

        await self.panel_view.apply_config_updates(
            interaction,
            {
                "scratch_cost": cost,
                "scratch_jackpot_multiplier": j_m,
                "scratch_second_multiplier": s_m,
                "scratch_third_multiplier": t_m,
            },
            "✅ 已更新「刮刮樂票價與各獎項倍率」設定！",
        )


class AdminCurrencyAndTransferModal(discord.ui.Modal, title="💸 修改貨幣名稱與轉帳設定"):
    def __init__(self, panel_view: "EconomyAdminView"):
        super().__init__()
        self.panel_view = panel_view
        cfg = panel_view.cog.config

        self.currency_name = discord.ui.TextInput(
            label="貨幣名稱 (例如：金幣、鑽石)",
            default=str(cfg["currency_name"]),
            required=True,
            max_length=20,
        )
        self.currency_emoji = discord.ui.TextInput(
            label="貨幣圖示 Emoji (例如：🪙)",
            default=str(cfg["currency_emoji"]),
            required=True,
            max_length=32,
        )
        self.transfer_enabled = discord.ui.TextInput(
            label="是否開放轉帳 (輸入 on 或 off)",
            default="on" if cfg["transfer_enabled"] else "off",
            required=True,
            max_length=10,
        )
        self.transfer_tax = discord.ui.TextInput(
            label="轉帳手續費百分比 % (0 ~ 90 的整數)",
            default=str(cfg["transfer_tax_percent"]),
            required=True,
            max_length=5,
        )
        self.transfer_min = discord.ui.TextInput(
            label="單筆最低轉帳金額 (整數)",
            default=str(cfg["transfer_min"]),
            required=True,
            max_length=10,
        )
        for item in (self.currency_name, self.currency_emoji, self.transfer_enabled, self.transfer_tax, self.transfer_min):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        errors: list[str] = []
        c_name = self.currency_name.value.strip() or "金幣"
        c_emoji = self.currency_emoji.value.strip() or "🪙"
        raw_enabled = self.transfer_enabled.value.strip().lower()
        if raw_enabled in ("on", "true", "1", "yes", "開啟", "開"):
            t_enabled = True
        elif raw_enabled in ("off", "false", "0", "no", "關閉", "關"):
            t_enabled = False
        else:
            t_enabled = True
            errors.append("• **是否開放轉帳** 請輸入 `on` 或 `off`")

        t_tax, e1 = _parse_int_field(self.transfer_tax.value, "轉帳手續費百分比", 0, 90)
        t_min, e2 = _parse_int_field(self.transfer_min.value, "單筆最低轉帳金額", 1)
        for err in (e1, e2):
            if err:
                errors.append(err)

        if errors:
            await interaction.response.send_message("❌ 參數驗證失敗：\n" + "\n".join(errors), ephemeral=True)
            return

        await self.panel_view.apply_config_updates(
            interaction,
            {
                "currency_name": c_name,
                "currency_emoji": c_emoji,
                "transfer_enabled": t_enabled,
                "transfer_tax_percent": t_tax,
                "transfer_min": t_min,
            },
            "✅ 已更新「貨幣名稱與轉帳設定」！",
        )


class AdminUserBalanceModal(discord.ui.Modal, title="💰 管理指定使用者金幣餘額"):
    def __init__(self, panel_view: "EconomyAdminView"):
        super().__init__()
        self.panel_view = panel_view

        self.target_user = discord.ui.TextInput(
            label="目標使用者 ID 或 @提及",
            placeholder="例如：123456789012345678 或 <@123456789012345678>",
            required=True,
            max_length=40,
        )
        self.action = discord.ui.TextInput(
            label="操作模式：add (發放) / sub (扣除) / set (直接設定)",
            default="add",
            required=True,
            max_length=10,
        )
        self.amount = discord.ui.TextInput(
            label="金幣數量 (正整數或 0)",
            placeholder="例如：1000",
            required=True,
            max_length=12,
        )
        for item in (self.target_user, self.action, self.amount):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        raw_user = self.target_user.value.strip()
        m = re.search(r"(\d{15,22})", raw_user)
        if not m:
            await interaction.response.send_message(
                "❌ 無法辨識使用者 ID，請輸入純數字 Discord User ID 或 `<@ID>`。",
                ephemeral=True,
            )
            return
        uid = int(m.group(1))

        act = self.action.value.strip().lower()
        if act not in ("add", "sub", "set"):
            await interaction.response.send_message(
                "❌ 操作模式請填寫 `add`（增加）、`sub`（扣除）或 `set`（直接設定餘額）。",
                ephemeral=True,
            )
            return

        amt, err = _parse_int_field(self.amount.value, "金幣數量", 0, 1_000_000_000)
        if err or amt is None:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return

        before = get_economy_user(uid)["balance"]
        if act == "add":
            _, after = update_economy_balance(uid, amt, add_earned=amt)
            act_label = f"發放 +{amt:,}"
        elif act == "sub":
            _, after = update_economy_balance(uid, -amt, allow_negative=False)
            act_label = f"扣除 -{amt:,}"
        else:
            after = set_economy_balance(uid, amt)
            act_label = f"設定為 {amt:,}"

        cur = self.panel_view.cog.currency_str()
        status = f"💰 已對 <@{uid}> (`{uid}`) 執行 **{act_label}** {cur}（變更前：`{before:,}` → 變更後：`{after:,}`）"
        embed = self.panel_view.build_panel_embed(status_note=status)
        await interaction.response.edit_message(embed=embed, view=self.panel_view)


class AdminAiCurrencyModal(discord.ui.Modal, title="⚡ 設定 AI 專屬貨幣與費率"):
    def __init__(self, panel_view: "EconomyAdminView"):
        super().__init__()
        self.panel_view = panel_view
        cfg = panel_view.cog.config

        self.ai_name = discord.ui.TextInput(
            label="AI 貨幣名稱",
            default=str(cfg.get("ai_currency_name", "AI 運算幣")),
            required=True,
            max_length=20,
        )
        self.ai_emoji = discord.ui.TextInput(
            label="AI 貨幣圖示 (Emoji)",
            default=str(cfg.get("ai_currency_emoji", "⚡")),
            required=True,
            max_length=10,
        )
        self.ai_daily_reward = discord.ui.TextInput(
            label="每日簽到發放數量 (整數)",
            default=str(cfg.get("ai_daily_reward", 1000)),
            required=True,
            max_length=10,
        )
        self.ai_daily_streak_bonus = discord.ui.TextInput(
            label="連簽每日加成,上限 (格式: 加成,上限)",
            default=f"{cfg.get('ai_daily_streak_bonus', 100)},{cfg.get('ai_daily_streak_max', 1000)}",
            required=True,
            max_length=20,
        )
        self.ai_cost_per_1k = discord.ui.TextInput(
            label="每 1000 Tokens 計費點數 (整數)",
            default=str(cfg.get("ai_cost_per_1k_tokens", 100)),
            required=True,
            max_length=10,
        )
        for item in (self.ai_name, self.ai_emoji, self.ai_daily_reward, self.ai_daily_streak_bonus, self.ai_cost_per_1k):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        errors: list[str] = []
        name = self.ai_name.value.strip() or "AI 運算幣"
        emoji = self.ai_emoji.value.strip() or "⚡"
        daily_reward, e1 = _parse_int_field(self.ai_daily_reward.value, "每日簽到發放數量", 0, 1_000_000)
        cost_per_1k, e2 = _parse_int_field(self.ai_cost_per_1k.value, "每 1000 Tokens 計費點數", 1, 100_000)

        streak_parts = [p.strip() for p in self.ai_daily_streak_bonus.value.split(",") if p.strip()]
        streak_bon, streak_max = 100, 1000
        if len(streak_parts) == 2:
            s_b, err_b = _parse_int_field(streak_parts[0], "連簽每日加成", 0, 100_000)
            s_m, err_m = _parse_int_field(streak_parts[1], "連簽上限", 0, 1_000_000)
            if err_b:
                errors.append(err_b)
            else:
                streak_bon = s_b
            if err_m:
                errors.append(err_m)
            else:
                streak_max = s_m
        else:
            errors.append("• **連簽每日加成與上限** 請以逗號分隔，例如 `100,1000`")

        for err in (e1, e2):
            if err:
                errors.append(err)

        if errors:
            await interaction.response.send_message("❌ 參數驗證失敗：\n" + "\n".join(errors), ephemeral=True)
            return

        await self.panel_view.apply_config_updates(
            interaction,
            {
                "ai_currency_name": name,
                "ai_currency_emoji": emoji,
                "ai_daily_reward": daily_reward,
                "ai_daily_streak_bonus": streak_bon,
                "ai_daily_streak_max": streak_max,
                "ai_cost_per_1k_tokens": cost_per_1k,
            },
            "✅ 已更新「AI 專屬貨幣與費率」設定！",
        )


class AdminUserAiPointsModal(discord.ui.Modal, title="🤖 管理指定使用者 AI 貨幣餘額"):
    def __init__(self, panel_view: "EconomyAdminView"):
        super().__init__()
        self.panel_view = panel_view

        self.target_user = discord.ui.TextInput(
            label="目標使用者 ID 或 @提及",
            placeholder="例如：123456789012345678 或 <@123456789012345678>",
            required=True,
            max_length=40,
        )
        self.action = discord.ui.TextInput(
            label="操作模式：add (發放) / sub (扣除) / set (直接設定)",
            default="add",
            required=True,
            max_length=10,
        )
        self.amount = discord.ui.TextInput(
            label="AI 幣數量 (正整數或 0)",
            placeholder="例如：1000",
            required=True,
            max_length=12,
        )
        for item in (self.target_user, self.action, self.amount):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        raw_user = self.target_user.value.strip()
        m = re.search(r"(\d{15,22})", raw_user)
        if not m:
            await interaction.response.send_message(
                "❌ 無法辨識使用者 ID，請輸入純數字 Discord User ID 或 `<@ID>`。",
                ephemeral=True,
            )
            return
        uid = int(m.group(1))

        act = self.action.value.strip().lower()
        if act not in ("add", "sub", "set"):
            await interaction.response.send_message(
                "❌ 操作模式請填寫 `add`（增加）、`sub`（扣除）或 `set`（直接設定餘額）。",
                ephemeral=True,
            )
            return

        amt, err = _parse_int_field(self.amount.value, "AI 幣數量", 0, 1_000_000_000)
        if err or amt is None:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return

        before = get_user_ai_points(uid)
        if act == "add":
            _, after = update_user_ai_points(uid, amt)
            act_label = f"發放 +{amt:,}"
        elif act == "sub":
            _, after = update_user_ai_points(uid, -amt, allow_negative=False)
            act_label = f"扣除 -{amt:,}"
        else:
            after = set_user_ai_points(uid, amt)
            act_label = f"設定為 {amt:,}"

        ai_emoji = self.panel_view.cog.config.get("ai_currency_emoji", "⚡")
        ai_name = self.panel_view.cog.config.get("ai_currency_name", "AI 運算幣")
        status = f"🤖 已對 <@{uid}> (`{uid}`) 執行 **{act_label}** {ai_emoji} {ai_name}（變更前：`{before:,}` → 變更後：`{after:,}`）"
        embed = self.panel_view.build_panel_embed(status_note=status)
        await interaction.response.edit_message(embed=embed, view=self.panel_view)


class EconomyAdminSelect(discord.ui.Select):
    """後台管理面板的下拉選單。"""

    def __init__(self, panel_view: "EconomyAdminView"):
        self.panel_view = panel_view
        options = [
            discord.SelectOption(
                label="修改「每日簽到」參數",
                value="daily",
                emoji="📅",
                description="調整每日簽到獎勵範圍與連續簽到加成（固定每日 00:00 UTC+8 刷新）",
            ),
            discord.SelectOption(
                label="修改「每小時簽到」參數",
                value="hourly",
                emoji="⏰",
                description="調整每小時簽到獎勵範圍（固定每小時整點 XX:00 刷新）",
            ),
            discord.SelectOption(
                label="修改「AI 貨幣 & Token 計費」設定",
                value="ai_currency",
                emoji="⚡",
                description="調整 AI 幣名稱、每日簽到發放數量、連簽加成與 Token 計費費率",
            ),
            discord.SelectOption(
                label="修改「賭注上下限 & 擲硬幣/骰子」參數",
                value="gamble",
                emoji="🎲",
                description="調整小遊戲單局最低/最高賭注、硬幣與骰子賠率",
            ),
            discord.SelectOption(
                label="修改「21 點 & 拉霸機」賠率參數",
                value="bj_slots",
                emoji="🎰",
                description="調整 21 點一般/Blackjack 賠率、拉霸機各連線倍率",
            ),
            discord.SelectOption(
                label="修改「刮刮樂」票價與獎項倍率",
                value="scratch",
                emoji="🎫",
                description="調整刮刮樂單張購買價格與頭獎/貳獎/參獎倍率",
            ),
            discord.SelectOption(
                label="修改「貨幣名稱 & 轉帳手續費」設定",
                value="currency_transfer",
                emoji="💸",
                description="調整貨幣名稱、圖示、轉帳開關、手續費%與最低轉帳額",
            ),
            discord.SelectOption(
                label="發放 / 扣除 / 設定使用者金幣",
                value="user_balance",
                emoji="💰",
                description="直接對指定使用者 ID 增加、扣除或設定金幣餘額",
            ),
            discord.SelectOption(
                label="發放 / 扣除 / 設定使用者 AI 幣",
                value="user_ai_points",
                emoji="🤖",
                description="直接對指定使用者 ID 增加、扣除或設定 AI 專屬貨幣點數",
            ),
        ]
        super().__init__(
            placeholder="🛠️ 選擇要修改的經濟系統參數類別或管理功能…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice == "daily":
            await interaction.response.send_modal(AdminDailyModal(self.panel_view))
        elif choice == "hourly":
            await interaction.response.send_modal(AdminHourlyModal(self.panel_view))
        elif choice == "ai_currency":
            await interaction.response.send_modal(AdminAiCurrencyModal(self.panel_view))
        elif choice == "gamble":
            await interaction.response.send_modal(AdminBetAndGambleModal(self.panel_view))
        elif choice == "bj_slots":
            await interaction.response.send_modal(AdminBlackjackAndSlotsModal(self.panel_view))
        elif choice == "scratch":
            await interaction.response.send_modal(AdminScratchModal(self.panel_view))
        elif choice == "currency_transfer":
            await interaction.response.send_modal(AdminCurrencyAndTransferModal(self.panel_view))
        elif choice == "user_balance":
            await interaction.response.send_modal(AdminUserBalanceModal(self.panel_view))
        elif choice == "user_ai_points":
            await interaction.response.send_modal(AdminUserAiPointsModal(self.panel_view))


class EconomyAdminView(BaseEconomyView):
    """經濟系統後台參數控制面板 View（僅限擁有者操作）。"""

    def __init__(self, cog: "Economy", user_id: int):
        super().__init__(cog, user_id, timeout=180.0)
        self.add_item(EconomyAdminSelect(self))
        self.add_item(build_panel_link_button(owner=True, row=1))

    def build_panel_embed(self, status_note: Optional[str] = None) -> discord.Embed:
        cfg = self.cog.config
        cur = self.cog.currency_str()
        transfer_state = "🟢 開放中" if cfg["transfer_enabled"] else "🔴 已關閉"
        ai_name = cfg.get("ai_currency_name", "AI 運算幣")
        ai_emoji = cfg.get("ai_currency_emoji", "⚡")

        embed = discord.Embed(
            title="🛠️ 經濟系統後台參數控制面板 (Owner Only)",
            description=(
                "請從下方選單選擇要修改的功能類別，所有參數變更都會**立即生效並寫入 SQLite 資料庫**。"
            ),
            color=discord.Color.dark_gold(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="🪙 貨幣與轉帳設定",
            value=(
                f"• **貨幣顯示**：{cur}\n"
                f"• **轉帳狀態**：{transfer_state}\n"
                f"• **轉帳手續費**：`{cfg['transfer_tax_percent']}%`\n"
                f"• **最低轉帳額**：`{cfg['transfer_min']:,}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="📅 每日 & ⏰ 每小時簽到",
            value=(
                f"• **每日基礎獎勵**：`{cfg['daily_min']:,}` ~ `{cfg['daily_max']:,}`\n"
                f"• **連簽加成**：`+{cfg['daily_streak_bonus']:,}`/日（上限 `{cfg['daily_streak_max']:,}`）\n"
                f"• **每日刷新**：固定每日 `00:00 (UTC+8)`\n"
                f"• **每小時獎勵**：`{cfg['hourly_min']:,}` ~ `{cfg['hourly_max']:,}`（整點 `XX:00` 刷新）"
            ),
            inline=True,
        )
        embed.add_field(
            name=f"{ai_emoji} AI 專屬貨幣設定",
            value=(
                f"• **貨幣名稱**：{ai_emoji} {ai_name}\n"
                f"• **每日簽到**：`+{cfg.get('ai_daily_reward', 1000):,}` 點\n"
                f"• **連簽加成**：`+{cfg.get('ai_daily_streak_bonus', 100):,}`/日（上限 `{cfg.get('ai_daily_streak_max', 1000):,}`）\n"
                f"• **Token 費率**：每 1k Tokens `{cfg.get('ai_cost_per_1k_tokens', 100):,}` 點"
            ),
            inline=True,
        )
        embed.add_field(
            name="🎲 小遊戲賭注與賠率（硬幣 / 骰子 / 21點）",
            value=(
                f"• **單局賭注範圍**：`{cfg['bet_min']:,}` ~ `{cfg['bet_max']:,}`\n"
                f"• **擲硬幣賠率**：`{cfg['coinflip_multiplier']:g}x` ｜ **擲骰子賠率**：`{cfg['dice_multiplier']:g}x`\n"
                f"• **21 點獲勝賠率**：一般 `{cfg['blackjack_multiplier']:g}x` ／ Blackjack `{cfg['blackjack_natural_multiplier']:g}x`"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎰 拉霸機 & 🎫 刮刮樂設定",
            value=(
                f"• **拉霸機倍率**：頭獎 `{cfg['slots_jackpot_multiplier']:g}x` ／ 三連線 `{cfg['slots_triple_multiplier']:g}x` ／ 雙連線 `{cfg['slots_pair_multiplier']:g}x`\n"
                f"• **刮刮樂票價**：`{cfg['scratch_cost']:,}` {cur}\n"
                f"• **刮刮樂倍率**：頭獎 `{cfg['scratch_jackpot_multiplier']:g}x` ／ 貳獎 `{cfg['scratch_second_multiplier']:g}x` ／ 參獎 `{cfg['scratch_third_multiplier']:g}x`"
            ),
            inline=False,
        )
        if status_note:
            embed.add_field(name="📌 最新操作紀錄", value=status_note, inline=False)
        embed.set_footer(text="提示：選取下拉選單項目會開啟預填好目前數值的彈出視窗，修改後送出即可。")
        return embed

    async def apply_config_updates(self, interaction: discord.Interaction, updates: dict[str, Any], note: str):
        old_snapshot = dict(self.cog.config)
        self.cog.config.update(updates)
        try:
            save_economy_config(self.cog.config)
        except Exception as exc:
            self.cog.config = old_snapshot
            traceback.print_exc()
            await self.cog.bot.notify_owner_error(exc, interaction, extra_info="EconomyAdminView.apply_config_updates")
            await interaction.response.send_message("❌ 儲存設定至資料庫時發生錯誤，變更已回滾。", ephemeral=True)
            return

        embed = self.build_panel_embed(status_note=note)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🔄 重置所有參數為預設值", style=discord.ButtonStyle.danger, row=1)
    async def reset_defaults_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply_config_updates(
            interaction,
            dict(DEFAULT_ECONOMY_CONFIG),
            "🔄 已將所有經濟系統參數恢復為出廠預設值！",
        )


# ==============================================================================
# Economy Cog 主體
# ==============================================================================

class Economy(commands.Cog):
    """全域經濟系統：包含每日簽到、每小時簽到、餘額查詢、排行榜、轉帳、4 種花錢遊玩的小遊戲與後台參數控制面板。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[str, Any] = load_economy_config()
        # 防止同一名玩家同時開啟多局 21 點或動畫進行中重複觸發
        self._active_blackjack_users: set[int] = set()
        self._animating_users: set[int] = set()
        # 記錄玩家點擊小遊戲重玩／押注按鈕的最後時間戳 (monotonic)，防止連點洗版
        self._button_cooldowns: dict[int, float] = {}

    def check_button_cooldown(self, user_id: int) -> tuple[bool, float]:
        """檢查使用者點擊小遊戲互動按鈕是否在冷卻中，回傳 (是否允許, 剩餘秒數)。"""
        now = time.monotonic()
        last = self._button_cooldowns.get(user_id)
        if last is not None and (now - last) < ECONOMY_BUTTON_COOLDOWN_SECONDS:
            return False, ECONOMY_BUTTON_COOLDOWN_SECONDS - (now - last)
        self._button_cooldowns[user_id] = now
        return True, 0.0

    def currency_str(self) -> str:
        """回傳形如 `🪙 金幣` 的貨幣顯示字串。"""
        return f"{self.config.get('currency_emoji', '🪙')} {self.config.get('currency_name', '金幣')}"

    def _validate_bet(self, bet: int) -> Optional[str]:
        """檢查下注金額是否符合後台設定的上下限，不合法時回傳錯誤訊息。"""
        b_min = int(self.config.get("bet_min", 10))
        b_max = int(self.config.get("bet_max", 50000))
        cur = self.currency_str()
        if bet < b_min:
            return f"❌ 下注金額不能低於最低限制 **{b_min:,}** {cur}！"
        if bet > b_max:
            return f"❌ 下注金額不能超過單局上限 **{b_max:,}** {cur}！"
        return None

    # --------------------------------------------------------------------------
    # 1. 每日簽到 (/economy_daily) 與 每小時簽到 (/economy_hourly)
    # --------------------------------------------------------------------------

    @app_commands.command(name="economy_daily", description="領取每日簽到金幣獎勵（每日 00:00 UTC+8 刷新，連續簽到享額外加成！）")
    @app_commands.checks.cooldown(1, ECONOMY_CMD_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def economy_daily(self, interaction: discord.Interaction):
        now_ts = time.time()
        cfg = self.config

        d_min = int(cfg.get("daily_min", 100))
        d_max = max(d_min, int(cfg.get("daily_max", 300)))
        base_reward = random.randint(d_min, d_max)

        s_bon = max(0, int(cfg.get("daily_streak_bonus", 20)))
        s_max = max(0, int(cfg.get("daily_streak_max", 200)))
        if s_bon > 0 and s_max < s_bon:
            s_max = s_bon
        init_bal = max(0, int(cfg.get("initial_balance", 200)))
        ai_daily = cfg.get("ai_daily_reward", 1000)
        ai_s_bon = cfg.get("ai_daily_streak_bonus", 100)
        ai_s_max = cfg.get("ai_daily_streak_max", 1000)
        ai_init_bal = cfg.get("ai_initial_balance", 1000)

        res = claim_economy_daily(
            user_id=interaction.user.id,
            now_ts=now_ts,
            base_reward=base_reward,
            streak_bonus_per_day=s_bon,
            streak_bonus_max=s_max,
            initial_balance=init_bal,
            ai_daily_reward=ai_daily,
            ai_streak_bonus_per_day=ai_s_bon,
            ai_streak_bonus_max=ai_s_max,
            ai_initial_balance=ai_init_bal,
        )
        cur = self.currency_str()
        ai_name = cfg.get("ai_currency_name", "AI 運算幣")
        ai_emoji = cfg.get("ai_currency_emoji", "⚡")
        next_dt_str = f"<t:{int(res['next_available_ts'])}:R>（明日 `00:00 UTC+8`）"

        if not res["ok"]:
            ai_pts = res.get("ai_points", 0)
            embed = discord.Embed(
                title="⏳ 今日已完成每日簽到",
                description=(
                    f"你今天已經領取過每日簽到獎勵囉！\n"
                    f"• **下次刷新時間**：{next_dt_str}\n"
                    f"• **目前連簽天數**：🔥 **{res['streak']}** 天\n"
                    f"• **目前金幣餘額**：**{res['balance']:,}** {cur}\n"
                    f"• **{ai_name}餘額**：**{ai_pts:,}** {ai_emoji}"
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        tot_ai = res.get("total_ai_reward", 0)
        new_ai = res.get("new_ai_points", 0)

        desc_lines: list[str] = []
        if res.get("initial_bonus", 0) > 0:
            desc_lines.append(f"🎉 **首次簽到初始金幣**：`+{res['initial_bonus']:,}` {cur}")
        desc_lines.append(f"🎁 **金幣基礎獎勵**：`+{res['base_reward']:,}` {cur}")
        if res["streak_bonus"] > 0:
            desc_lines.append(f"🔥 **金幣連續簽到加成（第 {res['streak']} 天）**：`+{res['streak_bonus']:,}` {cur}")
        elif res.get("streak_broken"):
            desc_lines.append(f"⚠️ 昨日未簽到，連簽紀錄已重置為 **第 1 天**（原先 {res['old_streak']} 天）。")
        else:
            desc_lines.append("🔥 **連續簽到**：第 **1** 天（明天繼續簽到可領額外加成！）")

        if tot_ai > 0:
            desc_lines.append("")
            ai_details = f"基礎 +{res.get('ai_reward', 0):,}"
            if res.get('ai_streak_bonus', 0) > 0:
                ai_details += f"，連簽加成 +{res.get('ai_streak_bonus', 0):,}"
            if res.get('ai_initial_bonus', 0) > 0:
                ai_details += f"，首簽贈送 +{res.get('ai_initial_bonus', 0):,}"
            desc_lines.append(f"⚡ **獲得 {ai_name}**：`+{tot_ai:,}` {ai_emoji} ({ai_details})")

        desc_lines.extend([
            "",
            f"💰 **本次獲得金幣**：**+{res['total_reward']:,}** {cur}",
            f"🏦 **目前金幣總餘額**：**{res['new_balance']:,}** {cur}",
            f"⚡ **{ai_name}總餘額**：**{new_ai:,}** {ai_emoji}",
            f"🕒 **下次刷新**：{next_dt_str}",
        ])

        embed = discord.Embed(
            title="📅 每日簽到成功！",
            description="\n".join(desc_lines),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="economy_hourly", description="領取每小時簽到零用錢（每小時整點 XX:00 刷新）")
    @app_commands.checks.cooldown(1, ECONOMY_CMD_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def economy_hourly(self, interaction: discord.Interaction):
        now_ts = time.time()
        cfg = self.config
        h_min = int(cfg.get("hourly_min", 20))
        h_max = max(h_min, int(cfg.get("hourly_max", 60)))
        reward = random.randint(h_min, h_max)
        init_bal = max(0, int(cfg.get("initial_balance", 200)))

        res = claim_economy_hourly(
            user_id=interaction.user.id,
            now_ts=now_ts,
            reward=reward,
            initial_balance=init_bal,
        )
        cur = self.currency_str()
        next_dt_str = f"<t:{int(res['next_available_ts'])}:R>（下個整點）"

        if not res["ok"]:
            embed = discord.Embed(
                title="⏳ 本小時已領取過零用錢",
                description=(
                    f"你在這個小時內已經領過每小時零用錢囉！\n"
                    f"• **下次刷新時間**：{next_dt_str}\n"
                    f"• **目前餘額**：**{res['balance']:,}** {cur}"
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        desc_lines: list[str] = []
        if res.get("initial_bonus", 0) > 0:
            desc_lines.append(f"🎉 **首次簽到初始資金**：`+{res['initial_bonus']:,}` {cur}")
            desc_lines.append(f"🎁 **獲得零用錢**：`+{res['reward']:,}` {cur}")
            desc_lines.append(f"💰 **本次共獲得**：**+{res.get('total_reward', res['reward'] + res['initial_bonus']):,}** {cur}")
        else:
            desc_lines.append(f"🎁 **獲得零用錢**：**+{res['reward']:,}** {cur}")
        desc_lines.extend([
            f"🏦 **目前總餘額**：**{res['new_balance']:,}** {cur}",
            f"🕒 **下次刷新**：{next_dt_str}",
        ])

        embed = discord.Embed(
            title="⏰ 每小時簽到成功！",
            description="\n".join(desc_lines),
            color=discord.Color.teal(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    # --------------------------------------------------------------------------
    # 2. 餘額查詢 (/economy_balance)、富豪排行榜 (/economy_leaderboard) 與 轉帳 (/economy_pay)
    # --------------------------------------------------------------------------

    @app_commands.command(name="economy_balance", description="查詢自己或指定成員的金幣與 AI 貨幣餘額與全域富豪排名")
    @app_commands.checks.cooldown(1, ECONOMY_CMD_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    @app_commands.describe(member="要查詢的對象（選填，不填則查詢自己）")
    async def economy_balance(self, interaction: discord.Interaction, member: Optional[discord.User] = None):
        target = member or interaction.user
        info = get_economy_user(target.id)
        owner_id = get_bot_owner_id(self.bot)

        if owner_id is not None and target.id == owner_id:
            rank_str = "機器人擁有者不參與排行"
        else:
            _, rank_info = get_economy_leaderboard(limit=1, user_id=target.id, exclude_user_id=owner_id)
            rank_str = f"第 **{rank_info['rank']}** 名" if rank_info and rank_info.get("rank") else "未上榜"

        cur = self.currency_str()
        ai_name = self.config.get("ai_currency_name", "AI 運算幣")
        ai_emoji = self.config.get("ai_currency_emoji", "⚡")
        ai_pts = info.get("ai_points", 0)

        embed = discord.Embed(
            title=f"🏦 {target.display_name} 的個人帳戶",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="💰 金幣餘額", value=f"**{info['balance']:,}** {cur}", inline=True)
        embed.add_field(name=f"{ai_emoji} {ai_name}", value=f"**{ai_pts:,}** 點", inline=True)
        embed.add_field(name="🏆 全域富豪排名", value=rank_str, inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="economy_leaderboard", description="查看全域金幣富豪排行榜前 10 名")
    @app_commands.checks.cooldown(1, ECONOMY_LEADERBOARD_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def economy_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        owner_id = get_bot_owner_id(self.bot)
        top_list, caller_info = get_economy_leaderboard(
            limit=10,
            user_id=interaction.user.id,
            exclude_user_id=owner_id,
        )
        cur = self.currency_str()

        if not top_list:
            embed = discord.Embed(
                title="🏆 全域富豪排行榜",
                description="目前還沒有任何玩家擁有金幣，快使用 `/economy_daily` 或 `/economy_hourly` 成為第一位富豪吧！",
                color=discord.Color.gold(),
            )
            await interaction.followup.send(embed=embed)
            return

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines: list[str] = []
        for entry in top_list:
            rank = entry["rank"]
            uid = entry["user_id"]
            bal = entry["balance"]
            streak = entry["daily_streak"]
            badge = medals.get(rank, f"`#{rank:02d}`")
            user_obj = self.bot.get_user(uid)
            name_display = f"**{user_obj.display_name}**" if user_obj else f"<@{uid}>"
            lines.append(f"{badge} {name_display} — **{bal:,}** {cur}（🔥 連簽 {streak} 天）")

        embed = discord.Embed(
            title="🏆 全域富豪排行榜 Top 10",
            description="\n".join(lines),
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        if caller_info:
            if owner_id is not None and interaction.user.id == owner_id:
                c_rank = "機器人擁有者不參與排行"
            else:
                c_rank = f"第 {caller_info['rank']} 名" if caller_info.get("rank") else "未上榜"
            embed.set_footer(
                text=f"你的排名：{c_rank} ｜ 你的餘額：{caller_info['balance']:,} {self.config['currency_name']}"
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="economy_pay", description="將自己的金幣轉帳給其他成員")
    @app_commands.checks.cooldown(1, ECONOMY_CMD_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    @app_commands.describe(
        recipient="要接收金幣的成員",
        amount="要轉出的金幣數量（正整數）",
    )
    async def economy_pay(self, interaction: discord.Interaction, recipient: discord.User, amount: int):
        cur = self.currency_str()
        if not self.config.get("transfer_enabled", True):
            await interaction.response.send_message("❌ 目前管理員已關閉轉帳功能。", ephemeral=True)
            return
        if recipient.id == interaction.user.id:
            await interaction.response.send_message("❌ 不能轉帳給自己喔！", ephemeral=True)
            return
        if recipient.bot:
            await interaction.response.send_message("❌ 不能轉帳給機器人！", ephemeral=True)
            return

        min_amt = int(self.config.get("transfer_min", 10))
        if amount < min_amt:
            await interaction.response.send_message(
                f"❌ 單筆最低轉帳金額為 **{min_amt:,}** {cur}！",
                ephemeral=True,
            )
            return

        tax_pct = int(self.config.get("transfer_tax_percent", 5))
        res = transfer_economy_balance(
            sender_id=interaction.user.id,
            receiver_id=recipient.id,
            amount=amount,
            tax_percent=tax_pct,
        )
        if not res["ok"]:
            await interaction.response.send_message(
                f"❌ 你的餘額不足！欲轉出 **{amount:,}** {cur}，但目前只有 **{res['sender_balance']:,}** {cur}。",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="💸 轉帳成功！",
            description=(
                f"**匯款人**：{interaction.user.mention}\n"
                f"**收款人**：{recipient.mention}\n\n"
                f"• **轉出總額**：`{res['amount']:,}` {cur}\n"
                f"• **系統手續費 ({tax_pct}%)**：`-{res['tax']:,}` {cur}\n"
                f"• **對方實際收到**：**+{res['net_amount']:,}** {cur}\n\n"
                f"🏦 **你的剩餘餘額**：**{res['sender_balance']:,}** {cur}"
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        await interaction.response.send_message(embed=embed)

    # --------------------------------------------------------------------------
    # 3. 花錢遊玩的小遊戲 1：🎰 拉霸機 (/economy_slots)
    # --------------------------------------------------------------------------

    async def _execute_slots(
        self,
        interaction: discord.Interaction,
        bet: int,
        *,
        panel_view: SlotsPanelView,
        is_all_in: bool = False,
    ):
        uid = interaction.user.id
        if uid in self._animating_users:
            await interaction.response.send_message("⏳ 你的上一局開獎動畫仍在進行中，請稍候！", ephemeral=True)
            return

        allowed, rem = self.check_button_cooldown(uid)
        if not allowed:
            await interaction.response.send_message(
                f"⏳ 按鈕冷卻中，請稍後再試（還需 **{rem:.1f}** 秒）！",
                ephemeral=True,
            )
            return

        err = self._validate_bet(bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        cur = self.currency_str()
        ok, bal_after_bet = update_economy_balance(uid, -bet, add_wagered=bet)
        if not ok:
            await interaction.response.send_message(
                f"❌ 餘額不足！拉霸需要 **{bet:,}** {cur}，你目前只有 **{bal_after_bet:,}** {cur}。",
                ephemeral=True,
            )
            return

        # 立即在資料庫完成派彩結算（確保即使動畫中途網路波動也不會吃錢），再透過編輯訊息呈現逐軸停下的動畫
        reels = spin_slot_reels()
        mult, outcome_text = evaluate_slots(reels, self.config)
        payout = int(round(bet * mult))
        if payout > 0:
            net_earned = max(0, payout - bet)
            _, final_bal = update_economy_balance(uid, payout, add_earned=net_earned)
            net_diff = payout - bet
            profit_line = f"💰 **派彩獎金**：**+{payout:,}** {cur}（淨利 **+{net_diff:,}**）"
            final_color = discord.Color.gold() if mult >= 5.0 else discord.Color.green()
        else:
            final_bal = bal_after_bet
            profit_line = f"💸 **本局損益**：**-{bet:,}** {cur}"
            final_color = discord.Color.red()

        bet_tag = f"**{bet:,}** {cur}" + (" 🔥 *(All In 全押)*" if is_all_in else "")

        def _build_spin_embed(
            r0: str,
            r1: str,
            r2: str,
            status_line: str,
            color: discord.Color,
            settled: bool = False,
            current_bet: int = bet,
        ) -> discord.Embed:
            desc = (
                f"**玩家**：{interaction.user.mention} ｜ **單局投入**：{bet_tag}\n\n"
                f"> 🎰 **{status_line}** 🎰\n"
                f"> # 【 {r0} ｜ {r1} ｜ {r2} 】\n\n"
            )
            if settled:
                desc += f"{outcome_text}\n{profit_line}"
            else:
                desc += "⏳ *轉輪高速運轉中，請屏息以待...*"
            emb = discord.Embed(title="🎰 皇家拉霸機 (Slots)", description=desc, color=color)
            if settled:
                emb.set_footer(
                    text=f"目前餘額：{final_bal:,} {self.config['currency_name']} ｜ 當前設定賭注：{current_bet:,}"
                )
            else:
                emb.set_footer(text="🎰 開獎動畫進行中...")
            return emb

        self._animating_users.add(uid)
        try:
            # 先停止舊 View 的閒置計時器（避免動畫進行中觸發舊 View 的 on_timeout 把按鈕鎖死）
            msg = panel_view.message or interaction.message
            panel_view.message = None
            panel_view.stop()
            panel_view.disable_all_items()

            frame1 = _build_spin_embed("🔄", "🔄", "🔄", "拉霸機高速旋轉中...", discord.Color.blurple())
            await interaction.response.edit_message(embed=frame1, view=panel_view)

            await asyncio.sleep(0.8)
            frame2 = _build_spin_embed(reels[0], "🔄", "🔄", "第一軸停下！", discord.Color.blurple())
            try:
                await interaction.edit_original_response(embed=frame2)
            except discord.HTTPException:
                if msg is not None:
                    try:
                        await msg.edit(embed=frame2)
                    except discord.HTTPException:
                        pass

            await asyncio.sleep(0.8)
            frame3 = _build_spin_embed(reels[0], reels[1], "🔄", "第二軸停下！", discord.Color.blurple())
            try:
                await interaction.edit_original_response(embed=frame3)
            except discord.HTTPException:
                if msg is not None:
                    try:
                        await msg.edit(embed=frame3)
                    except discord.HTTPException:
                        pass

            await asyncio.sleep(0.8)

            # 動畫結束：建立全新的 SlotsPanelView 實例，讓 120 秒閒置計時器從開獎結束這一刻重新計算
            new_view = SlotsPanelView(
                self,
                interaction.user,
                panel_view.bet,
                is_all_in=is_all_in,
                played_once=True,
            )
            new_view.message = msg
            final_embed = _build_spin_embed(
                reels[0],
                reels[1],
                reels[2],
                "最終開獎結果",
                final_color,
                settled=True,
                current_bet=new_view.bet,
            )

            # 確保最終開獎與解鎖按鈕的更新必定送達（若遇瞬時限流則自動重試）
            for attempt in range(3):
                try:
                    await interaction.edit_original_response(embed=final_embed, view=new_view)
                    break
                except discord.HTTPException:
                    if msg is not None:
                        try:
                            await msg.edit(embed=final_embed, view=new_view)
                            break
                        except discord.HTTPException:
                            pass
                    if attempt < 2:
                        await asyncio.sleep(1.0)
        finally:
            self._animating_users.discard(uid)

    @app_commands.command(name="economy_slots", description="🎰 開啟皇家拉霸機互動面板（可自由調整賭注、All In 全押並觀看轉輪動畫）")
    @app_commands.checks.cooldown(1, ECONOMY_GAME_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def economy_slots(self, interaction: discord.Interaction):
        view = SlotsPanelView(self, interaction.user)
        embed = view.build_lobby_embed()
        await interaction.response.send_message(embed=embed, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    # --------------------------------------------------------------------------
    # 4. 花錢遊玩的小遊戲 2：🎲 擲硬幣與骰子比大小 (/economy_gamble)
    # --------------------------------------------------------------------------

    async def _execute_gamble(
        self,
        interaction: discord.Interaction,
        bet: int,
        mode: str,
        user_choice: str,
        *,
        panel_view: GamblePanelView,
        is_all_in: bool = False,
    ):
        uid = interaction.user.id
        if uid in self._animating_users:
            await interaction.response.send_message("⏳ 你的上一局開獎動畫仍在進行中，請稍候！", ephemeral=True)
            return

        allowed, rem = self.check_button_cooldown(uid)
        if not allowed:
            await interaction.response.send_message(
                f"⏳ 按鈕冷卻中，請稍後再試（還需 **{rem:.1f}** 秒）！",
                ephemeral=True,
            )
            return

        err = self._validate_bet(bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        cur = self.currency_str()
        ok, bal_after_bet = update_economy_balance(uid, -bet, add_wagered=bet)
        if not ok:
            await interaction.response.send_message(
                f"❌ 餘額不足！下注需要 **{bet:,}** {cur}，你目前只有 **{bal_after_bet:,}** {cur}。",
                ephemeral=True,
            )
            return

        # 預先計算開獎結果並寫入資料庫，再透過編輯訊息呈現拋硬幣／擲骰子動畫
        if mode == "coinflip":
            outcome = random.choice(["正面", "反面"])
            won = user_choice == outcome
            mult = float(self.config.get("coinflip_multiplier", 2.0))
            game_title = "🪙 幸運擲硬幣"
            anim_frames = [
                ("硬幣拋向空中高速旋轉中...", "💫 正面 ⇄ 反面 💫"),
                ("硬幣叮噹落地中...", "✨ 🪙 ✨"),
            ]
            final_center = f"🪙 {outcome}"
            choice_summary = f"🎯 **你的押注**：`{user_choice}` ｜ 🪙 **開出結果**：**`{outcome}`**"
        else:
            dice_num = random.randint(1, 6)
            dice_emojis = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}
            hl_res = "大" if dice_num >= 4 else "小"
            oe_res = "雙" if dice_num % 2 == 0 else "單"
            won = user_choice in (hl_res, oe_res)
            mult = float(self.config.get("dice_multiplier", 2.0))
            game_title = "🎲 皇家擲骰子"
            anim_frames = [
                ("骰子在骰盅裡劇烈滾動中...", "🎲 ⚃ ⚁ ⚄"),
                ("打開骰盅，骰子即將停下...", "🎲 ⚀ ⚅ ⚂"),
            ]
            final_center = f"{dice_emojis[dice_num]} {dice_num} 點（{hl_res}／{oe_res}）"
            choice_summary = (
                f"🎯 **你的押注**：`{user_choice}` ｜ 🎲 **骰出點數**：{dice_emojis[dice_num]} **`{dice_num} 點`（{hl_res}／{oe_res}）**"
            )

        if won:
            payout = int(round(bet * mult))
            net_earned = max(0, payout - bet)
            _, final_bal = update_economy_balance(uid, payout, add_earned=net_earned)
            result_line = f"🎉 **猜中了！獲得 {mult:g} 倍獎金 +{payout:,} {cur}（淨賺 +{payout - bet:,}）！**"
            final_color = discord.Color.green()
        else:
            final_bal = bal_after_bet
            result_line = f"💨 **可惜沒猜中！損失了 {bet:,} {cur}。**"
            final_color = discord.Color.red()

        bet_tag = f"**{bet:,}** {cur}" + (" 🔥 *(All In 全押)*" if is_all_in else "")

        def _build_gamble_embed(
            status_line: str,
            center_box: str,
            color: discord.Color,
            settled: bool = False,
            current_bet: int = bet,
        ) -> discord.Embed:
            desc = (
                f"**玩家**：{interaction.user.mention} ｜ **本局下注**：{bet_tag}\n\n"
                f"> {status_line}\n"
                f"> # 【 {center_box} 】\n\n"
            )
            if settled:
                desc += f"{choice_summary}\n{result_line}"
            else:
                desc += f"🎯 **已押注**：`{user_choice}` — *等待開獎結果...*"
            emb = discord.Embed(title=game_title, description=desc, color=color)
            if settled:
                emb.set_footer(
                    text=f"目前餘額：{final_bal:,} {self.config['currency_name']} ｜ 當前設定賭注：{current_bet:,}（可直接點擊按鈕再賭一局）"
                )
            else:
                emb.set_footer(text="🎲 開獎動畫進行中...")
            return emb

        self._animating_users.add(uid)
        try:
            msg = panel_view.message or interaction.message
            panel_view.message = None
            panel_view.stop()
            panel_view.disable_all_items()

            f1_status, f1_box = anim_frames[0]
            await interaction.response.edit_message(
                embed=_build_gamble_embed(f"✨ **{f1_status}**", f1_box, discord.Color.blurple()),
                view=panel_view,
            )

            await asyncio.sleep(0.8)
            f2_status, f2_box = anim_frames[1]
            frame2_embed = _build_gamble_embed(f"✨ **{f2_status}**", f2_box, discord.Color.blurple())
            try:
                await interaction.edit_original_response(embed=frame2_embed)
            except discord.HTTPException:
                if msg is not None:
                    try:
                        await msg.edit(embed=frame2_embed)
                    except discord.HTTPException:
                        pass

            await asyncio.sleep(0.8)

            # 動畫結束：建立全新的 GamblePanelView 實例，讓 120 秒閒置計時器從開獎結束這一刻重新計算
            b_max = int(self.config.get("bet_max", 50000))
            next_bet = min(final_bal, b_max) if is_all_in else panel_view.bet
            new_view = GamblePanelView(
                self,
                interaction.user,
                next_bet,
                is_all_in=False,
                played_once=True,
            )
            new_view.message = msg
            final_embed = _build_gamble_embed(
                "🎊 **最終開獎結果**",
                final_center,
                final_color,
                settled=True,
                current_bet=new_view.bet,
            )

            for attempt in range(3):
                try:
                    await interaction.edit_original_response(embed=final_embed, view=new_view)
                    break
                except discord.HTTPException:
                    if msg is not None:
                        try:
                            await msg.edit(embed=final_embed, view=new_view)
                            break
                        except discord.HTTPException:
                            pass
                    if attempt < 2:
                        await asyncio.sleep(1.0)
        finally:
            self._animating_users.discard(uid)

    @app_commands.command(name="economy_gamble", description="🎲 開啟擲硬幣與骰子比大小互動面板（可自由調整賭注、All In 全押並觀看開獎動畫）")
    @app_commands.checks.cooldown(1, ECONOMY_GAME_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def economy_gamble(self, interaction: discord.Interaction):
        view = GamblePanelView(self, interaction.user)
        embed = view.build_lobby_embed()
        await interaction.response.send_message(embed=embed, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    # --------------------------------------------------------------------------
    # 5. 花錢遊玩的小遊戲 3：🃏 21 點 (/economy_blackjack)
    # --------------------------------------------------------------------------

    async def _start_blackjack(self, interaction: discord.Interaction, bet: int, *, is_all_in: bool = False):
        allowed, rem = self.check_button_cooldown(interaction.user.id)
        if not allowed:
            await interaction.response.send_message(
                f"⏳ 按鈕冷卻中，請稍後再試（還需 **{rem:.1f}** 秒）！",
                ephemeral=True,
            )
            return

        if interaction.user.id in self._active_blackjack_users:
            await interaction.response.send_message(
                "❌ 你目前已經有一局進行中的 21 點，請先完成該局再開始新牌局！",
                ephemeral=True,
            )
            return

        err = self._validate_bet(bet)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        cur = self.currency_str()
        ok, bal_after_bet = update_economy_balance(interaction.user.id, -bet, add_wagered=bet)
        if not ok:
            await interaction.response.send_message(
                f"❌ 餘額不足！本局需要 **{bet:,}** {cur}，你目前只有 **{bal_after_bet:,}** {cur}。",
                ephemeral=True,
            )
            return

        shoe = build_shoe()
        player_hand = [shoe.pop(), shoe.pop()]
        dealer_hand = [shoe.pop(), shoe.pop()]

        p_val = calculate_hand_value(player_hand)
        d_val = calculate_hand_value(dealer_hand)

        # 檢查起手是否直接 21 點 (Natural Blackjack)
        if p_val == 21 or d_val == 21:
            nat_mult = float(self.config.get("blackjack_natural_multiplier", 2.5))
            if p_val == 21 and d_val == 21:
                payout = bet
                color = discord.Color.gold()
                res_text = f"🤝 **雙方起手都是 Blackjack (21 點)！平手退還本金 {bet:,} {cur}！**"
            elif p_val == 21:
                payout = int(round(bet * nat_mult))
                color = discord.Color.gold()
                res_text = f"👑 **天生 Blackjack (21 點)！獲得 {nat_mult:g} 倍大獎 +{payout:,} {cur}（淨賺 +{payout - bet:,}）！**"
            else:
                payout = 0
                color = discord.Color.red()
                res_text = f"😱 **莊家起手就是 Blackjack (21 點)！你損失了 {bet:,} {cur}。**"

            if payout > 0:
                net_earned = max(0, payout - bet)
                _, final_bal = update_economy_balance(interaction.user.id, payout, add_earned=net_earned)
            else:
                final_bal = bal_after_bet

            dummy_view = BlackjackView(self, interaction.user, bet, shoe, player_hand, dealer_hand, is_all_in=is_all_in)
            dummy_view.finished = True
            embed = dummy_view.build_embed(reveal_dealer=True, result_text=res_text, balance=final_bal, color=color)
            replay_view = BlackjackLobbyView(self, interaction.user, bet, is_all_in=is_all_in, played_once=True)
            replay_view.message = interaction.message
            await interaction.response.edit_message(embed=embed, view=replay_view)
            return

        self._active_blackjack_users.add(interaction.user.id)
        view = BlackjackView(self, interaction.user, bet, shoe, player_hand, dealer_hand, is_all_in=is_all_in)
        embed = view.build_embed(reveal_dealer=False)
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = interaction.message

    @app_commands.command(name="economy_blackjack", description="🃏 開啟 21 點撲克牌對決互動面板（可自由調整賭注或 All In 全押發牌）")
    @app_commands.checks.cooldown(1, ECONOMY_GAME_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def economy_blackjack(self, interaction: discord.Interaction):
        view = BlackjackLobbyView(self, interaction.user)
        embed = view.build_lobby_embed()
        await interaction.response.send_message(embed=embed, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    # --------------------------------------------------------------------------
    # 6. 花錢遊玩的小遊戲 4：🎫 互動式九宮格刮刮樂 (/economy_scratch)
    # --------------------------------------------------------------------------

    async def _start_scratch(self, interaction: discord.Interaction, *, edit_existing: bool = False):
        if edit_existing:
            allowed, rem = self.check_button_cooldown(interaction.user.id)
            if not allowed:
                await interaction.response.send_message(
                    f"⏳ 按鈕冷卻中，請稍後再試（還需 **{rem:.1f}** 秒）！",
                    ephemeral=True,
                )
                return

        cost = int(self.config.get("scratch_cost", 100))
        cur = self.currency_str()
        ok, bal_after = update_economy_balance(interaction.user.id, -cost, add_wagered=cost)
        if not ok:
            await interaction.response.send_message(
                f"❌ 餘額不足！購買一張刮刮樂需要 **{cost:,}** {cur}，你目前只有 **{bal_after:,}** {cur}。",
                ephemeral=True,
            )
            return

        card_data = generate_scratch_card(self.config)
        view = ScratchCardView(self, interaction.user, cost, card_data)
        embed = view.build_embed()
        if edit_existing:
            await interaction.response.edit_message(embed=embed, view=view)
            view.message = interaction.message
        else:
            await interaction.response.send_message(embed=embed, view=view)
            try:
                view.message = await interaction.original_response()
            except discord.HTTPException:
                pass

    @app_commands.command(name="economy_scratch", description="🎫 花費金幣購買一張九宮格刮刮樂，湊滿 3 個相同圖案贏大獎！")
    @app_commands.checks.cooldown(1, ECONOMY_GAME_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def economy_scratch(self, interaction: discord.Interaction):
        await self._start_scratch(interaction, edit_existing=False)

    # --------------------------------------------------------------------------
    # 7. 後台參數控制面板 (@owner_guild_only)
    # --------------------------------------------------------------------------

    @owner_guild_only
    @app_commands.command(
        name="economy_admin",
        description="🛠️ [後台專屬] 開啟經濟系統參數控制面板，修改簽到獎勵、小遊戲倍率與管理使用者金幣",
    )
    @app_commands.check(owner_only)
    async def economy_admin(self, interaction: discord.Interaction):
        self.config = load_economy_config()
        view = EconomyAdminView(self, interaction.user.id)
        embed = view.build_panel_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))

