"""AI 模型拆分（含 Gemini Hydrogen 與預設 Flash-Lite）及 LaTeX 自動格式化離線單元測試。"""
# pyright: reportMissingImports=false

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.ai import (
    AI,
    MEDIA_IMAGE_TOKEN_RE,
    RENDERED_MEDIA_FILENAME_PATTERN,
    _build_ai_model_choices,
    _prepare_ai_output,
    _resolve_provider_and_model,
)
from cogs.database import AI_PROVIDER_MODELS, MODEL_TOKEN_RATES
from cogs.latex_formatter import format_latex_expression, format_latex_in_text
from cogs.latex_renderer import extract_and_render_latex_formulas, render_latex_to_image
from cogs.llm_router import (
    DEPRECATED_MODEL_MAPPING,
    MultiProviderLLMRouter,
    _gemini_reasoning_effort,
    _infer_provider_from_model,
)
from cogs.table_renderer import _split_table_pipes, extract_and_render_markdown_tables, strip_cell_markdown


def test_gemini_models_split_and_defaults():
    """驗證 Gemini 模型已拆分為獨立選項，預設為最新 Flash-Lite 並納入 Hydrogen。"""
    assert len(AI_PROVIDER_MODELS) <= 25
    ids = [m["id"] for m in AI_PROVIDER_MODELS]

    assert "gemini:gemini-3.5-flash-lite" in ids
    assert "gemini:gemini-4-hydrogen" in ids
    expected_split = [
        "gemini:gemini-3.8-flash",
        "gemini:gemini-3.7-flash",
        "gemini:gemini-3.6-flash",
        "gemini:gemini-3.5-flash",
        "gemini:gemini-3-flash-preview",
        "gemini:gemini-3.1-flash-lite",
        "gemini:gemini-2.5-flash",
        "gemini:gemini-flash-latest",
        "gemini:gemini-flash-lite-latest",
    ]
    for model_id in expected_split:
        assert model_id in ids, f"缺少獨立模型選項: {model_id}"
        assert model_id in MODEL_TOKEN_RATES
        bare_id = model_id.split(":", 1)[1]
        assert bare_id in MODEL_TOKEN_RATES

    # 預設 Gemini 項目應排在第一位 Gemini 模型
    gemini_entries = [m for m in AI_PROVIDER_MODELS if m.get("provider") == "gemini"]
    assert gemini_entries[0]["id"] == "gemini:gemini-3.5-flash-lite"

    # 斜線指令選單符合 Discord 上限規定
    choices = _build_ai_model_choices()
    assert len(choices) == len(AI_PROVIDER_MODELS)
    assert all(len(c.name) <= 100 for c in choices)
    choice_values = [c.value for c in choices]
    assert "gemini:gemini-3.5-flash-lite" in choice_values
    assert "gemini:gemini-4-hydrogen" in choice_values


def test_ai_command_provider_and_model_resolution():
    """驗證 /ai 與伺服器設定解析能相容 `provider:model`、純模型名稱與舊版 `provider` 值。"""
    assert _resolve_provider_and_model("gemini:gemini-4-hydrogen", "auto") == ("gemini", "gemini-4-hydrogen")
    assert _resolve_provider_and_model("auto", "gemini:gemini-3.8-flash") == ("gemini", "gemini-3.8-flash")
    assert _resolve_provider_and_model("gemini", "gemini:gemini-3.6-flash") == ("gemini", "gemini-3.6-flash")
    assert _resolve_provider_and_model("gemini", "nvidia:deepseek-ai/deepseek-v4.1-flash") == ("gemini", None)
    assert _resolve_provider_and_model("gemini-4-hydrogen", "auto") == (None, "gemini-4-hydrogen")
    assert _resolve_provider_and_model("auto", "auto") == (None, None)


