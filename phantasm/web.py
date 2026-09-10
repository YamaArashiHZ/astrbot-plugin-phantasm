"""插件设置页后端 API（以 Mixin 形式提供，由 main.py 注册路由）。

安全说明：``GET /config`` 返回的凭据一律脱敏（返回占位掩码），且
``POST /config`` 保存时，凡凭据字段值是「掩码」或空串的，一律保留原值，
绝不把脱敏后的字符串写回配置。
"""
from __future__ import annotations

import copy
import json
from typing import Any

from astrbot.api.web import error_response, json_response, request

from .config import DEFAULT_CONFIG, _deep_merge, mask_secret


def _is_masked(value: Any) -> bool:
    """是否为「脱敏占位值」。

    ``mask_secret`` 会保留尾部若干真实字符（如 ``******abcd``），所以不能要求
    全为 ``*``；这里只要有连续 3 个以上 ``*`` 就认为是掩码（真实凭据几乎不可能含 ``***``）。
    """
    if not isinstance(value, str) or not value:
        return False
    return "***" in value


class WebMixin:
    # ---- GET/SET 配置 ----
    async def web_get_config(self):
        cfg = self._config_for_web()
        return json_response(cfg)

    async def web_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容格式无效", status_code=400)
        old_token = str((self.config.credentials.get("x") or {}).get("rsshub_auth_token") or "")
        try:
            self._apply_web_save(payload)
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"配置保存失败：{e}")
            return error_response(f"保存配置失败：{e}", status_code=500)
        self._reconfigure()
        out: dict[str, Any] = {"saved": True}
        # RSSHub Auth_Token 变化且开启自动应用 → 重建 RSSHub 容器使其生效
        x = self.config.credentials.get("x") or {}
        new_token = str(x.get("rsshub_auth_token") or "")
        if new_token and new_token != old_token and x.get("rsshub_auto_apply", True):
            ok, msg = await self._apply_rsshub_token(new_token,
                                                     str(x.get("rsshub_container") or "rsshub"))
            out["rsshub_apply"] = {"ok": ok, "message": msg}
        return json_response(out)

    async def web_apply_rsshub_token(self):
        """手动把当前配置里的 RSSHub Auth_Token 应用到容器（重建 RSSHub）。"""
        x = self.config.credentials.get("x") or {}
        token = str(x.get("rsshub_auth_token") or "")
        if not token:
            return error_response("未配置 RSSHub Auth_Token", status_code=400)
        ok, msg = await self._apply_rsshub_token(token, str(x.get("rsshub_container") or "rsshub"))
        return json_response({"ok": ok, "message": msg})

    async def _apply_rsshub_token(self, token: str, container: str) -> tuple[bool, str]:
        from .dockerctl import DockerControl
        ctl = DockerControl(self.logger, getattr(self, "docker_socket", "/var/run/docker.sock"))
        if not await ctl.ping():
            return False, ("无法访问 docker socket（需给 AstrBot 容器挂载 "
                           "-v /var/run/docker.sock:/var/run/docker.sock）；"
                           "否则请手动更新 RSSHub 的 TWITTER_AUTH_TOKEN")
        return await ctl.recreate_with_env(container, {"TWITTER_AUTH_TOKEN": token})

    # ---- 状态 / 统计 ----
    async def web_get_status(self):
        return json_response(self._status_payload())

    # ---- 操作 ----
    async def web_trigger_check(self):
        summary = await self.poller.check_once()
        return json_response(summary)

    async def web_clear_history(self):
        n = self.storage.clear()
        return json_response({"cleared": n})

    async def web_get_cards(self):
        import glob as _g
        img_dir = self.data_dir / str(self.config.image.get("dir", "cards"))
        files = []
        for p in sorted(img_dir.glob("phantasm_*"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
            try:
                files.append({"name": p.name, "size": p.stat().st_size,
                              "mtime": p.stat().st_mtime})
            except OSError:
                pass
        return json_response({"dir": str(img_dir), "files": files})

    # ----------------------------------------------------------------
    # 内部
    # ----------------------------------------------------------------
    def _config_for_web(self) -> dict[str, Any]:
        cfg = copy.deepcopy(self.config.config)
        try:
            from . import __version__ as _v
            cfg["version"] = _v
        except Exception:  # noqa: BLE001
            pass
        # 凭据脱敏（不回显明文）
        creds = cfg.setdefault("credentials", {})
        bil = creds.setdefault("bilibili", {})
        if bil.get("cookie"):
            bil["cookie"] = mask_secret(bil["cookie"], 6)
        x = creds.setdefault("x", {})
        for key in ("bearer_token", "api_key", "api_secret", "access_token", "access_token_secret",
                    "rsshub_auth_token"):
            if x.get(key):
                x[key] = mask_secret(x[key], 4)
        return cfg

    def _apply_web_save(self, payload: dict) -> None:
        """把前端 payload 合并进配置；凭据字段若是掩码/空则保留原值。"""
        # 先移除掩码凭据（避免把掩码写回）
        safe = copy.deepcopy(payload)
        creds = safe.get("credentials")
        if isinstance(creds, dict):
            bil = creds.get("bilibili")
            if isinstance(bil, dict) and _is_masked(bil.get("cookie")):
                bil.pop("cookie", None)
            x = creds.get("x")
            if isinstance(x, dict):
                for k in ("bearer_token", "api_key", "api_secret", "access_token", "access_token_secret"):
                    if _is_masked(x.get(k)):
                        x.pop(k, None)
        # 在合并前补齐凭据：掩码/空 用当前真实值填充
        self._preserve_credentials(safe)
        # 账号代称：若 payload 某账号缺 display_name，用当前配置补齐（避免保存后变回 id）
        self._preserve_accounts(safe)
        # 再深合并（数组整体替换），保存
        merged = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), safe)
        self.config.config = merged
        self.config.save()
        self.storage.prune_if_needed(self.config.storage.get("history_limit"))

    def _preserve_accounts(self, safe: dict) -> None:
        cur = self.config.accounts()
        cur_by_key = {a.key: a for a in cur}
        accs = safe.get("accounts")
        if not isinstance(accs, list):
            return
        for a in accs:
            if not isinstance(a, dict):
                continue
            # 清理旧字段名 name（若存在且无 display_name）
            if "name" in a:
                a.setdefault("display_name", a["name"])
                a.pop("name", None)
            if "display_name" not in a:
                c = cur_by_key.get(f"{a.get('platform','')}:{a.get('account_id','')}")
                if c is not None:
                    a["display_name"] = c.display_name or ""
            if "filter_retweet" not in a:
                c = cur_by_key.get(f"{a.get('platform','')}:{a.get('account_id','')}")
                if c is not None:
                    a["filter_retweet"] = bool(c.filter_retweet)

    def _preserve_credentials(self, safe: dict) -> None:
        # 对于 payload 中缺失/为空的凭据，用当前配置中的真实值补齐
        cur = self.config.credentials
        creds = safe.get("credentials") or {}
        def fill(platform: str, keys: list[str]):
            cur_p = cur.get(platform) or {}
            safe_p = creds.get(platform)
            if not isinstance(safe_p, dict):
                return
            for k in keys:
                if not safe_p.get(k) or _is_masked(safe_p.get(k)):
                    safe_p[k] = cur_p.get(k, "")
        fill("bilibili", ["cookie"])
        fill("x", ["bearer_token", "api_key", "api_secret", "access_token", "access_token_secret",
                   "rsshub_auth_token"])

    def _reconfigure(self) -> None:
        self.http.configure(self.config.network)
        self.renderer.render_cfg = self.config.render
        self.renderer.image_cfg = self.config.image
        self.renderer.send_cfg = self.config.send
        self.sender.send_cfg = self.config.send
        # HTML 渲染器是独立对象，需同步更新它引用的配置（否则主题/黑白不生效）
        html = getattr(self.renderer, "_html_renderer", None)
        if html is not None:
            html.render_cfg = self.config.render
            html.send_cfg = self.config.send

    def _status_payload(self) -> dict[str, Any]:
        accs = self.config.accounts()
        return {
            "enabled": self.config.enabled,
            "poll_interval": self.config.poll_interval,
            "running": self.poller._state.get("running", False),
            "last_run": self.poller._state.get("last_run", 0),
            "processed_since_start": self.poller._state.get("processed_since_start", 0),
            "errors": self.poller._state.get("errors", 0),
            "storage_count": self.storage.count,
            "account_count": len(accs),
            "enabled_accounts": sum(1 for a in accs if a.enabled),
            "accounts": [
                {"platform": a.platform, "id": a.account_id, "name": a.name,
                 "enabled": a.enabled, "targets": a.target_labels()}
                for a in accs
            ],
        }
