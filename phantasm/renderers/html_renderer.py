"""基于 Playwright + Chromium 的 HTML→图片卡片渲染器。

相比 Pillow 直接绘制，HTML 方案能：
- 原生渲染**彩色 emoji**（通过 @font-face 引入 Noto Color Emoji）；
- 用 CSS flex/grid 实现更细腻的排版；
- 主题/圆角/字号等更易维护。

字体通过 ``@font-face`` 引入（CJK 用内置的 Noto 子集，emoji 用下载的 Noto Color Emoji），
不依赖容器已安装字体。

依赖：``pip install playwright && playwright install --with-deps chromium``（或 ``playwright install --with-deps chromium``）。
若未安装 / Chromium 不可用，上层会回退到 Pillow 渲染。
"""
from __future__ import annotations

import html as _html
import logging
import re
import uuid
from pathlib import Path
from typing import Optional

from ..config import ConfigManager
from ..models import Account, Post
from .theme import resolve_theme

EMOJI_FONT_URL = ("https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf")
_CJK_UNICODE_RANGE = "U+4E00-9FFF,U+3000-303F,U+FF00-FFEF,U+2000-206F,U+00A0-00FF,U+20-7F"


class HtmlCardRenderer:
    def __init__(self, config: ConfigManager, logger: logging.Logger, store_dir: Path):
        self.config = config
        self.logger = logger
        self.render_cfg = config.render
        self.send_cfg = config.send
        self.store_dir = Path(store_dir)
        self._pw = None
        self._browser = None
        self._cjk_font: Optional[Path] = None
        self._emoji_font: Optional[Path] = None
        self._font_ready = False

    # ----------------------------------------------------------------
    async def ensure_fonts(self, downloader) -> None:
        """确保字体文件就绪：CJK（内置/下载）与 emoji（下载）。"""
        if self._font_ready:
            return
        self._font_ready = True
        # CJK：内置 phantasm/fonts 优先，否则 data/fonts
        pkg_fonts = Path(__file__).resolve().parent.parent / "fonts"
        self._cjk_font = self._find_font(pkg_fonts) or self._find_font(self.store_dir / "fonts")
        # emoji
        fonts_dir = self.store_dir / "fonts"
        emoji_target = fonts_dir / "NotoColorEmoji.ttf"
        if not emoji_target.exists() and downloader:
            try:
                fonts_dir.mkdir(parents=True, exist_ok=True)
                self.logger.info("正在下载彩色 emoji 字体…")
                data = await downloader(EMOJI_FONT_URL)
                emoji_target.write_bytes(data)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"emoji 字体下载失败，emoji 可能无法渲染：{e}")
        if emoji_target.exists():
            self._emoji_font = emoji_target
        if not self._cjk_font:
            self.logger.warning("未找到中文字体，HTML 卡片中文可能显示为方框")

    async def warmup(self, downloader) -> None:
        """初始化自检：确保字体就绪并拉起 Chromium（不渲染）。失败会抛出，供上层定位。"""
        await self.ensure_fonts(downloader)
        await self._ensure_browser()

    @staticmethod
    def _find_font(folder: Path) -> Optional[Path]:
        try:
            if not folder.exists():
                return None
            for f in sorted(folder.iterdir()):
                if f.is_file() and f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    return f
        except OSError:
            return None
        return None

    # ----------------------------------------------------------------
    async def render(self, post: Post, account: Account, out_dir: Path) -> str:
        html_doc = self._build_html(post, account)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        # 文件名必须每次唯一：同一篇帖子可能被渲染两次（译文卡片 + 原文卡片），
        # 若按 (post_id, created_ts) 命名，第二次会覆盖第一次，导致主卡片显示原文。
        uniq = uuid.uuid4().hex[:6]
        html_path = out_dir / f"_card_{post.post_id[:16]}_{uniq}.html"
        html_path.write_text(html_doc, encoding="utf-8")
        out_png = out_dir / f"phantasm_{post.platform}_{post.post_id[:16]}_{uniq}.png"

        await self._ensure_browser()
        page = await self._browser.new_page(viewport={"width": 688, "height": 360, "deviceScaleFactor": 2})
        try:
            await page.goto(f"file://{html_path.resolve().as_posix()}")
            try:
                await page.evaluate("document.fonts.ready")
            except Exception:  # noqa: BLE001
                pass
            el = await page.query_selector("#card")
            if el is None:
                raise RuntimeError("未找到卡片元素 #card")
            await el.screenshot(path=str(out_png), type="png")
        finally:
            await page.close()
        try:
            html_path.unlink(missing_ok=True)  # 只保留截图
        except OSError:
            pass
        return str(out_png)

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"未安装 playwright：pip install playwright && playwright install --with-deps chromium（{e}）") from e
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(
                args=["--allow-file-access-from-files", "--disable-dev-shm-usage"])
        except Exception as e:  # noqa: BLE001
            await self._pw.stop()
            self._pw = None
            raise RuntimeError(
                f"Chromium 启动失败，请确认在容器内执行 playwright install --with-deps chromium（{e}）") from e

    async def close(self):
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    # ----------------------------------------------------------------
    def _build_html(self, post: Post, account: Account) -> str:
        mode_map = self.render_cfg.get("theme_mode") or {}
        dark = str(mode_map.get(post.platform, "light")).lower() in ("dark", "true", "1")
        theme = resolve_theme(post.platform, self.render_cfg.get("theme") or {},
                              dark=dark, dark_cfg=self.render_cfg.get("dark_theme") or {})
        if post.platform == "x":
            body = self._x_body(post, account, theme)
        else:
            body = self._bili_body(post, account, theme)
        return self._doc(body, theme, post.platform)

    def _doc(self, body: str, theme: dict, platform: str) -> str:
        cjk_uri = self._cjk_font.resolve().as_uri() if self._cjk_font else ""
        emoji_uri = self._emoji_font.resolve().as_uri() if self._emoji_font else ""
        radius = int(self.config.image.get("corner_radius", 24) or 0)
        bg = theme.get("background", "#FFFFFF")
        text = theme.get("text", "#0F1419")
        return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@font-face {{ font-family:'cjk'; src:url("{cjk_uri}"); }}
