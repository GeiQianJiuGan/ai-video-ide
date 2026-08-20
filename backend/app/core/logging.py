"""structlog 配置：开发期彩色控制台，生产期 JSON 落盘。"""

from __future__ import annotations

import logging
import sys

import structlog

from app.core.config import settings

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return

    log_dir = settings.runtime_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )
    file_handler = logging.FileHandler(log_dir / "backend.log", encoding="utf-8")
    file_handler.setLevel(level)
    logging.getLogger().addHandler(file_handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    setup_logging()
    return structlog.get_logger(name)
