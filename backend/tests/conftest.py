"""pytest 公共 fixture。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.persistence.db import Database


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "project.db")
    await database.create_all()
    return database
