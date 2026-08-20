"""地点、变体与道具接口（Step 3）。

删除类操作在服务层会先检查引用并拒绝，路由不做二次判断——
「能不能删」这条口径必须只有一处。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.world import world

router = APIRouter(tags=["world"])


class LocationBody(BaseModel):
    name: str | None = None
    description: str | None = None
    notes: str | None = None


class VariantBody(BaseModel):
    name: str | None = None
    time_of_day: str | None = None
    weather: str | None = None
    lighting: str | None = None
    description: str | None = None


class ReferenceBody(BaseModel):
    asset_id: str
    camera: str | None = Field(default=None, description="机位说明，例如「正面中景」")
    note: str | None = None


class PropBody(BaseModel):
    name: str | None = None
    description: str | None = None
    notes: str | None = None


class PropReferenceBody(BaseModel):
    asset_id: str
    note: str | None = None


@router.get("/projects/{pid}/locations")
async def list_locations(pid: str) -> list[dict[str, Any]]:
    return await world.list_locations(pid)


@router.post("/projects/{pid}/locations", status_code=201)
async def create_location(pid: str, body: LocationBody) -> dict[str, Any]:
    return await world.create_location(pid, body.model_dump(exclude_none=True))


@router.patch("/projects/{pid}/locations/{lid}")
async def update_location(pid: str, lid: str, body: LocationBody) -> dict[str, Any]:
    return await world.update_location(pid, lid, body.model_dump(exclude_none=True))


@router.delete("/projects/{pid}/locations/{lid}", status_code=204)
async def delete_location(pid: str, lid: str) -> None:
    await world.delete_location(pid, lid)


@router.post("/projects/{pid}/locations/{lid}/variants", status_code=201)
async def create_variant(pid: str, lid: str, body: VariantBody) -> dict[str, Any]:
    return await world.create_variant(pid, lid, body.model_dump(exclude_none=True))


@router.patch("/projects/{pid}/variants/{vid}")
async def update_variant(pid: str, vid: str, body: VariantBody) -> dict[str, Any]:
    return await world.update_variant(pid, vid, body.model_dump(exclude_none=True))


@router.delete("/projects/{pid}/variants/{vid}", status_code=204)
async def delete_variant(pid: str, vid: str) -> None:
    await world.delete_variant(pid, vid)


@router.get("/projects/{pid}/variants/{vid}/usage")
async def variant_usage(pid: str, vid: str) -> list[dict[str, Any]]:
    """「被 N 个 Scene 引用」背后那张可点击的清单。"""
    return await world.variant_usage(pid, vid)


@router.get("/projects/{pid}/variants/{vid}/references")
async def variant_references(pid: str, vid: str) -> list[dict[str, Any]]:
    return await world.variant_references(pid, vid)


@router.post("/projects/{pid}/variants/{vid}/references", status_code=201)
async def add_variant_reference(pid: str, vid: str, body: ReferenceBody) -> dict[str, Any]:
    return await world.add_variant_reference(pid, vid, body.asset_id, body.camera, body.note)


@router.get("/projects/{pid}/props")
async def list_props(pid: str) -> list[dict[str, Any]]:
    return await world.list_props(pid)


@router.post("/projects/{pid}/props", status_code=201)
async def create_prop(pid: str, body: PropBody) -> dict[str, Any]:
    return await world.create_prop(pid, body.model_dump(exclude_none=True))


@router.patch("/projects/{pid}/props/{prop_id}")
async def update_prop(pid: str, prop_id: str, body: PropBody) -> dict[str, Any]:
    return await world.update_prop(pid, prop_id, body.model_dump(exclude_none=True))


@router.delete("/projects/{pid}/props/{prop_id}", status_code=204)
async def delete_prop(pid: str, prop_id: str) -> None:
    await world.delete_prop(pid, prop_id)


@router.get("/projects/{pid}/props/{prop_id}/references")
async def prop_references(pid: str, prop_id: str) -> list[dict[str, Any]]:
    return await world.prop_references(pid, prop_id)


@router.post("/projects/{pid}/props/{prop_id}/references", status_code=201)
async def add_prop_reference(pid: str, prop_id: str, body: PropReferenceBody) -> dict[str, Any]:
    return await world.add_prop_reference(pid, prop_id, body.asset_id, body.note)
