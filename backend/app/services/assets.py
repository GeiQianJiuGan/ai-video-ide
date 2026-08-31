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
from app.generation.providers.base import DESC_MAX
from app.persistence.models import utc_now
from app.persistence.models_world import Asset, AssetRef
from app.services.base import (
    as_dict,
    assign,
    db_of,
    dump_json,
    fetch,
    fetch_all,
    load_json,
    project_of,
)

#: 资产类型 → 存放子目录。generations/ 只放生成物，手动素材一律进 assets/。
KIND_DIR = {
    "character_sheet": "assets/character_sheets",
    "location_reference": "assets/locations",
    "prop_reference": "assets/props",
    #: 从视频里抽出来的单帧（真末帧续接靠它）——**临时资源，不算资产**。
    #: 落在 cache/ 而不是 assets/：它们是派生的、可重新生成的，删掉成片时应该连带删掉。
    #: 老工程里已经在 assets/frames/ 的那些帧路径记在库里，照旧能读（cache/ 优先），
    #: 只是新抽的帧不再进 assets/。见 services/frames.py 与下面的 delete() 级联清理。
    "frame": "cache/frames",
    #: 从视频里拆出来的音频（时间线的音频轨靠它）——同样是**临时资源，不算资产**。
    #: 理由与帧完全一样：派生的、能重拆的、源成片一没它就没意义。见 services/audio.py。
    "clip_audio": "cache/audio",
    "audio": "assets/audio",
    "upload": "assets/uploads",
    "generated_image": "generations/images",
    "generated_video": "generations/videos",
    #: 音源那条链生成的声音。**是正经资产，不是临时文件**：它是某个镜头采用的那条音轨
    #: （`Shot.current_audio_version_id`），删了就得重新跑一次音源服务，
    #: 与「随时能重抽的首尾帧」完全不同，所以进 generations/ 而不是 cache/。
    "generated_audio": "generations/audio",
    "proxy": "proxies",
    "export": "generations/exports",
}
IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_SUFFIX = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
#: 音频后缀。加进来是因为**参考素材可以是一段音频**（S2V 那类模型靠对白音频驱动口型），
#: 而「这个文件是什么媒体」只能由后缀回答——把 `.wav` 当成 `other` 的话，它要么被当图
#: 填进 LoadImage（不报错也出不了片），要么在账单里被当成「不是图片」整条丢掉。
AUDIO_SUFFIX = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus"}

#: **临时资源**：登记在 asset 表里（生成层与时间线都要靠 asset_id → path 找文件），
#: 但不算「工程资产」——资产页与孤儿扫描一律不列它们，它们的生命周期挂在源文件上
#: （源成片一删，从它派生的帧与音频跟着删，见 `AssetService.delete`）。
#: 用户自己导入的音乐是 `audio`（真资产），从画面里拆出来的是 `clip_audio`（临时）。
TRANSIENT_KINDS = frozenset({"frame", "clip_audio"})


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
    """这个文件是什么媒体：`image` / `video` / `audio` / `other`。

    只看后缀，刻意不去读文件头：这句话在只读路径上被问很多次（分镜板、版本轨、账单），
    为一张缩略图去读几百 MB 的成片不值得。认不出来就是 `other`——调用方一律
    「不是我要的那一种」处置，绝不猜。
    """
    lowered = suffix.lower()
    if lowered in IMAGE_SUFFIX:
        return "image"
    if lowered in VIDEO_SUFFIX:
        return "video"
    if lowered in AUDIO_SUFFIX:
        return "audio"
    return "other"


# --- 落盘公共件：素材库（services/library.py）复用同一套，不复制第二遍 ---


def ensure_dir(target: Path, what: str = "资产目录") -> Path:
    """建目录，失败时区分「磁盘满」与其他，绝不静默。"""
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AppError(
            ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
            f"无法创建{what}",
            f"{target}: {type(exc).__name__}: {exc}",
            ["确认磁盘可写且空间充足"],
        ) from exc
    return target


def content_name(sha1: str, suffix: str) -> str:
    """文件名 = 内容 sha1 前缀 + 后缀。改内容必然换名字，缓存与去重都靠它。"""
    return f"{sha1[:12]}{suffix or '.bin'}"


def require_bytes(data: bytes, filename: str) -> str:
    """空文件不许登记；顺手把 sha1 算出来。"""
    if not data:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "文件是空的",
            f"{filename} 长度为 0 字节。",
            ["确认选中的文件没有损坏", "重新导出后再上传"],
        )
    return hashlib.sha1(data).hexdigest()


def write_file(target: Path, data: bytes) -> None:
    try:
        target.write_bytes(data)
    except OSError as exc:
        raise AppError(
            ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
            "资产写入失败",
            f"{target}: {type(exc).__name__}: {exc}",
            ["确认磁盘空间充足", "确认目录未被安全软件锁定"],
        ) from exc


def source_file(src: str) -> Path:
    """把用户给的路径变成一个确实存在的文件，否则结构化报错。"""
    path = Path(src.strip().strip('"')).expanduser()  # noqa: ASYNC240 - 本地磁盘元数据，开销可忽略
    if not path.is_file():
        raise AppError(
            ErrorCode.NOT_FOUND,
            "文件不存在",
            f"{path} 不存在或不是文件。",
            ["确认路径拼写正确", "确认文件没有被移动或删除"],
            {"path": str(path)},
        )
    return path


