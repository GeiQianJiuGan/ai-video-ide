"""生成队列与版本接口（Step 7）。

入队前会做上下文完整性检查（check_context=false 可以显式跳过，用于「我确认无误」）。
版本只增不改：这里没有任何 PUT/PATCH 能改写一个已存在的 GenerationVersion。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.generation import generation

router = APIRouter(tags=["generation"])


class EnqueueShotBody(BaseModel):
    kind: str | None = Field(default=None, description="能力名；留空按是否有上游自动判断")
    priority: int = 100
    workflow_id: str | None = None
    check_context: bool = Field(default=True, description="false 表示跳过上下文完整性门槛")


class EnqueueSceneBody(BaseModel):
    priority: int = 100


class PriorityBody(BaseModel):
    priority: int


class ManualVersionBody(BaseModel):
    asset_id: str = Field(description="手动导入的成片资产")
    kind: str = "video"
    duration: float | None = None
    params: dict[str, Any] | None = None


@router.get("/projects/{pid}/queue")
async def queue_state(pid: str) -> dict[str, Any]:
    return await generation.queue_state(pid)


@router.get("/projects/{pid}/jobs")
async def list_jobs(pid: str, status: str | None = None) -> list[dict[str, Any]]:
    return await generation.list_jobs(pid, status)


@router.post("/projects/{pid}/shots/{shot_id}/generate", status_code=201)
async def enqueue_shot(pid: str, shot_id: str, body: EnqueueShotBody) -> dict[str, Any]:
    return await generation.enqueue_shot(
        pid,
        shot_id,
        kind=body.kind,
        priority=body.priority,
        workflow_id=body.workflow_id,
        check_context=body.check_context,
    )


@router.post("/projects/{pid}/scenes/{scene_id}/generate", status_code=201)
async def enqueue_scene(pid: str, scene_id: str, body: EnqueueSceneBody) -> dict[str, Any]:
    """整场生成。逐个镜头入队，被跳过的镜头连结构化原因一起返回。"""
    return await generation.enqueue_scene(pid, scene_id, body.priority)


@router.post("/projects/{pid}/queue/pause")
async def pause_queue(pid: str) -> dict[str, Any]:
    return await generation.pause(pid)


@router.post("/projects/{pid}/queue/resume")
async def resume_queue(pid: str) -> dict[str, Any]:
    return await generation.resume(pid)


@router.post("/projects/{pid}/queue/retry-failed")
async def retry_failed(pid: str) -> dict[str, Any]:
    return await generation.retry_failed(pid)


@router.post("/projects/{pid}/jobs/{job_id}/cancel")
async def cancel_job(pid: str, job_id: str) -> dict[str, Any]:
    return await generation.cancel(pid, job_id)


@router.post("/projects/{pid}/jobs/{job_id}/retry")
async def retry_job(pid: str, job_id: str) -> dict[str, Any]:
    return await generation.retry(pid, job_id)


@router.put("/projects/{pid}/jobs/{job_id}/priority")
async def set_priority(pid: str, job_id: str, body: PriorityBody) -> dict[str, Any]:
    return await generation.set_priority(pid, job_id, body.priority)


@router.get("/projects/{pid}/shots/{shot_id}/versions")
async def list_versions(pid: str, shot_id: str) -> list[dict[str, Any]]:
    return await generation.list_versions(pid, shot_id)


@router.post("/projects/{pid}/shots/{shot_id}/versions", status_code=201)
async def add_manual_version(pid: str, shot_id: str, body: ManualVersionBody) -> dict[str, Any]:
    """手动导入成片也走版本系统——不生成也能把工程做完。"""
    return await generation.add_version(
        pid,
        shot_id,
        asset_id=body.asset_id,
        kind=body.kind,
        params=body.params,
        source="manual",
        duration=body.duration,
    )


@router.post("/projects/{pid}/versions/{version_id}/current")
async def set_current_version(pid: str, version_id: str) -> dict[str, Any]:
    return await generation.set_current_version(pid, version_id)
