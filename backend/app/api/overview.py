"""项目概览与连续性检查接口（Step 9）。

概览页要回答三件事：现在到哪了、下一步做什么、哪里不对。
连续性检查只报事实与坐标，不自动改数据。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.services.overview import overview

router = APIRouter(tags=["overview"])


@router.get("/projects/{pid}/overview")
async def summary(pid: str) -> dict[str, Any]:
    return await overview.summary(pid)


@router.get("/projects/{pid}/overview/activity")
async def activity(pid: str, limit: int = 20) -> list[dict[str, Any]]:
    return await overview.activity(pid, limit)


@router.get("/projects/{pid}/overview/continuity")
async def continuity(pid: str) -> dict[str, Any]:
    return await overview.continuity(pid)


@router.get("/projects/{pid}/overview/environment")
async def environment(pid: str) -> dict[str, Any]:
    return await overview.environment(pid)


@router.get("/projects/{pid}/overview/workflows")
async def workflow_health(pid: str) -> list[dict[str, Any]]:
    return await overview.workflow_health(pid)


@router.get("/environment")
async def global_environment() -> dict[str, Any]:
    """不带工程的环境探测，起始页状态条用它。"""
    return await overview.environment(None)
