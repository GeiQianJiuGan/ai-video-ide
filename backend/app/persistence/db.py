"""SQLite 接入层。

每个工程一个 project.db。写操作用单一 asyncio.Lock 串行化（SQLite 单写者），
读走独立 session，避免 "database is locked"。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.logging import get_logger

log = get_logger("db")


class Base(DeclarativeBase):
    """所有 ORM 模型的基类；M1 起在 app/persistence/models.py 中定义具体表。"""


def _apply_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


class Database:
    """单个工程的数据库句柄。"""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        url = "sqlite+aiosqlite:///" + db_path.as_posix()
        self.engine: AsyncEngine = create_async_engine(url, echo=False, future=True)
        _apply_pragmas(self.engine)
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)
        self._write_lock = asyncio.Lock()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncSession]:
        """写事务：串行化 + 自动提交/回滚。"""
        async with self._write_lock, self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def ping(self) -> bool:
        async with self.read() as s:
            return (await s.execute(text("SELECT 1"))).scalar_one() == 1

    async def close(self) -> None:
        await self.engine.dispose()
        log.debug("db.closed", path=str(self.path))
