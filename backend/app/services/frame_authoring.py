"""Independent frame drafts and explicit, frozen frame-to-video submissions."""

from typing import Any

from app.ai import prompts
from app.ai.frame_plan import FramePlanDraft
from app.ai.llm import client as llm
from app.ai.skills.frame_video import frame_video_render
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
    async def reference_materials(self, pid: str) -> dict[str, Any]:
        from app.persistence.models_cast import Appearance, Character, SheetVersion
        from app.persistence.models_world import (
            Location,
            LocationReference,
            LocationVariant,
            Prop,
            PropReference,
        )
        from app.services.images import images

        db = db_of(pid)
        asset_rows = {a.id: a for a in await fetch_all(db, Asset)}

        def entry(kind: str, entity_id: str, label: str, aid: str | None) -> dict:
            asset = asset_rows.get(aid)
            available = bool(
                asset
                and asset.path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
                and (project_of(pid).dir / asset.path).is_file()
            )
            return {
                "kind": kind,
                "id": entity_id,
                "label": label,
                "asset_id": aid,
                "available": available,
            }

        characters = {c.id: c for c in await fetch_all(db, Character)}
        sheets = {
            s.appearance_id: s.asset_id for s in await fetch_all(db, SheetVersion) if s.is_current
        }
        character_refs = [
            entry(
                "character", a.id, f"{characters[a.character_id].name} · {a.name}", sheets.get(a.id)
            )
            for a in await fetch_all(db, Appearance, order_by=Appearance.created_at)
            if a.character_id in characters
        ]
        locations = {loc.id: loc for loc in await fetch_all(db, Location)}
        location_images = {
            r.variant_id: r.asset_id
            for r in await fetch_all(db, LocationReference, order_by=LocationReference.created_at)
            if r.is_current
        }
        location_refs = [
            entry(
                "location",
                v.id,
                f"{locations[v.location_id].name} · {v.name}",
                location_images.get(v.id),
            )
            for v in await fetch_all(db, LocationVariant, order_by=LocationVariant.created_at)
            if v.location_id in locations
        ]
        prop_images = {
            r.prop_id: r.asset_id for r in await fetch_all(db, PropReference) if r.is_current
        }
        prop_refs = [
            entry("prop", p.id, p.name, prop_images.get(p.id))
            for p in await fetch_all(db, Prop, order_by=Prop.created_at)
        ]
        return {
            "groups": {"character": character_refs, "location": location_refs, "prop": prop_refs},
            "provider": images.capability(),
        }

    @staticmethod
    def empty_plan(workspace: dict[str, Any]) -> bool:
        return not any(
            workspace["video"].get(k) for k in ("prompt", "first_card_id", "last_card_id")
        ) and all(
            not any(
                card.get(k)
                for k in ("prompt", "story", "refs", "candidates", "jobs", "adopted_asset_id")
            )
            for card in workspace["cards"]
        )

    async def propose_plan(self, pid: str, sid: str) -> dict[str, Any]:
        from app.services.story import story

        llm.require_configured()
        shot = await story.get_shot(pid, sid)
        workspace = await self.get(pid, sid)
        if not self.empty_plan(workspace):
            raise invalid("本镜头已有制作内容，请直接编辑，不能用新计划覆盖。")
        slots = workspace["cards"]
        if len(slots) > 8:
            raise invalid("AI 单镜规划最多八个画面；更多画面可手动填写或拆成多个镜头。")
        repair = ""
        for _ in range(3):
            data = await llm.complete_json(
                "只规划当前已有镜头的制帧方案，忠实保留剧情、人物与台词，不新增镜头。"
                "已有空白画面位置时，保持其数量、顺序与名称，为每个位置规划剧情与图片提示词。"
                "仅返回符合 schema 的 JSON。\n" + frame_video_render(),
                dump_json(
                    {
                        "shot": shot,
                        "frame_slots": [{"title": c["title"]} for c in slots],
                        "schema": FramePlanDraft.model_json_schema(),
                        "repair": repair,
                    }
                ),
            )
            try:
                plan = FramePlanDraft.model_validate(data)
                if slots and [f.title for f in plan.frames] != [c["title"] for c in slots]:
                    raise ValueError("保持已有空白画面的数量、顺序和名称，不要增删画面位置")
                return {
                    **plan.model_dump(),
                    "duration": shot["duration"],
                    "shot_updated_at": shot["updated_at"],
                    "frame_card_ids": [card["id"] for card in slots],
                }
            except ValueError as exc:
                repair = str(exc)[:1800]
        raise invalid(f"制帧计划三次未通过检查：{repair}")

    async def apply_plan(self, pid: str, sid: str, data: dict[str, Any]) -> dict[str, Any]:
        plan = FramePlanDraft.model_validate(data)
        async with db_of(pid).atomic():
            shot = await fetch(db_of(pid), Shot, sid, "镜头")
            workspace = await self.get(pid, sid)
            if shot.updated_at != data["shot_updated_at"]:
                raise invalid("镜头剧情已被修改，请重新规划后采用。")
            if not self.empty_plan(workspace):
                raise invalid("本镜头已有制帧草稿，请直接编辑，不能用新计划覆盖。")
            slots = workspace["cards"]
            if [c["id"] for c in slots] != data.get("frame_card_ids", []):
                raise invalid("画面位置已变化，请重新规划后采用。")
            if slots:
                if len(slots) != len(plan.frames):
                    raise invalid("计划必须完整覆盖现有空白画面位置。")
                cards = [
                    await self.update(pid, card["id"], frame.model_dump(exclude={"title"}))
                    for card, frame in zip(slots, plan.frames, strict=True)
                ]
            else:
                cards = [await self.create(pid, sid, frame.model_dump()) for frame in plan.frames]
            prompt, _ = prompts.with_shot_audio_policy(plan.video_prompt, "")
            await self.save_video(
                pid,
                sid,
                {
                    "prompt": prompt,
                    "duration": shot.duration,
                    "preset": workspace["video"].get("preset", ""),
                    "first_card_id": cards[0]["id"],
                    "last_card_id": cards[-1]["id"] if cards[-1]["role"] == "last" else "",
                },
            )
        return await self.get(pid, sid)

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
        video = load_json(rows[0].video_json, {}) if rows else {}
        shot_rows = {s.id: s for s in await fetch_all(db, Shot)}
        continuity = {}
        for candidate in candidates:
            snapshot = load_json(candidate.snapshot_json, {})
            if snapshot.get("source") == "video_continuity":
                source = shot_rows.get(snapshot.get("source_shot_id"))
                continuity[candidate.id] = {
                    **snapshot,
                    "stale": not source
                    or source.current_version_id != snapshot.get("source_version_id"),
                }

        # 默认跟随本镜头的画面；只读解析，不写回草稿，也不自动采用候选。
        if not video.get("first_card_id"):
            first = next((c for c in cards if c.role == "first"), None) or next(
                (c for c in cards if c.adopted_asset_id), None
            )
            if first is not None and first.adopted_asset_id:
                video["first_card_id"] = first.id
        # 已明确选择「仅使用首帧」的空串必须保留。
        if "last_card_id" not in video:
            last = next(
                (c for c in reversed(cards) if c.role == "last" and c.adopted_asset_id), None
            )
            if last is not None:
                video["last_card_id"] = last.id
        return {
            "video": video,
            "cards": [
                {
                    **as_dict(c),
                    "refs": load_json(c.refs_json, []),
                    "candidates": [
                        {
                            **as_dict(v),
                            "snapshot": load_json(v.snapshot_json, {}),
                            "continuity": continuity.get(v.id),
                        }
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
            story=data.get("story", ""),
            role=data.get("role", "key"),
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
            for key in ("title", "prompt", "story", "role", "position"):
                if key in data:
                    setattr(row, key, data[key])
            if "refs" in data:
                row.refs_json = dump_json(list(dict.fromkeys(data["refs"])))
            return as_dict(row)

    async def save_video(self, pid: str, sid: str, data: dict[str, Any]) -> dict[str, Any]:
        await fetch(db_of(pid), Shot, sid, "镜头")
        async with db_of(pid).write() as session:
            row = await session.get(FrameWorkspace, sid)
            if row is None:
                row = FrameWorkspace(shot_id=sid)
                session.add(row)
            row.video_json = dump_json(data)
        return data

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
        origin = load_json(candidate.snapshot_json, {})
        if origin.get("source") == "video_continuity":
            source = await fetch(db_of(pid), Shot, origin["source_shot_id"], "来源镜头")
            if source.current_version_id != origin["source_version_id"]:
                raise invalid("来源视频已改选，请重新提取衔接画面后采用。")
        await self.image(pid, candidate.asset_id)
        async with db_of(pid).write() as session:
            card = await session.get(FrameCard, cid)
            if card is None:
                raise invalid("画面不存在。")
            card.adopted_asset_id = candidate.asset_id
            if origin.get("source") == "video_continuity" and origin.get("use") == "opening":
                workspace = await session.get(FrameWorkspace, card.shot_id)
                if workspace is None:
                    workspace = FrameWorkspace(shot_id=card.shot_id)
                    session.add(workspace)
                video = load_json(workspace.video_json, {})
                video["first_card_id"] = cid
                workspace.video_json = dump_json(video)
            return as_dict(card)

    async def continuity_source(self, pid: str, cid: str, data: dict[str, Any]):
        """校验已采用的真实成片和抽帧位置；读操作不执行 FFmpeg。"""
        import math

        from app.domain.frames import FrameContinuityInput
        from app.persistence.models_gen import GenerationVersion
        from app.services.frames import tail_at

        body = FrameContinuityInput.model_validate(data)
        card = await fetch(db_of(pid), FrameCard, cid, "目标画面")
        version = await fetch(db_of(pid), GenerationVersion, body.source_version_id, "来源版本")
        source = await fetch(db_of(pid), Shot, version.shot_id, "来源镜头")
        if source.id == card.shot_id:
            raise invalid("请选择另一个镜头的已采用视频作为衔接来源。")
        if source.current_version_id != version.id:
            raise invalid("来源视频已不是当前采用版本，请重新选择。")
        if version.kind != "video" or version.status != "done" or not version.asset_id:
            raise invalid("请先生成并采用来源镜头的视频。")
        at = body.at
        if at == "end":
            at = tail_at(version.out_point, version.in_point)
        else:
            low = float(version.in_point or 0)
            high = version.out_point if version.out_point is not None else version.duration
            if not math.isfinite(at) or at < low or high is None or at >= high:
                raise invalid("抽帧时间必须位于已采用视频的有效区间内。")
        return card, version, at, body.use

    async def extract_continuity(self, pid: str, cid: str, data: dict[str, Any]):
        """将已采用成片的实际画面追加为候选或构图参考，保留来源快照。"""
        from app.services.frames import frames as extractor

        card, version, at, use = await self.continuity_source(pid, cid, data)
        extracted = await extractor.extract(pid, version.asset_id, at)
        async with db_of(pid).atomic():
            # 抽帧期间上游可能改选，写入前再次核验。
            await self.continuity_source(pid, cid, data)
            snapshot = {
                "source": "video_continuity",
                "source_shot_id": version.shot_id,
                "source_version_id": version.id,
                "source_asset_id": version.asset_id,
                "at": at,
                "use": use,
                "requires_trim": data.get("at", "end") != "end",
            }
            row = await self.add_candidate(pid, cid, extracted["id"], snapshot=snapshot)
            await assets.link(
                pid, version.asset_id, "shot", card.shot_id, role="frame_continuity_source"
            )
            if use == "reference":
                current = await fetch(db_of(pid), FrameCard, cid, "目标画面")
                await self.update(
                    pid,
                    cid,
                    {
                        "refs": list(
                            dict.fromkeys([extracted["id"], *load_json(current.refs_json, [])])
                        )
                    },
                )
        return {
            "candidate": row,
            "asset_id": extracted["id"],
            "continuity": snapshot,
            "message": "已添加实际视频画面；请审阅后采用为开场。"
            if use == "opening"
            else "已添加为首位参考图，请按下一镜剧情重新构图。",
        }

    async def optimize_video(self, pid: str, sid: str, data: dict[str, Any]) -> dict[str, Any]:
        """只返回待审阅提示词，不改写制作草稿。"""
        from app.ai import skills

        llm.require_configured()
        shot = await fetch(db_of(pid), Shot, sid, "镜头")
        workspace = await self.get(pid, sid)
        selected = []
        for key in ("first_card_id", "last_card_id"):
            cid = data.get(key)
            if not cid:
                if key == "first_card_id":
                    raise invalid("请先选择首帧画面。")
                continue
            card = next((c for c in workspace["cards"] if c["id"] == cid), None)
            if not card:
                raise invalid("选择的画面不属于当前镜头。")
            selected.append({"role": key, "story": card["story"], "prompt": card["prompt"]})
        result = await llm.complete_json(
            "按所附镜头制作 Skill 优化制帧视频提示词。只返回 JSON 对象，字段 prompt。"
            "输出三个完整 H3 核心字段，不输出图片对齐首行（程序按真实输入补齐）。"
            "图片外观描述仅保留必要锚点，重点写动作推进、运镜和如何落到尾帧。"
            "Picture 1 为首帧，Picture 2 为可选尾帧；无尾帧禁止引用 Picture 2。"
            "默认一个连续 [Shot 1]，只有原意明确切镜才增加带切换时间的 [Shot 2]。"
            "编号从 1 开始，切换时间严格递增且小于总时长。保留对白原文，不编台词。"
            "正文英文，环境声独立填写，无配乐填写 non_diegetic_music: N/A。"
            "只根据给出的画面剧情与图片描述规划，不宣称已观察图片像素。\n"
            + skills.render("h3-base"),
            dump_json(
                {
                    "story": shot.description,
                    "duration": data["duration"],
                    "frames": selected,
                    "draft": data["prompt"],
                }
            ),
        )
        prompt = str(result.get("prompt") or "").strip()
        if not all(
            field + ":" in prompt
            for field in (
                "integrated_multimodal_description",
                "overall_soundscape",
                "non_diegetic_music",
            )
        ):
            raise invalid("优化结果缺少完整视频字段，请重试或手动编辑提示词。")
        import re

        numbers = [int(n) for n in re.findall(r"\[Shot (\d+)\]", prompt)]
        cuts = re.findall(r"\[Shot [2-9]\d*\]\s+At (\d{2}):(\d{2}\.\d{3})", prompt)
        times = [int(m) * 60 + float(sec) for m, sec in cuts]
        if (
            not numbers
            or numbers != list(range(1, len(numbers) + 1))
            or len(times) != len(numbers) - 1
            or any(t <= 0 or t >= data["duration"] for t in times)
            or times != sorted(set(times))
        ):
            raise invalid("优化结果的镜头编号或切换时间不合格，请重试或手动编辑。")
        refs = [int(n) for n in re.findall(r"Picture (\d+)", prompt)]
        if any(n < 1 or n > len(selected) for n in refs):
            raise invalid("优化结果引用了未选择的画面，请重试或手动编辑。")
        return {"prompt": prompt}

    async def video(
        self, pid: str, sid: str, data: dict[str, Any], *, enqueue: bool = False
    ) -> dict[str, Any]:
        from app.services.generation import generation

        shot = await fetch(db_of(pid), Shot, sid, "镜头")
        workspace = await self.get(pid, sid)
        data = {"prompt": "", "duration": shot.duration, **workspace["video"], **data}
        if not data.get("first_card_id"):
            data["first_card_id"] = workspace["video"].get("first_card_id", "")
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
            candidates = await fetch_all(
                db_of(pid),
                FrameCandidate,
                where=FrameCandidate.card_id == card.id,
                order_by=FrameCandidate.created_at,
            )
            provenance = [
                load_json(c.snapshot_json, {})
                for c in candidates
                if c.asset_id == card.adopted_asset_id
            ][-1:]
            for origin in provenance:
                if origin.get("source") == "video_continuity":
                    source = await fetch(db_of(pid), Shot, origin["source_shot_id"], "来源镜头")
                    if source.current_version_id != origin["source_version_id"]:
                        raise invalid("衔接画面的来源视频已改选，请重新抽帧并审阅后再生成。")
            frozen[role + "_asset_id"] = card.adopted_asset_id
            frozen["cards"].append(
                {
                    "id": cid,
                    "title": card.title,
                    "asset_id": card.adopted_asset_id,
                    "role": role,
                    "provenance": provenance,
                }
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
            from app.generation.providers.base import Picture, PictureBook, VideoRequest
            from app.generation.renderers import render_prompt

            book = PictureBook(
                items=[Picture(index=i + 1, role=c["role"]) for i, c in enumerate(frozen["cards"])]
            )
            preview = render_prompt(
                chosen.skill,
                VideoRequest(
                    mode=capability,
                    prompt=data["prompt"],
                    duration=data["duration"],
                    extra={"frame_pipeline": True},
                ),
                book,
            )
            return {
                "prompt_preview": preview,
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
