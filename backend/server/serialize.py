"""HTTP 层的 JSON 转换与文件响应（契约 §10「响应形状」）。

- 金额（Decimal）一律输出两位小数的字符串（如 "52.00"），可空；日期 `YYYY-MM-DD`；时刻 ISO 8601 带时区。
- FastAPI 自带的 jsonable_encoder 会把 Decimal 转成浮点数，所以路由**不直接返回 dict**，一律经 respond()
  先转成纯 JSON 值再交给 UTF-8 的 JSONResponse。
- 下载文件（xlsx）带 `Content-Disposition: attachment`，文件名按 RFC 5987 同时给 ASCII 兜底名与 UTF-8 原名。
- 只做形状转换，不做业务判断。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi.responses import JSONResponse, Response

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CENT = Decimal("0.01")
_ASCII_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def money(v: Any) -> str | None:
    """输入：金额（Decimal / int / str / None）→ 输出：两位小数的字符串；None 原样为 None（NULL 不是 0）。"""
    if v is None:
        return None
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    return format(d.quantize(_CENT), "f")


def _decimal(v: Decimal) -> str:
    """金额列是 numeric(12,2)：不超过两位小数的按两位输出；更精细的（非金额）原样保留全部精度。"""
    if not v.is_finite():
        return str(v)
    if v == v.quantize(_CENT):
        return format(v.quantize(_CENT), "f")
    return format(v, "f")


def jsonable(v: Any) -> Any:
    """输入：服务层返回的任意值（dict / list / Decimal / date / datetime …）→ 输出：只含 JSON 原生类型的等价值。"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Decimal):
        return _decimal(v)
    if isinstance(v, datetime):
        return (v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [jsonable(x) for x in v]
    if isinstance(v, Enum):
        return jsonable(v.value)
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None   # 二进制（如上传原件）从不经 JSON 输出
    if hasattr(v, "to_json") and callable(v.to_json):
        return jsonable(v.to_json())
    return str(v)


class UTF8JSONResponse(JSONResponse):
    """JSON 响应：中文不转义、不允许 NaN；内容须已是纯 JSON 值（见 respond）。"""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        """输入：纯 JSON 值 → 输出：UTF-8 编码的紧凑 JSON 字节。"""
        return json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def respond(data: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> UTF8JSONResponse:
    """输入：服务层结果、HTTP 状态、额外响应头 → 输出：UTF-8 JSON 响应（金额为两位小数字符串）。"""
    return UTF8JSONResponse(jsonable(data), status_code=status_code, headers=headers)


def error_body(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    """输入：错误码、中文说明、细节 → 输出：统一错误体 {code, message, detail}（detail 总是对象）。"""
    d = jsonable(detail) if detail is not None else {}
    if not isinstance(d, dict):
        d = {"value": d}
    return {"code": code, "message": message, "detail": d}


def error_response(code: str, message: str, status_code: int, detail: Any = None,
                   headers: dict[str, str] | None = None) -> UTF8JSONResponse:
    """输入：错误码、中文说明、HTTP 状态、细节 → 输出：统一错误 JSON 响应。"""
    return UTF8JSONResponse(error_body(code, message, detail), status_code=status_code, headers=headers)


def page(items: list[Any], page_no: int, page_size: int) -> dict[str, Any]:
    """输入：完整列表、页码（≥1）、每页条数 → 输出：{total, rows}（服务层只给列表时由路由切页）。"""
    start = (page_no - 1) * page_size
    return {"total": len(items), "rows": items[start:start + page_size]}


def content_disposition(filename: str) -> str:
    """输入：文件名（可含中文）→ 输出：RFC 6266 / 5987 的 Content-Disposition 值（attachment，ASCII 兜底名 + UTF-8 原名）。"""
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    ascii_stem = _ASCII_SAFE.sub("_", stem).strip("_-.")
    if not re.search(r"[A-Za-z]", ascii_stem):      # 中文名只剩符号和数字时，兜底名用 download
        ascii_stem = "download"
    ascii_ext = _ASCII_SAFE.sub("", ext)
    fallback = f"{ascii_stem}.{ascii_ext}" if ascii_ext else ascii_stem
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def xlsx_response(data: bytes, filename: str) -> Response:
    """输入：xlsx 字节、下载文件名 → 输出：附件下载响应（不缓存）。"""
    return Response(content=data, media_type=XLSX_MEDIA_TYPE, headers={
        "Content-Disposition": content_disposition(filename),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })
