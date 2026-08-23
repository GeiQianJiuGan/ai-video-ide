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
    allow_ref_drop: bool = Field(
        default=False,
        description=(
            "账单里有镜头的参考图超出模型端能收的张数时，默认整次编排一个任务都不入队，"
            "先回 REF_OVER_CAPACITY 让用户确认；带上 true 就是「确认丢弃并继续」。"
        ),
    )


class ShotLinkBody(BaseModel):
    from_shot_id: str
    to_shot_id: str
    mode: str = Field(description="cut 无转场 / transition 补一段转场")
    duration: float | None = Field(default=None, description="转场时长（秒），只有 transition 用")
    prompt: str | None = None


class TransitionRunBody(BaseModel):
    priority: int = 100
    allow_ref_drop: bool = Field(
        default=False,
        description="转场镜头的参考图装不下时先回 REF_OVER_CAPACITY；true 就是「确认丢弃并继续」。",
    )
    only: list[str] | None = Field(
        default=None,
        description="只生成这几条衔接（ShotLink / SceneLink 的 id）。不传就是全部。",
    )


@router.get("/projects/{pid}/flow")
async def flow_graph(pid: str) -> dict[str, Any]:
    """流程图：场景节点 + 衔接边。第一级页面的唯一数据源。"""
    return await sequence.graph(pid)


@router.get("/projects/{pid}/scenes/{sid}/videos")
async def scene_videos(pid: str, sid: str) -> dict[str, Any]:
    """这一幕**按镜头分组**的视频候选。不能当候选的那些在 omitted 里带原因。

    这里只列候选。**采用是镜头级的**，走已有的那唯一一个入口
    `POST /projects/{pid}/versions/{version_id}/current`——它改的就是
    `Shot.current_version_id`，时间线装配认的也是它。刻意不在这一层再开一个
    「采用」端点：同一件事两个入口，迟早两边行为分叉。
    """
    return await sequence.scene_videos(pid, sid)


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


@router.get("/projects/{pid}/shot-links")
async def list_shot_links(pid: str) -> list[dict[str, Any]]:
    """镜头之间的衔接。分镜板上两张卡片之间那条线，没有行就是「无转场」。"""
    return await sequence.list_shot_links(pid)


@router.put("/projects/{pid}/shot-links")
async def set_shot_link(pid: str, body: ShotLinkBody) -> dict[str, Any]:
    """新建或改一条镜头衔接。同一对镜头之间只有一条，所以是 PUT 而不是 POST。"""
    return await sequence.set_shot_link(
        pid,
        body.from_shot_id,
        body.to_shot_id,
        mode=body.mode,
        duration=body.duration,
        prompt=body.prompt,
    )


@router.delete("/projects/{pid}/shot-links/{link_id}", status_code=204)
async def delete_shot_link(pid: str, link_id: str) -> None:
    await sequence.delete_shot_link(pid, link_id)


@router.post("/projects/{pid}/sequence/transitions/plan")
async def transition_plan(pid: str) -> dict[str, Any]:
    """一键生成转场的账单：配了转场却还没生成的，两级一起列出来，不入队任何任务。"""
    return await sequence.transition_plan(pid)


@router.post("/projects/{pid}/sequence/transitions/run", status_code=201)
async def transition_run(pid: str, body: TransitionRunBody) -> dict[str, Any]:
    """按账单补转场。已经出片的转场一条都不重做（版本永不覆盖）。"""
    return await sequence.transition_run(
        pid, body.priority, allow_ref_drop=body.allow_ref_drop, only=body.only
    )


@router.post("/projects/{pid}/sequence/plan")
async def plan(pid: str, body: RunBody) -> dict[str, Any]:
    """只出账单，不入队任何任务。`ref_drops` 里是「参考图装不下」的镜头。"""
    return await sequence.plan(pid, body.mode)


@router.post("/projects/{pid}/sequence/run", status_code=201)
async def run(pid: str, body: RunBody) -> dict[str, Any]:
    """按账单入队。返回里带上那份账单，界面上「说好的」与「做了的」能对上。"""
    return await sequence.run(pid, body.mode, body.priority, allow_ref_drop=body.allow_ref_drop)
