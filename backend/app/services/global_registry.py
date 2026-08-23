"""Application-level SQLite registries shared by every project."""

from __future__ import annotations

import asyncio

from app.core.config import settings
from app.persistence.db import Database
from app.persistence.models_global import GlobalBase


class GlobalRegistry:
    def __init__(self) -> None:
        self.db: Database | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> Database:
        if self.db is not None:
            return self.db
        async with self._lock:
            if self.db is None:
                settings.runtime_dir.mkdir(parents=True, exist_ok=True)
                self.db = Database(settings.runtime_dir / "global.db")
                await self.db.create_all(GlobalBase.metadata)
        return self.db

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None


global_registry = GlobalRegistry()
