"""平台风格卡片渲染（Pillow 实现）。

:class:`CardRenderer` 负责：
- 按 ``post.platform`` 选择主题色板与版式（B 站 / X）；
- 异步下载头像与媒体图（失败用占位色块兜底）；
- 两阶段绘制：先测量版式（文本换行 / 媒体网格高度），再创建画布统一绘制。

布局固定宽度（:const:`CARD_W`），高度随内容自适应；界面元素包含：平台标识条、
作者头像/昵称/@handle/时间/认证徽标、正文、媒体网格、统计条与平台水印。
"""
from __future__ import annotations

import logging
import uuid
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from ..config import ConfigManager
from ..fetchers.base import sanitize_text
from ..models import Account, Post
from .base import (FontResolver, circle_avatar, cover_crop, format_count, rounded_rect,
                   rgb, wrap_text)
from .theme import resolve_theme

CARD_W = 640
PAD = 24
BRAND_H = 30
FOOTER_EXTRA = 18
GAP = 8

# 无中文字体时的兜底：首次渲染前尝试下载 Noto Sans CJK SC（OFL 许可，一次性、落盘于插件 data 目录）。
FONT_DOWNLOAD_URL = ("https://raw.githubusercontent.com/notofonts/noto-cjk/main/"
                     "Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf")


