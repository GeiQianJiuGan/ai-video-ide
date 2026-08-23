"""项目容器：新建 / 打开 / 最近打开。

一个项目就是磁盘上的一个自包含目录：

    my_film/
      project.aivs.json   工程清单（人可读，可进版本控制）
      project.db          SQLite（WAL），本工程的唯一真源
      assets/             角色表、场景参考、道具图
      generations/        每次生成的输出与参数快照
      proxies/            720p 代理流，仅用于预览
      cache/              派生的临时文件（抽出来的首尾帧），删了能重新生成

两条硬规矩：
  1. 绝不覆盖用户的文件。目录里已有无法识别的 project.db 时直接报错，
     并给出「换目录 / 备份后重试 / 只读检查」三条出路。
  2. 打开旧工程时自动升级 schema，并把「从哪升到哪」明确告诉用户。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from math import gcd
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
from app.persistence.models import Project, utc_now

log = get_logger("projects")

MANIFEST_NAME = "project.aivs.json"
DB_NAME = "project.db"
SUBDIRS = ("assets", "generations", "proxies", "cache")
MANIFEST_KIND = "aivs-project"
RECENT_LIMIT = 20

#: 目录里已有别的 project.db 时给用户的三条出路（新建与打开共用同一份文案）。
OCCUPIED_SUGGESTIONS = [
    "换一个空目录新建工程",
    "把这个 project.db 备份到别处后重试",
    "以只读方式检查这个 project.db 属于哪个程序",
]


def aspect_ratio(width: int, height: int) -> str:
    d = gcd(width, height) or 1
    return f"{width // d}:{height // d}"


@dataclass(slots=True)
class OpenProject:
    """一个已打开的工程：目录、数据库句柄与本次打开时的 schema 事实。"""

    id: str
    name: str
    dir: Path
    db: Database
    width: int
    height: int
    fps: float
    duration_unit: str
    schema_version: int
    created_at: str
    updated_at: str
    #: 本次打开时从哪个 schema 升上来的；None 表示无需升级。
    migrated_from: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "dir": self.dir.as_posix(),
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "aspect_ratio": aspect_ratio(self.width, self.height),
            "duration_unit": self.duration_unit,
            "schema_version": self.schema_version,
            "migrated_from": self.migrated_from,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _resolve_dir(raw: str) -> Path:
    text = raw.strip().strip('"')
    if not text:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有选择目录",
            "工程目录不能为空。",
            ["填写一个绝对路径，例如 D:/works/my_film"],
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


def _occupied(directory: Path) -> AppError:
    return AppError(
        ErrorCode.CONFLICT,
        "目录已被占用",
        "该目录存在无法识别的 project.db。",
        OCCUPIED_SUGGESTIONS,
        {"dir": directory.as_posix()},
    )


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "工程清单无法读取",
            f"{path.name}: {type(exc).__name__}: {exc}",
            ["确认文件未被其他程序占用", "从备份恢复 project.aivs.json"],
        ) from exc
    if not isinstance(data, dict) or data.get("kind") != MANIFEST_KIND:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "工程清单不是本应用的格式",
            f"{path.name} 缺少 kind={MANIFEST_KIND} 标记。",
            ["确认选的是本应用创建的工程目录", "或改用「新建项目」"],
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
            "工程清单写入失败",
            f"{target}: {type(exc).__name__}: {exc}",
            ["确认磁盘可写且空间充足", "确认目录未被安全软件锁定"],
        ) from exc


def _ensure_layout(directory: Path) -> None:
    """建目录结构；已存在则原样保留（打开旧工程时顺手自愈缺失的子目录）。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (directory / sub).mkdir(exist_ok=True)
    except OSError as exc:
        raise AppError(
            ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
            "无法创建工程目录",
            f"{directory}: {type(exc).__name__}: {exc}",
            ["换一个有写权限的目录", "确认磁盘空间充足"],
        ) from exc


