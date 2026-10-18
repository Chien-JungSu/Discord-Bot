from __future__ import annotations

import functools
import io
import os
import re
import struct
from typing import List, Tuple, Optional, Set, Any, Union

import discord
from PIL import Image, ImageDraw, ImageFont

DELIM_PATTERN = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$')


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_FONTS_DIR = os.path.join(BASE_DIR, 'static', 'fonts')
BUNDLED_FONT = os.path.join(PROJECT_FONTS_DIR, 'wqy-microhei.ttc')
UNIFONT_PATH = os.path.join(PROJECT_FONTS_DIR, 'unifont.otf')


@functools.lru_cache(maxsize=16)
def load_font_cmap(font_path: str) -> Set[int]:
    """快速解析 TTF/OTF/TTC 字型檔的 cmap 表，取得其支援的 Unicode 碼位集合。"""
    if not font_path or not os.path.exists(font_path):
        return set()
    try:
        with open(font_path, 'rb') as f:
            data = f.read()
        if data[:4] == b'ttcf':
            offset = struct.unpack('>I', data[12:16])[0]
        else:
            offset = 0
        num_tables = struct.unpack('>H', data[offset + 4 : offset + 6])[0]
        cmap_offset = None
        for i in range(num_tables):
            entry = offset + 12 + i * 16
            if data[entry : entry + 4] == b'cmap':
                cmap_offset = struct.unpack('>I', data[entry + 8 : entry + 12])[0]
                break
        if not cmap_offset:
            return set()
        num_subtables = struct.unpack('>H', data[cmap_offset + 2 : cmap_offset + 4])[0]
        supported: Set[int] = set()
        for i in range(num_subtables):
            sub = cmap_offset + 4 + i * 8
            plat, enc, sub_off = struct.unpack('>HHI', data[sub : sub + 8])
            fmt = struct.unpack('>H', data[cmap_offset + sub_off : cmap_offset + sub_off + 2])[0]
            if fmt == 4:
                st = cmap_offset + sub_off
                seg_count = struct.unpack('>H', data[st + 6 : st + 8])[0] // 2
                end_codes = struct.unpack(f'>{seg_count}H', data[st + 14 : st + 14 + seg_count * 2])
                start_codes = struct.unpack(f'>{seg_count}H', data[st + 16 + seg_count * 2 : st + 16 + seg_count * 4])
                for start, end in zip(start_codes, end_codes):
                    if start <= end and end < 0xFFFF:
                        supported.update(range(start, end + 1))
            elif fmt == 12:
                st = cmap_offset + sub_off
                num_groups = struct.unpack('>I', data[st + 12 : st + 16])[0]
                for g in range(num_groups):
                    start, end, _ = struct.unpack('>III', data[st + 16 + g * 12 : st + 28 + g * 12])
                    supported.update(range(start, end + 1))
        return supported
    except Exception as e:
        print(f"⚠️ 解析字型 cmap 失敗 ({font_path}): {e}")
        return set()


class FontRenderer:
    """支援多字型自動回退 (Font Fallback) 的渲染器，確保中文、顏文字與特殊符號皆能無缺字顯示。"""

    def __init__(self, fonts_with_cmaps: List[Tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, Set[int]]]):
        self.fonts = fonts_with_cmaps

    def _split_into_runs(self, text: str) -> List[Tuple[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]]:
        if not text:
            return []
        if not self.fonts:
            return [(text, ImageFont.load_default())]
        if len(self.fonts) == 1:
            return [(text, self.fonts[0][0])]

        runs: List[Tuple[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]] = []
        cur_chars: List[str] = []
        cur_font: Optional[ImageFont.FreeTypeFont | ImageFont.ImageFont] = None

        for ch in text:
            if ch in ('\n', ' ', '\t', '\r'):
                chosen_font = cur_font or self.fonts[0][0]
            else:
                code = ord(ch)
                chosen_font = self.fonts[0][0]
                for font_obj, cmap in self.fonts:
                    if code in cmap:
                        chosen_font = font_obj
                        break

            if cur_font is None:
                cur_font = chosen_font
                cur_chars.append(ch)
            elif chosen_font == cur_font:
                cur_chars.append(ch)
            else:
                runs.append((''.join(cur_chars), cur_font))
                cur_chars = [ch]
                cur_font = chosen_font

        if cur_chars and cur_font:
            runs.append((''.join(cur_chars), cur_font))

        return runs

    def get_text_width(self, text: str) -> int:
        if not text:
            return 0
        runs = self._split_into_runs(text)
        total_w = 0
        for run_text, font_obj in runs:
            try:
                bbox = font_obj.getbbox(run_text)
                total_w += (bbox[2] - bbox[0])
            except Exception:
                total_w += len(run_text) * 10
        return total_w

    def getbbox(self, text: str) -> Tuple[int, int, int, int]:
        w = self.get_text_width(text)
        return (0, 0, w, 20)

    def draw_text(self, draw: ImageDraw.ImageDraw, xy: Tuple[int, int], text: str, fill: Any):
        if not text:
            return
        cur_x, cur_y = xy
        runs = self._split_into_runs(text)
        for run_text, font_obj in runs:
            draw.text((cur_x, cur_y), run_text, font=font_obj, fill=fill)
            try:
                bbox = font_obj.getbbox(run_text)
                cur_x += (bbox[2] - bbox[0])
            except Exception:
                cur_x += len(run_text) * 10