def test_llm_router_defaults_and_hydrogen_routing():
    """離線模擬 OpenAI 端點，驗證預設 Flash-Lite、Hydrogen 路由與 reasoning_effort 降級。"""
    assert DEPRECATED_MODEL_MAPPING["gemini-2.5-flash-lite"] == "gemini-3.5-flash-lite"
    assert DEPRECATED_MODEL_MAPPING["gemini-3.0-flash"] == "gemini-3-flash-preview"
    assert _infer_provider_from_model("gemini-4-hydrogen") == "gemini"
    assert _infer_provider_from_model("gemini-3.5-flash-lite") == "gemini"
    assert _gemini_reasoning_effort("gemini-3.5-flash-lite") == "minimal"
    assert _gemini_reasoning_effort("gemini-2.5-flash") == "none"
    assert _gemini_reasoning_effort("gemini-4-hydrogen") == "low"

    with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key-for-test"}, clear=True):
        r = MultiProviderLLMRouter()
        r.reload_env_keys = MagicMock()
        gemini = r.providers["gemini"]
        assert gemini.default_text_model == "gemini-3.5-flash-lite"
        assert gemini.default_vision_model == "gemini-3.5-flash-lite"
        assert "gemini-4-hydrogen" in gemini.fallback_models

        fake_resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(total_tokens=42, prompt_tokens=20, completion_tokens=22),
        )

        # 1. 指定 bare preferred_model='gemini-4-hydrogen' 時自動選用 Gemini 並帶 reasoning_effort='low'
        mock_create = AsyncMock(return_value=fake_resp)
        mock_client = MagicMock()
        mock_client.chat.completions.create = mock_create
        gemini.client = mock_client
        r.image_url_to_base64_data_uri = AsyncMock(return_value="data:image/png;base64,ZmFrZQ==")

        text, err, stats = asyncio.run(
            r.generate_chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                preferred_model="gemini-4-hydrogen",
            )
        )
        assert err is None and text == "ok"
        assert stats["provider_id"] == "gemini"
        assert stats["model"] == "gemini-4-hydrogen"
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["model"] == "gemini-4-hydrogen"
        assert call_kwargs.get("reasoning_effort") == "low"
        assert "extra_body" not in call_kwargs

        # 2. 不指定模型時文字與圖片視覺模式預設皆走 gemini-3.5-flash-lite + reasoning_effort='minimal'
        mock_create.reset_mock()
        _, err_v, stats_v = asyncio.run(
            r.generate_chat_completion(
                messages=[{"role": "user", "content": "看圖"}],
                image_url="https://example.com/test.png",
                preferred_provider_id="gemini",
            )
        )
        assert err_v is None
        assert stats_v["model"] == "gemini-3.5-flash-lite"
        assert mock_create.call_args.kwargs.get("reasoning_effort") == "minimal"

        # 3. 模擬 400 Bad Request 時自動移除 reasoning_effort 重試並成功
        mock_create.reset_mock()
        mock_create.side_effect = [
            Exception("Error code: 400 - Unknown field reasoning_effort"),
            fake_resp,
        ]
        text_retry, err_retry, _ = asyncio.run(
            r.generate_chat_completion(
                messages=[{"role": "user", "content": "retry"}],
                preferred_model="gemini-3.8-flash",
            )
        )
        assert err_retry is None and text_retry == "ok"
        assert mock_create.await_count == 2
        assert "reasoning_effort" in mock_create.call_args_list[0].kwargs
        assert "reasoning_effort" not in mock_create.call_args_list[1].kwargs


def test_latex_expression_conversion():
    """驗證常見數學公式、分數、開方、上下標、希臘字母與化學式轉換。"""
    assert format_latex_expression(r"E = mc^2") == "E = mc²"
    assert format_latex_expression(r"\alpha + \beta = \gamma") == "α + β = γ"
    assert format_latex_expression(r"x_1 + x_2 + \dots + x_n") == "x₁ + x₂ + … + xₙ"
    assert format_latex_expression(r"2^{10} = 1024") == "2¹⁰ = 1024"
    assert format_latex_expression(r"10^{-5}") == "10⁻⁵"
    assert format_latex_expression(r"x^{2k}") == "x^(2k)"
    assert format_latex_expression(r"\frac{1}{2}") == "½"
    assert format_latex_expression(r"\frac{1}{2a}") == "1/(2a)"
    assert format_latex_expression(r"\frac{-b \pm \sqrt{b^2 - 4ac}}{2a}") == "(-b ± √(b² - 4ac))/(2a)"
    assert format_latex_expression(r"\sqrt[3]{27} = 3") == "∛27 = 3"
    assert format_latex_expression(r"\binom{n}{k}") == "C(n, k)"
    assert format_latex_expression(r"90^\circ") == "90°"
    assert format_latex_expression(r"\forall x \in \mathbb{R}, x^2 \geq 0") == "∀ x ∈ ℝ, x² ≥ 0"
    assert "H₂O" in format_latex_expression(r"\ce{CO2 + H2O -> H2CO3}")
    assert "→" in format_latex_expression(r"\ce{CO2 + H2O -> H2CO3}")


