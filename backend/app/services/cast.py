"""角色与形象服务（Step 2）。

这里最重要的不是 CRUD，而是**每个字段的值从哪来**：
派生形象的字段要么是自己覆写的，要么是从父形象继承的，二者必须能被区分。
resolve_fields() 把这件事变成结构化数据，前端才能画出「浅色继承值 + 小锁」。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.events.bus import Channel, bus
from app.persistence.models import utc_now
from app.persistence.models_cast import INHERITABLE, Appearance, Character, SheetVersion
from app.services.base import as_dict, db_of, fetch, fetch_all

CHARACTER_FIELDS = (
    "name",
    "alias",
    "gender",
    "age_range",
    "personality",
    "background",
    "voice_desc",
    "notes",
)
APPEARANCE_FIELDS = ("name", *INHERITABLE)


def _override_set(row: Appearance) -> set[str]:
    return {f for f in (row.overrides or "").split(",") if f}


def resolve_fields(row: Appearance, chain: dict[str, Appearance]) -> dict[str, Any]:
    """算出每个可继承字段的最终值与来源。

    返回 {字段: {value, source, from_id, from_name, overridden}}：
      source = "own"       自己填的（根形象或已覆写）
      source = "inherited" 来自某个祖先形象
      source = "empty"     整条链上都没人填
    """
    own = _override_set(row)
    out: dict[str, Any] = {}
    for field in INHERITABLE:
        if row.parent_id is None or field in own:
            value = getattr(row, field)
            out[field] = {
                "value": value,
                "source": "own" if value not in (None, "") else "empty",
                "from_id": None,
                "from_name": None,
                "overridden": field in own and row.parent_id is not None,
            }
            continue
        node: Appearance | None = chain.get(row.parent_id or "")
        found: tuple[str, Appearance] | None = None
        while node is not None:
            if field in _override_set(node) or node.parent_id is None:
                value = getattr(node, field)
                if value not in (None, ""):
                    found = (value, node)
                    break
            node = chain.get(node.parent_id or "")
        out[field] = {
            "value": found[0] if found else None,
            "source": "inherited" if found else "empty",
            "from_id": found[1].id if found else None,
            "from_name": found[1].name if found else None,
            "overridden": False,
        }
    return out


class CastService:
    # --- Character ---

    async def list_characters(self, pid: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        chars = await fetch_all(db, Character, order_by=Character.created_at)
        apps = await fetch_all(db, Appearance, order_by=Appearance.created_at)
        by_char: dict[str, int] = {}
        for a in apps:
            by_char[a.character_id] = by_char.get(a.character_id, 0) + 1
        return [{**as_dict(c), "appearance_count": by_char.get(c.id, 0)} for c in chars]

    async def create_character(self, pid: str, patch: dict[str, Any]) -> dict[str, Any]:
        name = str(patch.get("name") or "").strip()
        if not name:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "角色需要一个名字",
                "名字是角色的唯一必填项，其余都可以之后再补。",
                ["填一个名字，例如「林昭」"],
            )
        db = db_of(pid)
        now = utc_now()
        row = Character(
            id=new_id("character"),
            **{k: patch.get(k) for k in CHARACTER_FIELDS if k != "name"},
            name=name,
            created_at=now,
            updated_at=now,
        )
        async with db.write() as session:
            session.add(row)
        # 建角色时顺手给一个根形象：没有形象的角色在镜头里无法被引用
        await self.create_appearance(pid, row.id, {"name": "默认形象"}, default=True)
        bus.emit(Channel.SHOT, "character.created", {"id": row.id, "name": name}, project_id=pid)
        return as_dict(row)

    async def update_character(self, pid: str, cid: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Character, cid, "角色")
        async with db.write() as session:
            row = await session.get(Character, cid)
            assert row is not None
            for key in CHARACTER_FIELDS:
                if key in patch:
                    setattr(row, key, patch[key])
            row.updated_at = utc_now()
            out = as_dict(row)
        return out

    async def delete_character(self, pid: str, cid: str) -> None:
        db = db_of(pid)
        row = await fetch(db, Character, cid, "角色")
        async with db.write() as session:
            fresh = await session.get(Character, cid)
            if fresh is not None:
                await session.delete(fresh)
        bus.emit(Channel.SHOT, "character.deleted", {"id": row.id}, project_id=pid)

    # --- Appearance ---

    async def list_appearances(self, pid: str, cid: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        await fetch(db, Character, cid, "角色")
        rows = await fetch_all(
            db, Appearance, where=Appearance.character_id == cid, order_by=Appearance.created_at
        )
        chain = {r.id: r for r in rows}
        sheets = await fetch_all(db, SheetVersion, order_by=SheetVersion.version_no)
        out: list[dict[str, Any]] = []
        for row in rows:
            mine = [s for s in sheets if s.appearance_id == row.id]
            current = next((s for s in mine if s.is_current), None)
            out.append(
                {
                    **as_dict(row),
                    "overrides": sorted(_override_set(row)),
                    "fields": resolve_fields(row, chain),
                    "sheet_count": len(mine),
                    "current_sheet": as_dict(current) if current else None,
                }
            )
        return out

    async def create_appearance(
        self,
        pid: str,
        cid: str,
        patch: dict[str, Any],
        *,
        parent_id: str | None = None,
        default: bool = False,
    ) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Character, cid, "角色")
        if parent_id:
            parent = await fetch(db, Appearance, parent_id, "父形象")
            if parent.character_id != cid:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "父形象不属于这个角色",
                    f"形象 {parent_id} 属于角色 {parent.character_id}。",
                    ["只能从同一角色的形象派生"],
                )
        name = str(patch.get("name") or "").strip() or "新形象"
        now = utc_now()
        # 派生时：显式填了值的字段即为覆写，其余留空表示继承
        overrides = sorted(f for f in INHERITABLE if parent_id and patch.get(f) not in (None, ""))
        row = Appearance(
            id=new_id("appearance"),
            character_id=cid,
            parent_id=parent_id,
            name=name,
            **{f: patch.get(f) for f in INHERITABLE},
            overrides=",".join(overrides),
            is_default=1 if default else 0,
            created_at=now,
            updated_at=now,
        )
        async with db.write() as session:
            session.add(row)
        bus.emit(
            Channel.SHOT,
            "appearance.created",
            {"id": row.id, "character_id": cid, "parent_id": parent_id},
            project_id=pid,
        )
        return as_dict(row)

    async def update_appearance(self, pid: str, aid: str, patch: dict[str, Any]) -> dict[str, Any]:
        """写入字段值，并把被写的可继承字段登记为「已覆写」。"""
        db = db_of(pid)
        await fetch(db, Appearance, aid, "形象")
        async with db.write() as session:
            row = await session.get(Appearance, aid)
            assert row is not None
            overrides = _override_set(row)
            for key in APPEARANCE_FIELDS:
                if key not in patch:
                    continue
                setattr(row, key, patch[key])
                if key in INHERITABLE and row.parent_id is not None:
                    overrides.add(key)
            row.overrides = ",".join(sorted(overrides))
            row.updated_at = utc_now()
        return await self.get_appearance(pid, aid)

    async def revert_field(self, pid: str, aid: str, field: str) -> dict[str, Any]:
        """把某个字段还原成继承：清掉覆写标记与本地值，值重新由父形象决定。"""
        if field not in INHERITABLE:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "该字段不可继承",
                f"{field} 不在可继承字段里：{'、'.join(INHERITABLE)}。",
                ["只对可继承字段调用「恢复继承」"],
            )
        db = db_of(pid)
        row = await fetch(db, Appearance, aid, "形象")
        if row.parent_id is None:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "根形象没有可继承的来源",
                f"形象「{row.name}」不是派生形象，字段值只能自己填。",
                ["从其他形象派生一个新形象", "或直接清空该字段"],
            )
        async with db.write() as session:
            fresh = await session.get(Appearance, aid)
            assert fresh is not None
            fresh.overrides = ",".join(sorted(_override_set(fresh) - {field}))
            setattr(fresh, field, None)
            fresh.updated_at = utc_now()
        return await self.get_appearance(pid, aid)

    async def get_appearance(self, pid: str, aid: str) -> dict[str, Any]:
        db = db_of(pid)
        row = await fetch(db, Appearance, aid, "形象")
        siblings = await fetch_all(
            db, Appearance, where=Appearance.character_id == row.character_id
        )
        chain = {r.id: r for r in siblings}
        return {
            **as_dict(row),
            "overrides": sorted(_override_set(row)),
            "fields": resolve_fields(row, chain),
        }

    async def delete_appearance(self, pid: str, aid: str) -> None:
        db = db_of(pid)
        await fetch(db, Appearance, aid, "形象")
        async with db.write() as session:
            fresh = await session.get(Appearance, aid)
            if fresh is not None:
                await session.delete(fresh)

    async def set_default_appearance(self, pid: str, aid: str) -> dict[str, Any]:
        db = db_of(pid)
        row = await fetch(db, Appearance, aid, "形象")
        async with db.write() as session:
            for sib in await fetch_all(
                db, Appearance, where=Appearance.character_id == row.character_id
            ):
                fresh = await session.get(Appearance, sib.id)
                if fresh is not None:
                    fresh.is_default = 1 if sib.id == aid else 0
        return await self.get_appearance(pid, aid)

    # --- Character Sheet 版本（只增不改） ---

    async def add_sheet(
        self, pid: str, aid: str, asset_id: str | None, source: str = "manual"
    ) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Appearance, aid, "形象")
        existing = await fetch_all(db, SheetVersion, where=SheetVersion.appearance_id == aid)
        row = SheetVersion(
            id=new_id("sheet_version"),
            appearance_id=aid,
            version_no=max((s.version_no for s in existing), default=0) + 1,
            asset_id=asset_id,
            source=source,
            is_current=1,
            created_at=utc_now(),
        )
        async with db.write() as session:
            for old in existing:  # 旧版本保留，只是不再是「当前」
                fresh = await session.get(SheetVersion, old.id)
                if fresh is not None:
                    fresh.is_current = 0
            session.add(row)
        bus.emit(
            Channel.VERSION,
            "sheet.created",
            {"appearance_id": aid, "version_no": row.version_no},
            project_id=pid,
        )
        return as_dict(row)

    async def list_sheets(self, pid: str, aid: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        rows = await fetch_all(
            db,
            SheetVersion,
            where=SheetVersion.appearance_id == aid,
            order_by=SheetVersion.version_no.desc(),
        )
        return [as_dict(r) for r in rows]


cast = CastService()
