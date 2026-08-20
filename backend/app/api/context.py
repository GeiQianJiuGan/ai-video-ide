"""上下文检查器接口（Step 6）。

GET 返回完整账单（含被省略项与省略理由）；POST /override 做人工干预，
action=reset 就是「恢复自动」。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.context import context

router = APIRouter(tags=["context"])


class OverrideBody(BaseModel):
    action: str = Field(description="remove / add / reset")
    key: str | None = Field(default=None, description="remove / reset 时指定条目 key")
    asset_id: str | None = Field(default=None, description="add 时指定资产")
    label: str | None = None


@router.get("/projects/{pid}/shots/{shot_id}/context")
async def resolve_context(pid: str, shot_id: str) -> dict[str, Any]:
    return await context.resolve(pid, shot_id)


@router.post("/projects/{pid}/shots/{shot_id}/context/override")
async def override_context(pid: str, shot_id: str, body: OverrideBody) -> dict[str, Any]:
    payload = body.model_dump(exclude={"action"}, exclude_none=True)
    return await context.override(pid, shot_id, body.action, payload)


@router.get("/projects/{pid}/shots/{shot_id}/context/snapshot")
async def snapshot_context(pid: str, shot_id: str) -> dict[str, Any]:
    return await context.snapshot(pid, shot_id)