def get_font_renderer(size: int = 16, bold: bool = False) -> FontRenderer:
    """建立支援多字型自動回退 (Font Fallback) 的字型渲染器。
    以中文字型 (微軟正黑/微米黑) 作為主要文字渲染，並以 Unifont 及系統符號字型作為顏文字與特殊符號之無缺字備援。
    """
    fonts_with_cmaps: List[Tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, Set[int]]] = []

    # 1. 尋找主要中文字型
    primary_candidates = [
        'C:/Windows/Fonts/msjhbd.ttc' if bold else 'C:/Windows/Fonts/msjh.ttc',
        BUNDLED_FONT,
        'C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc' if bold else '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
        '/System/Library/Fonts/PingFang.ttc',
        'C:/Windows/Fonts/mingliu.ttc',
    ]

    primary_loaded = False
    for path in primary_candidates:
        if path and os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                cmap = load_font_cmap(path)
                fonts_with_cmaps.append((f, cmap))
                primary_loaded = True
                break
            except Exception:
                continue

    if not primary_loaded and not os.path.exists(BUNDLED_FONT):
        try:
            import urllib.request
            os.makedirs(PROJECT_FONTS_DIR, exist_ok=True)
            url = 'https://github.com/anthonyfok/fonts-wqy-microhei/raw/master/wqy-microhei.ttc'
            urllib.request.urlretrieve(url, BUNDLED_FONT)
            if os.path.exists(BUNDLED_FONT):
                f = ImageFont.truetype(BUNDLED_FONT, size)
                cmap = load_font_cmap(BUNDLED_FONT)
                fonts_with_cmaps.append((f, cmap))
                primary_loaded = True
        except Exception as dl_err:
            print(f"⚠️ 中文字型自動下載失敗: {dl_err}")

    # 2. 尋找萬用符號回退字型 (GNU Unifont，覆蓋所有 Unicode BMP 碼位，涵蓋各類顏文字與奇異符號)
    fallback_candidates = [
        UNIFONT_PATH,
        'C:/Windows/Fonts/seguisym.ttf',
        'C:/Windows/Fonts/segoeui.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
    ]

    unifont_loaded = False
    for path in fallback_candidates:
        if path and os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                cmap = load_font_cmap(path)
                fonts_with_cmaps.append((f, cmap))
                if path == UNIFONT_PATH:
                    unifont_loaded = True
                break
            except Exception:
                continue

    if not unifont_loaded and not os.path.exists(UNIFONT_PATH):
        try:
            import urllib.request
            os.makedirs(PROJECT_FONTS_DIR, exist_ok=True)
            url = 'https://ftp.gnu.org/gnu/unifont/unifont-15.1.05/unifont-15.1.05.otf'
            urllib.request.urlretrieve(url, UNIFONT_PATH)
            if os.path.exists(UNIFONT_PATH):
                f = ImageFont.truetype(UNIFONT_PATH, size)
                cmap = load_font_cmap(UNIFONT_PATH)
                fonts_with_cmaps.append((f, cmap))
        except Exception as dl_err:
            print(f"⚠️ Unifont 回退字型自動下載失敗: {dl_err}")

    if not fonts_with_cmaps:
        default_f = ImageFont.load_default()
        fonts_with_cmaps.append((default_f, set(range(128))))

    return FontRenderer(fonts_with_cmaps)


