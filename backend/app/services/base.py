"""服务层公共件。

各业务服务都需要同样的三件事：拿到当前工程的库、按 id 取行（取不到就结构化报错）、
把 ORM 行转成 JSON 可序列化的字典。集中在这里，避免每个模块各写一遍。
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from sqlalchemy import inspect, select
from sqlalchemy.sql import ColumnElement

from app.core.errors import AppError, ErrorCode
from app.persistence.db import Base, Database
from app.services.projects import OpenProject, projects

T = TypeVar("T", bound=Base)


def project_of(pid: str) -> OpenProject:
    return projects.get(pid)


def db_of(pid: str) -> Database:
    return projects.get(pid).db


def as_dict(row: Base) -> dict[str, Any]:
    """ORM 行 → 字典。列名即字段名，前后端共用同一套口径。"""
    mapper = inspect(type(row)).mapper
    return {col.key: getattr(row, col.key) for col in mapper.column_attrs}


def load_json(raw: str | None, fallback: Any) -> Any:
    """容忍坏 JSON：数据是用户的，读不动就退回默认值并保持可用。"""
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


async def fetch(db: Database, model: type[T], ident: str, what: str) -> T:
    """按 id 取一行；不存在时报 404 并说清「怎么回到能用的状态」。"""
    async with db.read() as session:
        row = await session.get(model, ident)
    if row is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"{what}不存在",
            f"未找到 id 为 {ident} 的{what}（可能已被删除）。",
            ["刷新列表后重试", "确认没有在别处删除它"],
            {"id": ident},
        )
    return row


async def fetch_all(
    db: Database,
    model: type[T],
    *,
    where: ColumnElement[bool] | None = None,
    order_by: Any = None,
) -> list[T]:
    stmt = select(model)
    if where is not None:
        stmt = stmt.where(where)
    if order_by is not None:
        stmt = stmt.order_by(order_by)
    async with db.read() as session:
        return list((await session.execute(stmt)).scalars().all())


def assign(row: Base, patch: dict[str, Any], allowed: tuple[str, ...]) -> list[str]:
    """把补丁写进行，返回真正变化的字段名。未列入 allowed 的字段一律忽略。"""
    changed: list[str] = []
    for key, value in patch.items():
        if key not in allowed or value is None:
            continue
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed.append(key)
    return changed
