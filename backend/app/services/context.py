"""Context Resolver 与 Context Inspector（Step 6）。

这一层的唯一目标：让「到底喂了什么给模型」变成一张可读的账单。
每一条都必须回答三个问题——哪来的、优先级多少、为什么被包含或被省略。
人可以手动移除 / 添加 / 替换，覆写记录写在 shot.context_overrides_json，
随时可以「恢复自动」。

被采用的条目还带一个 `role`：**哪一张当首帧、剩下的当参考图**。这条规则只写在这里，
`services/generation.py` 照账单读它，不再自己挑一遍——否则界面上标的和真正喂进去的会分叉。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.persistence.models import utc_now
from app.persistence.models_cast import Appearance, Character, SheetVersion
from app.persistence.models_gen import GenerationVersion
from app.persistence.models_story import Scene, SceneCast, SceneLocation, Shot, ShotCast, ShotProp
from app.persistence.models_world import (
    Asset,
    Location,
    LocationReference,
    LocationVariant,
    Prop,
    PropReference,
)
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, load_json

#: 优先级：同一角色只留最高的那个形象；上游末帧比道具重要，因为它决定连续性。
PRIORITY = {
    "character_sheet": 100,
    "location_reference": 90,
    "prev_frame": 80,
    "prop_reference": 60,
    "manual": 110,
}


def ref_limit() -> int:
    """一次生成最多算到第几张参考图（含当首帧那一张）。

    运行期可配（设置页 `video.ref_limit`）：真实上限取决于模型端那份图有几个参考图槽位，
    我们这边只负责别把账单算得比它还满。
    """
    return max(1, int(settings.video_ref_limit))


def _assign_roles(items: list[dict[str, Any]], has_prev: bool) -> None:
    """给采用的条目标上 `role`：一张 `first_frame`，其余 `reference`。

    规则只有这一份，`services/generation.py` 照它读——以前那边自己又挑了一遍首帧，
    于是检查器上标的和真正喂进去的可能不是同一张。

    挑首帧的顺序：**有上游就用上游末帧**（连续性优先，这是 tail_frame 衔接的全部意义），
    否则用优先级最高的那张（通常是角色表）。没有采用任何条目时谁都不标，
    生成层会去 `params.first_frame_asset_id` 里找显式指定的那张。
    """
    used = [i for i in items if i.get("included")]
    for item in items:
        item["role"] = "reference" if item.get("included") else ""
    if not used:
        return
    first = next((i for i in used if i["kind"] == "prev_frame"), None) if has_prev else None
    (first or used[0])["role"] = "first_frame"


def _extracted_frame(assets: Any, from_asset_id: str | None) -> str | None:
    """找一找这段视频的末帧是不是已经抽过了（frames.extract 会把出处记进 meta）。"""
    if not from_asset_id:
        return None
    for asset in assets:
        if asset.kind != "frame":
            continue
        meta = load_json(asset.meta_json, {})
        if (
            isinstance(meta, dict)
            and meta.get("from_asset_id") == from_asset_id
            and meta.get("at") == "end"
        ):
            return str(asset.id)
    return None


class ContextService:
    async def resolve(self, pid: str, shot_id: str) -> dict[str, Any]:
        db = db_of(pid)
        shot = await fetch(db, Shot, shot_id, "镜头")
        scene = await fetch(db, Scene, shot.scene_id, "场景")
        overrides: list[dict[str, Any]] = load_json(shot.context_overrides_json, [])
        removed = {o["key"] for o in overrides if o.get("action") == "remove"}
        added = [o for o in overrides if o.get("action") == "add"]

        assets = {a.id: a for a in await fetch_all(db, Asset)}
        items: list[dict[str, Any]] = []

        # 1. 出场形象的角色表：同一角色保留优先级最高的一个形象。
        #    镜头没单独挂出场表时**继承这一幕的人物**——流程图上那些「人物」小节点
        #    必须真的影响生成，否则只是装饰（幕级清单见 services/story.py::set_scene_cast）。
        cast_rows = await fetch_all(db, ShotCast, where=ShotCast.shot_id == shot_id)
        picks = [r.appearance_id for r in sorted(cast_rows, key=lambda r: r.id)]
        inherited = False
        if not picks:
            scene_cast = await fetch_all(
                db, SceneCast, where=SceneCast.scene_id == scene.id, order_by=SceneCast.index_no
            )
            picks = [r.appearance_id for r in scene_cast]
            inherited = bool(picks)
        apps = {a.id: a for a in await fetch_all(db, Appearance)}
        chars = {c.id: c for c in await fetch_all(db, Character)}
        sheets = await fetch_all(db, SheetVersion, order_by=SheetVersion.version_no)
        seen_char: set[str] = set()
        for appearance_id in picks:
            app = apps.get(appearance_id)
            if app is None:
                continue
            char = chars.get(app.character_id)
            mine = [s for s in sheets if s.appearance_id == app.id]
            current = next((s for s in mine if s.is_current), mine[-1] if mine else None)
            label = (
                f"{char.name if char else '未知角色'}（{app.name}）"
                f" · Character Sheet v{current.version_no}"
                if current
                else f"{char.name if char else '未知角色'}（{app.name}） · 无角色表"
            )
            duplicate = app.character_id in seen_char
            seen_char.add(app.character_id)
            items.append(
                {
                    "key": f"character_sheet:{app.id}",
                    "kind": "character_sheet",
                    "label": label + ("（本幕人物）" if inherited else ""),
                    "priority": PRIORITY["character_sheet"] if not duplicate else 30,
                    "asset_id": current.asset_id if current else None,
                    "source_id": app.id,
                    "eligible": bool(current and current.asset_id) and not duplicate,
                    "reason": (
                        "同一角色已有更高优先级形象"
                        if duplicate
                        else (
                            (
                                "该角色在本幕出场（镜头没单独挂人物）"
                                if inherited
                                else "该角色在本镜头出场"
                            )
                            if current and current.asset_id
                            else "该形象还没有角色表图"
                        )
                    ),
                }
            )

        # 2. 地点参考：本 Scene 的主地点优先；幕里另外选的地点也能用，只是低一档；
        #    同地点的其他变体列出但省略，理由写清。
        variants = {v.id: v for v in await fetch_all(db, LocationVariant)}
        locations = {loc.id: loc for loc in await fetch_all(db, Location)}
        loc_refs = await fetch_all(db, LocationReference, order_by=LocationReference.created_at)
        chosen = variants.get(scene.location_variant_id or "")
        picked_rows = await fetch_all(
            db,
            SceneLocation,
            where=SceneLocation.scene_id == scene.id,
            order_by=SceneLocation.index_no,
        )
        picked = {r.location_variant_id for r in picked_rows}
        for ref in loc_refs:
            variant = variants.get(ref.variant_id)
            if variant is None:
                continue
            location = locations.get(variant.location_id)
            same = chosen is not None and variant.id == chosen.id
            extra = not same and variant.id in picked
            sibling = (
                not same
                and not extra
                and chosen is not None
                and variant.location_id == chosen.location_id
            )
            if not same and not extra and not sibling:
                continue
            items.append(
                {
                    "key": f"location_reference:{ref.id}",
                    "kind": "location_reference",
                    "label": f"{location.name if location else '未知地点'} · {variant.name}"
                    + (f" · 机位 {ref.camera}" if ref.camera else ""),
                    "priority": (
                        PRIORITY["location_reference"]
                        if same
                        else (PRIORITY["location_reference"] - 5 if extra else 20)
                    ),
                    "asset_id": ref.asset_id,
                    "source_id": ref.id,
                    "eligible": same or extra,
                    "reason": (
                        "本 Scene 选定的地点变体"
                        if same
                        else ("本幕另外选中的地点变体" if extra else "与本 Scene 的时间设定冲突")
                    ),
                }
            )

        # 3. 上游镜头末帧：连续性的来源。
        #    注意这里指的是**抽出来的那张图**，不是上游那整段视频——模型端拿一段视频当
        #    首帧是用不了的。抽帧要起 FFmpeg 进程，所以不在这条只读路径上做：还没抽的时候
        #    先标 pending_extract，真正抽取发生在入队前（services/generation.py）。
        if shot.prev_shot_id:
            prev = next((s for s in await fetch_all(db, Shot) if s.id == shot.prev_shot_id), None)
            version = None
            if prev is not None and prev.current_version_id:
                version = next(
                    (
                        v
                        for v in await fetch_all(db, GenerationVersion)
                        if v.id == prev.current_version_id
                    ),
                    None,
                )
            source_asset = version.asset_id if version else None
            ready = source_asset is not None
            frame = _extracted_frame(assets.values(), source_asset) if ready else None
            items.append(
                {
                    "key": f"prev_frame:{shot.prev_shot_id}",
                    "kind": "prev_frame",
                    "label": f"Shot {prev.index_no if prev else '?'} 末帧",
                    "priority": PRIORITY["prev_frame"],
                    "asset_id": frame or source_asset,
                    "source_id": shot.prev_shot_id,
                    "eligible": ready,
                    "pending_extract": bool(ready and frame is None),
                    "from_asset_id": source_asset,
                    "reason": (
                        "已抽取的上游末帧，用于保持连续性"
                        if frame
                        else (
                            "上游已出片，生成前会从它抽取末帧"
                            if ready
                            else "上游镜头还没有当前版本，末帧不存在"
                        )
                    ),
                }
            )

        # 4. 出场道具参考图
        props = {p.id: p for p in await fetch_all(db, Prop)}
        prop_refs = await fetch_all(db, PropReference)
        for row in await fetch_all(db, ShotProp, where=ShotProp.shot_id == shot_id):
            prop = props.get(row.prop_id)
            mine = [r for r in prop_refs if r.prop_id == row.prop_id]
            current = next((r for r in mine if r.is_current), None)
            present = row.state == "present"
            items.append(
                {
                    "key": f"prop_reference:{row.prop_id}",
                    "kind": "prop_reference",
                    "label": f"{prop.name if prop else '未知道具'} · 参考图 v{current.version_no}"
                    if current
                    else f"{prop.name if prop else '未知道具'} · 无参考图",
                    "priority": PRIORITY["prop_reference"] if present else 10,
                    "asset_id": current.asset_id if current else None,
                    "source_id": row.prop_id,
                    "eligible": bool(current) and present,
                    "reason": ("本镜头出场道具" if present else "该道具在本镜头标为已丢弃")
                    if current
                    else "该道具还没有参考图",
                }
            )

        # 5. 人工添加的条目：优先级最高，永不被自动逻辑挤掉
        for extra in added:
            items.append(
                {
                    "key": extra.get("key") or f"manual:{extra.get('asset_id')}",
                    "kind": "manual",
                    "label": extra.get("label") or "手动添加的参考图",
                    "priority": PRIORITY["manual"],
                    "asset_id": extra.get("asset_id"),
                    "source_id": None,
                    "eligible": True,
                    "reason": "手动添加",
                }
            )

        # 排序 → 应用覆写 → 卡上限
        items.sort(key=lambda i: (-int(i["priority"]), str(i["key"])))
        limit = ref_limit()
        included = 0
        for item in items:
            item["manual"] = item["kind"] == "manual" or item["key"] in removed
            if item["key"] in removed:
                item["included"] = False
                item["reason"] = "手动移除"
            elif not item["eligible"]:
                item["included"] = False
            elif item["asset_id"] is None:
                item["included"] = False
                item["reason"] = "没有可用的图片资产"
            elif included >= limit:
                item["included"] = False
                item["reason"] = f"已达参考图上限 {limit} 张（可在设置页的「视频生成 API」里调整）"
            else:
                item["included"] = True
                included += 1
            asset = assets.get(item["asset_id"] or "")
            item["asset_path"] = asset.path if asset else None
            item["missing_file"] = bool(item["asset_id"]) and asset is None
            item.pop("eligible", None)
        _assign_roles(items, bool(shot.prev_shot_id))

        problems = []
        if not scene.location_variant_id:
            problems.append("本 Scene 还没有选定地点变体")
        if not picks:
            problems.append("本镜头没有出场角色")
        # 幕级 prompt 是镜头没写 prompt 时的兜底，取值口径与 generation.enqueue_shot 一致
        if not (shot.prompt or shot.description or scene.prompt):
            problems.append("既没有 prompt 也没有画面描述")
        if shot.prev_shot_id and not any(
            i["kind"] == "prev_frame" and i["included"] for i in items
        ):
            problems.append("需要上游末帧，但上游镜头还没有当前版本")

        return {
            "shot_id": shot_id,
            "items": items,
            "included_count": included,
            "limit": limit,
            "at_limit": included >= limit,
            "complete": not problems,
            "problems": problems,
            "overrides": overrides,
            "resolved_at": utc_now(),
        }

    async def ensure_frames(self, pid: str, shot_id: str) -> dict[str, Any]:
        """把「生成前会抽取」那些条目真的抽出来，然后重新出账单。

        单独一个方法而不是塞进 `resolve`：resolve 是只读的、UI 会频繁调，
        起 FFmpeg 进程不该发生在那条路径上。入队前调这个。
        """
        ctx = await self.resolve(pid, shot_id)
        pending = [i for i in ctx["items"] if i.get("pending_extract") and i.get("from_asset_id")]
        if not pending:
            return ctx
        from app.services.frames import frames  # 延迟导入：context 不该在模块级依赖 FFmpeg 层

        for item in pending:
            await frames.extract(pid, str(item["from_asset_id"]), "end")
        return await self.resolve(pid, shot_id)

    async def require_complete(self, pid: str, shot_id: str) -> dict[str, Any]:
        """生成前的门槛：上下文不完整就明确拒绝，而不是生成一张废图。"""
        ctx = await self.ensure_frames(pid, shot_id)
        if not ctx["complete"]:
            raise AppError(
                ErrorCode.CONTEXT_INCOMPLETE,
                "上下文不完整",
                "；".join(ctx["problems"]),
                [
                    "在镜头编辑器的上下文检查器里补齐缺失项",
                    "或先给本 Scene 选一个地点变体",
                    "确认无误也可以在检查器里手动添加参考图",
                ],
                {"shot_id": shot_id},
            )
        return ctx

    # --- 人工干预 ---

    async def override(
        self, pid: str, shot_id: str, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """action ∈ remove / add / reset。replace = remove 旧的 + add 新的。"""
        if action not in ("remove", "add", "reset"):
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的干预动作",
                f"{action} 不在 remove / add / reset 里。",
                ["用「移除」「添加」或「恢复自动」"],
            )
        db = db_of(pid)
        shot = await fetch(db, Shot, shot_id, "镜头")
        overrides: list[dict[str, Any]] = load_json(shot.context_overrides_json, [])

        if action == "reset":
            key = payload.get("key")
            overrides = [] if not key else [o for o in overrides if o.get("key") != key]
        elif action == "remove":
            key = str(payload.get("key") or "")
            if not key:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "缺少要移除的条目",
                    "payload.key 是必填的。",
                    ["从上下文清单里点某一行的「移除」"],
                )
            overrides = [o for o in overrides if o.get("key") != key]
            overrides.append({"action": "remove", "key": key, "at": utc_now()})
        else:
            asset_id = str(payload.get("asset_id") or "")
            if not asset_id:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "缺少要添加的资产",
                    "payload.asset_id 是必填的。",
                    ["从资产库里选一张图再添加"],
                )
            await fetch(db, Asset, asset_id, "资产")
            overrides.append(
                {
                    "action": "add",
                    "key": f"manual:{asset_id}",
                    "asset_id": asset_id,
                    "label": payload.get("label"),
                    "at": utc_now(),
                }
            )

        async with db.write() as session:
            row = await session.get(Shot, shot_id)
            assert row is not None
            row.context_overrides_json = dump_json(overrides)
            row.updated_at = utc_now()
        return await self.resolve(pid, shot_id)

    async def snapshot(self, pid: str, shot_id: str) -> dict[str, Any]:
        """冻结进 GenerationVersion.context_json 的那份账单。"""
        ctx = await self.resolve(pid, shot_id)
        return {
            "resolved_at": ctx["resolved_at"],
            "limit": ctx["limit"],
            "included": [
                {
                    "key": i["key"],
                    "kind": i["kind"],
                    "label": i["label"],
                    "asset_id": i["asset_id"],
                    "priority": i["priority"],
                    #: 这一张是当首帧还是当参考图。冻结它，事后才说得清「喂了什么」。
                    "role": i.get("role") or "reference",
                }
                for i in ctx["items"]
                if i["included"]
            ],
            "omitted": [
                {"key": i["key"], "label": i["label"], "reason": i["reason"]}
                for i in ctx["items"]
                if not i["included"]
            ],
        }

    async def asset_of(self, pid: str, asset_id: str) -> dict[str, Any]:
        return as_dict(await fetch(db_of(pid), Asset, asset_id, "资产"))


context = ContextService()