class ProjectService:
    """已打开工程的注册表。后续所有业务模块都从这里拿 Database 句柄。"""

    def __init__(self) -> None:
        self._open: dict[str, OpenProject] = {}
        self._lock = asyncio.Lock()

    # --- 查询 ---

    @property
    def opened(self) -> list[OpenProject]:
        return list(self._open.values())

    def get(self, pid: str) -> OpenProject:
        proj = self._open.get(pid)
        if proj is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "项目未打开",
                f"进程内没有 id 为 {pid} 的已打开工程（后端可能重启过）。",
                ["回到起始页重新打开该工程目录"],
                {"project_id": pid},
                status_code=404,
            )
        return proj

    def _by_dir(self, directory: Path) -> OpenProject | None:
        return next((p for p in self._open.values() if p.dir == directory), None)

    # --- 新建 ---

    async def create(
        self,
        directory: str,
        name: str,
        width: int,
        height: int,
        fps: float,
        duration_unit: str,
    ) -> OpenProject:
        target = _resolve_dir(directory)
        async with self._lock:
            if target.exists() and not target.is_dir():
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "选中的不是目录",
                    f"{target} 是一个文件。",
                    ["选择一个目录，而不是文件"],
                )
            if (target / MANIFEST_NAME).exists():
                raise AppError(
                    ErrorCode.CONFLICT,
                    "该目录已经是一个工程",
                    f"{target} 下已存在 {MANIFEST_NAME}。",
                    ["改用「打开项目」打开它", "或换一个空目录新建"],
                    {"dir": target.as_posix()},
                )
            if (target / DB_NAME).exists():
                raise _occupied(target)

            _ensure_layout(target)
            db_path = target / DB_NAME
            await asyncio.to_thread(migrate.upgrade_to_head, db_path)

            pid = new_id("project")
            now = utc_now()
            db = Database(db_path)
            async with db.write() as session:
                session.add(
                    Project(
                        id=pid,
                        name=name,
                        width=width,
                        height=height,
                        fps=fps,
                        aspect_ratio=aspect_ratio(width, height),
                        duration_unit=duration_unit,
                        preset_name=settings.video_preset or None,
                        r2v_preset_name=settings.video_preset or None,
                        flf_preset_name=settings.video_preset or None,
                        schema_version=settings.schema_version,
                        created_at=now,
                        updated_at=now,
                    )
                )

            proj = OpenProject(
                id=pid,
                name=name,
                dir=target,
                db=db,
                width=width,
                height=height,
                fps=fps,
                duration_unit=duration_unit,
                schema_version=settings.schema_version,
                created_at=now,
                updated_at=now,
            )
            _write_manifest(target, self._manifest_of(proj))
            self._open[pid] = proj
            self._remember(proj)
            log.info("project.created", id=pid, dir=target.as_posix(), name=name)
            bus.emit(Channel.SYSTEM, "project.opened", proj.to_dict(), project_id=pid)
            return proj

    def _manifest_of(self, proj: OpenProject) -> dict[str, Any]:
        return {
            "kind": MANIFEST_KIND,
            "app": settings.app_name,
            "id": proj.id,
            "name": proj.name,
            "schema_version": proj.schema_version,
            "width": proj.width,
            "height": proj.height,
            "fps": proj.fps,
            "aspect_ratio": aspect_ratio(proj.width, proj.height),
            "duration_unit": proj.duration_unit,
            "created_at": proj.created_at,
            "updated_at": proj.updated_at,
        }

    # --- 打开 ---

    async def open(self, directory: str) -> OpenProject:
        target = _resolve_dir(directory)
        async with self._lock:
            if not target.is_dir():
                raise AppError(
                    ErrorCode.NOT_FOUND,
                    "目录不存在",
                    f"{target} 不存在或不是目录。",
                    ["确认路径拼写正确", "如果是新目录，请改用「新建项目」"],
                    {"dir": target.as_posix()},
                )

            manifest_path = target / MANIFEST_NAME
            db_path = target / DB_NAME
            if not manifest_path.exists():
                if db_path.exists() and not migrate.is_our_db(db_path):
                    raise _occupied(target)
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "不是一个工程目录",
                    f"{target} 下没有 {MANIFEST_NAME}。",
                    [
                        "确认选的是工程根目录，而不是它的子目录",
                        "如果这是空目录，请改用「新建项目」",
                        f"工程根目录必须同时含 {MANIFEST_NAME} 与 {DB_NAME}",
                    ],
                    {"dir": target.as_posix()},
                )

            existing = self._by_dir(target)
            if existing is not None:
                return existing

            manifest = _read_manifest(manifest_path)
            stored = int(manifest.get("schema_version") or 0)
            if stored > settings.schema_version:
                raise AppError(
                    ErrorCode.SCHEMA_MISMATCH,
                    "工程由更新版本的应用创建",
                    f"工程 schema {stored}，当前应用只支持 {settings.schema_version}。",
                    ["升级应用后再打开", "或用创建它的版本打开"],
                    {"dir": target.as_posix()},
                )
            if db_path.exists() and not migrate.is_our_db(db_path):
                raise _occupied(target)
            if not db_path.exists():
                raise AppError(
                    ErrorCode.NOT_FOUND,
                    "工程数据库丢失",
                    f"{target} 下有工程清单，但缺少 {DB_NAME}。",
                    ["从备份恢复 project.db", "或新建工程后重新导入素材"],
                    {"dir": target.as_posix()},
                )

            _ensure_layout(target)
            await asyncio.to_thread(migrate.upgrade_to_head, db_path)

            db = Database(db_path)
            async with db.read() as session:
                row = (await session.execute(select(Project))).scalars().first()
            if row is None:
                await db.close()
                raise AppError(
                    ErrorCode.SCHEMA_MISMATCH,
                    "工程数据库缺少工程记录",
                    f"{DB_NAME} 里没有 project 行，无法确定这是哪个工程。",
                    ["从备份恢复 project.db", "或新建工程后重新导入素材"],
                    {"dir": target.as_posix()},
                )

            migrated_from = stored if stored < settings.schema_version else None
            inherited_preset = row.preset_name or settings.video_preset or None
            missing_r2v = row.r2v_preset_name is None
            missing_flf = row.flf_preset_name is None
            if (row.preset_name is None or missing_r2v or missing_flf) and inherited_preset:
                async with db.write() as session:
                    fresh = await session.get(Project, row.id)
                    if fresh is not None:
                        if fresh.preset_name is None:
                            fresh.preset_name = inherited_preset
                        if fresh.r2v_preset_name is None:
                            fresh.r2v_preset_name = inherited_preset
                        if fresh.flf_preset_name is None:
                            fresh.flf_preset_name = inherited_preset
                        fresh.updated_at = utc_now()
            proj = OpenProject(
                id=row.id,
                name=row.name,
                dir=target,
                db=db,
                width=row.width,
                height=row.height,
                fps=row.fps,
                duration_unit=row.duration_unit,
                schema_version=settings.schema_version,
                created_at=row.created_at,
                updated_at=row.updated_at,
                migrated_from=migrated_from,
            )

            if migrated_from is not None or row.schema_version != settings.schema_version:
                now = utc_now()
                async with db.write() as session:
                    fresh = await session.get(Project, row.id)
                    if fresh is not None:
                        fresh.schema_version = settings.schema_version
                        fresh.updated_at = now
                proj.updated_at = now
                _write_manifest(target, self._manifest_of(proj))

            self._open[proj.id] = proj
            self._remember(proj)
            log.info(
                "project.opened",
                id=proj.id,
                dir=target.as_posix(),
                schema=proj.schema_version,
                migrated_from=migrated_from,
            )
            bus.emit(Channel.SYSTEM, "project.opened", proj.to_dict(), project_id=proj.id)
            if migrated_from is not None:
                bus.emit(
                    Channel.SYSTEM,
                    "project.migrated",
                    {
                        "project_id": proj.id,
                        "name": proj.name,
                        "from": migrated_from,
                        "to": settings.schema_version,
                    },
                    project_id=proj.id,
                )
            return proj

    # --- 关闭 ---

    async def close(self, pid: str) -> None:
        proj = self._open.pop(pid, None)
        if proj is None:
            return
        await proj.db.close()
        log.info("project.closed", id=pid)
        bus.emit(Channel.SYSTEM, "project.closed", {"project_id": pid}, project_id=pid)

    async def close_all(self) -> None:
        for pid in list(self._open):
            await self.close(pid)

    # --- 最近打开 ---
    # 存在应用级 runtime 目录里，而不是工程内：它记录的是「这台机器上打开过什么」。

    @property
    def _recent_path(self) -> Path:
        return settings.runtime_dir / "recent.json"

    def _load_recent(self) -> list[dict[str, Any]]:
        path = self._recent_path
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 最近列表只是便利功能，坏了就当空的，但要留下日志而不是假装没事
            log.warning("project.recent_unreadable", path=str(path), error=str(exc))
            return []
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict) and e.get("dir")]

    def _remember(self, proj: OpenProject) -> None:
        entries = [e for e in self._load_recent() if e.get("dir") != proj.dir.as_posix()]
        entries.insert(
            0,
            {
                "id": proj.id,
                "name": proj.name,
                "dir": proj.dir.as_posix(),
                "schema_version": proj.schema_version,
                "opened_at": utc_now(),
            },
        )
        try:
            self._recent_path.write_text(
                json.dumps(entries[:RECENT_LIMIT], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:  # 记不住最近列表不该让「打开工程」失败
            log.warning("project.recent_unwritable", error=str(exc))

    def recent(self) -> list[dict[str, Any]]:
        """最近打开列表。目录已被删除或移动的条目不隐藏，而是标 exists=false。"""
        out: list[dict[str, Any]] = []
        for entry in self._load_recent():
            directory = Path(str(entry["dir"]))
            out.append(
                {
                    "id": entry.get("id", ""),
                    "name": entry.get("name", directory.name),
                    "dir": directory.as_posix(),
                    "schema_version": int(entry.get("schema_version") or 0),
                    "opened_at": entry.get("opened_at", ""),
                    "exists": (directory / MANIFEST_NAME).exists(),
                    "is_open": self._by_dir(directory) is not None,
                }
            )
        return out

    def forget(self, directory: str) -> None:
        target = _resolve_dir(directory)
        entries = [e for e in self._load_recent() if e.get("dir") != target.as_posix()]
        try:
            self._recent_path.write_text(
                json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("project.recent_unwritable", error=str(exc))


#: 全进程唯一实例：已打开工程的注册表必须是单例，否则 pid 会找不到库。
projects = ProjectService()