def get_font(size: int = 16, bold: bool = False) -> FontRenderer:
    """尋找並載入支援繁體/簡體中文及顏文字符號的多字型渲染器。"""
    return get_font_renderer(size=size, bold=bold)


def wrap_cell_text(text: str, font: FontRenderer | ImageFont.ImageFont, max_width: int) -> List[str]:
    """將單元格文字依最大寬度自動折行，避免欄位過度延伸。"""
    if not text:
        return ['']
    lines: List[str] = []
    for raw_line in text.split('\n'):
        cur = ''
        for char in raw_line:
            test = cur + char
            try:
                if hasattr(font, 'get_text_width'):
                    w = font.get_text_width(test)
                else:
                    bbox = font.getbbox(test)
                    w = bbox[2] - bbox[0]
            except Exception:
                w = len(test) * 10
            if w > max_width and cur:
                lines.append(cur)
                cur = char
            else:
                cur = test
        if cur:
            lines.append(cur)
    return lines or ['']


def strip_cell_markdown(s: str) -> str:
    """移除單元格內的粗體、斜體與代碼反引號。"""
    s = re.sub(r'\*\*(.*?)\*\*', r'\1', s)
    s = re.sub(r'\*(.*?)\*', r'\1', s)
    s = re.sub(r'`(.*?)`', r'\1', s)
    return s.strip()


def render_table_to_image(
    headers: List[str],
    aligns: List[str],
    rows: List[List[str]],
) -> io.BytesIO:
    """使用 Pillow 將 Markdown 表格繪製為現代 Discord 深色風格的精緻圖片，支援繁體中文與各國顏文字符號。"""
    header_font = get_font_renderer(17, bold=True)
    body_font = get_font_renderer(15, bold=False)

    max_col_w = 320
    pad_x = 18
    pad_y = 10
    line_h = 24

    col_count = len(headers)
    while len(aligns) < col_count:
        aligns.append('left')

    col_widths = [0] * col_count
    wrapped_headers: List[List[str]] = []

    for i, h in enumerate(headers):
        wlines = wrap_cell_text(h, header_font, max_col_w)
        wrapped_headers.append(wlines)
        w = max((header_font.get_text_width(l) for l in wlines), default=0)
        col_widths[i] = max(col_widths[i], w + pad_x * 2)

    wrapped_rows: List[List[List[str]]] = []
    row_heights: List[int] = []

    for row in rows:
        w_row: List[List[str]] = []
        max_lines = 1
        for c_idx in range(col_count):
            cell = row[c_idx] if c_idx < len(row) else ''
            wlines = wrap_cell_text(cell, body_font, max_col_w)
            w_row.append(wlines)
            max_lines = max(max_lines, len(wlines))
            w = max((body_font.get_text_width(l) for l in wlines), default=0)
            col_widths[c_idx] = max(col_widths[c_idx], w + pad_x * 2)
        wrapped_rows.append(w_row)
        row_heights.append(max(42, max_lines * line_h + pad_y * 2))

    header_h = max(46, max((len(l) for l in wrapped_headers), default=1) * line_h + pad_y * 2)

    margin = 16
    card_w = sum(col_widths)
    img_w = card_w + margin * 2
    img_h = header_h + sum(row_heights) + margin * 2 + 6

    # 建立圖片 (Discord 深色背景 #1e1f22)
    img = Image.new('RGB', (img_w, img_h), color=(30, 31, 35))
    draw = ImageDraw.Draw(img)

    x0, y0 = margin, margin
    x1, y1 = margin + card_w, margin + header_h + sum(row_heights) + 6

    # 卡片外框與底色 (#2b2d31, 邊框 #3e4147)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=12, fill=(43, 45, 49), outline=(62, 65, 71), width=1)

    # 頂部 Discord Blurple 裝飾條 (#5865F2)
    draw.rounded_rectangle([x0, y0, x1, y0 + 5], radius=2, fill=(88, 101, 242))

    # 表頭背景 (#35383e)
    draw.rectangle([x0, y0 + 5, x1, y0 + header_h], fill=(53, 56, 62))

    # 繪製表頭文字與垂直分割線
    cur_x = x0
    for c_idx, w in enumerate(col_widths):
        lines = wrapped_headers[c_idx]
        align = aligns[c_idx]
        cur_y = y0 + 5 + (header_h - 5 - len(lines) * line_h) // 2
        for l in lines:
            tw = header_font.get_text_width(l)
            if align == 'center':
                tx = cur_x + (w - tw) // 2
            elif align == 'right':
                tx = cur_x + w - pad_x - tw
            else:
                tx = cur_x + pad_x
            header_font.draw_text(draw, (tx, cur_y), l, fill=(255, 255, 255))
            cur_y += line_h
        cur_x += w
        if c_idx < col_count - 1:
            draw.line([(cur_x, y0 + 5), (cur_x, y1)], fill=(58, 61, 67), width=1)

    # 繪製資料列
    cur_y = y0 + header_h
    for r_idx, w_row in enumerate(wrapped_rows):
        rh = row_heights[r_idx]
        bg = (38, 40, 44) if r_idx % 2 == 1 else (43, 45, 49)
        draw.rectangle([x0, cur_y, x1, cur_y + rh], fill=bg)
        draw.line([(x0, cur_y), (x1, cur_y)], fill=(58, 61, 67), width=1)

        cur_x = x0
        for c_idx, w in enumerate(col_widths):
            lines = w_row[c_idx]
            align = aligns[c_idx]
            text_y = cur_y + (rh - len(lines) * line_h) // 2
            for l in lines:
                tw = body_font.get_text_width(l)
                if align == 'center':
                    tx = cur_x + (w - tw) // 2
                elif align == 'right':
                    tx = cur_x + w - pad_x - tw
                else:
                    tx = cur_x + pad_x
                # 星級符號以鮮黃金亮色標記，一般文字以淺灰白標記
                fill_c = (241, 196, 15) if ('★' in l or '☆' in l) else (220, 222, 225)
                body_font.draw_text(draw, (tx, text_y), l, fill=fill_c)
                text_y += line_h
            cur_x += w
        cur_y += rh

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


