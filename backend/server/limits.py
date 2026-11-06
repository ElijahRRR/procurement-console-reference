"""请求入口的两道闸（纯 ASGI 中间件，在认证之前执行）：

- **HostOriginMiddleware**（防 DNS 重绑定）：Host 头必须是本机地址（127.0.0.1、localhost、::1）、settings.host
  或 settings.allowed_hosts（PC_ALLOWED_HOSTS，逗号分隔）里的名字，否则 403 FORBIDDEN——包括没有登录要求的
  /api/v1/auth/setup、/auth/login 与 /plugin/v1/pair。/api 下的写请求带 Origin 时，Origin 的主机名也必须在名单里
  （插件从扩展 service worker 调 /plugin，Origin 是 chrome-extension://，不检查）。
- **BodyLimitMiddleware**：按路由限制请求体字节数，不管有没有 Content-Length（分块上传也算）：
  POST /api/v1/imports 为上传上限 + multipart 余量，其他一律 1 MB。超过 → 413，不等认证、不把整个请求体读进内存。
  Content-Length 已超限的直接拒绝；否则边读边数，超过就停（读到的部分交给应用之前就拒绝）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from server.serialize import error_body

#: 本机地址（永远接受）
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: 除上传外的请求体上限
DEFAULT_BODY_LIMIT = 1024 * 1024
FORBIDDEN_HOST_MESSAGE = "不接受这个主机名的请求：只接受本机地址；其他地址请在 PC_ALLOWED_HOSTS 里登记"
FORBIDDEN_ORIGIN_MESSAGE = "请求来源（Origin）与控制台地址不符"
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _host_name(value: str) -> str:
    """'127.0.0.1:8790' / '[::1]:8790' / 'Example.Local' → 小写主机名（去端口与方括号）。"""
    v = value.strip().lower()
    if v.startswith("["):
        return v[1:v.find("]")] if "]" in v else v[1:]
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


async def _send_error(send: Any, status: int, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
    raw = json.dumps(error_body(code, message, detail), ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(raw)).encode()), (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": raw})


class HostOriginMiddleware:
    """拒绝 Host 不在名单里的请求、以及 Origin 不在名单里的 /api 写请求（见模块说明）。"""

    def __init__(self, app: Any, *, allowed_hosts: Iterable[str]) -> None:
        self.app = app
        self.allowed = frozenset(LOCAL_HOSTS | {_host_name(h) for h in allowed_hosts if h and h.strip()})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = _header(scope, b"host")
        if host is None or _host_name(host) not in self.allowed:
            await _send_error(send, 403, "FORBIDDEN", FORBIDDEN_HOST_MESSAGE)
            return
        method = scope.get("method", "GET").upper()
        origin = _header(scope, b"origin")
        if origin and scope["path"].startswith("/api/") and method not in ("GET", "HEAD", "OPTIONS"):
            parts = urlsplit(origin)
            if parts.scheme not in ("http", "https") or _host_name(parts.netloc) not in self.allowed:
                await _send_error(send, 403, "FORBIDDEN", FORBIDDEN_ORIGIN_MESSAGE)
                return
        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    """按路由限制请求体大小（见模块说明）。limits：{(方法, 路径): 字节上限}，其余用 default_limit。"""

    def __init__(self, app: Any, *, limits: dict[tuple[str, str], int], too_large: Any,
                 default_limit: int = DEFAULT_BODY_LIMIT) -> None:
        self.app = app
        self.limits = limits
        self.too_large = too_large          # (上限) → (code, message, detail)
        self.default_limit = default_limit

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        limit = self.limits.get((scope["method"].upper(), scope["path"].rstrip("/") or "/"), self.default_limit)
        raw_len = _header(scope, b"content-length")
        try:
            declared = int(raw_len) if raw_len is not None else None
        except ValueError:
            declared = None
        if declared is not None and declared > limit:
            await _send_error(send, 413, *self.too_large(limit))
            return
        chunks: list[bytes] = []
        total = 0
        disconnect: dict[str, Any] | None = None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                disconnect = message
                break
            body = message.get("body", b"")
            total += len(body)
            if total > limit:
                await _send_error(send, 413, *self.too_large(limit))
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break
        data = b"".join(chunks)
        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                if disconnect is not None:
                    return disconnect
                return {"type": "http.request", "body": data, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
