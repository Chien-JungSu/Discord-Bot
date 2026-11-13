r"""將 AI 產出之 LaTeX 數學與化學公式渲染為高解析度 Discord 深色卡片 PNG 圖片。

採純 Python + Pillow 實作 TeX AST 語法樹與二維方盒排版引擎 (Box-and-Glue Layout)：
支援堆疊分數、平方／高次方根、真實上下標與極限上下置中、大型運算子 (∑ ∏ ∫)、
自動伸縮大小括號／絕對值、矩陣／聯立方程 (`cases`)、對齊多行 (`aligned`) 及 CJK 中文混排。
"""

from __future__ import annotations

import functools
import io
import math
import os
import re
import struct
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import discord
from PIL import Image, ImageDraw, ImageFont

from cogs.latex_formatter import (
    _BLACKBOARD_BOLD,
    _FUNCTION_COMMANDS,
    _SPACE_COMMANDS,
    _SUPPORTED_ENVIRONMENTS,
    _SYMBOL_COMMANDS,
    _find_inline_dollar_end,
    _find_matching_brace,
    _find_matching_bracket,
    _find_unescaped,
    _format_chemistry,
    _looks_like_inline_latex,
    _match_standalone_environment,
    _split_code_regions,
    _split_top_level,
    _split_top_level_over,
    format_latex_expression,
)
from cogs.table_renderer import BUNDLED_FONT, UNIFONT_PATH


# ==============================================================================
# 字型載入與多字型回退 (Cambria Math / Cambria Italic / 中文 / Unifont)
# ==============================================================================

_BINARY_OPERATORS = set("+−±∓×·÷∗⋆∘•⊕⊖⊗⊘⊙∧∨∩∪")
_RELATION_OPERATORS = set("=≠≈≡∼≃≅∝≤≥≪≫≺≻⪯⪰∈∉∋⊂⊃⊆⊇⊈⊉⊊⊋⊏⊐⊑⊒→←↔⇒⇐⇔↦⟶⟵⟷⟹⟸⟺∣∤⊥∥⊢⊨≜≔≕")
_BIG_DISPLAY_OPERATORS = {"∑", "∏", "∐", "∫", "∬", "∭", "∮", "⋃", "⋂", "⨁", "⨂", "⋁", "⋀"}
_LIMIT_OP_COMMANDS = {
    "sum",
    "prod",
    "coprod",
    "bigcup",
    "bigcap",
    "bigoplus",
    "bigotimes",
    "bigvee",
    "bigwedge",
    "lim",
    "limsup",
    "liminf",
    "sup",
    "inf",
    "max",
    "min",
    "argmax",
    "argmin",
}
_INT_OP_COMMANDS = {"int", "iint", "iiint", "oint"}
_ACCENT_COMMANDS = {
    "vec",
    "overrightarrow",
    "overleftarrow",
    "bar",
    "overline",
    "underline",
    "hat",
    "widehat",
    "tilde",
    "widetilde",
    "dot",
    "ddot",
}
_STRUCTURAL_INLINE_RE = re.compile(
    r"(?:\\(?:frac|dfrac|tfrac|cfrac|sqrt|binom|tbinom|dbinom|sum|prod|coprod|"
    r"int|iint|iiint|oint|lim|limsup|liminf|vec|overrightarrow|overleftarrow|"
    r"overline|widehat|widetilde|begin|over|choose)(?![A-Za-z]))"
)
_TEXT_COMMANDS = {"text", "textrm", "textup", "textsf", "texttt", "mbox", "hbox"}
_ROMAN_COMMANDS = {"mathrm", "operatorname", "operatorname*", "mathsf", "mathtt"}
_BOLD_COMMANDS = {"mathbf", "textbf", "boldsymbol", "bm"}
_DELIM_ALIASES: Dict[str, str] = {
    "(": "(",
    ")": ")",
    "[": "[",
    "]": "]",
    "|": "|",
    "\\|": "‖",
    "\\lVert": "‖",
    "\\rVert": "‖",
    "\\lvert": "|",
    "\\rvert": "|",
    "\\{": "{",
    "\\}": "}",
    "\\langle": "⟨",
    "\\rangle": "⟩",
    "\\lfloor": "⌊",
    "\\rfloor": "⌋",
    "\\lceil": "⌈",
    "\\rceil": "⌉",
    ".": "",
}


@functools.lru_cache(maxsize=24)
def _load_font_cmap_with_index(font_path: str, index: int = 0) -> Set[int]:
    """解析 TTF/OTF/TTC 指定子字型索引的 cmap 碼位集合，支援 TTC 內多字體。"""
    if not font_path or not os.path.exists(font_path):
        return set()
    try:
        with open(font_path, "rb") as f:
            data = f.read()
        if data[:4] == b"ttcf":
            num_fonts = struct.unpack(">I", data[8:12])[0]
            safe_idx = min(index, max(0, num_fonts - 1))
            offset = struct.unpack(">I", data[12 + safe_idx * 4 : 16 + safe_idx * 4])[0]
        else:
            offset = 0
        num_tables = struct.unpack(">H", data[offset + 4 : offset + 6])[0]
        cmap_offset = None
        for i in range(num_tables):
            entry = offset + 12 + i * 16
            if data[entry : entry + 4] == b"cmap":
                cmap_offset = struct.unpack(">I", data[entry + 8 : entry + 12])[0]
                break
        if not cmap_offset:
            return set()
        num_subtables = struct.unpack(">H", data[cmap_offset + 2 : cmap_offset + 4])[0]
        supported: Set[int] = set()
        for i in range(num_subtables):
            sub = cmap_offset + 4 + i * 8
            _plat, _enc, sub_off = struct.unpack(">HHI", data[sub : sub + 8])
            st = cmap_offset + sub_off
            fmt = struct.unpack(">H", data[st : st + 2])[0]
            if fmt == 4:
                seg_count = struct.unpack(">H", data[st + 6 : st + 8])[0] // 2
                end_codes = struct.unpack(f">{seg_count}H", data[st + 14 : st + 14 + seg_count * 2])
                start_codes = struct.unpack(
                    f">{seg_count}H",
                    data[st + 16 + seg_count * 2 : st + 16 + seg_count * 4],
                )
                for s_c, e_c in zip(start_codes, end_codes):
                    if s_c <= e_c < 0xFFFF:
                        supported.update(range(s_c, e_c + 1))
            elif fmt == 12:
                num_groups = struct.unpack(">I", data[st + 12 : st + 16])[0]
                for g in range(num_groups):
                    s_c, e_c, _ = struct.unpack(">III", data[st + 16 + g * 12 : st + 28 + g * 12])
                    supported.update(range(s_c, e_c + 1))
        return supported
    except Exception:
        return set()


@functools.lru_cache(maxsize=64)
def _load_truetype_cached(path: str, size: int, index: int = 0) -> Optional[ImageFont.FreeTypeFont]:
    """快取載入 FreeTypeFont 實例避免重複磁碟 I/O。"""
    if not path or not os.path.exists(path):
        return None
    try:
        return ImageFont.truetype(path, size, index=index)
    except Exception:
        return None


class _MathFontRunSet:
    """依樣式挑選主要字型並提供缺字自動回退（含數學符號與繁體中文）。"""

    def __init__(self, entries: List[Tuple[Any, Set[int]]]):
        self.entries = entries

    def split_runs(self, text: str) -> List[Tuple[str, Any]]:
        """將混合字元依支援字型拆分為連續文字區段。"""
        if not text:
            return []
        if not self.entries:
            return [(text, ImageFont.load_default())]
        if len(self.entries) == 1:
            return [(text, self.entries[0][0])]

        runs: List[Tuple[str, Any]] = []
        cur_chars: List[str] = []
        cur_font: Optional[Any] = None

        for ch in text:
            if ch in (" ", "\t"):
                chosen = cur_font or self.entries[0][0]
            else:
                code = ord(ch)
                chosen = self.entries[0][0]
                for f_obj, cmap in self.entries:
                    if code in cmap:
                        chosen = f_obj
                        break
            if cur_font is None:
                cur_font = chosen
                cur_chars.append(ch)
            elif chosen == cur_font:
                cur_chars.append(ch)
            else:
                runs.append(("".join(cur_chars), cur_font))
                cur_chars = [ch]
                cur_font = chosen

        if cur_chars and cur_font is not None:
            runs.append(("".join(cur_chars), cur_font))
        return runs


