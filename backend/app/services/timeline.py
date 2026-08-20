"""时间线与导出（Step 8）。

这一层完全不依赖 AI：即使 ComfyUI 和 LLM 都不在，装配、剪辑、导出照样能跑。
撤销栈用整轨快照实现——片段数量是几十到几百的量级，快照比逐条反向操作更不容易错。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.events.bus import Channel, bus
from app.persistence.models import utc_now
from app.persistence.models_edit import (
    TRANSITION_KINDS,
    ExportRecord,
    Timeline,
    TimelineClip,
    Track,
    Transition,
)
from app.persistence.models_gen import GenerationVersion
from app.persistence.models_story import Scene, Shot
from app.persistence.models_world import Asset
from app.services.assets import assets as asset_service
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, load_json, project_of

log = get_logger("timeline")

#: 吸附阈值（秒）：剪短片段后自动贴到前一段末尾，中间不留黑帧。
SNAP = 0.08
UNDO_DEPTH = 50


class TimelineService:
    def __init__(self) -> None:
        self._undo: dict[str, list[list[dict[str, Any]]]] = {}
        self._redo: dict[str, list[list[dict[str, Any]]]] = {}

    # --- 时间线与轨道 ---

    async def get(self, pid: str) -> dict[str, Any]:
        db = db_of(pid)
        rows = await fetch_all(db, Timeline, order_by=Timeline.created_at)
        if not rows:
            proj = project_of(pid)
            now = utc_now()
            timeline = Timeline(
                id=new_id("timeline"),
                name="主时间线",
                fps=proj.fps,
                width=proj.width,
                height=proj.height,
                created_at=now,
                updated_at=now,
            )
            async with db.write() as session:
                session.add(timeline)
                session.add(
                    Track(
                        id=new_id("track"),
                        timeline_id=timeline.id,
                        kind="video",
                        index_no=0,
                        name="V1",
                    )
                )
                session.add(
                    Track(
                        id=new_id("track"),
                        timeline_id=timeline.id,
                        kind="audio",
                        index_no=1,
                        name="A1",
                    )
                )
        else:
            timeline = rows[0]
        return await self._shape(pid, timeline)

    async def _shape(self, pid: str, timeline: Timeline) -> dict[str, Any]:
        db = db_of(pid)
        tracks = await fetch_all(
            db, Track, where=Track.timeline_id == timeline.id, order_by=Track.index_no
        )
        clips = await fetch_all(db, TimelineClip, order_by=TimelineClip.index_no)
        assets_by_id = {a.id: a for a in await fetch_all(db, Asset)}
        shots = {s.id: s for s in await fetch_all(db, Shot)}
        versions = {v.id: v for v in await fetch_all(db, GenerationVersion)}
        by_track: dict[str, list[dict[str, Any]]] = {t.id: [] for t in tracks}
        for clip in clips:
            if clip.track_id not in by_track:
                continue
            shot = shots.get(clip.shot_id or "")
            version = versions.get(clip.version_id or "")
            asset = assets_by_id.get(clip.asset_id or "")
            by_track[clip.track_id].append(
                {
                    **as_dict(clip),
                    "shot_index_no": shot.index_no if shot else None,
                    "version_no": version.version_no if version else None,
                    "asset_path": asset.path if asset else None,
                    "missing_file": bool(clip.asset_id) and asset is None,
                }
            )
        total = max(
            (c["start"] + c["duration"] for items in by_track.values() for c in items), default=0.0
        )
        return {
            **as_dict(timeline),
            "tracks": [{**as_dict(t), "clips": by_track.get(t.id, [])} for t in tracks],
            "duration_total": total,
            "can_undo": bool(self._undo.get(pid)),
            "can_redo": bool(self._redo.get(pid)),
        }

    # --- 撤销栈 ---

    async def _capture(self, pid: str) -> None:
        clips = [as_dict(c) for c in await fetch_all(db_of(pid), TimelineClip)]
        stack = self._undo.setdefault(pid, [])
        stack.append(clips)
        del stack[:-UNDO_DEPTH]
        self._redo.pop(pid, None)

    async def _restore(self, pid: str, clips: list[dict[str, Any]]) -> None:
        db = db_of(pid)
        current = await fetch_all(db, TimelineClip)
        async with db.write() as session:
            for row in current:
                fresh = await session.get(TimelineClip, row.id)
                if fresh is not None:
                    await session.delete(fresh)
            for data in clips:
                session.add(TimelineClip(**data))

    async def undo(self, pid: str) -> dict[str, Any]:
        stack = self._undo.get(pid) or []
        if not stack:
            raise AppError(
                ErrorCode.CONFLICT,
                "没有可撤销的操作",
                "撤销栈是空的（重启应用后会清空）。",
                ["继续编辑后再撤销"],
            )
        current = [as_dict(c) for c in await fetch_all(db_of(pid), TimelineClip)]
        await self._restore(pid, stack.pop())
        self._redo.setdefault(pid, []).append(current)
        return await self.get(pid)

    async def redo(self, pid: str) -> dict[str, Any]:
        stack = self._redo.get(pid) or []
        if not stack:
            raise AppError(
                ErrorCode.CONFLICT,
                "没有可恢复的操作",
                "恢复栈是空的。",
                ["先撤销一次再恢复"],
            )
        current = [as_dict(c) for c in await fetch_all(db_of(pid), TimelineClip)]
        await self._restore(pid, stack.pop())
        self._undo.setdefault(pid, []).append(current)
        return await self.get(pid)

    # --- 装配 ---

    async def auto_assemble(self, pid: str, *, replace: bool = True) -> dict[str, Any]:
        """按 Scene / Shot 顺序把当前版本铺到视频轨。没有当前版本的镜头明确列出来。"""
        timeline = await self.get(pid)
        video = next((t for t in timeline["tracks"] if t["kind"] == "video"), None)
        if video is None:  # pragma: no cover - get() 保证有 V1
            raise AppError(ErrorCode.CONFLICT, "时间线缺少视频轨", "", ["重建时间线"])
        db = db_of(pid)
        scenes = await fetch_all(db, Scene, order_by=Scene.index_no)
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        versions = {v.id: v for v in await fetch_all(db, GenerationVersion)}
        ordered: list[Shot] = []
        for scene in scenes:
            ordered += [s for s in shots if s.scene_id == scene.id]

        await self._capture(pid)
        placed, skipped = [], []
        cursor = 0.0
        async with db.write() as session:
            if replace:
                for clip in await fetch_all(
                    db, TimelineClip, where=TimelineClip.track_id == video["id"]
                ):
                    fresh = await session.get(TimelineClip, clip.id)
                    if fresh is not None:
                        await session.delete(fresh)
            for shot in ordered:
                version = versions.get(shot.current_version_id or "")
                if version is None or version.asset_id is None:
                    skipped.append(
                        {"shot_id": shot.id, "index_no": shot.index_no, "reason": "还没有当前版本"}
                    )
                    continue
                duration = float(version.duration or shot.duration or 4.0)
                clip = TimelineClip(
                    id=new_id("timeline_clip"),
                    track_id=video["id"],
                    shot_id=shot.id,
                    version_id=version.id,
                    asset_id=version.asset_id,
                    index_no=len(placed) + 1,
                    start=cursor,
                    duration=duration,
                    in_point=0.0,
                    out_point=duration,
                    label=f"Shot {shot.index_no} {shot.title}".strip(),
                )
                session.add(clip)
                placed.append(clip.id)
                cursor += duration
        bus.emit(
            Channel.SHOT,
            "timeline.assembled",
            {"clips": len(placed), "skipped": len(skipped)},
            project_id=pid,
        )
        return {"placed": placed, "skipped": skipped, "timeline": await self.get(pid)}

    # --- 编辑命令 ---

    async def move_clip(self, pid: str, clip_id: str, start: float) -> dict[str, Any]:
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        await self._capture(pid)
        siblings = [
            c
            for c in await fetch_all(db, TimelineClip, where=TimelineClip.track_id == clip.track_id)
            if c.id != clip_id
        ]
        snapped = max(0.0, float(start))
        for other in siblings:  # 吸附到邻居边界
            for edge in (other.start, other.start + other.duration):
                if abs(snapped - edge) <= SNAP:
                    snapped = edge
        async with db.write() as session:
            row = await session.get(TimelineClip, clip_id)
            assert row is not None
            row.start = snapped
        await self._reindex(pid, clip.track_id)
        return await self.get(pid)

    async def trim_clip(
        self,
        pid: str,
        clip_id: str,
        *,
        in_point: float | None = None,
        out_point: float | None = None,
        ripple: bool = True,
    ) -> dict[str, Any]:
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        new_in = clip.in_point if in_point is None else max(0.0, float(in_point))
        source_end = clip.out_point if clip.out_point is not None else clip.in_point + clip.duration
        new_out = source_end if out_point is None else float(out_point)
        if new_out - new_in <= 0:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "裁切后长度为零",
                f"in={new_in} out={new_out}，片段不能没有长度。",
                ["把出点调到入点之后", "或撤销这次裁切"],
                {"clip_id": clip_id},
            )
        await self._capture(pid)
        async with db.write() as session:
            row = await session.get(TimelineClip, clip_id)
            assert row is not None
            row.in_point = new_in
            row.out_point = new_out
            row.duration = new_out - new_in
        if ripple:
            await self._close_gaps(pid, clip.track_id)
        return await self.get(pid)

    async def split_clip(self, pid: str, clip_id: str, at: float) -> dict[str, Any]:
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        offset = float(at) - clip.start
        if not 0 < offset < clip.duration:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "切点不在片段内",
                f"片段范围 {clip.start}~{clip.start + clip.duration}，切点 {at}。",
                ["把播放头移到片段内部再切"],
                {"clip_id": clip_id},
            )
        await self._capture(pid)
        async with db.write() as session:
            head = await session.get(TimelineClip, clip_id)
            assert head is not None
            tail = TimelineClip(
                id=new_id("timeline_clip"),
                track_id=clip.track_id,
                shot_id=clip.shot_id,
                version_id=clip.version_id,
                asset_id=clip.asset_id,
                index_no=clip.index_no + 1,
                start=clip.start + offset,
                duration=clip.duration - offset,
                in_point=clip.in_point + offset,
                out_point=clip.out_point,
                label=clip.label,
            )
            head.duration = offset
            head.out_point = clip.in_point + offset
            session.add(tail)
        await self._reindex(pid, clip.track_id)
        return await self.get(pid)

    async def delete_clip(self, pid: str, clip_id: str, *, ripple: bool = True) -> dict[str, Any]:
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        await self._capture(pid)
        async with db.write() as session:
            fresh = await session.get(TimelineClip, clip_id)
            if fresh is not None:
                await session.delete(fresh)
        if ripple:
            await self._close_gaps(pid, clip.track_id)
        else:
            await self._reindex(pid, clip.track_id)
        return await self.get(pid)

    async def replace_version(self, pid: str, clip_id: str, version_id: str) -> dict[str, Any]:
        """只换这一个片段的素材，整条时间线不重排。"""
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        version = await fetch(db, GenerationVersion, version_id, "生成版本")
        if clip.shot_id and version.shot_id != clip.shot_id:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "版本不属于该片段的镜头",
                f"片段来自 {clip.shot_id}，版本来自 {version.shot_id}。",
                ["只在同一镜头的版本之间替换"],
                {"clip_id": clip_id, "version_id": version_id},
            )
        if version.asset_id is None:
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "该版本没有可用素材",
                f"版本 v{version.version_no} 没有关联资产。",
                ["换一个有素材的版本", "或重新生成这个镜头"],
                {"version_id": version_id},
            )
        await self._capture(pid)
        async with db.write() as session:
            row = await session.get(TimelineClip, clip_id)
            assert row is not None
            row.version_id = version_id
            row.asset_id = version.asset_id
        return await self.get(pid)

    async def _reindex(self, pid: str, track_id: str) -> None:
        db = db_of(pid)
        rows = sorted(
            await fetch_all(db, TimelineClip, where=TimelineClip.track_id == track_id),
            key=lambda c: c.start,
        )
        async with db.write() as session:
            for i, row in enumerate(rows):
                fresh = await session.get(TimelineClip, row.id)
                if fresh is not None:
                    fresh.index_no = i + 1

    async def _close_gaps(self, pid: str, track_id: str) -> None:
        """吸附收尾：把片段一个接一个贴紧，中间不留黑帧。"""
        db = db_of(pid)
        rows = sorted(
            await fetch_all(db, TimelineClip, where=TimelineClip.track_id == track_id),
            key=lambda c: c.start,
        )
        cursor = 0.0
        async with db.write() as session:
            for i, row in enumerate(rows):
                fresh = await session.get(TimelineClip, row.id)
                if fresh is None:
                    continue
                fresh.start = cursor
                fresh.index_no = i + 1
                cursor += fresh.duration

    # --- 转场 ---

    async def add_transition(
        self,
        pid: str,
        from_clip_id: str,
        to_clip_id: str,
        kind: str = "dissolve",
        duration: float = 0.5,
    ) -> dict[str, Any]:
        if kind not in TRANSITION_KINDS:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的转场类型",
                f"{kind} 不在 {'、'.join(TRANSITION_KINDS)} 里。",
                ["用列表里的转场类型"],
            )
        db = db_of(pid)
        await fetch(db, TimelineClip, from_clip_id, "片段")
        await fetch(db, TimelineClip, to_clip_id, "片段")
        timeline = await self.get(pid)
        row = Transition(
            id=new_id("transition"),
            timeline_id=timeline["id"],
            from_clip_id=from_clip_id,
            to_clip_id=to_clip_id,
            kind=kind,
            duration=float(duration),
        )
        async with db.write() as session:
            session.add(row)
        return as_dict(row)

    async def list_transitions(self, pid: str) -> list[dict[str, Any]]:
        return [as_dict(r) for r in await fetch_all(db_of(pid), Transition)]

    async def delete_transition(self, pid: str, tid: str) -> None:
        db = db_of(pid)
        await fetch(db, Transition, tid, "转场")
        async with db.write() as session:
            fresh = await session.get(Transition, tid)
            if fresh is not None:
                await session.delete(fresh)

    # --- 导出 ---

    def _ffmpeg(self) -> str:
        found = shutil.which(settings.ffmpeg_path) or (
            settings.ffmpeg_path if Path(settings.ffmpeg_path).is_file() else None
        )
        if not found:
            raise AppError(
                ErrorCode.FFMPEG_MISSING,
                "找不到 FFmpeg",
                f"当前配置 ffmpeg_path = {settings.ffmpeg_path}，在 PATH 里也没找到。",
                [
                    "安装 FFmpeg 并加入 PATH",
                    "或用 AIVS_FFMPEG_PATH 指向可执行文件的绝对路径",
                ],
            )
        return found

    async def ensure_proxy(self, pid: str, asset_id: str) -> dict[str, Any]:
        """为预览生成 720p 代理。代理只用于播放，导出永远走原始素材。"""
        db = db_of(pid)
        proj = project_of(pid)
        asset = await fetch(db, Asset, asset_id, "资产")
        src = proj.dir / asset.path
        if not src.is_file():
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "源文件不在了",
                f"{asset.path} 在磁盘上找不到。",
                ["从备份恢复该文件", "或重新生成对应镜头"],
                {"asset_id": asset_id},
            )
        target = proj.dir / "proxies" / f"{Path(asset.path).stem}_720p.mp4"
        if target.is_file():
            return {"asset_id": asset_id, "proxy_path": target.as_posix(), "reused": True}
        target.parent.mkdir(parents=True, exist_ok=True)
        args = [
            self._ffmpeg(),
            "-y",
            "-i",
            str(src),
            "-vf",
            "scale=-2:720",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "26",
            "-an",
            str(target),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise AppError(
                ErrorCode.FFMPEG_ERROR,
                "代理生成失败",
                f"FFmpeg 退出码 {proc.returncode}。",
                ["预览可以直接播放原始素材", "确认该文件的编码可被 FFmpeg 读取"],
                {"asset_id": asset_id, "raw": (stderr or b"").decode("utf-8", "replace")[-2000:]},
            )
        registered = await asset_service.register_path(
            pid, "proxy", str(target), source="proxy", copy=False
        )
        log.info("timeline.proxy_built", project_id=pid, asset_id=asset_id)
        return {
            "asset_id": asset_id,
            "proxy_asset_id": registered["id"],
            "proxy_path": target.as_posix(),
            "reused": False,
        }

    async def build_command(self, pid: str, out_path: str | None = None) -> dict[str, Any]:
        """产出 FFmpeg 命令。用原始素材而不是代理。"""
        timeline = await self.get(pid)
        video = next((t for t in timeline["tracks"] if t["kind"] == "video"), None)
        clips = sorted((video or {}).get("clips") or [], key=lambda c: c["start"])
        if not clips:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "时间线是空的",
                "视频轨上没有任何片段。",
                ["先点「自动装配」", "或手动把片段拖到轨道上"],
            )
        proj = project_of(pid)
        missing = [
            c for c in clips if not c["asset_path"] or not (proj.dir / c["asset_path"]).is_file()
        ]
        if missing:
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "有片段的源文件不在了",
                f"{len(missing)} 个片段找不到磁盘文件："
                + "、".join(str(c.get("label") or c["id"]) for c in missing[:5]),
                ["从备份恢复这些文件", "或重新生成对应镜头后再导出"],
                {"clip_ids": [c["id"] for c in missing]},
            )
        target = (
            Path(out_path)
            if out_path
            else (
                proj.dir
                / "generations"
                / "exports"
                / f"export_{utc_now()[:19].replace(':', '-')}.mp4"
            )
        )
        target.parent.mkdir(parents=True, exist_ok=True)

        args: list[str] = [self._ffmpeg(), "-y"]
        for clip in clips:
            src = proj.dir / clip["asset_path"]
            args += [
                "-ss",
                f"{clip['in_point']:.3f}",
                "-t",
                f"{clip['duration']:.3f}",
                "-i",
                str(src),
            ]
        parts = []
        for i, _ in enumerate(clips):
            parts.append(
                f"[{i}:v]scale={timeline['width']}:{timeline['height']}:force_original_aspect_ratio=decrease,"
                f"pad={timeline['width']}:{timeline['height']}:-1:-1,setsar=1,"
                f"fps={timeline['fps']}[v{i}]"
            )
        concat = "".join(f"[v{i}]" for i in range(len(clips)))
        filter_complex = ";".join(parts) + f";{concat}concat=n={len(clips)}:v=1:a=0[vout]"
        args += [
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "18",
            str(target),
        ]
        return {
            "path": str(target),
            "args": args,
            "command": " ".join(args),
            "clips": clips,
            "version_ids": [c["version_id"] for c in clips if c["version_id"]],
        }

    async def export(self, pid: str, out_path: str | None = None) -> dict[str, Any]:
        plan = await self.build_command(pid, out_path)
        timeline = await self.get(pid)
        db = db_of(pid)
        record = ExportRecord(
            id=new_id("export_record"),
            timeline_id=timeline["id"],
            path=plan["path"],
            status="running",
            version_ids_json=dump_json(plan["version_ids"]),
            command=plan["command"],
            created_at=utc_now(),
        )
        async with db.write() as session:
            session.add(record)
        bus.emit(
            Channel.JOB, "export.started", {"id": record.id, "path": plan["path"]}, project_id=pid
        )

        proc = await asyncio.create_subprocess_exec(
            *plan["args"], stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        tail = (stderr or b"").decode("utf-8", "replace")[-4000:]
        if proc.returncode != 0:
            err = AppError(
                ErrorCode.FFMPEG_ERROR,
                "导出失败",
                f"FFmpeg 退出码 {proc.returncode}。",
                [
                    "展开原始报错查看 FFmpeg 输出",
                    "确认所有片段的源文件编码可被读取",
                    "尝试先重新生成有问题的镜头",
                ],
                {"export_id": record.id},
            )
            async with db.write() as session:
                fresh = await session.get(ExportRecord, record.id)
                if fresh is not None:
                    fresh.status = "failed"
                    fresh.error_json = dump_json({**err.to_dict(), "raw": tail})
                    fresh.finished_at = utc_now()
            bus.emit(
                Channel.ERROR, "export.failed", {"id": record.id, **err.to_dict()}, project_id=pid
            )
            raise err

        asset = await asset_service.register_path(
            pid, "export", plan["path"], source="export", copy=False
        )
        async with db.write() as session:
            fresh = await session.get(ExportRecord, record.id)
            if fresh is not None:
                fresh.status = "done"
                fresh.finished_at = utc_now()
                fresh.duration = timeline["duration_total"]
        bus.emit(
            Channel.JOB, "export.done", {"id": record.id, "path": plan["path"]}, project_id=pid
        )
        return {
            "id": record.id,
            "path": plan["path"],
            "asset_id": asset["id"],
            "version_ids": plan["version_ids"],
            "duration": timeline["duration_total"],
        }

    async def list_exports(self, pid: str) -> list[dict[str, Any]]:
        rows = await fetch_all(db_of(pid), ExportRecord, order_by=ExportRecord.created_at.desc())
        return [
            {
                **as_dict(r),
                "version_ids": load_json(r.version_ids_json, []),
                "error": load_json(r.error_json, None),
            }
            for r in rows
        ]


timeline = TimelineService()
