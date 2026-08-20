"""角色与形象接口（Step 2）。

形象继承的「值 + 来源」在服务层已经算好，这里只负责把它原样送出去——
前端要能显示「继承自 母亲装 · 已覆写」，靠的就是 resolved 字段。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.cast import cast

router = APIRouter(tags=["cast"])


class CharacterBody(BaseModel):
    name: str | None = None
    alias: str | None = None
    gender: str | None = None
    age_range: str | None = None
    personality: str | None = None
    background: str | None = None
    voice_desc: str | None = None
    notes: str | None = None


class AppearanceBody(BaseModel):
    name: str | None = None
    face: str | None = None
    hair: str | None = None
    body: str | None = None
    traits: str | None = None
    age: str | None = None
    costume: str | None = None
    state: str | None = None
    notes: str | None = None
    parent_id: str | None = Field(default=None, description="从哪个形象派生；派生字段默认继承")
    default: bool = False


class SheetBody(BaseModel):
    asset_id: str | None = Field(default=None, description="角色表图资产 id；留空表示占位版本")
    source: str = "manual"


@router.get("/projects/{pid}/characters")
async def list_characters(pid: str) -> list[dict[str, Any]]:
    return await cast.list_characters(pid)


@router.post("/projects/{pid}/characters", status_code=201)
async def create_character(pid: str, body: CharacterBody) -> dict[str, Any]:
    return await cast.create_character(pid, body.model_dump(exclude_none=True))


@router.patch("/projects/{pid}/characters/{cid}")
async def update_character(pid: str, cid: str, body: CharacterBody) -> dict[str, Any]:
    return await cast.update_character(pid, cid, body.model_dump(exclude_none=True))


@router.delete("/projects/{pid}/characters/{cid}", status_code=204)
async def delete_character(pid: str, cid: str) -> None:
    await cast.delete_character(pid, cid)


@router.get("/projects/{pid}/characters/{cid}/appearances")
async def list_appearances(pid: str, cid: str) -> list[dict[str, Any]]:
    return await cast.list_appearances(pid, cid)


@router.post("/projects/{pid}/characters/{cid}/appearances", status_code=201)
async def create_appearance(pid: str, cid: str, body: AppearanceBody) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True, exclude={"parent_id", "default"})
    return await cast.create_appearance(
        pid, cid, patch, parent_id=body.parent_id, default=body.default
    )


@router.get("/projects/{pid}/appearances/{aid}")
async def get_appearance(pid: str, aid: str) -> dict[str, Any]:
    return await cast.get_appearance(pid, aid)


@router.patch("/projects/{pid}/appearances/{aid}")
async def update_appearance(pid: str, aid: str, body: AppearanceBody) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True, exclude={"parent_id", "default"})
    return await cast.update_appearance(pid, aid, patch)


@router.post("/projects/{pid}/appearances/{aid}/revert/{field}")
async def revert_field(pid: str, aid: str, field: str) -> dict[str, Any]:
    return await cast.revert_field(pid, aid, field)


@router.post("/projects/{pid}/appearances/{aid}/default")
async def set_default_appearance(pid: str, aid: str) -> dict[str, Any]:
    return await cast.set_default_appearance(pid, aid)


@router.delete("/projects/{pid}/appearances/{aid}", status_code=204)
async def delete_appearance(pid: str, aid: str) -> None:
    await cast.delete_appearance(pid, aid)


@router.get("/projects/{pid}/appearances/{aid}/sheets")
async def list_sheets(pid: str, aid: str) -> list[dict[str, Any]]:
    return await cast.list_sheets(pid, aid)


@router.post("/projects/{pid}/appearances/{aid}/sheets", status_code=201)
async def add_sheet(pid: str, aid: str, body: SheetBody) -> dict[str, Any]:
    return await cast.add_sheet(pid, aid, body.asset_id, body.source)
