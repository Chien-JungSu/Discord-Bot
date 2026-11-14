"""獨立離線單元測試：驗證極致輕量、零 AI 模型依賴的「台灣幹話產生器」。

涵蓋項目：
- memes_data.json 語料結構與規模驗證（categorized_memes >= 15 組、fallback_memes >= 15 句）
- LightweightGanhuaEngine：字元級 TF-IDF 檢索、模糊比對、未命中保底池、Mention 清理與 reload() 熱重載
- /幹話 與 /ganhua 斜線指令：Embed 格式、ephemeral 控制、空輸入防呆
- 右鍵訊息選單「用幹話回覆」：純圖片/貼圖防呆、過濾 Mention、target_message.reply + ephemeral 確認與權限退回
- main.py 整合檢查：INITIAL_EXTENSIONS、intents.message_content、!sync 擁有者指令與確認未註冊冗餘重載指令
- 零深度學習框架檢驗：確認未匯入 torch / transformers / tensorflow

執行方式：python tools/test_ganhua.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import discord
from discord import app_commands
from discord.ext import commands

import cogs.ganhua as ganhua_mod
import cogs.ganhua_engine as engine_mod
from cogs.ganhua import Ganhua, build_ganhua_embed
from cogs.ganhua_engine import (
    EMPTY_INPUT_REPLY,
    LightweightGanhuaEngine,
    clean_input_text,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(cond: bool, name: str) -> None:
    (PASS if cond else FAIL).append(name)
    print(("✅ " if cond else "❌ ") + name)


def run_coro(coro):
    """輕量執行測試 coroutine，避免受限沙箱無法建立事件迴圈 socketpair。"""
    try:
        while True:
            yielded = coro.send(None)
            if yielded is not None:
                raise RuntimeError(f"測試 coroutine 意外等待非同步物件: {yielded!r}")
    except StopIteration as exc:
        return exc.value


def make_fake_bot(owner_id: int = 99999) -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix=("!", "/"), intents=intents)
    bot.owner_id = owner_id
    bot.notify_owner_error = AsyncMock()  # type: ignore[attr-defined]
    return bot


def make_fake_interaction(
    user_name: str = "測試仔",
    user_id: int = 10001,
    manage_guild: bool = False,
) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.name = user_name
    user.display_name = user_name
    user.display_avatar = SimpleNamespace(url="https://cdn.discordapp.com/embed/avatars/0.png")
    user.guild_permissions = SimpleNamespace(manage_guild=manage_guild, administrator=False)
    interaction.user = user

    response = MagicMock()
    response.defer = AsyncMock()
    response.send_message = AsyncMock()
    response.is_done = MagicMock(return_value=False)
    interaction.response = response

    followup = MagicMock()
    followup.send = AsyncMock()
    interaction.followup = followup
    interaction.delete_original_response = AsyncMock()
    return interaction


def make_fake_message(content: str = "", attachments: list | None = None) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.content = content
    message.attachments = attachments or []
    message.reply = AsyncMock()
    return message


def test_zero_deep_learning_deps() -> None:
    print("\n--- [1] 測試零深度學習框架依賴 ---")
    forbidden = ("torch", "transformers", "tensorflow")
    for mod_name in forbidden:
        check(mod_name not in sys.modules, f"執行環境未載入 {mod_name}")

    for rel_path in ("cogs/ganhua_engine.py", "cogs/ganhua.py"):
        full_path = os.path.join(PROJECT_ROOT, rel_path)
        with open(full_path, "r", encoding="utf-8") as fp:
            src = fp.read()
        has_dl_import = any(
            f"import {lib}" in src or f"from {lib}" in src
            for lib in forbidden
        )
        check(not has_dl_import, f"{rel_path} 原始碼完全不含深度學習框架匯入")


def test_memes_data_json() -> None:
    print("\n--- [2] 測試 memes_data.json 外部語料庫規格 ---")
    json_path = os.path.join(PROJECT_ROOT, "memes_data.json")
    check(os.path.exists(json_path), "專案根目錄存在 memes_data.json")
    with open(json_path, "r", encoding="utf-8") as fp:
        data = json.load(fp)

    cat_memes = data.get("categorized_memes", [])
    fb_memes = data.get("fallback_memes", [])
    check(isinstance(cat_memes, list) and len(cat_memes) >= 15, f"categorized_memes 至少 15 組（目前 {len(cat_memes)} 組）")
    check(isinstance(fb_memes, list) and len(fb_memes) >= 15, f"fallback_memes 至少 15 句（目前 {len(fb_memes)} 句）")

    all_valid = all(
        isinstance(item, dict)
        and isinstance(item.get("keywords"), list)
        and len(item["keywords"]) > 0
        and isinstance(item.get("replies"), list)
        and len(item["replies"]) > 0
        for item in cat_memes
    )
    check(all_valid, "所有主題皆包含非空的 keywords 與 replies 陣列")


def test_lightweight_engine() -> None:
    print("\n--- [3] 測試 LightweightGanhuaEngine 檢索、保底與熱重載 ---")
    check(clean_input_text(" <@123456>  明天要段考  <@!999> \n <#777> ") == "明天要段考", "clean_input_text 正確過濾 Mention 與空白")
    check(clean_input_text("<@123> <@&456>") == "", "純 Mention 清理後回傳空字串")

    engine = LightweightGanhuaEngine(data_path="memes_data.json")
    check(len(engine.categorized_memes) >= 30, f"引擎載入至少 30 組分類幹話（目前 {len(engine.categorized_memes)} 組）")
    check(len(engine.fallback_memes) >= 30, f"引擎載入至少 30 句萬用保底幹話（目前 {len(engine.fallback_memes)} 句）")

    # 測試毫秒級反應與關鍵字命中
    t0 = time.perf_counter()
    exam_reply = engine.reply("明天要段考微積分讀不完怎麼辦")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    exam_candidates = set(engine.categorized_memes[0][1])
    check(exam_reply in exam_candidates, "段考提問精準命中考試學業幹話庫")
    check(elapsed_ms < 50.0, f"單次推論耗時極低（{elapsed_ms:.2f} ms）")

    food_reply = engine.reply("好餓喔宵夜吃什麼義大利麵")
    food_candidates = set(engine.categorized_memes[2][1])
    check(food_reply in food_candidates, "宵夜飲食提問命中義大利麵混凝土／西北風幹話庫")

    # 測試新增之寶可夢梗、諧音梗與時事梗
    groudon_reply = engine.reply("固拉多為什麼不會飛 塞車")
    check(groudon_reply in set(engine.categorized_memes[20][1]), "固拉多梗精準命中寶可夢固拉多幹話庫")

    ptcg_reply = engine.reply("PTCG 小霞擲硬幣反面 開卡包")
    check(ptcg_reply in set(engine.categorized_memes[22][1]), "PTCG Pocket 抽卡梗精準命中卡牌幹話庫")

    pun_reply = engine.reply("台灣諧音梗 冷笑話 講個笑話")
    check(pun_reply in set(engine.categorized_memes[24][1]), "台灣諧音梗提問命中諧教冷笑話幹話庫")

    threads_reply = engine.reply("這波操作 YBSG 高麗菜 留友看")
    check(threads_reply in set(engine.categorized_memes[26][1]), "Threads 流行語命中高麗菜煮蛋桌幹話庫")

    stock_reply = engine.reply("台股 台積電 套牢 當沖 韭菜")
    check(stock_reply in set(engine.categorized_memes[29][1]), "台股韭菜時事提問命中股票幹話庫")

    # 測試未命中任何關鍵字時從 fallback_memes 抽取
    unmatched_reply = engine.reply("xyzqwertypoiuytrewq 完全不相關測試詞彙")
    check(unmatched_reply in set(engine.fallback_memes), "未命中關鍵字時從 fallback_memes 保底抽取")

    # 測試空輸入／純 Mention
    check(engine.reply("   <@12345>  ") == EMPTY_INPUT_REPLY, "過濾後為空字串時回傳預設無文字吐槽")

    # 測試隨機生成 random_reply()
    all_pool = {rep for _, reps in engine.categorized_memes for rep in reps} | set(engine.fallback_memes)
    rand_msg = engine.random_reply()
    check(isinstance(rand_msg, str) and rand_msg in all_pool, "random_reply() 從所有幹話語料總池隨機抽選")

    # 測試暫存 JSON 熱重載（不碰觸真實檔案）
    tmp_dir = tempfile.mkdtemp()
    try:
        tmp_json = os.path.join(tmp_dir, "custom_memes.json")
        with open(tmp_json, "w", encoding="utf-8") as fp:
            json.dump(
                {
                    "categorized_memes": [
                        {"keywords": ["測試熱重載"], "replies": ["第一版幹話"]}
                    ],
                    "fallback_memes": ["保底一號"],
                },
                fp,
                ensure_ascii=False,
            )
        hot_engine = LightweightGanhuaEngine(data_path=tmp_json)
        check(hot_engine.reply("測試熱重載") == "第一版幹話", "暫存 JSON 初次載入成功")

        with open(tmp_json, "w", encoding="utf-8") as fp:
            json.dump(
                {
                    "categorized_memes": [
                        {"keywords": ["測試熱重載"], "replies": ["第二版新梗"]},
                        {"keywords": ["新增主題"], "replies": ["熱更新成功"]},
                    ],
                    "fallback_memes": ["保底二號"],
                },
                fp,
                ensure_ascii=False,
            )
        reloaded_count = hot_engine.reload()
        check(reloaded_count == 2, "reload() 回傳最新載入的主題組數 (2)")
        check(hot_engine.reply("新增主題") == "熱更新成功", "reload() 後新加入的主題立刻生效")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_slash_commands() -> None:
    print("\n--- [4] 測試 /幹話 與 /ganhua 無參數隨機斜線指令 ---")
    bot = make_fake_bot()
    engine = LightweightGanhuaEngine(data_path="memes_data.json")
    cog = Ganhua(bot, engine=engine)
    run_coro(bot.add_cog(cog))

    zh_cmd = bot.tree.get_command("幹話")
    en_cmd = bot.tree.get_command("ganhua")
    check(zh_cmd is not None and isinstance(zh_cmd, app_commands.Command), "已註冊 /幹話 斜線指令")
    check(en_cmd is not None and isinstance(en_cmd, app_commands.Command), "已註冊 /ganhua 斜線指令")
    check(bot.tree.get_command("重載幹話庫") is None, "已移除冗餘的 /重載幹話庫 指令（統一使用 /reload_cogs）")
    check(bot.tree.get_command("reload_ganhua") is None, "已移除冗餘的 /reload_ganhua 指令")

    if isinstance(zh_cmd, app_commands.Command) and isinstance(en_cmd, app_commands.Command):
        check(len(zh_cmd.parameters) == 0, "/幹話 已移除所有參數")
        check(len(en_cmd.parameters) == 0, "/ganhua 已移除所有參數")

    all_pool = {rep for _, reps in engine.categorized_memes for rep in reps} | set(engine.fallback_memes)

    # 測試 /幹話 直接隨機生成幹話並公開發送
    interaction_zh = make_fake_interaction()
    run_coro(cog.ganhua_zh_slash.callback(cog, interaction_zh))
    interaction_zh.response.send_message.assert_awaited_once()
    zh_reply = interaction_zh.response.send_message.call_args.args[0]
    zh_kwargs = interaction_zh.response.send_message.call_args.kwargs
    check(zh_reply in all_pool, "/幹話 回傳幹話語料池內的隨機一句")
    check("ephemeral" not in zh_kwargs, "/幹話 預設公開發送至頻道")

    # 測試 /ganhua 直接隨機生成幹話
    interaction_en = make_fake_interaction()
    run_coro(cog.ganhua_slash.callback(cog, interaction_en))
    interaction_en.response.send_message.assert_awaited_once()
    en_reply = interaction_en.response.send_message.call_args.args[0]
    check(en_reply in all_pool, "/ganhua 回傳幹話語料池內的隨機一句")

    run_coro(bot.remove_cog(cog.qualified_name))


def test_context_menu() -> None:
    print("\n--- [5] 測試訊息右鍵選單「用幹話回覆」（無私人確認通知） ---")
    bot = make_fake_bot()
    engine = LightweightGanhuaEngine(data_path="memes_data.json")
    cog = Ganhua(bot, engine=engine)

    ctx_cmd = bot.tree.get_command("用幹話回覆", type=discord.AppCommandType.message)
    check(ctx_cmd is not None and isinstance(ctx_cmd, app_commands.ContextMenu), "已註冊右鍵訊息選單「用幹話回覆」")

    # 情境 1：純圖片 / 貼圖（無文字）
    interaction_img = make_fake_interaction()
    msg_img = make_fake_message(content="", attachments=[MagicMock()])
    run_coro(cog.ganhua_context_menu(interaction_img, msg_img))
    interaction_img.response.send_message.assert_awaited_once()
    check(interaction_img.response.send_message.call_args.kwargs["ephemeral"] is True, "純圖片/貼圖訊息以 ephemeral=True 提示無法吐槽")
    msg_img.reply.assert_not_awaited()

    # 情境 2：正常文字含 Mention，透過 target_message.reply 回覆並刪除互動佔位，不留私人通知
    interaction_ok = make_fake_interaction()
    msg_ok = make_fake_message(content=" <@111222> 借錢不還 下週還 ")
    run_coro(cog.ganhua_context_menu(interaction_ok, msg_ok))
    interaction_ok.response.defer.assert_awaited_once_with(ephemeral=True)
    msg_ok.reply.assert_awaited_once()
    reply_text = msg_ok.reply.call_args.args[0]
    pidgeot_candidates = set(engine.categorized_memes[10][1])
    check(reply_text in pidgeot_candidates, "右鍵訊息過濾 Mention 後精準檢索出比雕幹話")
    check(msg_ok.reply.call_args.kwargs["mention_author"] is False, "target_message.reply 關閉 mention_author")
    interaction_ok.delete_original_response.assert_awaited_once()
    interaction_ok.response.send_message.assert_not_awaited()
    interaction_ok.followup.send.assert_not_awaited()
    check(True, "右鍵成功回覆後刪除佔位回應，完全不發送私人通知")

    # 情境 3：頻道缺乏回覆權限時安全退回 followup.send
    interaction_fb = make_fake_interaction()
    msg_fb = make_fake_message(content="群組好安靜 好無聊")
    msg_fb.reply = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Permissions"))
    run_coro(cog.ganhua_context_menu(interaction_fb, msg_fb))
    interaction_fb.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction_fb.followup.send.assert_awaited_once()
    interaction_fb.delete_original_response.assert_not_awaited()
    fb_reply = interaction_fb.followup.send.call_args.args[0]
    check(fb_reply in set(engine.categorized_memes[9][1]), "reply 受限時安全退回 followup.send 並傳送對應幹話")

    run_coro(cog.cog_unload())
    check(bot.tree.get_command("用幹話回覆", type=discord.AppCommandType.message) is None, "cog_unload 正確卸載右鍵選單")


def test_main_integration() -> None:
    print("\n--- [6] 測試 main.py 整合與 !sync 指令 ---")
    import main as main_mod

    check("cogs.ganhua" in main_mod.INITIAL_EXTENSIONS, "main.INITIAL_EXTENSIONS 包含 cogs.ganhua")
    check(main_mod.bot.intents.message_content is True, "main.bot.intents.message_content 已啟用")

    sync_cmd = main_mod.bot.get_command("sync")
    check(sync_cmd is not None and len(sync_cmd.checks) >= 1, "main.bot 已註冊含 @commands.is_owner() 的 !sync 指令")

    fake_ctx = MagicMock(spec=commands.Context)
    fake_ctx.bot = MagicMock()
    fake_ctx.bot.tree = MagicMock()
    fake_ctx.bot.tree.sync = AsyncMock(return_value=[MagicMock(), MagicMock(), MagicMock()])
    fake_ctx.bot._print_synced_commands = MagicMock()
    fake_ctx.reply = AsyncMock()
    fake_ctx.guild = MagicMock(spec=discord.Guild)
    fake_ctx.guild.id = 123456
    fake_ctx.guild.name = "幹話測試群"

    run_coro(main_mod.sync_commands.callback(fake_ctx, None))
    fake_ctx.bot.tree.sync.assert_awaited_once_with()
    check("3" in fake_ctx.reply.call_args.args[0], "!sync 全域同步回報同步指令數")

    fake_ctx.bot.tree.sync.reset_mock()
    fake_ctx.reply.reset_mock()
    run_coro(main_mod.sync_commands.callback(fake_ctx, "guild"))
    fake_ctx.bot.tree.sync.assert_awaited_once_with(guild=fake_ctx.guild)
    check("幹話測試群" in fake_ctx.reply.call_args.args[0], "!sync guild 立即同步當前伺服器")


def main() -> int:
    test_zero_deep_learning_deps()
    test_memes_data_json()
    test_lightweight_engine()
    test_slash_commands()
    test_context_menu()
    test_main_integration()

    print(f"\n共 {len(PASS) + len(FAIL)} 項測試：{len(PASS)} 通過，{len(FAIL)} 失敗")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
