"""基于 Pillow 的卡片渲染基础工具。

提供：
- :class:`FontResolver`：解析可用的中文字体并按字号缓存；
- 文本换行、圆角矩形、圆形头像、封面裁切（cover-fit）等绘制原语。

设计目标：不引入浏览器/无头依赖，纯 Python 直接绘制，中文/英文正常；
emoji 由 :func:`~phantasm.fetchers.base.sanitize_text` 按 ``emoji_mode`` 处理
（Pillow 无法原生渲染彩色 emoji，默认 ``strip`` 移除）。
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# 常见中文字体搜索路径（按优先级）
FONT_CANDIDATES = [
    # Windows（原生路径，AstrBot 运行于 Windows 时）
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/Deng.ttf",
    # WSL / Windows 挂载路径
    "/mnt/c/Windows/Fonts/msyh.ttc",
    "/mnt/c/Windows/Fonts/msyhbd.ttc",
    "/mnt/c/Windows/Fonts/simhei.ttf",
    "/mnt/c/Windows/Fonts/simsun.ttc",
    "/mnt/c/Windows/Fonts/Deng.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

# 常规字体的常见回退（仅拉丁字符，作为最后保底）
_STANDARD_FALLBACKS = {
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
}


class FontResolver:
    def __init__(self, override: str = "", logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("phantasm")
        self._path: str = ""
        self._resolved = False
        self._override = (override or "").strip()

    def resolve(self) -> str:
        if self._resolved:
            return self._path
        if self._override and Path(self._override).is_file():
            self._path = self._override
            self._resolved = True
            return self._path
        for cand in FONT_CANDIDATES:
            if Path(cand).is_file():
                self._path = cand
                self._resolved = True
                return self._path
        self._resolved = True
        return ""

    def font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        path = self.resolve()
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"加载字体失败 {path}: {e}")
        return ImageFont.load_default()


def rgb(value: str | None, fallback: str = "#000000") -> tuple[int, int, int]:
    from .theme import hex_to_rgb  # 避免循环导入
    return hex_to_rgb(value, fallback)


def rounded_rect(draw: ImageDraw.ImageDraw, box, radius: int,
                 fill: tuple | None = None, outline: tuple | None = None, width: int = 1):
    draw.rounded_rectangle(list(box), radius=radius, fill=fill, outline=outline, width=width)


def circle_avatar(img: Image.Image, size: int) -> Image.Image:
    """把图片裁切成尺寸为 ``size`` 的圆形头像（cover-fit）。"""
    img = img.convert("RGB")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse((0, 0, size, size), fill=255)
    img.putalpha(mask)
    return img


def cover_crop(img: Image.Image, box_w: int, box_h: int, radius: int = 0) -> Image.Image:
    """按 box 尺寸做 cover-fit（裁切居中）并加圆角。"""
    img = img.convert("RGB")
    w, h = img.size
    if w <= 0 or h <= 0:
        img = Image.new("RGB", (box_w, box_h), (200, 200, 200))
    else:
        target_ratio = box_w / box_h
        src_ratio = w / h
        if src_ratio > target_ratio:  # 过宽，裁左右
            new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            img = img.crop((left, 0, left + new_w, h))
        else:  # 过高，裁上下
            new_h = int(w / target_ratio)
            top = (h - new_h) // 2
            img = img.crop((0, top, 0 + w, top + new_h))
        img = img.resize((box_w, box_h), Image.LANCZOS)
    if radius > 0:
        mask = Image.new("L", (box_w, box_h), 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.rounded_rectangle((0, 0, box_w, box_h), radius=radius, fill=255)
        out = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        return out
    return img


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """中英混排按字符换行，遇换行符强制断行。返回行列表。"""
    lines: list[str] = []
    for para in (text or "").split("\n"):
        para = para.rstrip()
        if not para:
            lines.append("")
            continue
        cur = ""
        for ch in para:
            if ch == "\t":
                ch = "    "
            test = cur + ch
            if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = ch.lstrip()
                if cur and draw.textbbox((0, 0), cur, font=font)[2] > max_width:
                    # 单个字符也超宽，硬塞
                    pass
        if cur:
            lines.append(cur)
    return lines


def measure_text_height(draw, text: str, font, max_width: int, line_gap: int) -> int:
    lines = wrap_text(draw, text, font, max_width)
    ascent, descent = font.getmetrics()
    return int(len(lines) * (ascent + descent) + max(0, len(lines) - 1) * line_gap)


def truncate(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def format_count(value, suffix=""):
    """把数字格式化为 1.2万 / 1.3K 之类。"""
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        return f"0{suffix}"
    if v >= 10000:
        w = v / 10000
        return f"{w:.1f}万{suffix}" if w < 100 else f"{round(w)}万{suffix}"
    if v >= 1000:
        return f"{v / 1000:.1f}K{suffix}"
    return f"{v}{suffix}"
