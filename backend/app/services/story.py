"""剧本、Scene、Shot 与分镜板（Step 5）。

两条路径产出完全相同的数据结构：
  - 手动：create_scene / create_shot，一个字段一个字段填；
  - AI：propose_breakdown 只产出**提案**（不落库），前端逐条 Diff 审阅，
        再用 apply_breakdown 把被接受的条目写进去。

Shot 的 index_no 就是时间顺序，跨 Scene 移动后统一重排，避免出现空号或重号。
"""

from __future__ import annotations

from typing import Any

from app.ai.llm import client as llm
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.persistence.models import utc_now
from app.persistence.models_cast import Appearance, Character
from app.persistence.models_gen import GenerationVersion
from app.persistence.models_story import SHOT_STATUS, Scene, Shot, ShotCast, ShotProp, Story
from app.persistence.models_world import Location, LocationVariant, Prop
from app.services.base import as_dict, db_of, fetch, fetch_all, load_json

SCENE_FIELDS = ("title", "summary", "source_text", "location_variant_id", "time_of_day", "notes")
SHOT_FIELDS = (
    "title",
    "description",
    "duration",
    "camera",
    "movement",
    "status",
    "prompt",
    "negative_prompt",
    "seed",
    "steps",
    "workflow_id",
    "prev_shot_id",
)

BREAKDOWN_SYSTEM = (
    "你是分镜师。把中文剧本拆成 Scene 与 Shot，只返回 JSON 对象，"
    '形如 {"scenes":[{"title":"","summary":"","time_of_day":"",'
    '"shots":[{"title":"","description":"","duration":4,"camera":"","movement":"",'
    '"characters":["角色名"]}]}]}。duration 单位为秒，取 2~8。不要输出解释文字。'
)


def _renumber(rows: list[Shot]) -> list[tuple[str, int]]:
    return [(row.id, i + 1) for i, row in enumerate(rows)]


