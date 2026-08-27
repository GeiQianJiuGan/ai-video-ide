"""Workflow 能力层接口（Step 4）。

能力矩阵是这一步的核心视图：四种能力各自「就绪 / 缺什么 / 缺了会影响什么」，
以及 ComfyUI 在不在线。校验分本地绑定检查与节点探测两段，离线也能用前一半。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.workflows import workflows

router = APIRouter(tags=["workflows"])


class ImportBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    capability: str = Field(description="text2image / image2video / first_last_frame / upscale")
    api_json: str = Field(description="ComfyUI 导出的 workflow_api.json 原文")
    bindings: dict[str, Any] | None = Field(
        default=None, description='槽位 → "节点id.字段名"，例如 {"prompt": "6.text"}'
    )
    notes: str | None = None


class BindBody(BaseModel):
    bindings: dict[str, Any]


class ProjectBindingsBody(BaseModel):
    generation_mode: Literal["comfy_preset", "http_api", "workflow_api"] = Field(
        default="comfy_preset", description="comfy_preset / http_api / workflow_api"
    )
    text2image: str | None = None
    image2video: str | None = None
    first_last_frame: str | None = None
    upscale: str | None = None


class UpdateBody(BaseModel):
    name: str | None = None
    notes: str | None = None
    capability: str | None = None
    status: str | None = Field(default=None, description="只允许改为 disabled / draft")


@router.get("/workflows")
async def list_global_workflows() -> list[dict[str, Any]]:
    return await workflows.list_global()


@router.get("/capabilities")
async def global_capability_matrix() -> dict[str, Any]:
    return await workflows.global_capability_matrix()


@router.get("/workflows/{wid}")
async def get_global_workflow(wid: str) -> dict[str, Any]:
    return await workflows.get_global(wid)


@router.post("/workflows", status_code=201)
async def import_global_workflow(body: ImportBody) -> dict[str, Any]:
    return await workflows.import_global(
        name=body.name,
        capability=body.capability,
        api_json=body.api_json,
        bindings=body.bindings,
        notes=body.notes,
    )


@router.put("/workflows/{wid}/bindings")
async def bind_global_workflow(wid: str, body: BindBody) -> dict[str, Any]:
    return await workflows.bind_global(wid, body.bindings)


@router.post("/workflows/{wid}/validate")
async def validate_global_workflow(wid: str, probe: bool = True) -> dict[str, Any]:
    return await workflows.validate_global(wid, probe=probe)


@router.post("/workflows/{wid}/default")
async def set_global_default(wid: str) -> dict[str, Any]:
    return await workflows.set_default_global(wid)


@router.patch("/workflows/{wid}")
async def update_global_workflow(wid: str, body: UpdateBody) -> dict[str, Any]:
    return await workflows.update("", wid, body.model_dump(exclude_none=True))


@router.delete("/workflows/{wid}", status_code=204)
async def delete_global_workflow(wid: str) -> None:
    await workflows.delete_global(wid)


@router.get("/projects/{pid}/workflow-bindings")
async def get_project_workflow_bindings(pid: str) -> dict[str, str | None]:
    return await workflows.project_bindings(pid)


@router.put("/projects/{pid}/workflow-bindings")
async def set_project_workflow_bindings(
    pid: str, body: ProjectBindingsBody
) -> dict[str, str | None]:
    return await workflows.set_project_bindings(pid, body.model_dump())


@router.get("/projects/{pid}/workflows")
async def list_workflows(pid: str) -> list[dict[str, Any]]:
    return await workflows.list_workflows(pid)


@router.get("/projects/{pid}/capabilities")
async def capability_matrix(pid: str) -> dict[str, Any]:
    return await workflows.project_capabilities(pid)


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
