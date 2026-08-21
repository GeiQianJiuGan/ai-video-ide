"""场景衔接与编排接口（幕流程图的后端）。

`plan` 与 `run` 分成两个端点是刻意的：编排一次可能起十几个任务、造出几段转场镜头，
所以先给账单、再动手（和 `adopt/plan` 同一个习惯）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.sequence import sequence

router = APIRouter(tags=["sequence"])


class LinkBody(BaseModel):
    from_scene_id: str
    to_scene_id: str
    mode: str = Field(description="cut / transition / tail_frame")
    duration: float | None = Field(default=None, description="转场时长（秒），只有 transition 用")
    prompt: str | None = None


class RunBody(BaseModel):
    mode: str = Field(default="parallel", description="parallel 各幕并发 / sequential 单线程续接")
    priority: int = 100


@router.get("/projects/{pid}/flow")
async def flow_graph(pid: str) -> dict[str, Any]:
    """流程图：场景节点 + 衔接边。第一级页面的唯一数据源。"""
    return await sequence.graph(pid)


@router.get("/projects/{pid}/links")
async def list_links(pid: str) -> list[dict[str, Any]]:
    return await sequence.list_links(pid)


@router.put("/projects/{pid}/links")
async def set_link(pid: str, body: LinkBody) -> dict[str, Any]:
    """新建或改一条衔接。同一对场景之间只有一条，所以是 PUT 而不是 POST。"""
    return await sequence.set_link(
        pid,
        body.from_scene_id,
        body.to_scene_id,
        mode=body.mode,
        duration=body.duration,
        prompt=body.prompt,
    )


@router.delete("/projects/{pid}/links/{link_id}", status_code=204)
async def delete_link(pid: str, link_id: str) -> None:
    await sequence.delete_link(pid, link_id)


@router.post("/projects/{pid}/sequence/plan")
async def plan(pid: str, body: RunBody) -> dict[str, Any]:
    """只出账单，不入队任何任务。"""
    return await sequence.plan(pid, body.mode)


@router.post("/projects/{pid}/sequence/run", status_code=201)
async def run(pid: str, body: RunBody) -> dict[str, Any]:
    """按账单入队。返回里带上那份账单，界面上「说好的」与「做了的」能对上。"""
    return await sequence.run(pid, body.mode, body.priority)