class CardRenderer:
    def __init__(self, config: ConfigManager, logger: logging.Logger, store_dir: Path | None = None):
        self.config = config
        self.logger = logger
        self.render_cfg = config.render
        self.image_cfg = config.image
        self.send_cfg = config.send
        self._store_dir = Path(store_dir) if store_dir else None
        self.fonts = FontResolver(override=str(self.render_cfg.get("font_path", "")), logger=logger)
        self._downloader: Optional[callable] = None
        self._font_ensured = False
        try:
            self._scale = max(0.8, min(1.5, float(self.render_cfg.get("font_size_scale", 1.0) or 1.0)))
        except (TypeError, ValueError):
            self._scale = 1.0
        self._radius = min(28, max(0, int(self.image_cfg.get("corner_radius", 24) or 0)))
        self._fmt = str(self.image_cfg.get("format", "png")).lower()

    def set_downloader(self, fn) -> None:
        """注入 ``async (url) -> bytes`` 下载器。"""
        self._downloader = fn

    # ----------------------------------------------------------------
    # 字体就绪（缺失时自动下载中文字体；失败降级默认字体并告警）
    # ----------------------------------------------------------------
    async def ensure_font(self, store_dir: Path | None = None,
                          downloader: Optional[callable] = None) -> None:
        self._font_ensured = True
        if store_dir:
            self._store_dir = Path(store_dir)
        if self.fonts.resolve():
            return  # 已能找到字体（render.font_path / 系统路径）

        # 1) 插件包内置字体目录 phantasm/fonts/
        pkg_fonts = Path(__file__).resolve().parent.parent / "fonts"
        found = self._first_font(pkg_fonts)
        # 2) 插件 data 目录的 fonts/（用户可自行放字体）
        if not found and self._store_dir:
            found = self._first_font(self._store_dir / "fonts")
        if found:
            self.fonts = FontResolver(override=str(found), logger=self.logger)
            self.logger.info(f"已使用字体：{found}")
            return

        dl = downloader or self._downloader
        if not dl or not self._store_dir:
            self.logger.warning("未找到中文字体且无可用下载器，中文可能显示为方框；"
                                "请设置 render.font_path 或把 .ttf/.otf 放进插件 data 目录 fonts/")
            return
        fonts_dir = self._store_dir / "fonts"
        try:
            fonts_dir.mkdir(parents=True, exist_ok=True)
            target = fonts_dir / "NotoSansCJKsc-Regular.otf"
            if not target.exists():
                self.logger.info("未找到中文字体，正在下载 Noto Sans CJK SC…")
                data = await dl(FONT_DOWNLOAD_URL)
                target.write_bytes(data)
            self.fonts = FontResolver(override=str(target), logger=self.logger)
            self.logger.info(f"中文字体已就绪：{target}")
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"中文字体下载失败，中文可能显示为方框：{e}。"
                                f"请手动放置字体到 {fonts_dir} 或设置 render.font_path")

    @staticmethod
    def _first_font(folder: Path) -> Optional[Path]:
        try:
            if not folder.exists():
                return None
            for f in sorted(folder.iterdir()):
                if f.is_file() and f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    return f
        except OSError:
            return None
        return None

    async def _ensure_font(self) -> None:
        if not self._font_ensured and (self._store_dir or self._downloader):
            await self.ensure_font(self._store_dir, self._downloader)
        else:
            self._font_ensured = True

    # ----------------------------------------------------------------
    async def render(self, post: Post, account: Account, out_dir: Path) -> str:
        await self._ensure_font()
        theme = resolve_theme(post.platform, self.render_cfg.get("theme") or {})
        canvas = await self._paint(post, account, theme)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = "jpg" if self._fmt in ("jpg", "jpeg") else "png"
        fname = f"phantasm_{post.platform}_{post.post_id[:16]}_{uuid.uuid4().hex[:6]}.{ext}"
        path = out_dir / fname
        if ext == "jpg":
            canvas.convert("RGB").save(path, quality=90)
        else:
            canvas.save(path)
        return str(path)

    # ----------------------------------------------------------------
    async def _paint(self, post: Post, account: Account, theme) -> Image.Image:
        is_x = post.platform == "x"

        # 字体
        body_font, body_bold = self._fonts(22)
        name_font, name_bold = self._fonts(20) if not is_x else self._fonts(21)
        meta_font, _ = self._fonts(15) if not is_x else self._fonts(16)
        stat_font, _ = self._fonts(18) if not is_x else self._fonts(17)
        brand_font, _ = self._fonts(15)

        # 主题色
        bg = rgb(theme.get("background", "#FFFFFF"))
        text_c = rgb(theme.get("text", "#0F1419"))
        sub_c = rgb(theme.get("subtext", "#536471"))
        accent = rgb(theme.get("accent" if is_x else "primary", "#1D9BF0"))
        divider = rgb(theme.get("divider", "#EFF3F4"))

        avatar_size = 48 if is_x else 46
        avatar = await self._avatar_image(post.author_avatar_url, avatar_size)

        # 下载媒体（最多 media_max 张）
        media_urls = [u for u in post.media_urls if u][: int(self.send_cfg.get("media_max", 4))]
        media_imgs: list[Image.Image] = []
        for u in media_urls:
            img = await self._load_image(u)
            media_imgs.append(img if img is not None else Image.new("RGB", (4, 4), rgb(theme.get("media_bg", "#F1F2F3"))))

        # ---- 测量版式 ----
        # 在渲染层再清洗一次（emoji/空白），避免抓取层漏网导致 font 渲染出“豆腐块”
        emoji_mode = str(self.render_cfg.get("emoji_mode", "strip"))
        content = sanitize_text(post.content, emoji_mode=emoji_mode)
        orig_content = sanitize_text(post.extra.get("orig_content") or "", emoji_mode=emoji_mode)

        measure = self._measure_draw()
        content_w = CARD_W - 2 * PAD
        body_lines = wrap_text(measure, content, body_font, content_w)
        body_lh = body_font.getmetrics()[0] + body_font.getmetrics()[1] + 4
        body_h = len(body_lines) * body_lh

        grid_layout = self._grid_layout(len(media_imgs))

        # bilibili 转发引用块
        quote_lines: list[str] = []
        quote_lh = 0
        if post.extra.get("orig_author"):
            qf = self.fonts.font(int(17 * self._scale))
            quote_txt = f"引用 @{post.extra['orig_author']}：{truncate(orig_content, 90)}"
            quote_lines = wrap_text(measure, quote_txt, qf, content_w - 20)
            quote_lh = qf.getmetrics()[0] + qf.getmetrics()[1] + 4
            self._quote_font = qf
            self._quote_txt = quote_txt

        brand_bottom = PAD + 22          # 平台标识胶囊底部
        header_y = brand_bottom + 8      # 头部与胶囊之间留 8px
        header_h = avatar_size
        body_y = header_y + header_h + 12
        media_top = body_y + body_h + (12 if body_h else 0)
        media_h = grid_layout["total_h"] if media_imgs else 0
        quote_y = media_top + media_h + (12 if (media_imgs and quote_lines) else (12 if quote_lines else 0))
        quote_h = (len(quote_lines) * quote_lh + 22) if quote_lines else 0
        stats_y = quote_y + quote_h + (12 if quote_h else 0)
        stat_h = 30
        show_link = bool(post.url) and bool(self.send_cfg.get("link_to_post", True))
        link_h = 20 if show_link else 0
        total_h = stats_y + stat_h + FOOTER_EXTRA + link_h + PAD

        canvas = Image.new("RGB", (CARD_W, max(int(total_h), 240)), bg)
        draw = ImageDraw.Draw(canvas)

        # ---- 平台标识条 ----
        label = "X / Twitter" if is_x else "Bilibili 动态"
        brand_accent = accent if is_x else rgb(theme.get("primary", "#FB7299"))
        self._brand_bar(draw, label, brand_font, brand_accent, PAD, PAD)

        # ---- 头部 ----
        canvas.paste(avatar, (PAD, header_y), avatar)
        nx = PAD + avatar_size + 12
        author_name = post.author_name or account.name
        draw.text((nx, header_y), author_name, font=name_bold, fill=text_c)
        # 认证徽标（X 风格蓝 V）
        name_w = int(name_bold.getlength(author_name))
        if is_x and post.extra.get("verified"):
            br = 8
            bx = nx + name_w + 8
            draw.ellipse((bx, header_y + 5, bx + 2 * br, header_y + 5 + 2 * br), fill=accent)
            vf = self.fonts.font(12)
            draw.text((bx + br * 0.5, header_y + 4), "✓", font=vf, fill=(255, 255, 255))
        meta_y = header_y + (avatar_size - name_font.getmetrics()[1])
        handle = post.author_handle or ("@" + account.account_id if is_x else f"UID {account.account_id}")
        meta_text = f"{handle} · {post.created_at}" if post.created_at else handle
        draw.text((nx, meta_y), meta_text, font=meta_font, fill=sub_c)

        # ---- 正文 ----
        self._draw_wrapped(draw, content, body_font, text_c, PAD, body_y, content_w, body_lh)

        # ---- 媒体 ----
        if media_imgs:
            self._draw_grid(draw, canvas, media_imgs, media_top, grid_layout, theme,
                            aspect_ratio=self._aspect_ratio(self.image_cfg.get("aspect_ratio", "1:1")))

        # ---- 引用块（bilibili 转发） ----
        if quote_lines:
            qbg = rgb("#F1F2F3")
            rounded_rect(draw, (PAD, quote_y, CARD_W - PAD, quote_y + quote_h), radius=10, fill=qbg)
            cy = quote_y + 10
            for ln in quote_lines:
                draw.text((PAD + 10, cy), ln, font=self._quote_font, fill=sub_c)
                cy += quote_lh

        # ---- 统计条 ----
        self._stats(draw, post, stat_font, sub_c, stats_y, is_x)
        rule_y = stats_y + stat_h - 4
        draw.line([(PAD, rule_y), (CARD_W - PAD, rule_y)], fill=divider, width=1)
        wm_font = self.fonts.font(14)
        draw.text((PAD, stats_y + stat_h), f"Phantasm · {label}", font=wm_font, fill=sub_c)
        if show_link and post.url:
            y_link = stats_y + stat_h + 20
            lf = self.fonts.font(15)
            link_text = f"原帖：{post.url}"
            # 超宽则按字符截断，保证不超出右边界
            max_w = CARD_W - 2 * PAD
            if lf.getlength(link_text) > max_w:
                while link_text and lf.getlength(link_text + "…") > max_w:
                    link_text = link_text[:-1]
                link_text += "…"
            draw.text((PAD, y_link), link_text, font=lf, fill=rgb(theme.get("accent", "#1D9BF0")))
        return canvas

    # ----------------------------------------------------------------
    # 绘制原语
    # ----------------------------------------------------------------
    def _fonts(self, size: int):
        f = self.fonts.font(int(size * self._scale))
        return f, f

    def _measure_draw(self) -> ImageDraw.ImageDraw:
        return ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def _brand_bar(self, draw, label, font, accent, y, x0):
        tw = int(font.getlength(label))
        pill_w = tw + 26
        rounded_rect(draw, (x0, y, x0 + pill_w, y + 22), radius=11, fill=accent)
        draw.text((x0 + 13, y + 3), label, font=font, fill=(255, 255, 255))

    def _draw_wrapped(self, draw, text, font, fill, x, y, max_width, line_h):
        for ln in wrap_text(draw, text, font, max_width):
            draw.text((x, y), ln, font=font, fill=fill)
            y += line_h

    def _grid_layout(self, n: int) -> dict:
        if n <= 0:
            return {"cols": 0, "rows": 0, "cell_w": 0, "cell_h": 0, "total_h": 0}
        g = GAP
        width = CARD_W - 2 * PAD
        ratio = self._aspect_ratio(self.image_cfg.get("aspect_ratio", "1:1"))
        if n == 1:
            cols, rows = 1, 1
            cw = width
        elif n == 3:
            # 三图：一行三列，避免 2x2 出现空洞
            cols, rows = 3, 1
            cw = (width - 2 * g) // 3
        else:
            cols, rows = 2, (2 if n >= 3 else 1)
            cw = (width - g) // 2
        ch = int(cw * ratio)
        total_h = rows * ch + (rows - 1) * g
        return {"cols": cols, "rows": rows, "cell_w": cw, "cell_h": ch, "total_h": total_h}

    def _draw_grid(self, draw, canvas, images, top, layout, theme, aspect_ratio):
        cols = layout["cols"]; rows = layout["rows"]
        cw = layout["cell_w"]; ch = layout["cell_h"]
        g = GAP
        n = min(len(images), cols * rows)
        for i in range(n):
            row, col = divmod(i, cols)
            x = PAD + col * (cw + g)
            y = top + row * (ch + g)
            img = images[i]
            if img.size == (4, 4):  # 占位
                cover = Image.new("RGB", (cw, ch), rgb(theme.get("media_bg", "#F1F2F3")))
            else:
                cover = cover_crop(img, cw, ch, radius=self._radius // 2)
            if cover.mode == "RGBA":
                canvas.paste(cover, (x, y), cover)
            else:
                canvas.paste(cover, (x, y))
            rounded_rect(draw, (x, y, x + cw, y + ch),
                         radius=self._radius // 2,
                         outline=rgb(theme.get("divider", "#E3E5E7")), width=1)
        # 视频封面角标：时长
        return layout

    def _stats(self, draw, post, font, color, y, is_x):
        if is_x:
            reply = format_count(post.stats.get("reply", 0))
            repost = format_count(post.stats.get("repost", 0))
            like = format_count(post.like_count)
            quote = format_count(post.stats.get("quote", 0))
            parts = [f"回复 {reply}", f"转发 {repost}", f"喜欢 {like}"]
            if quote:
                parts.append(f"引用 {quote}")
        else:
            play = post.stats.get("play", 0)
            parts = []
            if play:
                parts.append(f"播放 {format_count(play)}")
            parts.append(f"点赞 {format_count(post.like_count)}")
            parts.append(f"评论 {format_count(post.stats.get('comment', 0))}")
            parts.append(f"转发 {format_count(post.stats.get('forward', 0))}")
        x = PAD
        for p in parts:
            w = draw.textbbox((0, 0), p, font=font)[2]
            draw.text((x, y), p, font=font, fill=color)
            x += w + 18

    # ----------------------------------------------------------------
    # 异步图片加载
    # ----------------------------------------------------------------
    async def _load_image(self, url: str) -> Optional[Image.Image]:
        if not url or self._downloader is None:
            return None
        try:
            data = await self._downloader(url)
            img = Image.open(BytesIO(data))
            img.load()
            return img
        except Exception as e:  # noqa: BLE001
            self.logger.debug(f"媒体图下载失败（回退占位）：{e}")
            return None

    async def _avatar_image(self, url: str, size: int) -> Image.Image:
        img = await self._load_image(url)
        if img is None:
            img = Image.new("RGB", (size, size), (220, 222, 225))
        return circle_avatar(img, size)

    def _aspect_ratio(self, aspect: str) -> float:
        mapping = {"1:1": 1.0, "4:3": 3 / 4, "3:4": 4 / 3, "16:9": 9 / 16,
                   "9:16": 16 / 9, "3:2": 2 / 3, "2:3": 3 / 2}
        return mapping.get((aspect or "1:1").strip(), 1.0)


def truncate(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"
