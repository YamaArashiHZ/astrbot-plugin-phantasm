"""基于 RSSHub 的 X/Twitter 推文抓取器（不消耗 X 官方 API 额度）。

数据源：RSSHub 的 ``/twitter/user/:screen_name``（需一个可达的 RSSHub 实例）。

  GET <rss_base>/twitter/user/<screen_name>

对比官方式：
- 不占用 X 官方 credits，免费；
- 依赖 RSSHub 实例可用性，偶尔可能超时/限流/失效；
- 内容来自 RSS（标题/描述/日期/链接/媒体图片）。

配置：``credentials.x.mode`` 填 ``rss``，``credentials.x.rss_base`` 填 RSSHub 实例地址
（默认 ``https://rsshub.app``，失败可换自建实例或其它公共实例）。
"""
from __future__ import annotations

import html as _h
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from ..config import ConfigManager
from ..http import HttpClient, FetchError
from ..models import Account, FetchResult, Post
from .base import BaseFetcher, sanitize_text

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_VIDEO_POSTER_RE = re.compile(r'<video[^>]+poster=["\']([^"\']+)["\']', re.IGNORECASE)
_VIDEO_SRC_RE = re.compile(r'<video[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_STATUS_RE = re.compile(r"/status/(\d+)")


class XRssFetcher(BaseFetcher):
    platform = "x"
    display_label = "X (RSS)"

    async def fetch_recent(self, limit: int = 20) -> FetchResult:
        creds = self.config.credential("x") or {}
        if str(creds.get("mode", "api")).lower() != "rss":
            return self.err_result("X mode 需为 rss")
        rss_base = str(creds.get("rss_base") or "https://rsshub.app").strip().rstrip("/")
        if not rss_base:
            rss_base = "https://rsshub.app"
        screen = self.account.account_id.strip()

        url = f"{rss_base}/twitter/user/{screen}"
        try:
            buf = await self.http.get_bytes(url, headers={"User-Agent": UA})
        except FetchError as e:
            return self.err_result(f"RSSHub 抓取失败：{e}")

        xml = buf.decode("utf-8", errors="ignore")
        if "<rss" not in xml.lower() and "<feed" not in xml.lower():
            return self.err_result(f"RSSHub 未返回 RSS（{rss_base}）：实例不可用或不支持该路由")

        posts = self._parse_rss(xml, screen)
        raw_count = len(posts)
        posts = self.apply_account_filters(posts)     # 按账号配置过滤转推
        posts.sort(key=lambda p: p.created_ts, reverse=True)
        if raw_count == 0:
            # RSSHub 在 auth_token 失效时会返回 200 + 空 feed（不报错），需给出结论与修复入口
            self.logger.warning(
                f"[{self.account.key}] RSSHub 返回 0 条推文（多为 Auth_Token 失效）。"
                f"排查 `docker logs rsshub | grep 'is not valid'`；"
                f"修复：WebUI「凭据 / 网络」更新 RSSHub Auth_Token")
        return FetchResult(posts=posts[:limit], total=len(posts))

    # ----------------------------------------------------------------
    def _parse_rss(self, xml: str, screen: str) -> list[Post]:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            self.logger.warning(f"RSS 解析失败：{e}")
            return []
        # 频道级头像：RSSHub 在 <channel><image><url> 里给用户头像
        channel_avatar = ""
        try:
            img = root.find("channel/image/url")
            if img is not None and img.text:
                channel_avatar = img.text.strip()
        except Exception:  # noqa: BLE001
            pass
        posts: list[Post] = []
        for item in list(root.iter("item"))[:40]:
            try:
                posts.append(self._parse_item(item, screen, channel_avatar))
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"解析 RSS 条目失败：{e}")
        return [p for p in posts if p]

    def _parse_item(self, item: ET.Element, screen: str, channel_avatar: str = "") -> Optional[Post]:
        text = lambda tag: (item.findtext(tag) or "").strip()  # noqa: E731
        title = text("title")
        desc = text("description")
        pub = text("pubDate")
        link = text("link")
        author = text("author") or screen   # RSSHub 的 <author> 是显示名(如"雫丶Shizuku✨DUCEXY")

        # 正文：description 用 <br> 保留换行；title 是压平文本。优先 description 还原换行。
        # 引用帖：RSSHub 用 <div class="rsshub-quote">…</div> 包住被引用的推文，先拆出来
        desc_main, quote = self._split_quote(desc)
        content = self._desc_to_content(desc_main, title)

        # 媒体图：主正文的 img src + 视频海报；引用帖的图归到 orig_media
        media_urls = self._extract_media_urls(desc_main)

        post_id = ""
        m = _STATUS_RE.search(link)
        if m:
            post_id = m.group(1)
        if not post_id:
            post_id = re.sub(r"\W+", "_", title)[:24] or f"rss_{abs(hash((link, title))) & 0xFFFFFF:x}"

        url = link or f"https://x.com/{screen}/status/{post_id}"
        ts = self._parse_pub(pub)

        # 转推检测：渲染时显示"转推 @原作者"标识
        is_rt = bool(re.match(r"^RT\s", title) or re.match(r"^RT\s", desc))
        rt_author = ""
        if is_rt:
            m = re.match(r"^RT\s+(@?[^:：\n]{1,60})\s*[:：]", title) \
                or re.match(r"^RT\s+(@?[^:：\n]{1,60})\s*[:：]", desc)
            if m:
                rt_author = m.group(1).strip()
        extra = {"rss": True, "link": link, "stats_unavailable": True}
        if is_rt:
            extra["is_retweet"] = True
            extra["rt_author"] = rt_author
        if quote:
            extra["orig_author"] = quote.get("author") or ""
            extra["orig_content"] = quote.get("content") or ""
            if quote.get("media"):
                extra["orig_media"] = quote["media"]
            if quote.get("avatar"):
                extra["orig_avatar"] = quote["avatar"]

        return Post(
            platform="x",
            account_id=screen,
            account_name=author,
            post_id=post_id,
            author_name=author,
            author_handle=f"@{screen}",
            author_avatar_url=channel_avatar,
            content=content,
            created_at=self._format_cn(pub),
            created_ts=ts,
            media_urls=media_urls,
            stats={},
            url=url,
            source_type="rss",
            extra=extra,
        )

    @staticmethod
    def _extract_media_urls(html: str) -> list[str]:
        """抽取 HTML 里的图片/视频封面（URL 先做 HTML 解码，否则下载 404）。"""
        out: list[str] = []
        for u in _IMG_RE.findall(html or ""):
            u = _h.unescape(u)
            if u.startswith("http") and u not in out:
                out.append(u)
        for u in _VIDEO_POSTER_RE.findall(html or ""):
            u = _h.unescape(u)
            if u.startswith("http") and u not in out:
                out.append(u)
        return out

    @staticmethod
    def _split_quote(desc: str) -> tuple[str, dict]:
        """把 description 里的引用帖 ``<div class="rsshub-quote">…</div>`` 拆出来。

        返回 (主正文 HTML, 引用信息 {author, content, media, avatar})。
        """
        m = re.search(r'<div class="rsshub-quote">(.*?)</div>', desc or "",
                      re.DOTALL | re.IGNORECASE)
        if not m:
            return desc or "", {}
        qhtml = m.group(1)
        main = (desc or "").replace(m.group(0), "")
        main = re.sub(r"(?:\s*<hr\s*/?>\s*)+$", "", main, flags=re.IGNORECASE)
        media: list[str] = []
        avatar = ""
        for tag in re.findall(r"<img[^>]*>", qhtml, re.IGNORECASE):
            src = re.search(r'src="([^"]+)"', tag, re.IGNORECASE)
            if not src:
                continue
            u = _h.unescape(src.group(1))
            if not u.startswith("http"):
                continue
            size = re.search(r'(?:width|height)="?(\d+)', tag, re.IGNORECASE)
            if not avatar and size and int(size.group(1)) <= 48:
                avatar = u          # 小尺寸 => 引用作者头像
            elif u not in media:
                media.append(u)
        qtext = _TAG_RE.sub("", qhtml)
        qtext = _h.unescape(qtext).replace("\u2002", " ").replace("&ensp;", " ")
        qtext = re.sub(r"\s+", " ", qtext).strip()
        qauthor, qcontent = "", qtext
        if ":" in qtext:
            qauthor, _, qcontent = qtext.partition(":")
            qauthor, qcontent = qauthor.strip(), qcontent.strip()
        return main, {"author": qauthor, "content": qcontent, "media": media, "avatar": avatar}

    @staticmethod
    def _desc_to_content(desc: str, title_fallback: str) -> str:
        """把 RSS description(HTML) 转成保留换行的正文；标签剥除、实体解码。"""
        import html as _h
        if desc:
            s = re.sub(r"<br\s*/?>", "\n", desc, flags=re.IGNORECASE)
            s = re.sub(r"</(p|div|li|blockquote)>", "\n", s, flags=re.IGNORECASE)
            s = _TAG_RE.sub("", s)
            s = _h.unescape(s)
            s = re.sub(r"[ \t]+", " ", s)
            s = re.sub(r"\n\s*\n+", "\n", s)          # 折叠连续空行
            s = s.strip("\n").strip()
            if s:
                return sanitize_text(XRssFetcher._strip_rt_prefix(s), emoji_mode="keep")
        return sanitize_text(XRssFetcher._strip_rt_prefix(title_fallback), emoji_mode="keep")

    @staticmethod
    def _strip_rt_prefix(text: str) -> str:
        """去掉转推前缀，避免卡片正文开头出现一串 RT 信息。

        兼容 ``RT @user:``、``RT 名字:``、``RT 名字``（无冒号）等形态。
        """
        s = text.strip()
        # 去 "RT @user:" / "RT 名字:"（冒号形式，含中文/括号）
        s = re.sub(r"^RT\s+@?[^:\n]{1,80}:\s*", "", s)
        # 无冒号：去掉 "RT " 标记，保留原内容
        if s.startswith("RT "):
            s = s[3:].lstrip()
        return s or text.strip()

    @staticmethod
    def _format_cn(pub: str) -> str:
        """把 pubDate(GMT/UTC) 转成 UTC+8 的可读时间(卡片与文字都用它)。"""
        ts = XRssFetcher._parse_pub(pub)
        if ts <= 0:
            return (pub or "").strip()[:25]
        try:
            dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            return (pub or "").strip()[:25]

    @staticmethod
    def _parse_pub(pub: str) -> float:
        if not pub:
            return 0.0
        try:
            return parsedate_to_datetime(pub).timestamp()
        except Exception:  # noqa: BLE001
            return 0.0
