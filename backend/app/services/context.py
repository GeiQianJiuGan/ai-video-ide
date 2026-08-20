"""Context Resolver 与 Context Inspector（Step 6）。

这一层的唯一目标：让「到底喂了什么给模型」变成一张可读的账单。
每一条都必须回答三个问题——哪来的、优先级多少、为什么被包含或被省略。
人可以手动移除 / 添加 / 替换，覆写记录写在 shot.context_overrides_json，
随时可以「恢复自动」。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import AppError, ErrorCode
from app.persistence.models import utc_now
from app.persistence.models_cast import Appearance, Character, SheetVersion
from app.persistence.models_gen import GenerationVersion
from app.persistence.models_story import Scene, Shot, ShotCast, ShotProp
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
#: 单次生成能喂进去的参考图上限。真实上限应由 Workflow 声明，这里给一个保守默认。
DEFAULT_REF_LIMIT = 4


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

        # 1. 出场形象的角色表：同一角色保留优先级最高的一个形象
        cast_rows = await fetch_all(db, ShotCast, where=ShotCast.shot_id == shot_id)
        apps = {a.id: a for a in await fetch_all(db, Appearance)}
        chars = {c.id: c for c in await fetch_all(db, Character)}
        sheets = await fetch_all(db, SheetVersion, order_by=SheetVersion.version_no)
        seen_char: set[str] = set()
        for row in sorted(cast_rows, key=lambda r: r.id):
            app = apps.get(row.appearance_id)
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
                    "label": label,
                    "priority": PRIORITY["character_sheet"] if not duplicate else 30,
                    "asset_id": current.asset_id if current else None,
                    "source_id": app.id,
                    "eligible": bool(current and current.asset_id) and not duplicate,
                    "reason": (
                        "同一角色已有更高优先级形象"
                        if duplicate
                        else (
                            "该角色在本镜头出场"
                            if current and current.asset_id
                            else "该形象还没有角色表图"
                        )
                    ),
                }
            )

        # 2. 地点参考：本 Scene 选定的变体优先；同地点其他变体列出但省略，理由写清
        variants = {v.id: v for v in await fetch_all(db, LocationVariant)}
        locations = {loc.id: loc for loc in await fetch_all(db, Location)}
        loc_refs = await fetch_all(db, LocationReference, order_by=LocationReference.created_at)
        chosen = variants.get(scene.location_variant_id or "")
        for ref in loc_refs:
            variant = variants.get(ref.variant_id)
            if variant is None:
                continue
            location = locations.get(variant.location_id)
            same = chosen is not None and variant.id == chosen.id
            sibling = (
                chosen is not None
                and variant.id != chosen.id
                and variant.location_id == chosen.location_id
            )
            if not same and not sibling:
                continue
            items.append(
                {
                    "key": f"location_reference:{ref.id}",
                    "kind": "location_reference",
                    "label": f"{location.name if location else '未知地点'} · {variant.name}"
                    + (f" · 机位 {ref.camera}" if ref.camera else ""),
                    "priority": PRIORITY["location_reference"] if same else 20,
                    "asset_id": ref.asset_id,
                    "source_id": ref.id,
                    "eligible": same,
                    "reason": "本 Scene 选定的地点变体" if same else "与本 Scene 的时间设定冲突",
                }
            )

        # 3. 上游镜头末帧：连续性的来源
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
            ready = version is not None and version.asset_id is not None
            items.append(
                {
                    "key": f"prev_frame:{shot.prev_shot_id}",
                    "kind": "prev_frame",
                    "label": f"Shot {prev.index_no if prev else '?'} 末帧",
                    "priority": PRIORITY["prev_frame"],
                    "asset_id": version.asset_id if version else None,
                    "source_id": shot.prev_shot_id,
                    "eligible": ready,
                    "reason": "用于保持连续性" if ready else "上游镜头还没有当前版本，末帧不存在",
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
        limit = DEFAULT_REF_LIMIT
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
                item["reason"] = f"参考图总数已达 Workflow 上限 {limit}"
            else:
                item["included"] = True
                included += 1
            asset = assets.get(item["asset_id"] or "")
            item["asset_path"] = asset.path if asset else None
            item["missing_file"] = bool(item["asset_id"]) and asset is None
            item.pop("eligible", None)

        problems = []
        if not scene.location_variant_id:
            problems.append("本 Scene 还没有选定地点变体")
        if not cast_rows:
            problems.append("本镜头没有出场角色")
        if not (shot.prompt or shot.description):
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

    async def require_complete(self, pid: str, shot_id: str) -> dict[str, Any]:
        """生成前的门槛：上下文不完整就明确拒绝，而不是生成一张废图。"""
        ctx = await self.resolve(pid, shot_id)
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
