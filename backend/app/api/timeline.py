"""时间线与导出接口（Step 8）。

这一整组不依赖 ComfyUI / LLM。导出用原始素材，代理只服务预览。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.timeline import timeline

router = APIRouter(tags=["timeline"])


class AssembleBody(BaseModel):
    replace: bool = Field(default=True, description="true 清空视频轨后重铺；false 追加")


class MoveBody(BaseModel):
    start: float


class TrimBody(BaseModel):
    in_point: float | None = None
    out_point: float | None = None
    ripple: bool = Field(default=True, description="裁切后是否自动贴紧后续片段")


class SplitBody(BaseModel):
    at: float = Field(description="时间线上的绝对秒数")


class ReplaceVersionBody(BaseModel):
    version_id: str


class TransitionBody(BaseModel):
    from_clip_id: str
    to_clip_id: str
    kind: str = "dissolve"
    duration: float = 0.5


class ExportBody(BaseModel):
    path: str | None = Field(default=None, description="留空则写入工程 generations/exports/")


@router.get("/projects/{pid}/timeline")
async def get_timeline(pid: str) -> dict[str, Any]:
    return await timeline.get(pid)


@router.post("/projects/{pid}/timeline/assemble")
async def auto_assemble(pid: str, body: AssembleBody) -> dict[str, Any]:
    return await timeline.auto_assemble(pid, replace=body.replace)


@router.post("/projects/{pid}/timeline/undo")
async def undo(pid: str) -> dict[str, Any]:
    return await timeline.undo(pid)


@router.post("/projects/{pid}/timeline/redo")
async def redo(pid: str) -> dict[str, Any]:
    return await timeline.redo(pid)


@router.post("/projects/{pid}/clips/{clip_id}/move")
async def move_clip(pid: str, clip_id: str, body: MoveBody) -> dict[str, Any]:
    return await timeline.move_clip(pid, clip_id, body.start)


@router.post("/projects/{pid}/clips/{clip_id}/trim")
async def trim_clip(pid: str, clip_id: str, body: TrimBody) -> dict[str, Any]:
    return await timeline.trim_clip(
        pid, clip_id, in_point=body.in_point, out_point=body.out_point, ripple=body.ripple
    )


@router.post("/projects/{pid}/clips/{clip_id}/split")
async def split_clip(pid: str, clip_id: str, body: SplitBody) -> dict[str, Any]:
    return await timeline.split_clip(pid, clip_id, body.at)


@router.delete("/projects/{pid}/clips/{clip_id}")
async def delete_clip(pid: str, clip_id: str, ripple: bool = True) -> dict[str, Any]:
    return await timeline.delete_clip(pid, clip_id, ripple=ripple)


@router.post("/projects/{pid}/clips/{clip_id}/version")
async def replace_version(pid: str, clip_id: str, body: ReplaceVersionBody) -> dict[str, Any]:
    """只替换这一个片段的版本，整条时间线不重排。"""
    return await timeline.replace_version(pid, clip_id, body.version_id)


@router.get("/projects/{pid}/transitions")
async def list_transitions(pid: str) -> list[dict[str, Any]]:
    return await timeline.list_transitions(pid)


@router.post("/projects/{pid}/transitions", status_code=201)
async def add_transition(pid: str, body: TransitionBody) -> dict[str, Any]:
    return await timeline.add_transition(
        pid, body.from_clip_id, body.to_clip_id, body.kind, body.duration
    )


@router.delete("/projects/{pid}/transitions/{tid}", status_code=204)
async def delete_transition(pid: str, tid: str) -> None:
    await timeline.delete_transition(pid, tid)


@router.post("/projects/{pid}/assets/{asset_id}/proxy")
async def ensure_proxy(pid: str, asset_id: str) -> dict[str, Any]:
    return await timeline.ensure_proxy(pid, asset_id)


@router.get("/projects/{pid}/exports")
async def list_exports(pid: str) -> list[dict[str, Any]]:
    return await timeline.list_exports(pid)


@router.get("/projects/{pid}/export/command")
async def export_command(pid: str) -> dict[str, Any]:
    """导出前的预检：把将要执行的 FFmpeg 命令原样给人看。"""
    plan = await timeline.build_command(pid)
    return {"path": plan["path"], "command": plan["command"], "clips": len(plan["clips"])}


@router.post("/projects/{pid}/export", status_code=201)
async def export(pid: str, body: ExportBody) -> dict[str, Any]:
    return await timeline.export(pid, body.path)