def test_latex_in_text_blocks_currency_and_code_safety():
    """驗證區塊公式、矩陣、分段函式、程式碼保護與金額防誤判機制。"""
    # 單行區塊公式置於引用粗體中，混合星號粗體亦能安全共處
    assert format_latex_in_text("**$\\frac{1}{2} mv^2$**") == "**½ mv²**"
    assert format_latex_in_text("$$\n\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}\n$$") == "> **∑\\_(i = 1)ⁿ i = (n(n + 1))/2**"
    assert format_latex_in_text("\\[ \\lim_{x \\to \\infty} \\frac{1}{x} = 0 \\]") == "> **lim\\_(x → ∞) 1/x = 0**"

    # Markdown 跳脫保護：可轉換下標優先轉為 Unicode（如 aₘₐₓ），無法轉換者保留並跳脫底線（如 a\_(foo), b\_i）
    escaped = format_latex_in_text(r"令 $a_{max} + a_{foo} + b_i = c_1$")
    assert "aₘₐₓ" in escaped
    assert r"a\_(foo)" in escaped
    assert r"b\_i" in escaped
    assert "c₁" in escaped

    # 矩陣與 cases 分段函式（包含未加 $$ 的獨立環境）轉為對齊 code block
    matrix_src = r"\begin{bmatrix} 1 & 2 \\ 3 & 4 \end{bmatrix}"
    matrix_out = format_latex_in_text(matrix_src)
    assert matrix_out.startswith("```text\n") and matrix_out.endswith("\n```")
    assert "⎡" in matrix_out and "⎣" in matrix_out

    cases_src = r"$$f(x) = \begin{cases} x^2 & x \ge 0 \\ -x & x < 0 \end{cases}$$"
    cases_out = format_latex_in_text(cases_src)
    assert "⎧" in cases_out and "⎩" in cases_out and "≥" in cases_out

    # 程式碼區塊與反引號內文字嚴格不受影響
    code_sample = "行內 `$x^2$` 保留：\n```latex\n$$\\frac{a}{b}$$\n```"
    assert format_latex_in_text(code_sample) == code_sample

    # 金額寫法嚴格不誤判為數學公式
    money_samples = [
        "這本書售價 $100。",
        "Costs $10 to $20 per item.",
        "價格區間為 $10-$20 之間",
        r"跳脫錢字號 \$50 與 $x^2$",
    ]
    assert format_latex_in_text(money_samples[0]) == money_samples[0]
    assert format_latex_in_text(money_samples[1]) == money_samples[1]
    assert format_latex_in_text(money_samples[2]) == money_samples[2]
    assert format_latex_in_text(money_samples[3]) == r"跳脫錢字號 \$50 與 x²"


def test_table_and_ai_output_pipeline_with_latex():
    """驗證含 $|x|$、下標、分數與金額欄位的 Markdown 表格能正確轉圖且後續本文正常轉換。"""
    sample = (
        "導出公式整理：\n"
        "| 項目 | 表達式 | 備註 |\n"
        "| --- | --- | --- |\n"
        "| 絕對值 | $|x|$ | $a_{max}$ |\n"
        "| 分數 | **$\\frac{1}{2}$** | $100 | $200 |\n"
    )
    # 先檢查只包含 3 欄的有效表格（含 $|x|$ 與 $a_{max}$）
    table_only = (
        "前文 $x^2$。\n\n"
        "| 項目 | 表達式 | 備註 |\n"
        "| --- | --- | --- |\n"
        "| 絕對值 | $|x|$ | $a_{max}$ |\n"
        "| 分數 | **$\\frac{1}{2}$** | 單價 $100 |\n\n"
        "結論：\\[ E = mc^2 \\]"
    )
    rendered_text, files = extract_and_render_markdown_tables(table_only)
    assert len(files) == 1
    assert files[0].filename.endswith(".png")
    for f in files:
        f.close()

    # 確認 `_prepare_ai_output` 先轉表格，再將 2D/顯示公式轉圖片，最後格式化行內 Unicode
    final_text, final_files = _prepare_ai_output(table_only)
    try:
        assert [f.filename for f in final_files] == ["table_1.png", "math_1.png"]
    finally:
        for f in final_files:
            f.close()
    assert "前文 x²。" in final_text
    assert "[[TABLE_IMAGE:table_1.png]]" in final_text
    assert "[[MATH_IMAGE:math_1.png]]" in final_text
    assert MEDIA_IMAGE_TOKEN_RE.findall(final_text) == ["table_1.png", "math_1.png"]
    assert RENDERED_MEDIA_FILENAME_PATTERN.match("table_1.png")
    assert RENDERED_MEDIA_FILENAME_PATTERN.match("math_1.png")

    # 確認 `_split_table_pipes` 對 $|x|$ 不誤切欄，且對 $100 | $200 正常分欄
    abs_cells = [
        strip_cell_markdown(format_latex_in_text(c, escape_markdown=False, allow_multiline_block=False))
        for c in _split_table_pipes(" 絕對值 | $|x|$ | **$a_{max}$** | $b_{foo}$ ")
    ]
    assert abs_cells == ["絕對值", "|x|", "aₘₐₓ", "b_(foo)"]

    money_cells = [c.strip() for c in _split_table_pipes(" $100 | $200 ")]
    assert money_cells == ["$100", "$200"]

    # 測試 `$100 | $200` 兩欄金額不因跨欄 $ 被吃掉（table_renderer 欄數為 2）
    money_table = (
        "| A | B |\n"
        "| --- | --- |\n"
        "| $100 | $200 |\n"
    )
    _, money_files = extract_and_render_markdown_tables(money_table)
    assert len(money_files) == 1
    for f in money_files:
        f.close()


