"""进程内的失败次数限速（本机部署，单进程）：登录与插件配对的暴力尝试。

- POST /api/v1/auth/login：键 = 客户端 IP + 用户名（去空白、小写）；窗口内失败满 settings.login_fail_limit 次 → 429。
- POST /plugin/v1/pair：键 = 客户端 IP；窗口内失败满 settings.pair_fail_limit 次 → 429。
- 失败 = 响应状态 401 / 404 / 409 / 422；登录成功清掉该键的失败记录。窗口 = settings.rate_window_s（默认 600 秒）。
- 被限速时不调用后面的应用（不验证密码、不消耗配对码），返回
  ``429 {code: 'RATE_LIMITED', message: '尝试次数过多，请稍后再试', detail: {retry_after_s}}`` 与 Retry-After 头。

纯 ASGI 中间件：要读登录请求体里的用户名，先把请求体读进来再原样交给后面的应用（请求体大小由外层的
bodylimit 中间件限制）。时间取 services.clock（测试可冻结）。
"""

from __future__ import annotations

import json
import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

from registry import settings
from server.serialize import error_body
from services import clock

FAILED_STATUSES = frozenset({401, 404, 409, 422})
RATE_LIMITED_MESSAGE = "尝试次数过多，请稍后再试"


@dataclass(frozen=True)
class Rule:
    """一条限速规则：方法、路径、窗口内允许的失败次数、键里是否带用户名。"""

    method: str
    path: str
    limit: int
    by_username: bool


class FailureLimiter:
    """按键记录失败时刻（滑动窗口）。线程安全；只存在于本进程内存里。"""

    def __init__(self, window_s: int) -> None:
        self.window_s = max(1, int(window_s))
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._fails.get(key)
        if q is None:
            return deque()
        while q and q[0] <= now - self.window_s:
            q.popleft()
        if not q:
            self._fails.pop(key, None)
        return q

    def retry_after(self, key: str, limit: int, now: float) -> int | None:
        """输入：键、上限、当前时刻（秒）→ 输出：被限速时还要等的秒数（≥1），没被限速 → None。"""
        with self._lock:
            q = self._prune(key, now)
            if len(q) < limit:
                return None
            return max(1, math.ceil(q[len(q) - limit] + self.window_s - now))

    def fail(self, key: str, now: float) -> None:
        """输入：键、当前时刻 → 输出：无（记一次失败）。"""
        with self._lock:
            self._prune(key, now)
            self._fails.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        """输入：键 → 输出：无（清掉该键的失败记录）。"""
        with self._lock:
            self._fails.pop(key, None)


def default_rules(login_path: str, pair_path: str) -> tuple[Rule, ...]:
    """输入：登录与配对的完整路径 → 输出：按 registry.settings 的上限生成的两条规则。"""
    s = settings.get()
    return (Rule("POST", login_path, int(s.login_fail_limit), True),
            Rule("POST", pair_path, int(s.pair_fail_limit), False))


class RateLimitMiddleware:
    """ASGI 中间件：对 rules 里的请求按失败次数限速（见模块说明）。"""

    def __init__(self, app: Any, *, rules: tuple[Rule, ...], window_s: int | None = None) -> None:
        self.app = app
        self.rules = {(r.method, r.path): r for r in rules}
        self.limiter = FailureLimiter(window_s if window_s is not None else settings.get().rate_window_s)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rule = self.rules.get((scope["method"].upper(), scope["path"].rstrip("/") or "/"))
        if rule is None:
            await self.app(scope, receive, send)
            return
        body, receive = await _buffer(receive)
        ip = (scope.get("client") or ("unknown", 0))[0]
        key = f"{rule.path}|{ip}"
        if rule.by_username:
            key += "|" + _username(body)
        now = clock.now().timestamp()
        wait = self.limiter.retry_after(key, rule.limit, now)
        if wait is not None:
            await _send_json(send, 429, error_body("RATE_LIMITED", RATE_LIMITED_MESSAGE, {"retry_after_s": wait}),
                             {"retry-after": str(wait)})
            return
        status: list[int] = []

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status.append(int(message["status"]))
            await send(message)

        await self.app(scope, receive, send_wrapper)
        if status and status[0] in FAILED_STATUSES:
            self.limiter.fail(key, clock.now().timestamp())
        elif status and 200 <= status[0] < 300 and rule.by_username:
            self.limiter.reset(key)


def _username(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return ""
    name = data.get("username") if isinstance(data, dict) else None
    return name.strip().lower()[:200] if isinstance(name, str) else ""


async def _buffer(receive: Any) -> tuple[bytes, Any]:
    """读完请求体，返回 (请求体, 能把它原样再交一次的 receive)。"""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    sent = False

    async def replay() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, replay


async def _send_json(send: Any, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(raw)).encode())]
    hdrs += [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": raw})
