"""抓取器基类与通用小工具。"""
from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..config import ConfigManager
from ..http import HttpClient
from ..models import Account, FetchResult, Post

# 常见中文时间的解析格式（bilibili 等）
_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
)

_CN_MD_RE = re.compile(r"^(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?")

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF\u2190-\u21FF\u2700-\u27BF]"
)


def parse_time_to_ts(value: str, fallback: float = 0.0, tz_utc8: bool = True) -> float:
    """尽力把平台时间字符串转成时间戳（秒）；失败返回 fallback。

    解析不了时对 ``08月06日`` 这类相对文本给出「今年」的猜测时间。
    """
    if not value:
        return fallback
    v = str(value).strip()
    # ISO8601（X 等）：2026-08-06T12:00:00.000Z
    if re.match(r"^\d{4}-\d{2}-\d{2}T", v):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            pass
    # 纯时间戳字符串（秒）
    if v.isdigit() and len(v) >= 10:
        try:
            return float(v)
        except ValueError:
            pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(v, fmt).timestamp()
        except ValueError:
            continue
    # 相对日期 'MM月DD日' / 'MM月DD日 HH:MM'（bilibili），猜测为今年
    m = _CN_MD_RE.match(v)
    if m:
        now = datetime.now()
        month, day = int(m.group(1)), int(m.group(2))
        hour = int(m.group(3) or 0)
        minute = int(m.group(4) or 0)
        try:
            dt = datetime(now.year, month, day, hour, minute)
            if dt.timestamp() > time.time() + 3600 * 24 * 30:  # 未来 30 天，视为去年
                dt = datetime(now.year - 1, month, day, hour, minute)
            return dt.timestamp()
        except ValueError:
            return fallback
    # 相对时间：刚刚 / N分钟前 / N小时前 / N天前 / 昨天 / 前天
    now = time.time()
    if v in ("刚刚", "刚刚发布", "此刻"):
        return now
    m = re.match(r"^(\d+)\s*秒前$", v)
    if m:
        return now - int(m.group(1))
    m = re.match(r"^(\d+)\s*分钟前$", v)
    if m:
        return now - int(m.group(1)) * 60
    m = re.match(r"^(\d+)\s*小时前$", v)
    if m:
        return now - int(m.group(1)) * 3600
    m = re.match(r"^(\d+)\s*天前$", v)
    if m:
        return now - int(m.group(1)) * 86400
    if v.startswith("昨天"):
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return (base - timedelta(days=1)).timestamp()
    if v.startswith("前天"):
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return (base - timedelta(days=2)).timestamp()
    return fallback


def format_display_time(value: str, tz_utc8: bool = True) -> str:
    """把平台时间转成友好展示串。

    - X 的 ISO8601（``2026-08-06T12:00:00.000Z``）→ ``08月06日 20:00``（东八区）；
    - bilibili 的 ``08月06日`` 直接原样返回；
    - 其它无法解析的值原样返回。
    """
    if not value:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}T", value):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if tz_utc8:
                dt = dt.astimezone(timezone(timedelta(hours=8)))
            now = datetime.now()
            if dt.year == now.year:
                return f"{dt.month}月{dt.day}日 {dt.hour:02d}:{dt.minute:02d}"
            return f"{dt.year}-{dt.month:02d}-{dt.day:02d}"
        except ValueError:
            return value
    return value


def sanitize_text(text: str, emoji_mode: str = "strip", max_len: int = 4000) -> str:
    """清洗正文：去除多余空白、可选用 EMoji 模式。"""
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    # 去除 HTML 实体（如 &amp;）
    s = re.sub(r"&(?!#\d+;)[a-zA-Z]+;", " ", s)
    if emoji_mode == "strip":
        s = _EMOJI_RE.sub("", s)
    # 折叠超长空白
    s = re.sub(r"\n{3,}", "\n\n", s)
    if max_len and len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def parse_metrics(data: dict) -> dict[str, int]:
    """把平台统计字段规整成 {like, reply, repost, ...} 的 int 字典。"""
    out: dict[str, int] = {}
    for k, v in (data or {}).items():
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            out[k] = 0
    return out


class BaseFetcher(ABC):
    """平台抓取器抽象基类。

    每个平台实现 :meth:`fetch_recent`，返回最近帖子的 :class:`FetchResult`，
    并按「新→旧」排序。去重、投递由上层 :class:`~phantasm.poller.Poller` 负责。
    """

    platform: str = ""
    display_label: str = ""

    def __init__(self, account: Account, config: ConfigManager, http: HttpClient,
                 logger: logging.Logger):
        self.account = account
        self.config = config
        self.http = http
        self.logger = logger

    @abstractmethod
    async def fetch_recent(self, limit: int = 20) -> FetchResult:
        """抓取最近帖子。失败时返回 ``FetchResult(error=...)``，绝不抛异常到上层。

        返回的 ``posts`` 必须按发布时间的**新→旧**排序。
        """
        raise NotImplementedError

    async def fetch_raw(self, limit: int = 5) -> list:
        """抓取原始条目 JSON（调试用）。基类默认返回空列表，子类可覆盖。"""
        return []

    # ----------------------------------------------------------------
    def err_result(self, msg: str) -> FetchResult:
        return FetchResult(error=msg)

    def ok_result(self, posts: list[Post]) -> FetchResult:
        posts.sort(key=lambda p: p.created_ts, reverse=True)
        return FetchResult(posts=posts, total=len(posts))

    # ----------------------------------------------------------------
    @staticmethod
    def is_retweet(post: Post) -> bool:
        """是否为转推/转发：各平台在 ``post.extra['is_retweet']`` 自报。"""
        return bool(post.extra.get("is_retweet"))

    def apply_account_filters(self, posts: list[Post]) -> list[Post]:
        """按账号配置过滤。目前支持 ``filter_retweet``（剔除转推）。"""
        if not self.account.filter_retweet:
            return posts
        kept = [p for p in posts if not self.is_retweet(p)]
        dropped = len(posts) - len(kept)
        if dropped:
            self.logger.info(f"[{self.account.key}] 已过滤 {dropped} 条转推（filter_retweet=on）")
        return kept


def build_fetcher(account: Account, config: ConfigManager, http: HttpClient,
                  logger: logging.Logger) -> BaseFetcher:
    """按账号平台返回具体抓取器；未知平台返回 None。"""
    from .bilibili import BilibiliFetcher
    from .x import XFetcher

    if account.platform == "bilibili":
        return BilibiliFetcher(account, config, http, logger)
    if account.platform == "x":
        mode = str((config.get("credentials", {}).get("x") or {}).get("mode", "api")).lower()
        if mode == "rss":
            from .x_rss import XRssFetcher
            return XRssFetcher(account, config, http, logger)
        return XFetcher(account, config, http, logger)
    return None


def describe_platform(platform: str) -> str:
    return {"bilibili": "Bilibili", "x": "X/Twitter"}.get(platform, platform)
