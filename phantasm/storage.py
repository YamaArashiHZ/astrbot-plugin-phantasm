"""已处理帖子去重存储。

用 ``data/plugin_data/astrbot_plugin_phantasm/processed.json`` 持久化「已投递 /
已处理」的帖子全局去重键（``<platform>_<post_id>``）。提供有界历史，避免无限膨胀。
"""
from __future__ import annotations

import json
import logging
import threading
from collections import deque
from pathlib import Path
from typing import Iterable


class StorageManager:
    def __init__(self, data_dir: Path, logger: logging.Logger, history_limit: int = 5000):
        self.data_dir = Path(data_dir)
        self.logger = logger
        self.path = self.data_dir / "processed.json"
        self.history_limit = max(100, int(history_limit))
        self._lock = threading.Lock()
        self._seen: deque[str] = deque()
        self._set: set[str] = set()
        self._baselined: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                items = data.get("processed", []) if isinstance(data, dict) else data
                if isinstance(items, list):
                    for k in items:
                        k = str(k)
                        if k and k not in self._set:
                            self._set.add(k)
                            self._seen.append(k)
                if isinstance(data, dict):
                    for k in data.get("baselined", []) or []:
                        self._baselined.add(str(k))
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"去重记录读取失败，已重置：{e}")
            self._set.clear()
            self._seen.clear()
            self._baselined.clear()
        self._prune(do_write=False)

    def _prune(self, do_write: bool = True) -> None:
        while len(self._seen) > self.history_limit:
            old = self._seen.popleft()
            self._set.discard(old)
        if do_write:
            self._write()

    def _write(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({
                    "processed": list(self._seen),
                    "baselined": sorted(self._baselined),
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"去重记录保存失败：{e}")

    def is_processed(self, key: str) -> bool:
        return key in self._set

    def is_baselined(self, account_key: str) -> bool:
        return account_key in self._baselined

    def mark_baselined(self, account_key: str) -> None:
        with self._lock:
            self._baselined.add(account_key)
            self._write()

    def mark_processed(self, key: str) -> None:
        if key in self._set:
            return
        with self._lock:
            if key in self._set:
                return
            self._set.add(key)
            self._seen.append(key)
            self._prune()

    def mark_processed_many(self, keys: Iterable[str]) -> int:
        """批量标记，返回新增数量。"""
        added = 0
        for k in keys:
            if k and k not in self._set:
                added += 1
                self.mark_processed(k)
        return added

    def prune_if_needed(self, config_limit: int | None = None) -> None:
        """外部可调用以按配置裁剪历史。"""
        if config_limit:
            self.history_limit = max(100, int(config_limit))
        self._prune()

    @property
    def count(self) -> int:
        return len(self._set)

    def recent(self, n: int = 20) -> list[str]:
        return list(self._seen)[-n:]

    def clear(self) -> int:
        with self._lock:
            n = len(self._set)
            self._set.clear()
            self._seen.clear()
            self._baselined.clear()  # 一并清掉基线，避免清空后重新刷历史
            self._write()
        return n
