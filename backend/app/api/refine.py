"""优化层接口：对已经出好的那一段画面做二次处理（超分 / 插帧 / 重做）。

`plan` 与 `run` 分成两个端点（与 `adopt` / `sequence` 同一个习惯）：一次批量超分可能起十几
个任务、跑掉几十分钟显存，所以先给账单再动手。产出是**同一个镜头上的新版本**，所以这里
没有任何「采用」端点——那件事全工程只有 `POST /versions/{id}/current` 一个入口。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.persistence.models_gen import REFINE_KINDS
from app.services.refine import LABELS, refine

router = APIRouter(tags=["refine"])


class RefineBody(BaseModel):
    version_ids: list[str] | None = Field(
        default=None, description="就处理这几版（版本轨里勾的那些，最精确）"
    )
    shot_ids: list[str] | None = Field(
        default=None, description="这几个镜头**采用的那一版**；没采用过的会被跳过并说明"
    )
    scene_id: str | None = Field(default=None, description="整幕批量处理")
    kind: str = Field(default="upscale", description=" / ".join(REFINE_KINDS))
    preset: str | None = Field(
        default=None, description="临时换一份图；不传就是设置里的二次处理预设 → 默认预设"
    )
    prompt: str | None = Field(default=None, description="有些处理图也收 prompt，可留空")
    priority: int = 100
    extra: dict[str, Any] | None = None


@router.get("/refine/kinds")
async def kinds() -> list[dict[str, Any]]:
    """有哪几种二次处理。种类是后端的事实（`models_gen.REFINE_KINDS`），前端不要硬编码。"""
    return [{"kind": k, "label": LABELS.get(k, k)} for k in REFINE_KINDS]


@router.post("/projects/{pid}/refine/plan")
async def plan(pid: str, body: RefineBody) -> dict[str, Any]:
    """只出账单：处理哪几段、用哪份图、哪几个跳过为什么。**一个任务都不入队。**"""
    return await refine.plan(
        pid,
        version_ids=body.version_ids,
        shot_ids=body.shot_ids,
        scene_id=body.scene_id,
        kind=body.kind,
        preset=body.preset,
    )


@router.post("/projects/{pid}/refine/run", status_code=201)
async def run(pid: str, body: RefineBody) -> dict[str, Any]:
    """按账单入队。原来那一版一条都不动（只增不改），不满意就再采用回去。"""
    return await refine.run(
        pid,
        version_ids=body.version_ids,
        shot_ids=body.shot_ids,
        scene_id=body.scene_id,
        kind=body.kind,
        preset=body.preset,
        priority=body.priority,
        prompt=body.prompt,
        extra=body.extra,
    )


@router.get("/projects/{pid}/versions/{version_id}/lineage")
async def lineage(pid: str, version_id: str) -> dict[str, Any]:
    """这一版的谱系（原始 v1 → 超分 v2 → …）。版本轨靠它画出一条线而不是几条孤立版本。"""
    return await refine.lineage(pid, version_id)
