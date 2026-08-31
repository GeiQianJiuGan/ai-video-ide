"""图片素材生成接口（第三条生成链的对外入口）。

三个端点，界线就是「账单」与「入队」的界线（照 `api/sequence.py` 的作风）：

  - `GET  /images/skills`   内置的三份出图 SKILL（角色四视图 / 场景 / 道具）——
    界面上那个下拉的文案只有这一份，前端不抄第二遍；
  - `POST /images/plan`     账单：用哪个协议、照哪份 SKILL、拼出来的正 / 负向 prompt 全文、
    带几张参考图、图会落到哪里、缺什么。**一行库都不改**；
  - `POST /images/generate` 才真的入队（同一张 job 表、同一个 pump）。

极薄：Pydantic body + 转调 `services/images.py`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.images import images

router = APIRouter(tags=["images"])


class ImageBody(BaseModel):
    target_kind: str = Field(
        description="appearance | location_variant | prop | shot_first_frame | shot_last_frame"
    )
    target_id: str = Field(description="那一行的 id（形象 / 地点变体 / 道具 / 镜头）")
    #: 用户那段话**只填「长什么样」**：四视图、纯背景、无文字那些结构由 SKILL 固定补齐。
    prompt: str = Field(default="", description="这个角色 / 地点 / 道具长什么样")
    skill: str | None = Field(
        default=None, description="留空按 target_kind 自动选（角色→四视图，地点→简单场景图）"
    )
    #: 图生图 / 风格参考。**只认显式传进来的那几张**，服务端不替用户猜。
    ref_asset_ids: list[str] = Field(default_factory=list)


class GenerateBody(ImageBody):
    priority: int = 100


@router.get("/projects/{pid}/images/skills")
async def skills(pid: str) -> dict[str, Any]:
    return images.skills()


@router.post("/projects/{pid}/images/plan")
async def plan(pid: str, body: ImageBody) -> dict[str, Any]:
    """先账单再动手。缺服务、缺预设在这里就说出来，不必先点一次「生成」才知道做不了。"""
    return await images.plan(
        pid,
        body.target_kind,
        body.target_id,
        body.prompt,
        body.skill,
        body.ref_asset_ids,
    )


@router.post("/projects/{pid}/images/generate", status_code=201)
async def generate(pid: str, body: GenerateBody) -> dict[str, Any]:
    return await images.enqueue(
        pid,
        body.target_kind,
        body.target_id,
        prompt=body.prompt,
        skill=body.skill,
        ref_asset_ids=body.ref_asset_ids,
        priority=body.priority,
    )