def sha1_of_file(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "文件无法读取",
            f"{path}: {type(exc).__name__}: {exc}",
            ["确认文件未被其他程序占用"],
        ) from exc


class AssetService:
    def _target_dir(self, pid: str, kind: str) -> Path:
        proj = project_of(pid)
        rel = KIND_DIR.get(kind, "assets/uploads")
        return ensure_dir(proj.dir / rel)

    async def register_bytes(
        self, pid: str, kind: str, filename: str, data: bytes, source: str = "manual"
    ) -> dict[str, Any]:
        """把上传的字节落盘并登记。同内容重复上传直接复用已有资产。"""
        sha1 = require_bytes(data, filename)
        existing = await self.by_sha1(pid, sha1)
        if existing is not None:
            return existing

        target = self._target_dir(pid, kind) / content_name(sha1, Path(filename).suffix)
        write_file(target, data)
        return await self._insert(pid, kind, target, sha1, source, filename)

    async def register_path(
        self, pid: str, kind: str, src: str, source: str = "manual", copy: bool = True
    ) -> dict[str, Any]:
        """登记磁盘上已有的文件（桌面端选文件、从素材库采用都走这条）。"""
        proj = project_of(pid)
        raw_path = Path(src.strip().strip('"')).expanduser()
        if raw_path.is_file():
            path = raw_path
        elif (proj.dir / src).is_file():
            path = proj.dir / src
        elif (proj.dir / "assets" / src).is_file():
            path = proj.dir / "assets" / src
        elif (proj.dir / "assets/uploads" / src).is_file():
            path = proj.dir / "assets/uploads" / src
        else:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "文件不存在",
                f"未在磁盘上找到文件：{src}。\n如果是本机文件，请填写完整绝对路径（如 D:\\video.mp4）；若在浏览器中操作，请使用「选择视频文件」上传模式。",
                ["填写完整的本地绝对路径（如 D:\\video.mp4）", "或在界面点击「选择视频文件」直接上传", "确认文件未被移动或删除"],
                {"path": str(src)},
            )
        data_sha = sha1_of_file(path)
        existing = await self.by_sha1(pid, data_sha)
        if existing is not None:
            return existing

        target = path
        if copy:
            target = self._target_dir(pid, kind) / content_name(data_sha, path.suffix)
            try:
                if target.resolve() != path.resolve():
                    shutil.copy2(path, target)
            except OSError:
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

    async def merge_meta(self, pid: str, asset_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """往已登记资产的 meta_json 上补字段。

        「从素材库采用」要记出处，但采用可能命中 sha1 去重（文件早就在工程里了），
        那时候没有新行可写。所以出处统一在这里补，新登记和去重命中走同一条路。
        """
        db = db_of(pid)
        await fetch(db, Asset, asset_id, "资产")
        async with db.write() as session:
            row = await session.get(Asset, asset_id)
            assert row is not None
            meta = load_json(row.meta_json, {})
            if not isinstance(meta, dict):
                meta = {}
            meta.update(patch)
            row.meta_json = dump_json(meta)
            return as_dict(row)

    async def update(self, pid: str, asset_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """改这个资产的描述。**能改的只有 `description` 一个字段。**

        `path` / `kind` / `sha1` / `size_bytes` 是落盘事实，改了就与磁盘上那个文件对不上，
        所以这里刻意只放一个字段过去（`assign` 的 `allowed` 就是那道门）。

        描述是**模型唯一看得到的素材说明**：上下文账单把它当 `desc` 冻结进版本，最后由
        `providers/base.py::ref_hint()` 渲染成「参考图1=<名字>（<这一句>）」。所以它不是
        备注栏——写不写直接决定引用这张图时 prompt 里有没有内容。

        **清空传 `''`**：`None` 在 `assign` 里是「这次不改」（与 `ShotPatch` 同一条口径）。
        """
        db = db_of(pid)
        await fetch(db, Asset, asset_id, "资产")
        async with db.write() as session:
            row = await session.get(Asset, asset_id)
            assert row is not None
            changed = assign(row, patch, ("description",))
            out = as_dict(row)
        if changed:
            bus.emit(
                Channel.ASSET,
                "asset.updated",
                {"id": asset_id, "changed": changed},
                project_id=pid,
            )
        return out

    async def undescribed(self, pid: str) -> dict[str, Any]:
        """还没有描述的资产。「扫一遍缺描述的素材」与 AI 那条读工具共用这一份。

        **临时资源不算**（`TRANSIENT_KINDS`：抽出来的帧、拆出来的音频）：它们是派生物，
        给它们写描述既没人看得到，也会把这张清单刷满真正需要补的素材之外的东西。
        每条带 `owners`（靠已有的 `AssetRef`）说清「它挂在谁身上」——不然用户看到一串
        文件名也不知道该写什么。
        """
        db = db_of(pid)
        proj = project_of(pid)
        rows = await fetch_all(db, Asset, order_by=Asset.created_at.desc())
        refs = await fetch_all(db, AssetRef)
        owners: dict[str, list[dict[str, Any]]] = {}
        for ref in refs:
            owners.setdefault(ref.asset_id, []).append(
                {"owner_kind": ref.owner_kind, "owner_id": ref.owner_id, "role": ref.role}
            )
        items = [
            {
                **as_dict(row),
                "media": kind_of_suffix(Path(row.path).suffix),
                "owners": owners.get(row.id, []),
                "missing": not (proj.dir / row.path).exists(),
            }
            for row in rows
            if row.kind not in TRANSIENT_KINDS and not str(row.description or "").strip()
        ]
        described = sum(
            1
            for row in rows
            if row.kind not in TRANSIENT_KINDS and str(row.description or "").strip()
        )
        return {
            "items": items,
            "count": len(items),
            "described": described,
            #: 描述进 prompt 时的截断上限。前端照它显示字数提示，不写死第二份。
            "desc_max": DESC_MAX,
            "note": (
                "没有描述的素材，模型引用它时只会看到一个文件名——"
                "生成视频的 prompt 里就没有「这张图长什么样」这句话。"
            ),
        }

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
        """工程资产总账。

        **临时资源不在这张账上**（`TRANSIENT_KINDS`：抽出来的首尾帧 `frame`、从视频里
        拆出来的声音 `clip_audio`）：它们都是从成片派生的、可以重来一次的，生命周期跟着
        源文件走，列进资产页只会让人以为「这里多了一堆我没导入过的东西，是不是能删」。
        要看它们得显式传 `kind="frame"` / `kind="clip_audio"`。用户自己导入的音乐是
        `kind="audio"` 的真资产，照常在账上。
        """
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
            if not kind and row.kind in TRANSIENT_KINDS:
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
        """没有任何引用的资产。列出大小，让人在删之前知道能省多少空间。

        抽出来的帧从来没有 `AssetRef`，会把这张表刷满，所以它们不算孤儿——
        `list_assets` 已经把临时资源排除掉了（要清理它们请删掉对应的成片）。
        """
        return [a for a in await self.list_assets(pid) if a["ref_count"] == 0]

    async def delete(self, pid: str, asset_id: str, force: bool = False) -> dict[str, Any]:
        """删除资产。仍被引用时默认拒绝，并告诉用户是谁在用。

        删成功时会**连带删掉从它派生的临时文件**（抽出来的首 / 末帧、拆出来的音频）：
        那些东西只有配合这段视频才有意义，源片没了还留着一堆孤零零的 PNG / m4a 是垃圾。
        连带删除的每一条都写进返回值的 `derived_removed`——「绝不静默连带删除」说的是
        不静默，不是不删。
        """
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
        derived = [] if row.kind in TRANSIENT_KINDS else await self._derived(pid, asset_id)
        file_path = proj.dir / row.path
        async with db.write() as session:
            for extra in derived:  # 派生的帧没有引用，直接跟着走
                stale = await session.get(Asset, extra.id)
                if stale is not None:
                    await session.delete(stale)
            fresh = await session.get(Asset, asset_id)
            if fresh is not None:
                await session.delete(fresh)
        removed = self._unlink(pid, asset_id, file_path)
        derived_removed = []
        for extra in derived:
            self._unlink(pid, extra.id, proj.dir / extra.path)
            derived_removed.append({"id": extra.id, "path": extra.path})
        bus.emit(Channel.ASSET, "asset.deleted", {"id": asset_id}, project_id=pid)
        for extra_out in derived_removed:
            bus.emit(Channel.ASSET, "asset.deleted", extra_out, project_id=pid)
        return {
            "id": asset_id,
            "file_removed": removed,
            "broken_refs": len(refs) if force else 0,
            #: 跟着一起删掉的临时文件（抽出来的帧、拆出来的音频）。
            #: 空列表是常态（大多数资产没派生过东西）。
            "derived_removed": derived_removed,
        }

    async def _derived(self, pid: str, asset_id: str) -> list[Asset]:
        """从这个资产派生出来的所有临时文件。

        出处（`meta_json.from_asset_id`）的解读**只放在各自的模块里**：帧在
        `services/frames.py`，音频在 `services/audio.py`。这里只负责把它们串起来，
        绝不在本模块里再解一遍 meta——两处各解一遍，迟早有一处漏掉级联。
        """
        # 延迟导入：frames / audio 都依赖本模块
        from app.services.audio import derived_audio
        from app.services.frames import derived_frames

        rows = await fetch_all(db_of(pid), Asset)
        return [*derived_frames(rows, asset_id), *derived_audio(rows, asset_id)]

    def _unlink(self, pid: str, asset_id: str, file_path: Path) -> bool:
        if not file_path.is_file():
            return False
        try:
            file_path.unlink()
            return True
        except OSError as exc:  # 登记已删，文件删不掉要说出来而不是假装成功
            bus.emit(
                Channel.ERROR,
                "asset.file_kept",
                {"asset_id": asset_id, "error": str(exc)},
                project_id=pid,
            )
            return False


assets = AssetService()
