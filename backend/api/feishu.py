"""飞书开放平台适配：应用身份认证、重试、分页、限速。只做接口适配，不含任何业务判断。

用到的接口（请求 / 响应形状按飞书开放平台官方文档，统一为 ``{code, msg, data}`` 信封）：

- ``POST /open-apis/auth/v3/tenant_access_token/internal``：应用身份令牌（响应不在 data 里：
  ``{code, msg, tenant_access_token, expire}``）；缓存到 ``expire − 300`` 秒。
- ``GET  /open-apis/bitable/v1/apps/{app_token}/tables``：数据表列表（page_token / has_more 分页）。
- ``GET  /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields``：字段列表。
- ``POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search``：查询记录（分页 ≤500）。
- ``GET  /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}``：读一条记录。
- ``POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update``：批量改记录（每批 ≤500）。
- ``GET  /open-apis/wiki/v2/spaces/get_node?token=``：知识库节点 → 实际文档 obj_token。

重试：HTTP 429 / 5xx、网络错误、飞书限流与暂时性错误码按指数退避重试（最多 max_retries 次）；
令牌失效错误码先强制刷新令牌再重发一次（不占重试次数）。其余错误抛 FeishuError。
密钥只在内存里，不写日志、不进错误信息；重试日志里的路径经 redact_path 去掉多维表格 / 数据表 / 记录标识与查询串
（httpx 自己的请求日志由 cli.py 调到 WARNING）。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote

import httpx

from registry import settings

log = logging.getLogger(__name__)

_TOKEN_IN_PATH = re.compile(r"/(apps|tables)/[^/?]+|/(records)/rec[^/?]*")


def redact_path(path: str) -> str:
    """输入：请求路径 → 输出：日志里用的路径（多维表格、数据表、记录、知识库的标识换成 ***，去掉查询串）。"""
    return _TOKEN_IN_PATH.sub(lambda m: f"/{m.group(1) or m.group(2)}/***", path.split("?", 1)[0])

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"

#: 令牌无效 / 过期：刷新令牌后重发一次
TOKEN_INVALID_CODES = frozenset({99991661, 99991663, 99991664, 99991665, 99991671, 99991677})
#: 限流与暂时性错误：退避后重试
TRANSIENT_CODES = frozenset({
    99991400,   # 应用请求频率超限
    99991401,   # 租户请求频率超限
    1254290,    # 多维表格：请求过快（TooManyRequest）
    1254291,    # 多维表格：同一数据表写冲突（Write conflict）
    1254607,    # 多维表格：数据未就绪，请稍后重试
    1255001,    # 多维表格：内部错误
    1255002,    # 多维表格：RPC 错误
    1255040,    # 多维表格：请求超时
})

SEARCH_PAGE_MAX = 500
BATCH_UPDATE_MAX = 500
_TOKEN_EARLY_S = 300
_BACKOFF_BASE_S = 0.5
_BACKOFF_MAX_S = 8.0
_RETRY_AFTER_MAX_S = 30.0


class FeishuError(Exception):
    """输入：飞书错误码（整数；网络 / 非 JSON 时为字符串如 'NETWORK'、'HTTP_502'）、说明、HTTP 状态 → 异常。"""

    def __init__(self, code: int | str, msg: str, http_status: int | None = None):
        super().__init__(f"飞书接口错误 {code}：{msg}")
        self.code = code
        self.msg = msg
        self.http_status = http_status


class FeishuClient:
    """飞书开放平台客户端（应用身份 tenant_access_token）。线程不安全：每个后台任务各建一个。"""

    def __init__(self, app_id: str, app_secret: str, base_url: str = "https://open.feishu.cn", *,
                 transport: httpx.BaseTransport | None = None, timeout: float = 20, max_retries: int = 3,
                 min_interval_s: float = 0.2, sleep: Callable[[float], None] | None = None):
        """输入：应用凭证、开放平台地址、可选 httpx transport（测试用）、超时、重试次数、请求最小间隔、
        可选 sleep 函数（测试用）→ 输出：客户端。凭证缺失抛 FeishuError('NOT_CONFIGURED')。"""
        if not app_id or not app_secret:
            raise FeishuError("NOT_CONFIGURED", "未配置飞书应用")
        self._app_id = app_id
        self._app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, int(max_retries))
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._sleep = sleep or time.sleep
        self._clock: Callable[[], float] = time.monotonic   # 令牌缓存与限速用；测试可替换
        self._http = httpx.Client(base_url=self.base_url, transport=transport, timeout=timeout)
        self._token: str | None = None
        self._token_expires_at = 0.0          # self._clock() 基准
        self._last_request_at: float | None = None

    @classmethod
    def from_settings(cls, **kwargs: Any) -> "FeishuClient":
        """输入：其余构造参数 → 输出：用 registry.settings 里的 App ID / Secret / 地址建好的客户端。"""
        s = settings.get()
        return cls(s.feishu_app_id or "", s.feishu_app_secret or "", s.feishu_base_url, **kwargs)

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def close(self) -> None:
        """输入：无 → 输出：无（关闭底层连接池）。"""
        self._http.close()

    def __enter__(self) -> "FeishuClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── 认证 ─────────────────────────────────────────────────────────────────

    def tenant_token(self) -> str:
        """输入：无 → 输出：tenant_access_token（缓存到过期前 300 秒）。"""
        if self._token and self._clock() < self._token_expires_at:
            return self._token
        body = self._call("POST", TOKEN_PATH, json_body={"app_id": self._app_id, "app_secret": self._app_secret},
                          auth=False)
        token = body.get("tenant_access_token")
        if not token:
            raise FeishuError(body.get("code", "NO_TOKEN"), "飞书没有返回 tenant_access_token")
        expire = int(body.get("expire") or 0)
        self._token = token
        self._token_expires_at = self._clock() + max(0, expire - _TOKEN_EARLY_S)
        return token

    def invalidate_token(self) -> None:
        """输入：无 → 输出：无（丢弃缓存的令牌，下次请求重新获取）。"""
        self._token = None
        self._token_expires_at = 0.0

    # ── 多维表格 ──────────────────────────────────────────────────────────────

    def list_tables(self, app_token: str) -> list[dict[str, Any]]:
        """输入：多维表格 app_token → 输出：[{table_id, name}]（自动翻页）。"""
        path = f"/open-apis/bitable/v1/apps/{_seg(app_token)}/tables"
        return [{"table_id": it.get("table_id"), "name": it.get("name")}
                for it in self._paged_get(path, page_size=100)]

    def list_fields(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        """输入：app_token、table_id → 输出：[{field_id, field_name, type, ui_type, is_primary, property}]。"""
        path = f"/open-apis/bitable/v1/apps/{_seg(app_token)}/tables/{_seg(table_id)}/fields"
        return [{
            "field_id": it.get("field_id"),
            "field_name": it.get("field_name"),
            "type": it.get("type"),
            "ui_type": it.get("ui_type"),
            "is_primary": bool(it.get("is_primary")),
            "property": it.get("property"),
        } for it in self._paged_get(path, page_size=100)]

    def search_records(self, app_token: str, table_id: str, *, field_names: list[str] | None = None,
                       view_id: str | None = None, page_size: int = SEARCH_PAGE_MAX) -> Iterator[dict[str, Any]]:
        """输入：app_token、table_id、只读的列名、视图、每页条数 → 输出：逐条产出 {record_id, fields}（自动翻页）。"""
        path = f"/open-apis/bitable/v1/apps/{_seg(app_token)}/tables/{_seg(table_id)}/records/search"
        body: dict[str, Any] = {"automatic_fields": False}
        if view_id:
            body["view_id"] = view_id
        if field_names:
            body["field_names"] = list(field_names)
        size = max(1, min(int(page_size), SEARCH_PAGE_MAX))
        page_token: str | None = None
        seen: set[str] = set()
        while True:
            params = {"page_size": size}
            if page_token:
                params["page_token"] = page_token
            data = self._data(self._call("POST", path, params=params, json_body=body))
            for it in data.get("items") or []:
                yield {"record_id": it.get("record_id"), "fields": it.get("fields") or {}}
            page_token = _next_token(data, seen)
            if page_token is None:
                return

    def get_record(self, app_token: str, table_id: str, record_id: str) -> dict[str, Any]:
        """输入：app_token、table_id、record_id → 输出：{record_id, fields}。"""
        path = (f"/open-apis/bitable/v1/apps/{_seg(app_token)}/tables/{_seg(table_id)}"
                f"/records/{_seg(record_id)}")
        rec = self._data(self._call("GET", path)).get("record") or {}
        return {"record_id": rec.get("record_id") or record_id, "fields": rec.get("fields") or {}}

    def batch_update_records(self, app_token: str, table_id: str,
                             records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """输入：app_token、table_id、[{record_id, fields}] → 输出：飞书返回的已更新记录列表（每 500 条一批）。"""
        path = f"/open-apis/bitable/v1/apps/{_seg(app_token)}/tables/{_seg(table_id)}/records/batch_update"
        out: list[dict[str, Any]] = []
        for start in range(0, len(records), BATCH_UPDATE_MAX):
            chunk = [{"record_id": r["record_id"], "fields": r.get("fields") or {}}
                     for r in records[start:start + BATCH_UPDATE_MAX]]
            data = self._data(self._call("POST", path, json_body={"records": chunk}))
            out.extend(data.get("records") or [])
        return out

    # ── 知识库 ────────────────────────────────────────────────────────────────

    def wiki_get_node(self, token: str) -> dict[str, Any]:
        """输入：知识库节点 token → 输出：{obj_token, obj_type, title}（obj_type 为 'bitable' 时 obj_token 即 app_token）。"""
        data = self._data(self._call("GET", "/open-apis/wiki/v2/spaces/get_node", params={"token": token}))
        node = data.get("node") or {}
        return {"obj_token": node.get("obj_token"), "obj_type": node.get("obj_type"), "title": node.get("title")}

    # ── 内部：请求、重试、限速、分页 ──────────────────────────────────────────────

    def _paged_get(self, path: str, *, page_size: int) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self._data(self._call("GET", path, params=params))
            yield from data.get("items") or []
            page_token = _next_token(data, seen)
            if page_token is None:
                return

    @staticmethod
    def _data(body: dict[str, Any]) -> dict[str, Any]:
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def _throttle(self) -> None:
        if self._last_request_at is not None and self.min_interval_s > 0:
            wait = self.min_interval_s - (self._clock() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        delay = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * (2 ** attempt))
        if retry_after is not None:
            delay = max(delay, min(retry_after, _RETRY_AFTER_MAX_S))
        self._sleep(delay)

    def _call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
              json_body: dict[str, Any] | None = None, auth: bool = True) -> dict[str, Any]:
        """输入：方法、路径、查询参数、JSON 体、是否带令牌 → 输出：code == 0 的完整响应体。"""
        attempt = 0
        refreshed = False
        while True:
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if auth:
                headers["Authorization"] = f"Bearer {self.tenant_token()}"
            self._throttle()
            try:
                resp = self._http.request(method, path, params=params, json=json_body, headers=headers)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    log.warning("飞书请求网络错误，%s 后重试：%s %s（%s）", attempt + 1, method, redact_path(path),
                                type(exc).__name__)
                    self._backoff(attempt, None)
                    attempt += 1
                    continue
                raise FeishuError("NETWORK", f"连接飞书失败：{type(exc).__name__}") from exc

            status = resp.status_code
            body = _json_or_none(resp)
            code = body.get("code") if body is not None else None
            msg = str(body.get("msg") or "") if body is not None else ""

            if auth and code in TOKEN_INVALID_CODES and not refreshed:
                log.info("飞书令牌失效（%s），刷新后重发", code)
                self.invalidate_token()
                refreshed = True
                continue

            if status == 429 or status >= 500 or code in TRANSIENT_CODES:
                if attempt < self.max_retries:
                    log.warning("飞书请求暂时失败（HTTP %s，code %s），第 %s 次重试：%s %s",
                                status, code, attempt + 1, method, redact_path(path))
                    self._backoff(attempt, _retry_after(resp))
                    attempt += 1
                    continue
                raise FeishuError(code if code is not None else f"HTTP_{status}",
                                  msg or "飞书接口繁忙，重试后仍失败", status)

            if body is None:
                raise FeishuError(f"HTTP_{status}", "飞书返回了无法解析的内容", status)
            if code != 0:
                raise FeishuError(code if code is not None else f"HTTP_{status}", msg or "未知错误", status)
            return body


def _seg(s: str) -> str:
    """路径片段转义（token / id 只含字母数字，转义只为防御）。"""
    return quote(str(s), safe="")


def _json_or_none(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _retry_after(resp: httpx.Response) -> float | None:
    for name in ("x-ogw-ratelimit-reset", "retry-after"):
        raw = resp.headers.get(name)
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                continue
    return None


def _next_token(data: dict[str, Any], seen: set[str]) -> str | None:
    """输入：一页的 data、已用过的 page_token → 输出：下一页的 token；没有下一页返回 None。

    has_more 为真却没有新 token（或 token 重复）时抛 FeishuError，避免死循环。
    """
    if not data.get("has_more"):
        return None
    token = data.get("page_token")
    if not token or token in seen:
        raise FeishuError("BAD_PAGINATION", "飞书分页返回 has_more 但没有新的 page_token")
    seen.add(token)
    return token
