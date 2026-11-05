"""领域错误：服务层抛出，路由层统一转成 JSON {code, message, detail}。"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """输入：错误码、人能读的中文说明、HTTP 状态、细节 → 一个可被路由层翻译的异常。"""

    def __init__(self, code: str, message: str, http_status: int = 400,
                 detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail or {}


def not_found(what: str = "记录") -> DomainError:
    return DomainError("NOT_FOUND", f"{what}不存在", 404)


def validation(message: str, **detail: Any) -> DomainError:
    return DomainError("VALIDATION", message, 422, detail)


def conflict(message: str, code: str = "CONFLICT", **detail: Any) -> DomainError:
    return DomainError(code, message, 409, detail)
