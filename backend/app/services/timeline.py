"""时间线与导出（Step 8）。

这一层完全不依赖 AI：即使 ComfyUI 和 LLM 都不在，装配、剪辑、导出照样能跑。
撤销栈用整轨快照实现——片段数量是几十到几百的量级，快照比逐条反向操作更不容易错。
**快照连轨道一起存**：轨道也能增删（拆声音会自动开新的音频轨），只存片段的话，
撤销之后会剩下一堆挂在已经不存在的轨道上的片段——`_shape` 会把它们悄悄跳过，
用户看到的就是「撤销把我的片段吃掉了」。

声音有两个来源，各自独立：**没被静音的视频片段自带的音轨**，与**音频轨上的片段**
（`services/audio.py` 从画面里拆出来的，或用户导入的配乐）。音频轨之间可以随意重叠，
导出时用 `amix` 叠在一起——重叠是音频轨存在的意义，不是错误。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from app.core import ffmpeg as ffmpeg_tool
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.events.bus import Channel, bus
from app.persistence.models import utc_now
from app.persistence.models_edit import (
    TRACK_KINDS,
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
from app.services.audio import audio as audio_service
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, load_json, project_of

log = get_logger("timeline")

#: 吸附阈值（秒）：剪短片段后自动贴到前一段末尾，中间不留黑帧。
SNAP = 0.08
UNDO_DEPTH = 50
#: 自动起名用的前缀（V1 / A2 / S1）。轨道名是给人看的，可以随便改。
TRACK_PREFIX = {"video": "V", "audio": "A", "subtitle": "S"}
#: 音量倍数的上限。再往上不是「更响」而是削波，拦在这里比让人导出一坨爆音好。
MAX_VOLUME = 4.0
#: 导出音频统一 aac。
EXPORT_AUDIO_BITRATE = "192k"
#: 成片的声音**必须是一个定死的格式**，不能是「素材恰好是什么就是什么」。
#:
#: 两个理由，第二个是踩出来的：
#:   1. 混音的每一路来自不同素材（生成的视频多是单声道 44.1k，导入的配乐常是立体声 48k）。
#:      不收口的话成片声道数取决于第一路素材，同一条时间线换一段素材就变一次。
#:   2. **单声道 + 192k 会让 FFmpeg 的原生 aac 编码器停在原地**（本机 ffmpeg 8.0：
#:      `amix` 之后接单声道 192k，画面写到 3.97s 就再也不前进，进程不退也不报错，
#:      内存一路涨）。降到 160k 或者收成立体声都好，所以这里收成立体声——
#:      顺便把上面第 1 条也解决了。改动它之前先跑一遍 12 秒的真实导出。
EXPORT_AUDIO_FORMAT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

#: 装配铺配音的那条音频轨的名字。**单独一条**：用户自己的配乐、从画面里拆出去的声音
#: 都在别的音频轨上，两边就不会抢同一段时间，装配也不用在这里算避让。
DUB_TRACK_NAME = "A-配音"
#: 时长对不上多少秒算「值得说一句」。低于它是编码误差，报出来只会变成噪音。
AUDIO_DRIFT = 0.25

#: 一次撤销快照：轨道 + 片段两张表的整体状态。
Snapshot = dict[str, list[dict[str, Any]]]


def _version_span(version: GenerationVersion, shot: Shot) -> tuple[float, float]:
    """这一版**能用的源区间**（秒）。

    两列都空 = 整个文件，所以老版本行为不变；长视频切段的版本各自带自己的区间，
    `asset_id` 全部指向同一个源文件（零文件复制）。
    """
    start = float(version.in_point or 0.0)
    if version.out_point is not None:
        return start, max(start, float(version.out_point))
    length = float(version.duration or shot.duration or 4.0)
    return start, start + max(0.0, length)


def _keep_trim(
    clip: TimelineClip | None,
    span: tuple[float, float],
    warnings: list[dict[str, Any]],
    label: str,
) -> tuple[float, float]:
    """把手工裁切带到新版本上。**装得下就原样保留，装不下就收进来并说一句。**

    没有这一步，「二次处理」这一层每次重新装配都会被生成层踩平：用户剪掉的开头
    会自己长回来。静默截断同样不行（硬约束 4），所以收窄一律进 `warnings`。
    """
    lo, hi = span
    if clip is None:
        return lo, hi
    old_in = float(clip.in_point or 0.0)
    old_out = float(clip.out_point) if clip.out_point is not None else old_in + float(clip.duration)
    trimmed = old_in > lo + 0.001 or old_out + 0.001 < hi
    if not trimmed:
        return lo, hi
    if old_in >= lo - 0.001 and old_out <= hi + 0.001:
        return old_in, old_out
    new_in = min(max(old_in, lo), hi)
    new_out = max(min(old_out, hi), new_in)
    if new_out - new_in < 0.05:
        warnings.append(
            {
                "kind": "trim_reset",
                "clip_id": clip.id,
                "detail": f"「{label}」原来的裁切区间 {old_in:.2f}~{old_out:.2f}s "
                f"在新版本（{lo:.2f}~{hi:.2f}s）里已经不存在，这一段恢复成全长。",
                "suggestion": "重新裁一次，或者用「撤销」退回上一版装配",
            }
        )
        return lo, hi
    warnings.append(
        {
            "kind": "trim_clamped",
            "clip_id": clip.id,
            "detail": f"「{label}」的裁切区间从 {old_in:.2f}~{old_out:.2f}s "
            f"收到了 {new_in:.2f}~{new_out:.2f}s（新版本只有 {lo:.2f}~{hi:.2f}s）。",
            "suggestion": "确认这一段的出入点",
        }
    )
    return new_in, new_out


def _warn_audio_length(
    audio: GenerationVersion, video_duration: float, label: str, warnings: list[dict[str, Any]]
) -> None:
    """配音比画面长 / 短了就说出来。**绝不静默变速**——那是把声音改了却不告诉人。"""
    length = float(audio.duration or 0.0)
    if length <= 0 or abs(length - video_duration) <= AUDIO_DRIFT:
        return
    verb = "长" if length > video_duration else "短"
    warnings.append(
        {
            "kind": "audio_length",
            "version_id": audio.id,
            "detail": f"「{label}」的配音 {length:.2f}s，比画面 {video_duration:.2f}s "
            f"{verb} {abs(length - video_duration):.2f}s；这一段按画面长度铺，"
            f"{'多出来的会被切掉' if length > video_duration else '结尾会没有声音'}。",
            "suggestion": "重新生成配音时把时长对齐，或者在音频轨上手工调这一段",
        }
    )


class TimelineService:
    def __init__(self) -> None:
        self._undo: dict[str, list[Snapshot]] = {}
        self._redo: dict[str, list[Snapshot]] = {}

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
        kind_by_track = {t.id: t.kind for t in tracks}
        known_clips = {c.id for c in clips}
        # 「这段画面的声音已经拆出去了」是双向的：源片段要能指到那条音频，
        # 音频要能说出自己是从哪来的。两边都在这里一次算好，前端不再自己配对。
        detached: dict[str, str] = {}
        for clip in clips:
            if clip.source_clip_id and clip.source_clip_id not in detached:
                detached[clip.source_clip_id] = clip.id
        by_track: dict[str, list[dict[str, Any]]] = {t.id: [] for t in tracks}
        for clip in clips:
            if clip.track_id not in by_track:
                continue
            shot = shots.get(clip.shot_id or "")
            version = versions.get(clip.version_id or "")
            asset = assets_by_id.get(clip.asset_id or "")
            source_duration = (
                float(asset.duration)
                if asset is not None and asset.duration is not None
                else float(version.duration)
                if version is not None and version.duration is not None
                else float(clip.out_point)
                if clip.out_point is not None
                else float(clip.in_point + clip.duration)
            )
            by_track[clip.track_id].append(
                {
                    **as_dict(clip),
                    "track_kind": kind_by_track.get(clip.track_id),
                    "shot_index_no": shot.index_no if shot else None,
                    "version_no": version.version_no if version else None,
                    "asset_path": asset.path if asset else None,
                    "source_duration": source_duration,
                    "missing_file": bool(clip.asset_id) and asset is None,
                    #: 这段画面的声音被拆到了哪个片段上（没拆过是 None）。
                    "detached_audio_clip_id": detached.get(clip.id),
                    #: 拆声音的那段画面已经不在了（删掉或重新装配过）。声音照旧能播，
                    #: 但界面上要标出来——不然用户不知道它为什么对不上任何画面。
                    "source_missing": bool(clip.source_clip_id)
                    and clip.source_clip_id not in known_clips,
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

    async def _snapshot(self, pid: str) -> Snapshot:
        """当前的轨道 + 片段。**两张表必须一起存**，理由见模块开头。"""
        db = db_of(pid)
        return {
            "tracks": [as_dict(t) for t in await fetch_all(db, Track)],
            "clips": [as_dict(c) for c in await fetch_all(db, TimelineClip)],
        }

    async def _capture(self, pid: str) -> None:
        stack = self._undo.setdefault(pid, [])
        stack.append(await self._snapshot(pid))
        del stack[:-UNDO_DEPTH]
        self._redo.pop(pid, None)

    async def _restore(self, pid: str, snap: Snapshot) -> None:
        db = db_of(pid)
        current_clips = await fetch_all(db, TimelineClip)
        current_tracks = await fetch_all(db, Track)
        want_tracks = {t["id"]: t for t in snap["tracks"]}
        async with db.write() as session:
            # 片段全删了重建：轨道是它的外键父，顺序反了会撞外键。
            for row in current_clips:
                fresh = await session.get(TimelineClip, row.id)
                if fresh is not None:
                    await session.delete(fresh)
            await session.flush()
            for row in current_tracks:
                fresh = await session.get(Track, row.id)
                if fresh is None:  # pragma: no cover - 同一事务内不该消失
                    continue
                data = want_tracks.pop(row.id, None)
                if data is None:  # 快照之后新建的轨道
                    await session.delete(fresh)
                    continue
                for key, value in data.items():
                    setattr(fresh, key, value)
            await session.flush()
            for data in want_tracks.values():  # 快照之后被删掉的轨道，原样补回
                session.add(Track(**data))
            await session.flush()
            for data in snap["clips"]:
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
        current = await self._snapshot(pid)
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
        current = await self._snapshot(pid)
        await self._restore(pid, stack.pop())
        self._undo.setdefault(pid, []).append(current)
        return await self.get(pid)

    # --- 轨道 ---

    async def add_track(
        self, pid: str, *, kind: str = "audio", name: str | None = None
    ) -> dict[str, Any]:
        """加一条轨道。名字不给就自动编号（A1 已占用就叫 A2）。"""
        if kind not in TRACK_KINDS:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的轨道类型",
                f"{kind} 不在 {'、'.join(TRACK_KINDS)} 里。",
                ["用列表里的轨道类型"],
            )
        timeline = await self.get(pid)
        db = db_of(pid)
        rows = await fetch_all(db, Track, where=Track.timeline_id == timeline["id"])
        await self._capture(pid)
        row = Track(
            id=new_id("track"),
            timeline_id=timeline["id"],
            kind=kind,
            index_no=max((t.index_no for t in rows), default=-1) + 1,
            name=(name or "").strip() or self._next_name(kind, rows),
        )
        async with db.write() as session:
            session.add(row)
        return {"track": as_dict(row), "timeline": await self.get(pid)}

    async def update_track(
        self,
        pid: str,
        track_id: str,
        *,
        name: str | None = None,
        muted: bool | None = None,
        locked: bool | None = None,
    ) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Track, track_id, "轨道")
        await self._capture(pid)
        async with db.write() as session:
            row = await session.get(Track, track_id)
            assert row is not None
            if name is not None and name.strip():
                row.name = name.strip()
            if muted is not None:
                row.muted = int(muted)
            if locked is not None:
                row.locked = int(locked)
        return await self.get(pid)

    async def delete_track(self, pid: str, track_id: str, *, force: bool = False) -> dict[str, Any]:
        """删一条轨道。上面还有片段时先问一句——那是一次点击毁掉一堆剪辑的操作。"""
        db = db_of(pid)
        track = await fetch(db, Track, track_id, "轨道")
        tracks = await fetch_all(db, Track, where=Track.timeline_id == track.timeline_id)
        if track.kind == "video" and len([t for t in tracks if t.kind == "video"]) <= 1:
            raise AppError(
                ErrorCode.CONFLICT,
                "不能删掉唯一的视频轨",
                f"{track.name} 是最后一条视频轨，删了就没有画面可导出了。",
                ["要清空画面请删掉轨道上的片段", "或者先加一条新的视频轨"],
                {"track_id": track_id},
            )
        clips = await fetch_all(db, TimelineClip, where=TimelineClip.track_id == track_id)
        if clips and not force:
            raise AppError(
                ErrorCode.CONFLICT,
                "这条轨道上还有片段",
                f"{track.name} 上有 {len(clips)} 段，删除轨道会把它们一起删掉。",
                ["确认要连片段一起删，请再按一次", "或者先把这些片段移到别的轨道上"],
                #: confirm 告诉前端「重放这次请求时加上哪个开关」，与生成层的
                #: allow_ref_drop 是同一套约定：需要确认不是失败。
                {"track_id": track_id, "clips": len(clips), "confirm": "force"},
            )
        await self._capture(pid)
        async with db.write() as session:
            for clip in clips:
                stale = await session.get(TimelineClip, clip.id)
                if stale is not None:
                    await session.delete(stale)
            await session.flush()
            fresh = await session.get(Track, track_id)
            if fresh is not None:
                await session.delete(fresh)
        return await self.get(pid)

    def _next_name(self, kind: str, existing: list[Track]) -> str:
        prefix = TRACK_PREFIX.get(kind, "T")
        used = {t.name for t in existing}
        n = 1
        while f"{prefix}{n}" in used:
            n += 1
        return f"{prefix}{n}"

    # --- 装配 ---

    async def assemble_plan(self, pid: str) -> dict[str, Any]:
        """**只读账单**：这次重新装配会新增 / 更新 / 删除 / 挪动哪些片段，哪几段有冲突。

        照 `sequence.plan` / `adopt.plan` 的老规矩——「重新装配」以前是个不敢按的按钮
        （它会清空整条视频轨），现在按之前能先看见它要干什么。
        """
        diff = await self._assemble_diff(pid, dub_track_id=None)
        return {
            "add": diff["add"],
            "update": diff["update"],
            "remove": diff["remove"],
            "move": diff["move"],
            "skipped": diff["skipped"],
            "warnings": diff["warnings"],
            "repaired": diff["repaired"],
            #: 用户自己铺的片段有几段（装配一段都不会碰）。
            "preserved": diff["preserved"],
            #: 需要为「采用的配音」新开一条音频轨（`run` 时才真的开）。
            "dub_track_needed": diff["dub_track_needed"],
        }

    async def auto_assemble(self, pid: str, *, replace: bool = False) -> dict[str, Any]:
        """按 Scene / Shot 顺序把当前版本对位铺到视频轨（**只碰自己铺的那些片段**）。

        这里刻意**不是**「清空整轨再重建」：

          · 用户在片段上做过的手工裁切 / 静音 / 音量必须活下来，不然「二次处理」这一层
            每次重新装配都会被生成层踩平；
          · 片段 id 保住了，被拆到音频轨上的声音就不会集体悬空
            （`_shape` 里那个 `source_missing` 说的就是它）；
          · 长视频切出来的段、用户自己加的素材是 `origin="manual"`，装配永不触碰。

        `replace=True` 是**兼容入口**：先把自己铺过的全删掉再铺一遍（`manual` 的仍然不动），
        给「我就是要彻底重铺」这个诉求留一条路。
        """
        timeline = await self.get(pid)
        video = next((t for t in timeline["tracks"] if t["kind"] == "video"), None)
        if video is None:  # pragma: no cover - get() 保证有 V1
            raise AppError(ErrorCode.CONFLICT, "时间线缺少视频轨", "", ["重建时间线"])
        db = db_of(pid)

        await self._capture(pid)
        if replace:
            async with db.write() as session:
                for clip in await fetch_all(db, TimelineClip):
                    if clip.origin != "assembled":
                        continue
                    fresh = await session.get(TimelineClip, clip.id)
                    if fresh is not None:
                        await session.delete(fresh)

        #: 采用了配音的镜头要有地方放。**单独一条轨**，只装装配铺的配音——和用户自己的
        #: 配乐 / 拆出来的声音分开，两边就不会抢同一段时间，也不用在这里算避让。
        dub_track_id = await self._ensure_dub_track(pid, needed=await self._dub_needed(pid))
        diff = await self._assemble_diff(pid, dub_track_id=dub_track_id)

        placed: list[str] = []
        async with db.write() as session:
            for item in diff["remove"]:
                fresh = await session.get(TimelineClip, item["clip_id"])
                if fresh is not None:
                    await session.delete(fresh)
            for item in diff["update"]:
                fresh = await session.get(TimelineClip, item["clip_id"])
                if fresh is None:
                    continue
                fresh.version_id = item["version_id"]
                fresh.asset_id = item["asset_id"]
                fresh.shot_id = item["shot_id"]
                fresh.index_no = item["index_no"]
                fresh.start = item["start"]
                fresh.duration = item["duration"]
                fresh.in_point = item["in_point"]
                fresh.out_point = item["out_point"]
                fresh.label = item["label"]
                if item["mute"]:
                    # 只强制静音，绝不强制取消静音——「这一段我不想让它出声」也是用户的编辑。
                    fresh.muted = 1
                placed.append(fresh.id)
            for item in diff["add"]:
                row = TimelineClip(
                    id=new_id("timeline_clip"),
                    track_id=item["track_id"],
                    shot_id=item["shot_id"],
                    version_id=item["version_id"],
                    asset_id=item["asset_id"],
                    index_no=item["index_no"],
                    start=item["start"],
                    duration=item["duration"],
                    in_point=item["in_point"],
                    out_point=item["out_point"],
                    label=item["label"],
                    muted=1 if item["mute"] else 0,
                    volume=1.0,
                    origin="assembled",
                )
                session.add(row)
                placed.append(row.id)
            for item in diff["repaired"]:
                fresh = await session.get(TimelineClip, item["clip_id"])
                if fresh is not None:
                    fresh.source_clip_id = item["to_clip_id"]

        bus.emit(
            Channel.SHOT,
            "timeline.assembled",
            {
                "clips": len(placed),
                "added": len(diff["add"]),
                "updated": len(diff["update"]),
                "removed": len(diff["remove"]),
                "skipped": len(diff["skipped"]),
            },
            project_id=pid,
        )
        return {
            "placed": placed,
            "added": [i["shot_id"] for i in diff["add"]],
            "updated": [i["clip_id"] for i in diff["update"]],
            "removed": [i["clip_id"] for i in diff["remove"]],
            "skipped": diff["skipped"],
            "warnings": diff["warnings"],
            "repaired": diff["repaired"],
            "preserved": diff["preserved"],
            "timeline": await self.get(pid),
        }

    async def _dub_needed(self, pid: str) -> bool:
        """有没有镜头采用了配音——没有就不要凭空开一条空轨道。"""
        return any(bool(s.current_audio_version_id) for s in await fetch_all(db_of(pid), Shot))

    async def _ensure_dub_track(self, pid: str, *, needed: bool) -> str | None:
        if not needed:
            return None
        timeline = await self.get(pid)
        existing = next(
            (t for t in timeline["tracks"] if t["kind"] == "audio" and t["name"] == DUB_TRACK_NAME),
            None,
        )
        if existing is not None:
            return str(existing["id"])
        track = (await self.add_track(pid, kind="audio", name=DUB_TRACK_NAME))["track"]
        return str(track["id"])

    async def _assemble_diff(self, pid: str, *, dub_track_id: str | None) -> dict[str, Any]:
        """算出「对位调和」要做的事。**只读，不写库**，所以 plan 与 run 共用它。

        对位的键是 `(轨道用途, shot_id)`：镜头还在、版本换了就只改素材；镜头没了才删；
        顺序变了只挪 `start`。**手工裁切能活下来**——新版本装得下就原样保留，装不下就
        收进新区间并在账单里说一句，绝不静默截断（硬约束 4）。
        """
        db = db_of(pid)
        timeline = await self.get(pid)
        video = next((t for t in timeline["tracks"] if t["kind"] == "video"), None)
        if video is None:  # pragma: no cover
            raise AppError(ErrorCode.CONFLICT, "时间线缺少视频轨", "", ["重建时间线"])
        video_track_id = str(video["id"])

        scenes = await fetch_all(db, Scene, order_by=Scene.index_no)
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        shots_by_id = {s.id: s for s in shots}
        versions = {v.id: v for v in await fetch_all(db, GenerationVersion)}
        ordered: list[Shot] = []
        for scene in scenes:
            ordered += [s for s in shots if s.scene_id == scene.id]

        clips = await fetch_all(db, TimelineClip)
        track_kind = {t["id"]: t["kind"] for t in timeline["tracks"]}
        live_ids = {c.id for c in clips}
        assembled: dict[tuple[str, str], TimelineClip] = {}
        dupes: list[TimelineClip] = []
        manual_video: list[TimelineClip] = []
        manual_count = 0
        for clip in sorted(clips, key=lambda c: (c.index_no, c.start)):
            if clip.origin != "assembled":
                manual_count += 1
                if clip.track_id == video_track_id:
                    manual_video.append(clip)
                continue
            slot = "video" if clip.track_id == video_track_id else "dub"
            key = (slot, clip.shot_id or "")
            if not clip.shot_id or key in assembled:
                dupes.append(clip)
                continue
            assembled[key] = clip

        add: list[dict[str, Any]] = []
        update: list[dict[str, Any]] = []
        remove: list[dict[str, Any]] = []
        move: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        dub_track_needed = False
        cursor = 0.0
        index = 0

        for shot in ordered:
            version = versions.get(shot.current_version_id or "")
            if version is None or version.asset_id is None:
                skipped.append(
                    {"shot_id": shot.id, "index_no": shot.index_no, "reason": "还没有当前版本"}
                )
                continue
            index += 1
            span = _version_span(version, shot)
            audio_version = versions.get(shot.current_audio_version_id or "")
            mute_video = audio_version is not None and audio_version.asset_id is not None
            label = f"Shot {shot.index_no} {shot.title}".strip()

            key = ("video", shot.id)
            seen.add(key)
            current = assembled.get(key)
            window = _keep_trim(current, span, warnings, label)
            spec = {
                "track_id": video_track_id,
                "shot_id": shot.id,
                "version_id": version.id,
                "asset_id": version.asset_id,
                "index_no": index,
                "start": cursor,
                "duration": window[1] - window[0],
                "in_point": window[0],
                "out_point": window[1],
                "label": label,
                "mute": mute_video,
            }
            if current is None:
                add.append(spec)
            else:
                if abs(current.start - cursor) > 0.001:
                    move.append({"clip_id": current.id, "from": current.start, "to": cursor})
                update.append({**spec, "clip_id": current.id})
            cursor += spec["duration"]

            if not mute_video:
                continue
            assert audio_version is not None
            dub_track_needed = True
            dub_key = ("dub", shot.id)
            seen.add(dub_key)
            dub_current = assembled.get(dub_key)
            dub_spec = {
                "track_id": dub_track_id or "",
                "shot_id": shot.id,
                "version_id": audio_version.id,
                "asset_id": audio_version.asset_id,
                "index_no": index,
                "start": spec["start"],
                "duration": spec["duration"],
                "in_point": 0.0,
                "out_point": spec["duration"],
                "label": f"{label} 配音".strip(),
                "mute": False,
            }
            _warn_audio_length(audio_version, spec["duration"], label, warnings)
            if dub_current is None:
                add.append(dub_spec)
            else:
                update.append({**dub_spec, "clip_id": dub_current.id})

        for (slot, shot_id), clip in assembled.items():
            if (slot, shot_id) in seen:
                continue
            shot = shots_by_id.get(shot_id)
            if shot is None:
                reason = "镜头已删除"
            elif slot == "dub":
                reason = "这个镜头不再采用配音"
            else:
                reason = "镜头不再有当前版本"
            remove.append({"clip_id": clip.id, "shot_id": shot_id, "reason": reason})
        for clip in dupes:
            remove.append(
                {
                    "clip_id": clip.id,
                    "shot_id": clip.shot_id,
                    "reason": "同一个镜头有多个装配片段，只留一个",
                }
            )

        for clip in manual_video:
            end = float(clip.start) + float(clip.duration)
            if float(clip.start) < cursor - 0.001 and end > 0.001:
                warnings.append(
                    {
                        "kind": "manual_overlap",
                        "clip_id": clip.id,
                        "detail": f"「{clip.label or clip.id}」落在装配区间 0~{cursor:.2f}s 里，"
                        "装配不会挪动它，导出时会和铺开的镜头重叠。",
                        "suggestion": "把它移到另一条视频轨，或者删掉它",
                    }
                )

        #: 之前用 `replace=True` 铺过的工程里，拆出去的声音多半已经指不到源片段了。
        #: 对位调和之后片段 id 不再变，所以这里顺手把还能认出来的重新配上——
        #: 靠 `shot_id` 认，认不出来的不动（`source_missing` 照旧如实显示）。
        repaired: list[dict[str, Any]] = []
        for clip in clips:
            if track_kind.get(clip.track_id) != "audio" or not clip.source_clip_id:
                continue
            if clip.source_clip_id in live_ids or not clip.shot_id:
                continue
            target = assembled.get(("video", clip.shot_id))
            if target is not None:
                repaired.append(
                    {"clip_id": clip.id, "to_clip_id": target.id, "shot_id": clip.shot_id}
                )

        return {
            "video_track_id": video_track_id,
            "add": add,
            "update": update,
            "remove": remove,
            "move": move,
            "skipped": skipped,
            "warnings": warnings,
            "repaired": repaired,
            "preserved": manual_count,
            "dub_track_needed": dub_track_needed and dub_track_id is None,
        }

    # --- 编辑命令 ---

    async def move_clip(self, pid: str, clip_id: str, start: float) -> dict[str, Any]:
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        track = await fetch(db, Track, clip.track_id, "轨道")
        siblings = await self._siblings(pid, clip.track_id, clip_id)
        await self._capture(pid)
        if track.kind == "video":
            # 视频轨没有“任意坐标”这件事：横向拖动只决定插到哪个片段前后，
            # 真正的 start 始终由连续顺序重新计算。
            desired = max(0.0, float(start))
            ordered = sorted(siblings, key=lambda row: row.start)
            position = sum(1 for row in ordered if desired >= row.start + row.duration / 2)
            ordered.insert(position, clip)
            await self._write_contiguous_order(pid, clip.track_id, [row.id for row in ordered])
            return await self.get(pid)

        snapped = self._snap_to_neighbours(float(start), siblings)
        self._ensure_free(snapped, clip.duration, siblings, track.name)
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
        start: float | None = None,
        ripple: bool = True,
    ) -> dict[str, Any]:
        """裁剪。

        `start` 是为**拖左边缘**准备的：往右拖左边缘要同时改「从源素材的哪里开始」
        （in_point）和「在时间线上的哪里开始」（start），否则画面会往前跳。这两件事
        必须在一次请求里做完——分两次调不但会在中间留下一个错的状态，还会在撤销栈上
        留两格，撤销一次只回到一半。
        """
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        track = await fetch(db, Track, clip.track_id, "轨道")
        new_in = clip.in_point if in_point is None else max(0.0, float(in_point))
        source_end = clip.out_point if clip.out_point is not None else clip.in_point + clip.duration
        new_out = source_end if out_point is None else float(out_point)
        async with db.read() as session:
            asset = await session.get(Asset, clip.asset_id) if clip.asset_id else None
            version = (
                await session.get(GenerationVersion, clip.version_id) if clip.version_id else None
            )
        source_duration = (
            float(asset.duration)
            if asset is not None and asset.duration is not None
            else float(version.duration)
            if version is not None and version.duration is not None
            else source_end
        )
        if new_in < 0 or new_out > source_duration + 0.001:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "裁切范围超出原视频",
                f"原视频长度 {source_duration:.3f} 秒，收到的裁切范围是 {new_in:.3f}~{new_out:.3f} 秒。",
                ["把前后边界线移回原视频范围内", "需要更长内容时换用更长的素材版本"],
                {"clip_id": clip_id, "source_duration": source_duration},
            )
        if new_out - new_in <= 0:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "裁切后长度为零",
                f"in={new_in} out={new_out}，片段不能没有长度。",
                ["把出点调到入点之后", "或撤销这次裁切"],
                {"clip_id": clip_id},
            )
        new_start = clip.start
        if start is not None:
            new_start = self._snap_to_neighbours(
                float(start), await self._siblings(pid, clip.track_id, clip_id)
            )
        if track.kind == "audio":
            self._ensure_free(
                new_start,
                new_out - new_in,
                await self._siblings(pid, clip.track_id, clip_id),
                track.name,
            )
        await self._capture(pid)
        async with db.write() as session:
            row = await session.get(TimelineClip, clip_id)
            assert row is not None
            row.in_point = new_in
            row.out_point = new_out
            row.duration = new_out - new_in
            row.start = new_start
        if ripple:
            await self._close_gaps(pid, clip.track_id)
        return await self.get(pid)

    async def split_clip(self, pid: str, clip_id: str, at: float) -> dict[str, Any]:
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        track = await fetch(db, Track, clip.track_id, "轨道")
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
                # 混音设置跟着走：切一刀不该让后半段忽然出声或者变响。
                muted=clip.muted,
                volume=clip.volume,
                source_clip_id=clip.source_clip_id,
                #: **切一刀就等于接管这一段**：两半都转成 `manual`，装配从此不再动它们。
                #: 否则两半共用一个 shot_id，下一次装配会把后半段当「多余的装配片段」删掉。
                origin="manual",
            )
            head.duration = offset
            head.out_point = clip.in_point + offset
            head.origin = "manual"
            session.add(tail)
        await self._reindex(pid, clip.track_id)
        return await self.get(pid)

    async def isolate_audio_selection(
        self,
        pid: str,
        clip_id: str,
        *,
        in_point: float,
        out_point: float,
    ) -> dict[str, Any]:
        """把音频裁切线之间的选区独立成一段，选区外内容原样保留。"""
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        track = await fetch(db, Track, clip.track_id, "轨道")
        if track.kind != "audio":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "选中切断只用于音频片段",
                f"{track.name} 不是音频轨。",
                ["在音频轨上拖出选区后再切断"],
            )
        source_in = float(clip.in_point)
        source_out = float(
            clip.out_point if clip.out_point is not None else clip.in_point + clip.duration
        )
        selected_in = float(in_point)
        selected_out = float(out_point)
        if (
            selected_in < source_in - 0.001
            or selected_out > source_out + 0.001
            or selected_out - selected_in < 0.05
        ):
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "音频选区无效",
                f"原片段范围 {source_in:.3f}~{source_out:.3f}，收到 {selected_in:.3f}~{selected_out:.3f}。",
                ["把两条裁切线放在原音频范围内", "选区至少保留 0.05 秒"],
                {"clip_id": clip_id},
            )
        before = max(0.0, selected_in - source_in)
        selected_duration = selected_out - selected_in
        after = max(0.0, source_out - selected_out)
        if before < 0.001 and after < 0.001:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "还没有可切断的边界",
                "当前选区就是完整音频片段。",
                ["先拖动左边或右边的裁切线形成选区"],
            )

        def piece(
            *, piece_id: str, start: float, duration: float, start_in: float, end_out: float
        ) -> TimelineClip:
            return TimelineClip(
                id=piece_id,
                track_id=clip.track_id,
                shot_id=clip.shot_id,
                version_id=clip.version_id,
                asset_id=clip.asset_id,
                index_no=clip.index_no,
                start=start,
                duration=duration,
                in_point=start_in,
                out_point=end_out,
                label=clip.label,
                muted=clip.muted,
                volume=clip.volume,
                source_clip_id=clip.source_clip_id,
            )

        await self._capture(pid)
        async with db.write() as session:
            selected = await session.get(TimelineClip, clip_id)
            assert selected is not None
            selected.start = clip.start + before
            selected.duration = selected_duration
            selected.in_point = selected_in
            selected.out_point = selected_out
            if before >= 0.001:
                session.add(
                    piece(
                        piece_id=new_id("timeline_clip"),
                        start=clip.start,
                        duration=before,
                        start_in=source_in,
                        end_out=selected_in,
                    )
                )
            if after >= 0.001:
                session.add(
                    piece(
                        piece_id=new_id("timeline_clip"),
                        start=clip.start + before + selected_duration,
                        duration=after,
                        start_in=selected_out,
                        end_out=source_out,
                    )
                )
        await self._reindex(pid, clip.track_id)
        return {
            "selected_clip_id": clip_id,
            "segments": 1 + int(before >= 0.001) + int(after >= 0.001),
            "timeline": await self.get(pid),
        }

    async def delete_clip(self, pid: str, clip_id: str, *, ripple: bool = True) -> dict[str, Any]:
        """删一个片段。

        **不连带删拆出去的声音**：声音一旦独立成一段，它就是音频轨上的一段素材，
        用户完全可能是想「换掉这段画面、留住这段声音」。留下来的那段会被标成
        `source_missing`，界面上说清楚它的来源片段已经不在了。
        """
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        track = await fetch(db, Track, clip.track_id, "轨道")
        await self._capture(pid)
        async with db.write() as session:
            fresh = await session.get(TimelineClip, clip_id)
            if fresh is not None:
                await session.delete(fresh)
        if track.kind == "video" or ripple:
            await self._close_gaps(pid, clip.track_id)
        else:
            await self._reindex(pid, clip.track_id)
        return await self.get(pid)

    async def add_clip(
        self,
        pid: str,
        track_id: str,
        asset_id: str,
        *,
        start: float = 0.0,
        duration: float | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """把一个已登记的资产放到轨道上（导入的配乐 / 音效走这条路）。

        长度按「显式给的 → 资产上记的 → ffprobe 探出来的」取，**一个都拿不到就报错**：
        随便填 4 秒会让用户以为音乐只有 4 秒长，那是猜出来的假数据。
        """
        db = db_of(pid)
        track = await fetch(db, Track, track_id, "轨道")
        asset = await fetch(db, Asset, asset_id, "资产")
        proj = project_of(pid)
        src = proj.dir / asset.path
        if not src.is_file():
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "素材文件不在磁盘上",
                f"{asset.path} 找不到，放到轨道上也没法播放或导出。",
                ["重新导入这个文件", "或从备份恢复它"],
                {"asset_id": asset_id},
            )
        probe = await audio_service.peek(src)
        if track.kind == "audio" and probe is not None and not probe.has_audio:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这个文件没有声音",
                f"{asset.path} 里没有音频流，放到音频轨上不会有任何声音。",
                ["选一个音频文件（mp3 / wav / m4a）", "视频的声音请用「拆出声音」而不是直接放"],
                {"asset_id": asset_id, "track_id": track_id},
            )
        length = duration if duration is not None else (asset.duration or None)
        if length is None and probe is not None:
            length = probe.duration
        if not length or length <= 0:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "不知道这段素材有多长",
                f"{asset.path} 上没有记录时长，ffprobe 也没能探出来。",
                [
                    "手动填一个时长再放上去",
                    "或安装 / 配置 FFmpeg 后重试（设置页可以看用的是哪一份）",
                ],
                {"asset_id": asset_id},
            )
        await self._capture(pid)
        row = TimelineClip(
            id=new_id("timeline_clip"),
            track_id=track_id,
            asset_id=asset_id,
            index_no=0,
            start=self._snap_to_neighbours(float(start), await self._siblings(pid, track_id, None)),
            duration=float(length),
            in_point=0.0,
            out_point=float(length),
            label=(label or "").strip() or _asset_label(asset),
        )
        if track.kind == "video":
            siblings = await self._siblings(pid, track_id, None)
            if siblings:
                row.start = max(c.start + c.duration for c in siblings)
        else:
            self._ensure_free(
                row.start,
                row.duration,
                await self._siblings(pid, track_id, None),
                track.name,
            )
        async with db.write() as session:
            session.add(row)
        if track.kind == "video":
            await self._close_gaps(pid, track_id)
        else:
            await self._reindex(pid, track_id)
        return {"clip_id": row.id, "timeline": await self.get(pid)}

    async def add_blank_clip(
        self,
        pid: str,
        track_id: str,
        *,
        start: float = 0.0,
        duration: float = 1.0,
        label: str | None = None,
    ) -> dict[str, Any]:
        """在视频轨放一段可导出的黑场占位片段。"""
        db = db_of(pid)
        track = await fetch(db, Track, track_id, "轨道")
        if track.kind != "video":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "空白片段只能放到视频轨",
                f"{track.name} 不是视频轨。",
                ["选择视频轨再添加空白视频段"],
            )
        if duration <= 0:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "空白片段时长无效",
                "时长必须大于 0 秒。",
                ["填入正数时长"],
            )
        siblings = await self._siblings(pid, track_id, None)
        await self._capture(pid)
        row = TimelineClip(
            id=new_id("timeline_clip"),
            track_id=track_id,
            index_no=0,
            start=max((c.start + c.duration for c in siblings), default=0.0),
            duration=float(duration),
            in_point=0.0,
            out_point=float(duration),
            label=(label or "空白视频段").strip(),
        )
        async with db.write() as session:
            session.add(row)
        await self._close_gaps(pid, track_id)
        return {"clip_id": row.id, "timeline": await self.get(pid)}

    async def resize_blank_clip(self, pid: str, clip_id: str, *, duration: float) -> dict[str, Any]:
        """修改黑场占位的时长；后面的画面随即重新贴紧。"""
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        track = await fetch(db, Track, clip.track_id, "轨道")
        if track.kind != "video" or clip.asset_id or clip.shot_id or clip.version_id:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "只有空白视频段能直接设置时长",
                "普通素材片段请用左右裁切线修改可见范围。",
                ["选中空白视频段后再设置时长"],
            )
        if duration <= 0:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "空白片段时长无效",
                "时长必须大于 0 秒。",
                ["填入正数时长"],
            )
        await self._capture(pid)
        async with db.write() as session:
            row = await session.get(TimelineClip, clip_id)
            assert row is not None
            row.duration = float(duration)
            row.in_point = 0.0
            row.out_point = float(duration)
        await self._close_gaps(pid, clip.track_id)
        return await self.get(pid)

    async def move_clip_to_track(
        self, pid: str, clip_id: str, target_track_id: str
    ) -> dict[str, Any]:
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        source = await fetch(db, Track, clip.track_id, "轨道")
        target = await fetch(db, Track, target_track_id, "目标轨道")
        if source.kind != "audio" or target.kind != "audio":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "只能在音频轨之间移动片段",
                f"{source.name} → {target.name} 不是音频轨移动。",
                ["视频片段请通过视频轨重新排序"],
            )
        self._ensure_free(
            clip.start,
            clip.duration,
            await self._siblings(pid, target_track_id, None),
            target.name,
        )
        await self._capture(pid)
        async with db.write() as session:
            row = await session.get(TimelineClip, clip_id)
            assert row is not None
            row.track_id = target_track_id
        await self._reindex(pid, source.id)
        await self._reindex(pid, target.id)
        return await self.get(pid)

    async def move_clip_to_new_audio_track(self, pid: str, clip_id: str) -> dict[str, Any]:
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        source = await fetch(db, Track, clip.track_id, "轨道")
        if source.kind != "audio":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "只有音频片段能移到新音频轨",
                f"{source.name} 不是音频轨。",
                ["先在音频轨中选中一个片段"],
            )
        timeline = await self.get(pid)
        rows = await fetch_all(db, Track, where=Track.timeline_id == timeline["id"])
        await self._capture(pid)
        target = Track(
            id=new_id("track"),
            timeline_id=timeline["id"],
            kind="audio",
            index_no=max((t.index_no for t in rows), default=-1) + 1,
            name=self._next_name("audio", rows),
        )
        async with db.write() as session:
            session.add(target)
            await session.flush()
            row = await session.get(TimelineClip, clip_id)
            assert row is not None
            row.track_id = target.id
        await self._reindex(pid, source.id)
        await self._reindex(pid, target.id)
        return {"track_id": target.id, "timeline": await self.get(pid)}

    async def detach_audio(self, pid: str, clip_id: str) -> dict[str, Any]:
        """把一段画面的声音拆成音频轨上的独立片段。

        三件事一次做完：拆出音频文件（`services/audio.py`，没有音轨时明确报错）、
        找一条**这个时间段还空着**的音频轨（都占着就新开一条，所以音频天然可以叠）、
        把源片段静音（声音已经挪走了，再让画面自己出一遍就是听两遍）。
        """
        db = db_of(pid)
        clip = await fetch(db, TimelineClip, clip_id, "片段")
        track = await fetch(db, Track, clip.track_id, "轨道")
        if track.kind != "video":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "只有视频轨上的片段能拆声音",
                f"{track.name} 是{'音频轨' if track.kind == 'audio' else track.kind}。",
                ["在视频轨上选一段再拆", "音频片段本身就是独立的，不需要拆"],
                {"clip_id": clip_id},
            )
        if not clip.asset_id:
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "这个片段没有素材",
                "片段上没有关联任何文件，拆不出声音。",
                ["先给这个镜头换一个有素材的版本"],
                {"clip_id": clip_id},
            )
        existing = next(
            (
                c
                for c in await fetch_all(
                    db, TimelineClip, where=TimelineClip.source_clip_id == clip_id
                )
            ),
            None,
        )
        if existing is not None:
            raise AppError(
                ErrorCode.CONFLICT,
                "这段的声音已经拆出来了",
                "音频轨上已经有一段是从它拆出来的，再拆一次会得到两份一样的声音。",
                ["在音频轨上找到那一段直接调整", "或者先删掉那一段再重新拆"],
                {"clip_id": clip_id, "audio_clip_id": existing.id, "track_id": existing.track_id},
            )

        extracted = await audio_service.extract(pid, clip.asset_id)
        timeline = await self.get(pid)
        clip_end = clip.start + clip.duration
        target = next(
            (
                t
                for t in timeline["tracks"]
                if t["kind"] == "audio" and not _occupied(t["clips"], clip.start, clip_end)
            ),
            None,
        )
        created = False
        if target is None:
            # 所有音频轨在这个时间段都占着：新开一条。音频轨之间可以叠加，
            # 「叠加」在数据上就是「同一时间有多条轨道各有一段」。
            target = (await self.add_track(pid, kind="audio"))["track"]
            created = True
        await self._capture(pid)
        row = TimelineClip(
            id=new_id("timeline_clip"),
            track_id=target["id"],
            shot_id=clip.shot_id,
            asset_id=extracted["id"],
            index_no=0,
            start=clip.start,
            duration=clip.duration,
            in_point=clip.in_point,
            out_point=clip.out_point
            if clip.out_point is not None
            else clip.in_point + clip.duration,
            label=f"{clip.label or '片段'} 的声音",
            volume=1.0,
            source_clip_id=clip.id,
        )
        async with db.write() as session:
            session.add(row)
            source = await session.get(TimelineClip, clip_id)
            assert source is not None
            source.muted = 1
        await self._reindex(pid, target["id"])
        log.info("timeline.audio_detached", project_id=pid, clip_id=clip_id, track_id=target["id"])
        return {
            "audio_clip_id": row.id,
            "track_id": target["id"],
            "track_name": target["name"],
            #: 为了放下它新开了一条轨道（原来的音频轨在这个时间段都被占着）。
            "created_track": created,
            #: 之前已经拆过同一段素材，复用了那份音频文件（没有重跑 FFmpeg）。
            "reused_file": bool(extracted.get("reused")),
            "asset_id": extracted["id"],
            "timeline": await self.get(pid),
        }

    async def set_mix(
        self,
        pid: str,
        clip_id: str,
        *,
        muted: bool | None = None,
        volume: float | None = None,
    ) -> dict[str, Any]:
        """调一个片段的静音 / 音量。视频片段与音频片段用同一个入口。"""
        db = db_of(pid)
        await fetch(db, TimelineClip, clip_id, "片段")
        if volume is not None and not 0.0 <= float(volume) <= MAX_VOLUME:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "音量超出范围",
                f"{volume} 不在 0 ~ {MAX_VOLUME} 之间。",
                [f"把音量填在 0（无声）到 {MAX_VOLUME} 之间", "要更响请在导出后用音频软件处理"],
                {"clip_id": clip_id},
            )
        await self._capture(pid)
        async with db.write() as session:
            row = await session.get(TimelineClip, clip_id)
            assert row is not None
            if muted is not None:
                row.muted = int(muted)
            if volume is not None:
                row.volume = float(volume)
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

    async def _siblings(
        self, pid: str, track_id: str, exclude_id: str | None
    ) -> list[TimelineClip]:
        rows = await fetch_all(db_of(pid), TimelineClip, where=TimelineClip.track_id == track_id)
        return [c for c in rows if c.id != exclude_id]

    @staticmethod
    def _ensure_free(
        start: float, duration: float, siblings: list[TimelineClip], track_name: str
    ) -> None:
        end = float(start) + float(duration)
        for other in siblings:
            other_end = float(other.start) + float(other.duration)
            if float(start) < other_end - 0.001 and end > float(other.start) + 0.001:
                raise AppError(
                    ErrorCode.CONFLICT,
                    "同一条音频轨不能重叠",
                    f"目标区间 {start:.3f}~{end:.3f} 与「{other.label or other.id}」重叠。",
                    ["拖到当前轨道的空闲区间", "或把片段移到另一条音频轨"],
                    {"track": track_name, "clip_id": other.id},
                )

    async def _write_contiguous_order(self, pid: str, track_id: str, clip_ids: list[str]) -> None:
        """按给定顺序重写视频轨；视频永远从 0 秒连续铺开。"""
        db = db_of(pid)
        rows = {
            c.id: c
            for c in await fetch_all(db, TimelineClip, where=TimelineClip.track_id == track_id)
        }
        cursor = 0.0
        async with db.write() as session:
            for index, clip_id in enumerate(clip_ids, start=1):
                row = rows.get(clip_id)
                if row is None:
                    continue
                fresh = await session.get(TimelineClip, clip_id)
                if fresh is None:
                    continue
                fresh.start = cursor
                fresh.index_no = index
                cursor += fresh.duration

    def _snap_to_neighbours(self, value: float, siblings: list[TimelineClip]) -> float:
        """吸附到同轨邻居的边界。手拖的坐标永远差那么几毫秒，靠它对齐。"""
        out = max(0.0, float(value))
        for other in siblings:
            for edge in (other.start, other.start + other.duration):
                if abs(out - edge) <= SNAP:
                    return max(0.0, edge)
        return out

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
        """内置副本优先；找不到时 `require` 会带着「怎么拿到」抛出来。"""
        return ffmpeg_tool.require("ffmpeg")

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
        """产出 FFmpeg 命令。用原始素材而不是代理。

        画面来自视频轨；声音有两个来源——**没被静音的视频片段自带的音轨**与
        **音频轨上的片段**，各按自己在时间线上的起点 `adelay` 之后用 `amix` 叠起来
        （`normalize=0`：叠加不该把每一条都自动压小，那样加一条配乐会让对白变轻）。
        一段声音都没有时回到从前的样子（`a=0`、不 map 音频），而不是塞一条静音轨。

        拿不准的事情一律写进 `warnings` 让人看见：ffprobe 不在所以不知道某段画面
        有没有声音、某条音频轨是静音的所以里面的片段不会进成片、音频比画面长会被截断。
        这些都不该拦住导出，但**更不该悄悄发生**。
        """
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
        warnings: list[str] = []
        # 画面用 concat 一段接一段。服务层会保持视频轨连续；旧数据若仍有空档，预检仍会提示。
        gaps, prev_end = 0.0, 0.0
        for clip in clips:
            gaps += max(0.0, clip["start"] - prev_end)
            prev_end = clip["start"] + clip["duration"]
        audio_clips: list[dict[str, Any]] = []
        for track in timeline["tracks"]:
            if track["kind"] != "audio" or not track["clips"]:
                continue
            if track["muted"]:
                warnings.append(
                    f"音频轨 {track['name']} 是静音的，上面 {len(track['clips'])} 段不会进入成片。"
                )
                continue
            audio_clips += [c for c in track["clips"] if not c["muted"] and c["volume"] > 0]
        if gaps > 0.05:
            warnings.append(
                f"视频轨上有 {gaps:.2f} 秒空档，导出会把它们合掉（画面一段接一段）——"
                "要真的留白请在那里放一段黑场素材。"
                + ("音频轨按时间线上的绝对位置放，所以合掉空档后会对不上。" if audio_clips else "")
            )
        missing = [
            c
            for c in [*clips, *audio_clips]
            if c["asset_path"] and not (proj.dir / c["asset_path"]).is_file()
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
        for clip in [*clips, *audio_clips]:
            if clip["asset_path"]:
                args += [
                    "-ss",
                    f"{clip['in_point']:.3f}",
                    "-t",
                    f"{clip['duration']:.3f}",
                    "-i",
                    str(proj.dir / clip["asset_path"]),
                ]
            else:
                args += [
                    "-f",
                    "lavfi",
                    "-t",
                    f"{clip['duration']:.3f}",
                    "-i",
                    f"color=c=black:s={timeline['width']}x{timeline['height']}:r={timeline['fps']}",
                ]
        graph = []
        for i, _ in enumerate(clips):
            graph.append(
                f"[{i}:v]scale={timeline['width']}:{timeline['height']}:force_original_aspect_ratio=decrease,"
                f"pad={timeline['width']}:{timeline['height']}:-1:-1,setsar=1,"
                f"fps={timeline['fps']}[v{i}]"
            )
        concat = "".join(f"[v{i}]" for i in range(len(clips)))
        graph.append(f"{concat}concat=n={len(clips)}:v=1:a=0[vout]")

        # 声音第一路：视频片段自带的音轨。concat 后的时间轴是「一段接一段」，所以延迟
        # 用累计游标算，不是 clip["start"]——视频轨上有空隙时那两个数不一样。
        sources: list[tuple[int, float, float]] = []
        video_muted = bool((video or {}).get("muted"))
        cursor = 0.0
        unknown = 0
        for i, clip in enumerate(clips):
            offset, cursor = cursor, cursor + clip["duration"]
            if video_muted or clip["muted"] or clip["volume"] <= 0 or not clip["asset_path"]:
                continue
            probe = await audio_service.peek(proj.dir / clip["asset_path"])
            if probe is None:
                unknown += 1
                continue
            if probe.has_audio:
                sources.append((i, offset, float(clip["volume"])))
        if unknown:
            warnings.append(
                f"有 {unknown} 段画面无法确认是否自带声音（ffprobe 不可用），"
                "这次按「没有声音」处理。"
            )
        # 声音第二路：音频轨。它们的时间是绝对时间，直接用 start。
        picture_total = cursor
        overrun = 0.0
        for j, clip in enumerate(audio_clips):
            sources.append((len(clips) + j, float(clip["start"]), float(clip["volume"])))
            overrun = max(overrun, clip["start"] + clip["duration"] - picture_total)
        if overrun > 0.05:
            warnings.append(
                f"音频比画面长 {overrun:.2f} 秒，导出会按画面长度截断——"
                "需要留白请在视频轨末尾补一段。"
            )

        aout = ""
        for k, (index, delay, volume) in enumerate(sources):
            graph.append(
                f"[{index}:a]adelay={int(round(delay * 1000))}:all=1,volume={volume:.3f}[a{k}]"
            )
        # 混完（或只有一路时直接）把格式收口成立体声 48k，理由见 EXPORT_AUDIO_FORMAT。
        if len(sources) > 1:
            mix = "".join(f"[a{k}]" for k in range(len(sources)))
            graph.append(f"{mix}amix=inputs={len(sources)}:normalize=0,{EXPORT_AUDIO_FORMAT}[aout]")
            aout = "[aout]"
        elif sources:
            graph.append(f"[a0]{EXPORT_AUDIO_FORMAT}[aout]")
            aout = "[aout]"

        args += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
        if aout:
            # -t 收在画面长度上：混音的每一路都可能比画面长（adelay 之后更是）。
            args += ["-map", aout, "-c:a", "aac", "-b:a", EXPORT_AUDIO_BITRATE]
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18"]
        if aout:
            args += ["-t", f"{picture_total:.3f}"]
        args.append(str(target))
        return {
            "path": str(target),
            "args": args,
            "command": " ".join(args),
            "clips": clips,
            #: 参与混音的音频片段（音频轨上的那些）。视频自带的声音不在这张表里。
            "audio_clips": audio_clips,
            #: 这次导出有哪些「说不准 / 会被丢掉」的地方。空列表是常态。
            "warnings": warnings,
            "version_ids": [c["version_id"] for c in clips if c["version_id"]],
        }

    async def export(self, pid: str, out_path: str | None = None) -> dict[str, Any]:
        try:
            plan = await self.build_command(pid, out_path)
        except AppError as err:
            bus.emit(
                Channel.ERROR,
                "export.preflight_failed",
                err.to_dict(),
                project_id=pid,
            )
            raise
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

    async def open_export_folder(self, pid: str) -> dict[str, str]:
        """在系统文件管理器中打开工程的默认成片目录。"""
        target = project_of(pid).dir / "generations" / "exports"
        target.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            command = ["explorer.exe", str(target)]
        elif sys.platform == "darwin":
            command = ["open", str(target)]
        else:
            command = ["xdg-open", str(target)]
        try:
            await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                "无法打开成片文件夹",
                f"{type(exc).__name__}: {exc}",
                [f"手动打开 {target}", "确认系统文件管理器可以正常启动"],
                {"path": str(target)},
            ) from exc
        bus.emit(
            Channel.SYSTEM,
            "export.folder_opened",
            {"path": str(target)},
            project_id=pid,
        )
        return {"path": str(target)}

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


def _occupied(clips: list[dict[str, Any]], start: float, end: float) -> bool:
    """这条轨道的 [start, end) 区间上已经有片段了吗（用于给拆出来的声音找位置）。"""
    eps = 1e-6
    return any(c["start"] < end - eps and start < c["start"] + c["duration"] - eps for c in clips)


def _asset_label(asset: Asset) -> str:
    """轨道上显示的名字：优先用户导入时那个文件名。

    落盘用的是内容哈希名（`assets.content_name`），把它摆到轨道上等于什么都没说——
    「c72d5eb2b494.m4a」和「片头曲.m4a」是同一个文件，但只有后者能让人认出来。
    原名记在 `meta_json.filename` 里（生成物没有这一项，退回文件名）。
    """
    name = str(load_json(asset.meta_json, {}).get("filename") or "").strip()
    return Path(name).name or Path(asset.path).name


timeline = TimelineService()
