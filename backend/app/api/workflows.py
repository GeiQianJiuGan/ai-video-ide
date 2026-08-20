"""Workflow 能力层接口（Step 4）。

能力矩阵是这一步的核心视图：四种能力各自「就绪 / 缺什么 / 缺了会影响什么」，
以及 ComfyUI 在不在线。校验分本地绑定检查与节点探测两段，离线也能用前一半。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.workflows import workflows

router = APIRouter(tags=["workflows"])


class ImportBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    capability: str = Field(description="text2image / image2video / first_last_frame / upscale")
    api_json: str = Field(description="ComfyUI 导出的 workflow_api.json 原文")
    bindings: dict[str, str] | None = Field(
        default=None, description='槽位 → "节点id.字段名"，例如 {"prompt": "6.text"}'
    )
    notes: str | None = None


class BindBody(BaseModel):
    bindings: dict[str, str]


class UpdateBody(BaseModel):
    name: str | None = None
    notes: str | None = None
    status: str | None = Field(default=None, description="只允许改为 disabled / draft")


@router.get("/projects/{pid}/workflows")
async def list_workflows(pid: str) -> list[dict[str, Any]]:
    return await workflows.list_workflows(pid)


@router.get("/projects/{pid}/capabilities")
async def capability_matrix(pid: str) -> dict[str, Any]:
    return await workflows.capability_matrix(pid)


@router.post("/projects/{pid}/workflows", status_code=201)
async def import_workflow(pid: str, body: ImportBody) -> dict[str, Any]:
    return await workflows.import_workflow(
        pid,
        name=body.name,
        capability=body.capability,
        api_json=body.api_json,
        bindings=body.bindings,
        notes=body.notes,
    )


@router.get("/projects/{pid}/workflows/{wid}")
async def get_workflow(pid: str, wid: str) -> dict[str, Any]:
    return await workflows.get(pid, wid)


@router.patch("/projects/{pid}/workflows/{wid}")
async def update_workflow(pid: str, wid: str, body: UpdateBody) -> dict[str, Any]:
    return await workflows.update(pid, wid, body.model_dump(exclude_none=True))


@router.put("/projects/{pid}/workflows/{wid}/bindings")
async def bind_workflow(pid: str, wid: str, body: BindBody) -> dict[str, Any]:
    return await workflows.bind(pid, wid, body.bindings)


@router.post("/projects/{pid}/workflows/{wid}/validate")
async def validate_workflow(pid: str, wid: str, probe: bool = True) -> dict[str, Any]:
    return await workflows.validate(pid, wid, probe=probe)


@router.post("/projects/{pid}/workflows/{wid}/default")
async def set_default(pid: str, wid: str) -> dict[str, Any]:
    return await workflows.set_default(pid, wid)


@router.delete("/projects/{pid}/workflows/{wid}", status_code=204)
async def delete_workflow(pid: str, wid: str) -> None:
    await workflows.delete(pid, wid)