def _build_font_candidates(style: str) -> List[Tuple[str, int]]:
    """依數學樣式傳回跨平台 (Windows / Linux) 優先字型路徑列表。"""
    cjk_paths = [
        ("C:/Windows/Fonts/msjh.ttc", 0),
        (BUNDLED_FONT, 0),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
        ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
        (UNIFONT_PATH, 0),
    ]
    math_paths = [
        ("C:/Windows/Fonts/cambria.ttc", 1),
        ("C:/Windows/Fonts/cambria.ttc", 0),
        ("C:/Windows/Fonts/seguisym.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", 0),
        ("/usr/share/fonts/truetype/freefont/FreeSerif.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ]
    if style == "italic":
        primary = [
            ("C:/Windows/Fonts/cambriai.ttf", 0),
            ("C:/Windows/Fonts/timesi.ttf", 0),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf", 0),
            ("/usr/share/fonts/truetype/freefont/FreeSerifItalic.ttf", 0),
        ] + math_paths + cjk_paths
    elif style == "bold":
        primary = [
            ("C:/Windows/Fonts/cambriab.ttf", 0),
            ("C:/Windows/Fonts/timesbd.ttf", 0),
            ("C:/Windows/Fonts/msjhbd.ttc", 0),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", 0),
            ("/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf", 0),
        ] + math_paths + cjk_paths
    elif style == "text":
        primary = [
            ("C:/Windows/Fonts/cambria.ttc", 0),
            ("C:/Windows/Fonts/segoeui.ttf", 0),
        ] + cjk_paths + math_paths
    else:  # upright / math symbol
        primary = math_paths + cjk_paths
    return primary


@functools.lru_cache(maxsize=128)
def _get_math_font_set(size: int, style: str = "math") -> _MathFontRunSet:
    """建置支援數學符號、斜體變數、粗體與 CJK 中文回退之字型集合。"""
    safe_size = max(14, int(round(size)))
    entries: List[Tuple[Any, Set[int]]] = []
    seen: Set[Tuple[str, int]] = set()
    for path, idx in _build_font_candidates(style):
        key = (path, idx)
        if key in seen or not path or not os.path.exists(path):
            continue
        font_obj = _load_truetype_cached(path, safe_size, idx)
        if font_obj is None:
            continue
        cmap = _load_font_cmap_with_index(path, idx)
        if cmap:
            entries.append((font_obj, cmap))
            seen.add(key)
        if len(entries) >= 4:
            break

    if not entries:
        entries.append((ImageFont.load_default(), set(range(128))))
    return _MathFontRunSet(entries)


# ==============================================================================
# TeX AST 節點與遞迴下降解析器
# ==============================================================================


class MathNode:
    """所有 TeX 數學抽象語法樹節點基底。"""


@dataclass
class AtomNode(MathNode):
    text: str
    kind: str = "var"  # var | number | roman | bold | text | binop | relop | bigop | punct


@dataclass
class SpaceNode(MathNode):
    em: float


@dataclass
class RowNode(MathNode):
    items: List[MathNode]


@dataclass
class FracNode(MathNode):
    num: MathNode
    den: MathNode
    bar: bool = True
    full_scale: bool = False


@dataclass
class SqrtNode(MathNode):
    radicand: MathNode
    degree: Optional[MathNode] = None


@dataclass
class ScriptNode(MathNode):
    base: MathNode
    sub: Optional[MathNode] = None
    sup: Optional[MathNode] = None
    limits: bool = False


@dataclass
class DelimNode(MathNode):
    left: str
    right: str
    body: MathNode


@dataclass
class AccentNode(MathNode):
    cmd: str
    body: MathNode


@dataclass
class MatrixNode(MathNode):
    rows: List[List[MathNode]]
    left_delim: str = ""
    right_delim: str = ""
    col_aligns: str = ""
    v_bars: Set[int] = None  # type: ignore[assignment]
    h_lines: Set[int] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.v_bars is None:
            self.v_bars = set()
        if self.h_lines is None:
            self.h_lines = set()


@dataclass
class LinesNode(MathNode):
    rows: List[List[MathNode]]
    aligned: bool = False


def _chemistry_to_node(expr: str) -> MathNode:
    r"""將 `\ce{...}` 化學方程式轉為帶正確下標數字與電荷上標之 AST 節點。"""
    s = _format_chemistry(expr)
    # 還原並使用正常數字節點配合上下標（因 Cambria Math 支援豐富但非所有字型都有完整上下標連字）
    sub_rev = {"₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4", "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9"}
    sup_rev = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁺": "+", "⁻": "−"}
    items: List[MathNode] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch in sub_rev:
            j = i
            buf = []
            while j < n and s[j] in sub_rev:
                buf.append(sub_rev[s[j]])
                j += 1
            base = items.pop() if items else AtomNode("", "roman")
            items.append(ScriptNode(base=base, sub=AtomNode("".join(buf), "number")))
            i = j
            continue
        if ch in sup_rev:
            j = i
            buf = []
            while j < n and s[j] in sup_rev:
                buf.append(sup_rev[s[j]])
                j += 1
            base = items.pop() if items else AtomNode("", "roman")
            if isinstance(base, ScriptNode) and base.sup is None:
                base.sup = AtomNode("".join(buf), "roman")
                items.append(base)
            else:
                items.append(ScriptNode(base=base, sup=AtomNode("".join(buf), "roman")))
            i = j
            continue
        if ch in ("→", "←", "↔", "⇌"):
            items.append(AtomNode(ch, "relop"))
        elif ch == "+":
            items.append(AtomNode("+", "binop"))
        elif ch == " ":
            items.append(SpaceNode(0.25))
        else:
            items.append(AtomNode(ch, "roman"))
        i += 1
    return RowNode(items)


class _TeXASTParser:
    """將 TeX 數學字串解析為結構化 `MathNode` 抽象語法樹。"""

    def parse(self, expr: str) -> MathNode:
        """解析頂層數學運算式（支援頂層 `\\\\` 多行換行與 `\\over`/`\\choose`）。"""
        src = expr.strip()
        if not src:
            return RowNode([])

        # 若最頂層含換行 \\，視為多行顯示算式
        normalized_nl = re.sub(r"\\\\\s*\[[^\]]*\]", r"\\\\", src)
        top_lines = _split_top_level(normalized_nl, "\\\\")
        if len(top_lines) > 1:
            rows: List[List[MathNode]] = []
            for ln in top_lines:
                cleaned = ln.strip()
                if cleaned:
                    rows.append([self.parse(cleaned)])
            if len(rows) > 1:
                return LinesNode(rows=rows, aligned=False)
            if len(rows) == 1:
                return rows[0][0]

        infix = _split_top_level_over(src)
        if infix is not None:
            left_s, right_s = infix
            cmd_m = re.search(r"\\(over|choose)(?![A-Za-z])", src)
            cmd = cmd_m.group(1) if cmd_m else "over"
            frac = FracNode(self.parse(left_s), self.parse(right_s), bar=(cmd == "over"))
            return frac if cmd == "over" else DelimNode("(", ")", frac)

        nodes, _ = self._parse_sequence(src, 0, stop_at_right=False)
        return nodes[0] if len(nodes) == 1 else RowNode(nodes)

    def _parse_sequence(
        self,
        src: str,
        start: int,
        *,
        stop_at_right: bool = False,
    ) -> Tuple[List[MathNode], int]:
        """循序解析一串 TeX 元素，並自動將緊隨的 `_`/`^` 結合成 `ScriptNode`。"""
        items: List[MathNode] = []
        i = start
        n = len(src)

        while i < n:
            ch = src[i]
            if ch.isspace():
                i += 1
                continue

            if stop_at_right and src.startswith("\\right", i) and (i + 6 == n or not src[i + 6].isalpha()):
                return items, i

            node, i = self._parse_atom(src, i)
            if node is None:
                continue

            # 檢查緊隨其後的一對或多個上下標 (_ 與 ^ 不限順序)
            sub_node: Optional[MathNode] = None
            sup_node: Optional[MathNode] = None
            explicit_limits: Optional[bool] = None

            while i < n:
                while i < n and src[i].isspace():
                    i += 1
                if src.startswith("\\limits", i) and (i + 7 == n or not src[i + 7].isalpha()):
                    explicit_limits = True
                    i += 7
                    continue
                if src.startswith("\\nolimits", i) and (i + 9 == n or not src[i + 9].isalpha()):
                    explicit_limits = False
                    i += 9
                    continue
                if i < n and src[i] in ("_", "^"):
                    is_sup = src[i] == "^"
                    i += 1
                    arg_node, i = self._parse_arg_node(src, i)
                    if is_sup:
                        sup_node = arg_node
                    else:
                        sub_node = arg_node
                    continue
                break

            if sub_node is not None or sup_node is not None:
                default_limits = isinstance(node, AtomNode) and node.kind == "bigop" and node.text not in ("∫", "∬", "∭", "∮")
                use_limits = default_limits if explicit_limits is None else explicit_limits
                items.append(ScriptNode(base=node, sub=sub_node, sup=sup_node, limits=use_limits))
            else:
                items.append(node)

        return items, i

    def _consume_raw_arg(self, src: str, start: int) -> Tuple[str, int]:
        """讀取單一原始 TeX 參數文字，供 `\\text`、`\\begin{...}` 等使用。"""
        n = len(src)
        pos = start
        while pos < n and src[pos].isspace():
            pos += 1
        if pos >= n:
            return "", n
        if src[pos] == "{":
            end_b = _find_matching_brace(src, pos)
            return src[pos + 1 : end_b], (end_b + 1 if end_b < n else n)
        if src[pos] == "\\":
            if pos + 1 < n and not src[pos + 1].isalpha():
                return src[pos : pos + 2], pos + 2
            j = pos + 1
            while j < n and src[j].isalpha():
                j += 1
            return src[pos:j], j
        return src[pos], pos + 1

    def _consume_optional_arg(self, src: str, start: int) -> Tuple[Optional[str], int]:
        """讀取可選中括號 `[...]` 原始文字。"""
        n = len(src)
        pos = start
        while pos < n and src[pos].isspace():
            pos += 1
        if pos < n and src[pos] == "[":
            end_br = _find_matching_bracket(src, pos)
            if end_br != -1:
                return src[pos + 1 : end_br], end_br + 1
        return None, start

    def _parse_arg_node(self, src: str, start: int) -> Tuple[MathNode, int]:
        """將緊接的參數轉為 `MathNode`（支援 `{...}`、單指令與單字元）。"""
        n = len(src)
        pos = start
        while pos < n and src[pos].isspace():
            pos += 1
        if pos >= n:
            return RowNode([]), n
        if src[pos] == "{":
            end_b = _find_matching_brace(src, pos)
            inner = src[pos + 1 : end_b]
            return self.parse(inner), (end_b + 1 if end_b < n else n)
        node, next_i = self._parse_atom(src, pos)
        return (node if node is not None else RowNode([])), next_i

    def _consume_delim_token(self, src: str, start: int) -> Tuple[str, int]:
        """讀取 `\\left` 或 `\\right` 後方緊接之定界符。"""
        n = len(src)
        pos = start
        while pos < n and src[pos].isspace():
            pos += 1
        if pos >= n:
            return "", pos
        if src[pos] == "\\":
            if pos + 1 < n and not src[pos + 1].isalpha():
                tok = src[pos : pos + 2]
                return _DELIM_ALIASES.get(tok, tok[1:]), pos + 2
            j = pos + 1
            while j < n and src[j].isalpha():
                j += 1
            tok = src[pos:j]
            return _DELIM_ALIASES.get(tok, ""), j
        ch = src[pos]
        return _DELIM_ALIASES.get(ch, ch), pos + 1

    def _parse_atom(self, src: str, start: int) -> Tuple[Optional[MathNode], int]:
        """解析單一原子單元、括號群組或 TeX 指令。"""
        n = len(src)
        ch = src[start]

        # 處理不帶底數的獨立上/下標（如 ^{14}_6 C）
        if ch in ("^", "_"):
            return AtomNode("", "roman"), start

        if ch == "{":
            end_b = _find_matching_brace(src, start)
            inner = src[start + 1 : end_b]
            return self.parse(inner), (end_b + 1 if end_b < n else n)

        if ch == "\\":
            return self._parse_command(src, start)

        # 連續數字合併為單一 upright number atom（含小數點如 3.14）
        if ch.isdigit():
            j = start + 1
            while j < n and (src[j].isdigit() or (src[j] == "." and j + 1 < n and src[j + 1].isdigit())):
                j += 1
            return AtomNode(src[start:j], "number"), j

        if ch.isalpha():
            return AtomNode(ch, "var"), start + 1

        if ch == "-":
            return AtomNode("−", "binop"), start + 1
        if ch in "+*":
            return AtomNode("∗" if ch == "*" else "+", "binop"), start + 1
        if ch == "/":
            return AtomNode("/", "roman"), start + 1
        if ch in ("=", "<", ">"):
            return AtomNode(ch, "relop"), start + 1
        if ch in (",", ";", ":"):
            return AtomNode(ch, "punct"), start + 1
        if ch == "'":
            return AtomNode("′", "roman"), start + 1
        return AtomNode(ch, "roman"), start + 1

    def _parse_command(self, src: str, start: int) -> Tuple[Optional[MathNode], int]:
        """解析以 backslash 開頭的 TeX 指令與環境。"""
        n = len(src)
        if start + 1 >= n:
            return AtomNode("\\", "roman"), start + 1

        nxt = src[start + 1]
        if not nxt.isalpha():
            i = start + 2
            if nxt in _SPACE_COMMANDS:
                w = 0.0 if nxt == "!" else 0.22
                return (SpaceNode(w) if w > 0 else None), i
            if nxt == ".":
                return None, i
            if nxt in _SYMBOL_COMMANDS:
                sym = _SYMBOL_COMMANDS[nxt]
                kind = "relop" if sym in _RELATION_OPERATORS else "roman"
                return AtomNode(sym, kind), i
            return AtomNode(nxt, "roman"), i

        j = start + 1
        while j < n and src[j].isalpha():
            j += 1
        if j < n and src[j] == "*" and src[start + 1 : j] in ("operatorname", "xrightarrow", "xleftarrow"):
            j += 1
        cmd = src[start + 1 : j]

        if cmd == "begin":
            return self._parse_environment(src, j)

        if cmd == "left":
            left_d, pos = self._consume_delim_token(src, j)
            inner_nodes, pos = self._parse_sequence(src, pos, stop_at_right=True)
            right_d = ""
            if src.startswith("\\right", pos):
                right_d, pos = self._consume_delim_token(src, pos + 6)
            body = inner_nodes[0] if len(inner_nodes) == 1 else RowNode(inner_nodes)
            return DelimNode(left_d, right_d, body), pos

        if cmd in ("right", "limits", "nolimits", "nonumber", "notag", "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle", "allowbreak"):
            return None, j

        if cmd in ("big", "Big", "bigg", "Bigg", "bigl", "Bigl", "biggl", "Biggl", "bigr", "Bigr", "biggr", "Biggr", "bigm", "Bigm"):
            return None, j

        if cmd in ("frac", "dfrac", "tfrac", "cfrac"):
            num_node, pos = self._parse_arg_node(src, j)
            den_node, pos = self._parse_arg_node(src, pos)
            return FracNode(num_node, den_node, bar=True, full_scale=(cmd in ("dfrac", "cfrac"))), pos

        if cmd in ("binom", "tbinom", "dbinom"):
            top_node, pos = self._parse_arg_node(src, j)
            bot_node, pos = self._parse_arg_node(src, pos)
            return DelimNode("(", ")", FracNode(top_node, bot_node, bar=False)), pos

        if cmd == "sqrt":
            deg_raw, pos = self._consume_optional_arg(src, j)
            rad_node, pos = self._parse_arg_node(src, pos)
            deg_node = self.parse(deg_raw) if deg_raw is not None and deg_raw.strip() else None
            return SqrtNode(rad_node, deg_node), pos

        if cmd in _ACCENT_COMMANDS:
            inner_node, pos = self._parse_arg_node(src, j)
            return AccentNode(cmd, inner_node), pos

        if cmd == "ce":
            chem_raw, pos = self._consume_raw_arg(src, j)
            return _chemistry_to_node(chem_raw), pos

        if cmd == "mathbb":
            bb_raw, pos = self._consume_raw_arg(src, j)
            mapped = "".join(_BLACKBOARD_BOLD.get(c, c) for c in bb_raw.strip())
            return AtomNode(mapped, "roman"), pos

        if cmd in _TEXT_COMMANDS:
            txt_raw, pos = self._consume_raw_arg(src, j)
            return AtomNode(txt_raw, "text"), pos

        if cmd in _ROMAN_COMMANDS:
            inner_raw, pos = self._consume_raw_arg(src, j)
            cleaned = inner_raw.strip()
            kind = "bigop" if cmd == "operatorname*" else "roman"
            return AtomNode(cleaned, kind), pos

        if cmd in _BOLD_COMMANDS:
            inner_raw, pos = self._consume_raw_arg(src, j)
            # 若內部僅單字元或英數字，可直接以粗體繪製；複合式仍遞迴解析
            if re.fullmatch(r"[A-Za-z0-9α-ωΑ-Ω ]+", inner_raw.strip()):
                return AtomNode(inner_raw.strip(), "bold"), pos
            return self.parse(inner_raw), pos

        if cmd in ("phantom", "hphantom", "vphantom", "label"):
            _arg, pos = self._consume_raw_arg(src, j)
            return None, pos

        if cmd == "textcolor":
            _clr, pos = self._consume_raw_arg(src, j)
            body_node, pos = self._parse_arg_node(src, pos)
            return body_node, pos

        if cmd == "color":
            _clr, pos = self._consume_raw_arg(src, j)
            return None, pos

        if cmd in ("xrightarrow", "xleftarrow", "overset", "underset", "stackrel"):
            if cmd in ("xrightarrow", "xleftarrow"):
                _below, pos = self._consume_optional_arg(src, j)
                above, pos = self._parse_arg_node(src, pos)
                sym = AtomNode("←" if "left" in cmd else "→", "relop")
                return ScriptNode(sym, sup=above, limits=True), pos
            first, pos = self._parse_arg_node(src, j)
            second, pos = self._parse_arg_node(src, pos)
            if cmd == "underset":
                return ScriptNode(second, sub=first, limits=True), pos
            return ScriptNode(second, sup=first, limits=True), pos

        if cmd in ("quad", "qquad", "enspace", "thinspace", "medspace", "thickspace"):
            widths = {"quad": 1.0, "qquad": 2.0, "enspace": 0.5, "thinspace": 0.18, "medspace": 0.25, "thickspace": 0.35}
            return SpaceNode(widths.get(cmd, 0.25)), j

        if cmd == "not":
            pos = j
            while pos < n and src[pos].isspace():
                pos += 1
            if pos < n and src[pos] == "=":
                return AtomNode("≠", "relop"), pos + 1
            if src.startswith("\\in", pos):
                return AtomNode("∉", "relop"), pos + 3
            if src.startswith("\\equiv", pos):
                return AtomNode("≢", "relop"), pos + 6
            return AtomNode("¬", "roman"), j

        if cmd in _LIMIT_OP_COMMANDS:
            if cmd in _SYMBOL_COMMANDS:
                return AtomNode(_SYMBOL_COMMANDS[cmd], "bigop"), j
            return AtomNode(cmd, "bigop"), j

        if cmd in _INT_OP_COMMANDS:
            return AtomNode(_SYMBOL_COMMANDS[cmd], "bigop"), j

        if cmd in _FUNCTION_COMMANDS:
            return AtomNode(cmd, "func"), j

        if cmd in _SYMBOL_COMMANDS:
            sym = _SYMBOL_COMMANDS[cmd]
            if not sym:
                return None, j
            if sym in _RELATION_OPERATORS:
                kind = "relop"
            elif sym in _BINARY_OPERATORS:
                kind = "binop"
            elif "α" <= sym <= "ω" or sym in ("ϑ", "ϖ", "ς", "ℏ", "ℓ"):
                kind = "var"
            else:
                kind = "roman"
            return AtomNode(sym, kind), j

        return AtomNode(cmd, "roman"), j

    def _parse_environment(self, src: str, after_begin_idx: int) -> Tuple[MathNode, int]:
        r"""解析 `\begin{env} ... \end{env}` 為矩陣、`cases` 或多行對齊節點。"""
        env_raw, pos = self._consume_raw_arg(src, after_begin_idx)
        env_name = env_raw.strip()
        begin_tag = f"\\begin{{{env_name}}}"
        end_tag = f"\\end{{{env_name}}}"

        search_pos = pos
        depth = 1
        end_idx = -1
        while search_pos < len(src):
            nb = src.find(begin_tag, search_pos)
            ne = src.find(end_tag, search_pos)
            if ne == -1:
                break
            if nb != -1 and nb < ne:
                depth += 1
                search_pos = nb + len(begin_tag)
            else:
                depth -= 1
                if depth == 0:
                    end_idx = ne
                    break
                search_pos = ne + len(end_tag)

        if end_idx == -1:
            return AtomNode(env_name, "roman"), pos

        body = src[pos:end_idx]
        after_end = end_idx + len(end_tag)

        col_aligns = ""
        v_bars: Set[int] = set()
        if env_name in ("array", "subarray") and body.lstrip().startswith("{"):
            lstripped = body.lstrip()
            end_spec = _find_matching_brace(lstripped, 0)
            if end_spec < len(lstripped):
                spec = lstripped[1:end_spec]
                body = lstripped[end_spec + 1 :]
                col_idx = 0
                for ch in spec:
                    if ch in ("l", "c", "r"):
                        col_aligns += ch
                        col_idx += 1
                    elif ch == "|":
                        v_bars.add(col_idx)

        normalized = re.sub(r"\\\\\s*\[[^\]]*\]", r"\\\\", body)
        raw_rows = _split_top_level(normalized, "\\\\")
        grid: List[List[MathNode]] = []
        h_lines: Set[int] = set()

        for row_str in raw_rows:
            has_hline = "\\hline" in row_str
            cleaned_row = re.sub(r"\\hline(?![A-Za-z])", "", row_str).strip()
            if has_hline:
                h_lines.add(len(grid))
            if not cleaned_row:
                continue
            cols = _split_top_level(cleaned_row, "&")
            grid.append([self.parse(c) for c in cols])

        matrix_delims = {
            "pmatrix": ("(", ")"),
            "bmatrix": ("[", "]"),
            "Bmatrix": ("{", "}"),
            "vmatrix": ("|", "|"),
            "Vmatrix": ("‖", "‖"),
            "matrix": ("", ""),
            "smallmatrix": ("", ""),
            "array": ("", ""),
            "subarray": ("", ""),
            "cases": ("{", ""),
            "dcases": ("{", ""),
            "rcases": ("", "}"),
        }
        if env_name in matrix_delims:
            ld, rd = matrix_delims[env_name]
            if env_name in ("cases", "dcases", "rcases"):
                col_aligns = "ll"
            return MatrixNode(grid, left_delim=ld, right_delim=rd, col_aligns=col_aligns, v_bars=v_bars, h_lines=h_lines), after_end

        is_aligned = env_name in ("align", "align*", "aligned", "alignedat", "split", "eqnarray", "eqnarray*")
        return LinesNode(rows=grid, aligned=is_aligned), after_end


# ==============================================================================
# 二維 Box-and-Glue 排版引擎與向量數學符號繪圖
# ==============================================================================


@dataclass
class _MathBox:
    """帶有統一基線座標的排版方盒：繪圖原點為 `(x, baseline_y)`。"""

    width: float
    ascent: float
    descent: float
    draw_fn: Callable[[ImageDraw.ImageDraw, float, float, Tuple[int, int, int]], None]

    @property
    def height(self) -> float:
        return self.ascent + self.descent


class _MathBoxLayout:
    """將 `MathNode` 抽象語法樹量測並排版為可精準合成的 `_MathBox`。"""

    def layout(self, node: MathNode, size: float) -> _MathBox:
        """依目前縮放字級遞迴排版任何 `MathNode`。"""
        s = max(14.0, size)
        if isinstance(node, SpaceNode):
            return _MathBox(node.em * s, 0.0, 0.0, lambda *_a: None)
        if isinstance(node, AtomNode):
            return self._layout_atom(node, s)
        if isinstance(node, RowNode):
            return self._layout_row(node, s)
        if isinstance(node, FracNode):
            return self._layout_frac(node, s)
        if isinstance(node, SqrtNode):
            return self._layout_sqrt(node, s)
        if isinstance(node, ScriptNode):
            return self._layout_script(node, s)
        if isinstance(node, DelimNode):
            return self._layout_delim(node, s)
        if isinstance(node, AccentNode):
            return self._layout_accent(node, s)
        if isinstance(node, MatrixNode):
            return self._layout_matrix(node, s)
        if isinstance(node, LinesNode):
            return self._layout_lines(node, s)
        return _MathBox(0.0, 0.0, 0.0, lambda *_a: None)

    @staticmethod
    def _math_axis(size: float) -> float:
        """數學水平中軸高於基線的距離（對齊 `=`、`+`、分數線與大括號中心）。"""
        return 0.275 * size

    def _layout_atom(self, node: AtomNode, size: float) -> _MathBox:
        text = node.text
        if not text:
            return _MathBox(0.0, 0.55 * size, 0.15 * size, lambda *_a: None)

        draw_size = size * 1.45 if node.kind == "bigop" and text in _BIG_DISPLAY_OPERATORS else size
        if node.kind == "var":
            style = "italic"
        elif node.kind == "bold":
            style = "bold"
        elif node.kind == "text":
            style = "text"
        else:
            style = "math"

        font_set = _get_math_font_set(int(round(draw_size)), style=style)
        runs = font_set.split_runs(text)

        min_top = -0.55 * draw_size
        max_bot = 0.15 * draw_size
        total_advance = 0.0
        left_bearing = 0.0
        max_right_ink = 0.0
        first = True

        run_metrics: List[Tuple[str, Any, float, float]] = []
        for run_str, f_obj in runs:
            try:
                adv = float(f_obj.getlength(run_str))
            except Exception:
                adv = float(len(run_str) * draw_size * 0.55)
            try:
                bbox = f_obj.getbbox(run_str, anchor="ls")
                l, t, r, b = bbox
            except Exception:
                l, t, r, b = 0, int(-0.8 * draw_size), int(adv), int(0.2 * draw_size)

            if first and l < 0:
                left_bearing = float(-l)
                first = False
            elif first:
                first = False

            min_top = min(min_top, float(t))
            max_bot = max(max_bot, float(b))
            cur_ink_right = total_advance + float(r)
            max_right_ink = max(max_right_ink, cur_ink_right)
            run_metrics.append((run_str, f_obj, total_advance, float(r)))
            total_advance += adv

        # 大型數學運算子（如 ∑ ∫）垂直對中調整以兼顧上下限距離
        y_shift = 0.10 * draw_size if (node.kind == "bigop" and text in _BIG_DISPLAY_OPERATORS) else 0.0
        width = max(total_advance, max_right_ink) + left_bearing
        ascent = max(0.2 * size, -min_top - y_shift)
        descent = max(0.1 * size, max_bot + y_shift)

        def _draw(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
            for r_str, r_font, rel_x, _r_edge in run_metrics:
                try:
                    draw.text(
                        (x + left_bearing + rel_x, baseline_y + y_shift),
                        r_str,
                        font=r_font,
                        fill=color,
                        anchor="ls",
                    )
                except Exception:
                    draw.text(
                        (x + left_bearing + rel_x, baseline_y + y_shift - ascent),
                        r_str,
                        font=r_font,
                        fill=color,
                    )

        return _MathBox(width, ascent, descent, _draw)

    def _layout_row(self, node: RowNode, size: float) -> _MathBox:
        if not node.items:
            return _MathBox(0.0, 0.55 * size, 0.15 * size, lambda *_a: None)

        placed: List[Tuple[float, _MathBox]] = []
        cursor = 0.0
        n = len(node.items)

        for idx, item in enumerate(node.items):
            prev_item = node.items[idx - 1] if idx > 0 else None
            next_item = node.items[idx + 1] if idx + 1 < n else None

            pre_gap = 0.0
            post_gap = 0.0
            if isinstance(item, AtomNode):
                if item.kind == "relop":
                    pre_gap = 0.28 * size
                    post_gap = 0.28 * size
                elif item.kind == "binop":
                    # 區分一元符號（開頭、左括號後、關係或二元運算子後）與二元運算
                    is_unary = (
                        prev_item is None
                        or (isinstance(prev_item, AtomNode) and (prev_item.kind in ("binop", "relop", "punct") or prev_item.text in ("(", "[", "{", "⟨")))
                    )
                    if not is_unary:
                        pre_gap = 0.22 * size
                        post_gap = 0.22 * size
                elif item.kind == "punct":
                    post_gap = 0.22 * size
                elif item.kind in ("func", "bigop"):
                    if next_item is not None and not (isinstance(next_item, AtomNode) and next_item.text in ("(", "[", "{")):
                        post_gap = 0.18 * size
            elif isinstance(item, ScriptNode) and isinstance(item.base, AtomNode) and item.base.kind == "bigop":
                if next_item is not None:
                    post_gap = 0.20 * size

            # 避免前後連續空白重複疊加
            if placed and pre_gap > 0:
                cursor += pre_gap
            box = self.layout(item, size)
            placed.append((cursor, box))
            cursor += box.width + (post_gap if idx < n - 1 else 0.0)

        ascent = max((b.ascent for _, b in placed), default=0.55 * size)
        descent = max((b.descent for _, b in placed), default=0.15 * size)

        def _draw(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
            for rel_x, box in placed:
                box.draw_fn(draw, x + rel_x, baseline_y, color)

        return _MathBox(cursor, ascent, descent, _draw)

    def _layout_frac(self, node: FracNode, size: float) -> _MathBox:
        child_size = max(16.0, size * (0.90 if node.full_scale else 0.78))
        num_box = self.layout(node.num, child_size)
        den_box = self.layout(node.den, child_size)

        axis = self._math_axis(size)
        thickness = max(2.0, round(0.055 * size))
        rule_gap = max(4.0, 0.18 * size)
        side_pad = 0.18 * size

        inner_w = max(num_box.width, den_box.width)
        total_w = inner_w + side_pad * 2
        num_x = (total_w - num_box.width) / 2.0
        den_x = (total_w - den_box.width) / 2.0

        # num baseline 位於 math_axis 之上：num.descent + rule_gap + thickness/2
        num_rel_baseline = -(axis + thickness / 2.0 + rule_gap + num_box.descent)
        # den baseline 位於 math_axis 之下：rule_gap + thickness/2 + den.ascent
        den_rel_baseline = -axis + thickness / 2.0 + rule_gap + den_box.ascent

        ascent = max(0.6 * size, -num_rel_baseline + num_box.ascent)
        descent = max(0.2 * size, den_rel_baseline + den_box.descent)

        def _draw(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
            num_box.draw_fn(draw, x + num_x, baseline_y + num_rel_baseline, color)
            den_box.draw_fn(draw, x + den_x, baseline_y + den_rel_baseline, color)
            if node.bar:
                bar_y = baseline_y - axis
                draw.line(
                    [(x + side_pad * 0.35, bar_y), (x + total_w - side_pad * 0.35, bar_y)],
                    fill=color,
                    width=int(round(thickness)),
                )

        return _MathBox(total_w, ascent, descent, _draw)

    def _layout_sqrt(self, node: SqrtNode, size: float) -> _MathBox:
        rad_box = self.layout(node.radicand, size)
        deg_box = self.layout(node.degree, max(14.0, size * 0.56)) if node.degree else None

        stroke = max(2.0, round(0.055 * size))
        top_gap = max(5.0, 0.18 * size)
        tail_pad = 0.14 * size
        hook_base_w = 0.58 * size

        deg_extra_left = max(0.0, deg_box.width - hook_base_w * 0.42 + 0.04 * size) if deg_box else 0.0
        hook_start_x = deg_extra_left
        rad_x = hook_start_x + hook_base_w + 0.08 * size
        total_w = rad_x + rad_box.width + tail_pad

        top_bar_rel_y = -(rad_box.ascent + top_gap)
        bot_rel_y = rad_box.descent + 0.06 * size
        shelf_rel_y = (top_bar_rel_y + bot_rel_y) * 0.52 - 0.06 * size
        deg_rel_baseline = 0.0
        top_deg_extent = -top_bar_rel_y
        if deg_box is not None:
            # 根指數置於根號小橫槓上方，與勾尖斜線錯開
            deg_rel_baseline = shelf_rel_y - 0.10 * size - deg_box.descent
            top_deg_extent = max(top_deg_extent, -deg_rel_baseline + deg_box.ascent)

        ascent = max(top_deg_extent + stroke, rad_box.ascent + top_gap + stroke)
        descent = max(rad_box.descent, bot_rel_y)

        def _draw(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
            w_px = int(round(stroke))
            sx = x + hook_start_x
            mid_y = baseline_y + (top_bar_rel_y + bot_rel_y) * 0.52
            p1 = (sx, mid_y)
            p2 = (sx + hook_base_w * 0.24, baseline_y + shelf_rel_y)
            p3 = (sx + hook_base_w * 0.54, baseline_y + bot_rel_y)
            p4 = (sx + hook_base_w, baseline_y + top_bar_rel_y)
            p5 = (x + total_w - tail_pad * 0.3, baseline_y + top_bar_rel_y)
            draw.line([p1, p2], fill=color, width=w_px)
            draw.line([p2, p3], fill=color, width=max(w_px, int(round(stroke * 1.35))))
            draw.line([p3, p4], fill=color, width=w_px)
            draw.line([p4, p5], fill=color, width=w_px)
            rad_box.draw_fn(draw, x + rad_x, baseline_y, color)
            if deg_box is not None:
                deg_x = max(x, sx + hook_base_w * 0.42 - deg_box.width - 0.04 * size)
                deg_box.draw_fn(draw, deg_x, baseline_y + deg_rel_baseline, color)

        return _MathBox(total_w, ascent, descent, _draw)

    def _layout_script(self, node: ScriptNode, size: float) -> _MathBox:
        base_box = self.layout(node.base, size)
        script_size = max(14.0, size * 0.64)
        sub_box = self.layout(node.sub, script_size) if node.sub else None
        sup_box = self.layout(node.sup, script_size) if node.sup else None

        if node.limits and (sub_box or sup_box):
            gap = 0.14 * size
            total_w = max(
                base_box.width,
                sub_box.width if sub_box else 0.0,
                sup_box.width if sup_box else 0.0,
            )
            base_x = (total_w - base_box.width) / 2.0
            ascent = base_box.ascent
            descent = base_box.descent

            sup_rel_baseline = 0.0
            sub_rel_baseline = 0.0
            if sup_box:
                sup_rel_baseline = -(base_box.ascent + gap + sup_box.descent)
                ascent = max(ascent, -sup_rel_baseline + sup_box.ascent)
            if sub_box:
                sub_rel_baseline = base_box.descent + gap + sub_box.ascent
                descent = max(descent, sub_rel_baseline + sub_box.descent)

            def _draw_limits(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
                base_box.draw_fn(draw, x + base_x, baseline_y, color)
                if sup_box:
                    sup_box.draw_fn(draw, x + (total_w - sup_box.width) / 2.0, baseline_y + sup_rel_baseline, color)
                if sub_box:
                    sub_box.draw_fn(draw, x + (total_w - sub_box.width) / 2.0, baseline_y + sub_rel_baseline, color)

            return _MathBox(total_w, ascent, descent, _draw_limits)

        # 右側角標（依主體實際上下緣自動提高/降下，避免高底數重疊）
        kern = 0.06 * size if base_box.width > 0 else 0.0
        script_x = base_box.width + kern
        sup_rel_baseline = 0.0
        sub_rel_baseline = 0.0
        ascent = base_box.ascent
        descent = base_box.descent

        if sup_box:
            # 普通字母上標約提升 0.42*size；若主體特別高（如括號或分數）則往上對齊頂緣
            target_sup_top = -max(0.45 * size + sup_box.descent, base_box.ascent * 0.72)
            sup_rel_baseline = target_sup_top
            ascent = max(ascent, -sup_rel_baseline + sup_box.ascent)

        if sub_box:
            target_sub = max(0.24 * size, base_box.descent + 0.06 * size)
            # 若同時有上標與下標，確保兩者不垂直相撞
            if sup_box:
                min_sub_baseline = sup_rel_baseline + sup_box.descent + 0.10 * size + sub_box.ascent
                target_sub = max(target_sub, min_sub_baseline)
            sub_rel_baseline = target_sub
            descent = max(descent, sub_rel_baseline + sub_box.descent)

        extra_w = max(sub_box.width if sub_box else 0.0, sup_box.width if sup_box else 0.0)
        total_w = script_x + extra_w + (0.04 * size if extra_w > 0 else 0.0)

        def _draw_scripts(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
            base_box.draw_fn(draw, x, baseline_y, color)
            if sup_box:
                sup_box.draw_fn(draw, x + script_x, baseline_y + sup_rel_baseline, color)
            if sub_box:
                sub_box.draw_fn(draw, x + script_x, baseline_y + sub_rel_baseline, color)

        return _MathBox(total_w, ascent, descent, _draw_scripts)

    def _draw_tall_delim(
        self,
        draw: ImageDraw.ImageDraw,
        delim: str,
        x: float,
        w: float,
        y_top: float,
        y_bot: float,
        stroke: float,
        color: Tuple[int, int, int],
    ) -> None:
        """以向量幾何繪製可自動伸縮之高括號、方括號、大括號、絕對值與尖括號。"""
        h = y_bot - y_top
        y_mid = (y_top + y_bot) / 2.0
        lw = max(2, int(round(stroke)))

        if delim in ("|", "‖"):
            if delim == "|":
                cx = x + w / 2.0
                draw.line([(cx, y_top), (cx, y_bot)], fill=color, width=lw)
            else:
                cx1 = x + w * 0.34
                cx2 = x + w * 0.66
                draw.line([(cx1, y_top), (cx1, y_bot)], fill=color, width=lw)
                draw.line([(cx2, y_top), (cx2, y_bot)], fill=color, width=lw)
            return

        if delim in ("(", ")"):
            is_left = delim == "("
            outer_x = x + w * (0.28 if is_left else 0.72)
            inner_x = x + w * (0.82 if is_left else 0.18)
            steps = 28
            pts = []
            for k in range(steps + 1):
                t = (k / steps) * 2.0 - 1.0  # -1 .. +1
                # 平滑二次曲線：端點彎入、中段略收
                px = outer_x + (inner_x - outer_x) * (t * t)
                py = y_mid + t * (h / 2.0)
                pts.append((px, py))
            draw.line(pts, fill=color, width=lw, joint="curve")
            return

        if delim in ("[", "]", "⌊", "⌋", "⌈", "⌉"):
            is_left = delim in ("[", "⌊", "⌈")
            spine_x = x + w * (0.32 if is_left else 0.68)
            tip_x = x + w * (0.85 if is_left else 0.15)
            draw.line([(spine_x, y_top), (spine_x, y_bot)], fill=color, width=lw)
            if delim in ("[", "]", "⌈", "⌉"):
                draw.line([(spine_x, y_top), (tip_x, y_top)], fill=color, width=lw)
            if delim in ("[", "]", "⌊", "⌋"):
                draw.line([(spine_x, y_bot), (tip_x, y_bot)], fill=color, width=lw)
            return

        if delim in ("{", "}"):
            is_left = delim == "{"
            mid_tip = x + w * (0.12 if is_left else 0.88)
            stem_x = x + w * 0.50
            end_tip = x + w * (0.88 if is_left else 0.12)
            hook_h = min(h * 0.14, max(8.0, w * 0.55))
            pts = [
                (end_tip, y_top),
                (stem_x + (end_tip - stem_x) * 0.25, y_top + hook_h * 0.35),
                (stem_x, y_top + hook_h),
                (stem_x, y_mid - hook_h * 0.85),
                (mid_tip, y_mid),
                (stem_x, y_mid + hook_h * 0.85),
                (stem_x, y_bot - hook_h),
                (stem_x + (end_tip - stem_x) * 0.25, y_bot - hook_h * 0.35),
                (end_tip, y_bot),
            ]
            draw.line(pts, fill=color, width=lw, joint="curve")
            return

        if delim in ("⟨", "⟩"):
            tip_x = x + w * (0.20 if delim == "⟨" else 0.80)
            arm_x = x + w * (0.82 if delim == "⟨" else 0.18)
            draw.line([(arm_x, y_top), (tip_x, y_mid), (arm_x, y_bot)], fill=color, width=lw, joint="curve")

    def _layout_delim(self, node: DelimNode, size: float) -> _MathBox:
        body_box = self.layout(node.body, size)
        # 判斷是否達需啟用向量高括號之高度
        if body_box.height <= 1.45 * size and len(node.left) <= 1 and len(node.right) <= 1:
            parts: List[_MathBox] = []
            if node.left:
                parts.append(self._layout_atom(AtomNode(node.left, "roman"), size))
            parts.append(body_box)
            if node.right:
                parts.append(self._layout_atom(AtomNode(node.right, "roman"), size))
            total_w = sum(p.width for p in parts)
            asc = max(p.ascent for p in parts)
            desc = max(p.descent for p in parts)

            def _draw_std(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
                cx = x
                for p in parts:
                    p.draw_fn(draw, cx, baseline_y, color)
                    cx += p.width

            return _MathBox(total_w, asc, desc, _draw_std)

        pad_y = 0.08 * size
        top_rel = -(body_box.ascent + pad_y)
        bot_rel = body_box.descent + pad_y
        tall_h = bot_rel - top_rel
        stroke = max(2.0, 0.055 * size)
        delim_w = (0.34 * size + min(0.22 * size, tall_h * 0.06)) if node.left or node.right else 0.0
        lw = delim_w if node.left else 0.0
        rw = delim_w if node.right else 0.0
        gap = 0.06 * size
        total_w = lw + (gap if lw else 0.0) + body_box.width + (gap if rw else 0.0) + rw
        body_x = lw + (gap if lw else 0.0)
        ascent = max(body_box.ascent + pad_y, -top_rel)
        descent = max(body_box.descent + pad_y, bot_rel)

        def _draw_tall(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
            if node.left:
                self._draw_tall_delim(
                    draw, node.left, x, lw, baseline_y + top_rel, baseline_y + bot_rel, stroke, color
                )
            body_box.draw_fn(draw, x + body_x, baseline_y, color)
            if node.right:
                self._draw_tall_delim(
                    draw,
                    node.right,
                    x + total_w - rw,
                    rw,
                    baseline_y + top_rel,
                    baseline_y + bot_rel,
                    stroke,
                    color,
                )

        return _MathBox(total_w, ascent, descent, _draw_tall)

    def _layout_accent(self, node: AccentNode, size: float) -> _MathBox:
        body_box = self.layout(node.body, size)
        stroke = max(2.0, round(0.05 * size))
        cmd = node.cmd
        pad_above = 0.24 * size
        top_y_rel = -(body_box.ascent + pad_above)
        bot_pad = 0.14 * size if cmd == "underline" else 0.0

        ascent = body_box.ascent + (0.0 if cmd == "underline" else pad_above + 0.08 * size)
        descent = body_box.descent + bot_pad

        def _draw(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
            body_box.draw_fn(draw, x, baseline_y, color)
            w_px = int(round(stroke))
            lx = x + 0.04 * size
            rx = x + max(body_box.width - 0.04 * size, 0.25 * size)
            mid_x = (lx + rx) / 2.0
            ay = baseline_y + top_y_rel + 0.08 * size

            if cmd in ("bar", "overline"):
                draw.line([(lx, ay), (rx, ay)], fill=color, width=w_px)
            elif cmd == "underline":
                uy = baseline_y + body_box.descent + 0.08 * size
                draw.line([(lx, uy), (rx, uy)], fill=color, width=w_px)
            elif cmd in ("vec", "overrightarrow"):
                draw.line([(lx, ay), (rx, ay)], fill=color, width=w_px)
                tip = min(0.16 * size, (rx - lx) * 0.45)
                draw.line([(rx - tip, ay - tip * 0.65), (rx, ay), (rx - tip, ay + tip * 0.65)], fill=color, width=w_px)
            elif cmd == "overleftarrow":
                draw.line([(lx, ay), (rx, ay)], fill=color, width=w_px)
                tip = min(0.16 * size, (rx - lx) * 0.45)
                draw.line([(lx + tip, ay - tip * 0.65), (lx, ay), (lx + tip, ay + tip * 0.65)], fill=color, width=w_px)
            elif cmd in ("hat", "widehat"):
                peak = 0.14 * size
                draw.line([(lx, ay + peak * 0.5), (mid_x, ay - peak * 0.5), (rx, ay + peak * 0.5)], fill=color, width=w_px)
            elif cmd in ("tilde", "widetilde"):
                amp = 0.07 * size
                pts = []
                for k in range(17):
                    t = k / 16.0
                    pts.append((lx + (rx - lx) * t, ay - amp * math.sin(2.0 * math.pi * t)))
                draw.line(pts, fill=color, width=w_px, joint="curve")
            elif cmd == "dot":
                r = max(2.0, stroke * 0.85)
                draw.ellipse([mid_x - r, ay - r, mid_x + r, ay + r], fill=color)
            elif cmd == "ddot":
                r = max(1.8, stroke * 0.78)
                sep = max(3.5, 0.14 * size)
                draw.ellipse([mid_x - sep - r, ay - r, mid_x - sep + r, ay + r], fill=color)
                draw.ellipse([mid_x + sep - r, ay - r, mid_x + sep + r, ay + r], fill=color)

        return _MathBox(body_box.width, ascent, descent, _draw)

    def _layout_matrix(self, node: MatrixNode, size: float) -> _MathBox:
        if not node.rows:
            return _MathBox(0.0, 0.55 * size, 0.15 * size, lambda *_a: None)

        num_cols = max((len(r) for r in node.rows), default=0)
        box_grid: List[List[_MathBox]] = []
        col_widths = [0.0] * num_cols
        row_ascs: List[float] = []
        row_descs: List[float] = []

        for row in node.rows:
            b_row: List[_MathBox] = []
            max_a = 0.55 * size
            max_d = 0.15 * size
            for c_idx in range(num_cols):
                cell_node = row[c_idx] if c_idx < len(row) else RowNode([])
                c_box = self.layout(cell_node, size)
                b_row.append(c_box)
                col_widths[c_idx] = max(col_widths[c_idx], c_box.width)
                max_a = max(max_a, c_box.ascent)
                max_d = max(max_d, c_box.descent)
            box_grid.append(b_row)
            row_ascs.append(max_a)
            row_descs.append(max_d)

        col_gap = 0.75 * size
        row_gap = 0.42 * size
        grid_w = sum(col_widths) + max(0, num_cols - 1) * col_gap
        grid_h = sum(a + d for a, d in zip(row_ascs, row_descs)) + max(0, len(box_grid) - 1) * row_gap

        axis = self._math_axis(size)
        grid_ascent = grid_h / 2.0 + axis
        grid_descent = grid_h / 2.0 - axis

        def _draw_grid(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
            top_y = baseline_y - grid_ascent
            cur_y = top_y
            col_x_starts: List[float] = []
            cx = x
            for cw in col_widths:
                col_x_starts.append(cx)
                cx += cw + col_gap

            rule_w = max(1, int(round(0.04 * size)))
            for r_idx, b_row in enumerate(box_grid):
                if r_idx in node.h_lines:
                    hy = cur_y - row_gap * 0.5
                    draw.line([(x, hy), (x + grid_w, hy)], fill=color, width=rule_w)
                r_baseline = cur_y + row_ascs[r_idx]
                for c_idx, c_box in enumerate(b_row):
                    align = node.col_aligns[c_idx] if c_idx < len(node.col_aligns) else "c"
                    if align == "l":
                        cell_x = col_x_starts[c_idx]
                    elif align == "r":
                        cell_x = col_x_starts[c_idx] + col_widths[c_idx] - c_box.width
                    else:
                        cell_x = col_x_starts[c_idx] + (col_widths[c_idx] - c_box.width) / 2.0
                    c_box.draw_fn(draw, cell_x, r_baseline, color)
                cur_y += row_ascs[r_idx] + row_descs[r_idx] + row_gap

            if len(box_grid) in node.h_lines:
                hy = top_y + grid_h + row_gap * 0.25
                draw.line([(x, hy), (x + grid_w, hy)], fill=color, width=rule_w)

            for bar_col in node.v_bars:
                if bar_col == 0:
                    bx = x - col_gap * 0.25
                elif bar_col >= num_cols:
                    bx = x + grid_w + col_gap * 0.25
                else:
                    bx = col_x_starts[bar_col] - col_gap * 0.5
                draw.line([(bx, top_y), (bx, top_y + grid_h)], fill=color, width=rule_w)

        grid_box = _MathBox(grid_w, grid_ascent, grid_descent, _draw_grid)
        if not node.left_delim and not node.right_delim:
            return grid_box

        # 將整體矩陣包裹於自動高度的 DelimNode-like 括號中
        pad_y = 0.10 * size
        top_rel = -(grid_box.ascent + pad_y)
        bot_rel = grid_box.descent + pad_y
        tall_h = bot_rel - top_rel
        stroke = max(2.0, 0.055 * size)
        delim_w = 0.36 * size + min(0.24 * size, tall_h * 0.06)
        lw = delim_w if node.left_delim else 0.0
        rw = delim_w if node.right_delim else 0.0
        gap = 0.14 * size
        total_w = lw + (gap if lw else 0.0) + grid_box.width + (gap if rw else 0.0) + rw
        body_x = lw + (gap if lw else 0.0)

        def _draw_wrapped(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
            if node.left_delim:
                self._draw_tall_delim(
                    draw, node.left_delim, x, lw, baseline_y + top_rel, baseline_y + bot_rel, stroke, color
                )
            grid_box.draw_fn(draw, x + body_x, baseline_y, color)
            if node.right_delim:
                self._draw_tall_delim(
                    draw,
                    node.right_delim,
                    x + total_w - rw,
                    rw,
                    baseline_y + top_rel,
                    baseline_y + bot_rel,
                    stroke,
                    color,
                )

        return _MathBox(total_w, grid_box.ascent + pad_y, grid_box.descent + pad_y, _draw_wrapped)

    def _layout_lines(self, node: LinesNode, size: float) -> _MathBox:
        if not node.rows:
            return _MathBox(0.0, 0.55 * size, 0.15 * size, lambda *_a: None)

        num_cols = max((len(r) for r in node.rows), default=1)
        box_grid: List[List[_MathBox]] = []
        col_widths = [0.0] * num_cols
        row_ascs: List[float] = []
        row_descs: List[float] = []

        for r in node.rows:
            b_row: List[_MathBox] = []
            max_a = 0.55 * size
            max_d = 0.15 * size
            for c_idx in range(num_cols):
                cell_node = r[c_idx] if c_idx < len(r) else RowNode([])
                c_box = self.layout(cell_node, size)
                b_row.append(c_box)
                col_widths[c_idx] = max(col_widths[c_idx], c_box.width)
                max_a = max(max_a, c_box.ascent)
                max_d = max(max_d, c_box.descent)
            box_grid.append(b_row)
            row_ascs.append(max_a)
            row_descs.append(max_d)

        row_gap = 0.45 * size
        col_gap = 0.30 * size if node.aligned else 0.0
        total_w = sum(col_widths) + max(0, num_cols - 1) * col_gap
        total_h = sum(a + d for a, d in zip(row_ascs, row_descs)) + max(0, len(box_grid) - 1) * row_gap
        axis = self._math_axis(size)
        ascent = total_h / 2.0 + axis
        descent = total_h / 2.0 - axis

        def _draw(draw: ImageDraw.ImageDraw, x: float, baseline_y: float, color: Tuple[int, int, int]) -> None:
            cur_y = baseline_y - ascent
            for r_idx, b_row in enumerate(box_grid):
                r_baseline = cur_y + row_ascs[r_idx]
                if not node.aligned and num_cols == 1:
                    c_box = b_row[0]
                    c_box.draw_fn(draw, x + (total_w - c_box.width) / 2.0, r_baseline, color)
                else:
                    cx = x
                    for c_idx, c_box in enumerate(b_row):
                        # TeX align 規則：偶數欄右對齊、奇數欄左對齊（正好對齊 &=）
                        if c_idx % 2 == 0:
                            cell_x = cx + col_widths[c_idx] - c_box.width
                        else:
                            cell_x = cx
                        c_box.draw_fn(draw, cell_x, r_baseline, color)
                        cx += col_widths[c_idx] + col_gap
                cur_y += row_ascs[r_idx] + row_descs[r_idx] + row_gap

        return _MathBox(total_w, ascent, descent, _draw)


# ==============================================================================
# 圖片渲染主入口 (2x SSAA 抗鋸齒 + Discord 深色數學公式卡片)
# ==============================================================================


def render_latex_to_image(expr: str) -> io.BytesIO:
    """將單一 LaTeX 數學或化學式渲染為高畫質深色 Discord 卡片 PNG 位元組串流。"""
    parser = _TeXASTParser()
    ast_root = parser.parse(expr)
    layout_engine = _MathBoxLayout()

    # 於 2x 超取樣畫布繪製再縮小，確保分數線、根號與括號曲線平滑銳利
    ssaa_scale = 2
    base_font_px = 34 * ssaa_scale
    max_content_px = 1320 * ssaa_scale

    box = layout_engine.layout(ast_root, float(base_font_px))
    if box.width > max_content_px and box.width > 0:
        shrink = max(0.50, max_content_px / box.width)
        box = layout_engine.layout(ast_root, float(base_font_px) * shrink)

    margin_x = 14 * ssaa_scale
    margin_y = 10 * ssaa_scale
    pad_x = 28 * ssaa_scale
    pad_y = 20 * ssaa_scale
    accent_w = 4 * ssaa_scale

    min_card_w = 280 * ssaa_scale
    card_w = max(min_card_w, int(math.ceil(box.width)) + pad_x * 2 + accent_w)
    card_h = max(62 * ssaa_scale, int(math.ceil(box.height)) + pad_y * 2)

    canvas_w = card_w + margin_x * 2
    canvas_h = card_h + margin_y * 2

    # Discord 深色卡片配色（與 table_renderer.py 風格一致）
    img = Image.new("RGB", (canvas_w, canvas_h), color=(30, 31, 35))
    draw = ImageDraw.Draw(img)

    x0, y0 = margin_x, margin_y
    x1, y1 = margin_x + card_w, margin_y + card_h
    radius = 10 * ssaa_scale
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=(43, 45, 49), outline=(62, 65, 71), width=ssaa_scale)

    # 左側 Discord Blurple 裝飾引線條
    draw.rounded_rectangle(
        [x0 + 3 * ssaa_scale, y0 + 8 * ssaa_scale, x0 + 3 * ssaa_scale + accent_w, y1 - 8 * ssaa_scale],
        radius=2 * ssaa_scale,
        fill=(88, 101, 242),
    )

    inner_left = x0 + accent_w + pad_x
    inner_avail_w = (x1 - pad_x) - inner_left
    draw_x = inner_left + max(0.0, (inner_avail_w - box.width) / 2.0)
    baseline_y = y0 + (card_h - box.height) / 2.0 + box.ascent

    box.draw_fn(draw, draw_x, baseline_y, (245, 247, 252))

    # 降採樣至 1x 高清銳利輸出
    out_w = max(1, canvas_w // ssaa_scale)
    out_h = max(1, canvas_h // ssaa_scale)
    final_img = img.resize((out_w, out_h), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    final_img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ==============================================================================
# 回覆文本自動抽取與交錯標記 (`[[MATH_IMAGE:math_k.png]]`)
# ==============================================================================


def _has_structural_math(expr: str) -> bool:
    """判斷 `$...$` 或 `\\(...\\)` 是否含二維結構（分數、根號、極限、求和積分、矩陣等）。"""
    return bool(_STRUCTURAL_INLINE_RE.search(expr))


def _flush_pending_math_images(line_buffer: List[str], pending_markers: List[str]) -> None:
    """將行內觸發渲染的公式圖片標記安插於該行／段落結尾，不打斷句子連貫性。"""
    if not pending_markers:
        return
    if line_buffer and line_buffer[-1].strip():
        line_buffer.append("\n" + "\n".join(pending_markers))
    else:
        line_buffer.append("\n".join(pending_markers))
    pending_markers.clear()


def _extract_prose_formulas(
    segment: str,
    files: List[discord.File],
    counter: List[int],
    max_images: int,
) -> str:
    """於純文字區段中擷取顯示公式與二維結構公式並渲染為 PNG，保留簡易行內公式待後續格式化。"""
    out: List[str] = []
    pending_markers: List[str] = []
    i = 0
    n = len(segment)

    def _try_render_formula(raw_expr: str) -> Optional[str]:
        if counter[0] >= max_images:
            return None
        cleaned = raw_expr.strip()
        if not cleaned:
            return None
        try:
            buf = render_latex_to_image(cleaned)
            counter[0] += 1
            fname = f"math_{counter[0]}.png"
            files.append(discord.File(fp=buf, filename=fname))
            return fname
        except Exception as render_err:
            print(f"⚠️ LaTeX 轉圖片例外，改用文字格式化: {render_err}")
            return None

    while i < n:
        ch = segment[i]
        if ch == "\n":
            out.append(ch)
            _flush_pending_math_images(out, pending_markers)
            i += 1
            continue

        # 1. $$ ... $$ 顯示公式
        if segment.startswith("$$", i) and (i == 0 or segment[i - 1] != "\\"):
            end = _find_unescaped(segment, "$$", i + 2)
            if end != -1:
                expr = segment[i + 2 : end]
                fname = _try_render_formula(expr)
                if fname:
                    out.append(f"\n[[MATH_IMAGE:{fname}]]\n")
                    i = end + 2
                    continue

        # 2. \[ ... \] 顯示公式
        if segment.startswith("\\[", i) and (i == 0 or segment[i - 1] != "\\"):
            end = _find_unescaped(segment, "\\]", i + 2)
            if end != -1:
                expr = segment[i + 2 : end]
                fname = _try_render_formula(expr)
                if fname:
                    out.append(f"\n[[MATH_IMAGE:{fname}]]\n")
                    i = end + 2
                    continue

        # 3. 獨立 \begin{env} ... \end{env} 數學環境
        if segment.startswith("\\begin{", i) and (i == 0 or segment[i - 1] != "\\"):
            matched = _match_standalone_environment(segment, i)
            if matched is not None:
                end_pos, env_src = matched
                fname = _try_render_formula(env_src)
                if fname:
                    out.append(f"\n[[MATH_IMAGE:{fname}]]\n")
                    i = end_pos
                    continue

        # 4. \( ... \) 或 $ ... $（若單獨成行、接於冒號標籤結尾或含分數/根號/積分/矩陣等二維結構則渲染圖片）
        is_paren = segment.startswith("\\(", i) and (i == 0 or segment[i - 1] != "\\")
        is_dollar = (ch == "$") and (i == 0 or segment[i - 1] != "\\") and not segment.startswith("$$", i)

        if is_paren or is_dollar:
            if is_paren:
                end = _find_unescaped(segment, "\\)", i + 2)
                inner = segment[i + 2 : end] if end != -1 else ""
                span_end = end + 2 if end != -1 else -1
                valid = end != -1
            else:
                end = _find_inline_dollar_end(segment, i + 1)
                inner = segment[i + 1 : end] if end != -1 else ""
                span_end = end + 1 if end != -1 else -1
                after_char = segment[span_end] if (span_end != -1 and span_end < n) else ""
                valid = end != -1 and _looks_like_inline_latex(inner, after_char)

            if valid and inner.strip():
                line_start = segment.rfind("\n", 0, i) + 1
                before_on_line = segment[line_start:i].strip()
                line_end = segment.find("\n", span_end)
                if line_end == -1:
                    line_end = n
                after_on_line = segment[span_end:line_end].strip()

                isolated = not before_on_line and not after_on_line
                labeled_end = bool(before_on_line) and before_on_line.endswith((":", "：")) and len(after_on_line.rstrip(".。,，；;")) == 0
                structural = _has_structural_math(inner)

                if isolated or labeled_end or structural:
                    fname = _try_render_formula(inner)
                    if fname:
                        if isolated:
                            out.append(f"\n[[MATH_IMAGE:{fname}]]\n")
                        elif labeled_end:
                            out.append(f"\n[[MATH_IMAGE:{fname}]]")
                        else:
                            # 句子中間有後續文字時，句內保留可讀 Unicode，卡片緊隨本段附於下方
                            out.append(format_latex_expression(inner, multiline=False))
                            pending_markers.append(f"[[MATH_IMAGE:{fname}]]")
                        i = span_end
                        continue

        out.append(ch)
        i += 1

    _flush_pending_math_images(out, pending_markers)
    return "".join(out)


def extract_and_render_latex_formulas(
    text: str,
    *,
    max_images: int = 8,
) -> Tuple[str, List[discord.File]]:
    """自動從回覆內容抽出 LaTeX 顯示公式與二維數學式並渲染為 PNG 卡片附件。"""
    if not text or max_images <= 0 or not any(m in text for m in ("$", "\\(", "\\[", "\\begin{")):
        return text, []

    files: List[discord.File] = []
    counter = [0]
    parts: List[str] = []

    for is_code, seg in _split_code_regions(text):
        if is_code:
            parts.append(seg)
        else:
            parts.append(_extract_prose_formulas(seg, files, counter, max_images))

    joined = "".join(parts)
    # 整理標記前後過多連續空行
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return joined, files
