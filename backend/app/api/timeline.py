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
    #: 默认 false = 对位调和（只更新自己铺过的片段，手工编辑活下来）。
    #: true 是兼容入口「彻底重铺」：先删掉所有 `origin="assembled"` 的片段再铺一遍，
    #: `manual` 的（用户自己加的、拆出去的声音）两种模式下都不碰。
    replace: bool = Field(
        default=False, description="true 先删掉装配铺的片段再重铺；false 对位调和"
    )


class MoveBody(BaseModel):
    start: float


class TrimBody(BaseModel):
    in_point: float | None = None
    out_point: float | None = None
    #: 拖左边缘时和 in_point 一起给：一次请求改完「从源素材哪里开始」与
    #: 「在时间线上哪里开始」，撤销栈上也只占一格（见 timeline.trim_clip）。
    start: float | None = None
    ripple: bool = Field(default=True, description="裁切后是否自动贴紧后续片段")


class SplitBody(BaseModel):
    at: float = Field(description="时间线上的绝对秒数")


class AudioSelectionBody(BaseModel):
    in_point: float
    out_point: float


class ReplaceVersionBody(BaseModel):
    version_id: str


class TrackBody(BaseModel):
    kind: str = Field(default="audio", description="video / audio / subtitle")
    name: str | None = Field(default=None, description="留空则自动编号（A1 占了就叫 A2）")


class TrackPatchBody(BaseModel):
    name: str | None = None
    muted: bool | None = None
    locked: bool | None = None


class AddClipBody(BaseModel):
    asset_id: str
    start: float = 0.0
    duration: float | None = Field(default=None, description="留空则用资产时长或 ffprobe 探测")
    label: str | None = None


class BlankClipBody(BaseModel):
    duration: float = Field(default=1.0, gt=0, description="黑场时长，单位秒")
    label: str | None = None


class BlankDurationBody(BaseModel):
    duration: float = Field(gt=0, description="黑场时长，单位秒")


class MoveTrackBody(BaseModel):
    track_id: str


class MixBody(BaseModel):
    muted: bool | None = None
    volume: float | None = Field(default=None, description="0 ~ 4，1 是原样")


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


@router.post("/projects/{pid}/timeline/assemble/plan")
async def assemble_plan(pid: str) -> dict[str, Any]:
    """只读账单：这次装配会动哪些片段。「重新装配」按下去之前先给人看一眼。"""
    return await timeline.assemble_plan(pid)


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
        pid,
        clip_id,
        in_point=body.in_point,
        out_point=body.out_point,
        start=body.start,
        ripple=body.ripple,
    )


@router.post("/projects/{pid}/clips/{clip_id}/split")
async def split_clip(pid: str, clip_id: str, body: SplitBody) -> dict[str, Any]:
    return await timeline.split_clip(pid, clip_id, body.at)


@router.post("/projects/{pid}/clips/{clip_id}/isolate-audio-selection")
async def isolate_audio_selection(
    pid: str, clip_id: str, body: AudioSelectionBody
) -> dict[str, Any]:
    return await timeline.isolate_audio_selection(
        pid, clip_id, in_point=body.in_point, out_point=body.out_point
    )


@router.post("/projects/{pid}/clips/{clip_id}/clear")
async def clear_clip(pid: str, clip_id: str) -> dict[str, Any]:
    """清空这一段的内容（位置与长度留着，变成黑场 / 静音占位）。"""
    return await timeline.clear_clip(pid, clip_id)


@router.delete("/projects/{pid}/clips/{clip_id}")
async def delete_clip(pid: str, clip_id: str, ripple: bool = True) -> dict[str, Any]:
    return await timeline.delete_clip(pid, clip_id, ripple=ripple)


@router.post("/projects/{pid}/clips/{clip_id}/version")
async def replace_version(pid: str, clip_id: str, body: ReplaceVersionBody) -> dict[str, Any]:
    """只替换这一个片段的版本，整条时间线不重排。"""
    return await timeline.replace_version(pid, clip_id, body.version_id)


