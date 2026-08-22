"""结构化错误契约。

硬约束：绝不静默失败。任何失败都必须携带 code / title / detail / suggestions，
让 UI 能直接把「为什么失败、怎么修」摆在用户面前。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ErrorCode(StrEnum):
    # 生成链路
    WORKFLOW_ERROR = "WORKFLOW_ERROR"
    MISSING_ASSET = "MISSING_ASSET"
    MISSING_INPUT = "MISSING_INPUT"
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    INVALID_WORKFLOW = "INVALID_WORKFLOW"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    UPSTREAM_NOT_READY = "UPSTREAM_NOT_READY"
    CONTEXT_INCOMPLETE = "CONTEXT_INCOMPLETE"
    #: 账单里的参考图比模型端那份图能收的多。**这不是错，是一次确认**：
    #: 用户点了确认就按槽位顺序喂前 N 张，所以它必须先挡下来而不是悄悄丢图。
    REF_OVER_CAPACITY = "REF_OVER_CAPACITY"
    # 外部依赖
    COMFY_OFFLINE = "COMFY_OFFLINE"
    COMFY_NODE_MISSING = "COMFY_NODE_MISSING"
    COMFY_LOST = "COMFY_LOST"
    GPU_OOM = "GPU_OOM"
    FFMPEG_ERROR = "FFMPEG_ERROR"
    FFMPEG_MISSING = "FFMPEG_MISSING"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_INVALID_OUTPUT = "LLM_INVALID_OUTPUT"
    # 系统
    DISK_FULL = "DISK_FULL"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    INTERNAL = "INTERNAL"


_STATUS = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    # schema 不匹配本质上也是冲突：文件在那里，但和当前应用对不上
    ErrorCode.SCHEMA_MISMATCH: 409,
    ErrorCode.VALIDATION_ERROR: 422,
    #: 要用户点一下「确认丢弃并继续」才能过——语义上和「状态冲突」是一回事：
    #: 现在这个状态下不能直接做，换个参数（allow_ref_drop）就能做。
    ErrorCode.REF_OVER_CAPACITY: 409,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.COMFY_OFFLINE: 503,
    ErrorCode.LLM_UNAVAILABLE: 503,
    ErrorCode.INTERNAL: 500,
}


class AppError(Exception):
    """所有业务错误的唯一基类。"""

    def __init__(
        self,
        code: ErrorCode,
        title: str,
        detail: str = "",
        suggestions: list[str] | None = None,
        related_ids: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(title)
        self.code = code
        self.title = title
        self.detail = detail
        self.suggestions = suggestions or []
        self.related_ids = related_ids or {}
        self.status_code = status_code or _STATUS.get(code, 400)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "title": self.title,
            "detail": self.detail,
            "suggestions": self.suggestions,
            "related_ids": self.related_ids,
        }


def not_found(what: str, ident: str) -> AppError:
    return AppError(
        ErrorCode.NOT_FOUND,
        f"{what}不存在",
        f"未找到 id 为 {ident} 的{what}。",
        ["确认 id 是否正确", "刷新列表后重试"],
        {"id": ident},
    )


async def app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return JSONResponse(status_code=exc.status_code, content={"error": exc.to_dict()})


async def validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """FastAPI 的请求校验错误也要变成同一种结构，前端只需要认一种错误形状。"""
    raw = getattr(exc, "errors", lambda: [])()
    lines = [f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}" for e in raw]
    err = AppError(
        ErrorCode.VALIDATION_ERROR,
        "请求参数不合法",
        "；".join(lines) or str(exc),
        ["按提示修正对应字段后重试"],
    )
    return JSONResponse(status_code=422, content={"error": err.to_dict()})


async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    err = AppError(
        ErrorCode.INTERNAL,
        "内部错误",
        f"{type(exc).__name__}: {exc}",
        ["查看后端日志 .runtime/logs", "如可复现请附带日志反馈"],
    )
    return JSONResponse(status_code=500, content={"error": err.to_dict()})