@font-face {{ font-family:'ph-emojis'; src:url("{emoji_uri}"); unicode-range:{_CJK_UNICODE_RANGE}; }}
body {{ margin:0; background:{bg}; font-family:'cjk','ph-emojis',sans-serif; color:{text};
        -webkit-font-smoothing:antialiased; }}
#card {{ width:640px; box-sizing:border-box; padding:24px; background:{bg}; position:relative; }}
{build_css(theme, radius, platform)}
</style></head><body>{body}</body></html>"""

    def _bili_body(self, post: Post, account: Account, theme: dict) -> str:
        name = _html.escape(post.author_name or account.name)
        title = _html.escape(post.extra.get("title") or "")
        content = _html.escape(post.content or "")
        media = self._media_html(post.media_urls)
        like = self._num(post.like_count)
        comment = self._num(post.stats.get("comment", 0))
        forward = self._num(post.stats.get("forward", 0))
        title_html = f'<div class="title">{title}</div>' if title else ""
        tr_badge = '<div class="tr-badge">译文</div>' if post.extra.get("translated") else ""
        return f"""
<div class="card" id="card">
  <div class="brand"><span class="brand-pill">Bilibili 动态</span></div>
  <div class="header">
    <img class="avatar" src="{self._img(post.author_avatar_url)}" />
    <div class="head-text">
      <div class="name">{name}</div>
      <div class="meta">UID {_html.escape(post.account_id)} · {_html.escape(post.created_at or '')}</div>
    </div>
  </div>
  {title_html}
  <div class="content">{tr_badge}{content}</div>
  {media}
  {self._quote_html(post)}
  <div class="stats"><span>点赞 {like}</span><span>评论 {comment}</span><span>转发 {forward}</span></div>
  <div class="wm">Phantasm · Bilibili 动态</div>
