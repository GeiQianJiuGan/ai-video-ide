"""应用级素材库（Phase 3）。

这是四条硬约束里「每工程一个库，没有全局数据库」的一个**记录在案的例外**：
素材库有自己的目录与 library.db，但它

  · 不管理工程，也不持有任何 Shot / Generation / Timeline 数据；
  · 只存可复用的素材文件与角色 / 地点 / 道具预设；
  · 「采用」是单向复制（见 services/adopt.py）——工程运行期完全不依赖库在不在。

工程仍然是唯一真源。库目录与工程目录同构，所以落盘、sha1 去重、相对路径这些规则
直接复用 services/assets.py 的模块级函数，没有第二套实现：

    我的素材库/
      library.aivs.json   清单：kind: "aivs-library" + schema_version
      library.db          SQLite（WAL）
      assets/             素材文件，按 kind 分子目录

库不走 alembic：打开时 create_all（幂等、只增表），清单里的 schema_version 把关。
清单版本比当前应用新就拒开，绝不静默改写用户的文件。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.events.bus import Channel, bus
from app.persistence import migrate
from app.persistence.db import Database
from app.persistence.models import utc_now
from app.persistence.models_cast import INHERITABLE
from app.persistence.models_library import (
    LibAppearance,
    LibAsset,
    LibAssetRef,
    LibCharacter,
    LibLocation,
    LibLocationReference,
    LibLocationVariant,
    LibProp,
    LibPropReference,
    LibraryBase,
    LibraryMeta,
    LibSheet,
    LibTag,
    LibTagLink,
)

# 字段名清单直接用工程侧的：库表镜像工程表，共用一份字段定义，
# 「采用」才能把库里的行原样喂给工程侧写路径而不需要任何翻译。
from app.services.assets import (
    KIND_DIR,
    content_name,
    ensure_dir,
    kind_of_suffix,
    require_bytes,
    sha1_of_file,
    sniff_size,
    source_file,
    write_file,
)
from app.services.base import (
    as_dict,
    dump_json,
    fetch,
    fetch_all,
    require_name,
)
from app.services.cast import APPEARANCE_FIELDS, CHARACTER_FIELDS
from app.services.world import LOCATION_FIELDS, PROP_FIELDS, VARIANT_FIELDS

log = get_logger("library")

MANIFEST_NAME = "library.aivs.json"
DB_NAME = "library.db"
MANIFEST_KIND = "aivs-library"
#: 库的 schema 版本与工程的 settings.schema_version 是两条独立的线。
LIBRARY_SCHEMA = 1
SUBDIRS = ("assets",)

#: 库里允许的素材类型 → 子目录。只收 `assets/` 下的那些：生成物与代理流是工程的东西，
#: 抽出来的首尾帧是临时资源（落 `cache/frames/`），两类都不该进库。
LIB_KIND_DIR = {kind: rel for kind, rel in KIND_DIR.items() if rel.startswith("assets/")}
TAG_OWNERS = ("asset", "character", "location", "prop")

#: 目录里已有别的 library.db 时给用户的出路，与工程侧同一套写法。
OCCUPIED_SUGGESTIONS = [
    "换一个空目录作为素材库",
    f"把这个 {DB_NAME} 备份到别处后重试",
    f"以只读方式检查这个 {DB_NAME} 属于哪个程序",
]

RESELECT = "在素材库页重新选择目录"


def _resolve_dir(raw: str) -> Path:
    text = raw.strip().strip('"')
    if not text:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有选择目录",
            "素材库目录不能为空。",
            ["填写一个绝对路径，例如 D:/aivs/素材库", "或用「浏览…」选一个文件夹"],
        )
    try:
        return Path(text).expanduser().resolve()
    except OSError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "目录路径无效",
            f"{type(exc).__name__}: {exc}",
            ["检查路径是否含非法字符", "改用绝对路径"],
        ) from exc


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "素材库清单无法读取",
            f"{path.name}: {type(exc).__name__}: {exc}",
            ["确认文件未被其他程序占用", f"从备份恢复 {MANIFEST_NAME}"],
        ) from exc
    if not isinstance(data, dict) or data.get("kind") != MANIFEST_KIND:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "这个清单不是素材库",
            f"{path.name} 缺少 kind={MANIFEST_KIND} 标记（工程目录不能当素材库用）。",
            ["选一个空目录新建素材库", "工程目录请用「打开项目」"],
        )
    return data


def _write_manifest(directory: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再替换：断电或磁盘满不会留下半截清单。"""
    target = directory / MANIFEST_NAME
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise AppError(
            ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
            "素材库清单写入失败",
            f"{target}: {type(exc).__name__}: {exc}",
            ["确认磁盘可写且空间充足", "确认目录未被安全软件锁定"],
        ) from exc


def _ensure_layout(directory: Path) -> None:
    ensure_dir(directory, "素材库目录")
    for sub in SUBDIRS:
        ensure_dir(directory / sub, "素材库目录")


def is_our_library_db(db_path: Path) -> bool:
    """这个 library.db 是不是我们的：有 library_meta 表就是。

    库不走 alembic，所以判据不能像工程那样看 alembic_version。
    """
    return "library_meta" in migrate.table_names(db_path)


def _occupied(directory: Path) -> AppError:
    return AppError(
        ErrorCode.CONFLICT,
        "目录已被占用",
        f"{directory} 下存在无法识别的 {DB_NAME}。",
        OCCUPIED_SUGGESTIONS,
        {"dir": directory.as_posix()},
    )


