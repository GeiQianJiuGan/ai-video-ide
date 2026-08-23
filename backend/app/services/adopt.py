"""从素材库采用（Phase 3）。

采用 = **单向复制**：库里的行按同名字段喂给工程侧**已有**的写路径（cast / world /
assets），文件复制进工程目录。之后两边各改各的——库改了不回流工程，工程改了也不
影响库。这条性质是「工程自包含」的前提：采用完把库目录改名甚至删掉，工程照常打开。

出处只留线索，不留依赖：文件走 `Asset.meta_json` 的 library_asset_id / library_sha1 /
adopted_at，结构化实体走 `origin_library_id` 列。都不是外键，运行期不解析。

这里刻意不新增任何写路径，只做编排。所以「采用来的角色」和手建的角色在工程里完全
同构，没有第二类公民。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import AppError, ErrorCode
from app.events.bus import Channel, bus
from app.persistence.models import utc_now
from app.persistence.models_cast import INHERITABLE
from app.services.assets import assets
from app.services.base import project_of
from app.services.cast import CHARACTER_FIELDS, cast
from app.services.library import library
from app.services.world import LOCATION_FIELDS, PROP_FIELDS, VARIANT_FIELDS, world

#: 能采用的四种东西。素材文件之外，三种预设都是「实体 + 它的参考图」。
ADOPT_KINDS = ("asset", "character", "location", "prop")
KIND_LABEL = {
    "asset": "素材",
    "character": "角色预设",
    "location": "地点预设",
    "prop": "道具预设",
}
#: UI 必须把这句话显示出来，否则用户会以为库和工程是联动的。
ONE_WAY = "采用是单向复制：之后库里改动不会回流到工程，工程里改动也不影响库。"


def _check_kind(kind: str) -> None:
    if kind not in ADOPT_KINDS:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不支持采用这种东西",
            f"kind={kind!r} 不在可采用范围内：{'、'.join(ADOPT_KINDS)}。",
            ["kind 用 asset / character / location / prop 之一"],
        )


def _appearance_patch(row: dict[str, Any]) -> dict[str, Any]:
    """派生形象只搬库里明确覆写过的字段，其余留空继续继承。

    库里的 overrides 才是真源——和工程侧 resolve_fields 一样，不靠「值是否为空」猜。
    """
    fields: tuple[str, ...] = INHERITABLE
    if row.get("parent_id"):
        marked = {f for f in str(row.get("overrides") or "").split(",") if f}
        fields = tuple(f for f in INHERITABLE if f in marked)
    return {"name": row.get("name"), **{f: row.get(f) for f in fields}}


class AdoptService:
    # --- 库侧快照与账单 ---

    async def _bundle(self, kind: str, library_id: str) -> dict[str, Any]:
        """库侧的整份快照。四种 kind 各有一个已有的 bundle 方法，这里只做分派。"""
        _check_kind(kind)
        if kind == "asset":
            return await library.asset(library_id)
        if kind == "character":
            return await library.character_bundle(library_id)
        if kind == "location":
            return await library.location_bundle(library_id)
        return await library.prop_bundle(library_id)

    def _asset_ids(self, kind: str, bundle: dict[str, Any]) -> list[str]:
        """这次采用会牵动库里哪些素材文件（去重，保持出现顺序）。"""
        if kind == "asset":
            ids = [bundle["id"]]
        elif kind == "character":
            ids = [
                s["asset_id"]
                for app in bundle["appearances"]
                for s in app["sheets"]
                if s.get("asset_id")
            ]
        elif kind == "location":
            ids = [
                r["asset_id"]
                for v in bundle["variants"]
                for r in v["references"]
                if r.get("asset_id")
            ]
        else:
            ids = [r["asset_id"] for r in bundle["references"] if r.get("asset_id")]
        return list(dict.fromkeys(ids))

    async def plan(self, pid: str, kind: str, library_id: str) -> dict[str, Any]:
        """采用前的账单：复制几个文件、多大、哪些工程里已经有了、落在哪个目录。

        先给账单再动手是硬要求——文件要进用户的工程目录，代价得先说清。
        """
        proj = project_of(pid)
        bundle = await self._bundle(kind, library_id)
        files: list[dict[str, Any]] = []
        for aid in self._asset_ids(kind, bundle):
            row = await library.asset(aid)
            existing = await assets.by_sha1(pid, row["sha1"]) if row["sha1"] else None
            files.append(
                {
                    "library_asset_id": aid,
                    "title": row.get("title") or aid,
                    "size_bytes": row["size_bytes"] or 0,
                    "missing": row["missing"],
                    "already_in_project": existing is not None,
                }
            )
        reuse = [f for f in files if f["already_in_project"]]
        broken = [f for f in files if f["missing"] and not f["already_in_project"]]
        pending = [f for f in files if not f["already_in_project"] and not f["missing"]]
        return {
            "kind": kind,
            "library_id": library_id,
            "label": KIND_LABEL[kind],
            "name": bundle.get("name") or bundle.get("title") or library_id,
            "project_dir": proj.dir.as_posix(),
            "files": files,
            "copy_count": len(pending),
            "reuse_count": len(reuse),
            "missing_count": len(broken),
            "total_bytes": sum(int(f["size_bytes"]) for f in pending),
            "one_way": ONE_WAY,
        }

    # --- 采用 ---

    async def adopt(self, pid: str, kind: str, library_id: str) -> dict[str, Any]:
        """在工程里建一份可再改的副本，并记下出处。"""
        project_of(pid)  # 工程没打开就先结构化报错，别复制到一半才发现
        _check_kind(kind)
        tally: dict[str, Any] = {"copied": 0, "reused": 0, "asset_ids": []}
        if kind == "asset":
            row = await self._adopt_file(pid, library_id, tally)
            out: dict[str, Any] = {"target_id": row["id"], "name": row["path"]}
        elif kind == "character":
            out = await self._adopt_character(pid, library_id, tally)
        elif kind == "location":
            out = await self._adopt_location(pid, library_id, tally)
        else:
            out = await self._adopt_prop(pid, library_id, tally)
        payload = {
            "kind": kind,
            "library_id": library_id,
            "label": KIND_LABEL[kind],
            **out,
            "copied": tally["copied"],
            "reused": tally["reused"],
            "asset_ids": tally["asset_ids"],
            "one_way": ONE_WAY,
        }
        bus.emit(Channel.SYSTEM, "library.adopted", payload, project_id=pid)
        return payload

    async def _adopt_file(
        self, pid: str, lib_asset_id: str, tally: dict[str, Any]
    ) -> dict[str, Any]:
        """把库里一个素材复制进工程并记出处。

        工程里已经有同内容的文件（sha1 命中）时不复制第二份，直接复用那条登记——
        重复采用因此是幂等的，不会把硬盘塞满。
        """
        row = await library.asset(lib_asset_id)
        existing = await assets.by_sha1(pid, row["sha1"]) if row["sha1"] else None
        if existing is not None:
            asset = existing
            tally["reused"] += 1
        else:
            path = await library.asset_path(lib_asset_id)  # 文件不见了在这里结构化报错
            asset = await assets.register_path(
                pid, row["kind"], str(path), source="imported", copy=True
            )
            tally["copied"] += 1
        asset = await assets.merge_meta(
            pid,
            asset["id"],
            {
                "library_asset_id": lib_asset_id,
                "library_sha1": row["sha1"],
                "adopted_at": utc_now(),
            },
        )
        tally["asset_ids"].append(asset["id"])
        return asset

    def _ordered_appearances(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """父在子前——否则派生关系搬不过去。库里按创建序通常就对，但不能赌。"""
        remaining = list(rows)
        done: set[str] = set()
        out: list[dict[str, Any]] = []
        while remaining:
            ready = [r for r in remaining if not r.get("parent_id") or r["parent_id"] in done]
            if not ready:  # 成环或 parent 指向库外：剩下的按原序当根形象搬，不卡死
                out.extend(remaining)
                break
            for row in ready:
                out.append(row)
                done.add(row["id"])
            remaining = [r for r in remaining if r["id"] not in done]
        return out

    async def _adopt_character(self, pid: str, cid: str, tally: dict[str, Any]) -> dict[str, Any]:
        bundle = await library.character_bundle(cid)
        char = await cast.create_character(
            pid, {f: bundle.get(f) for f in CHARACTER_FIELDS}, origin_library_id=cid
        )
        # create_character 顺手建了一个空的「默认形象」。库里的第一个根形象直接落在这个
        # 空位上，不然工程里会多出一个谁也没填的形象。
        appearances = await cast.list_appearances(pid, char["id"])
        slot: dict[str, Any] | None = next((a for a in appearances if a["is_default"]), None)
        mapped: dict[str, str] = {}
        for row in self._ordered_appearances(bundle["appearances"]):
            patch = _appearance_patch(row)
            if slot is not None and not row.get("parent_id"):
                created = await cast.update_appearance(pid, slot["id"], patch)
                slot = None
            else:
                created = await cast.create_appearance(
                    pid,
                    char["id"],
                    patch,
                    parent_id=mapped.get(str(row.get("parent_id") or "")),
                )
            mapped[row["id"]] = created["id"]
            for sheet in row["sheets"]:
                if not sheet.get("asset_id"):
                    continue
                asset = await self._adopt_file(pid, sheet["asset_id"], tally)
                await cast.add_sheet(pid, created["id"], asset["id"], source="imported")
            if row.get("is_default"):
                await cast.set_default_appearance(pid, created["id"])
        return {
            "target_id": char["id"],
            "name": char["name"],
            "appearance_ids": list(mapped.values()),
        }

    async def _adopt_location(self, pid: str, lid: str, tally: dict[str, Any]) -> dict[str, Any]:
        bundle = await library.location_bundle(lid)
        loc = await world.create_location(
            pid, {f: bundle.get(f) for f in LOCATION_FIELDS}, origin_library_id=lid
        )
        variant_ids: list[str] = []
        for row in bundle["variants"]:
            variant = await world.create_variant(
                pid, loc["id"], {f: row.get(f) for f in VARIANT_FIELDS}
            )
            variant_ids.append(variant["id"])
            for ref in row["references"]:
                asset = await self._adopt_file(pid, ref["asset_id"], tally)
                await world.add_variant_reference(
                    pid, variant["id"], asset["id"], ref.get("camera"), ref.get("note")
                )
        return {"target_id": loc["id"], "name": loc["name"], "variant_ids": variant_ids}

    async def _adopt_prop(self, pid: str, prop_id: str, tally: dict[str, Any]) -> dict[str, Any]:
        bundle = await library.prop_bundle(prop_id)
        prop = await world.create_prop(
            pid, {f: bundle.get(f) for f in PROP_FIELDS}, origin_library_id=prop_id
        )
        for ref in bundle["references"]:  # 已按 version_no 排序，版本顺序照搬
            asset = await self._adopt_file(pid, ref["asset_id"], tally)
            await world.add_prop_reference(pid, prop["id"], asset["id"], ref.get("note"))
        return {"target_id": prop["id"], "name": prop["name"]}


adopt = AdoptService()
