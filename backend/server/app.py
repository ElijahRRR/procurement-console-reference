"""FastAPI 应用（契约 §8 控制台接口、§5 插件接口）。uvicorn 入口：`server.app:app`。

- 控制台接口前缀 /api/v1，插件接口前缀 /plugin/v1，健康检查 GET /healthz → {ok: true}。
- 错误统一为 JSON {code, message, detail}：
  DomainError → 它自己的 http_status；请求参数校验失败 → 422 VALIDATION「请求参数不正确」；
  框架层 404 / 405 → NOT_FOUND / METHOD_NOT_ALLOWED；其他异常 → 500 INTERNAL「服务器内部错误」（记日志，不把堆栈给客户端）。
- 防跨站：/api/ 下的写请求必须带 `X-Requested-With: pc-console`，否则 403 FORBIDDEN（/plugin 用设备令牌，不检查）。
- OPTIONS 一律 204（不做 CORS：插件从扩展 service worker 调用，凭 host 权限不需要预检）。
- 上传：Content-Length 超过 10 MB（加 multipart 开销余量）的请求在读请求体之前就以 413 拒绝。
- 前端：settings.web_dist（缺省 web/dist）存在时托管在 / 下，非 /api、/plugin 的 GET 找不到文件就回 index.html
  （单页应用回退）；不存在时 / 返回一段 JSON 提示。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from registry import settings
from server import deps
from server.routes import (accounts, allocation, auth, devices, events, exports, feishu, imports, orders, overview,
                           plugin)
from server.routes import settings as settings_routes
from server.routes import simulation, writebacks
from server.serialize import error_response, respond
from services.errors import DomainError

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
PLUGIN_PREFIX = "/plugin/v1"
#: multipart 包装的余量：Content-Length 超过「10 MB + 余量」直接 413
UPLOAD_OVERHEAD_BYTES = 256 * 1024
_RESERVED_PREFIXES = ("/api", "/plugin")
_HTTP_CODES = {400: "VALIDATION", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
               405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 413: "VALIDATION", 422: "VALIDATION"}
_HTTP_MESSAGES = {400: "请求格式不正确", 401: "请先登录", 403: "没有权限执行此操作", 404: "接口不存在",
                  405: "接口不支持这个请求方法", 413: "请求内容过大", 422: "请求参数不正确"}

CONSOLE_ROUTERS = (auth, overview, orders, allocation, accounts, devices, settings_routes, simulation, feishu,
                   writebacks, imports, exports, events)


# ── 错误处理 ──────────────────────────────────────────────────────────────────


def _validation_errors(exc: RequestValidationError) -> dict[str, Any]:
    """输入：请求校验异常 → 输出：{errors: [{loc, msg, type}], field?}（去掉不可序列化的上下文）。"""
    errors = []
    for e in exc.errors():
        loc = [x if isinstance(x, (str, int)) else str(x) for x in e.get("loc", ())]
        errors.append({"loc": loc, "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))})
    detail: dict[str, Any] = {"errors": errors}
    first = next((e["loc"] for e in errors if e["loc"]), None)
    if first and isinstance(first[-1], str) and first[-1] not in ("body", "query", "path"):
        detail["field"] = first[-1]
    return detail


async def domain_error_handler(request: Request, exc: DomainError) -> Response:
    """输入：请求、DomainError → 输出：{code, message, detail}，状态为 exc.http_status。"""
    if exc.http_status >= 500:
        log.warning("%s %s → %s %s：%s", request.method, request.url.path, exc.http_status, exc.code, exc.message)
    return error_response(exc.code, exc.message, exc.http_status, exc.detail)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
    """输入：请求、参数校验异常 → 输出：422 {code: 'VALIDATION', message: '请求参数不正确', detail: {errors, field?}}。"""
    return error_response("VALIDATION", "请求参数不正确", 422, _validation_errors(exc))


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """输入：请求、框架层 HTTP 异常（路由不存在、方法不对、请求体解析失败）→ 输出：统一错误体。"""
    code = _HTTP_CODES.get(exc.status_code, "INTERNAL" if exc.status_code >= 500 else "VALIDATION")
    message = _HTTP_MESSAGES.get(exc.status_code) or (exc.detail if isinstance(exc.detail, str) else "请求失败")
    return error_response(code, message, exc.status_code, headers=getattr(exc, "headers", None))


def internal_error_response() -> Response:
    """输入：无 → 输出：500 {code: 'INTERNAL', message: '服务器内部错误'}（不带堆栈）。"""
    return error_response("INTERNAL", "服务器内部错误", 500)


async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
    """输入：请求、未预期的异常 → 输出：500 INTERNAL（记录堆栈到日志）。"""
    log.exception("未处理的异常：%s %s", request.method, request.url.path)
    return internal_error_response()


# ── 中间件：OPTIONS、防跨站、上传大小、兜底 500 ───────────────────────────────


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


async def guard_middleware(request: Request, call_next):
    """输入：请求、下一层 → 输出：响应。OPTIONS → 204；/api 写请求缺防跨站头 → 403；上传过大 → 413；未处理异常 → 500。"""
    method = request.method.upper()
    path = request.url.path
    if method == "OPTIONS":
        return Response(status_code=204, headers={"Allow": "GET, HEAD, POST, PATCH, DELETE, OPTIONS"})
    if path.startswith("/api/") and not deps.csrf_ok(request):
        return error_response("FORBIDDEN", "缺少防跨站请求头 X-Requested-With: pc-console", 403)
    if method == "POST" and path.rstrip("/") == f"{API_PREFIX}/imports":
        size = _content_length(request)
        if size is not None and size > imports.MAX_UPLOAD_BYTES + UPLOAD_OVERHEAD_BYTES:
            exc = imports.too_large()
            return error_response(exc.code, exc.message, exc.http_status, exc.detail)
    try:
        return await call_next(request)
    except Exception:  # noqa: BLE001 - 兜底：任何漏网的异常都变成 500 JSON，不把堆栈给客户端
        log.exception("未处理的异常：%s %s", method, path)
        return internal_error_response()


# ── 前端托管 ──────────────────────────────────────────────────────────────────


def _is_reserved(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _RESERVED_PREFIXES)


def _mount_frontend(app: FastAPI, dist: Path | None) -> None:
    """输入：应用、前端构建目录（None 或不存在 = 未构建）→ 输出：无（注册 / 与单页回退路由，必须最后注册）。"""
    root = dist.resolve() if dist is not None and (dist / "index.html").is_file() else None

    if root is None:
        @app.get("/", include_in_schema=False)
        def frontend_missing():
            """输入：无 → 输出：{ok, message}（前端尚未构建时的提示）。"""
            return respond({"ok": True, "message": "采购控制台服务已启动；控制台前端尚未构建，请在 web/ 下运行 npm run build",
                            "api": API_PREFIX, "health": "/healthz"})
        return

    index = root / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str):
        """输入：任意非 /api、/plugin 的路径 → 输出：dist 里的同名文件，找不到时回 index.html（单页应用回退）。"""
        path = "/" + full_path
        if _is_reserved(path):
            return error_response("NOT_FOUND", "接口不存在", 404)
        if full_path:
            candidate = (root / full_path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                cache = "public, max-age=31536000, immutable" if "/assets/" in path else "no-cache"
                return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


# ── 应用 ──────────────────────────────────────────────────────────────────────


def create_app(web_dist: Path | str | None = None) -> FastAPI:
    """输入：可选的前端构建目录（缺省 settings.web_dist）→ 输出：配置好路由、错误处理、中间件与前端托管的 FastAPI 应用。"""
    app = FastAPI(title="采购控制台", version="0.1.0", docs_url=None, redoc_url=None,
                  openapi_url=f"{API_PREFIX}/openapi.json")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.middleware("http")(guard_middleware)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        """输入：无 → 输出：{ok: true}（不碰数据库）。"""
        return respond({"ok": True})

    for module in CONSOLE_ROUTERS:
        app.include_router(module.router, prefix=API_PREFIX)
    app.include_router(plugin.router, prefix=PLUGIN_PREFIX)

    dist = Path(web_dist) if web_dist is not None else settings.get().web_dist
    _mount_frontend(app, dist)
    return app


app = create_app()
