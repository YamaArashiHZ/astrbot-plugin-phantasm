"""轮询调度器：定时抓取各账号 → 去重 → 渲染 → 投递。

职责：
- 后台 asyncio 任务，按 ``poll_interval_seconds`` + 抖动循环；
- 对每个启用账号并发抓取（受 ``max_concurrency`` 限制），单账号失败不影响其它账号；
- 从抓取结果中筛出尚未处理的新帖，逐帖渲染卡片并投递到该账号的目标；
- 对成功投递的帖子打上去重标记（避免重复投递）；
- 提供 :meth:`check_once` 便于 `/phantasm check` 手动触发（同步等待并返回摘要）。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from .config import ConfigManager
from .http import HttpClient
from .models import Account
from .renderers import CardRenderer
from .sender import Sender
from .storage import StorageManager


class Poller:
    def __init__(self, config: ConfigManager, storage: StorageManager, http: HttpClient,
                 renderer: CardRenderer, sender: Sender, logger: logging.Logger,
                 data_dir: Path, translator=None):
        self.config = config
        self.storage = storage
        self.http = http
        self.renderer = renderer
        self.sender = sender
        self.logger = logger
        self.data_dir = Path(data_dir)
        self.translator = translator

        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._attempts: dict[str, int] = {}
        self._state: dict[str, Any] = {
            "running": False,
            "last_run": 0.0,
            "last_duration": 0.0,
            "last_summary": "",
            "processed_since_start": 0,
            "errors": 0,
        }

    # ----------------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------------
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        self._state["running"] = False

    async def _loop(self) -> None:
        self._state["running"] = True
        self.logger.info("Phantasm 轮询任务已启动")
        try:
            while True:
                try:
                    await self.check_once()
                except Exception as e:  # noqa: BLE001
                    self.logger.exception(f"轮询过程出现异常：{e}")
                    self._state["errors"] += 1
                interval = self._next_interval()
                self.logger.debug(f"Phantasm 将在 {interval:.0f}s 后再次检查")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            self.logger.info("Phantasm 轮询任务已停止")
            raise

    def _next_interval(self) -> float:
        base = self.config.poll_interval
        try:
            jitter = max(0, float(self.config.get("poll_jitter_seconds", 10)))
        except (TypeError, ValueError):
            jitter = 10.0
        return base + random.uniform(0, jitter)

    # ----------------------------------------------------------------
    # 一次检查
    # ----------------------------------------------------------------
    async def check_once(self) -> dict[str, Any]:
        """执行一轮抓取→渲染→投递。返回摘要 dict。"""
        if self._lock.locked():
            return {"skipped": "previous run in progress"}  # 防重入
        async with self._lock:
            started = time.time()
            summary = await self._run()
            self._state["last_run"] = started
            self._state["last_duration"] = time.time() - started
            self._state["last_summary"] = self._fmt_summary(summary)
            return summary

    async def _run(self) -> dict[str, Any]:
        accounts = [a for a in self.config.accounts() if a.enabled]
        if not self.config.enabled:
            return {"skipped": "插件已暂停（enabled=false）"}
        if not accounts:
            return {"skipped": "未配置账号"}

        # 并发限制
        try:
            max_concurrency = max(1, int(self.config.network.get("max_concurrency", 3)))
        except (TypeError, ValueError):
            max_concurrency = 3
        sem = asyncio.Semaphore(max_concurrency)

        async def handle(account: Account):
            async with sem:
                try:
                    return await self._handle_account(account)
                except Exception as e:  # noqa: BLE001
                    self.logger.exception(f"处理账号 {account.key} 异常：{e}")
                    return {"account": account.key, "error": str(e)}

        results = await asyncio.gather(*(handle(a) for a in accounts))
        return self._aggregate(results)

    async def _handle_account(self, account: Account) -> dict[str, Any]:
        from .fetchers import build_fetcher
        fetcher = build_fetcher(account, self.config, self.http, self.logger)
        if fetcher is None:
            return {"account": account.key, "error": "未知平台"}

        res = await fetcher.fetch_recent(limit=self._fetch_limit(account))
        out = {"account": account.key, "fetched": res.total}
        if res.error:
            out["error"] = res.error
            self.logger.warning(f"[{account.key}] 抓取失败：{res.error}")
            return out

        # 首次看到该账号（尚未基线化）：把当前抓到的历史动态全部标记为已处理，
        # 本轮不投递，避免「新加账号时把历史全部刷屏」。
        if not self.storage.is_baselined(account.key):
            n = self.storage.mark_processed_many(p.kebab_id for p in res.posts)
            self.storage.mark_baselined(account.key)
            out.update({"baselined": True, "baselined_count": n,
                        "new": 0, "sent": 0, "failed": 0, "errored": 0})
            self.logger.info(
                f"[{account.key}] 已完成历史基线化（标记 {n} 条历史为已处理），之后仅投递新帖")
            return out

        # 新帖 = 尚未处理
        new_posts = [p for p in res.posts if not self.storage.is_processed(p.kebab_id)]
        out["new"] = len(new_posts)

        max_jobs = max(1, int(self.config.send.get("max_jobs_per_account", 10)))
        sent, failed, errored = 0, 0, 0
        for post in new_posts[:max_jobs]:
            try:
                ok, fail = await self._process_post(post, account)
                sent += ok
                failed += fail
                if ok > 0:
                    self.storage.mark_processed(post.kebab_id)
                    self._state["processed_since_start"] += 1
                else:
                    # 全部目标失败：记一次尝试，超过上限则强制标记，避免无限重试
                    self._attempts[post.kebab_id] = self._attempts.get(post.kebab_id, 0) + 1
                    if self._attempts[post.kebab_id] >= 3:
                        self.storage.mark_processed(post.kebab_id)
                        self.logger.warning(f"[{post.kebab_id}] 连续 3 次投递失败，已标记避免无限重试")
            except Exception as e:  # noqa: BLE001
                errored += 1
                self.logger.exception(f"[{post.kebab_id}] 处理异常：{e}")
                # 渲染/投递异常：确定性失败，标记避免重复
                self.storage.mark_processed(post.kebab_id)
        out.update({"sent": sent, "failed": failed, "errored": errored})
        return out

    def _fetch_limit(self, account: Account) -> int:
        # 抓一个略多于 jobs 上限的数量，保证有足够候选
        max_jobs = max(1, int(self.config.send.get("max_jobs_per_account", 10)))
        return min(100, max(20, max_jobs + 10))

    async def _process_post(self, post, account: Account) -> tuple[int, int]:
        original_card = ""
        render_post = post
        try:
            # 翻译：主卡片显示译文，另渲一张原文卡片随附图打包发送
            if self.translator is not None:
                tr = await self.translator.maybe_translate(post)
                if tr:
                    render_post = replace(post, content=tr,
                                          extra={**post.extra, "translated": True})
            card_path = await self.renderer.render(render_post, account, self._render_dir())
            if render_post is not post:
                try:
                    original_card = await self.renderer.render(post, account, self._render_dir())
                except Exception as e:  # noqa: BLE001
                    self.logger.warning(f"[{post.kebab_id}] 原文卡片渲染失败（仅发译文卡片）：{e}")
        except Exception as e:  # noqa: BLE001
            self.logger.exception(f"[{post.kebab_id}] 卡片渲染失败：{e}")
            return 0, 0
        ok, fail = await self.sender.send_post(post, account, card_path,
                                               original_card=original_card)
        self._cleanup_card(post, account, card_path)
        return ok, fail

    def _render_dir(self) -> Path:
        mode = self.config.image.get("output_mode", "keep")
        base = self.data_dir / str(self.config.image.get("dir", "cards"))
        if mode == "temp":
            base = self.data_dir / "cards"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _cleanup_card(self, post, account, card_path: str) -> None:
        # 发送后按 output_mode 决定是否清理
        try:
            mode = self.config.image.get("output_mode", "keep")
            if mode == "temp":
                Path(card_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def _aggregate(self, results: list) -> dict[str, Any]:
        agg = {"accounts": len(results), "fetched": 0, "new": 0, "sent": 0,
               "failed": 0, "errored": 0, "baselined": 0, "errors": []}
        for r in results:
            agg["fetched"] += int(r.get("fetched", 0))
            agg["new"] += int(r.get("new", 0))
            agg["sent"] += int(r.get("sent", 0))
            agg["failed"] += int(r.get("failed", 0))
            agg["errored"] += int(r.get("errored", 0))
            if r.get("baselined"):
                agg["baselined"] += int(r.get("baselined_count", 0))
            if r.get("error"):
                agg["errors"].append(f"{r.get('account')}: {r.get('error')}")
        return agg

    @staticmethod
    def _fmt_summary(s: dict) -> str:
        if s.get("skipped"):
            return f"跳过：{s['skipped']}"
        base = (f"账号 {s.get('accounts', 0)} | 抓到 {s.get('fetched', 0)} | "
                f"新帖 {s.get('new', 0)} | 投递成功 {s.get('sent', 0)} | "
                f"失败 {s.get('failed', 0)} | 异常 {s.get('errored', 0)}")
        if s.get("baselined"):
            base += f" | 历史基线化 {s.get('baselined')}"
        return base

    # ----------------------------------------------------------------
    # 状态查询
    # ----------------------------------------------------------------
    def status_text(self) -> str:
        state = self._state
        return (f"运行中={state['running']}，插件启用={self.config.enabled}，"
                f"轮询间隔={self.config.poll_interval}s，"
                f"上次检查={time.strftime('%H:%M:%S', time.localtime(state['last_run'])) if state['last_run'] else '从未'}，"
                f"本轮时长={state['last_duration']:.1f}s，自启动投递={state['processed_since_start']}，"
                f"异常={state['errors']}，已记录去重={self.storage.count}")
