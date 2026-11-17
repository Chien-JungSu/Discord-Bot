"""獨立離線單元測試：驗證 `/bot_steal`（bot owner guild-only）與 `/steal` 指令行為。

涵蓋項目：
- 指令註冊與權限隔離：`@owner_guild_only`（僅同步至 OWNER_GUILD_ID）、`@app_commands.check(owner_only)`、OWNER_GUILD_ID 未設定時的 `guild_only` 退回防護
- 名稱清理與去重：`resolve_unique_emoji_name` 確保 2～32 字元且不與既有機器人表情符號衝突
- `/bot_steal` 執行流程：從 CDN 下載（支援多個 emoji 與 CDN 網址）、大小上限 256 KB 驗證、透過 `bot.create_application_emoji` 建立靜態與動態表情符號
- 錯誤處理與脫敏：DM 阻擋、格式錯誤、HTTP 失敗、檔案過大、`MissingApplicationID`、`ValueError`（不支援格式）、`Forbidden`、`HTTPException` 與未知例外通知開發者
- 回歸測試：確認 `/steal` 仍正確呼叫 `guild.create_custom_emoji`

執行方式：python tools/test_bot_steal.py
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import cogs.database as db
import cogs.general as general_mod

PASS: list[str] = []
FAIL: list[str] = []


def check(cond: bool, name: str) -> None:
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name)
    assert cond, name


def run_coro(coro):
    """輕量執行無真實網路 I/O 的測試 coroutine（避免受限沙箱無法建立事件迴圈 socketpair）。"""
    try:
        while True:
            yielded = coro.send(None)
            if yielded is not None:
                raise RuntimeError(f"測試 coroutine 意外等待非同步物件: {yielded!r}")
    except StopIteration as exc:
        return exc.value


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"\x89PNG\r\n\x1a\nfake"):
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeClientSession:
    def __init__(self, responses_by_url: dict[str, FakeResponse] | list[FakeResponse]):
        self.responses = responses_by_url
        self.requested_urls: list[str] = []

    def get(self, url: str, **kwargs):
        self.requested_urls.append(url)
        if isinstance(self.responses, list):
            idx = len(self.requested_urls) - 1
            return self.responses[idx]
        return self.responses[url]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def make_fake_bot(owner_id: int = 99999) -> commands.Bot:
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix=("!", "/"), intents=intents)
    bot.owner_id = owner_id
    bot.notify_owner_error = AsyncMock()  # type: ignore[attr-defined]
    bot.fetch_application_emojis = AsyncMock(return_value=[])  # type: ignore[assignment]
    bot.create_application_emoji = AsyncMock()  # type: ignore[assignment]
    return bot


def make_fake_interaction(
    bot: commands.Bot,
    *,
    user_id: int = 99999,
    guild_id: int | None = 111222333444555666,
) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.__str__ = lambda self=user: "OwnerUser#0001"
    interaction.user = user

    if guild_id is None:
        interaction.guild = None
    else:
        guild = MagicMock(spec=discord.Guild)
        guild.id = guild_id
        guild.create_custom_emoji = AsyncMock()
        interaction.guild = guild

    response = MagicMock()
    response.defer = AsyncMock()
    response.send_message = AsyncMock()
    response.is_done = MagicMock(return_value=False)
    interaction.response = response

    followup = MagicMock()
    followup.send = AsyncMock()
    interaction.followup = followup
    return interaction


def with_temp_db(fn):
    def wrapper(*args, **kwargs):
        tmpdir = tempfile.mkdtemp()
        orig_db_file = db.DB_FILE
        orig_db_init = db._db_initialized
        orig_wal_init = db._wal_initialized
        try:
            db.DB_FILE = os.path.join(tmpdir, "test_bot_steal.db")
            db._db_initialized = False
            db._wal_initialized = False
            return fn(*args, **kwargs)
        finally:
            db.DB_FILE = orig_db_file
            db._db_initialized = orig_db_init
            db._wal_initialized = orig_wal_init
            shutil.rmtree(tmpdir, ignore_errors=True)
    return wrapper


@with_temp_db
def test_command_registration_and_permissions() -> None:
    print("\n--- [1] 測試 /bot_steal 指令註冊與 owner_guild_only / owner_only 權限 ---")
    orig_owner_guild = os.environ.get("DISCORD_OWNER_GUILD_ID")
    orig_alt_guild = os.environ.get("OWNER_GUILD_ID")
    try:
        # 1. 有設定 OWNER_GUILD_ID 時：應僅註冊於該 guild，且不可含 contexts 衝突
        os.environ["DISCORD_OWNER_GUILD_ID"] = "777888999000111222"
        os.environ.pop("OWNER_GUILD_ID", None)
        importlib.reload(general_mod)

        bot = make_fake_bot(owner_id=424242)
        cog = general_mod.General(bot)
        with patch.object(cog._status_rotation_loop, "start"):
            run_coro(bot.add_cog(cog))

        global_cmd = bot.tree.get_command("bot_steal")
        guild_cmd = bot.tree.get_command("bot_steal", guild=discord.Object(id=777888999000111222))
        other_guild_cmd = bot.tree.get_command("bot_steal", guild=discord.Object(id=111111111111111111))

        check(global_cmd is None, "設定 OWNER_GUILD_ID 時，/bot_steal 不會註冊為全域指令")
        check(
            guild_cmd is not None and isinstance(guild_cmd, app_commands.Command),
            "設定 OWNER_GUILD_ID 時，/bot_steal 成功註冊為該擁有者伺服器專屬指令",
        )
        check(other_guild_cmd is None, "/bot_steal 不會出現在其他伺服器指令樹")
        payload = guild_cmd.to_dict(bot.tree)
        check(payload.get("contexts") is None, "guild 專屬 /bot_steal payload 不含衝突的 contexts 欄位")

        # 檢查 owner_only check
        owner_inter = make_fake_interaction(bot, user_id=424242, guild_id=777888999000111222)
        stranger_inter = make_fake_interaction(bot, user_id=123456, guild_id=777888999000111222)
        check(all(chk(owner_inter) for chk in guild_cmd.checks), "機器人擁有者通過 /bot_steal 的 owner_only 檢查")
        check(not all(chk(stranger_inter) for chk in guild_cmd.checks), "非擁有者被 /bot_steal 的 owner_only 檢查拒絕")

        # 2. 未設定 OWNER_GUILD_ID 時：應自動套用 guild_only 且仍受 owner_only 保護
        with patch("dotenv.load_dotenv", return_value=False):
            os.environ.pop("DISCORD_OWNER_GUILD_ID", None)
            os.environ.pop("OWNER_GUILD_ID", None)
            importlib.reload(general_mod)

        bot2 = make_fake_bot(owner_id=424242)
        cog2 = general_mod.General(bot2)
        with patch.object(cog2._status_rotation_loop, "start"):
            run_coro(bot2.add_cog(cog2))
        fallback_cmd = bot2.tree.get_command("bot_steal")
        check(
            fallback_cmd is not None and fallback_cmd.guild_only is True,
            "未設定 OWNER_GUILD_ID 時，/bot_steal 自動標記 guild_only=True",
        )
    finally:
        if orig_owner_guild is None:
            os.environ.pop("DISCORD_OWNER_GUILD_ID", None)
        else:
            os.environ["DISCORD_OWNER_GUILD_ID"] = orig_owner_guild
        if orig_alt_guild is None:
            os.environ.pop("OWNER_GUILD_ID", None)
        else:
            os.environ["OWNER_GUILD_ID"] = orig_alt_guild
        importlib.reload(general_mod)


def test_resolve_unique_emoji_name() -> None:
    print("\n--- [2] 測試表情符號名稱清理、長度與去重邏輯 ---")
    used = {"pepe_smile", "long_name_" + "a" * 22}
    check(len("long_name_" + "a" * 22) == 32, "測試基準名稱長度為 32 字元")

    n1 = general_mod.resolve_unique_emoji_name("pepe_smile", used, start_index=2)
    check(n1 == "pepe_smile_2", "與既有機器人表情符號撞名時自動加 _2 後綴")

    n2 = general_mod.resolve_unique_emoji_name("pepe_smile", used, start_index=2)
    check(n2 == "pepe_smile_3", "再次撞名時遞增為 _3")

    n3 = general_mod.resolve_unique_emoji_name("long_name_" + "a" * 22, used, start_index=2)
    check(len(n3) == 32 and n3.endswith("_2"), "32 字元上限名稱撞名後截斷並保留 _2 後綴（總長不超過 32）")

    n4 = general_mod.resolve_unique_emoji_name("x_", used, min_length=2)
    check(len(n4) >= 2 and n4 == "x_emoji", "單字元名稱自動補齊至至少 2 字元")


@with_temp_db
def test_bot_steal_success_and_dedup() -> None:
    print("\n--- [3] 測試 /bot_steal 成功偷取（單張、多張、動態、CDN 網址與名稱去重） ---")
    bot = make_fake_bot(owner_id=99999)
    bot.fetch_application_emojis = AsyncMock(return_value=[SimpleNamespace(name="pepe_smile")])
    created_objs = [
        SimpleNamespace(id=1001, name="pepe_smile_2", animated=False),
        SimpleNamespace(id=1002, name="cat_dance", animated=True),
        SimpleNamespace(id=1003, name="emote_555666", animated=False),
    ]
    bot.create_application_emoji = AsyncMock(side_effect=created_objs)

    cog = general_mod.General(bot)
    inter = make_fake_interaction(bot, user_id=99999)

    session = FakeClientSession([
        FakeResponse(200, b"\x89PNG\r\n\x1a\npng_bytes"),
        FakeResponse(200, b"GIF89a_gif_bytes"),
        FakeResponse(200, b"\x89PNG\r\n\x1a\ncdn_bytes"),
    ])

    raw_input = (
        "<:pepe_smile:111> "
        "<a:cat_dance:222> "
        "https://cdn.discordapp.com/emojis/555666.png"
    )
    with patch.object(aiohttp, "ClientSession", return_value=session):
        run_coro(cog.steal_to_bot.callback(cog, inter, raw_input))

    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    check(bot.create_application_emoji.await_count == 3, "共建立 3 個機器人應用程式表情符號")
    inter.guild.create_custom_emoji.assert_not_awaited()

    first_call = bot.create_application_emoji.await_args_list[0]
    second_call = bot.create_application_emoji.await_args_list[1]
    third_call = bot.create_application_emoji.await_args_list[2]
    check(first_call.kwargs == {"name": "pepe_smile_2", "image": b"\x89PNG\r\n\x1a\npng_bytes"}, "第一個撞名表情自動改為 pepe_smile_2")
    check(second_call.kwargs == {"name": "cat_dance", "image": b"GIF89a_gif_bytes"}, "第二個動態表情正確傳入 GIF bytes")
    check(third_call.kwargs == {"name": "emote_555666", "image": b"\x89PNG\r\n\x1a\ncdn_bytes"}, "第三個 CDN 網址表情正確解析並建立")

    sent_msg = inter.followup.send.call_args.args[0]
    check(
        "<:pepe_smile_2:1001>" in sent_msg and "<a:cat_dance:1002>" in sent_msg and "<:emote_555666:1003>" in sent_msg,
        "成功訊息正確格式化靜態與動態機器人表情符號預覽",
    )
    check("機器人表情符號" in sent_msg, "成功訊息清楚標示新增至機器人表情符號")


@with_temp_db
def test_bot_steal_validation_and_errors() -> None:
    print("\n--- [4] 測試 /bot_steal 邊界驗證與錯誤處理 ---")
    bot = make_fake_bot(owner_id=99999)
    cog = general_mod.General(bot)

    # 1. DM 阻擋
    dm_inter = make_fake_interaction(bot, user_id=99999, guild_id=None)
    run_coro(cog.steal_to_bot.callback(cog, dm_inter, "<:a:123>"))
    check("只能在伺服器中使用" in dm_inter.response.send_message.call_args.args[0], "DM 執行時回傳伺服器限定提示")

    # 2. 格式錯誤
    bad_inter = make_fake_interaction(bot, user_id=99999)
    run_coro(cog.steal_to_bot.callback(cog, bad_inter, "not an emoji"))
    check("格式錯誤" in bad_inter.response.send_message.call_args.args[0], "無效表情格式回傳格式錯誤提示")

    # 3. CDN 下載失敗 (HTTP 404)
    inter_404 = make_fake_interaction(bot, user_id=99999)
    with patch.object(aiohttp, "ClientSession", return_value=FakeClientSession([FakeResponse(404, b"")])):
        run_coro(cog.steal_to_bot.callback(cog, inter_404, "<:pepe:123>"))
    check("無法下載第 1 個 emoji" in inter_404.followup.send.call_args.args[0], "CDN 404 時回傳友善下載失敗提示")

    # 4. 超過 256 KB
    inter_big = make_fake_interaction(bot, user_id=99999)
    with patch.object(aiohttp, "ClientSession", return_value=FakeClientSession([FakeResponse(200, b"x" * 262145)])):
        run_coro(cog.steal_to_bot.callback(cog, inter_big, "<:pepe:123>"))
    check("256 KB" in inter_big.followup.send.call_args.args[0], "超過 256 KB 時阻擋並提示上限")

    # 5. MissingApplicationID
    inter_missing_app = make_fake_interaction(bot, user_id=99999)
    bot.create_application_emoji = AsyncMock(side_effect=discord.MissingApplicationID())
    with patch.object(aiohttp, "ClientSession", return_value=FakeClientSession([FakeResponse(200, b"\x89PNG\r\n\x1a\nok")])):
        run_coro(cog.steal_to_bot.callback(cog, inter_missing_app, "<:pepe:123>"))
    check("Application ID" in inter_missing_app.followup.send.call_args.args[0], "MissingApplicationID 回傳友善提示")

    # 6. ValueError (Unsupported image type)
    inter_bad_img = make_fake_interaction(bot, user_id=99999)
    bot.create_application_emoji = AsyncMock(side_effect=ValueError("Unsupported image type given"))
    with patch.object(aiohttp, "ClientSession", return_value=FakeClientSession([FakeResponse(200, b"badbytes")])):
        run_coro(cog.steal_to_bot.callback(cog, inter_bad_img, "<:pepe:123>"))
    check("不支援的圖片格式" in inter_bad_img.followup.send.call_args.args[0], "不支援的圖片格式回傳友善提示且不誤報開發者")
    bot.notify_owner_error.assert_not_awaited()

    # 7. discord.HTTPException
    inter_http = make_fake_interaction(bot, user_id=99999)
    fake_http_resp = SimpleNamespace(status=400, reason="Bad Request")
    bot.create_application_emoji = AsyncMock(side_effect=discord.HTTPException(fake_http_resp, "Maximum emojis reached"))
    with patch.object(aiohttp, "ClientSession", return_value=FakeClientSession([FakeResponse(200, b"\x89PNG\r\n\x1a\nok")])):
        run_coro(cog.steal_to_bot.callback(cog, inter_http, "<:pepe:123>"))
    check("建立機器人表情符號失敗" in inter_http.followup.send.call_args.args[0], "HTTPException 回傳友善提示而不曝露原始錯誤")

    # 8. 未知例外 -> notify_owner_error
    inter_err = make_fake_interaction(bot, user_id=99999)
    bot.create_application_emoji = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(aiohttp, "ClientSession", return_value=FakeClientSession([FakeResponse(200, b"\x89PNG\r\n\x1a\nok")])):
        run_coro(cog.steal_to_bot.callback(cog, inter_err, "<:pepe:123>"))
    bot.notify_owner_error.assert_awaited_once()
    check(
        bot.notify_owner_error.await_args.kwargs.get("extra_info") == "General.bot_steal",
        "未知例外正確呼叫 notify_owner_error(extra_info='General.bot_steal')",
    )


@with_temp_db
def test_steal_regression() -> None:
    print("\n--- [5] 回歸測試：/steal 仍維持原伺服器表情符號流程 ---")
    bot = make_fake_bot(owner_id=99999)
    cog = general_mod.General(bot)
    inter = make_fake_interaction(bot, user_id=12345)
    inter.guild.create_custom_emoji = AsyncMock(
        side_effect=[
            SimpleNamespace(id=2001, name="pepe", animated=False),
            SimpleNamespace(id=2002, name="pepe_2", animated=False),
        ]
    )
    session = FakeClientSession([
        FakeResponse(200, b"\x89PNG\r\n\x1a\n1"),
        FakeResponse(200, b"\x89PNG\r\n\x1a\n2"),
    ])
    with patch.object(aiohttp, "ClientSession", return_value=session):
        run_coro(cog.steal.callback(cog, inter, "<:pepe:1> <:pepe:2>"))

    check(inter.guild.create_custom_emoji.await_count == 2, "/steal 仍呼叫 guild.create_custom_emoji")
    bot.create_application_emoji.assert_not_awaited()
    check(
        inter.guild.create_custom_emoji.await_args_list[1].kwargs["name"] == "pepe_2",
        "/steal 同批次重複名稱自動加 _2 後綴",
    )
    check(
        "成功偷到2個表情符號: <:pepe:2001> <:pepe_2:2002>" in inter.followup.send.call_args.args[0],
        "/steal 回覆格式與原本完全一致",
    )


def main() -> int:
    test_command_registration_and_permissions()
    test_resolve_unique_emoji_name()
    test_bot_steal_success_and_dedup()
    test_bot_steal_validation_and_errors()
    test_steal_regression()
    print(f"\n共 {len(PASS) + len(FAIL)} 項測試：{len(PASS)} 通過，{len(FAIL)} 失敗")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
