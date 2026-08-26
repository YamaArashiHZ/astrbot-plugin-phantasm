"""Phantasm — AstrBot 插件入口。

监听 Bilibili 动态与 X/Twitter 推文，把新帖渲染成对应平台风格的卡片图片，
并自动投递到配置的 QQ 群聊 / 私聊。

模块划分见 :mod:`phantasm` 包：config/storage/http/fetchers/renderers/sender/
poller/commands/web 各司其职。
"""
from __future__ import annotations

from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 用「相对导入」引入子包，使 phantasm 各模块的 module path 落在插件包前缀下
# （如 data.plugins.astrbot_plugin_phantasm.phantasm.commands），
# 这样 AstrBot 才能把 @filter.command 注册的命令关联到本插件。
from .phantasm.commands import CommandsMixin
from .phantasm import __version__ as _phantasm_version
from .phantasm.config import ConfigManager
from .phantasm.http import HttpClient
from .phantasm.poller import Poller
from .phantasm.renderers import CardRenderer
from .phantasm.sender import Sender
from .phantasm.storage import StorageManager
from .phantasm.web import WebMixin

PLUGIN_NAME = "astrbot_plugin_phantasm"


class PhantasmPlugin(CommandsMixin, WebMixin, Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        plugin_name = getattr(self, "name", PLUGIN_NAME)
        self.plugin_name = plugin_name
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 配置 / 存储 / 网络
        self.config = ConfigManager(self.data_dir, logger)
        self.storage = StorageManager(
            self.data_dir, logger,
            history_limit=int(self.config.storage.get("history_limit", 5000)))
        self.http = HttpClient(logger)
        self.http.configure(self.config.network)

        # 渲染与发送
        self.renderer = CardRenderer(self.config, logger, store_dir=self.data_dir)
        self.renderer.set_downloader(self.http.get_bytes)
        self.sender = Sender(context, self.config, logger)

        # 轮询调度
        self.poller = Poller(self.config, self.storage, self.http,
                             self.renderer, self.sender, logger, self.data_dir)

        # ---- 插件设置页后端 API ----
        context.register_web_api(
            f"/{plugin_name}/config", self.web_get_config, ["GET"], "获取 Phantasm 配置")
        context.register_web_api(
            f"/{plugin_name}/config/save", self.web_save_config, ["POST"], "保存 Phantasm 配置")
        context.register_web_api(
            f"/{plugin_name}/status", self.web_get_status, ["GET"], "获取 Phantasm 状态")
        context.register_web_api(
            f"/{plugin_name}/check", self.web_trigger_check, ["POST"], "立即触发一轮检查")
        context.register_web_api(
            f"/{plugin_name}/history/clear", self.web_clear_history, ["POST"], "清空已处理记录")
        context.register_web_api(
            f"/{plugin_name}/cards", self.web_get_cards, ["GET"], "查看近期生成的卡片")

    # ------------------------------------------------------------------
    # 命令：/phantasm
    # ------------------------------------------------------------------
    # 必须与 Star 类同模块（main.py）注册，AstrBot 才按模块前缀关联到本插件。
    # 参数用 (event, prompt="") 而非 *args，避免 AstrBot 的命令参数绑定把变长参数当作必填项。
    @filter.command("phantasm", alias={"phan"})
    async def phantasm(self, event: AstrMessageEvent, prompt: str = ""):
        async for r in self._phantasm_command(event):
            yield r

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """插件被加载后调用：按最新配置重建网络/渲染参数并（如启用）启动轮询。"""
        self.http.configure(self.config.network)
        logger.info(f"Phantasm v{_phantasm_version} 已初始化")
        # 确保中文字体可用（缺省时自动下载 Noto Sans CJK，避免渲染出方框）
        await self.renderer.ensure_font(
            self.data_dir, lambda url: self.http.get_bytes(url, timeout=180))
        self.storage.prune_if_needed(self.config.storage.get("history_limit"))
        if self.config.enabled:
            self.poller.start()
            logger.info(
                f"Phantasm 已启用并启动轮询：interval={self.config.poll_interval}s, "
                f"账号={len(self.config.accounts())}, 已处理={self.storage.count}")
        else:
            logger.info("Phantasm 已加载（enabled=false，未启动轮询）")

    async def terminate(self) -> None:
        """插件被卸载时调用：停止轮询任务。"""
        await self.poller.stop()
        logger.info("Phantasm 已停止轮询")