def extract_and_render_markdown_tables(text: str) -> Tuple[str, List[discord.File]]:
    """自動從文字中辨識 Markdown 表格，轉換為圖片附件 (discord.File) 並將原表格替換為提示標記。"""
    if not text or '|' not in text:
        return text, []

    lines = text.split('\n')
    output_lines: List[str] = []
    files: List[discord.File] = []
    i = 0
    in_code_block = False
    table_index = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith('```'):
            in_code_block = not in_code_block
            output_lines.append(line)
            i += 1
            continue

        if in_code_block:
            output_lines.append(line)
            i += 1
            continue

        if '|' in line and (i + 1 < len(lines)) and DELIM_PATTERN.match(lines[i + 1]):
            header_line = line
            delim_line = lines[i + 1]
            data_lines: List[str] = []
            j = i + 2
            while j < len(lines) and '|' in lines[j] and lines[j].strip():
                data_lines.append(lines[j])
                j += 1

            def split_row(l: str) -> List[str]:
                s = l.strip()
                if s.startswith('|'):
                    s = s[1:]
                if s.endswith('|'):
                    s = s[:-1]
                return [strip_cell_markdown(c) for c in s.split('|')]

            headers = split_row(header_line)
            delims = split_row(delim_line)
            aligns = []
            for d in delims:
                d = d.strip()
                if d.startswith(':') and d.endswith(':'):
                    aligns.append('center')
                elif d.endswith(':'):
                    aligns.append('right')
                else:
                    aligns.append('left')

            rows = [split_row(dl) for dl in data_lines]

            try:
                buf = render_table_to_image(headers, aligns, rows)
                table_index += 1
                filename = f"table_{table_index}.png"
                files.append(discord.File(fp=buf, filename=filename))
                output_lines.append(f"[[TABLE_IMAGE:{filename}]]")
            except Exception as render_err:
                print(f"⚠️ 表格轉圖片例外，保留原表格文字: {render_err}")
                output_lines.append(header_line)
                output_lines.append(delim_line)
                output_lines.extend(data_lines)

            i = j
        else:
            output_lines.append(line)
            i += 1

    return '\n'.join(output_lines), files

