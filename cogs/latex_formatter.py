"""將 AI 產出的 LaTeX 數學公式與化學式自動轉換為 Discord 易讀之 Unicode 與排版格式。"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


# --- Unicode 上標 / 下標對照表（僅採用各平台字型與 Unifont 完整支援之字元） ---

_SUPERSCRIPT_MAP: Dict[str, str] = {
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
    "+": "⁺",
    "-": "⁻",
    "−": "⁻",
    "=": "⁼",
    "(": "⁽",
    ")": "⁾",
    "n": "ⁿ",
    "i": "ⁱ",
}

_SUBSCRIPT_MAP: Dict[str, str] = {
    "0": "₀",
    "1": "₁",
    "2": "₂",
    "3": "₃",
    "4": "₄",
    "5": "₅",
    "6": "₆",
    "7": "₇",
    "8": "₈",
    "9": "₉",
    "+": "₊",
    "-": "₋",
    "−": "₋",
    "=": "₌",
    "(": "₍",
    ")": "₎",
    "a": "ₐ",
    "e": "ₑ",
    "h": "ₕ",
    "k": "ₖ",
    "l": "ₗ",
    "m": "ₘ",
    "n": "ₙ",
    "o": "ₒ",
    "p": "ₚ",
    "s": "ₛ",
    "t": "ₜ",
    "x": "ₓ",
}

_VULGAR_FRACTIONS: Dict[Tuple[str, str], str] = {
    ("1", "2"): "½",
    ("1", "3"): "⅓",
    ("2", "3"): "⅔",
    ("1", "4"): "¼",
    ("3", "4"): "¾",
    ("1", "5"): "⅕",
    ("2", "5"): "⅖",
    ("3", "5"): "⅗",
    ("4", "5"): "⅘",
    ("1", "6"): "⅙",
    ("5", "6"): "⅚",
    ("1", "7"): "⅐",
    ("1", "8"): "⅛",
    ("3", "8"): "⅜",
    ("5", "8"): "⅝",
    ("7", "8"): "⅞",
    ("1", "9"): "⅑",
    ("1", "10"): "⅒",
}

_BLACKBOARD_BOLD: Dict[str, str] = {
    "R": "ℝ",
    "N": "ℕ",
    "Z": "ℤ",
    "Q": "ℚ",
    "C": "ℂ",
    "H": "ℍ",
    "P": "ℙ",
}

_COMBINING_ACCENTS: Dict[str, str] = {
    "vec": "\u20d7",
    "overrightarrow": "\u20d7",
    "overleftarrow": "\u20d6",
    "bar": "\u0304",
    "overline": "\u0304",
    "hat": "\u0302",
    "widehat": "\u0302",
    "tilde": "\u0303",
    "widetilde": "\u0303",
    "dot": "\u0307",
    "ddot": "\u0308",
}

_TEXT_WRAPPER_COMMANDS = {
    "text",
    "textrm",
    "textup",
    "textsf",
    "texttt",
    "textbf",
    "textit",
    "mbox",
    "hbox",
    "mathrm",
    "mathbf",
    "mathit",
    "mathsf",
    "mathtt",
    "mathcal",
    "mathscr",
    "mathfrak",
    "boldsymbol",
    "bm",
    "operatorname",
    "operatorname*",
    "limits",
    "nolimits",
}

_FUNCTION_COMMANDS = {
    "sin",
    "cos",
    "tan",
    "cot",
    "sec",
    "csc",
    "arcsin",
    "arccos",
    "arctan",
    "sinh",
    "cosh",
    "tanh",
    "coth",
    "log",
    "ln",
    "lg",
    "exp",
    "lim",
    "limsup",
    "liminf",
    "sup",
    "inf",
    "max",
    "min",
    "argmax",
    "argmin",
    "arg",
    "det",
    "dim",
    "ker",
    "deg",
    "gcd",
    "lcm",
    "Pr",
    "mod",
    "bmod",
}

_SIZE_PREFIX_COMMANDS = {
    "left",
    "right",
    "middle",
    "big",
    "Big",
    "bigg",
    "Bigg",
    "bigl",
    "Bigl",
    "biggl",
    "Biggl",
    "bigr",
    "Bigr",
    "biggr",
    "Biggr",
    "bigm",
    "Bigm",
    "biggm",
    "Biggm",
    "displaystyle",
    "textstyle",
    "scriptstyle",
    "scriptscriptstyle",
}

_SPACE_COMMANDS: Dict[str, str] = {
    ",": " ",
    ":": " ",
    ";": " ",
    " ": " ",
    "~": " ",
    "!": "",
    "quad": "  ",
    "qquad": "    ",
    "enspace": " ",
    "thinspace": " ",
    "medspace": " ",
    "thickspace": " ",
}

# 單一符號 LaTeX 指令對照表
_SYMBOL_COMMANDS: Dict[str, str] = {
    # 希臘小寫字母
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "varepsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "vartheta": "ϑ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "varpi": "ϖ",
    "rho": "ρ",
    "varrho": "ρ",
    "sigma": "σ",
    "varsigma": "ς",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "φ",
    "varphi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    # 希臘大寫字母
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Upsilon": "Υ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
    # 特殊數學字母與常數
    "hbar": "ℏ",
    "ell": "ℓ",
    "imath": "i",
    "jmath": "j",
    "wp": "℘",
    "Re": "Re",
    "Im": "Im",
    "aleph": "ℵ",
    "infty": "∞",
    "emptyset": "∅",
    "varnothing": "∅",
    "partial": "∂",
    "nabla": "∇",
    # 二元運算符號
    "times": "×",
    "cdot": "·",
    "cdotp": "·",
    "div": "÷",
    "pm": "±",
    "mp": "∓",
    "ast": "∗",
    "star": "⋆",
    "circ": "∘",
    "bullet": "•",
    "oplus": "⊕",
    "ominus": "⊖",
    "otimes": "⊗",
    "oslash": "⊘",
    "odot": "⊙",
    "dagger": "†",
    "ddagger": "‡",
    "setminus": "\\",
    "smallsetminus": "\\",
    "triangleleft": "◁",
    "triangleright": "▷",
    "diamond": "◇",
    "wedge": "∧",
    "land": "∧",
    "vee": "∨",
    "lor": "∨",
    "neg": "¬",
    "lnot": "¬",
    "cap": "∩",
    "cup": "∪",
    "uplus": "⊎",
    "sqcap": "⊓",
    "sqcup": "⊔",
    # 關係運算符號
    "leq": "≤",
    "le": "≤",
    "geq": "≥",
    "ge": "≥",
    "neq": "≠",
    "ne": "≠",
    "approx": "≈",
    "equiv": "≡",
    "sim": "∼",
    "simeq": "≃",
    "cong": "≅",
    "ncong": "≇",
    "propto": "∝",
    "ll": "≪",
    "gg": "≫",
    "prec": "≺",
    "succ": "≻",
    "preceq": "⪯",
    "succeq": "⪰",
    "subset": "⊂",
    "supset": "⊃",
    "subseteq": "⊆",
    "supseteq": "⊇",
    "nsubseteq": "⊈",
    "nsupseteq": "⊉",
    "subsetneq": "⊊",
    "supsetneq": "⊋",
    "sqsubset": "⊏",
    "sqsupset": "⊐",
    "sqsubseteq": "⊑",
    "sqsupseteq": "⊒",
    "in": "∈",
    "notin": "∉",
    "ni": "∋",
    "models": "⊨",
    "vdash": "⊢",
    "dashv": "⊣",
    "perp": "⊥",
    "bot": "⊥",
    "top": "⊤",
    "parallel": "∥",
    "mid": "∣",
    "nmid": "∤",
    "asymp": "≍",
    "doteq": "≐",
    "triangleq": "≜",
    "coloneqq": "≔",
    "eqqcolon": "≕",
    # 箭頭
    "to": "→",
    "rightarrow": "→",
    "gets": "←",
    "leftarrow": "←",
    "leftrightarrow": "↔",
    "Rightarrow": "⇒",
    "implies": "⇒",
    "Leftarrow": "⇐",
    "Leftrightarrow": "⇔",
    "iff": "⇔",
    "mapsto": "↦",
    "longmapsto": "⟼",
    "longrightarrow": "⟶",
    "longleftarrow": "⟵",
    "longleftrightarrow": "⟷",
    "Longrightarrow": "⟹",
    "Longleftarrow": "⟸",
    "Longleftrightarrow": "⟺",
    "uparrow": "↑",
    "downarrow": "↓",
    "updownarrow": "↕",
    "Uparrow": "⇑",
    "Downarrow": "⇓",
    "nearrow": "↗",
    "searrow": "↘",
    "swarrow": "↙",
    "nwarrow": "↖",
    "hookrightarrow": "↪",
    "hookleftarrow": "↩",
    "rightleftharpoons": "⇌",
    # 大型運算與邏輯幾何
    "sum": "∑",
    "prod": "∏",
    "coprod": "∐",
    "int": "∫",
    "iint": "∬",
    "iiint": "∭",
    "oint": "∮",
    "bigcup": "⋃",
    "bigcap": "⋂",
    "bigoplus": "⨁",
    "bigotimes": "⨂",
    "bigvee": "⋁",
    "bigwedge": "⋀",
    "forall": "∀",
    "exists": "∃",
    "nexists": "∄",
    "therefore": "∴",
    "because": "∵",
    "angle": "∠",
    "measuredangle": "∡",
    "triangle": "△",
    "square": "□",
    "blacksquare": "■",
    "qed": "∎",
    "checkmark": "✓",
    "prime": "′",
    "degree": "°",
    # 省略號與括號跳脫
    "cdots": "⋯",
    "ldots": "…",
    "dots": "…",
    "dotsc": "…",
    "dotsb": "⋯",
    "dotsm": "⋯",
    "vdots": "⋮",
    "ddots": "⋱",
    "langle": "⟨",
    "rangle": "⟩",
    "lfloor": "⌊",
    "rfloor": "⌋",
    "lceil": "⌈",
    "rceil": "⌉",
    "lvert": "|",
    "rvert": "|",
    "lVert": "‖",
    "rVert": "‖",
    "|": "‖",
    "{": "{",
    "}": "}",
    "%": "%",
    "$": "$",
    "&": "&",
    "#": "#",
    "_": "_",
    # 忽略純排版指令
    "nonumber": "",
    "notag": "",
    "displaystyle": "",
    "textstyle": "",
    "allowbreak": "",
}

_BINARY_OP_CHARS = set("+=≠≈≡∼≃≅∝≤≥≪≫≺≻∈∉∋⊂⊃⊆⊇→←↔⇒⇐⇔↦⟶⟵⟷⟹⟸⟺±∓×÷∧∨∩∪⊕⊗∣")

_MATRIX_DELIMITERS: Dict[str, Tuple[Tuple[str, str], Tuple[str, str], Tuple[str, str], Tuple[str, str]]] = {
    # (單行左右, 多行頂左右, 多行中左右, 多行底左右)
    "pmatrix": (("(", ")"), ("⎛", "⎞"), ("⎜", "⎟"), ("⎝", "⎠")),
    "bmatrix": (("[", "]"), ("⎡", "⎤"), ("⎢", "⎥"), ("⎣", "⎦")),
    "Bmatrix": (("{", "}"), ("⎧", "⎫"), ("⎨", "⎬"), ("⎩", "⎭")),
    "vmatrix": (("|", "|"), ("|", "|"), ("|", "|"), ("|", "|")),
    "Vmatrix": (("‖", "‖"), ("‖", "‖"), ("‖", "‖"), ("‖", "‖")),
    "matrix": (("", ""), ("", ""), ("", ""), ("", "")),
    "smallmatrix": (("", ""), ("", ""), ("", ""), ("", "")),
}

_SUPPORTED_ENVIRONMENTS = {
    "equation",
    "equation*",
    "align",
    "align*",
    "aligned",
    "alignedat",
    "gather",
    "gather*",
    "gathered",
    "split",
    "multline",
    "multline*",
    "eqnarray",
    "eqnarray*",
    "cases",
    "dcases",
    "rcases",
    "pmatrix",
    "bmatrix",
    "Bmatrix",
    "vmatrix",
    "Vmatrix",
    "matrix",
    "smallmatrix",
    "array",
    "subarray",
}

_KNOWN_INLINE_WORDS = _FUNCTION_COMMANDS | {"and", "or", "if", "not", "mod", "otherwise", "true", "false", "nan", "inf", "dx", "dy", "dz", "dt", "dr", "ds", "du", "dv"}


def _char_display_width(ch: str) -> int:
    """估算字元於等寬終端機/Code Block 中的顯示寬度。"""
    code = ord(ch)
    if (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
        or 0xFE10 <= code <= 0xFE6F
        or 0xFF01 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
    ):
        return 2
    return 1


def _display_width(text: str) -> int:
    """計算字串等寬顯示總寬度。"""
    return sum(_char_display_width(ch) for ch in text)


def _pad_right(text: str, target_width: int) -> str:
    """依等寬顯示寬度向右補齊空白。"""
    return text + " " * max(0, target_width - _display_width(text))


def _pad_center(text: str, target_width: int) -> str:
    """依等寬顯示寬度置中補齊空白。"""
    gap = max(0, target_width - _display_width(text))
    left = gap // 2
    right = gap - left
    return (" " * left) + text + (" " * right)


def _find_matching_brace(src: str, open_idx: int) -> int:
    """從 `{` 位置往後找尋對應的 `}` 索引，找不到時回傳字串結尾。"""
    depth = 0
    i = open_idx
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _find_matching_bracket(src: str, open_idx: int) -> int:
    """從 `[` 位置往後找尋對應的 `]` 索引（需同時考量 `{}` 平衡）。"""
    brace_depth = 0
    i = open_idx + 1
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)
        elif ch == "]" and brace_depth == 0:
            return i
        i += 1
    return -1


def _split_top_level(src: str, sep: str) -> List[str]:
    r"""在頂層（未進入 `{...}` 與 `\begin...\end`）以指定分隔字串切割。"""
    parts: List[str] = []
    cur: List[str] = []
    brace_depth = 0
    env_depth = 0
    i = 0
    n = len(src)
    sep_len = len(sep)

    while i < n:
        if src.startswith("\\begin{", i):
            env_depth += 1
            cur.append("\\begin{")
            i += 7
            continue
        if src.startswith("\\end{", i):
            env_depth = max(0, env_depth - 1)
            cur.append("\\end{")
            i += 5
            continue
        ch = src[i]
        if ch == "\\" and i + 1 < n:
            if src.startswith(sep, i) and brace_depth == 0 and env_depth == 0:
                parts.append("".join(cur))
                cur = []
                i += sep_len
                continue
            cur.append(src[i : i + 2])
            i += 2
            continue
        if ch == "{":
            brace_depth += 1
            cur.append(ch)
            i += 1
            continue
        if ch == "}":
            brace_depth = max(0, brace_depth - 1)
            cur.append(ch)
            i += 1
            continue
        if brace_depth == 0 and env_depth == 0 and src.startswith(sep, i):
            parts.append("".join(cur))
            cur = []
            i += sep_len
            continue
        cur.append(ch)
        i += 1

    parts.append("".join(cur))
    return parts


def _split_top_level_over(src: str) -> Optional[Tuple[str, str]]:
    r"""檢查頂層是否含有 infix `\over` 或 `\choose`，有則回傳左右兩側。"""
    brace_depth = 0
    env_depth = 0
    i = 0
    n = len(src)
    while i < n:
        if src.startswith("\\begin{", i):
            env_depth += 1
            i += 7
            continue
        if src.startswith("\\end{", i):
            env_depth = max(0, env_depth - 1)
            i += 5
            continue
        ch = src[i]
        if ch == "{":
            brace_depth += 1
            i += 1
            continue
        if ch == "}":
            brace_depth = max(0, brace_depth - 1)
            i += 1
            continue
        if ch == "\\" and brace_depth == 0 and env_depth == 0:
            m = re.match(r"\\(over|choose)(?![A-Za-z])", src[i:])
            if m:
                left = src[:i]
                right = src[i + len(m.group(0)) :]
                return left, right
            i += 2
            continue
        i += 1
    return None


def _is_atomic_math(expr: str) -> bool:
    """判斷已格式化後的數學子式在分數/開方中是否無須額外加括號。"""
    s = expr.strip()
    if not s:
        return True
    if len(s) == 1:
        return True
    if re.fullmatch(r"[A-Za-zα-ωΑ-ΩπθφλμσΔΣΩ∞°′0-9.,]+", s):
        return True
    if s.startswith("(") and s.endswith(")"):
        depth = 0
        for idx, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and idx < len(s) - 1:
                    return False
        return True
    return False


def _is_single_script_atom(expr: str) -> bool:
    """判斷上/下標回退為 `^` 或 `_` 時是否為單一單元（避免 `x^{2k}` 誤寫成 `x^2k`）。"""
    s = expr.strip()
    if len(s) == 1:
        return True
    if re.fullmatch(r"(?:\d+(?:\.\d+)?|[A-Za-zα-ωΑ-ΩπθφλμσΔΣΩ][′°]?)", s):
        return True
    if s.startswith("(") and s.endswith(")") and _is_atomic_math(s):
        return True
    return False


def _wrap_if_compound(expr: str, *, allow_product_atomic: bool = True) -> str:
    """視需要為複合運算式加上括號（特別避免 1/(2a) 誤讀為 1/2a）。"""
    s = expr.strip()
    if not s:
        return s
    if not allow_product_atomic:
        if len(s) == 1 or re.fullmatch(r"(?:\d+(?:\.\d+)?|[A-Za-zα-ωΑ-Ω])", s):
            return s
        if s.startswith("(") and s.endswith(")") and _is_atomic_math(s):
            return s
        return f"({s})"
    if _is_atomic_math(s):
        return s
    return f"({s})"


def _to_unicode_script(text: str, mapping: Dict[str, str]) -> Optional[str]:
    """若字元皆可映至 Unicode 上/下標，回傳轉換結果；否則回傳 None 採全有或全無策略。"""
    s = text.strip()
    if not s:
        return None
    out: List[str] = []
    for ch in s:
        if ch in (" ", "\t"):
            continue
        mapped = mapping.get(ch)
        if mapped is None:
            return None
        out.append(mapped)
    return "".join(out) if out else None


def _format_chemistry(expr: str) -> str:
    r"""將簡易 `\ce{...}` 化學反應式轉為含上下標與反應箭頭之格式。"""
    s = expr.strip()
    s = s.replace("<=>", "⇌").replace("<->", "↔").replace("->", "→").replace("<-", "←")
    s = re.sub(r"\^\{([^}]+)\}", lambda m: _to_unicode_script(m.group(1), _SUPERSCRIPT_MAP) or f"^({m.group(1)})", s)
    s = re.sub(r"_\{([^}]+)\}", lambda m: _to_unicode_script(m.group(1), _SUBSCRIPT_MAP) or f"_({m.group(1)})", s)

    # 元素或右括號後的計數數字轉為下標：H2O -> H₂O, (SO4)2 -> (SO₄)₂
    def _chem_sub(match: re.Match) -> str:
        prefix, digits = match.group(1), match.group(2)
        sub = _to_unicode_script(digits, _SUBSCRIPT_MAP)
        return prefix + (sub or digits)

    s = re.sub(r"([A-Za-z\)])(\d+)", _chem_sub, s)
    # 離子電荷：2+、3-、+、-
    s = re.sub(
        r"([A-Za-z\)\u2080-\u2089])(\d*[+-])(?=\s|$|[→←↔⇌,])",
        lambda m: m.group(1) + (_to_unicode_script(m.group(2), _SUPERSCRIPT_MAP) or f"^({m.group(2)})"),
        s,
    )
    return re.sub(r"\s{2,}", " ", s).strip()


def _normalize_inline_spacing(text: str) -> str:
    """整理數學式空白（避免逗號前空一格、確保雙元運算子可讀性）。"""
    s = re.sub(r"[ \t]{2,}", " ", text).strip()
    # 標點與右括號前不留多餘空白
    s = re.sub(r"\s+([,.;:?!\)\]\}])", r"\1", s)
    # 左括號後不留多餘空白
    s = re.sub(r"([\(\[\{])\s+", r"\1", s)
    # 二元運算關係符號左右適度保留空格（排除正負號於開頭或左括號後之情況）
    s = re.sub(r"(?<=[^\s\(\[\{,=<>+\-*/^_])([=≠≈≡∼≃≅∝≤≥≪≫→←↔⇒⇐⇔↦])", r" \1", s)
    s = re.sub(r"([=≠≈≡∼≃≅∝≤≥≪≫→←↔⇒⇐⇔↦])(?=[^\s\)\]\}.,;:?!])", r"\1 ", s)
    s = re.sub(r"(?<=[^\s\(\[\{=≠≈≤≥→←↔⇒⇐⇔±×÷+*/^_])\s*([+±∓×÷])\s*(?=[^\s])", r" \1 ", s)
    # 一元與二元減號區分：前為字母/數字/右括號/上標時為二元減號，留空白
    s = re.sub(
        r"(?<=[A-Za-z0-9\)\]\}α-ωΑ-Ωπ∞⁰¹²³⁴⁵⁶⁷⁸⁹ⁿⁱ₀₁₂₃₄₅₆₇₈₉ₐ-ₓ])\s*-\s*(?=[A-Za-z0-9\(\[\{α-ωΑ-Ωπ∞√])",
        " - ",
        s,
    )
    # 逗號後補空白（避免 1,000 誤拆時只處理前不緊接數字與後為字母或空白後無字的情況）
    s = re.sub(r",(?=[A-Za-zα-ωΑ-Ω(\[√∑∏∫])", ", ", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


class _TeXConverter:
    """遞迴下降解析並格式化單一 TeX 數學片段。"""

    def __init__(self, *, in_script: bool = False, multiline: bool = True) -> None:
        self.in_script = in_script
        self.multiline = multiline

    def convert(self, expr: str) -> str:
        """解析並轉換整個 TeX 運算式。"""
        stripped = expr.strip()
        if not stripped:
            return ""
        # 處理頂層 infix \over / \choose
        infix = _split_top_level_over(stripped)
        if infix is not None:
            left_raw, right_raw = infix
            cmd_match = re.search(r"\\(over|choose)(?![A-Za-z])", stripped)
            cmd = cmd_match.group(1) if cmd_match else "over"
            if cmd == "over":
                return self._format_fraction(left_raw, right_raw)
            return self._format_binom(left_raw, right_raw)

        out = self._parse_sequence(stripped)
        if "\n" in out:
            return "\n".join(line.rstrip() for line in out.splitlines())
        return _normalize_inline_spacing(out)

    def _parse_sequence(self, src: str) -> str:
        """由左至右掃描 TeX 字串並組裝輸出。"""
        tokens: List[str] = []
        i = 0
        n = len(src)
        while i < n:
            ch = src[i]
            if ch in ("\r", "\n", "\t"):
                if not tokens or not tokens[-1].endswith(" "):
                    tokens.append(" ")
                i += 1
                continue
            if ch == " ":
                if not tokens or not tokens[-1].endswith(" "):
                    tokens.append(" ")
                i += 1
                continue
            if ch == "&":
                tokens.append(" & ")
                i += 1
                continue
            if ch == "\\" and i + 1 < n and src[i + 1] == "\\":
                # 跳過可選行距參數 \\[6pt]
                i += 2
                if i < n and src[i] == "[":
                    end_br = _find_matching_bracket(src, i)
                    if end_br != -1:
                        i = end_br + 1
                tokens.append("\n" if self.multiline else "; ")
                continue
            if ch == "{":
                end_b = _find_matching_brace(src, i)
                inner = src[i + 1 : end_b]
                infix = _split_top_level_over(inner)
                if infix is not None:
                    l_raw, r_raw = infix
                    if "\\choose" in inner:
                        tokens.append(self._format_binom(l_raw, r_raw))
                    else:
                        tokens.append(self._format_fraction(l_raw, r_raw))
                else:
                    tokens.append(self.convert_sub(inner, in_script=self.in_script))
                i = end_b + 1 if end_b < n else n
                continue
            if ch in ("^", "_"):
                is_sup = ch == "^"
                i += 1
                while i < n and src[i].isspace():
                    i += 1
                arg_raw, next_i = self._consume_arg(src, i)
                i = next_i
                tokens.append(self._format_script(arg_raw, is_sup=is_sup))
                continue
            if ch == "\\":
                piece, next_i = self._parse_command(src, i)
                tokens.append(piece)
                i = next_i
                continue
            tokens.append(ch)
            i += 1
        return "".join(tokens)

    def convert_sub(self, src: str, *, in_script: bool) -> str:
        """以指定上下標狀態轉換子運算式。"""
        sub = _TeXConverter(in_script=in_script, multiline=False)
        return sub.convert(src)

    def _consume_arg(self, src: str, start: int) -> Tuple[str, int]:
        r"""讀取單一 TeX 參數（`{...}`、`\cmd` 或單一字元）。"""
        n = len(src)
        while start < n and src[start].isspace():
            start += 1
        if start >= n:
            return "", n
        if src[start] == "{":
            end_b = _find_matching_brace(src, start)
            return src[start + 1 : end_b], (end_b + 1 if end_b < n else n)
        if src[start] == "\\":
            if start + 1 < n and not src[start + 1].isalpha():
                return src[start : start + 2], start + 2
            j = start + 1
            while j < n and src[j].isalpha():
                j += 1
            return src[start:j], j
        return src[start], start + 1

    def _consume_optional_bracket(self, src: str, start: int) -> Tuple[Optional[str], int]:
        """讀取可選中括號參數 `[...]`。"""
        n = len(src)
        pos = start
        while pos < n and src[pos].isspace():
            pos += 1
        if pos < n and src[pos] == "[":
            end_br = _find_matching_bracket(src, pos)
            if end_br != -1:
                return src[pos + 1 : end_br], end_br + 1
        return None, start

    def _parse_command(self, src: str, start: int) -> Tuple[str, int]:
        r"""解析並轉換以 `\` 開頭的 TeX 指令或環境。"""
        n = len(src)
        if start + 1 >= n:
            return "\\", start + 1

        next_ch = src[start + 1]
        if not next_ch.isalpha():
            i = start + 2
            if next_ch in _SPACE_COMMANDS:
                return _SPACE_COMMANDS[next_ch], i
            if next_ch in _SYMBOL_COMMANDS:
                return _SYMBOL_COMMANDS[next_ch], i
            if next_ch == ".":
                return "", i
            return next_ch, i

        j = start + 1
        while j < n and src[j].isalpha():
            j += 1
        # 支援含星號變體如 \operatorname*
        if j < n and src[j] == "*" and src[start + 1 : j] in ("operatorname", "xrightarrow", "xleftarrow"):
            j += 1
        cmd = src[start + 1 : j]

        if cmd == "begin":
            return self._parse_environment(src, j)
        if cmd in ("frac", "dfrac", "tfrac", "cfrac"):
            num_raw, pos = self._consume_arg(src, j)
            den_raw, pos = self._consume_arg(src, pos)
            return self._format_fraction(num_raw, den_raw), pos
        if cmd in ("binom", "tbinom", "dbinom"):
            top_raw, pos = self._consume_arg(src, j)
            bot_raw, pos = self._consume_arg(src, pos)
            return self._format_binom(top_raw, bot_raw), pos
        if cmd == "sqrt":
            degree_raw, pos = self._consume_optional_bracket(src, j)
            rad_raw, pos = self._consume_arg(src, pos)
            return self._format_sqrt(rad_raw, degree_raw), pos
        if cmd == "ce":
            chem_raw, pos = self._consume_arg(src, j)
            return _format_chemistry(chem_raw), pos
        if cmd == "mathbb":
            arg_raw, pos = self._consume_arg(src, j)
            mapped = "".join(_BLACKBOARD_BOLD.get(ch, ch) for ch in arg_raw.strip())
            return mapped, pos
        if cmd in ("xrightarrow", "xleftarrow", "xrightarrow*", "xleftarrow*"):
            _below, pos = self._consume_optional_bracket(src, j)
            above_raw, pos = self._consume_arg(src, pos)
            arrow = "←" if "left" in cmd else "→"
            above = self.convert_sub(above_raw, in_script=True)
            return f" {arrow}({above}) " if above else f" {arrow} ", pos
        if cmd in ("stackrel", "overset", "underset"):
            first_raw, pos = self._consume_arg(src, j)
            second_raw, pos = self._consume_arg(src, pos)
            first = self.convert_sub(first_raw, in_script=True)
            second = self.convert_sub(second_raw, in_script=True)
            if cmd == "underset":
                return f"{second}({first})" if first else second, pos
            if second in ("=", "→", "←", "↔", "⇒", "⇔", "≈"):
                return f" {second}({first}) " if first else f" {second} ", pos
            return f"{second}({first})" if first else second, pos
        if cmd in _COMBINING_ACCENTS:
            arg_raw, pos = self._consume_arg(src, j)
            return self._format_accent(cmd, arg_raw), pos
        if cmd == "underline":
            arg_raw, pos = self._consume_arg(src, j)
            return self.convert_sub(arg_raw, in_script=self.in_script), pos
        if cmd == "phantom" or cmd == "hphantom" or cmd == "vphantom" or cmd == "label":
            _arg, pos = self._consume_arg(src, j)
            return "", pos
        if cmd in _TEXT_WRAPPER_COMMANDS:
            arg_raw, pos = self._consume_arg(src, j)
            if cmd in ("text", "textrm", "textup", "textsf", "texttt", "textbf", "textit", "mbox", "hbox"):
                return arg_raw, pos
            return self.convert_sub(arg_raw, in_script=self.in_script), pos
        if cmd in _SIZE_PREFIX_COMMANDS:
            # 處理 \left. 與 \right. 的隱藏邊界
            pos = j
            while pos < n and src[pos].isspace():
                pos += 1
            if pos < n and src[pos] == ".":
                return "", pos + 1
            return "", j
        if cmd in _SPACE_COMMANDS:
            return _SPACE_COMMANDS[cmd], j
        if cmd in _FUNCTION_COMMANDS:
            # 函式前後保留可讀邊界，避免 sinx 黏在一起
            suffix = " " if (j < n and src[j] not in ("(", "[", "{", "^", "_", " ", "\n", ",", ")", "]")) else ""
            prefix = " " if j > 0 else ""
            return f"{prefix}{cmd}{suffix}", j
        if cmd in _SYMBOL_COMMANDS:
            sym = _SYMBOL_COMMANDS[cmd]
            # 希臘字母等單一識別字後若直接接英文字母（如 \pi r^2），可視需求保留清晰間隔
            suffix = " " if (sym and sym.isalpha() and j < n and src[j].isalpha()) else ""
            return sym + suffix, j
        # 未收錄但純裝飾性指令（如 \hline、\color{red}）
        if cmd == "hline":
            return "", j
        if cmd in ("color", "textcolor"):
            _clr, pos = self._consume_arg(src, j)
            if cmd == "textcolor":
                txt_raw, pos = self._consume_arg(src, pos)
                return self.convert_sub(txt_raw, in_script=self.in_script), pos
            return "", pos
        if cmd == "not":
            # 處理 \not= -> ≠、\not\in -> ∉
            pos = j
            while pos < n and src[pos].isspace():
                pos += 1
            if pos < n and src[pos] == "=":
                return "≠", pos + 1
            if src.startswith("\\in", pos):
                return "∉", pos + 3
            if src.startswith("\\equiv", pos):
                return "≢", pos + 6
            return "¬", j
        # 其餘未知指令保留指令名稱以便除錯閱讀
        return cmd, j

    def _format_fraction(self, num_raw: str, den_raw: str) -> str:
        """格式化分數（支援 Unicode 常用分數與雙重防誤讀括號）。"""
        num = self.convert_sub(num_raw, in_script=True).strip()
        den = self.convert_sub(den_raw, in_script=True).strip()
        if not num and not den:
            return ""
        if not self.in_script and (num, den) in _VULGAR_FRACTIONS:
            return _VULGAR_FRACTIONS[(num, den)]
        num_fmt = _wrap_if_compound(num, allow_product_atomic=True)
        den_fmt = _wrap_if_compound(den, allow_product_atomic=False)
        return f"{num_fmt}/{den_fmt}"

    def _format_binom(self, top_raw: str, bot_raw: str) -> str:
        """格式化二項式係數為 `C(n, k)`。"""
        top = self.convert_sub(top_raw, in_script=True).strip()
        bot = self.convert_sub(bot_raw, in_script=True).strip()
        return f"C({top}, {bot})"

    def _format_sqrt(self, rad_raw: str, degree_raw: Optional[str]) -> str:
        """格式化平方根與高次方根。"""
        rad = self.convert_sub(rad_raw, in_script=False).strip()
        rad_fmt = rad if _is_atomic_math(rad) else f"({rad})"
        if not degree_raw:
            return f"√{rad_fmt}"
        deg = self.convert_sub(degree_raw, in_script=True).strip()
        if deg == "3":
            return f"∛{rad_fmt}"
        if deg == "4":
            return f"∜{rad_fmt}"
        sup_deg = _to_unicode_script(deg, _SUPERSCRIPT_MAP)
        if sup_deg:
            return f"{sup_deg}√{rad_fmt}"
        return f"^({deg})√{rad_fmt}"

    def _format_accent(self, cmd: str, arg_raw: str) -> str:
        """格式化向量/平均值/估計帽等上標記號。"""
        inner = self.convert_sub(arg_raw, in_script=self.in_script).strip()
        if len(inner) == 1 and cmd in _COMBINING_ACCENTS:
            return inner + _COMBINING_ACCENTS[cmd]
        label = "vec" if "vec" in cmd else ("bar" if "bar" in cmd or "line" in cmd else cmd.lstrip("wide"))
        return f"{label}({inner})"

    def _format_script(self, arg_raw: str, *, is_sup: bool) -> str:
        """格式化上標 `^` 與下標 `_`（單一可轉換字元優先採用 Unicode 上下標）。"""
        stripped_raw = arg_raw.strip()
        if is_sup and stripped_raw in ("\\circ", "\\degree"):
            return "°"
        if is_sup and stripped_raw == "\\prime":
            return "′"

        converted = self.convert_sub(stripped_raw, in_script=True).strip()
        if not converted:
            return ""
        mapping = _SUPERSCRIPT_MAP if is_sup else _SUBSCRIPT_MAP
        unicode_script = _to_unicode_script(converted, mapping)
        if unicode_script is not None:
            return unicode_script

        marker = "^" if is_sup else "_"
        if _is_single_script_atom(converted):
            return f"{marker}{converted}"
        return f"{marker}({converted})"

    def _parse_environment(self, src: str, after_begin_idx: int) -> Tuple[str, int]:
        r"""解析並格式化 `\begin{env} ... \end{env}` 巢狀結構。"""
        env_raw, pos = self._consume_arg(src, after_begin_idx)
        env_name = env_raw.strip()
        end_tag = f"\\end{{{env_name}}}"

        # 尋找配對的 \end{env}
        search_pos = pos
        depth = 1
        end_idx = -1
        begin_tag = f"\\begin{{{env_name}}}"
        while search_pos < len(src):
            next_begin = src.find(begin_tag, search_pos)
            next_end = src.find(end_tag, search_pos)
            if next_end == -1:
                break
            if next_begin != -1 and next_begin < next_end:
                depth += 1
                search_pos = next_begin + len(begin_tag)
            else:
                depth -= 1
                if depth == 0:
                    end_idx = next_end
                    break
                search_pos = next_end + len(end_tag)

        if end_idx == -1:
            return env_name, pos

        body = src[pos:end_idx]
        after_end = end_idx + len(end_tag)

        # array 環境帶有必選欄寬規格參數 {ccc}
        if env_name in ("array", "subarray") and body.lstrip().startswith("{"):
            l_stripped = body.lstrip()
            close_spec = _find_matching_brace(l_stripped, 0)
            if close_spec < len(l_stripped):
                body = l_stripped[close_spec + 1 :]

        formatted = self._format_environment_body(env_name, body)
        return formatted, after_end

    def _split_env_rows(self, body: str) -> List[str]:
        """切割環境內部由 `\\` 分隔的各列，並略過空列與可選 `[spacing]`。"""
        # 將 \\[6pt] 正規化為 \\ 便於頂層切割
        normalized = re.sub(r"\\\\\s*\[[^\]]*\]", r"\\\\", body)
        raw_rows = _split_top_level(normalized, "\\\\")
        rows: List[str] = []
        for row in raw_rows:
            cleaned = re.sub(r"\\hline\b", "", row).strip()
            if cleaned:
                rows.append(cleaned)
        return rows

    def _format_environment_body(self, env_name: str, body: str) -> str:
        """依據環境類型轉換矩陣、分段函數或多行對齊算式。"""
        rows = self._split_env_rows(body)
        if not rows:
            return ""

        if env_name in _MATRIX_DELIMITERS or env_name in ("array", "subarray"):
            return self._format_matrix(env_name, rows)
        if env_name in ("cases", "dcases", "rcases"):
            return self._format_cases(env_name, rows)
        return self._format_aligned_rows(rows)

    def _format_matrix(self, env_name: str, rows: List[str]) -> str:
        """將矩陣/行列式對齊為等寬字型可用的多行括號區塊，或單行小矩陣。"""
        grid: List[List[str]] = []
        max_cols = 0
        for row in rows:
            cells = [_TeXConverter(multiline=False).convert(cell) for cell in _split_top_level(row, "&")]
            max_cols = max(max_cols, len(cells))
            grid.append(cells)

        # 內聯/單行表格模式時保持單行精簡表示
        if not self.multiline or len(grid) == 1:
            single, _, _, _ = _MATRIX_DELIMITERS.get(env_name, _MATRIX_DELIMITERS["matrix"])
            left, right = single if single[0] or len(grid) == 1 else ("[", "]")
            row_strs = [", ".join(r) for r in grid]
            inner = "; ".join(row_strs)
            return f"{left}{inner}{right}"

        col_widths = [0] * max_cols
        for r in grid:
            for idx, val in enumerate(r):
                col_widths[idx] = max(col_widths[idx], _display_width(val))

        single_d, top_d, mid_d, bot_d = _MATRIX_DELIMITERS.get(env_name, _MATRIX_DELIMITERS["matrix"])
        out_lines: List[str] = []
        total_rows = len(grid)
        for r_idx, r in enumerate(grid):
            padded_cells = [_pad_center(r[c_idx] if c_idx < len(r) else "", col_widths[c_idx]) for c_idx in range(max_cols)]
            content = "  ".join(padded_cells).rstrip()
            if total_rows == 1:
                ld, rd = single_d
            elif r_idx == 0:
                ld, rd = top_d
            elif r_idx == total_rows - 1:
                ld, rd = bot_d
            else:
                ld, rd = mid_d
            if ld or rd:
                out_lines.append(f"{ld} {content} {rd}".rstrip())
            else:
                out_lines.append(content)
        return "\n".join(out_lines)

    def _format_cases(self, env_name: str, rows: List[str]) -> str:
        """將 `cases` 分段函數對齊為帶左側大括號的多行結構。"""
        parsed_rows: List[Tuple[str, str]] = []
        max_expr_w = 0
        for row in rows:
            cols = _split_top_level(row, "&")
            expr_part = _TeXConverter(multiline=False).convert(cols[0])
            cond_part = _TeXConverter(multiline=False).convert(cols[1]) if len(cols) > 1 else ""
            max_expr_w = max(max_expr_w, _display_width(expr_part))
            parsed_rows.append((expr_part, cond_part))

        if not self.multiline:
            joined = "; ".join(f"{e}, {c}" if c else e for e, c in parsed_rows)
            return f"{{ {joined} }}"

        total = len(parsed_rows)
        if total == 1:
            e, c = parsed_rows[0]
            return f"{{ {e}" + (f" ,  {c}" if c else "")

        mid_idx = total // 2
        lines: List[str] = []
        for idx, (e, c) in enumerate(parsed_rows):
            if env_name == "rcases":
                prefix = ""
                suffix = " ⎫" if idx == 0 else (" ⎭" if idx == total - 1 else (" ⎬" if idx == mid_idx else " ⎪"))
            else:
                if total == 2:
                    prefix = "⎧ " if idx == 0 else "⎩ "
                else:
                    prefix = "⎧ " if idx == 0 else ("⎩ " if idx == total - 1 else ("⎨ " if idx == mid_idx else "⎪ "))
                suffix = ""
            if c:
                line = f"{prefix}{_pad_right(e, max_expr_w)}  ,  {c}{suffix}"
            else:
                line = f"{prefix}{e}{suffix}"
            lines.append(line.rstrip())
        return "\n".join(lines)

    def _format_aligned_rows(self, rows: List[str]) -> str:
        """格式化 `aligned`、`align`、`gather`、`equation` 等多行算式。"""
        formatted_lines: List[str] = []
        for row in rows:
            cols = _split_top_level(row, "&")
            if len(cols) == 1:
                line = _TeXConverter(multiline=False).convert(cols[0])
            else:
                parts = [_TeXConverter(multiline=False).convert(col) for col in cols]
                # 常見 `&= ...` 寫法中第一個欄常為空字串，或右欄以關係符號開頭
                line = _normalize_inline_spacing(" ".join(p for p in parts if p))
            if line:
                formatted_lines.append(line)
        joiner = "\n" if self.multiline else "; "
        return joiner.join(formatted_lines)


def format_latex_expression(expr: str, *, multiline: bool = True) -> str:
    """將單一 LaTeX 數學內容（不含外層定界符）轉為可讀 Unicode 文字。"""
    return _TeXConverter(multiline=multiline).convert(expr)


def _escape_discord_math_markdown(text: str) -> str:
    """跳脫轉換後數學式中可能被 Discord 誤判為斜體/刪除線的 ASCII `_`、`*`、`~`。"""
    out: List[str] = []
    for idx, ch in enumerate(text):
        if ch in ("_", "*", "~") and (idx == 0 or text[idx - 1] != "\\"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _looks_like_inline_latex(content: str, after_char: str) -> bool:
    """嚴格區分 `$...$` 行內數學公式與常見金額寫法（例如 `$100`、`Costs $10 to $20`）。"""
    if not content or "\n" in content or "\r" in content:
        return False
    if content[0].isspace() or content[-1].isspace():
        return False
    if after_char.isdigit():
        return False
    if re.fullmatch(r"\d[\d,]*(?:\.\d+)?(?:\s*[kKmMbB%])?", content):
        return False

    has_strong_tex = bool(
        re.search(r"(?:\\[A-Za-z]+|[_^{}]|[=≠≈<>≤≥±×÷·∈∉⊂⊃⊆⊇∪∩∀∃∂∇∫∑∏√∞])", content)
    )
    if has_strong_tex:
        # 避免跨多個英文單字的金額句被偶爾出現的數學符號誤判
        if re.match(r"^\d[\d,.\s]+(?:[a-zA-Z]{2,}\s+){2,}", content):
            return False
        return True

    if len(content) > 48:
        return False
    if not re.fullmatch(r"[A-Za-z0-9α-ωΑ-ΩπθφλμσΔΣΩ∞∂∇()\[\]|.,+\-*/=<>!'′\s]+", content):
        return False

    # 拒絕含非數學英文普通單字（如 "$10 to $20"、"$5 per item$"）
    words = re.findall(r"[A-Za-z]{2,}", content)
    for word in words:
        if word.lower() not in _KNOWN_INLINE_WORDS:
            return False

    # 純數字開頭且尾端無數學結構（如 "$10-"）不視為公式
    if content[0].isdigit() and content[-1] in ("+", "-", "*", "/", "="):
        return False
    return True


def _find_unescaped(src: str, needle: str, start: int) -> int:
    """從 start 起尋找未跳脫之特定字串（須先比對本身以 backslash 開頭的 `\\)` / `\\]`）。"""
    i = start
    n = len(src)
    needle_len = len(needle)
    while i <= n - needle_len:
        if src.startswith(needle, i):
            return i
        if src[i] == "\\":
            i += 2
            continue
        i += 1
    return -1


def _find_inline_dollar_end(src: str, start: int) -> int:
    r"""在單行內尋找配對的結尾 `$`（跳過 `\$` 與 `$$`）。"""
    i = start
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\n":
            return -1
        if ch == "\\":
            i += 2
            continue
        if ch == "$":
            if i + 1 < n and src[i + 1] == "$":
                return -1
            return i
        i += 1
    return -1


def _match_standalone_environment(src: str, start: int) -> Optional[Tuple[int, str]]:
    r"""若 start 為獨立 `\begin{env}...\end{env}` 數學環境，回傳 `(結束索引, 環境內容)`。"""
    m = re.match(r"\\begin\{([A-Za-z]+\*?)\}", src[start:])
    if not m:
        return None
    env_name = m.group(1)
    if env_name not in _SUPPORTED_ENVIRONMENTS:
        return None

    begin_tag = f"\\begin{{{env_name}}}"
    end_tag = f"\\end{{{env_name}}}"
    pos = start + len(begin_tag)
    depth = 1
    while pos < len(src):
        nb = src.find(begin_tag, pos)
        ne = src.find(end_tag, pos)
        if ne == -1:
            return None
        if nb != -1 and nb < ne:
            depth += 1
            pos = nb + len(begin_tag)
        else:
            depth -= 1
            if depth == 0:
                end_pos = ne + len(end_tag)
                return end_pos, src[start:end_pos]
            pos = ne + len(end_tag)
    return None


def _is_isolated_on_line(src: str, span_start: int, span_end: int) -> bool:
    """檢查該片段前後至換行符間是否皆僅為空白字元（判斷是否可套用區塊排版）。"""
    line_start = src.rfind("\n", 0, span_start)
    before = src[line_start + 1 : span_start]
    line_end = src.find("\n", span_end)
    if line_end == -1:
        line_end = len(src)
    after = src[span_end:line_end]
    return not before.strip() and not after.strip()


def _render_display_math(
    expr: str,
    *,
    isolated_on_line: bool,
    escape_markdown: bool,
    allow_multiline_block: bool,
) -> str:
    """渲染顯示區塊數學公式（多行矩陣/分段函式使用 code block，單行置於引述強調）。"""
    formatted = format_latex_expression(expr, multiline=allow_multiline_block)
    if not formatted:
        return ""
    if "\n" in formatted:
        if not allow_multiline_block:
            flat = " ; ".join(line.strip() for line in formatted.splitlines() if line.strip())
            return _escape_discord_math_markdown(flat) if escape_markdown else flat
        # 多行算式／矩陣／cases 以等寬 code block 呈現以維持精確對齊，且不加前置空白免破壞 Markdown
        return f"```text\n{formatted}\n```"
    escaped = _escape_discord_math_markdown(formatted) if escape_markdown else formatted
    if allow_multiline_block and isolated_on_line:
        return f"> **{escaped}**"
    return f"**{escaped}**" if allow_multiline_block else escaped


def _format_prose_segment(
    segment: str,
    *,
    escape_markdown: bool,
    allow_multiline_block: bool,
) -> str:
    """格式化不含程式碼區塊的純文字區段中之所有 LaTeX 公式。"""
    out: List[str] = []
    i = 0
    n = len(segment)

    while i < n:
        # 1. $$ ... $$ 顯示公式
        if segment.startswith("$$", i) and (i == 0 or segment[i - 1] != "\\"):
            end = _find_unescaped(segment, "$$", i + 2)
            if end != -1:
                expr = segment[i + 2 : end]
                isolated = _is_isolated_on_line(segment, i, end + 2)
                out.append(
                    _render_display_math(
                        expr,
                        isolated_on_line=isolated,
                        escape_markdown=escape_markdown,
                        allow_multiline_block=allow_multiline_block,
                    )
                )
                i = end + 2
                continue

        # 2. \[ ... \] 顯示公式
        if segment.startswith("\\[", i) and (i == 0 or segment[i - 1] != "\\"):
            end = _find_unescaped(segment, "\\]", i + 2)
            if end != -1:
                expr = segment[i + 2 : end]
                isolated = _is_isolated_on_line(segment, i, end + 2)
                out.append(
                    _render_display_math(
                        expr,
                        isolated_on_line=isolated,
                        escape_markdown=escape_markdown,
                        allow_multiline_block=allow_multiline_block,
                    )
                )
                i = end + 2
                continue

        # 3. \( ... \) 行內公式
        if segment.startswith("\\(", i) and (i == 0 or segment[i - 1] != "\\"):
            end = _find_unescaped(segment, "\\)", i + 2)
            if end != -1:
                expr = segment[i + 2 : end]
                formatted = format_latex_expression(expr, multiline=False)
                out.append(_escape_discord_math_markdown(formatted) if escape_markdown else formatted)
                i = end + 2
                continue

        # 4. 獨立 \begin{env}...\end{env} 數學環境
        if segment.startswith("\\begin{", i) and (i == 0 or segment[i - 1] != "\\"):
            matched = _match_standalone_environment(segment, i)
            if matched is not None:
                end_pos, env_src = matched
                isolated = _is_isolated_on_line(segment, i, end_pos)
                out.append(
                    _render_display_math(
                        env_src,
                        isolated_on_line=isolated,
                        escape_markdown=escape_markdown,
                        allow_multiline_block=allow_multiline_block,
                    )
                )
                i = end_pos
                continue

        # 5. $ ... $ 行內公式（套用金額防誤判邏輯）
        if segment[i] == "$" and (i == 0 or segment[i - 1] != "\\"):
            end = _find_inline_dollar_end(segment, i + 1)
            if end != -1:
                content = segment[i + 1 : end]
                after_char = segment[end + 1] if end + 1 < n else ""
                if _looks_like_inline_latex(content, after_char):
                    formatted = format_latex_expression(content, multiline=False)
                    if formatted:
                        out.append(_escape_discord_math_markdown(formatted) if escape_markdown else formatted)
                        i = end + 1
                        continue

        out.append(segment[i])
        i += 1

    return "".join(out)


def _split_code_regions(text: str) -> List[Tuple[bool, str]]:
    """將文字切割為「受保護的程式碼區塊/行內反引號」與「一般文字區段」。"""
    segments: List[Tuple[bool, str]] = []
    # 同時辨識多反引號 fenced block (```...```)、~~~...~~~ 與行內反引號 `...`
    code_pattern = re.compile(
        r"(```[\s\S]*?```|~~~[\s\S]*?~~~|(`+)[^\n`]*?\2)",
        re.MULTILINE,
    )
    last_idx = 0
    for match in code_pattern.finditer(text):
        if match.start() > last_idx:
            segments.append((False, text[last_idx : match.start()]))
        segments.append((True, match.group(0)))
        last_idx = match.end()
    if last_idx < len(text):
        segments.append((False, text[last_idx:]))
    return segments


def format_latex_in_text(
    text: str,
    *,
    escape_markdown: bool = True,
    allow_multiline_block: bool = True,
) -> str:
    """自動將文字內的 LaTeX 語法轉為可讀 Unicode 與 Discord 格式，並保留程式碼與金額。

    參數：
        text: 原始模型回覆或表格單元格內容。
        escape_markdown: 若為 True，會跳脫數學轉換後產生的 `_`、`*`、`~`，避免 Discord 誤判格式。
        allow_multiline_block: 若為 True，多行矩陣/分段函式使用 code block，單行區塊公式使用引用強調；
            在表格單元格中應設為 False 以維持單行不破壞欄列結構。
    """
    if not text or not any(marker in text for marker in ("$", "\\(", "\\[", "\\begin{")):
        return text

    parts: List[str] = []
    for is_code, seg in _split_code_regions(text):
        if is_code:
            parts.append(seg)
        else:
            parts.append(
                _format_prose_segment(
                    seg,
                    escape_markdown=escape_markdown,
                    allow_multiline_block=allow_multiline_block,
                )
            )
    return "".join(parts)
