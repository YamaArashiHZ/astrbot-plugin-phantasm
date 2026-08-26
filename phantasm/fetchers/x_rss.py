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

import logging
import re
import xml.etree.ElementTree as ET
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
_STATUS_RE = re.compile(r"/status/(\d+)")


class XRssFetcher(BaseFetcher):
    platform = "x"
    display_label = "X (RSS)"

    async def fetch_recent(self, limit: int = 20) -> FetchResult:
        creds = self.config.credential("x") or {}
        if str(creds.get("mode", "api")).lower() != "rss":
            return self.err_result("X 配置 mode 不是 rss，请设置 credentials.x.mode=rss")
        rss_base = str(creds.get("rss_base") or "https://rsshub.app").strip().rstrip("/")
        if not rss_base:
            rss_base = "https://rsshub.app"
        screen = self.account.account_id.strip()

        url = f"{rss_base}/twitter/user/{screen}"
        try:
            buf = await self.http.get_bytes(url, headers={"User-Agent": UA})
        except FetchError as e:
            return self.err_result(
                f"RSS 抓取失败：{e}（请确认 RSSHub 实例可达且支持 twitter/user 路由）")

        xml = buf.decode("utf-8", errors="ignore")
        if "<rss" not in xml.lower() and "<feed" not in xml.lower():
            return self.err_result(
                f"实例 {rss_base} 未返回 RSS（可能 404/被限流，或该实例不支持 twitter/user 路由）。"
                f"请换可达实例或自建 RSSHub。")

        posts = self._parse_rss(xml, screen)
        posts.sort(key=lambda p: p.created_ts, reverse=True)
        return FetchResult(posts=posts[:limit], total=len(posts))

    # ----------------------------------------------------------------
    def _parse_rss(self, xml: str, screen: str) -> list[Post]:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            self.logger.warning(f"RSS 解析失败：{e}")
            return []
        posts: list[Post] = []
        for item in list(root.iter("item"))[:40]:
            try:
                posts.append(self._parse_item(item, screen))
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"解析 RSS 条目失败：{e}")
        return [p for p in posts if p]

    def _parse_item(self, item: ET.Element, screen: str) -> Optional[Post]:
        text = lambda tag: (item.findtext(tag) or "").strip()  # noqa: E731
        title = text("title")
        desc = text("description")
        pub = text("pubDate")
        link = text("link")

        # 正文：优先 title(RSSHub 通常即推文文本)，其次 description 去 HTML
        content = sanitize_text(title, emoji_mode="keep") or ""
        if not content:
            desc_text = _TAG_RE.sub("", desc)
            content = sanitize_text(desc_text, emoji_mode="keep")

        # 媒体图：从 description 里抽 img src（RSSHub 会内嵌 pbs.twimg.com 图片）
        media_urls = [u for u in _IMG_RE.findall(desc) if u.startswith("http")]
        # 去重
        media_urls = list(dict.fromkeys(media_urls))

        post_id = ""
        m = _STATUS_RE.search(link)
        if m:
            post_id = m.group(1)
        if not post_id:
            post_id = re.sub(r"\W+", "_", title)[:24] or f"rss_{abs(hash((link, title))) & 0xFFFFFF:x}"

        url = link or f"https://x.com/{screen}/status/{post_id}"
        ts = self._parse_pub(pub)

        return Post(
            platform="x",
            account_id=screen,
            account_name=screen,
            post_id=post_id,
            author_name=screen,
            author_handle=f"@{screen}",
            author_avatar_url="",
            content=content,
            created_at=pub[:25] or "",
            created_ts=ts,
            media_urls=media_urls,
            stats={},
            url=url,
            source_type="rss",
            extra={"rss": True, "link": link},
        )

    @staticmethod
    def _parse_pub(pub: str) -> float:
        if not pub:
            return 0.0
        try:
            return parsedate_to_datetime(pub).timestamp()
        except Exception:  # noqa: BLE001
            return 0.0
