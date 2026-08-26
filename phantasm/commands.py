"""`/phantasm` 命令处理器（以 Mixin 形式提供）。

签名约定：handler 只接收 ``event``，从 ``event.message_str`` 解析子命令，
避免因 AstrBot 版本差异导致参数注入不一致。

子命令：
  phantasm status            查看状态
  phantasm check             立即手动触发一轮检查
  phantasm list              查看订阅账号与投递目标
  phantasm history [n]       最近 n 条已处理帖子（默认 10）
  phantasm add <p> <id> [name]   添加账号（管理员）
  phantasm del <p> <id>      删除账号（管理员）
  phantasm target <p> <id> add group <gid>   （管理员）
  phantasm target <p> <id> add private <qq>  （管理员）
  phantasm target <p> <id> clear            （管理员）
  phantasm pause            暂停轮询（管理员）
  phantasm resume           恢复轮询（管理员）
  phantasm help             用法
"""
from __future__ import annotations

import re

from astrbot.api.event import AstrMessageEvent

from . import __version__
from .models import Account

_COMMANDS = {"phantasm", "phan"}


class CommandsMixin:
    # 这些属性由主插件提供：config、poller、storage、sender、http、logger

    def _split_args(self, event: AstrMessageEvent) -> list[str]:
        text = (event.message_str or "").strip()
        parts = re.split(r"\s+", text)
        if parts and parts[0].lower().lstrip("/!") in _COMMANDS:
            parts = parts[1:]
        return [p for p in parts if p]

    # 注意：@filter.command 必须在 main.py 的 Star 类里注册（AstrBot 用
    # get_handlers_by_module_name 做「精确」模块路径匹配，handler 必须与 Star 同模块）。
    # 这里只保留可复用的命令实现，供 main.py 的 phantasm() 转发调用。
    async def _phantasm_command(self, event: AstrMessageEvent):
        args = self._split_args(event)
        cmd = args[0].lower() if args else "status"
        rest = args[1:]

        if cmd in ("status", "状态", ""):
            yield event.plain_result(self._status_text())
        elif cmd in ("check", "检查", "run"):
            yield event.plain_result("正在触发一轮检查，请稍候…")
            summary = await self.poller.check_once()
            yield event.plain_result(self._apply_summary(summary))
        elif cmd in ("list", "订阅", "账号"):
            yield event.plain_result(self._list_text())
        elif cmd in ("history", "记录"):
            yield event.plain_result(self._history_text(rest))
        elif cmd in ("raw", "原始"):
            async for r in self._cmd_raw(event, rest):
                yield r
        elif cmd in ("add", "添加"):
            yield self._require_admin(event, lambda: self._cmd_add(rest))
        elif cmd in ("del", "delete", "删除"):
            yield self._require_admin(event, lambda: self._cmd_del(rest))
        elif cmd in ("target", "目标"):
            yield self._require_admin(event, lambda: self._cmd_target(rest))
        elif cmd in ("pause", "暂停"):
            yield self._require_admin(event, lambda: self._cmd_pause())
        elif cmd in ("resume", "恢复"):
            yield self._require_admin(event, lambda: self._cmd_resume(rest))
        elif cmd in ("help", "帮助"):
            yield event.plain_result(self._help_text())
        else:
            yield event.plain_result(f"未知子命令「{cmd}」，输入 /phantasm help 查看用法")

    # ----------------------------------------------------------------
    @staticmethod
    def _require_admin(event, fn):
        if not event.is_admin():
            return event.plain_result("需要管理员权限才能执行该操作")
        return fn()

    def _status_text(self) -> str:
        poller_txt = self.poller.status_text()
        st = self.storage
        acc = self.config.accounts()
        enabled = sum(1 for a in acc if a.enabled)
        return (f"👻 Phantasm v{__version__} 状态\n"
                f"插件启用：{'✅' if self.config.enabled else '⛔'}  |  "
                f"账号 {len(acc)}（启用 {enabled}）\n"
                f"轮询：{poller_txt}\n"
                f"去重记录：{st.count} 条 | 平台：bilibili / x")

    def _list_text(self) -> str:
        accs = self.config.accounts()
        if not accs:
            return "尚未订阅任何账号。添加：/phantasm add <bilibili|x> <id> [名称]"
        lines = [f"👻 订阅账号（{len(accs)}）"]
        for a in accs:
            state = "启用" if a.enabled else "停用"
            lines.append(f"• [{a.platform}] {a.name} ({a.account_id}) [{state}]")
            lines.append(f"   投递：{a.target_labels()}")
        return "\n".join(lines)

    def _history_text(self, rest) -> str:
        try:
            n = int(rest[0]) if rest and rest[0].isdigit() else 10
        except (IndexError, ValueError):
            n = 10
        recent = self.storage.recent(n)
        if not recent:
            return "暂无已处理记录"
        return "最近处理：\n" + "\n".join(f"• {k}" for k in recent)

    async def _cmd_raw(self, event: AstrMessageEvent, rest) -> "async generator":
        """调试：抓取指定账号的原始 JSON 并写入日志（搜 phan_raw）。"""
        if len(rest) < 2:
            yield event.plain_result("用法：/phantasm raw <bilibili|x> <account_id> [条数]")
            return
        platform = rest[0].strip().lower()
        account_id = rest[1].strip()
        try:
            limit = max(1, min(int(rest[2]), 20)) if len(rest) > 2 and rest[2].isdigit() else 1
        except (IndexError, ValueError):
            limit = 1
        if platform not in ("bilibili", "x"):
            yield event.plain_result(f"不支持的平台「{platform}」，仅支持 bilibili / x")
            return
        if not account_id:
            yield event.plain_result("account_id 不能为空")
            return

        yield event.plain_result("正在抓取原始数据并写入日志（搜 phan_raw）…")
        from .fetchers import build_fetcher
        fetcher = build_fetcher(
            Account(platform=platform, account_id=account_id, display_name=""),
            self.config, self.http, self.logger)
        if fetcher is None:
            yield event.plain_result("未知平台")
            return
        try:
            raw = await fetcher.fetch_raw(limit=limit)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"抓取失败：{e}")
            return
        if not raw:
            yield event.plain_result(
                f"未抓到 {platform}:{account_id} 的原始数据（检查凭据或 B 站风控）")
            return
        import json as _json
        self.logger.info(f"phan_raw 开始 [{platform}:{account_id}] 条数={len(raw)}")
        for i, item in enumerate(raw):
            self.logger.info(f"phan_raw #{i}: {_json.dumps(item, ensure_ascii=False)}")
        self.logger.info("phan_raw 结束")
        yield event.plain_result(
            f"已抓取 {platform}:{account_id} 原始 {len(raw)} 条，完整 JSON 已写入日志（搜 phan_raw）。")

    def _cmd_add(self, rest) -> str:
        if len(rest) < 2:
            return "用法：/phantasm add <bilibili|x> <account_id> [display_name]"
        platform, account_id = rest[0].strip().lower(), rest[1].strip()
        name = rest[2].strip() if len(rest) > 2 else ""
        if platform not in ("bilibili", "x"):
            return f"不支持的平台「{platform}」，仅支持 bilibili / x"
        if not account_id:
            return "account_id 不能为空"
        ok = self.config.add_account(platform, account_id, name)
        if not ok:
            return f"账号 {platform}:{account_id} 已存在或添加失败"
        return f"已添加账号 {platform}:{account_id}（{name or account_id}）。记得配置投递目标：/phantasm target {platform} {account_id} add group <群号>"

    def _cmd_del(self, rest) -> str:
        if len(rest) < 2:
            return "用法：/phantasm del <bilibili|x> <account_id>"
        platform, account_id = rest[0].strip().lower(), rest[1].strip()
        ok = self.config.remove_account(platform, account_id)
        return f"已删除账号 {platform}:{account_id}" if ok else f"未找到账号 {platform}:{account_id}"

    def _cmd_target(self, rest) -> str:
        if len(rest) < 3:
            return "用法：/phantasm target <platform> <account_id> add|clear [group|private|umo <id>]"
        platform, account_id = rest[0].strip().lower(), rest[1].strip()
        action = rest[2].strip().lower()
        if action == "clear":
            ok = self.config.clear_targets(platform, account_id)
            return f"已清空 {platform}:{account_id} 的投递目标" if ok else f"未找到账号 {platform}:{account_id}"
        if action == "add":
            if len(rest) < 5:
                return "用法：/phantasm target <platform> <account_id> add <group|private|umo> <id>"
            ttype = rest[3].strip().lower()
            tid = rest[4].strip()
            if ttype == "friend":
                ttype = "private"
            if ttype not in ("group", "private", "umo"):
                return "目标类型仅支持 group / private / umo"
            ok = self.config.add_target(platform, account_id, ttype, tid)
            return f"已为 {platform}:{account_id} 添加投递目标 {ttype} {tid}" if ok else "添加失败（账号不存在或参数有误）"
        return f"未知目标操作「{action}」，支持 add / clear"

    def _cmd_pause(self) -> str:
        self.config.set_enabled(False)
        return "已暂停轮询（enabled=false）。恢复：/phantasm resume"

    def _cmd_resume(self, rest) -> str:
        self.config.set_enabled(True)
        self.poller.start()
        if rest and rest[0].isdigit():
            self.config.config["poll_interval_seconds"] = int(rest[0])
            self.config.save()
            return f"已恢复轮询，并把轮询间隔设为 {rest[0]} 秒"
        return "已恢复轮询"

    def _help_text(self) -> str:
        return ("👻 Phantasm 命令\n"
                "/phantasm             查看状态\n"
                "/phantasm check       立即检查并投递新帖\n"
                "/phantasm list        查看订阅\n"
                "/phantasm history [n] 最近处理记录\n"
                "/phantasm raw <p> <id> [n]  调试：输出最新原始 JSON 到日志(搜 phan_raw)\n"
                "/phantasm add <p> <id> [名称]   添加账号\n"
                "/phantasm del <p> <id>         删除账号\n"
                "/phantasm target <p> <id> add group|private|umo <id>\n"
                "/phantasm target <p> <id> clear\n"
                "/phantasm pause|resume         暂停/恢复")

    def _apply_summary(self, summary: dict) -> str:
        if summary.get("skipped"):
            return f"本轮跳过：{summary['skipped']}"
        base = f"本轮结果：抓到 {summary.get('fetched', 0)} 条，新帖 {summary.get('new', 0)}，" \
               f"投递成功 {summary.get('sent', 0)}，失败 {summary.get('failed', 0)}，异常 {summary.get('errored', 0)}"
        errs = summary.get("errors") or []
        if errs:
            base += "\n异常：" + "\n".join(f"• {e}" for e in errs[:5])
        return base
