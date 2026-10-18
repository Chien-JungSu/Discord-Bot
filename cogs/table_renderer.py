from __future__ import annotations

import io
import os
import re
from typing import List, Tuple, Optional

import discord
from PIL import Image, ImageDraw, ImageFont

DELIM_PATTERN = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$')


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_FONTS_DIR = os.path.join(BASE_DIR, 'static', 'fonts')
BUNDLED_FONT = os.path.join(PROJECT_FONTS_DIR, 'wqy-microhei.ttc')


def get_font(size: int = 16, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """尋找並載入支援繁體/簡體中文的中文字型，支援 Windows、Linux (如 Render/Docker) 與 macOS。"""
    candidates = [
        # Windows 微軟正黑體 (繁體中文最佳相容)
        'C:/Windows/Fonts/msjhbd.ttc' if bold else 'C:/Windows/Fonts/msjh.ttc',
        # 專案自帶開源微米黑字型 (確保 Render / Linux 雲端環境 100% 繁簡中文正常顯示)
        BUNDLED_FONT,
        # Windows 微軟雅黑
        'C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc',
        # Linux Noto Sans CJK
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc' if bold else '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        # Linux 文泉驛微米黑 / 正黑
        '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        # Linux Droid Sans Fallback
        '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
        # macOS 蘋方 / 黑體
        '/System/Library/Fonts/PingFang.ttc',
        '/System/Library/Fonts/STHeiti Light.ttc',
        # Windows 新細明體 / 宋體
        'C:/Windows/Fonts/mingliu.ttc',
        'C:/Windows/Fonts/simsun.ttc',
    ]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    # 若上述皆未找到且自帶字型檔案遺失，嘗試線上自動下載微米黑字型
    if not os.path.exists(BUNDLED_FONT):
        try:
            import urllib.request
            os.makedirs(PROJECT_FONTS_DIR, exist_ok=True)
            url = 'https://github.com/anthonyfok/fonts-wqy-microhei/raw/master/wqy-microhei.ttc'
            urllib.request.urlretrieve(url, BUNDLED_FONT)
            if os.path.exists(BUNDLED_FONT):
                return ImageFont.truetype(BUNDLED_FONT, size)
        except Exception as dl_err:
            print(f"⚠️ 中文字型自動下載失敗: {dl_err}")

    try:
        return ImageFont.load_default()
    except Exception:
        return None


def wrap_cell_text(text: str, font: ImageFont.ImageFont, max_width: int) -> List[str]:
    """將單元格文字依最大寬度自動折行，避免欄位過度延伸。"""
    if not text:
        return ['']
    lines: List[str] = []
    for raw_line in text.split('\n'):
        cur = ''
        for char in raw_line:
            test = cur + char
            try:
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
    """使用 Pillow 將 Markdown 表格繪製為現代 Discord 深色風格的精緻圖片。"""
    header_font = get_font(17, bold=True)
    body_font = get_font(15, bold=False)

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
        w = max((header_font.getbbox(l)[2] - header_font.getbbox(l)[0] for l in wlines), default=0)
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
            w = max((body_font.getbbox(l)[2] - body_font.getbbox(l)[0] for l in wlines), default=0)
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
            tw = header_font.getbbox(l)[2] - header_font.getbbox(l)[0]
            if align == 'center':
                tx = cur_x + (w - tw) // 2
            elif align == 'right':
                tx = cur_x + w - pad_x - tw
            else:
                tx = cur_x + pad_x
            draw.text((tx, cur_y), l, font=header_font, fill=(255, 255, 255))
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
                tw = body_font.getbbox(l)[2] - body_font.getbbox(l)[0]
                if align == 'center':
                    tx = cur_x + (w - tw) // 2
                elif align == 'right':
                    tx = cur_x + w - pad_x - tw
                else:
                    tx = cur_x + pad_x
                # 星級符號以鮮黃金亮色標記，一般文字以淺灰白標記
                fill_c = (241, 196, 15) if ('★' in l or '☆' in l) else (220, 222, 225)
                draw.text((tx, text_y), l, font=body_font, fill=fill_c)
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

