"""资产总账（Step 3）。

规矩三条：
  1. 所有落盘文件都要登记，path 相对工程目录存——整个目录拷走仍然有效；
  2. 谁在用它必须记下来（AssetRef），否则「能不能删」只能靠猜；
  3. 删除前先告诉用户会破坏什么，绝不静默连带删除。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.events.bus import Channel, bus
from app.persistence.models import utc_now
from app.persistence.models_world import Asset, AssetRef
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, project_of

#: 资产类型 → 存放子目录。generations/ 只放生成物，手动素材一律进 assets/。
KIND_DIR = {
    "character_sheet": "assets/character_sheets",
    "location_reference": "assets/locations",
    "prop_reference": "assets/props",
    "audio": "assets/audio",
    "upload": "assets/uploads",
    "generated_image": "generations/images",
    "generated_video": "generations/videos",
    "proxy": "proxies",
    "export": "generations/exports",
}
IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_SUFFIX = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


def sniff_size(path: Path) -> tuple[int | None, int | None]:
    """不依赖第三方库读图片宽高：只认 PNG / JPEG / GIF 头，认不出就返回 None。"""
    try:
        head = path.read_bytes()[: 64 * 1024]
    except OSError:
        return None, None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head[:3] == b"GIF" and len(head) >= 10:
        return int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little")
    if head[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            length = int.from_bytes(head[i + 2 : i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (
                    int.from_bytes(head[i + 7 : i + 9], "big"),
                    int.from_bytes(head[i + 5 : i + 7], "big"),
                )
            i += 2 + length
    return None, None


def kind_of_suffix(suffix: str) -> str:
    if suffix.lower() in IMAGE_SUFFIX:
        return "image"
    if suffix.lower() in VIDEO_SUFFIX:
        return "video"
    return "other"


class AssetService:
    def _target_dir(self, pid: str, kind: str) -> Path:
        proj = project_of(pid)
        rel = KIND_DIR.get(kind, "assets/uploads")
        target = proj.dir / rel
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppError(
                ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
                "无法创建资产目录",
                f"{target}: {type(exc).__name__}: {exc}",
                ["确认磁盘可写且空间充足"],
            ) from exc
        return target

    async def register_bytes(
        self, pid: str, kind: str, filename: str, data: bytes, source: str = "manual"
    ) -> dict[str, Any]:
        """把上传的字节落盘并登记。同内容重复上传直接复用已有资产。"""
        if not data:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "文件是空的",
                f"{filename} 长度为 0 字节。",
                ["确认选中的文件没有损坏", "重新导出后再上传"],
            )
        sha1 = hashlib.sha1(data).hexdigest()
        existing = await self.by_sha1(pid, sha1)
        if existing is not None:
            return existing

        suffix = Path(filename).suffix or ".bin"
        target = self._target_dir(pid, kind) / f"{sha1[:12]}{suffix}"
        try:
            target.write_bytes(data)
        except OSError as exc:
            raise AppError(
                ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
                "资产写入失败",
                f"{target}: {type(exc).__name__}: {exc}",
                ["确认磁盘空间充足", "确认目录未被安全软件锁定"],
            ) from exc
        return await self._insert(pid, kind, target, sha1, source, filename)

    async def register_path(
        self, pid: str, kind: str, src: str, source: str = "manual", copy: bool = True
    ) -> dict[str, Any]:
        """登记磁盘上已有的文件（桌面端选文件走这条）。"""
        path = Path(src.strip().strip('"')).expanduser()  # noqa: ASYNC240 - 本地磁盘元数据，开销可忽略
        if not path.is_file():
            raise AppError(
                ErrorCode.NOT_FOUND,
                "文件不存在",
                f"{path} 不存在或不是文件。",
                ["确认路径拼写正确", "确认文件没有被移动或删除"],
                {"path": str(path)},
            )
        try:
            data_sha = hashlib.sha1(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "文件无法读取",
                f"{path}: {type(exc).__name__}: {exc}",
                ["确认文件未被其他程序占用"],
            ) from exc
        existing = await self.by_sha1(pid, data_sha)
        if existing is not None:
            return existing

        target = path
        if copy:
            target = self._target_dir(pid, kind) / f"{data_sha[:12]}{path.suffix}"
            shutil.copy2(path, target)
        return await self._insert(pid, kind, target, data_sha, source, path.name)

    async def _insert(
        self, pid: str, kind: str, target: Path, sha1: str, source: str, filename: str
    ) -> dict[str, Any]:
        proj = project_of(pid)
        try:
            rel = target.relative_to(proj.dir).as_posix()
        except ValueError:
            rel = target.as_posix()  # 工程外的引用：保留绝对路径，但会标记为外部
        width, height = sniff_size(target)
        row = Asset(
            id=new_id("asset"),
            kind=kind,
            path=rel,
            mime=None,
            width=width,
            height=height,
            size_bytes=target.stat().st_size,  # noqa: ASYNC240 - 本地磁盘元数据，开销可忽略
            sha1=sha1,
            source=source,
            meta_json=dump_json({"filename": filename, "media": kind_of_suffix(target.suffix)}),
            created_at=utc_now(),
        )
        async with proj.db.write() as session:
            session.add(row)
        bus.emit(Channel.ASSET, "asset.created", {"id": row.id, "kind": kind}, project_id=pid)
        return as_dict(row)

    async def by_sha1(self, pid: str, sha1: str) -> dict[str, Any] | None:
        db = db_of(pid)
        async with db.read() as session:
            row = (await session.execute(select(Asset).where(Asset.sha1 == sha1))).scalars().first()
        return as_dict(row) if row else None

    # --- 引用关系 ---

    async def link(
        self, pid: str, asset_id: str, owner_kind: str, owner_id: str, role: str | None = None
    ) -> None:
        db = db_of(pid)
        await fetch(db, Asset, asset_id, "资产")
        async with db.write() as session:
            session.add(
                AssetRef(
                    id=new_id("asset_ref"),
                    asset_id=asset_id,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    role=role,
                    created_at=utc_now(),
                )
            )

    async def unlink(self, pid: str, asset_id: str, owner_id: str) -> None:
        db = db_of(pid)
        refs = await fetch_all(db, AssetRef, where=AssetRef.asset_id == asset_id)
        async with db.write() as session:
            for ref in refs:
                if ref.owner_id != owner_id:
                    continue
                fresh = await session.get(AssetRef, ref.id)
                if fresh is not None:
                    await session.delete(fresh)

    async def refs_of(self, pid: str, asset_id: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        return [
            as_dict(r) for r in await fetch_all(db, AssetRef, where=AssetRef.asset_id == asset_id)
        ]

    # --- 列表与孤儿 ---

    async def list_assets(self, pid: str, kind: str | None = None) -> list[dict[str, Any]]:
        db = db_of(pid)
        proj = project_of(pid)
        rows = await fetch_all(db, Asset, order_by=Asset.created_at.desc())
        refs = await fetch_all(db, AssetRef)
        counts: dict[str, int] = {}
        for ref in refs:
            counts[ref.asset_id] = counts.get(ref.asset_id, 0) + 1
        out = []
        for row in rows:
            if kind and row.kind != kind:
                continue
            out.append(
                {
                    **as_dict(row),
                    "ref_count": counts.get(row.id, 0),
                    "missing": not (proj.dir / row.path).exists(),
                }
            )
        return out

    async def orphans(self, pid: str) -> list[dict[str, Any]]:
        """没有任何引用的资产。列出大小，让人在删之前知道能省多少空间。"""
        return [a for a in await self.list_assets(pid) if a["ref_count"] == 0]

    async def delete(self, pid: str, asset_id: str, force: bool = False) -> dict[str, Any]:
        """删除资产。仍被引用时默认拒绝，并告诉用户是谁在用。"""
        db = db_of(pid)
        proj = project_of(pid)
        row = await fetch(db, Asset, asset_id, "资产")
        refs = await self.refs_of(pid, asset_id)
        if refs and not force:
            owners = "、".join(sorted({f"{r['owner_kind']}:{r['owner_id']}" for r in refs}))
            raise AppError(
                ErrorCode.CONFLICT,
                "该资产仍被引用",
                f"有 {len(refs)} 处在用它：{owners}。删除会让这些地方缺图。",
                [
                    "先解除引用，再删除资产",
                    "或改用「强制删除」并接受这些地方将缺图",
                    "只想清理没人用的文件，请用「扫描孤儿资产」",
                ],
                {"asset_id": asset_id},
            )
        file_path = proj.dir / row.path
        async with db.write() as session:
            fresh = await session.get(Asset, asset_id)
            if fresh is not None:
                await session.delete(fresh)
        removed = False
        if file_path.is_file():
            try:
                file_path.unlink()
                removed = True
            except OSError as exc:  # 登记已删，文件删不掉要说出来而不是假装成功
                bus.emit(
                    Channel.ERROR,
                    "asset.file_kept",
                    {"asset_id": asset_id, "error": str(exc)},
                    project_id=pid,
                )
        bus.emit(Channel.ASSET, "asset.deleted", {"id": asset_id}, project_id=pid)
        return {"id": asset_id, "file_removed": removed, "broken_refs": len(refs) if force else 0}


assets = AssetService()
