"""剧本 / Scene / Shot / 分镜板接口（Step 5）。

AI 拆解只返回**提案**（/breakdown/propose），提案不落库；
人确认后再 /breakdown/apply。手动新建 Scene / Shot 走同一套结构，不依赖 LLM。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.story import story

router = APIRouter(tags=["story"])


class StoryBody(BaseModel):
    title: str | None = None
    raw_text: str | None = None
    mode: str | None = Field(default=None, description="manual / ai_assisted")


class SceneBody(BaseModel):
    title: str | None = None
    summary: str | None = None
    source_text: str | None = None
    location_variant_id: str | None = None
    time_of_day: str | None = None
    notes: str | None = None


class ShotBody(BaseModel):
    title: str | None = None
    description: str | None = None
    duration: float | None = None
    camera: str | None = None
    movement: str | None = None
    status: str | None = None
    prompt: str | None = None
    negative_prompt: str | None = None
    seed: int | None = None
    steps: int | None = None
    workflow_id: str | None = None
    prev_shot_id: str | None = Field(default=None, description="上游镜头；用于首尾帧连续性")


class OrderBody(BaseModel):
    order: list[str]


class MoveBody(BaseModel):
    scene_id: str
    position: int | None = Field(default=None, description="目标 Scene 内 0-based 落点；None=末尾")


class CastBody(BaseModel):
    appearance_ids: list[str]


class PropsBody(BaseModel):
    items: list[dict[str, Any]] = Field(
        description='[{"prop_id": "prp_…", "state": "present|discarded"}]'
    )


class ProposeBody(BaseModel):
    text: str | None = Field(default=None, description="留空则用已保存的剧本原文")


class ApplyBody(BaseModel):
    scenes: list[dict[str, Any]] = Field(description="提案对象里的 scenes，可带 op=reject 剔除")


@router.get("/projects/{pid}/story")
async def get_story(pid: str) -> dict[str, Any]:
    return await story.get_story(pid)


@router.patch("/projects/{pid}/story")
async def save_story(pid: str, body: StoryBody) -> dict[str, Any]:
    return await story.save_story(pid, body.model_dump(exclude_none=True))


@router.get("/projects/{pid}/scenes")
async def list_scenes(pid: str) -> list[dict[str, Any]]:
    return await story.list_scenes(pid)


@router.post("/projects/{pid}/scenes", status_code=201)
async def create_scene(pid: str, body: SceneBody) -> dict[str, Any]:
    return await story.create_scene(pid, body.model_dump(exclude_none=True))


@router.patch("/projects/{pid}/scenes/{sid}")
async def update_scene(pid: str, sid: str, body: SceneBody) -> dict[str, Any]:
    return await story.update_scene(pid, sid, body.model_dump(exclude_none=True))


@router.delete("/projects/{pid}/scenes/{sid}", status_code=204)
async def delete_scene(pid: str, sid: str) -> None:
    await story.delete_scene(pid, sid)


@router.put("/projects/{pid}/scenes/order")
async def reorder_scenes(pid: str, body: OrderBody) -> list[dict[str, Any]]:
    return await story.reorder_scenes(pid, body.order)


@router.post("/projects/{pid}/scenes/{sid}/shots", status_code=201)
async def create_shot(pid: str, sid: str, body: ShotBody) -> dict[str, Any]:
    return await story.create_shot(pid, sid, body.model_dump(exclude_none=True))


@router.put("/projects/{pid}/scenes/{sid}/shots/order")
async def reorder_shots(pid: str, sid: str, body: OrderBody) -> list[dict[str, Any]]:
    await story.reorder_shots(pid, sid, body.order)
    return await story.storyboard(pid)


@router.get("/projects/{pid}/shots/{shot_id}")
async def get_shot(pid: str, shot_id: str) -> dict[str, Any]:
    return await story.get_shot(pid, shot_id)


@router.patch("/projects/{pid}/shots/{shot_id}")
async def update_shot(pid: str, shot_id: str, body: ShotBody) -> dict[str, Any]:
    return await story.update_shot(pid, shot_id, body.model_dump(exclude_none=True))


@router.delete("/projects/{pid}/shots/{shot_id}", status_code=204)
async def delete_shot(pid: str, shot_id: str) -> None:
    await story.delete_shot(pid, shot_id)


@router.post("/projects/{pid}/shots/{shot_id}/move")
async def move_shot(pid: str, shot_id: str, body: MoveBody) -> list[dict[str, Any]]:
    return await story.move_shot(pid, shot_id, body.scene_id, body.position)


@router.put("/projects/{pid}/shots/{shot_id}/cast")
async def set_shot_cast(pid: str, shot_id: str, body: CastBody) -> dict[str, Any]:
    return await story.set_shot_cast(pid, shot_id, body.appearance_ids)


@router.put("/projects/{pid}/shots/{shot_id}/props")
async def set_shot_props(pid: str, shot_id: str, body: PropsBody) -> dict[str, Any]:
    return await story.set_shot_props(pid, shot_id, body.items)


@router.get("/projects/{pid}/storyboard")
async def storyboard(pid: str) -> list[dict[str, Any]]:
    return await story.storyboard(pid)


@router.post("/projects/{pid}/breakdown/propose")
async def propose_breakdown(pid: str, body: ProposeBody) -> dict[str, Any]:
    """只返回提案，不写库。LLM 未配置时返回 LLM_UNAVAILABLE，并提示可手动添加。"""
    return await story.propose_breakdown(pid, body.text)


@router.post("/projects/{pid}/breakdown/apply")
async def apply_breakdown(pid: str, body: ApplyBody) -> dict[str, Any]:
    return await story.apply_breakdown(pid, body.scenes)
