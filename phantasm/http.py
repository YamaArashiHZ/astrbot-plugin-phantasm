"""统一 HTTP 请求层。

集中处理网络请求的代理、超时、重试与错误分类，供所有平台抓取器复用。

约定：
- 网络类错误（连接/超时/代理）与 5xx 会按 ``retry_backoff_base`` 退避重试；
- HTTP 429 视为限流，明确抛出 :class:`RateLimitedError`；
- 4xx 业务错误（如 bilibili -412 风控、401 鉴权）不盲目重试，原样抛出；
- 任何异常都被包装成 :class:`FetchError`，带人类可读的 ``message``，不裸抛 httpx 异常。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Optional

import httpx

try:  # httpx 0.27+ 打包进 httpx；旧版本无此别名也无妨
    from httpx import NetworkError as _NetErr
except Exception:  # noqa: BLE001
    _NetErr = httpx.TransportError  # type: ignore[assignment]

_NETWORK_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.TimeoutException,
    httpx.ProxyError,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class FetchError(Exception):
    """抓取过程中可预期/可展示的错误。"""


class RateLimitedError(FetchError):
    """触发平台限流（HTTP 429 或平台自定义限流码）。"""


def _redact_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    out = dict(headers or {})
    for k in list(out.keys()):
        if any(s in k.lower() for s in ("cookie", "authorization", "token", "key", "secret")):
            out[k] = "[REDACTED]"
    return out


class HttpClient:
    """配置驱动的异步 HTTP 客户端。

    通过 :meth:`configure` 注入 network 段配置（proxy/timeout/retry），
    然后调用 :meth:`get_json` / :meth:`post_json` / :meth:`get_bytes`。
    """

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._cfg: dict[str, Any] = {}

    def configure(self, network_cfg: dict[str, Any]) -> None:
        self._cfg = dict(network_cfg or {})

    def _proxy(self) -> str | None:
        if self._cfg.get("proxy_enabled"):
            return (self._cfg.get("proxy") or "").strip() or None
        return None

    def _timeout(self) -> float:
        try:
            return float(self._cfg.get("timeout_seconds", 15))
        except (TypeError, ValueError):
            return 15.0

    def _retries(self) -> int:
        try:
            return max(0, int(self._cfg.get("max_retries", 3)))
        except (TypeError, ValueError):
            return 3

    def _backoff(self) -> float:
        try:
            return float(self._cfg.get("retry_backoff_base", 1.5))
        except (TypeError, ValueError):
            return 1.5

    # ----------------------------------------------------------------
    async def get_json(self, url: str, headers: Mapping[str, str] | None = None,
                       params: Mapping[str, Any] | None = None,
                       timeout: float | None = None) -> dict:
        return await self._request("GET", url, headers=headers, params=params, timeout=timeout)

    async def post_json(self, url: str, headers: Mapping[str, str] | None = None,
                        payload: Mapping[str, Any] | None = None,
                        timeout: float | None = None) -> dict:
        return await self._request("POST", url, headers=headers, json=payload, timeout=timeout)

    async def get_bytes(self, url: str, headers: Mapping[str, str] | None = None,
                        timeout: float | None = None) -> bytes:
        try:
            async with httpx.AsyncClient(
                    proxy=self._proxy(),
                    timeout=httpx.Timeout(timeout or self._timeout()),
                    follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPStatusError as e:
            raise FetchError(f"图片下载失败（HTTP {e.response.status_code}）：{url}") from e
        except _NETWORK_ERRORS as e:
            raise FetchError(f"图片下载失败（网络）：{url}：{type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise FetchError(f"图片下载失败：{e}") from e

    # ----------------------------------------------------------------
    async def _request(self, method: str, url: str, *, headers=None, params=None,
                       json=None, timeout: float | None = None) -> dict:
        retries = self._retries()
        timeout = timeout or self._timeout()
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            self.logger.debug(
                f"HTTP {method} {url} (第{attempt + 1}次) headers={_redact_headers(headers)!r}")
            try:
                async with httpx.AsyncClient(
                        proxy=self._proxy(),
                        timeout=httpx.Timeout(timeout),
                        follow_redirects=True) as client:
                    resp = await client.request(method, url, headers=headers,
                                                params=params, json=json)
                if resp.status_code == 429:
                    raise RateLimitedError("请求过于频繁，触发限流（HTTP 429），已建议降低轮询频率")
                if resp.status_code >= 500:
                    raise RuntimeError(f"平台服务端错误（HTTP {resp.status_code}）")
                if resp.status_code >= 400:
                    body = (resp.text or "")[:160]
                    raise FetchError(f"平台返回错误（HTTP {resp.status_code}）：{body}")
                try:
                    return resp.json()
                except ValueError:
                    raise FetchError("平台返回了非 JSON 内容")
            except RateLimitedError:
                raise
            except _NETWORK_ERRORS as e:
                last_err = e
                self.logger.warning(f"网络错误（第{attempt + 1}次）：{type(e).__name__}: {e}")
            except httpx.HTTPStatusError as e:  # 理论不可达，防御
                last_err = FetchError(f"HTTP 状态 {e.response.status_code}")
            except httpx.HTTPError as e:
                last_err = FetchError(f"网络请求失败：{e}")
                self.logger.warning(f"网络请求失败：{e}")
            if attempt < retries:
                await asyncio.sleep(self._backoff() * (attempt + 1))
        raise FetchError(self._describe(last_err, url))

    @staticmethod
    def _describe(err: Exception | None, url: str) -> str:
        if isinstance(err, FetchError):
            return str(err)
        if isinstance(err, httpx.ProxyError):
            return "无法连接代理，请检查代理地址或关闭代理"
        if isinstance(err, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout)):
            return "请求超时，请检查网络与代理设置"
        if isinstance(err, httpx.ConnectError):
            return "无法连接平台，请检查网络与代理设置"
        return f"网络请求失败：{err}（{url}）"
