"""Independent frame drafts and explicit, frozen frame-to-video submissions."""

from typing import Any

from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.generation.providers import presets
from app.persistence.models_frames import FrameCandidate, FrameCard, FrameWorkspace
from app.persistence.models_gen import Job
from app.persistence.models_global import GlobalWorkflow
from app.persistence.models_story import Shot
from app.persistence.models_world import Asset
from app.services import route
from app.services.assets import assets
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, load_json, project_of
from app.services.global_registry import global_registry


def invalid(detail: str) -> AppError:
    return AppError(
        ErrorCode.VALIDATION_ERROR, "制帧输入不完整", detail, ["检查画面、提示词和工作流配置后重试"]
    )


class FrameService:
    async def image(self, pid: str, aid: str) -> Asset:
        asset = await fetch(db_of(pid), Asset, aid, "图片")
        if not asset.path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
            raise invalid("画面和设定参考只能使用图片。")
        if not (project_of(pid).dir / asset.path).is_file():
            raise invalid("所选图片文件已丢失。")
        return asset

    async def get(self, pid: str, sid: str) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Shot, sid, "镜头")
        rows = await fetch_all(db, FrameWorkspace, where=FrameWorkspace.shot_id == sid)
        cards = await fetch_all(
            db,
            FrameCard,
            where=FrameCard.shot_id == sid,
            order_by=FrameCard.position,
        )
        cards.sort(key=lambda c: (c.position, c.id))
        candidates = await fetch_all(db, FrameCandidate, order_by=FrameCandidate.id)
        jobs = await fetch_all(db, Job, where=Job.target_kind == "frame_card")
        return {
            "video": load_json(rows[0].video_json, {}) if rows else {},
            "cards": [
                {
                    **as_dict(c),
                    "refs": load_json(c.refs_json, []),
                    "candidates": [
                        {**as_dict(v), "snapshot": load_json(v.snapshot_json, {})}
                        for v in candidates
                        if v.card_id == c.id
                    ],
                    "jobs": [as_dict(j) for j in jobs if j.target_id == c.id],
                }
                for c in cards
            ],
        }

    async def create(self, pid: str, sid: str, data: dict[str, Any]) -> dict[str, Any]:
        await fetch(db_of(pid), Shot, sid, "镜头")
        cards = await fetch_all(db_of(pid), FrameCard, where=FrameCard.shot_id == sid)
        row = FrameCard(
            id=new_id("frame"),
            shot_id=sid,
            position=max((c.position for c in cards), default=-1) + 1,
            title=data["title"],
            prompt=data.get("prompt", ""),
            refs_json="[]",
        )
        async with db_of(pid).write() as session:
            session.add(row)
        return as_dict(row)

    async def update(self, pid: str, cid: str, data: dict[str, Any]) -> dict[str, Any]:
        for aid in data.get("refs", []):
            await self.image(pid, aid)
        async with db_of(pid).write() as session:
            row = await session.get(FrameCard, cid)
            if row is None:
                raise invalid("画面不存在。")
            for key in ("title", "prompt", "position"):
                if key in data:
                    setattr(row, key, data[key])
            if "refs" in data:
                row.refs_json = dump_json(list(dict.fromkeys(data["refs"])))
            return as_dict(row)

    async def add_candidate(
        self, pid: str, cid: str, aid: str, job_id: str | None = None, snapshot: dict | None = None
    ) -> dict[str, Any]:
        card = await fetch(db_of(pid), FrameCard, cid, "画面")
        await self.image(pid, aid)
        row = FrameCandidate(
            id=new_id("candidate"),
            card_id=cid,
            asset_id=aid,
            job_id=job_id,
            snapshot_json=dump_json(snapshot or {"source": "imported"}),
        )
        async with db_of(pid).write() as session:
            session.add(row)
        await assets.link(pid, aid, "shot", card.shot_id, role="frame_workspace")
        return as_dict(row)

    async def adopt(self, pid: str, cid: str, candidate_id: str) -> dict[str, Any]:
        candidate = await fetch(db_of(pid), FrameCandidate, candidate_id, "候选")
        if candidate.card_id != cid:
            raise invalid("候选不属于当前画面。")
        await self.image(pid, candidate.asset_id)
        async with db_of(pid).write() as session:
            card = await session.get(FrameCard, cid)
            if card is None:
                raise invalid("画面不存在。")
            card.adopted_asset_id = candidate.asset_id
            return as_dict(card)

    async def video(
        self, pid: str, sid: str, data: dict[str, Any], *, enqueue: bool = False
    ) -> dict[str, Any]:
        from app.services.generation import generation

        shot = await fetch(db_of(pid), Shot, sid, "镜头")
        frozen = {
            "prompt": data["prompt"],
            "duration": data["duration"],
            "cards": [],
            "preset": data.get("preset", ""),
        }
        if not data["prompt"].strip():
            raise invalid("请填写动作、运镜与声音描述。")
        for key, role in (("first_card_id", "first_frame"), ("last_card_id", "last_frame")):
            cid = data.get(key)
            if not cid:
                if role == "first_frame":
                    raise invalid("请选择首帧画面。")
                continue
            card = await fetch(db_of(pid), FrameCard, cid, "画面")
            if card.shot_id != sid or not card.adopted_asset_id:
                raise invalid("画面必须属于本镜头且已有采用的候选。")
            await self.image(pid, card.adopted_asset_id)
            frozen[role + "_asset_id"] = card.adopted_asset_id
            frozen["cards"].append(
                {"id": cid, "title": card.title, "asset_id": card.adopted_asset_id, "role": role}
            )
        capability = "first_last_frame" if data.get("last_card_id") else "image2video"
        chosen = await route.require(
            pid, capability, preset=data.get("preset") or None, workflow_id=shot.workflow_id
        )
        if chosen.preset:
            points = presets.entry_points(presets.load(chosen.preset))
            if "AIVS_FIRST_FRAME" not in points or (
                data.get("last_card_id") and "AIVS_LAST_FRAME" not in points
            ):
                raise invalid(
                    "所选视频预设缺少真正的首帧或尾帧入口，请更换预设；制帧流程不会降级为普通参考图。"
                )
        elif chosen.workflow_id:
            workflow = await fetch(
                await global_registry.start(), GlobalWorkflow, chosen.workflow_id, "工作流"
            )
            bindings = load_json(workflow.bindings_json, {})
            if "first_frame" not in bindings or (
                data.get("last_card_id") and "last_frame" not in bindings
            ):
                raise invalid("工作流没有绑定本次所需的首尾帧入口。")
        if not enqueue:
            return {
                "inputs": frozen,
                "route": chosen.to_dict(),
                "unused_cards": [
                    c["id"]
                    for c in (await self.get(pid, sid))["cards"]
                    if c["id"] not in {x["id"] for x in frozen["cards"]}
                ],
            }
        return await generation.enqueue_shot(pid, sid, kind=capability, frame_inputs=frozen)


frames = FrameService()
