"""通过 Docker API 重建容器（用于把 RSSHub 的 Auth_Token 从 WebUI 直接生效）。

RSSHub 的 ``TWITTER_AUTH_TOKEN`` 只在容器**环境变量**里生效，官方路由参数不支持
单次请求传 token。因此插件提供「在 WebUI 填写 token → 自动重建 RSSHub 容器」的能力：

- 需要把宿主机的 docker socket 挂进 AstrBot 容器
  （``-v /var/run/docker.sock:/var/run/docker.sock``）；
- 未挂载时 :meth:`DockerControl.available` 返回 False，上层会退化为「给出待执行命令」。

重建流程（尽量保留原配置）：inspect → stop → remove → create(合并 env) → start。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

DEFAULT_SOCKET = "/var/run/docker.sock"

# 重建时保留的 HostConfig 字段（安全子集）
# 注意：不含 Mounts —— inspect 会把 Binds 同时展开成 Mounts，两个都传会「duplicate mount point」
_KEEP_HOST_CONFIG = (
    "PortBindings", "RestartPolicy", "Binds", "NetworkMode", "ExtraHosts",
    "AutoRemove", "CapAdd", "CapDrop", "Devices", "Dns", "DnsSearch",
    "LogConfig", "Privileged", "ReadonlyRootfs", "Tmpfs", "Ulimits",
)


class DockerControl:
    """极简 Docker API 客户端（仅 unix socket，够用即可）。"""

    def __init__(self, logger: logging.Logger, socket_path: str = DEFAULT_SOCKET):
        self.logger = logger
        self.socket_path = socket_path or DEFAULT_SOCKET

    # ----------------------------------------------------------------
    def available(self) -> bool:
        try:
            return os.path.exists(self.socket_path)
        except OSError:
            return False

    def _client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path)
        return httpx.AsyncClient(transport=transport, base_url="http://docker",
                                 timeout=httpx.Timeout(60.0), trust_env=False)

    async def ping(self) -> bool:
        if not self.available():
            return False
        try:
            async with self._client() as c:
                r = await c.get("/_ping")
                return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def inspect(self, name: str) -> Optional[dict]:
        try:
            async with self._client() as c:
                r = await c.get(f"/containers/{name}/json")
                if r.status_code != 200:
                    return None
                return r.json()
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"inspect 容器 {name} 失败：{e}")
            return None

    # ----------------------------------------------------------------
    async def recreate_with_env(self, name: str, env_updates: dict[str, str],
                                image_override: str = "") -> tuple[bool, str]:
        """用合并后的环境变量重建容器（保留端口/挂载/重启策略等）。"""
        if not await self.ping():
            return False, (f"无法访问 docker socket（{self.socket_path}）。"
                           "请把 -v /var/run/docker.sock:/var/run/docker.sock 挂给 AstrBot 容器后重试")
        info = await self.inspect(name)
        if not info:
            return False, f"未找到容器 {name}（可在配置里改 rsshub_container）"

        cfg = info.get("Config") or {}
        host = info.get("HostConfig") or {}

        # 合并 env
        env_list = list(cfg.get("Env") or [])
        merged: dict[str, str] = {}
        order: list[str] = []
        for item in env_list:
            if "=" in item:
                k, _, v = item.partition("=")
                if k not in merged:
                    order.append(k)
                merged[k] = v
        for k, v in (env_updates or {}).items():
            if k not in merged:
                order.append(k)
            merged[k] = str(v)
        new_env = [f"{k}={merged[k]}" for k in order]

        new_cfg: dict[str, Any] = {"Image": image_override or info.get("Image") or cfg.get("Image"),
                                   "Env": new_env}
        # 注意：不复制 Hostname（容器重建后 id 变化，沿用旧 hostname 会指向已删除的容器）
        for key in ("Labels", "WorkingDir", "ExposedPorts", "Entrypoint", "Cmd", "User"):
            if cfg.get(key):
                new_cfg[key] = cfg[key]

        new_host: dict[str, Any] = {}
        for key in _KEEP_HOST_CONFIG:
            if host.get(key):
                new_host[key] = host[key]

        # 保留自定义网络的别名（RSSHub 常与 AstrBot 同网络并用服务名互访）
        net_cfg: dict[str, Any] = {}
        try:
            for net, nc in ((info.get("NetworkSettings") or {}).get("Networks") or {}).items():
                aliases = [a for a in (nc.get("Aliases") or []) if a and len(a) != 12]
                if aliases:
                    net_cfg[net] = {"Aliases": aliases}
        except Exception:  # noqa: BLE001
            net_cfg = {}

        # 换名保留原容器做回滚（失败时恢复）
        backup = f"{name}__phantasm_bak"
        rolled = False
        try:
            async with self._client() as c:
                await c.post(f"/containers/{name}/stop", params={"t": 10})
                # 先把旧容器改名（保住它），再创建同名新容器
                await c.post(f"/containers/{name}/rename", params={"name": backup})
                payload: dict[str, Any] = {
                    "Image": new_cfg["Image"], "Env": new_env,
                    "Labels": new_cfg.get("Labels") or {},
                    "HostConfig": new_host,
                    **{k: new_cfg[k] for k in
                       ("WorkingDir", "ExposedPorts", "Entrypoint", "Cmd", "User")
                       if k in new_cfg},
                }
                if net_cfg:
                    payload["NetworkingConfig"] = {"EndpointsConfig": net_cfg}
                r = await c.post("/containers/create", params={"name": name}, json=payload)
                if r.status_code not in (200, 201):
                    # 回滚：把旧容器改回原名并启动
                    rolled = True
                    await c.post(f"/containers/{backup}/rename", params={"name": name})
                    await c.post(f"/containers/{name}/start")
                    return False, f"创建新容器失败（HTTP {r.status_code}）：{r.text[:300]}"
                new_id = (r.json() or {}).get("Id", "")
                st = await c.post(f"/containers/{name}/start")
                if st.status_code not in (204, 304):
                    # 回滚：删掉新容器，恢复旧容器
                    rolled = True
                    await c.delete(f"/containers/{name}", params={"force": True})
                    await c.post(f"/containers/{backup}/rename", params={"name": name})
                    await c.post(f"/containers/{name}/start")
                    return False, f"新容器启动失败（HTTP {st.status_code}），已回滚旧容器：{st.text[:200]}"
                # 成功：删掉备份容器
                await c.delete(f"/containers/{backup}", params={"force": True})
            self.logger.info(f"已用新环境变量重建容器 {name}（id={new_id[:12]}）")
            return True, f"已重建容器 {name} 并重启，环境变量已更新"
        except Exception as e:  # noqa: BLE001
            self.logger.exception(f"重建容器 {name} 异常：{e}")
            if not rolled:
                # 尽力恢复：把备份改回原名并启动
                try:
                    async with self._client() as c:
                        if not await self.inspect(name):
                            await c.post(f"/containers/{backup}/rename", params={"name": name})
                        await c.post(f"/containers/{name}/start")
                except Exception as e2:  # noqa: BLE001
                    self.logger.error(f"回滚失败，请手动检查容器 {name}：{e2}")
            return False, f"重建容器异常：{e}"
