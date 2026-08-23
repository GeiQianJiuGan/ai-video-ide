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


#: 「参考图装不下，确认丢弃并继续」——两个入队端点共用这一句描述，口径只有一份。
#: 写成函数而不是共享一个 `Field(...)` 实例：同一个 FieldInfo 挂到两个模型上是自找麻烦。
def _allow_ref_drop() -> Any:
    return Field(
        default=False,
        description=(
            "账单里采用的参考图比模型端那份图能收的多时，默认不入队，先回 REF_OVER_CAPACITY "
            "说明会丢几张；带上 true 就是「确认丢弃并继续」，按槽位顺序喂前几张，"
            "丢了哪几张记进版本参数 ref_notes。"
        ),
    )


class EnqueueShotBody(BaseModel):
    kind: str | None = Field(
        default=None,
        description="能力名；留空生成普通 R2V Shot，首尾帧 / 转场请显式传 first_last_frame",
    )
    priority: int = 100
    workflow_id: str | None = None
    check_context: bool = Field(default=True, description="false 表示跳过上下文完整性门槛")
    allow_ref_drop: bool = _allow_ref_drop()


class EnqueueSceneBody(BaseModel):
    priority: int = 100
    allow_ref_drop: bool = _allow_ref_drop()


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
        allow_ref_drop=body.allow_ref_drop,
    )


@router.post("/projects/{pid}/scenes/{scene_id}/generate", status_code=201)
async def enqueue_scene(pid: str, scene_id: str, body: EnqueueSceneBody) -> dict[str, Any]:
    """整场生成。逐个镜头入队，被跳过的镜头连结构化原因一起返回。"""
    return await generation.enqueue_scene(
        pid, scene_id, body.priority, allow_ref_drop=body.allow_ref_drop
    )


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