</div>"""

    def _x_body(self, post: Post, account: Account, theme: dict) -> str:
        name = _html.escape(post.author_name or account.name)
        content = _html.escape(post.content or "")
        media = self._media_html(post.media_urls)
        verified = '<svg class="v" width="20" height="20" viewBox="0 0 24 24"><path fill="#1d9bf0" d="M22.25 12c0-1.43-.88-2.67-2.19-3.34.46-1.39.2-2.9-.81-3.91s-2.52-1.27-3.91-.81c-.66-1.31-1.91-2.19-3.34-2.19s-2.67.88-3.33 2.19c-1.4-.46-2.91-.2-3.92.81s-1.26 2.52-.8 3.91c-1.31.67-2.2 1.91-2.2 3.34s.89 2.67 2.2 3.34c-.46 1.39-.21 2.9.8 3.91s2.52 1.26 3.91.81c.67 1.31 1.91 2.19 3.34 2.19s2.68-.88 3.34-2.19c1.39.45 2.9.2 3.91-.81s1.27-2.52.81-3.91c1.31-.67 2.19-1.91 2.19-3.34zm-11.71 4.2L6.8 12.46l1.41-1.42 2.26 2.26 4.8-5.23 1.47 1.36-6.2 6.77z"/></svg>' if post.extra.get("verified") else ""
        handle = _html.escape(post.author_handle or "@" + account.account_id)
        meta = _html.escape(f"{handle} · {post.created_at or ''}")
        reply = self._num(post.stats.get("reply", 0))
        repost = self._num(post.stats.get("repost", 0))
        like = self._num(post.like_count)
        quote = self._num(post.stats.get("quote", 0))
        quote_html = f'<span>引用 {quote}</span>' if quote else ""
        stats_html = (f'<div class="stats"><span>回复 {reply}</span><span>转发 {repost}</span>'
                      f'<span>喜欢 {like}</span>{quote_html}</div>') \
            if not post.extra.get("stats_unavailable") else ""
        rt_badge = ""
        if post.extra.get("is_retweet"):
            rt = _html.escape(post.extra.get("rt_author") or ("@" + post.account_id))
            rt_badge = f'<div class="rt-badge">转推 {rt}</div>'
        tr_badge = '<div class="tr-badge">译文</div>' if post.extra.get("translated") else ""
        return f"""
<div class="card xcard" id="card">
  <div class="brand"><span class="brand-pill">X / Twitter</span></div>
  <div class="header">
    <img class="avatar" src="{self._img(post.author_avatar_url)}" />
    <div class="head-text">
      <div class="name">{name} {verified}</div>
      <div class="meta">{meta}</div>
    </div>
  </div>
  {rt_badge}
  <div class="content">{tr_badge}{content}</div>
  {media}
  {self._quote_html(post)}
  {stats_html}
  <div class="wm">Phantasm · X / Twitter</div>
</div>"""

    def _media_html(self, urls) -> str:
        urls = [u for u in urls if u][: int(self.send_cfg.get("media_max", 4))]
        if not urls:
            return ""
        n = len(urls)
        cls = "grid2" if n == 2 else ("grid3" if n == 3 else ("grid4" if n == 4 else "grid1"))
        imgs = "".join(f'<img class="media" src="{self._img(u)}" />' for u in urls)
        return f'<div class="media-wrap {cls}">{imgs}</div>'

    def _quote_html(self, post) -> str:
        """把被引用的帖子渲染成主卡片内的「子卡片」（引用块）。"""
        author = post.extra.get("orig_author") or ""
        content = post.extra.get("orig_content") or ""
        if not (author or content):
            return ""
        avatar = post.extra.get("orig_avatar") or ""
        media = [u for u in (post.extra.get("orig_media") or []) if u][:2]
        av = f'<img class="q-avatar" src="{self._img(avatar)}" />' if avatar \
            else '<span class="q-avatar q-avatar-ph"></span>'
        imgs = "".join(f'<img class="q-media" src="{self._img(u)}" />' for u in media)
        name = _html.escape(author) if author else "被引用的帖子"
        return (f'<div class="quote">{av}<div class="q-body">'
                f'<div class="q-name">{name}</div>'
                f'<div class="q-text">{_html.escape(content[:500])}</div>'
                f'{imgs}</div></div>')

    @staticmethod
    def _img(url: str) -> str:
        if url.startswith("http://"):
            return url.replace("http://", "https://", 1)
        return url

    @staticmethod
    def _num(v):
        try:
            v = int(v or 0)
        except (TypeError, ValueError):
            return "0"
        if v >= 10000:
            return f"{v / 10000:.1f}万"
        if v >= 1000:
            return f"{v / 1000:.1f}K"
        return str(v)


def build_css(theme: dict, radius: int, platform: str) -> str:
    primary = theme.get("primary", "#000")
    accent = theme.get("accent", "#1d9bf0")
    sub = theme.get("subtext", "#536471")
    divider = theme.get("divider", "#eef1f4")
    text = theme.get("text", "#0f1419")
    media_bg = theme.get("media_bg", divider)
    badge_bg = theme.get("badge_bg", "#3F3F46")
    badge_fg = theme.get("badge_fg", "#FFFFFF")
    r = max(0, radius)
    return f"""