class StoryService:
    # --- 剧本 ---

    async def get_story(self, pid: str) -> dict[str, Any]:
        db = db_of(pid)
        rows = await fetch_all(db, Story, order_by=Story.created_at)
        if rows:
            return {**as_dict(rows[0]), "llm": llm.status()}
        now = utc_now()
        row = Story(id=new_id("story"), created_at=now, updated_at=now)
        async with db.write() as session:
            session.add(row)
        return {**as_dict(row), "llm": llm.status()}

    async def save_story(self, pid: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = await self.get_story(pid)
        db = db_of(pid)
        async with db.write() as session:
            row = await session.get(Story, current["id"])
            assert row is not None
            for key in ("title", "raw_text", "mode"):
                if key in patch and patch[key] is not None:
                    setattr(row, key, patch[key])
            row.updated_at = utc_now()
        return await self.get_story(pid)

    # --- Scene ---

    async def list_scenes(self, pid: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        scenes = await fetch_all(db, Scene, order_by=Scene.index_no)
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        variants = {v.id: v for v in await fetch_all(db, LocationVariant)}
        locations = {loc.id: loc for loc in await fetch_all(db, Location)}
        out = []
        for scene in scenes:
            variant = variants.get(scene.location_variant_id or "")
            location = locations.get(variant.location_id) if variant else None
            out.append(
                {
                    **as_dict(scene),
                    "shot_count": len([s for s in shots if s.scene_id == scene.id]),
                    "duration_total": sum(s.duration for s in shots if s.scene_id == scene.id),
                    "location_variant_name": (
                        f"{location.name} · {variant.name}" if variant and location else None
                    ),
                }
            )
        return out

    async def create_scene(self, pid: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        title = str(patch.get("title") or "").strip() or "新场景"
        if patch.get("location_variant_id"):
            await fetch(db, LocationVariant, patch["location_variant_id"], "地点变体")
        existing = await fetch_all(db, Scene)
        now = utc_now()
        row = Scene(
            id=new_id("scene"),
            index_no=max((s.index_no for s in existing), default=0) + 1,
            **{k: patch.get(k) for k in SCENE_FIELDS if k != "title"},
            title=title,
            created_at=now,
            updated_at=now,
        )
        async with db.write() as session:
            session.add(row)
        return as_dict(row)

    async def update_scene(self, pid: str, sid: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Scene, sid, "场景")
        if patch.get("location_variant_id"):
            await fetch(db, LocationVariant, patch["location_variant_id"], "地点变体")
        async with db.write() as session:
            row = await session.get(Scene, sid)
            assert row is not None
            for key in SCENE_FIELDS:
                if key in patch:
                    setattr(row, key, patch[key])
            row.updated_at = utc_now()
            return as_dict(row)

    async def delete_scene(self, pid: str, sid: str) -> None:
        db = db_of(pid)
        await fetch(db, Scene, sid, "场景")
        async with db.write() as session:
            fresh = await session.get(Scene, sid)
            if fresh is not None:
                await session.delete(fresh)
        await self._resequence_scenes(pid)

    async def reorder_scenes(self, pid: str, order: list[str]) -> list[dict[str, Any]]:
        db = db_of(pid)
        known = {s.id for s in await fetch_all(db, Scene)}
        unknown = [i for i in order if i not in known]
        if unknown:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "排序里有不存在的场景",
                f"未找到：{'、'.join(unknown[:5])}。",
                ["刷新分镜板后重试"],
            )
        async with db.write() as session:
            for i, sid in enumerate(order):
                row = await session.get(Scene, sid)
                if row is not None:
                    row.index_no = i + 1
        return await self.list_scenes(pid)

    async def _resequence_scenes(self, pid: str) -> None:
        db = db_of(pid)
        rows = await fetch_all(db, Scene, order_by=Scene.index_no)
        async with db.write() as session:
            for sid, num in _renumber(rows):
                row = await session.get(Scene, sid)
                if row is not None:
                    row.index_no = num

    # --- Shot ---

    async def create_shot(self, pid: str, sid: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Scene, sid, "场景")
        status = patch.get("status") or "draft"
        if status not in SHOT_STATUS:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的镜头状态",
                f"{status} 不在 {'、'.join(SHOT_STATUS)} 里。",
                ["用列表中的状态值"],
            )
        existing = await fetch_all(db, Shot)
        now = utc_now()
        fields = {k: patch.get(k) for k in SHOT_FIELDS if k not in ("status", "duration")}
        row = Shot(
            id=new_id("shot"),
            scene_id=sid,
            index_no=max((s.index_no for s in existing), default=0) + 1,
            **fields,
            duration=float(patch.get("duration") or 4.0),
            status=status,
            created_at=now,
            updated_at=now,
        )
        row.title = row.title or ""
        async with db.write() as session:
            session.add(row)
        return as_dict(row)

    async def update_shot(self, pid: str, shot_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Shot, shot_id, "镜头")
        if patch.get("status") and patch["status"] not in SHOT_STATUS:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的镜头状态",
                f"{patch['status']} 不在 {'、'.join(SHOT_STATUS)} 里。",
                ["用列表中的状态值"],
            )
        if patch.get("prev_shot_id"):
            if patch["prev_shot_id"] == shot_id:
                raise AppError(
                    ErrorCode.DEPENDENCY_CYCLE,
                    "镜头不能依赖自己",
                    "上游镜头填成了本镜头。",
                    ["选择另一个镜头作为上游", "或清空上游依赖"],
                    {"shot_id": shot_id},
                )
            await fetch(db, Shot, patch["prev_shot_id"], "上游镜头")
            await self._guard_cycle(pid, shot_id, patch["prev_shot_id"])
        async with db.write() as session:
            row = await session.get(Shot, shot_id)
            assert row is not None
            for key in SHOT_FIELDS:
                if key in patch:
                    setattr(row, key, patch[key])
            if "duration" in patch and patch["duration"] is not None:
                row.duration = float(patch["duration"])
            row.updated_at = utc_now()
        return await self.get_shot(pid, shot_id)

    async def _guard_cycle(self, pid: str, shot_id: str, prev_id: str) -> None:
        db = db_of(pid)
        shots = {s.id: s for s in await fetch_all(db, Shot)}
        seen = {shot_id}
        node = prev_id
        while node:
            if node in seen:
                raise AppError(
                    ErrorCode.DEPENDENCY_CYCLE,
                    "镜头依赖成环",
                    f"沿着上游链又回到了 {node}。",
                    ["改用别的上游镜头", "或先清空链上的某个上游依赖"],
                    {"shot_id": shot_id, "prev_shot_id": prev_id},
                )
            seen.add(node)
            nxt = shots.get(node)
            node = nxt.prev_shot_id if nxt else None

    async def get_shot(self, pid: str, shot_id: str) -> dict[str, Any]:
        db = db_of(pid)
        row = await fetch(db, Shot, shot_id, "镜头")
        scene = await fetch(db, Scene, row.scene_id, "场景")
        cast_rows = await fetch_all(db, ShotCast, where=ShotCast.shot_id == shot_id)
        prop_rows = await fetch_all(db, ShotProp, where=ShotProp.shot_id == shot_id)
        apps = {a.id: a for a in await fetch_all(db, Appearance)}
        chars = {c.id: c for c in await fetch_all(db, Character)}
        props = {p.id: p for p in await fetch_all(db, Prop)}
        versions = await fetch_all(
            db,
            GenerationVersion,
            where=GenerationVersion.shot_id == shot_id,
            order_by=GenerationVersion.version_no.desc(),
        )
        return {
            **as_dict(row),
            "scene_title": scene.title,
            "scene_index_no": scene.index_no,
            "context_overrides": load_json(row.context_overrides_json, []),
            "cast": [
                {
                    **as_dict(c),
                    "appearance_name": apps[c.appearance_id].name
                    if c.appearance_id in apps
                    else None,
                    "character_id": apps[c.appearance_id].character_id
                    if c.appearance_id in apps
                    else None,
                    "character_name": (
                        chars[apps[c.appearance_id].character_id].name
                        if c.appearance_id in apps and apps[c.appearance_id].character_id in chars
                        else None
                    ),
                }
                for c in cast_rows
            ],
            "props": [
                {**as_dict(p), "prop_name": props[p.prop_id].name if p.prop_id in props else None}
                for p in prop_rows
            ],
            "version_count": len(versions),
            "versions": [as_dict(v) for v in versions],
        }

    async def delete_shot(self, pid: str, shot_id: str) -> None:
        db = db_of(pid)
        await fetch(db, Shot, shot_id, "镜头")
        dependents = [s for s in await fetch_all(db, Shot) if s.prev_shot_id == shot_id]
        if dependents:
            raise AppError(
                ErrorCode.CONFLICT,
                "该镜头被其他镜头当作上游",
                f"有 {len(dependents)} 个镜头需要它的末帧。",
                ["先清空那些镜头的上游依赖", "或改指到别的上游镜头"],
                {"shot_id": shot_id, "dependents": [s.id for s in dependents]},
            )
        async with db.write() as session:
            fresh = await session.get(Shot, shot_id)
            if fresh is not None:
                await session.delete(fresh)
        await self.resequence_shots(pid)

    async def move_shot(
        self, pid: str, shot_id: str, scene_id: str, position: int | None = None
    ) -> list[dict[str, Any]]:
        """跨 Scene 拖拽。position 是目标 Scene 内的 0-based 落点，None 表示放到末尾。"""
        db = db_of(pid)
        await fetch(db, Shot, shot_id, "镜头")
        await fetch(db, Scene, scene_id, "场景")
        async with db.write() as session:
            row = await session.get(Shot, shot_id)
            assert row is not None
            row.scene_id = scene_id
            row.updated_at = utc_now()
        siblings = [
            s
            for s in await fetch_all(db, Shot, order_by=Shot.index_no)
            if s.scene_id == scene_id and s.id != shot_id
        ]
        at = len(siblings) if position is None else max(0, min(position, len(siblings)))
        order = [s.id for s in siblings]
        order.insert(at, shot_id)
        await self.reorder_shots(pid, scene_id, order)
        return await self.storyboard(pid)

    async def reorder_shots(self, pid: str, scene_id: str, order: list[str]) -> None:
        db = db_of(pid)
        rows = {s.id: s for s in await fetch_all(db, Shot) if s.scene_id == scene_id}
        unknown = [i for i in order if i not in rows]
        if unknown:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "排序里有不属于该场景的镜头",
                f"未找到：{'、'.join(unknown[:5])}。",
                ["刷新分镜板后重试"],
            )
        async with db.write() as session:
            for i, shot_id in enumerate(order):
                row = await session.get(Shot, shot_id)
                if row is not None:
                    row.index_no = i + 1  # 先做场景内序号，再全局重排
        await self.resequence_shots(pid)

    async def resequence_shots(self, pid: str) -> None:
        """全局重排：先按 Scene 顺序，再按场景内顺序——序号即时间顺序。"""
        db = db_of(pid)
        scenes = await fetch_all(db, Scene, order_by=Scene.index_no)
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        ordered: list[Shot] = []
        for scene in scenes:
            ordered += [s for s in shots if s.scene_id == scene.id]
        async with db.write() as session:
            for shot_id, num in _renumber(ordered):
                row = await session.get(Shot, shot_id)
                if row is not None:
                    row.index_no = num

    # --- 出场表 ---

    async def set_shot_cast(
        self, pid: str, shot_id: str, appearance_ids: list[str]
    ) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Shot, shot_id, "镜头")
        for aid in appearance_ids:
            await fetch(db, Appearance, aid, "形象")
        existing = await fetch_all(db, ShotCast, where=ShotCast.shot_id == shot_id)
        async with db.write() as session:
            for row in existing:
                fresh = await session.get(ShotCast, row.id)
                if fresh is not None:
                    await session.delete(fresh)
            for aid in appearance_ids:
                session.add(ShotCast(id=new_id("shot_cast"), shot_id=shot_id, appearance_id=aid))
        return await self.get_shot(pid, shot_id)

    async def set_shot_props(
        self, pid: str, shot_id: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Shot, shot_id, "镜头")
        for item in items:
            await fetch(db, Prop, str(item.get("prop_id")), "道具")
        existing = await fetch_all(db, ShotProp, where=ShotProp.shot_id == shot_id)
        async with db.write() as session:
            for row in existing:
                fresh = await session.get(ShotProp, row.id)
                if fresh is not None:
                    await session.delete(fresh)
            for item in items:
                session.add(
                    ShotProp(
                        id=new_id("shot_prop"),
                        shot_id=shot_id,
                        prop_id=str(item["prop_id"]),
                        state=str(item.get("state") or "present"),
                    )
                )
        return await self.get_shot(pid, shot_id)

    # --- 分镜板 ---

    async def storyboard(self, pid: str) -> list[dict[str, Any]]:
        """泳道 + 卡片。卡片自带 Context 完备度，黄色感叹号的数据来源就是这里。"""
        db = db_of(pid)
        scenes = await fetch_all(db, Scene, order_by=Scene.index_no)
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        cast_rows = await fetch_all(db, ShotCast)
        versions = {v.id: v for v in await fetch_all(db, GenerationVersion)}
        variants = {v.id: v for v in await fetch_all(db, LocationVariant)}
        apps = {a.id: a for a in await fetch_all(db, Appearance)}
        chars = {c.id: c for c in await fetch_all(db, Character)}

        lanes = []
        for scene in scenes:
            cards = []
            for shot in [s for s in shots if s.scene_id == scene.id]:
                names = [
                    chars[apps[c.appearance_id].character_id].name
                    for c in cast_rows
                    if c.shot_id == shot.id
                    and c.appearance_id in apps
                    and apps[c.appearance_id].character_id in chars
                ]
                issues = []
                # 转场镜头不过上下文门槛（它没有出场角色也不需要地点变体），
                # 所以那几条对它不算问题——列出来只会变成永远消不掉的黄色感叹号。
                if shot.kind != "transition":
                    if not scene.location_variant_id or scene.location_variant_id not in variants:
                        issues.append("缺少地点变体，Context 不完整")
                    if not names:
                        issues.append("没有出场角色")
                    if not (shot.prompt or shot.description):
                        issues.append("没有 prompt 也没有画面描述")
                current = versions.get(shot.current_version_id or "")
                cards.append(
                    {
                        "id": shot.id,
                        "index_no": shot.index_no,
                        "title": shot.title,
                        # 转场是系统按衔接补出来的，界面上要能和导演排的戏区分开
                        "kind": shot.kind,
                        "duration": shot.duration,
                        "status": shot.status,
                        "camera": shot.camera,
                        "cast_names": names,
                        "thumbnail_asset_id": current.asset_id if current else None,
                        "version_count": len(
                            [v for v in versions.values() if v.shot_id == shot.id]
                        ),
                        "context_ok": not issues,
                        "context_issues": issues,
                    }
                )
            lanes.append(
                {
                    "id": scene.id,
                    "index_no": scene.index_no,
                    "title": scene.title,
                    "location_variant_id": scene.location_variant_id,
                    "shots": cards,
                }
            )
        return lanes

    # --- AI 拆解（可选） ---

    async def propose_breakdown(self, pid: str, text: str | None = None) -> dict[str, Any]:
        """产出提案，**不落库**。每条都带 op=add，供前端逐条 Diff 审阅。"""
        story = await self.get_story(pid)
        raw = (text if text is not None else story["raw_text"]) or ""
        if not raw.strip():
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "剧本是空的",
                "没有可拆解的文本。",
                ["先把剧本粘贴进左栏", "或直接用「手动添加 Scene」"],
            )
        data = await llm.complete_json(BREAKDOWN_SYSTEM, raw)
        scenes_raw = data.get("scenes")
        if not isinstance(scenes_raw, list) or not scenes_raw:
            raise AppError(
                ErrorCode.LLM_INVALID_OUTPUT,
                "拆解结果里没有场景",
                f"返回的键是：{'、'.join(map(str, data))}。",
                ["重试一次", "或改用手动拆解"],
            )
        db = db_of(pid)
        characters = await fetch_all(db, Character)
        proposal = []
        for si, scene in enumerate(scenes_raw):
            if not isinstance(scene, dict):
                continue
            shots_raw = scene.get("shots") if isinstance(scene.get("shots"), list) else []
            proposal.append(
                {
                    "op": "add",
                    "temp_id": f"s{si + 1}",
                    "title": str(scene.get("title") or f"场景 {si + 1}"),
                    "summary": scene.get("summary"),
                    "time_of_day": scene.get("time_of_day"),
                    "shots": [
                        {
                            "op": "add",
                            "temp_id": f"s{si + 1}h{hi + 1}",
                            "title": str(shot.get("title") or f"镜头 {hi + 1}"),
                            "description": shot.get("description"),
                            "duration": float(shot.get("duration") or 4.0),
                            "camera": shot.get("camera"),
                            "movement": shot.get("movement"),
                            "characters": [str(c) for c in (shot.get("characters") or [])],
                        }
                        for hi, shot in enumerate(shots_raw)
                        if isinstance(shot, dict)
                    ],
                }
            )
        names = {n for scene in proposal for shot in scene["shots"] for n in shot["characters"]}
        return {
            "scenes": proposal,
            "scene_count": len(proposal),
            "shot_count": sum(len(s["shots"]) for s in proposal),
            "character_mapping": [
                self._match_character(name, characters) for name in sorted(names)
            ],
            "note": "以上为提案，尚未写入数据库；逐条审阅后调用「落库」才会生效。",
        }

    def _match_character(self, name: str, characters: list[Character]) -> dict[str, Any]:
        """把文本里的名字对到已有角色，避免凭空多出一个重复的人。"""
        for c in characters:
            if c.name == name or (c.alias or "") == name:
                return {"name": name, "match_id": c.id, "match_name": c.name, "confidence": "exact"}
        for c in characters:
            if name and (name in c.name or c.name in name or name in (c.alias or "")):
                return {
                    "name": name,
                    "match_id": c.id,
                    "match_name": c.name,
                    "confidence": "fuzzy",
                }
        return {"name": name, "match_id": None, "match_name": None, "confidence": "none"}

    async def apply_breakdown(self, pid: str, scenes: list[dict[str, Any]]) -> dict[str, Any]:
        """把审阅通过的条目落库。只接受 op != 'reject' 的条目。"""
        created_scenes, created_shots = 0, 0
        for scene in scenes:
            if scene.get("op") == "reject":
                continue
            row = await self.create_scene(
                pid,
                {
                    "title": scene.get("title"),
                    "summary": scene.get("summary"),
                    "time_of_day": scene.get("time_of_day"),
                    "source_text": scene.get("source_text"),
                    "location_variant_id": scene.get("location_variant_id"),
                },
            )
            created_scenes += 1
            for shot in scene.get("shots") or []:
                if shot.get("op") == "reject":
                    continue
                made = await self.create_shot(
                    pid,
                    row["id"],
                    {
                        "title": shot.get("title"),
                        "description": shot.get("description"),
                        "duration": shot.get("duration"),
                        "camera": shot.get("camera"),
                        "movement": shot.get("movement"),
                        "prompt": shot.get("prompt"),
                    },
                )
                created_shots += 1
                if shot.get("appearance_ids"):
                    await self.set_shot_cast(pid, made["id"], list(shot["appearance_ids"]))
        await self.save_story(pid, {"mode": "ai_assisted"})
        return {"scenes_created": created_scenes, "shots_created": created_shots}


story = StoryService()
