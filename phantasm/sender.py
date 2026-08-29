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
                # 1) 一条消息：卡片（+ 文字说明）
                chain = self._build_chain(post, account, card_path, caption)
                sent = await self.context.send_message(umo, chain)
                # 2) 一条消息：附图打包（合并转发）
                att = await self.build_attachment_forward(post)
                if att is not None:
                    await self.context.send_message(umo, att)
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

    async def build_attachment_forward(self, post, self_id=None, bot_name=None,
                                       tmp_dir=None) -> Optional[MessageChain]:
        """把帖子原始附图打包成一条「合并转发」消息（参考 pixiv_sender 的 Nodes 方式）。

        下载附图 -> 生成 ``Nodes`` 转发链；无附图 / 下载失败 / 缺 self_id 时返回 None。
        """
        urls = [u for u in (post.media_urls or []) if u][: int(self.send_cfg.get("media_max", 4))]
        sid = str(self_id or self.send_cfg.get("bot_self_id") or "").strip()
        self.logger.info(
            f"[附图] platform={post.platform} media_urls={len(post.media_urls or [])} "
            f"可打包={len(urls)} self_id={sid!r} downloader={'有' if self._downloader else '无'}")
        if self._downloader is None:
            self.logger.warning("[附图] 无下载器，跳过")
            return None
        if not urls:
            self.logger.info("[附图] 原贴无附图，跳过")
            return None
        nm = str(bot_name or self.send_cfg.get("bot_nickname") or "").strip() or sid or "Phantasm"
        if not sid:
            self.logger.warning("[附图] self_id 为空(event.get_self_id 或 send.bot_self_id 都没取到)，跳过")
            return None
        base = tmp_dir or self.config.data_dir
        out_dir = Path(base) / "attachments"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.logger.warning("[附图] 无法创建输出目录，跳过")
            return None
        paths: list[Path] = []
        for i, u in enumerate(urls):
            try:
                data = await self._downloader(u)
                p = out_dir / f"{post.post_id}_{i}.jpg"
                p.write_bytes(data)
                paths.append(p)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"[附图] {u[:60]} 下载失败：{e}")
                continue
        if not paths:
            self.logger.warning("[附图] 附图全部下载失败")
            return None
        try:
            from astrbot.api.message_components import Image, Node, Nodes
            nodes = [Node(uin=sid, name=nm, content=[Image.fromFileSystem(str(p))]) for p in paths]
            self.logger.info(f"[附图] 打包 {len(paths)} 张合并转发成功")
            return MessageChain(chain=[Nodes(nodes=nodes)])
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"打包附图转发失败，回退为多图消息：{e}")
            chain = MessageChain()
            for p in paths:
                chain.file_image(str(p))
            return chain

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