#card {{ font-family:'cjk','ph-emojis',sans-serif; }}
.brand {{ margin-bottom:14px; }}
.brand-pill {{ background:{primary}; color:#fff; padding:5px 14px; border-radius:999px; font-size:14px; font-weight:600; }}
.header {{ display:flex; align-items:center; gap:12px; margin-bottom:12px; }}
.avatar {{ width:48px; height:48px; border-radius:50%; object-fit:cover; background:#e6e8eb; }}
.head-text {{ line-height:1.35; }}
.name {{ font-weight:700; font-size:18px; display:flex; align-items:center; gap:4px; }}
.v {{ width:20px; height:20px; }}
.meta {{ color:{sub}; font-size:14px; margin-top:2px; }}
.rt-badge {{ margin:2px 0 8px; padding:6px 12px; border-left:3px solid {accent};
            background:{divider}; color:{sub}; font-size:13px; border-radius:6px; }}
.tr-badge {{ display:block; width:fit-content; margin:2px 0 10px; padding:4px 12px;
            border-radius:8px; background:{badge_bg}; color:{badge_fg};
            font-size:12px; font-weight:600; line-height:1.45; letter-spacing:.3px; }}
.quote {{ display:flex; gap:10px; margin:10px 0 4px; padding:12px;
         border:1px solid {divider}; border-radius:14px; background:{media_bg}; }}
.q-avatar {{ width:34px; height:34px; border-radius:50%; object-fit:cover; flex:0 0 34px; }}
.q-avatar-ph {{ display:inline-block; background:{divider}; }}
.q-body {{ flex:1; min-width:0; }}
.q-name {{ font-weight:600; font-size:15px; color:{text}; margin-bottom:4px; }}
.q-text {{ font-size:15px; line-height:1.5; color:{sub}; white-space:pre-wrap; word-break:break-word; }}
.q-media {{ width:100%; max-height:260px; object-fit:cover; border-radius:10px; margin-top:8px; }}
.title {{ font-size:22px; font-weight:700; margin:6px 0 4px; }}
.content {{ font-size:18px; line-height:1.6; white-space:pre-wrap; word-break:break-word; margin-bottom:12px; min-height:4px; }}
.media-wrap {{ display:grid; gap:8px; margin-bottom:12px; }}
.media-wrap.grid1 {{ grid-template-columns:1fr; }}
.media-wrap.grid2 {{ grid-template-columns:1fr 1fr; }}
.media-wrap.grid3 {{ grid-template-columns:1fr 1fr 1fr; }}
.media-wrap.grid4 {{ grid-template-columns:1fr 1fr; }}
.media {{ width:100%; aspect-ratio:1/1; object-fit:cover; border-radius:{r // 2}px; background:#eef1f4; }}
.stats {{ display:flex; gap:18px; color:{sub}; font-size:16px; padding-top:12px; border-top:1px solid {divider}; margin-top:4px; }}
.wm {{ margin-top:10px; color:{sub}; font-size:12px; }}
"""