@dataclass(slots=True)
class OpenLibrary:
    """已打开的素材库。全进程最多一个——它是应用级的，不属于任何工程。"""

    id: str
    name: str
    dir: Path
    db: Database
    schema_version: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "dir": self.dir.as_posix(),
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class LibraryService:
    """素材库的生命周期 + 库内 CRUD。与 ProjectService 同构的进程内单例。"""

    def __init__(self) -> None:
        self._open: OpenLibrary | None = None
        self._lock = asyncio.Lock()

    # --- 位置记忆 ---
    # 库路径是「这台机器上素材库在哪」，和 recent.json 一样属于应用级状态，
    # 所以放 runtime_dir（测试里 clean_runtime 把它指到 tmp_path，天然隔离）。

    @property
    def _state_path(self) -> Path:
        return settings.runtime_dir / "library.json"

    def remembered(self) -> str | None:
        path = self._state_path
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("library.state_unreadable", path=str(path), error=str(exc))
            return None
        directory = data.get("dir") if isinstance(data, dict) else None
        return str(directory) if directory else None

    def _remember(self, directory: Path | None) -> None:
        try:
            if directory is None:
                self._state_path.unlink(missing_ok=True)
                return
            self._state_path.write_text(
                json.dumps({"dir": directory.as_posix(), "saved_at": utc_now()}, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:  # 记不住位置不该让「设置素材库」整件事失败
            log.warning("library.state_unwritable", error=str(exc))

    # --- 生命周期 ---

    async def configure(self, directory: str) -> OpenLibrary:
        """把某个目录设成素材库：空目录就建一个，已经是库就打开它。"""
        target = _resolve_dir(directory)
        async with self._lock:
            return await self._open_at(target)

    async def _open_at(self, target: Path) -> OpenLibrary:
        if self._open is not None and self._open.dir == target:
            return self._open  # 幂等：反复 configure 同一个目录不重开库
        # noqa 说明：都是本地磁盘元数据探测，开销可忽略，不值得为它引入 anyio.Path
        if target.exists() and not target.is_dir():  # noqa: ASYNC240
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "选中的不是目录",
                f"{target} 是一个文件。",
                ["选择一个目录，而不是文件"],
            )

        manifest_path = target / MANIFEST_NAME
        db_path = target / DB_NAME
        fresh = not manifest_path.exists()
        if fresh and db_path.exists() and not is_our_library_db(db_path):
            raise _occupied(target)

        name = target.name or "素材库"
        lib_id = new_id("library")
        now = utc_now()
        if not fresh:
            manifest = _read_manifest(manifest_path)
            stored = int(manifest.get("schema_version") or 0)
            if stored > LIBRARY_SCHEMA:
                raise AppError(
                    ErrorCode.SCHEMA_MISMATCH,
                    "素材库由更新版本的应用创建",
                    f"库 schema {stored}，当前应用只支持 {LIBRARY_SCHEMA}。",
                    ["升级应用后再打开这个库", "或换一个目录新建素材库"],
                    {"dir": target.as_posix()},
                )
            lib_id = str(manifest.get("id") or lib_id)
            name = str(manifest.get("name") or name)

        _ensure_layout(target)
        db = Database(target / DB_NAME)
        # create_all 幂等且只增表——这就是库不走 alembic 的那个取舍
        await db.create_all(LibraryBase.metadata)
        rows = await fetch_all(db, LibraryMeta)
        row = rows[0] if rows else None
        if row is None:
            row = LibraryMeta(
                id=lib_id,
                name=name,
                schema_version=LIBRARY_SCHEMA,
                created_at=now,
                updated_at=now,
            )
            async with db.write() as session:
                session.add(row)
        elif row.schema_version != LIBRARY_SCHEMA:
            async with db.write() as session:
                held = await session.get(LibraryMeta, row.id)
                assert held is not None
                held.schema_version = LIBRARY_SCHEMA
                held.updated_at = now

        lib = OpenLibrary(
            id=row.id,
            name=row.name,
            dir=target,
            db=db,
            schema_version=LIBRARY_SCHEMA,
            created_at=row.created_at,
            updated_at=now,
        )
        if self._open is not None:  # 换库：老的先关掉，全进程只留一个
            await self._open.db.close()
        self._open = lib
        _write_manifest(target, self._manifest_of(lib))
        self._remember(target)
        log.info("library.opened", id=lib.id, dir=target.as_posix(), fresh=fresh)
        bus.emit(Channel.SYSTEM, "library.opened", lib.to_dict())
        return lib

    def _manifest_of(self, lib: OpenLibrary) -> dict[str, Any]:
        return {
            "kind": MANIFEST_KIND,
            "app": settings.app_name,
            "id": lib.id,
            "name": lib.name,
            "schema_version": lib.schema_version,
            "created_at": lib.created_at,
            "updated_at": lib.updated_at,
        }

    async def current(self) -> OpenLibrary:
        """拿到已打开的库；记过位置就懒重开（后端重启后不该逼用户再选一次目录）。"""
        if self._open is not None:
            return self._open
        remembered = self.remembered()
        if not remembered:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "还没有设置素材库",
                "素材库是应用级的，需要先指定一个目录——不会自动替你建。",
                ["在素材库页选择一个目录", "选空目录会被初始化成新素材库"],
            )
        target = _resolve_dir(remembered)
        if not (target / MANIFEST_NAME).exists():
            raise AppError(
                ErrorCode.NOT_FOUND,
                "记住的素材库目录不见了",
                f"{target} 下没有 {MANIFEST_NAME}（目录可能被移动或改名了）。",
                [RESELECT, "把目录移回原处后重试"],
                {"dir": target.as_posix()},
            )
        async with self._lock:
            if self._open is not None:
                return self._open
            return await self._open_at(target)

    async def status(self) -> dict[str, Any]:
        """GET /library 的载荷。「没配置」不是错误——前端靠它决定画不画引导。"""
        if self._open is None and self.remembered():
            await self.current()  # 目录被删/改名时这里会结构化报错，不吞
        if self._open is None:
            return {"configured": False, "remembered_dir": None, "library": None}
        return {
            "configured": True,
            "remembered_dir": self.remembered(),
            "library": {**self._open.to_dict(), "counts": await self._counts()},
        }

    async def _counts(self) -> dict[str, int]:
        lib = self._open
        assert lib is not None
        return {
            "assets": len(await fetch_all(lib.db, LibAsset)),
            "characters": len(await fetch_all(lib.db, LibCharacter)),
            "locations": len(await fetch_all(lib.db, LibLocation)),
            "props": len(await fetch_all(lib.db, LibProp)),
            "tags": len(await fetch_all(lib.db, LibTag)),
        }

    async def shutdown(self) -> None:
        """进程退出时收尾：只 dispose 连接，**不忘记**库的位置。

        与 `close()` 的区别就是这一点——退出不是「不用这个库了」，下次启动还得打开它。
        SQLite WAL 需要正常 dispose 才会把 -wal 合并回主文件。
        """
        lib = self._open
        self._open = None
        if lib is not None:
            await lib.db.close()
            log.info("library.shutdown", id=lib.id)

    async def close(self) -> dict[str, Any]:
        """不再使用当前素材库：关库并忘掉位置。

        忘掉是必须的——`current()` 会按记住的位置懒重开，只关不忘等于什么都没做。
        工程侧不受影响：采用是单向复制，工程里的副本与库再无关系。
        """
        lib = self._open
        self._open = None
        self._remember(None)
        if lib is not None:
            await lib.db.close()
            log.info("library.closed", id=lib.id)
            bus.emit(Channel.SYSTEM, "library.closed", {"id": lib.id})
        return {"configured": False, "remembered_dir": None, "library": None}

    # --- 素材 ---

    def _kind_rel(self, kind: str) -> str:
        rel = LIB_KIND_DIR.get(kind)
        if rel is None:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这个素材类型不在库的范围内",
                f"{kind} 不是库支持的类型（生成物与代理流属于工程，库里不收）。",
                [f"改用其中之一：{'、'.join(LIB_KIND_DIR)}"],
                {"kind": kind},
            )
        return rel

    async def by_sha1(self, sha1: str) -> dict[str, Any] | None:
        lib = await self.current()
        async with lib.db.read() as session:
            row = (
                (await session.execute(select(LibAsset).where(LibAsset.sha1 == sha1)))
                .scalars()
                .first()
            )
        return as_dict(row) if row else None

    async def upload(
        self, kind: str, filename: str, data: bytes, title: str | None = None
    ) -> dict[str, Any]:
        """上传字节到库。同内容重复上传直接复用已有素材（sha1 去重）。"""
        lib = await self.current()
        sha1 = require_bytes(data, filename)
        existing = await self.by_sha1(sha1)
        if existing is not None:
            return existing
        target = ensure_dir(lib.dir / self._kind_rel(kind), "素材库目录") / content_name(
            sha1, Path(filename).suffix
        )
        write_file(target, data)
        return await self._insert(kind, target, sha1, "manual", filename, title)

    async def register(self, kind: str, src: str, title: str | None = None) -> dict[str, Any]:
        """登记磁盘上已有的文件。库**永远复制**：装着指针的库经不起原文件被移动。"""
        lib = await self.current()
        path = source_file(src)
        sha1 = sha1_of_file(path)
        existing = await self.by_sha1(sha1)
        if existing is not None:
            return existing
        target = ensure_dir(lib.dir / self._kind_rel(kind), "素材库目录") / content_name(
            sha1, path.suffix
        )
        try:
            shutil.copy2(path, target)
        except OSError as exc:
            raise AppError(
                ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
                "素材复制进库失败",
                f"{path} → {target}: {type(exc).__name__}: {exc}",
                ["确认磁盘空间充足", "确认源文件未被其他程序占用"],
            ) from exc
        return await self._insert(kind, target, sha1, "manual", path.name, title)

    async def _insert(
        self,
        kind: str,
        target: Path,
        sha1: str,
        source: str,
        filename: str,
        title: str | None,
    ) -> dict[str, Any]:
        lib = await self.current()
        width, height = sniff_size(target)
        row = LibAsset(
            id=new_id("library_asset"),
            kind=kind,
            path=target.relative_to(lib.dir).as_posix(),
            mime=None,
            width=width,
            height=height,
            size_bytes=target.stat().st_size,  # noqa: ASYNC240 - 本地磁盘元数据，开销可忽略
            sha1=sha1,
            source=source,
            title=(title or "").strip() or filename,
            meta_json=dump_json({"filename": filename, "media": kind_of_suffix(target.suffix)}),
            created_at=utc_now(),
        )
        async with lib.db.write() as session:
            session.add(row)
        bus.emit(Channel.ASSET, "library.asset.created", {"id": row.id, "kind": kind})
        return as_dict(row)

    async def _tags_by_owner(self, owner_kind: str) -> dict[str, list[dict[str, Any]]]:
        lib = await self.current()
        tags = {t.id: t for t in await fetch_all(lib.db, LibTag)}
        out: dict[str, list[dict[str, Any]]] = {}
        for link in await fetch_all(lib.db, LibTagLink, where=LibTagLink.owner_kind == owner_kind):
            tag = tags.get(link.tag_id)
            if tag is not None:
                out.setdefault(link.owner_id, []).append(
                    {"id": tag.id, "name": tag.name, "color": tag.color}
                )
        return out

    async def list_assets(
        self, kind: str | None = None, tag: str | None = None
    ) -> list[dict[str, Any]]:
        """列素材。missing 标出文件被外部删掉的行——库是长期资产，腐烂必须看得见。"""
        lib = await self.current()
        rows = await fetch_all(lib.db, LibAsset, order_by=LibAsset.created_at.desc())
        refs = await fetch_all(lib.db, LibAssetRef)
        counts: dict[str, int] = {}
        for ref in refs:
            counts[ref.asset_id] = counts.get(ref.asset_id, 0) + 1
        tags = await self._tags_by_owner("asset")
        out: list[dict[str, Any]] = []
        for row in rows:
            mine = tags.get(row.id, [])
            if kind and row.kind != kind:
                continue
            if tag and tag not in {t["name"] for t in mine}:
                continue
            out.append(
                {
                    **as_dict(row),
                    "ref_count": counts.get(row.id, 0),
                    "missing": not (lib.dir / row.path).exists(),
                    "tags": mine,
                }
            )
        return out

    async def asset_path(self, aid: str) -> Path:
        """库内素材的绝对路径。「采用」把它交给工程侧的 register_path 去复制。"""
        lib = await self.current()
        row = await fetch(lib.db, LibAsset, aid, "库素材")
        path = lib.dir / row.path
        if not path.is_file():
            raise AppError(
                ErrorCode.NOT_FOUND,
                "库里的素材文件不见了",
                f"{path} 不存在（登记还在，文件可能被库外的程序删了）。",
                ["重新上传这个素材", "或从库里删掉这条登记"],
                {"asset_id": aid},
            )
        return path

    async def update_asset(self, aid: str, patch: dict[str, Any]) -> dict[str, Any]:
        """只能改人写的那两个字段：title / note。落盘事实（path / sha1）不可改。"""
        lib = await self.current()
        await fetch(lib.db, LibAsset, aid, "库素材")
        async with lib.db.write() as session:
            row = await session.get(LibAsset, aid)
            assert row is not None
            for key in ("title", "note"):
                if key in patch:
                    setattr(row, key, patch[key])
            return as_dict(row)

    async def refs_of(self, aid: str) -> list[dict[str, Any]]:
        lib = await self.current()
        rows = await fetch_all(lib.db, LibAssetRef, where=LibAssetRef.asset_id == aid)
        return [as_dict(r) for r in rows]

    async def link(self, aid: str, owner_kind: str, owner_id: str, role: str | None = None) -> None:
        lib = await self.current()
        await fetch(lib.db, LibAsset, aid, "库素材")
        async with lib.db.write() as session:
            session.add(
                LibAssetRef(
                    id=new_id("library_asset_ref"),
                    asset_id=aid,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    role=role,
                    created_at=utc_now(),
                )
            )

    async def _unlink_owner(self, owner_id: str) -> None:
        """预设被删时清掉它的引用与标签，否则「能不能删素材」会一直被幽灵挡住。"""
        lib = await self.current()
        refs = await fetch_all(lib.db, LibAssetRef, where=LibAssetRef.owner_id == owner_id)
        links = await fetch_all(lib.db, LibTagLink, where=LibTagLink.owner_id == owner_id)
        async with lib.db.write() as session:
            for ref in refs:
                held = await session.get(LibAssetRef, ref.id)
                if held is not None:
                    await session.delete(held)
            for link in links:
                held_link = await session.get(LibTagLink, link.id)
                if held_link is not None:
                    await session.delete(held_link)

    async def delete_asset(self, aid: str, force: bool = False) -> dict[str, Any]:
        """删素材。仍被库内预设引用时默认拒绝，并说清是谁在用。"""
        lib = await self.current()
        row = await fetch(lib.db, LibAsset, aid, "库素材")
        refs = await self.refs_of(aid)
        protected: list[dict[str, Any]] = []
        for ref in refs:
            if ref["owner_kind"] == "appearance":
                appearance = await fetch(lib.db, LibAppearance, ref["owner_id"], "形象预设")
                sheets = await fetch_all(
                    lib.db, LibSheet, where=LibSheet.appearance_id == appearance.id
                )
                current = next((sheet for sheet in sheets if sheet.is_current), None)
                if appearance.is_default and current is not None and current.asset_id == aid:
                    protected.append(ref)
            elif ref["owner_kind"] == "location_variant":
                variant = await fetch(lib.db, LibLocationVariant, ref["owner_id"], "地点变体")
                variant_refs = await fetch_all(
                    lib.db,
                    LibLocationReference,
                    where=LibLocationReference.variant_id == variant.id,
                )
                current = next((item for item in variant_refs if item.is_current), None)
                if variant.name == "默认场景" and current is not None and current.asset_id == aid:
                    protected.append(ref)
            elif ref["owner_kind"] == "prop":
                prop_refs = await fetch_all(
                    lib.db, LibPropReference, where=LibPropReference.prop_id == ref["owner_id"]
                )
                current = next((item for item in prop_refs if item.is_current), None)
                if current is not None and current.asset_id == aid:
                    protected.append(ref)
        if protected:
            raise AppError(
                ErrorCode.CONFLICT,
                "默认参考图不能删除",
                "这是角色、地点或道具的默认定妆图 / 参考图，必须先替换默认图。",
                ["在对应预设上挂一张新图替换默认图", "其余历史定妆图可以单独删除"],
                {"asset_id": aid, "protected_refs": protected, "protected_default": True},
            )
        if refs and not force:
            owners = "、".join(sorted({f"{r['owner_kind']}:{r['owner_id']}" for r in refs}))
            raise AppError(
                ErrorCode.CONFLICT,
                "该素材仍被库内预设引用",
                f"有 {len(refs)} 处在用它：{owners}。删除会让这些预设缺图。",
                [
                    "先解除引用，再删除素材",
                    "或改用「强制删除」并接受这些预设将缺图",
                    "已采用进工程的副本不受影响——采用是单向复制",
                ],
                {"asset_id": aid},
            )
        links = await fetch_all(lib.db, LibTagLink, where=LibTagLink.owner_id == aid)
        file_path = lib.dir / row.path
        async with lib.db.write() as session:
            fresh = await session.get(LibAsset, aid)
            if fresh is not None:
                await session.delete(fresh)
            for link in links:
                held = await session.get(LibTagLink, link.id)
                if held is not None:
                    await session.delete(held)
        removed = False
        if file_path.is_file():
            try:
                file_path.unlink()
                removed = True
            except OSError as exc:  # 登记已删、文件删不掉要说出来，不假装成功
                bus.emit(
                    Channel.ERROR,
                    "library.asset.file_kept",
                    {"asset_id": aid, "error": str(exc)},
                )
        bus.emit(Channel.ASSET, "library.asset.deleted", {"id": aid})
        return {"id": aid, "file_removed": removed, "broken_refs": len(refs) if force else 0}

    # --- 标签 ---
    # 库会越攒越大，没有标签就只能靠肉眼翻。工程侧没有这一层，也不需要。

    def _check_owner_kind(self, owner_kind: str) -> None:
        if owner_kind not in TAG_OWNERS:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "标签挂不到这种东西上",
                f"{owner_kind} 不在可打标签的类型里。",
                [f"改用其中之一：{'、'.join(TAG_OWNERS)}"],
                {"owner_kind": owner_kind},
            )

    async def list_tags(self) -> list[dict[str, Any]]:
        lib = await self.current()
        counts: dict[str, int] = {}
        for link in await fetch_all(lib.db, LibTagLink):
            counts[link.tag_id] = counts.get(link.tag_id, 0) + 1
        rows = await fetch_all(lib.db, LibTag, order_by=LibTag.name)
        return [{**as_dict(r), "usage": counts.get(r.id, 0)} for r in rows]

    async def create_tag(self, patch: dict[str, Any]) -> dict[str, Any]:
        lib = await self.current()
        name = require_name(patch.get("name"), "标签", "水墨")
        async with lib.db.read() as session:
            dupe = (
                (await session.execute(select(LibTag).where(LibTag.name == name))).scalars().first()
            )
        if dupe is not None:
            raise AppError(
                ErrorCode.CONFLICT,
                "标签已存在",
                f"库里已经有一个叫「{name}」的标签。",
                ["直接用已有的那个标签", "或换一个名字"],
                {"tag_id": dupe.id},
            )
        row = LibTag(
            id=new_id("library_tag"),
            name=name,
            color=patch.get("color"),
            created_at=utc_now(),
        )
        async with lib.db.write() as session:
            session.add(row)
        return as_dict(row)

    async def delete_tag(self, tid: str) -> None:
        """删标签只是取消分类，挂过它的素材与预设一个都不动（链表随 FK 级联走）。"""
        lib = await self.current()
        await fetch(lib.db, LibTag, tid, "标签")
        async with lib.db.write() as session:
            fresh = await session.get(LibTag, tid)
            if fresh is not None:
                await session.delete(fresh)

    async def attach_tag(self, tid: str, owner_kind: str, owner_id: str) -> dict[str, Any]:
        lib = await self.current()
        self._check_owner_kind(owner_kind)
        await fetch(lib.db, LibTag, tid, "标签")
        same = [
            link
            for link in await fetch_all(lib.db, LibTagLink, where=LibTagLink.tag_id == tid)
            if link.owner_id == owner_id
        ]
        if same:
            return as_dict(same[0])  # 幂等：重复打同一个标签不产生第二条链
        row = LibTagLink(
            id=new_id("library_tag_link"),
            tag_id=tid,
            owner_kind=owner_kind,
            owner_id=owner_id,
            created_at=utc_now(),
        )
        async with lib.db.write() as session:
            session.add(row)
        return as_dict(row)

    async def detach_tag(self, tid: str, owner_id: str) -> None:
        lib = await self.current()
        links = [
            link
            for link in await fetch_all(lib.db, LibTagLink, where=LibTagLink.tag_id == tid)
            if link.owner_id == owner_id
        ]
        async with lib.db.write() as session:
            for link in links:
                held = await session.get(LibTagLink, link.id)
                if held is not None:
                    await session.delete(held)

    # --- 角色预设 ---
    # 列名与工程侧 character / appearance / sheet_version 一一对应：
    # 采用时把这里的行按同名字段喂给 cast.* 已有的写路径，不需要翻译。

    def _appearance_dict(self, row: LibAppearance, sheets: list[LibSheet]) -> dict[str, Any]:
        mine = [s for s in sheets if s.appearance_id == row.id]
        current = next((s for s in mine if s.is_current), None)
        return {
            **as_dict(row),
            "overrides": sorted(f for f in (row.overrides or "").split(",") if f),
            "sheet_count": len(mine),
            "current_sheet": as_dict(current) if current else None,
            "sheets": [as_dict(sheet) for sheet in sorted(mine, key=lambda item: item.version_no)],
        }

    async def list_characters(self) -> list[dict[str, Any]]:
        lib = await self.current()
        chars = await fetch_all(lib.db, LibCharacter, order_by=LibCharacter.created_at)
        apps = await fetch_all(lib.db, LibAppearance, order_by=LibAppearance.created_at)
        sheets = await fetch_all(lib.db, LibSheet, order_by=LibSheet.version_no)
        tags = await self._tags_by_owner("character")
        return [
            {
                **as_dict(char),
                "tags": tags.get(char.id, []),
                "appearances": [
                    self._appearance_dict(a, sheets) for a in apps if a.character_id == char.id
                ],
            }
            for char in chars
        ]

    async def create_character(self, patch: dict[str, Any]) -> dict[str, Any]:
        lib = await self.current()
        name = require_name(patch.get("name"), "角色预设", "林昭")
        default_asset_id = str(patch.get("default_asset_id") or "").strip()
        await self._default_asset(default_asset_id, "character_sheet")
        now = utc_now()
        row = LibCharacter(
            id=new_id("library_character"),
            **{k: patch.get(k) for k in CHARACTER_FIELDS if k != "name"},
            name=name,
            created_at=now,
            updated_at=now,
        )
        async with lib.db.write() as session:
            session.add(row)
        appearance = await self.create_appearance(row.id, {"name": "默认形象"}, default=True)
        await self.add_sheet(appearance["id"], default_asset_id)
        return as_dict(row)

    async def _default_asset(self, asset_id: str, expected_kind: str) -> dict[str, Any]:
        if not asset_id:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "必须设置默认参考图",
                "新建角色、地点或道具时必须选择一张默认定妆图 / 参考图。",
                [f"先上传 {expected_kind} 类型素材", "再在新建弹窗中选择默认图"],
            )
        lib = await self.current()
        asset = await fetch(lib.db, LibAsset, asset_id, "库素材")
        if asset.kind not in {expected_kind, "upload"}:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "默认参考图类型不匹配",
                f"当前素材类型是 {asset.kind}，不能作为 {expected_kind} 使用。",
                [f"选择 {expected_kind} 类型素材", "或重新上传并选择正确类型"],
            )
        return as_dict(asset)

    async def update_character(self, cid: str, patch: dict[str, Any]) -> dict[str, Any]:
        lib = await self.current()
        await fetch(lib.db, LibCharacter, cid, "角色预设")
        async with lib.db.write() as session:
            row = await session.get(LibCharacter, cid)
            assert row is not None
            for key in CHARACTER_FIELDS:
                if key in patch:
                    setattr(row, key, patch[key])
            row.updated_at = utc_now()
            return as_dict(row)

    async def delete_character(self, cid: str) -> None:
        """删角色预设。形象与定妆图随 FK 级联，素材文件本身一个不动。"""
        lib = await self.current()
        await fetch(lib.db, LibCharacter, cid, "角色预设")
        apps = await fetch_all(lib.db, LibAppearance, where=LibAppearance.character_id == cid)
        for app in apps:
            await self._unlink_owner(app.id)
        await self._unlink_owner(cid)
        async with lib.db.write() as session:
            fresh = await session.get(LibCharacter, cid)
            if fresh is not None:
                await session.delete(fresh)

    async def create_appearance(
        self,
        cid: str,
        patch: dict[str, Any],
        *,
        parent_id: str | None = None,
        default: bool = False,
    ) -> dict[str, Any]:
        lib = await self.current()
        await fetch(lib.db, LibCharacter, cid, "角色预设")
        if parent_id:
            parent = await fetch(lib.db, LibAppearance, parent_id, "父形象")
            if parent.character_id != cid:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "父形象不属于这个角色预设",
                    f"形象 {parent_id} 属于角色 {parent.character_id}。",
                    ["只能从同一角色预设的形象派生"],
                )
        now = utc_now()
        # 派生时显式填了值的字段即为覆写，其余留空表示继承（与工程侧同一套规则）
        overrides = sorted(f for f in INHERITABLE if parent_id and patch.get(f) not in (None, ""))
        row = LibAppearance(
            id=new_id("library_appearance"),
            character_id=cid,
            parent_id=parent_id,
            name=str(patch.get("name") or "").strip() or "新形象",
            **{f: patch.get(f) for f in INHERITABLE},
            overrides=",".join(overrides),
            is_default=1 if default else 0,
            created_at=now,
            updated_at=now,
        )
        async with lib.db.write() as session:
            session.add(row)
        return as_dict(row)

    async def update_appearance(self, aid: str, patch: dict[str, Any]) -> dict[str, Any]:
        lib = await self.current()
        await fetch(lib.db, LibAppearance, aid, "形象预设")
        async with lib.db.write() as session:
            row = await session.get(LibAppearance, aid)
            assert row is not None
            overrides = {f for f in (row.overrides or "").split(",") if f}
            for key in APPEARANCE_FIELDS:
                if key not in patch:
                    continue
                setattr(row, key, patch[key])
                if key in INHERITABLE and row.parent_id is not None:
                    overrides.add(key)
            row.overrides = ",".join(sorted(overrides))
            row.updated_at = utc_now()
            return as_dict(row)

    async def delete_appearance(self, aid: str) -> None:
        lib = await self.current()
        appearance = await fetch(lib.db, LibAppearance, aid, "形象预设")
        if appearance.is_default:
            raise AppError(
                ErrorCode.CONFLICT,
                "默认形象不能删除",
                "角色必须保留一个默认形象及其定妆图。",
                ["修改默认形象", "另建形象后删除非默认形象"],
            )
        await self._unlink_owner(aid)
        async with lib.db.write() as session:
            fresh = await session.get(LibAppearance, aid)
            if fresh is not None:
                await session.delete(fresh)

    async def add_sheet(self, aid: str, asset_id: str) -> dict[str, Any]:
        """给形象预设挂定妆图。与工程侧一样只增版本，旧版本永不覆盖。"""
        lib = await self.current()
        await fetch(lib.db, LibAppearance, aid, "形象预设")
        await fetch(lib.db, LibAsset, asset_id, "库素材")
        existing = await fetch_all(lib.db, LibSheet, where=LibSheet.appearance_id == aid)
        row = LibSheet(
            id=new_id("library_sheet"),
            appearance_id=aid,
            version_no=max((s.version_no for s in existing), default=0) + 1,
            asset_id=asset_id,
            source="manual",
            is_current=1,
            created_at=utc_now(),
        )
        async with lib.db.write() as session:
            for old in existing:  # 旧版本保留，只是不再是「当前」
                held = await session.get(LibSheet, old.id)
                if held is not None:
                    held.is_current = 0
            session.add(row)
        await self.link(asset_id, "appearance", aid, role="sheet")
        return as_dict(row)

    async def delete_sheet(self, sheet_id: str) -> None:
        lib = await self.current()
        sheet = await fetch(lib.db, LibSheet, sheet_id, "定妆图")
        appearance = await fetch(lib.db, LibAppearance, sheet.appearance_id, "形象预设")
        if appearance.is_default and sheet.is_current:
            raise AppError(
                ErrorCode.CONFLICT,
                "默认定妆图不能删除",
                "默认形象必须始终保留一张当前定妆图；挂新图即可替换它。",
                ["先给默认形象挂一张新定妆图", "再删除旧的历史版本"],
                {"protected_default": True},
            )
        siblings = await fetch_all(lib.db, LibSheet, where=LibSheet.appearance_id == appearance.id)
        await self._unlink_asset_ref(sheet.asset_id, "appearance", appearance.id, "sheet")
        async with lib.db.write() as session:
            if sheet.is_current:
                previous = max(
                    (item for item in siblings if item.id != sheet.id),
                    key=lambda item: item.version_no,
                    default=None,
                )
                if previous is not None:
                    held_previous = await session.get(LibSheet, previous.id)
                    if held_previous is not None:
                        held_previous.is_current = 1
            fresh = await session.get(LibSheet, sheet_id)
            if fresh is not None:
                await session.delete(fresh)

    async def _unlink_asset_ref(
        self, asset_id: str | None, owner_kind: str, owner_id: str, role: str
    ) -> None:
        if not asset_id:
            return
        lib = await self.current()
        refs = await fetch_all(
            lib.db,
            LibAssetRef,
            where=(
                (LibAssetRef.asset_id == asset_id)
                & (LibAssetRef.owner_kind == owner_kind)
                & (LibAssetRef.owner_id == owner_id)
                & (LibAssetRef.role == role)
            ),
        )
        async with lib.db.write() as session:
            if refs:
                held = await session.get(LibAssetRef, refs[0].id)
                if held is not None:
                    await session.delete(held)

    # --- 地点预设 ---
    # 库里删地点不需要像工程那样查 Scene 引用：库不持有任何镜头数据。

    async def list_locations(self) -> list[dict[str, Any]]:
        lib = await self.current()
        locs = await fetch_all(lib.db, LibLocation, order_by=LibLocation.created_at)
        variants = await fetch_all(
            lib.db, LibLocationVariant, order_by=LibLocationVariant.created_at
        )
        refs = await fetch_all(lib.db, LibLocationReference)
        tags = await self._tags_by_owner("location")
        return [
            {
                **as_dict(loc),
                "tags": tags.get(loc.id, []),
                "variants": [
                    {
                        **as_dict(v),
                        "reference_count": len([r for r in refs if r.variant_id == v.id]),
                        "references": [
                            as_dict(r)
                            for r in sorted(
                                [item for item in refs if item.variant_id == v.id],
                                key=lambda item: item.created_at,
                            )
                        ],
                        "current_reference": next(
                            (as_dict(r) for r in refs if r.variant_id == v.id and r.is_current),
                            None,
                        ),
                    }
                    for v in variants
                    if v.location_id == loc.id
                ],
            }
            for loc in locs
        ]

    async def create_location(self, patch: dict[str, Any]) -> dict[str, Any]:
        lib = await self.current()
        default_asset_id = str(patch.get("default_asset_id") or "").strip()
        await self._default_asset(default_asset_id, "location_reference")
        now = utc_now()
        row = LibLocation(
            id=new_id("library_location"),
            name=require_name(patch.get("name"), "地点预设", "城南旧宅"),
            description=patch.get("description"),
            notes=patch.get("notes"),
            created_at=now,
            updated_at=now,
        )
        async with lib.db.write() as session:
            session.add(row)
        variant = await self.create_variant(row.id, {"name": "默认场景"})
        await self.add_variant_reference(variant["id"], default_asset_id)
        return as_dict(row)

    async def update_location(self, lid: str, patch: dict[str, Any]) -> dict[str, Any]:
        lib = await self.current()
        await fetch(lib.db, LibLocation, lid, "地点预设")
        async with lib.db.write() as session:
            row = await session.get(LibLocation, lid)
            assert row is not None
            for key in LOCATION_FIELDS:
                if key in patch:
                    setattr(row, key, patch[key])
            row.updated_at = utc_now()
            return as_dict(row)

    async def delete_location(self, lid: str) -> None:
        lib = await self.current()
        await fetch(lib.db, LibLocation, lid, "地点预设")
        variants = await fetch_all(
            lib.db, LibLocationVariant, where=LibLocationVariant.location_id == lid
        )
        for variant in variants:
            await self._unlink_owner(variant.id)
        await self._unlink_owner(lid)
        async with lib.db.write() as session:
            fresh = await session.get(LibLocation, lid)
            if fresh is not None:
                await session.delete(fresh)

    async def create_variant(self, lid: str, patch: dict[str, Any]) -> dict[str, Any]:
        lib = await self.current()
        await fetch(lib.db, LibLocation, lid, "地点预设")
        now = utc_now()
        row = LibLocationVariant(
            id=new_id("library_location_variant"),
            location_id=lid,
            name=require_name(patch.get("name"), "变体", "雨夜"),
            **{k: patch.get(k) for k in VARIANT_FIELDS if k != "name"},
            created_at=now,
            updated_at=now,
        )
        async with lib.db.write() as session:
            session.add(row)
        return as_dict(row)

    async def update_variant(self, vid: str, patch: dict[str, Any]) -> dict[str, Any]:
        lib = await self.current()
        await fetch(lib.db, LibLocationVariant, vid, "地点变体预设")
        async with lib.db.write() as session:
            row = await session.get(LibLocationVariant, vid)
            assert row is not None
            for key in VARIANT_FIELDS:
                if key in patch:
                    setattr(row, key, patch[key])
            row.updated_at = utc_now()
            return as_dict(row)

    async def delete_variant(self, vid: str) -> None:
        lib = await self.current()
        variant = await fetch(lib.db, LibLocationVariant, vid, "地点变体预设")
        if variant.name == "默认场景":
            raise AppError(
                ErrorCode.CONFLICT,
                "默认场景不能删除",
                "地点必须保留默认场景及其当前参考图。",
                ["修改默认场景", "新增其他场景变体"],
                {"protected_default": True},
            )
        await self._unlink_owner(vid)
        async with lib.db.write() as session:
            fresh = await session.get(LibLocationVariant, vid)
            if fresh is not None:
                await session.delete(fresh)

    async def add_variant_reference(
        self, vid: str, asset_id: str, camera: str | None = None, note: str | None = None
    ) -> dict[str, Any]:
        lib = await self.current()
        await fetch(lib.db, LibLocationVariant, vid, "地点变体预设")
        await fetch(lib.db, LibAsset, asset_id, "库素材")
        existing = await fetch_all(
            lib.db,
            LibLocationReference,
            where=LibLocationReference.variant_id == vid,
        )
        row = LibLocationReference(
            id=new_id("library_location_reference"),
            variant_id=vid,
            asset_id=asset_id,
            camera=camera,
            note=note,
            is_current=1,
            created_at=utc_now(),
        )
        async with lib.db.write() as session:
            for old in existing:
                held = await session.get(LibLocationReference, old.id)
                if held is not None:
                    held.is_current = 0
            session.add(row)
        await self.link(asset_id, "location_variant", vid, role="reference")
        return as_dict(row)

    async def variant_references(self, vid: str) -> list[dict[str, Any]]:
        lib = await self.current()
        rows = await fetch_all(
            lib.db,
            LibLocationReference,
            where=LibLocationReference.variant_id == vid,
            order_by=LibLocationReference.created_at,
        )
        return [as_dict(r) for r in rows]

    async def delete_variant_reference(self, reference_id: str) -> None:
        lib = await self.current()
        reference = await fetch(lib.db, LibLocationReference, reference_id, "地点参考图")
        variant = await fetch(lib.db, LibLocationVariant, reference.variant_id, "地点变体预设")
        if variant.name == "默认场景" and reference.is_current:
            raise AppError(
                ErrorCode.CONFLICT,
                "默认参考图不能删除",
                "默认场景必须始终保留一张当前参考图；挂新图即可替换它。",
                ["先给默认场景挂一张新参考图", "再删除旧的历史版本"],
                {"protected_default": True},
            )
        siblings = await fetch_all(
            lib.db,
            LibLocationReference,
            where=LibLocationReference.variant_id == variant.id,
        )
        await self._unlink_asset_ref(
            reference.asset_id, "location_variant", variant.id, "reference"
        )
        async with lib.db.write() as session:
            if reference.is_current:
                previous = max(
                    (item for item in siblings if item.id != reference.id),
                    key=lambda item: item.created_at,
                    default=None,
                )
                if previous is not None:
                    held_previous = await session.get(LibLocationReference, previous.id)
                    if held_previous is not None:
                        held_previous.is_current = 1
            fresh = await session.get(LibLocationReference, reference_id)
            if fresh is not None:
                await session.delete(fresh)

    # --- 道具预设 ---

    async def list_props(self) -> list[dict[str, Any]]:
        lib = await self.current()
        props = await fetch_all(lib.db, LibProp, order_by=LibProp.created_at)
        refs = await fetch_all(lib.db, LibPropReference, order_by=LibPropReference.version_no)
        tags = await self._tags_by_owner("prop")
        out: list[dict[str, Any]] = []
        for prop in props:
            mine = [r for r in refs if r.prop_id == prop.id]
            current = next((r for r in mine if r.is_current), None)
            out.append(
                {
                    **as_dict(prop),
                    "tags": tags.get(prop.id, []),
                    "reference_count": len(mine),
                    "current_reference": as_dict(current) if current else None,
                    "references": [as_dict(item) for item in mine],
                }
            )
        return out

    async def create_prop(self, patch: dict[str, Any]) -> dict[str, Any]:
        lib = await self.current()
        default_asset_id = str(patch.get("default_asset_id") or "").strip()
        await self._default_asset(default_asset_id, "prop_reference")
        now = utc_now()
        row = LibProp(
            id=new_id("library_prop"),
            name=require_name(patch.get("name"), "道具预设", "油纸伞"),
            description=patch.get("description"),
            notes=patch.get("notes"),
            created_at=now,
            updated_at=now,
        )
        async with lib.db.write() as session:
            session.add(row)
        await self.add_prop_reference(row.id, default_asset_id)
        return as_dict(row)

    async def update_prop(self, prop_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        lib = await self.current()
        await fetch(lib.db, LibProp, prop_id, "道具预设")
        async with lib.db.write() as session:
            row = await session.get(LibProp, prop_id)
            assert row is not None
            for key in PROP_FIELDS:
                if key in patch:
                    setattr(row, key, patch[key])
            row.updated_at = utc_now()
            return as_dict(row)

    async def delete_prop(self, prop_id: str) -> None:
        lib = await self.current()
        await fetch(lib.db, LibProp, prop_id, "道具预设")
        await self._unlink_owner(prop_id)
        async with lib.db.write() as session:
            fresh = await session.get(LibProp, prop_id)
            if fresh is not None:
                await session.delete(fresh)

    async def add_prop_reference(
        self, prop_id: str, asset_id: str, note: str | None = None
    ) -> dict[str, Any]:
        """参考图只增版本，旧版本永不覆盖。"""
        lib = await self.current()
        await fetch(lib.db, LibProp, prop_id, "道具预设")
        await fetch(lib.db, LibAsset, asset_id, "库素材")
        existing = await fetch_all(
            lib.db, LibPropReference, where=LibPropReference.prop_id == prop_id
        )
        row = LibPropReference(
            id=new_id("library_prop_reference"),
            prop_id=prop_id,
            asset_id=asset_id,
            version_no=max((r.version_no for r in existing), default=0) + 1,
            note=note,
            is_current=1,
            created_at=utc_now(),
        )
        async with lib.db.write() as session:
            for old in existing:
                held = await session.get(LibPropReference, old.id)
                if held is not None:
                    held.is_current = 0
            session.add(row)
        await self.link(asset_id, "prop", prop_id, role="reference")
        return as_dict(row)

    async def delete_prop_reference(self, reference_id: str) -> None:
        lib = await self.current()
        reference = await fetch(lib.db, LibPropReference, reference_id, "道具参考图")
        if reference.is_current:
            raise AppError(
                ErrorCode.CONFLICT,
                "默认参考图不能删除",
                "道具必须始终保留一张当前参考图；挂新图即可替换它。",
                ["先给道具挂一张新参考图", "再删除旧的历史版本"],
                {"protected_default": True},
            )
        await self._unlink_asset_ref(reference.asset_id, "prop", reference.prop_id, "reference")
        async with lib.db.write() as session:
            fresh = await session.get(LibPropReference, reference_id)
            if fresh is not None:
                await session.delete(fresh)

    # --- 采用用的整份快照（services/adopt.py 照着它在工程里建副本） ---

    async def asset(self, aid: str) -> dict[str, Any]:
        lib = await self.current()
        row = await fetch(lib.db, LibAsset, aid, "库素材")
        return {**as_dict(row), "missing": not (lib.dir / row.path).exists()}

    async def character_bundle(self, cid: str) -> dict[str, Any]:
        """角色预设的完整快照：角色 + 形象链 + 每个形象的全部定妆图版本。"""
        lib = await self.current()
        char = await fetch(lib.db, LibCharacter, cid, "角色预设")
        apps = await fetch_all(
            lib.db,
            LibAppearance,
            where=LibAppearance.character_id == cid,
            order_by=LibAppearance.created_at,
        )
        sheets = await fetch_all(lib.db, LibSheet, order_by=LibSheet.version_no)
        return {
            **as_dict(char),
            "appearances": [
                {
                    **as_dict(app),
                    "sheets": [as_dict(s) for s in sheets if s.appearance_id == app.id],
                }
                for app in apps
            ],
        }

    async def location_bundle(self, lid: str) -> dict[str, Any]:
        lib = await self.current()
        loc = await fetch(lib.db, LibLocation, lid, "地点预设")
        variants = await fetch_all(
            lib.db,
            LibLocationVariant,
            where=LibLocationVariant.location_id == lid,
            order_by=LibLocationVariant.created_at,
        )
        refs = await fetch_all(
            lib.db, LibLocationReference, order_by=LibLocationReference.created_at
        )
        return {
            **as_dict(loc),
            "variants": [
                {
                    **as_dict(v),
                    "references": [as_dict(r) for r in refs if r.variant_id == v.id],
                }
                for v in variants
            ],
        }

    async def prop_bundle(self, prop_id: str) -> dict[str, Any]:
        lib = await self.current()
        prop = await fetch(lib.db, LibProp, prop_id, "道具预设")
        refs = await fetch_all(
            lib.db,
            LibPropReference,
            where=LibPropReference.prop_id == prop_id,
            order_by=LibPropReference.version_no,
        )
        return {**as_dict(prop), "references": [as_dict(r) for r in refs]}


#: 全进程唯一实例：素材库是应用级的，同一时刻只打开一个。
library = LibraryService()
