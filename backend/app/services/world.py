"""地点、变体与道具（Step 3）。

变体（「城南旧宅 · 雨夜」）是镜头真正引用的东西：地点只说「在哪」，
变体才说「什么时候、什么天气、什么光」。Scene 引用变体，因此每个变体都要能
回答「被几个 Scene 用着」——这一行数字决定了改它安不安全。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.persistence.models import utc_now
from app.persistence.models_story import Scene, Shot, ShotProp
from app.persistence.models_world import (
    Asset,
    Location,
    LocationReference,
    LocationVariant,
    Prop,
    PropReference,
)
from app.services.assets import assets
from app.services.base import as_dict, db_of, fetch, fetch_all, require_name

LOCATION_FIELDS = ("name", "description", "notes")
VARIANT_FIELDS = ("name", "time_of_day", "weather", "lighting", "description")
PROP_FIELDS = ("name", "description", "notes")


class WorldService:
    # --- 地点 ---

    async def list_locations(self, pid: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        locs = await fetch_all(db, Location, order_by=Location.created_at)
        variants = await fetch_all(db, LocationVariant, order_by=LocationVariant.created_at)
        scenes = await fetch_all(db, Scene)
        used: dict[str, int] = {}
        for scene in scenes:
            if scene.location_variant_id:
                used[scene.location_variant_id] = used.get(scene.location_variant_id, 0) + 1
        return [
            {
                **as_dict(loc),
                "variants": [
                    {**as_dict(v), "scene_count": used.get(v.id, 0)}
                    for v in variants
                    if v.location_id == loc.id
                ],
            }
            for loc in locs
        ]

    async def create_location(
        self, pid: str, patch: dict[str, Any], *, origin_library_id: str | None = None
    ) -> dict[str, Any]:
        """建地点。origin_library_id 只在「从素材库采用」时传，是出处不是外键
        （见 services/adopt.py）。"""
        default_asset_id = str(patch.get("default_asset_id") or "").strip()
        db = db_of(pid)
        if default_asset_id:
            await fetch(db, Asset, default_asset_id, "默认场景参考图资产")
        now = utc_now()
        row = Location(
            id=new_id("location"),
            name=require_name(patch.get("name"), "地点", "城南旧宅"),
            description=patch.get("description"),
            notes=patch.get("notes"),
            origin_library_id=origin_library_id,
            created_at=now,
            updated_at=now,
        )
        async with db.write() as session:
            session.add(row)
        if default_asset_id:
            variant = await self.create_variant(pid, row.id, {"name": "默认场景"})
            await self.add_variant_reference(
                pid, variant["id"], default_asset_id, None, None
            )
        return as_dict(row)

    async def update_location(self, pid: str, lid: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Location, lid, "地点")
        async with db.write() as session:
            row = await session.get(Location, lid)
            assert row is not None
            for key in LOCATION_FIELDS:
                if key in patch:
                    setattr(row, key, patch[key])
            row.updated_at = utc_now()
            return as_dict(row)

    async def delete_location(self, pid: str, lid: str) -> None:
        db = db_of(pid)
        await fetch(db, Location, lid, "地点")
        variants = await fetch_all(db, LocationVariant, where=LocationVariant.location_id == lid)
        scenes = await fetch_all(db, Scene)
        blocking = [s for s in scenes if s.location_variant_id in {v.id for v in variants}]
        if blocking:
            names = "、".join(s.title for s in blocking[:5])
            raise AppError(
                ErrorCode.CONFLICT,
                "该地点仍被 Scene 引用",
                f"有 {len(blocking)} 个 Scene 用着它的变体：{names}。",
                ["先把这些 Scene 改到别的变体", "或先删除这些 Scene"],
                {"location_id": lid},
            )
        async with db.write() as session:
            fresh = await session.get(Location, lid)
            if fresh is not None:
                await session.delete(fresh)

    # --- 变体 ---

    async def create_variant(self, pid: str, lid: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Location, lid, "地点")
        now = utc_now()
        row = LocationVariant(
            id=new_id("location_variant"),
            location_id=lid,
            name=require_name(patch.get("name"), "变体", "雨夜"),
            **{k: patch.get(k) for k in VARIANT_FIELDS if k != "name"},
            created_at=now,
            updated_at=now,
        )
        async with db.write() as session:
            session.add(row)
        return as_dict(row)

    async def update_variant(self, pid: str, vid: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, LocationVariant, vid, "地点变体")
        async with db.write() as session:
            row = await session.get(LocationVariant, vid)
            assert row is not None
            for key in VARIANT_FIELDS:
                if key in patch:
                    setattr(row, key, patch[key])
            row.updated_at = utc_now()
            return as_dict(row)

    async def delete_variant(self, pid: str, vid: str) -> None:
        db = db_of(pid)
        await fetch(db, LocationVariant, vid, "地点变体")
        scenes = [s for s in await fetch_all(db, Scene) if s.location_variant_id == vid]
        if scenes:
            raise AppError(
                ErrorCode.CONFLICT,
                "该变体仍被 Scene 引用",
                f"有 {len(scenes)} 个 Scene 用着它：{'、'.join(s.title for s in scenes[:5])}。",
                ["先把这些 Scene 改到别的变体", "或先删除这些 Scene"],
                {"variant_id": vid},
            )
        async with db.write() as session:
            fresh = await session.get(LocationVariant, vid)
            if fresh is not None:
                await session.delete(fresh)

    async def variant_usage(self, pid: str, vid: str) -> list[dict[str, Any]]:
        """「被 N 个 Scene 引用」背后的可点列表。"""
        db = db_of(pid)
        scenes = [s for s in await fetch_all(db, Scene) if s.location_variant_id == vid]
        return [{"id": s.id, "title": s.title, "index_no": s.index_no} for s in scenes]

    async def add_variant_reference(
        self, pid: str, vid: str, asset_id: str, camera: str | None, note: str | None
    ) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, LocationVariant, vid, "地点变体")
        row = LocationReference(
            id=new_id("location_reference"),
            variant_id=vid,
            asset_id=asset_id,
            camera=camera,
            note=note,
            is_current=1,
            created_at=utc_now(),
        )
        async with db.write() as session:
            session.add(row)
        await assets.link(pid, asset_id, "location_variant", vid, role="reference")
        return as_dict(row)

    async def variant_references(self, pid: str, vid: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        rows = await fetch_all(
            db,
            LocationReference,
            where=LocationReference.variant_id == vid,
            order_by=LocationReference.created_at,
        )
        return [as_dict(r) for r in rows]

    # --- 道具 ---

    async def list_props(self, pid: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        props = await fetch_all(db, Prop, order_by=Prop.created_at)
        refs = await fetch_all(db, PropReference)
        shot_props = await fetch_all(db, ShotProp)
        shots = {s.id: s for s in await fetch_all(db, Shot)}
        out = []
        for prop in props:
            mine = [r for r in refs if r.prop_id == prop.id]
            current = next((r for r in mine if r.is_current), None)
            appears = [sp for sp in shot_props if sp.prop_id == prop.id]
            out.append(
                {
                    **as_dict(prop),
                    "reference_count": len(mine),
                    "current_reference": as_dict(current) if current else None,
                    "shot_count": len({sp.shot_id for sp in appears if sp.shot_id in shots}),
                }
            )
        return out

    async def create_prop(
        self, pid: str, patch: dict[str, Any], *, origin_library_id: str | None = None
    ) -> dict[str, Any]:
        """建道具。origin_library_id 只在「从素材库采用」时传，是出处不是外键
        （见 services/adopt.py）。"""
        default_asset_id = str(patch.get("default_asset_id") or "").strip()
        db = db_of(pid)
        if default_asset_id:
            await fetch(db, Asset, default_asset_id, "默认道具参考图资产")
        now = utc_now()
        row = Prop(
            id=new_id("prop"),
            name=require_name(patch.get("name"), "道具", "油纸伞"),
            description=patch.get("description"),
            notes=patch.get("notes"),
            origin_library_id=origin_library_id,
            created_at=now,
            updated_at=now,
        )
        async with db.write() as session:
            session.add(row)
        if default_asset_id:
            await self.add_prop_reference(pid, row.id, default_asset_id)
        return as_dict(row)

    async def update_prop(self, pid: str, prop_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Prop, prop_id, "道具")
        async with db.write() as session:
            row = await session.get(Prop, prop_id)
            assert row is not None
            for key in PROP_FIELDS:
                if key in patch:
                    setattr(row, key, patch[key])
            row.updated_at = utc_now()
            return as_dict(row)

    async def delete_prop(self, pid: str, prop_id: str) -> None:
        db = db_of(pid)
        await fetch(db, Prop, prop_id, "道具")
        used = [sp for sp in await fetch_all(db, ShotProp) if sp.prop_id == prop_id]
        if used:
            raise AppError(
                ErrorCode.CONFLICT,
                "该道具仍出现在镜头里",
                f"有 {len(used)} 个镜头把它列为出场道具。",
                ["先从这些镜头移除该道具", "或保留道具，只把它标为已丢弃"],
                {"prop_id": prop_id},
            )
        async with db.write() as session:
            fresh = await session.get(Prop, prop_id)
            if fresh is not None:
                await session.delete(fresh)

    async def add_prop_reference(
        self, pid: str, prop_id: str, asset_id: str, note: str | None = None
    ) -> dict[str, Any]:
        """参考图只增版本，旧版本永不覆盖。"""
        db = db_of(pid)
        await fetch(db, Prop, prop_id, "道具")
        existing = await fetch_all(db, PropReference, where=PropReference.prop_id == prop_id)
        row = PropReference(
            id=new_id("prop_reference"),
            prop_id=prop_id,
            asset_id=asset_id,
            version_no=max((r.version_no for r in existing), default=0) + 1,
            note=note,
            is_current=1,
            created_at=utc_now(),
        )
        async with db.write() as session:
            for old in existing:
                fresh = await session.get(PropReference, old.id)
                if fresh is not None:
                    fresh.is_current = 0
            session.add(row)
        await assets.link(pid, asset_id, "prop", prop_id, role="reference")
        return as_dict(row)

    async def prop_references(self, pid: str, prop_id: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        rows = await fetch_all(
            db,
            PropReference,
            where=PropReference.prop_id == prop_id,
            order_by=PropReference.version_no.desc(),
        )
        return [as_dict(r) for r in rows]


world = WorldService()
