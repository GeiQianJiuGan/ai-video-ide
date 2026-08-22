"""剧本、Scene、Shot 与分镜板（Step 5）。

两条路径产出完全相同的数据结构：
  - 手动：create_scene / create_shot，一个字段一个字段填；
  - AI：propose_breakdown 只产出**提案**（不落库），前端逐条 Diff 审阅，
        再用 apply_breakdown 把被接受的条目写进去。

Shot 的 index_no 就是时间顺序，跨 Scene 移动后统一重排，避免出现空号或重号。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.ai import prompts
from app.ai.llm import client as llm
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.persistence.models import utc_now
from app.persistence.models_cast import Appearance, Character, SheetVersion
from app.persistence.models_gen import GenerationVersion
from app.persistence.models_story import (
    SHOT_STATUS,
    Scene,
    SceneCast,
    SceneLocation,
    Shot,
    ShotCast,
    ShotProp,
    Story,
)
from app.persistence.models_world import (
    Asset,
    Location,
    LocationReference,
    LocationVariant,
    Prop,
)
from app.services.assets import kind_of_suffix
from app.services.base import as_dict, db_of, fetch, fetch_all, load_json
from app.services.frames import frames, start_frame_index

SCENE_FIELDS = (
    "title",
    "summary",
    "source_text",
    "prompt",
    "location_variant_id",
    "time_of_day",
    "notes",
)
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


def _renumber(rows: list[Shot]) -> list[tuple[str, int]]:
    return [(row.id, i + 1) for i, row in enumerate(rows)]


#: 上限的改法只写一遍，错误建议里直接引它——用户看到的和设置页里的是同一个键。
LIMIT_HINT = "上限可改：设置页「幕（流程图节点）」→「一幕里人物 / 地点的上限」（scene.node_limit）"


def node_limit() -> int:
    """一幕里人物 / 地点各自的上限。可配置：settings.json → 环境变量 → 默认 9。"""
    return max(1, int(settings.scene_node_limit))


def _unique(ids: list[str]) -> list[str]:
    """去重但保留顺序——第一条是主地点，顺序有意义，不能用 set。"""
    seen: set[str] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


def _guard_node_limit(what: str, ids: list[str]) -> None:
    limit = node_limit()
    if len(ids) > limit:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"这一幕的{what}超过上限",
            f"选了 {len(ids)} 个，当前上限是 {limit} 个。",
            [f"最多留 {limit} 个，先去掉多出来的 {len(ids) - limit} 个", LIMIT_HINT],
            {"limit": limit, "count": len(ids)},
        )


def _image_path(asset_id: str | None, assets: dict[str, Asset]) -> str | None:
    """asset id → 相对路径，**只认图片**。

    小节点上那张缩略图是给人挑东西用的，所以按后缀过一遍：把 `.mp4` 喂给 `<img>`
    就是之前那个坏图的来源（同 `sequence._video_of` 的教训）。文件登记丢了就当没有图——
    缺图不是错误，界面显示「无图」照样能挂这个人物 / 地点。
    """
    asset = assets.get(asset_id or "")
    if asset is None or kind_of_suffix(Path(asset.path).suffix) != "image":
        return None
    return asset.path


def _shot_media(
    shot: Shot,
    versions: dict[str, GenerationVersion],
    assets: dict[str, Asset],
    posters: dict[str, Asset],
) -> dict[str, Any]:
    """一张卡片上「能播的那一段」与「能当图显示的那一张」。

    以前这里只回一个 `thumbnail_asset_id = 当前版本的资产`——而当前版本几乎总是
    一段 `.mp4`，前端把它塞进 `<img>` 就是「分镜里截取的首帧加载失败」的真正原因。
    现在两件事分开：视频永远走 `video_path`，缩略图**只认图片**。

    缩略图的挑选顺序 **抽出来的真首帧 → 该镜头生成出的图片版本**；两样都没有而
    确实有视频时回 `poster_pending=true`，前端据它去调
    `POST /storyboard/posters` 补抽——读路径自己绝不起 FFmpeg。
    """
    mine = [
        v
        for v in versions.values()
        if v.shot_id == shot.id and v.status == "done" and v.asset_id and v.asset_id in assets
    ]
    mine.sort(key=lambda v: v.version_no)

    def bucket(version: GenerationVersion) -> str:
        return kind_of_suffix(Path(assets[version.asset_id or ""].path).suffix)

    videos = [v for v in mine if bucket(v) == "video"]
    images = [v for v in mine if bucket(v) == "image"]

    current = versions.get(shot.current_version_id or "")
    picked = next((v for v in videos if current and v.id == current.id), None) or (
        videos[-1] if videos else None
    )
    video_asset = assets.get(picked.asset_id or "") if picked else None

    poster = posters.get(video_asset.id) if video_asset else None
    poster_path = poster.path if poster else None
    if poster_path is None:
        chosen = next((v for v in images if current and v.id == current.id), None) or (
            images[-1] if images else None
        )
        poster = assets.get(chosen.asset_id or "") if chosen else None
        poster_path = poster.path if poster else None
    return {
        "thumbnail_asset_id": poster.id if poster else None,
        "thumbnail_path": poster_path,
        "video_version_id": picked.id if picked else None,
        "video_asset_id": video_asset.id if video_asset else None,
        "video_path": video_asset.path if video_asset else None,
        # 有片子但还没有能当图显示的那一张：可以补抽，不是错误
        "poster_pending": bool(video_asset) and poster_path is None,
    }


def _sheet_thumbs(sheets: list[SheetVersion], assets: dict[str, Asset]) -> dict[str, str]:
    """appearance_id → 角色表缩略图。

    当前版本优先，当前那版没有图（占位版本）时退到最新一个有图的版本：
    挑人的时候看见一张旧图，比看见「无图」有用得多。
    """
    out: dict[str, str] = {}
    for row in sorted(sheets, key=lambda s: (s.is_current, s.version_no)):
        path = _image_path(row.asset_id, assets)
        if path:
            out[row.appearance_id] = path
    return out


def _variant_thumbs(refs: list[LocationReference], assets: dict[str, Asset]) -> dict[str, str]:
    """location_variant_id → 参考图缩略图。当前那张优先，其次最后登记的一张。"""
    out: dict[str, str] = {}
    for row in sorted(refs, key=lambda r: (r.is_current, r.created_at)):
        path = _image_path(row.asset_id, assets)
        if path:
            out[row.variant_id] = path
    return out


def _scene_nodes(
    scene: Scene,
    cast_rows: list[SceneCast],
    loc_rows: list[SceneLocation],
    apps: dict[str, Appearance],
    chars: dict[str, Character],
    variants: dict[str, LocationVariant],
    locations: dict[str, Location],
    cast_thumbs: dict[str, str],
    loc_thumbs: dict[str, str],
) -> dict[str, Any]:
    """一幕的小节点投影：名字与缩略图在后端拼好，前端不再自己查三张表。"""
    cast: list[dict[str, Any]] = []
    for row in cast_rows:
        appearance = apps.get(row.appearance_id)
        character = chars.get(appearance.character_id) if appearance else None
        cast.append(
            {
                "id": row.id,
                "appearance_id": row.appearance_id,
                "index_no": row.index_no,
                "appearance_name": appearance.name if appearance else None,
                "character_id": appearance.character_id if appearance else None,
                "character_name": character.name if character else None,
                "label": (
                    f"{character.name} · {appearance.name}"
                    if appearance and character
                    else (appearance.name if appearance else row.appearance_id)
                ),
                # 角色表的当前版本；只会是图片，节点上直接当 <img src> 用
                "thumbnail_path": cast_thumbs.get(row.appearance_id),
            }
        )
    locs: list[dict[str, Any]] = []
    for row in loc_rows:
        variant = variants.get(row.location_variant_id)
        location = locations.get(variant.location_id) if variant else None
        locs.append(
            {
                "id": row.id,
                "location_variant_id": row.location_variant_id,
                "index_no": row.index_no,
                "variant_name": variant.name if variant else None,
                "label": (
                    f"{location.name} · {variant.name}"
                    if variant and location
                    else (variant.name if variant else row.location_variant_id)
                ),
                # 主地点 = 同步回 scene.location_variant_id 的那一条（列表里的第一条）
                "is_primary": row.location_variant_id == scene.location_variant_id,
                "thumbnail_path": loc_thumbs.get(row.location_variant_id),
            }
        )
    return {
        "cast": cast,
        "cast_names": _unique([str(c["character_name"] or c["label"]) for c in cast]),
        "locations": locs,
        #: prompt 是唯一必填的小节点，缺它前端就该把节点标黄。
        "prompt_ok": bool((scene.prompt or "").strip()),
        "node_limit": node_limit(),
    }


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
        cast_rows = await fetch_all(db, SceneCast, order_by=SceneCast.index_no)
        loc_rows = await fetch_all(db, SceneLocation, order_by=SceneLocation.index_no)
        apps = {a.id: a for a in await fetch_all(db, Appearance)}
        chars = {c.id: c for c in await fetch_all(db, Character)}
        # 缩略图：一次拉全，别在循环里按 id 查——一幕最多 node_limit 个小节点，但幕可以很多
        assets = {a.id: a for a in await fetch_all(db, Asset)}
        cast_thumbs = _sheet_thumbs(await fetch_all(db, SheetVersion), assets)
        loc_thumbs = _variant_thumbs(await fetch_all(db, LocationReference), assets)
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
                    **_scene_nodes(
                        scene,
                        [c for c in cast_rows if c.scene_id == scene.id],
                        [r for r in loc_rows if r.scene_id == scene.id],
                        apps,
                        chars,
                        variants,
                        locations,
                        cast_thumbs,
                        loc_thumbs,
                    ),
                }
            )
        return out

    async def node_options(self, pid: str) -> dict[str, Any]:
        """挑小节点时的两张清单：可挑的形象、可挑的地点变体，各自带缩略图。

        为什么是一个接口而不是让前端拼：前端原来按角色一个个拉 `appearances`（N+1），
        要图还得再按变体拉一次 `references`（又一轮 N+1）。名字怎么拼
        （`角色 · 形象` / `地点 · 变体`）本来就只该有一处口径，图也一样。

        `thumbnail_path` 是**相对工程目录**的路径，前端过 `fileUrl(pid, path)`；
        没有图的条目给 `null` 并保留在清单里——没有角色表的形象照样能挂，
        只是生成时喂不出参考图，界面上标一下就行。
        """
        db = db_of(pid)
        chars = {c.id: c for c in await fetch_all(db, Character, order_by=Character.created_at)}
        apps = await fetch_all(db, Appearance, order_by=Appearance.created_at)
        locations = {loc.id: loc for loc in await fetch_all(db, Location)}
        variants = await fetch_all(db, LocationVariant, order_by=LocationVariant.created_at)
        assets = {a.id: a for a in await fetch_all(db, Asset)}
        sheets = await fetch_all(db, SheetVersion)
        cast_thumbs = _sheet_thumbs(sheets, assets)
        loc_thumbs = _variant_thumbs(await fetch_all(db, LocationReference), assets)
        sheet_count: dict[str, int] = {}
        for sheet in sheets:
            sheet_count[sheet.appearance_id] = sheet_count.get(sheet.appearance_id, 0) + 1
        return {
            "cast": [
                {
                    "appearance_id": row.id,
                    "character_id": row.character_id,
                    "character_name": chars[row.character_id].name
                    if row.character_id in chars
                    else None,
                    "appearance_name": row.name,
                    "label": (
                        f"{chars[row.character_id].name} · {row.name}"
                        if row.character_id in chars
                        else row.name
                    ),
                    "is_default": bool(row.is_default),
                    "has_sheet": sheet_count.get(row.id, 0) > 0,
                    "thumbnail_path": cast_thumbs.get(row.id),
                }
                for row in apps
            ],
            "locations": [
                {
                    "id": row.id,
                    "location_id": row.location_id,
                    "variant_name": row.name,
                    "label": (
                        f"{locations[row.location_id].name} · {row.name}"
                        if row.location_id in locations
                        else row.name
                    ),
                    "thumbnail_path": loc_thumbs.get(row.id),
                }
                for row in variants
            ],
            "node_limit": node_limit(),
            "limit_hint": LIMIT_HINT,
        }

    async def get_scene(self, pid: str, sid: str) -> dict[str, Any]:
        """单幕。小节点（prompt / 人物 / 地点）都在里面，前端不用再拼三张表。"""
        rows = await self.list_scenes(pid)
        row = next((r for r in rows if r["id"] == sid), None)
        if row is None:
            await fetch(db_of(pid), Scene, sid, "场景")  # 统一的 NOT_FOUND 四要素
            raise AssertionError("unreachable")
        return row

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
        if patch.get("location_variant_id"):
            await self._write_scene_locations(pid, row.id, [patch["location_variant_id"]])
        return await self.get_scene(pid, row.id)

    async def update_scene(self, pid: str, sid: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Scene, sid, "场景")
        if patch.get("location_variant_id"):
            await fetch(db, LocationVariant, patch["location_variant_id"], "地点变体")
        # 主地点要和 scene_location 列表对齐，先把目标算出来并过上限——
        # 否则列写进去了、列表没写，两边说法不一致。
        locations: list[str] | None = None
        if "location_variant_id" in patch:
            locations = await self._plan_primary_location(pid, sid, patch["location_variant_id"])
        async with db.write() as session:
            row = await session.get(Scene, sid)
            assert row is not None
            for key in SCENE_FIELDS:
                if key in patch:
                    setattr(row, key, patch[key])
            row.updated_at = utc_now()
        if locations is not None:
            await self._write_scene_locations(pid, sid, locations)
        return await self.get_scene(pid, sid)

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

    # --- 幕的小节点（prompt / 人物 / 地点） ---

    async def set_scene_cast(self, pid: str, sid: str, appearance_ids: list[str]) -> dict[str, Any]:
        """这一幕有哪些人物。可以一个都不选；上限见 `node_limit()`。

        镜头没挂自己的出场表时，Context Resolver 会用这一份——小节点必须真的影响生成，
        否则只是装饰（见 `services/context.py` 里 `resolve()` 的 `inherited` 分支）。
        """
        db = db_of(pid)
        await fetch(db, Scene, sid, "场景")
        ids = _unique([str(i) for i in appearance_ids])
        _guard_node_limit("人物", ids)
        for aid in ids:
            await fetch(db, Appearance, aid, "形象")
        existing = await fetch_all(db, SceneCast, where=SceneCast.scene_id == sid)
        async with db.write() as session:
            for row in existing:
                fresh = await session.get(SceneCast, row.id)
                if fresh is not None:
                    await session.delete(fresh)
            for i, aid in enumerate(ids):
                session.add(
                    SceneCast(id=new_id("scene_cast"), scene_id=sid, appearance_id=aid, index_no=i)
                )
        return await self.get_scene(pid, sid)

    async def set_scene_locations(
        self, pid: str, sid: str, variant_ids: list[str]
    ) -> dict[str, Any]:
        """这一幕可以用哪几个地点变体。第一条是主地点，会同步进 `Scene.location_variant_id`。"""
        db = db_of(pid)
        await fetch(db, Scene, sid, "场景")
        ids = _unique([str(i) for i in variant_ids])
        _guard_node_limit("地点", ids)
        for vid in ids:
            await fetch(db, LocationVariant, vid, "地点变体")
        await self._write_scene_locations(pid, sid, ids)
        return await self.get_scene(pid, sid)

    async def _plan_primary_location(self, pid: str, sid: str, variant_id: str | None) -> list[str]:
        """算出「把主地点换成 variant_id」之后地点列表该是什么样，顺手过一遍上限。

        传 None 表示这一幕不选地点了——整张列表一起清空：留着一串备选却没有主地点，
        在 Context Resolver 那边解释不通。
        """
        db = db_of(pid)
        rows = await fetch_all(
            db,
            SceneLocation,
            where=SceneLocation.scene_id == sid,
            order_by=SceneLocation.index_no,
        )
        current = [r.location_variant_id for r in rows]
        if not variant_id:
            return []
        target = [variant_id, *[v for v in current if v != variant_id]]
        _guard_node_limit("地点", target)
        return target

    async def _write_scene_locations(self, pid: str, sid: str, ids: list[str]) -> None:
        """整表重写 + 把第一条同步成主地点（与 `set_shot_cast` 同一套做法）。"""
        db = db_of(pid)
        existing = await fetch_all(db, SceneLocation, where=SceneLocation.scene_id == sid)
        async with db.write() as session:
            for row in existing:
                fresh = await session.get(SceneLocation, row.id)
                if fresh is not None:
                    await session.delete(fresh)
            for i, vid in enumerate(ids):
                session.add(
                    SceneLocation(
                        id=new_id("scene_location"),
                        scene_id=sid,
                        location_variant_id=vid,
                        index_no=i,
                    )
                )
            scene = await session.get(Scene, sid)
            if scene is not None:
                scene.location_variant_id = ids[0] if ids else None
                scene.updated_at = utc_now()

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
        scene_cast_rows = await fetch_all(db, SceneCast, order_by=SceneCast.index_no)
        versions = {v.id: v for v in await fetch_all(db, GenerationVersion)}
        variants = {v.id: v for v in await fetch_all(db, LocationVariant)}
        apps = {a.id: a for a in await fetch_all(db, Appearance)}
        chars = {c.id: c for c in await fetch_all(db, Character)}
        assets = {a.id: a for a in await fetch_all(db, Asset)}
        posters = start_frame_index(list(assets.values()))

        lanes = []
        for scene in scenes:
            cards = []
            # 幕级人物是镜头没挂出场表时的兜底，和生成时的取值口径保持一致
            # （见 services/context.py::_cast_appearances）。
            scene_names = _unique(
                [
                    chars[apps[c.appearance_id].character_id].name
                    for c in scene_cast_rows
                    if c.scene_id == scene.id
                    and c.appearance_id in apps
                    and apps[c.appearance_id].character_id in chars
                ]
            )
            for shot in [s for s in shots if s.scene_id == scene.id]:
                own = [
                    chars[apps[c.appearance_id].character_id].name
                    for c in cast_rows
                    if c.shot_id == shot.id
                    and c.appearance_id in apps
                    and apps[c.appearance_id].character_id in chars
                ]
                names = own or scene_names
                issues = []
                # 转场镜头不过上下文门槛（它没有出场角色也不需要地点变体），
                # 所以那几条对它不算问题——列出来只会变成永远消不掉的黄色感叹号。
                if shot.kind != "transition":
                    if not scene.location_variant_id or scene.location_variant_id not in variants:
                        issues.append("缺少地点变体，Context 不完整")
                    if not names:
                        issues.append("没有出场角色")
                    if not (shot.prompt or shot.description or scene.prompt):
                        issues.append("没有 prompt 也没有画面描述")
                media = _shot_media(shot, versions, assets, posters)
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
                        **media,
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

    async def extract_posters(self, pid: str, shot_ids: list[str] | None = None) -> dict[str, Any]:
        """给分镜板上「有片子但还没有图」的卡片补抽一张首帧。

        它是**写操作**，所以独立成一个端点：`GET /storyboard` 只读，绝不在读路径上
        起 FFmpeg 进程（同 `context.py` 那条规矩）。抽出来的是一张真 PNG，登记成
        `Asset(kind="frame")`，同一段视频再抽是幂等复用（`frames.extract`）。

        单条失败不打断其余——某一段视频损坏是它自己的事。每条失败都带完整四要素，
        界面照原样显示。只有 FFmpeg 本身缺失是**全局**问题，那种情况立刻抛出去，
        不给用户看 20 条一模一样的错。
        """
        lanes = await self.storyboard(pid)
        wanted = set(shot_ids or [])
        pending = [
            card
            for lane in lanes
            for card in lane["shots"]
            if card["poster_pending"] and (not wanted or card["id"] in wanted)
        ]
        extracted: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for card in pending:
            try:
                asset = await frames.extract(pid, card["video_asset_id"], "start")
            except AppError as err:
                if err.code == ErrorCode.FFMPEG_MISSING:
                    raise
                failed.append(
                    {"shot_id": card["id"], "title": card["title"], "error": err.to_dict()}
                )
                continue
            extracted.append(
                {
                    "shot_id": card["id"],
                    "asset_id": asset["id"],
                    "path": asset["path"],
                    "reused": bool(asset.get("reused")),
                }
            )
        return {"requested": len(pending), "extracted": extracted, "failed": failed}

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
        # 系统提示词是可配的（设置页「AI 提示词」→「剧本拆解」），内置默认与
        # 「形状永远由代码追加」这条规矩都在 app/ai/prompts.py。
        data = await llm.complete_json(prompts.breakdown(), raw)
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