@router.post("/projects/{pid}/clips/{clip_id}/mix")
async def set_mix(pid: str, clip_id: str, body: MixBody) -> dict[str, Any]:
    """静音 / 音量。视频片段与音频片段用同一个入口。"""
    return await timeline.set_mix(pid, clip_id, muted=body.muted, volume=body.volume)


@router.post("/projects/{pid}/clips/{clip_id}/detach-audio", status_code=201)
async def detach_audio(pid: str, clip_id: str) -> dict[str, Any]:
    """把这段画面的声音拆成音频轨上的独立片段（源片段随之静音）。"""
    return await timeline.detach_audio(pid, clip_id)


@router.post("/projects/{pid}/clips/{clip_id}/blank-duration")
async def resize_blank_clip(pid: str, clip_id: str, body: BlankDurationBody) -> dict[str, Any]:
    return await timeline.resize_blank_clip(pid, clip_id, duration=body.duration)


@router.post("/projects/{pid}/clips/{clip_id}/audio-track")
async def move_clip_to_audio_track(pid: str, clip_id: str, body: MoveTrackBody) -> dict[str, Any]:
    return await timeline.move_clip_to_track(pid, clip_id, body.track_id)


@router.post("/projects/{pid}/clips/{clip_id}/new-audio-track", status_code=201)
async def move_clip_to_new_audio_track(pid: str, clip_id: str) -> dict[str, Any]:
    return await timeline.move_clip_to_new_audio_track(pid, clip_id)


@router.post("/projects/{pid}/tracks", status_code=201)
async def add_track(pid: str, body: TrackBody) -> dict[str, Any]:
    return await timeline.add_track(pid, kind=body.kind, name=body.name)


@router.patch("/projects/{pid}/tracks/{track_id}")
async def update_track(pid: str, track_id: str, body: TrackPatchBody) -> dict[str, Any]:
    return await timeline.update_track(
        pid, track_id, name=body.name, muted=body.muted, locked=body.locked
    )


@router.delete("/projects/{pid}/tracks/{track_id}")
async def delete_track(pid: str, track_id: str, force: bool = False) -> dict[str, Any]:
    """删轨道。上面还有片段时先回 CONFLICT + `confirm: "force"`，确认后带 `?force=true` 重放。"""
    return await timeline.delete_track(pid, track_id, force=force)


@router.post("/projects/{pid}/tracks/{track_id}/clips", status_code=201)
async def add_clip(pid: str, track_id: str, body: AddClipBody) -> dict[str, Any]:
    """把一个已登记的资产放到轨道上（导入的配乐 / 音效走这里）。"""
    return await timeline.add_clip(
        pid,
        track_id,
        body.asset_id,
        start=body.start,
        duration=body.duration,
        label=body.label,
    )


@router.post("/projects/{pid}/tracks/{track_id}/blank-clips", status_code=201)
async def add_blank_clip(pid: str, track_id: str, body: BlankClipBody) -> dict[str, Any]:
    return await timeline.add_blank_clip(pid, track_id, duration=body.duration, label=body.label)


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


@router.post("/projects/{pid}/exports/open-folder")
async def open_export_folder(pid: str) -> dict[str, str]:
    return await timeline.open_export_folder(pid)


@router.get("/projects/{pid}/export/command")
async def export_command(pid: str) -> dict[str, Any]:
    """导出前的预检：把将要执行的 FFmpeg 命令原样给人看。

    `warnings` 里是「会被丢掉 / 说不准」的那些事（静音的音频轨、比画面长的声音、
    ffprobe 不在所以不知道某段画面有没有声音）——不拦导出，但必须显示出来。
    """
    plan = await timeline.build_command(pid)
    return {
        "path": plan["path"],
        "command": plan["command"],
        "clips": len(plan["clips"]),
        "audio_clips": len(plan["audio_clips"]),
        "warnings": plan["warnings"],
    }


@router.post("/projects/{pid}/export", status_code=201)
async def export(pid: str, body: ExportBody) -> dict[str, Any]:
    return await timeline.export(pid, body.path)
