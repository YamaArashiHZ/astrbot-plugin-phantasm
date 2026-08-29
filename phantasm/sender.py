"""AstrBot 主动消息发送。

负责把一张卡片图片（+ 文字说明）投递到配置的 QQ 群聊 / 私聊目标。

利用 AstrBot 的 ``context.send_message(unified_msg_origin, MessageChain)`` 主动发送。
``unified_msg_origin`` 形如 ``<platform>:<MessageType>:<target_id>``：
  - 群聊：``aiocqhttp:GroupMessage:<群号>``
  - 私聊：``aiocqhttp:FriendMessage:<QQ号>``

平台前缀默认取当前运行中的平台实例（如 ``aiocqhttp``），也可通过配置
``network.platform_id``（或顶层 ``platform_id``）覆盖。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from astrbot.api.event import MessageChain

from .config import ConfigManager
from .models import Account, Post, Target

# 消息类型锚点
_MSG_TYPE = {"group": "GroupMessage", "private": "FriendMessage", "friend": "FriendMessage"}


class Sender:
    def __init__(self, context, config: ConfigManager, logger: logging.Logger,
                 downloader=None):
        self.context = context
        self.config = config
        self.logger = logger
        self.send_cfg = config.send
        self._downloader = downloader

    def set_downloader(self, fn) -> None:
        """注入 ``async (url) -> bytes`` 下载器（用于附带原图）。"""
        self._downloader = fn

    # ----------------------------------------------------------------
    async def send_post(self, post: Post, account: Account, card_path: str) -> tuple[int, int]:
        """把一张卡片投递到该账号的所有目标。返回 (成功数, 失败数)。"""
        caption = self._build_caption(post, account)
        ok, fail = 0, 0
        targets = account.targets
        if not targets:
            self.logger.info(f"[{post.kebab_id}] 账号 {account.key} 未配置投递目标，跳过")
            return 0, 0
        for target in targets:
            try:
                umo = self._resolve_umo(target)
                if not umo:
                    self.logger.warning(f"[{post.kebab_id}] 目标 {target.describe()} 无法解析 UMO，跳过")
                    fail += 1
                    continue
                chain = self._build_chain(post, account, card_path, caption)
                await self._append_media_chain(post, chain)
                sent = await self.context.send_message(umo, chain)
                if sent:
                    ok += 1
                    self.logger.info(f"[{post.kebab_id}] 已投递到 {target.describe()} ({umo})")
                else:
                    fail += 1
                    self.logger.warning(f"[{post.kebab_id}] 投递到 {target.describe()} 未找到匹配平台 ({umo})")
            except Exception as e:  # noqa: BLE001
                fail += 1
                self.logger.warning(f"[{post.kebab_id}] 投递到 {target.describe()} 失败：{e}")
        return ok, fail

    # ----------------------------------------------------------------
    def _build_caption(self, post: Post, account: Account) -> str:
        """按 send.caption_format 生成文字说明。"""
        if not self.send_cfg.get("send_caption", True):
            return ""
        fmt = str(self.send_cfg.get("caption_format", "{platform} · {author} · {time}"))
        platform_label = {"bilibili": "Bilibili", "x": "X/Twitter"}.get(post.platform, post.platform)
        try:
            caption = fmt.format(
                platform=platform_label,
                author=post.author_name or account.name,
                time=post.created_at or "",
                handle=post.author_handle or "",
                url=post.url or "",
                account=account.display_name or account.account_id,
            )
        except (KeyError, IndexError, ValueError):
            caption = f"{platform_label} · {post.author_name or account.name} · {post.created_at or ''}"
        caption = caption.strip()
        # 附上原贴链接（QQ 里可点击），可关闭
        if self.send_cfg.get("link_to_post", True) and post.url:
            caption = caption + f"\n原帖：{post.url}" if caption else f"原帖：{post.url}"
        return caption

    def _build_chain(self, post, account, card_path: str, caption: str) -> MessageChain:
        chain = MessageChain()
        if caption:
            chain.message(caption)
        chain.file_image(card_path)
        return chain

    async def _append_media_chain(self, post, chain: MessageChain, tmp_dir=None) -> None:
        """把帖子原始附图（best-effort）追加到同一条消息里。失败/无下载器则跳过。"""
        if self._downloader is None:
            return
        urls = [u for u in (post.media_urls or []) if u][: int(self.send_cfg.get("media_max", 4))]
        if not urls:
            return
        base = tmp_dir or self.config.data_dir
        out_dir = Path(base) / "attachments"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        for i, u in enumerate(urls):
            try:
                data = await self._downloader(u)
                ext = "jpg"
                p = out_dir / f"{post.post_id}_{i}.{ext}"
                p.write_bytes(data)
                chain.file_image(str(p))
            except Exception:  # noqa: BLE001
                continue

    def _resolve_umo(self, target: Target) -> Optional[str]:
        if target.is_umo:
            return target.id
        msg_type = _MSG_TYPE.get(target.type)
        if not msg_type:
            return None
        platform_id = self._platform_id()
        return f"{platform_id}:{msg_type}:{target.id}"

    def _platform_id(self) -> str:
        """解析平台前缀：配置覆盖 > 当前运行平台实例 > 默认 aiocqhttp。"""
        override = self.config.get("platform_id", "") or self.config.network.get("platform_id", "")
        if override:
            return str(override).strip()
        try:
            for p in self.context.platform_manager.get_insts():
                pid = p.meta().id
                if pid and "webchat" not in str(pid).lower():
                    return pid
        except Exception as e:  # noqa: BLE001
            self.logger.debug(f"解析平台实例失败，回退 aiocqhttp：{e}")
        return "aiocqhttp"
