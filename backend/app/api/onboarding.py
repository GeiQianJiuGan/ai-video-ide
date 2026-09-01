"""新手引导接口（Step 10）：状态 + 演示工程。

刻意极薄——Pydantic body + 转调 `services/onboarding.py`。

环境状态**不在这里重复实现**：向导直接用已有的 `GET /system/deps`、`GET /settings`、
`POST /settings/probe`、`GET /settings/presets`、`PUT /projects/{pid}/preset`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.onboarding import onboarding

router = APIRouter(tags=["onboarding"])


class StatePatch(BaseModel):
    #: 走到哪一步了。取值见 `GET /onboarding` 回的 `steps`。
    step: str | None = None
    completed: bool | None = None
    skipped: bool | None = None


class DemoBody(BaseModel):
    #: 演示工程落在哪；留空用 `default_demo_dir`（文档目录下那一份）。
    dir: str | None = Field(default=None, description="演示工程目录；留空用默认位置")


@router.get("/onboarding")
async def get_state() -> dict[str, Any]:
    return onboarding.state()


@router.patch("/onboarding")
async def patch_state(body: StatePatch) -> dict[str, Any]:
    return onboarding.patch(step=body.step, completed=body.completed, skipped=body.skipped)


@router.post("/onboarding/demo/plan")
async def plan_demo(body: DemoBody) -> dict[str, Any]:
    """账单：目录在哪、会建什么、多大。**一个字节都不写。**"""
    return onboarding.plan_demo(body.dir)


@router.post("/onboarding/demo", status_code=201)
async def create_demo(body: DemoBody) -> dict[str, Any]:
    """落地演示工程；已经有了就只打开（`created=false`），不重建、不覆盖。"""
    return await onboarding.create_demo(body.dir)
