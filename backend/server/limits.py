"""请求入口的三道闸（纯 ASGI 中间件，在认证之前执行）：

- **PluginGateMiddleware**（/plugin 入口闸，契约 §5「信任边界」）：插件接口只接受本机上的插件。对路径以 /plugin/
  开头的请求按顺序检查，不过就 403 FORBIDDEN、不进后面的任何一层：
  1. 对端地址（scope["client"][0]）必须是回环（127.0.0.1、::1、::ffff:127.0.0.1）。能访问本机端口的程序被视为
     所有者自己；跨机器部署需要另加接入密钥，当前版本不支持。
  2. 请求不得带代理转发头（X-Forwarded-For、Forwarded、X-Real-IP、Via 任一个，不看值）。本机的反向代理把远端请求
     转过来时，对端与 Host 在服务端看来都是本机，第 1、3 步拦不住；代理通常会带上这些头，带了就拒绝。插件直接从
     扩展 service worker 连本机端口，从不带它们。（代理可以不带这些头，所以这只是多一道闸，不是保证：本机的反向
     代理仍然不要转发 /plugin 路径。）
  3. Host 头必须是本机名（127.0.0.1 / localhost / ::1）——**不**看 settings.host 与 PC_ALLOWED_HOSTS。
  4. Origin 头存在且是网页来源（http / https，或不透明来源 `null`）→ 拒绝。配置了 settings.plugin_extension_ids
     （PC_PLUGIN_EXTENSION_IDS）时，Origin 还必须是 `chrome-extension://<名单里的 ID>`，没带 Origin 也拒绝。
  5. POST 必须带 `Content-Type: application/json`（允许 `; charset=…`）。网页要带这个头就得先发预检，而本服务的
     OPTIONS 不给 CORS 头，所以网页的表单或 no-cors 请求进不来（包括不需要令牌的 /plugin/v1/register）。
- **HostOriginMiddleware**（防 DNS 重绑定）：Host 头必须是本机地址（127.0.0.1、localhost、::1）、settings.host
  或 settings.allowed_hosts（PC_ALLOWED_HOSTS，逗号分隔）里的名字，否则 403 FORBIDDEN——包括没有登录要求的
  /api/v1/auth/setup 与 /auth/login。/api 下的写请求带 Origin 时，Origin 的主机名也必须在名单里
  （/plugin 的 Origin 由上面的入口闸检查）。
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

#: /plugin 入口闸管的路径前缀
PLUGIN_PATH_PREFIX = "/plugin/"
#: 回环对端（IPv4、IPv6、以及双栈套接字上映射成 IPv6 的 IPv4）
LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
EXTENSION_ORIGIN_SCHEME = "chrome-extension"
PLUGIN_REMOTE_MESSAGE = "插件接口只接受本机请求：跨机器部署需要接入密钥，当前版本不支持"
#: 代理转发头（小写）：带了任何一个，就说明这次请求是经代理转来的
PROXY_HEADERS: tuple[bytes, ...] = (b"x-forwarded-for", b"forwarded", b"x-real-ip", b"via")
PLUGIN_PROXIED_MESSAGE = "插件接口不接受经过代理转发的请求"
PLUGIN_HOST_MESSAGE = "插件接口只接受发往本机地址（127.0.0.1 或 localhost）的请求：请在插件设置里把服务端地址改成本机地址"
PLUGIN_WEB_ORIGIN_MESSAGE = "插件接口不接受网页来源的请求"
PLUGIN_EXTENSION_MESSAGE = "插件接口只接受名单里的扩展：这个扩展的 ID 不在 PC_PLUGIN_EXTENSION_IDS 里"
PLUGIN_JSON_MESSAGE = "插件接口只接受 JSON 请求"


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


def is_plugin_path(path: str) -> bool:
    """输入：请求路径 → 输出：是否归 /plugin 入口闸管（以 /plugin/ 开头）。"""
    return path.startswith(PLUGIN_PATH_PREFIX)


def _extension_id(origin: str | None) -> str | None:
    """输入：Origin 头 → 输出：`chrome-extension://<ID>` 里的扩展 ID（小写）；不是这种来源 → None。"""
    if not origin:
        return None
    parts = urlsplit(origin.strip())
    if parts.scheme.lower() != EXTENSION_ORIGIN_SCHEME or not parts.netloc:
        return None
    return parts.netloc.lower()


def _is_json(content_type: str | None) -> bool:
    return content_type is not None and content_type.split(";", 1)[0].strip().lower() == "application/json"


def _has_header(scope: dict[str, Any], names: tuple[bytes, ...]) -> bool:
    """输入：ASGI scope、请求头名（小写）→ 输出：请求是否带了其中任何一个（不看值，空值也算）。"""
    return any(k.lower() in names for k, _ in scope.get("headers") or [])


class PluginGateMiddleware:
    """/plugin 入口闸：本机对端、不带代理转发头、本机 Host、不接受网页 Origin、可选的扩展 ID 名单、POST 必须是 JSON
    （见模块说明）。

    extension_ids：允许的扩展 ID（不分大小写）；空 = 不按扩展 ID 限制，只拒绝网页来源。
    """

    def __init__(self, app: Any, *, extension_ids: Iterable[str] = ()) -> None:
        self.app = app
        self.extension_ids = frozenset(x.strip().lower() for x in extension_ids if x and x.strip())

    def refusal(self, scope: dict[str, Any]) -> str | None:
        """输入：ASGI scope → 输出：拒绝这次 /plugin 请求的原因；可以放行 → None。"""
        client = scope.get("client")
        peer = client[0] if client else None
        if not isinstance(peer, str) or peer.strip().lower() not in LOOPBACK_PEERS:
            return PLUGIN_REMOTE_MESSAGE
        if _has_header(scope, PROXY_HEADERS):
            return PLUGIN_PROXIED_MESSAGE
        host = _header(scope, b"host")
        if host is None or _host_name(host) not in LOCAL_HOSTS:
            return PLUGIN_HOST_MESSAGE
        origin = _header(scope, b"origin")
        if origin is not None:
            value = origin.strip().lower()
            if value == "null" or urlsplit(value).scheme in ("http", "https"):
                return PLUGIN_WEB_ORIGIN_MESSAGE
        if self.extension_ids and _extension_id(origin) not in self.extension_ids:
            return PLUGIN_EXTENSION_MESSAGE
        if scope.get("method", "GET").upper() == "POST" and not _is_json(_header(scope, b"content-type")):
            return PLUGIN_JSON_MESSAGE
        return None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not is_plugin_path(scope.get("path", "")):
            await self.app(scope, receive, send)       # 本服务没有 WebSocket 路由，/plugin 只有 HTTP
            return
        reason = self.refusal(scope)
        if reason is not None:
            await _send_error(send, 403, "FORBIDDEN", reason)
            return
        await self.app(scope, receive, send)


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
