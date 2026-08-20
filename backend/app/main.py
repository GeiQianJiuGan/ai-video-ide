"""FastAPI 装配与生命周期。

安全约束：只监听 127.0.0.1；开启 require_handshake 后，所有 /api 请求必须携带
X-AIVS-Token（WebSocket 用 ?token=）。Tauri 启动 sidecar 时注入该 token。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import projects as projects_api
from app.api import system, ws
from app.core.config import settings
from app.core.errors import (
    AppError,
    ErrorCode,
    app_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.core.logging import get_logger, setup_logging
from app.services.projects import projects

log = get_logger("main")

API_PREFIX = "/api/v1"
PUBLIC_PATHS = {"/api/v1/health", "/docs", "/openapi.json", "/redoc"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    log.info(
        "backend.starting",
        version=settings.version,
        schema_version=settings.schema_version,
        runtime_dir=str(settings.runtime_dir),
        handshake=settings.require_handshake,
    )
    yield
    # 关闭所有工程库：SQLite WAL 需要正常 dispose 才会把 -wal 合并回主文件
    await projects.close_all()
    log.info("backend.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description="AI Video Studio — 视频工程与编排器",
        lifespan=lifespan,
    )

    if settings.dev_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "tauri://localhost"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def handshake_guard(request: Request, call_next):  # noqa: ANN001, ANN202
        if (
            settings.require_handshake
            and request.url.path.startswith("/api")
            and request.url.path not in PUBLIC_PATHS
            and request.headers.get("X-AIVS-Token") != settings.handshake_token
        ):
            err = AppError(
                ErrorCode.UNAUTHORIZED,
                "握手校验失败",
                "缺少或错误的 X-AIVS-Token。",
                ["由 Tauri 壳启动应用，而不要直接访问后端端口"],
            )
            return JSONResponse(status_code=401, content={"error": err.to_dict()})
        return await call_next(request)

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    app.include_router(system.router, prefix=API_PREFIX)
    app.include_router(projects_api.router, prefix=API_PREFIX)
    app.include_router(ws.router, prefix=API_PREFIX)
    return app


app = create_app()


def _write_endpoint(port: int) -> None:
    """把实际端口与 token 写给 Tauri / 前端读取。"""
    target = settings.runtime_dir / "endpoint.json"
    target.write_text(
        json.dumps(
            {
                "host": settings.host,
                "port": port,
                "base_url": f"http://{settings.host}:{port}{API_PREFIX}",
                "ws_url": f"ws://{settings.host}:{port}{API_PREFIX}/ws",
                "token": settings.handshake_token,
                "version": settings.version,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info("backend.endpoint_written", path=str(target), port=port)


def main() -> None:
    import socket

    import uvicorn

    port = settings.port
    if port == 0:
        with socket.socket() as sock:
            sock.bind((settings.host, 0))
            port = sock.getsockname()[1]
    _write_endpoint(port)
    uvicorn.run(app, host=settings.host, port=port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