def test_latex_renderer_png_and_extraction():
    """驗證 2D LaTeX 圖片排版引擎能渲染常見高階公式並正確嵌入 Discord Components V2。"""
    import discord
    import pytest

    Image = pytest.importorskip("PIL.Image", reason="Pillow is required for PNG image validation")

    formulas = [
        r"ax^2 + bx + c = 0",
        r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}",
        r"\sum_{i=1}^{n} \frac{1}{i^2} = \frac{\pi^2}{6}",
        r"\int_{-\infty}^{\infty} e^{-x^2}\,dx = \sqrt{\pi}",
        r"f(x) = \begin{cases} x^2, & x \ge 0 \\ -x, & x < 0 \end{cases}",
        r"\begin{bmatrix} 1 & 2 \\ 3 & 4 \end{bmatrix}",
        r"\text{最大值 }\vec{v} = \frac{1}{2}\sqrt[3]{\frac{\alpha+\beta}{\gamma^2+1}}",
        r"\lim_{x \to 0} \frac{\sin x}{x} = \binom{n}{k}, \quad \ce{2H2 + O2 -> 2H2O}",
    ]
    for expr in formulas:
        buf = render_latex_to_image(expr)
        raw = buf.getvalue()
        assert raw.startswith(b"\x89PNG\r\n\x1a\n"), f"公式未產出合法 PNG: {expr}"
        buf.seek(0)
        with Image.open(buf) as im:
            assert im.width >= 60 and im.height >= 36

    # 測試抽出顯示公式、標籤公式與段落內結構化分數，同時保護程式碼與金額
    reply = (
        "先看標準型：\n\n"
        "$$ax^2 + bx + c = 0$$\n\n"
        "- 求根公式：$x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}$\n"
        "另外若已知 $k = \\frac{1}{2}$ 且售價 $100 元（非公式），程式碼 `$$\\frac{1}{2}$$` 保留：\n"
        "```latex\n\\[ x^2 + y^2 = r^2 \\]\n```\n"
        "最後看分段函式：\n"
        "\\begin{cases} x^2 & x \\ge 0 \\\\ -x & x < 0 \\end{cases}"
    )
    rendered_text, math_files = extract_and_render_latex_formulas(reply, max_images=8)
    try:
        assert [f.filename for f in math_files] == ["math_1.png", "math_2.png", "math_3.png", "math_4.png"]
    finally:
        for f in math_files:
            f.close()

    assert "[[MATH_IMAGE:math_1.png]]" in rendered_text
    assert "[[MATH_IMAGE:math_2.png]]" in rendered_text
    assert "[[MATH_IMAGE:math_3.png]]" in rendered_text
    assert "[[MATH_IMAGE:math_4.png]]" in rendered_text
    assert "$100 元" in rendered_text
    assert "`$$\\frac{1}{2}$$`" in rendered_text
    assert "```latex\n\\[ x^2 + y^2 = r^2 \\]\n```" in rendered_text

    # max_images 上限測試
    capped_text, capped_files = extract_and_render_latex_formulas(reply, max_images=2)
    try:
        assert [f.filename for f in capped_files] == ["math_1.png", "math_2.png"]
    finally:
        for f in capped_files:
            f.close()
    assert "[[MATH_IMAGE:math_3.png]]" not in capped_text

    # 驗證 Discord Components V2 容器能把 TABLE_IMAGE 與 MATH_IMAGE 都轉成 ui.MediaGallery
    cog = AI.__new__(AI)

    async def _build_view():
        sample_out, sample_files = _prepare_ai_output(
            "| 名稱 | 式子 |\n| --- | --- |\n| 二次 | $x^2$ |\n\n$$x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}$$"
        )
        for sf in sample_files:
            sf.close()
        return cog._build_ai_components_v2_view(sample_out, footer_text="gemini-3.5-flash-lite | 1.00s")

    view = asyncio.run(_build_view())
    container = view.children[0]
    gallery_urls = [
        item.media.url
        for child in container.children
        if isinstance(child, discord.ui.MediaGallery)
        for item in child.items
    ]
    assert "attachment://table_1.png" in gallery_urls
    assert "attachment://math_1.png" in gallery_urls


if __name__ == "__main__":
    test_gemini_models_split_and_defaults()
    test_ai_command_provider_and_model_resolution()
    test_llm_router_defaults_and_hydrogen_routing()
    test_latex_expression_conversion()
    test_latex_in_text_blocks_currency_and_code_safety()
    test_table_and_ai_output_pipeline_with_latex()
    test_latex_renderer_png_and_extraction()
    print("✅ 所有 AI 模型與 LaTeX 格式化／圖片渲染測試皆通過！")
