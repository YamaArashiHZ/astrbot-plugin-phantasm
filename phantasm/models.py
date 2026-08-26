"""Phantasm 核心数据模型。

所有平台抓取器都把原始数据规整为统一的 :class:`Post`，渲染器与投递层只依赖
该归一化结构，从而做到「一次归一化、多平台复用」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Account / Target
# ---------------------------------------------------------------------------
@dataclass
class Target:
    """一个投递目标。

    type 支持：
      - ``group``   ：QQ 群聊，``id`` 为群号字符串
      - ``private`` ：QQ 私聊，``id`` 为对方 QQ 号字符串
      - ``umo``     ：直接给出 AstrBot 的 unified_msg_origin（高级用法，跳过拼装）
    """

    type: str = "group"
    id: str = ""

    @property
    def is_group(self) -> bool:
        return self.type == "group"

    @property
    def is_private(self) -> bool:
        return self.type in ("private", "friend")

    @property
    def is_umo(self) -> bool:
        return self.type == "umo"

    def describe(self) -> str:
        label = {"group": "群聊", "private": "私聊", "friend": "私聊", "umo": "UMO"}.get(self.type, self.type)
        return f"{label} {self.id}"


@dataclass
class Account:
    """一个被监听账号配置。"""

    platform: str = ""          # bilibili | x
    account_id: str = ""        # bilibili UID 或 X 的 screen_name/user_id
    display_name: str = ""      # 可选覆盖显示名
    enabled: bool = True
    targets: list[Target] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.account_id}"

    @property
    def name(self) -> str:
        return self.display_name or self.account_id

    def target_labels(self) -> str:
        if not self.targets:
            return "（无目标）"
        return "、".join(t.describe() for t in self.targets)


# ---------------------------------------------------------------------------
# Post
# ---------------------------------------------------------------------------
@dataclass
class Post:
    """归一化后的「帖子」。

    ``post_id`` 是平台内唯一 ID，也是去重存储的键；不同平台的前缀不同，
    :func:`kebab_id` 用于拼装全局去重键。
    """

    platform: str = ""                    # bilibili | x
    account_id: str = ""                  # 归属被监听账号（配置里的 account_id）
    account_name: str = ""                # 作者显示名（来自平台）
    post_id: str = ""                     # 平台内唯一 id
    author_name: str = ""                 # 作者昵称
    author_handle: str = ""               # 作者 handle（X @handle；B 站 UID 或 mid）
    author_avatar_url: str = ""           # 头像 URL
    content: str = ""                     # 正文
    created_at: str = ""                  # 展示用时间字符串（原始）
    created_ts: float = 0.0               # 时间戳（秒），尽量解析，未知为 0
    media_urls: list[str] = field(default_factory=list)   # 配图/封面 URL（可下载）
    stats: dict[str, int] = field(default_factory=dict)   # like/reply/repost/play 等
    url: str = ""                         # 帖子链接
    source_type: str = ""                 # bili: av/draw/forward/word；x: tweet
    extra: dict[str, Any] = field(default_factory=dict)   # 平台附加数据（如视频标题/时长/蓝V标识）

    @property
    def kebab_id(self) -> str:
        """全局去重键：`<platform>_<post_id>`。"""
        return f"{self.platform}_{self.post_id}"

    @property
    def like_count(self) -> int:
        return int(self.stats.get("like", 0) or 0)

    @property
    def display_short(self) -> str:
        text = (self.content or "").replace("\n", " ").strip()
        return text[:40] + ("…" if len(text) > 40 else "")


# ---------------------------------------------------------------------------
# 渲染结果
# ---------------------------------------------------------------------------
@dataclass
class RenderJob:
    """一次渲染任务：一个帖子里的一或几张图。"""

    card_path: str = ""       # 生成的卡片文件路径
    caption: str = ""         # 附带文字说明
    post: Optional[Post] = None


@dataclass
class FetchResult:
    """一次抓取的产出：新帖列表。"""

    posts: list[Post] = field(default_factory=list)   # 最近/新帖（按时间新→旧）
    new_posts: list[Post] = field(default_factory=list)  # 尚未处理的新帖（去重后）
    error: str = ""           # 非空表示本次抓取失败
    total: int = 0            # 本次抓取到的数量
