"""工程 / 场景导入导出：把「一套能跑起来的环境」搬到另一台机器上。

工程目录本来就是自包含的，但**拷目录不等于复制环境**：预设图（`runtime_dir/presets/`）
与 provider 地址（`settings.json`）都是应用级的，工程一个字都不记。所以包里除了数据与
文件，还有一份**环境要求清单** `env`——导入时逐条比对本机，缺什么如实说出来，
而不是等到真正入队才报「选中的预设不存在」。

三个取舍（都是刻意的）：

  1. 成片（`generations/`）默认不带、可勾选；素材（`assets/`）默认带；
     `cache/` / `proxies/` 永不带——派生物，换机重新生成就有。
  2. **不带预设图，只带清单**：那份图属于「我这台机器怎么调模型」，跟着模型端一起变。
     打进包里，两台机器的图迟早对不上，而且用户根本不知道自己在用哪一份。
  3. **密钥与地址一律不进包**：`settings.json` 不是包成员，`env` 里只有「要什么」，
     没有「在哪、用什么密钥」。
  4. **落点有两条路，主路是「下载到用户那台机器」**：界面跑在浏览器（或 Tauri 的 WebView）
     里，拿不到、也不该去猜后端机器上的路径。所以导出默认写进临时目录再当附件流回去
     （`download_project` / `download_scene`），导入默认是把文件传上来落进暂存区
     （`stage`）。「写进后端机器上某个目录 / 读后端机器上某个路径」那条老路照旧留着：
     桌面版里两台机器其实是同一台，几个 G 的包不必再从自己这儿传给自己一遍。

一个包 = 一个 zip（扩展名 `.aivspkg`），`manifest.json` 里**先认 kind 再解内容**——
与 `services/projects.py::MANIFEST_KIND` 同一套做法。

这个模块**只做编排，不新增任何写路径**（照 `services/adopt.py` 的规矩）：场景导入落库
全部转调已有的 `story` / `sequence` / `cast` / `world` / `assets` / `generation` 写方法，
所以它不需要迁移、不需要加列，出处只记在 `Asset.meta_json`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from app.core import ffmpeg
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.events.bus import Channel, bus
from app.generation.providers import presets
from app.persistence import migrate
from app.persistence.models import Project, utc_now
from app.persistence.models_cast import INHERITABLE, Appearance, Character, SheetVersion
from app.persistence.models_flow import ShotLink
from app.persistence.models_gen import GenerationVersion
from app.persistence.models_story import (
    Scene,
    SceneCast,
    SceneLocation,
    Shot,
    ShotCast,
    ShotProp,
)
from app.persistence.models_world import (
    Asset,
    Location,
    LocationReference,
    LocationVariant,
    Prop,
    PropReference,
)
from app.services import route
from app.services.assets import TRANSIENT_KINDS, assets
from app.services.base import db_of, fetch, fetch_all, load_json, project_of
from app.services.cast import CHARACTER_FIELDS, cast
from app.services.generation import generation
from app.services.projects import (
    DB_NAME,
    MANIFEST_KIND,
    MANIFEST_NAME,
    OCCUPIED_SUGGESTIONS,
    aspect_ratio,
    projects,
)
from app.services.sequence import sequence
from app.services.story import SCENE_FIELDS, SHOT_FIELDS, story
from app.services.world import LOCATION_FIELDS, PROP_FIELDS, VARIANT_FIELDS, world

log = get_logger("packages")

PACKAGE_KIND = "aivs-package"
PACKAGE_VERSION = 1
PACKAGE_EXT = ".aivspkg"
MANIFEST_MEMBER = "manifest.json"
DB_MEMBER = "project.db"
SCENE_MEMBER = "data/scene.json"
FILES_PREFIX = "files/"

SCOPE_PROJECT = "project"
SCOPE_SCENE = "scene"
SCOPES = (SCOPE_PROJECT, SCOPE_SCENE)

#: 带素材的目录（`generations/` 只在 include_generated 时带）。
PACK_DIRS = ("assets", "generations")
#: **永不进包**：派生物与应用级配置。前者换机重新生成就有，后者含地址与密钥。
NEVER_PACK = ("cache", "proxies", ".runtime", "settings.json")

#: 打包 / 解包时每处理这么多文件往控制台报一次进度。
PROGRESS_EVERY = 20

#: 上传上来的包先落这一层（`runtime_dir` 之下，与 `recent.json` / `settings.json` 同级）。
#: 浏览器只能给我们一个文件对象，落盘之后才有路径给 `inspect` / 导入那几道门用。
UPLOAD_SUB = "uploads"
#: 暂存的包活这么久就当废弃清掉——看了账单又关掉弹窗的那种，攒起来能有好几个 G。
UPLOAD_TTL_SECONDS = 6 * 3600
#: 接收上传时的分片大小。整包 `read()` 进内存的话，一个带成片的工程包能把后端顶掉。
UPLOAD_CHUNK = 1 << 20

# --- 路径与 zip 的公共件 ---


def _safe_member(name: str) -> str:
    """包内路径守卫。绝对路径、盘符、`..` 段一律拒绝——解包是往用户磁盘上写文件，
    一个恶意（或只是手工改坏了的）包不该有机会写到工程目录外面去。

    与 `api/files.py` 的越界口径一致：`VALIDATION_ERROR`（422）+「包内路径越界」。
    """
    text = str(name or "").replace("\\", "/").strip()
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or text.startswith("/") or ":" in text or ".." in parts:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "包内路径越界",
            f"成员 {name!r} 不是一个安全的相对路径（绝对路径、盘符、.. 一律拒绝）。",
            ["确认这个包是本应用导出的", "重新导出一份包"],
            {"member": str(name)},
        )
    return "/".join(parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """zip 里的符号链接：解包出来会把写入引到别处，所以一律当越界处理。"""
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _resolve_out(out_dir: str, filename: str) -> Path:
    """算出包要落在哪。目录必须已经存在（前端用 DirPicker 选，不在这里瞎建）。"""
    raw = str(out_dir or "").strip().strip('"')
    if not raw:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有选择保存位置",
            "导出目录不能为空。",
            ["选择一个已存在的目录"],
        )
    directory = Path(raw).expanduser()
    if not directory.is_dir():
        raise AppError(
            ErrorCode.NOT_FOUND,
            "保存目录不存在",
            f"{directory} 不存在或不是目录。",
            ["先创建这个目录", "或换一个已存在的目录"],
            {"dir": directory.as_posix()},
        )
    stem = str(filename or "").strip().replace("\\", "/")
    if "/" in stem or ":" in stem or stem in ("", ".", ".."):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "文件名不合法",
            f"{filename!r} 不能作为文件名（不要带路径分隔符）。",
            ["只填文件名，保存位置用上面的目录", f"例如 my_film{PACKAGE_EXT}"],
        )
    if not stem.endswith(PACKAGE_EXT):
        stem = f"{stem}{PACKAGE_EXT}"
    return (directory / stem).resolve()


def _walk(root: Path) -> list[Path]:
    """列出目录下所有文件（不含目录本身）。目录不存在时回空表。"""
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _disk_error(target: Path, exc: OSError, what: str) -> AppError:
    return AppError(
        ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
        f"{what}失败",
        f"{target}: {type(exc).__name__}: {exc}",
        ["确认磁盘可写且空间充足", "确认目录未被安全软件锁定"],
    )


def _progress(pid: str | None, phase: str, done: int, total: int, name: str = "") -> None:
    """往 system 频道报进度——底部控制台已经在听这个频道了。可从线程里调。"""
    bus.emit(
        Channel.SYSTEM,
        "package.progress",
        {"phase": phase, "done": done, "total": total, "name": name},
        project_id=pid,
    )


def _snapshot_db(src: Path, dest: Path) -> None:
    """用 sqlite 的 backup 拿一份一致的快照。

    直接把 `project.db` 塞进 zip 会漏掉还在 `-wal` 里的那部分——包能打开，但少几条最新的
    改动。backup 走的是 sqlite 自己的一致性读，不用去碰别人的连接，也不用停写。

    **两个连接必须真的 close**：`with sqlite3.connect(...)` 只提交事务、不关连接，
    在 Windows 上那份 `.db.tmp` 会一直被占着，收尾时删不掉（WinError 32）。
    """
    with (
        contextlib.closing(sqlite3.connect(src.as_posix())) as conn,
        contextlib.closing(sqlite3.connect(dest.as_posix())) as out,
    ):
        conn.backup(out)


def _write_zip(
    target: Path,
    texts: dict[str, str],
    files: list[tuple[str, Path]],
    pid: str | None,
) -> int:
    """写包。先落 `.tmp` 再 `replace`：断电或磁盘满不会留下半截包。阻塞，请放进线程。"""
    tmp = target.with_name(target.name + ".tmp")
    total = len(files)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for member, text in texts.items():
                zf.writestr(member, text)
            for i, (member, src) in enumerate(files, 1):
                zf.write(src, member)
                if i % PROGRESS_EVERY == 0 or i == total:
                    _progress(pid, "export", i, total, member)
        tmp.replace(target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise _disk_error(target, exc, "写入包") from exc
    return _size_of(target)


def _open_package(path: Path) -> zipfile.ZipFile:
    if not path.is_file():
        raise AppError(
            ErrorCode.NOT_FOUND,
            "包文件不存在",
            f"{path} 不存在或不是文件。",
            ["确认路径拼写正确", f"包的扩展名是 {PACKAGE_EXT}"],
            {"path": path.as_posix()},
        )
    try:
        return zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "包读不出来",
            f"{path.name}: {type(exc).__name__}: {exc}",
            ["确认文件没有传输中断", "重新导出一份包"],
        ) from exc


def _read_manifest(path: Path) -> dict[str, Any]:
    """只读 `manifest.json`，不解包。先认 kind 再解内容。阻塞，请放进线程。"""
    with _open_package(path) as zf:
        try:
            raw = zf.read(MANIFEST_MEMBER).decode("utf-8")
        except (KeyError, OSError, UnicodeDecodeError) as exc:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "包里没有清单",
                f"{path.name} 缺少 {MANIFEST_MEMBER}：{type(exc).__name__}: {exc}",
                ["确认选的是本应用导出的包", f"包的扩展名是 {PACKAGE_EXT}"],
            ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "包清单不是合法 JSON",
            f"{MANIFEST_MEMBER}: {exc}",
            ["重新导出一份包"],
        ) from exc
    if not isinstance(data, dict) or data.get("kind") != PACKAGE_KIND:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "这不是本应用的包",
            f"{MANIFEST_MEMBER} 缺少 kind={PACKAGE_KIND} 标记。",
            ["确认选的是本应用导出的包"],
        )
    if data.get("scope") not in SCOPES:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "包的范围无法识别",
            f"scope={data.get('scope')!r} 不在 {'、'.join(SCOPES)} 里。",
            ["用新版本的应用导出这个包"],
        )
    return data


def _read_member_json(path: Path, member: str) -> dict[str, Any]:
    with _open_package(path) as zf:
        try:
            raw = zf.read(member).decode("utf-8")
            data = json.loads(raw)
        except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "包内数据读不出来",
                f"{member}: {type(exc).__name__}: {exc}",
                ["重新导出一份包"],
            ) from exc
    if not isinstance(data, dict):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "包内数据格式不对",
            f"{member} 不是一个对象。",
            ["重新导出一份包"],
        )
    return data


def _extract_one(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path, root: Path) -> None:
    """解一个成员。落点必须仍在 root 之内——`_safe_member` 之外的第二道保险。"""
    resolved = dest.resolve()
    base = root.resolve()
    if base != resolved and base not in resolved.parents:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "包内路径越界",
            f"成员 {info.filename!r} 会被解到 {resolved}，已经不在目标目录里。",
            ["确认这个包是本应用导出的"],
            {"member": info.filename},
        )
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out)
    except OSError as exc:
        raise _disk_error(dest, exc, "解包") from exc


def _unpack(path: Path, target: Path, pid: str | None, *, want_db: bool) -> dict[str, Any]:
    """解包到 target。`want_db=True` 时 `project.db` 落成工程库，其余按原相对路径落。

    **越界检查在写第一个字节之前全部做完**：一个坏包不该留下半个目录。阻塞，请放进线程。
    """
    with _open_package(path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        members: list[tuple[str, zipfile.ZipInfo]] = []
        for info in infos:
            if _is_symlink(info):
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "包内路径越界",
                    f"成员 {info.filename!r} 是一个符号链接，解包会把写入引到别处。",
                    ["确认这个包是本应用导出的"],
                    {"member": info.filename},
                )
            members.append((_safe_member(info.filename), info))

        payload = [(m, i) for m, i in members if m.startswith(FILES_PREFIX)]
        total = len(payload) + (1 if want_db else 0)
        done = 0
        if want_db:
            db_info = next((i for m, i in members if m == DB_MEMBER), None)
            if db_info is None:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "包里没有工程库",
                    f"{path.name} 缺少 {DB_MEMBER}，无法还原成一个工程。",
                    ["确认这是「工程包」而不是「场景包」"],
                )
            target.mkdir(parents=True, exist_ok=True)
            _extract_one(zf, db_info, target / DB_NAME, target)
            done += 1
            _progress(pid, "import", done, total, DB_NAME)
        for member, info in payload:
            rel = member[len(FILES_PREFIX) :]
            _extract_one(zf, info, target / rel, target)
            done += 1
            if done % PROGRESS_EVERY == 0 or done == total:
                _progress(pid, "import", done, total, rel)
    return {"files": len(payload)}


# --- 环境要求清单：包里只说「要什么」，绝不说「在哪、用什么密钥」 ---

#: 角色 → 人看得懂的说法（清单与界面共用一份）。
ROLE_LABEL = {
    "r2v": "出正片（R2V）",
    "flf": "补转场（首尾帧）",
    "refine": "二次处理（超分 / 插帧）",
    "audio": "音源（声音那条链）",
}


def _preset_requirement(role: str, name: str) -> dict[str, Any]:
    """一份预设的「要求」：名字 + 它标了哪些入口。

    入口是从**导出机上那份图**里数出来的（`presets.entry_points`）——目标机器没有同名
    预设时，这份清单至少能告诉用户「要一份标了这几个入口的图」。图读不出来就只留名字并
    标 `unreadable`：预设坏了不该让导出失败，但也不能假装它是好的。
    """
    try:
        points = presets.entry_points(presets.load(name))
    except AppError:
        return {"role": role, "name": name, "markers": [], "unreadable": True}
    return {"role": role, "name": name, "markers": sorted(points), "unreadable": False}


async def _env_of(pid: str) -> dict[str, Any]:
    """这个工程在另一台机器上跑起来需要什么。**不含任何地址与密钥。**"""
    row = (await fetch_all(db_of(pid), Project))[0]
    wanted: list[tuple[str, str]] = [
        ("r2v", row.r2v_preset_name or row.preset_name or ""),
        ("flf", row.flf_preset_name or row.preset_name or ""),
        # refine / audio 是应用级选的那两份图，工程里不记——但它们同样是「换机后要准备的
        # 东西」，所以只要导出机上配了就一起写进清单。
        ("refine", settings.refine_preset),
        ("audio", settings.audio_preset),
    ]
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for role, name in wanted:
        key = (role, name)
        if not name or key in seen:
            continue
        seen.add(key)
        items.append(_preset_requirement(role, name))
    return {
        "video_provider": settings.video_provider,
        "audio_provider": settings.audio_provider,
        #: 这个工程显式选的调用方式（空 = 跟随目标机器的设置页）。**先归一**：老库里写的是
        #: `workflow_api`，导进新机器后要能和 registry 认的那个名字对上，否则清单上那一行
        #: 说的路在目标机器上根本不存在。
        "generation_mode": route._safe_normalize(row.generation_mode),
        "presets": items,
        "needs_ffmpeg": True,
        # 手动路径能走完整流程（硬约束 2），所以包永远不「要求」LLM。
        "needs_llm": False,
        "schema_version": settings.schema_version,
    }


def _env_check(manifest: dict[str, Any]) -> dict[str, Any]:
    """把包里的要求逐条与本机比对。**只报告，不抛**——缺什么要让用户先看见。

    收的是**整份清单**而不是 `manifest["env"]`：schema 那一格必须和导入时那道门读同一个数
    （`manifest["schema_version"]`，见 `import_project` / `import_scene`）。只看
    `env.schema_version` 的话，一份 schema 被改高的包会在 inspect 里显示「吃得下」、
    到导入才报 `SCHEMA_MISMATCH`——比对结果说的话和真事对不上。
    """
    env = manifest.get("env") or {}
    local = {row["name"]: row for row in presets.listing()}
    items: list[dict[str, Any]] = []
    for req in env.get("presets") or []:
        role = str(req.get("role") or "r2v")
        name = str(req.get("name") or "")
        row = local.get(name)
        ready_key = {"flf": "flf_ready", "refine": "refine_ready", "audio": "audio_ready"}.get(
            role, "r2v_ready"
        )
        markers = [str(m) for m in (req.get("markers") or [])]
        present = row is not None
        ready = bool(row and row.get(ready_key))
        impact = None
        if not present:
            impact = "本机没有这份预设，选用它的生成入队时会报「选中的预设不存在」"
        elif not ready:
            impact = f"本机这份同名预设不能用于{ROLE_LABEL.get(role, role)}"
        items.append(
            {
                "role": role,
                "label": ROLE_LABEL.get(role, role),
                "name": name,
                "markers": markers,
                "present": present,
                "ready": ready,
                "impact": impact,
            }
        )
    located = ffmpeg.locate()
    stored_schema = int(manifest.get("schema_version") or env.get("schema_version") or 0)
    return {
        "presets": items,
        "video_provider": {
            "wanted": env.get("video_provider"),
            "current": settings.video_provider,
            "matches": env.get("video_provider") == settings.video_provider,
        },
        "audio_provider": {
            "wanted": env.get("audio_provider"),
            "current": settings.audio_provider,
            "matches": env.get("audio_provider") == settings.audio_provider,
        },
        "ffmpeg": {"present": bool(located.path), "source": located.source},
        "schema": {
            "wanted": stored_schema,
            "current": settings.schema_version,
            "ok": stored_schema <= settings.schema_version,
        },
        "missing": [i["name"] for i in items if not i["present"]],
    }


def _resolve_pkg(path: str) -> Path:
    raw = str(path or "").strip().strip('"')
    if not raw:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有选择包文件",
            "包路径不能为空。",
            [f"选择一个 {PACKAGE_EXT} 文件"],
        )
    return Path(raw).expanduser()


def _resolve_target(path: str) -> Path:
    raw = str(path or "").strip().strip('"')
    if not raw:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有选择目录",
            "导入目标目录不能为空。",
            ["填写一个绝对路径，例如 D:/works/copied_film"],
        )
    return Path(raw).expanduser().resolve()


def _safe_stem(name: str) -> str:
    """工程名 → 建议的文件名。中文照留，路径分隔符与盘符一律换掉。"""
    text = "".join("_" if c in '\\/:*?"<>|' else c for c in str(name or "").strip())
    return text.strip(". ") or "package"


# --- 上传上来的包：暂存区 ---


def _upload_root() -> Path:
    """暂存区。应用级临时区，**不在任何工程目录里**——它还不属于任何工程。"""
    root = settings.runtime_dir / UPLOAD_SUB
    root.mkdir(parents=True, exist_ok=True)
    return root


def _upload_name(filename: str) -> str:
    """上传上来的文件名只用来给人看，一律压成一个安全的裸文件名。"""
    raw = str(filename or "").replace("\\", "/").split("/")[-1]
    stem = _safe_stem(raw)
    return stem if stem.endswith(PACKAGE_EXT) else f"{stem}{PACKAGE_EXT}"


def _is_staged(path: Path) -> bool:
    """这是一份刚上传上来的临时副本吗（用完就该删的那种）。"""
    try:
        return path.resolve().is_relative_to(_upload_root().resolve())
    except (OSError, ValueError):
        return False


def _drop_staged(path: Path) -> None:
    """删掉一份暂存副本（连它那层目录）。

    删不掉不算失败：下一次上传时 `_prune_uploads` 还会再来一次，而这时候用户等着的是
    「导入成功了没有」，不该被一个清理动作绊住。
    """
    if not _is_staged(path):
        return
    root = _upload_root().resolve()
    holder = path.parent.resolve()
    # 每份上传各有一层自己的目录；万一有人把包直接摆在暂存区根下，只删那一个文件
    if holder == root:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return
    shutil.rmtree(holder, ignore_errors=True)


def _prune_uploads() -> None:
    """清掉过期的暂存包。看了账单又关掉弹窗时那份副本没人删，所以每次上传前扫一遍。"""
    cutoff = time.time() - UPLOAD_TTL_SECONDS
    try:
        children = list(_upload_root().iterdir())
    except OSError:
        return
    for child in children:
        with contextlib.suppress(OSError):
            if child.stat().st_mtime >= cutoff:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)


def _reid_project(db_path: Path) -> dict[str, Any]:
    """给导入进来的库换一个新的 project id，并回这一行的字段。

    **不能保留原 id**：`ProjectService._open` 是按 pid 索引的单例注册表，同一台机器上把
    一个工程导入成副本后，两个目录同 id 会在注册表里互相顶掉——打开 A 再打开 B，A 的库
    句柄就没了。project id 也不是任何表的外键（一个库就是一个工程），换掉是安全的。
    下一个人不要「顺手」把这一段改回保留原 id。
    """
    try:
        with sqlite3.connect(db_path.as_posix()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM project LIMIT 1").fetchone()
            if row is None:
                raise AppError(
                    ErrorCode.SCHEMA_MISMATCH,
                    "包里的工程库缺少工程记录",
                    f"{DB_MEMBER} 里没有 project 行，无法确定这是哪个工程。",
                    ["重新导出一份包"],
                )
            pid = new_id("project")
            now = utc_now()
            conn.execute("UPDATE project SET id=?, updated_at=? WHERE id=?", (pid, now, row["id"]))
            data = {key: row[key] for key in row.keys()}
    except sqlite3.Error as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "包里的工程库无法写入",
            f"{db_path}: {type(exc).__name__}: {exc}",
            ["确认磁盘可写", "重新导出一份包"],
        ) from exc
    data.update(id=pid, updated_at=now)
    return data


def _project_manifest(row: dict[str, Any], schema_version: int) -> dict[str, Any]:
    """导入后写在工程目录里的 `project.aivs.json`（与 `projects._manifest_of` 同形状）。"""
    width = int(row.get("width") or 1920)
    height = int(row.get("height") or 1080)
    return {
        "kind": MANIFEST_KIND,
        "app": settings.app_name,
        "id": row["id"],
        "name": row.get("name") or "导入的工程",
        "schema_version": schema_version,
        "width": width,
        "height": height,
        "fps": float(row.get("fps") or 25.0),
        "aspect_ratio": aspect_ratio(width, height),
        "duration_unit": row.get("duration_unit") or "frames",
        "created_at": row.get("created_at") or utc_now(),
        "updated_at": row.get("updated_at") or utc_now(),
    }


def _write_json(target: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再替换——与 `projects._write_manifest` 同一套原子写。"""
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise _disk_error(target, exc, "清单写入") from exc


def _omitted(kind: str, label: str, reason: str, count: int | None = None) -> dict[str, Any]:
    """「带不走的东西」清单里的一条。**跳过不是失败，但必须说出来。**"""
    return {"kind": kind, "label": label, "reason": reason, "count": count}


def _write_project_package(
    db_src: Path,
    target: Path,
    manifest: dict[str, Any],
    files: list[tuple[str, Path]],
    pid: str | None,
) -> int:
    """工程包 = 清单 + 一份 `project.db` 快照 + 素材文件。阻塞，请放进线程。"""
    tmp_db = target.with_name(target.name + ".db.tmp")
    try:
        try:
            _snapshot_db(db_src, tmp_db)
        except sqlite3.Error as exc:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "工程库快照失败",
                f"{db_src}: {type(exc).__name__}: {exc}",
                ["稍后重试（等当前生成任务写完）", "确认磁盘空间充足"],
            ) from exc
        texts = {MANIFEST_MEMBER: json.dumps(manifest, ensure_ascii=False, indent=2)}
        return _write_zip(target, texts, [(DB_MEMBER, tmp_db), *files], pid)
    finally:
        tmp_db.unlink(missing_ok=True)


def _project_omitted(include_generated: bool) -> list[dict[str, Any]]:
    out = [
        _omitted("cache", "抽出来的首尾帧与拆出来的音频（cache/）", "派生物，换机后重新生成就有"),
        _omitted("proxies", "预览代理流（proxies/）", "派生物，预览时会重新转码"),
        _omitted("presets", "ComfyUI 预设图", "预设属于「这台机器怎么调模型」，包里只带要求清单"),
        _omitted("settings", "服务地址与 API Key", "应用级配置，一律不进包"),
    ]
    if not include_generated:
        out.append(
            _omitted("generations", "生成的成片（generations/）", "本次导出没有勾选「带上成片」")
        )
    return out


# --- 场景包：一幕的设定摊成行级快照 ---

#: 一幕里**带不走的东西**是固定的几类，与这一幕的内容无关，所以写成一张表。
#: 每一条都是「跳过不是失败，但必须说出来」——UI 必须把这张清单原样显示。
SCENE_ALWAYS_OMITTED: tuple[tuple[str, str, str], ...] = (
    ("scene_link", "与别的幕之间的衔接（SceneLink）", "包里只有这一幕，跨幕的线到了新工程无处可接"),
    ("timeline", "时间线 / 轨道 / 片段", "成片装配是目标工程自己的事，导入后重新装配即可"),
    ("job", "生成队列与任务历史", "任务是那台机器上的运行记录，换机后没有意义"),
    ("director", "AI 导演的会话与提案", "对话属于原工程的过程记录"),
    ("workflow", "镜头上绑的 Workflow", "Workflow 是工程内的一行记录，导入后请重新指定"),
    (
        "transition_shot",
        "已经补出来的转场镜头",
        "包里带的是镜头之间那条线（ShotLink），导入后用「一键生成转场」重做",
    ),
)


def _scene_shot_status(status: str, include_generated: bool) -> str:
    """不带成片时把「已生成 / 待审 / 已定稿」降回 `ready`。

    带过去的 status 必须与包里真有的东西一致：一个标着 generated 却一段成片都没有的
    镜头，会让新工程的分镜板与时间线一起说谎。
    """
    if include_generated or status not in ("generated", "review", "locked"):
        return status
    return "ready"


async def _scene_bundle(
    pid: str, sid: str, include_generated: bool
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把一幕的设定摊成行级快照 + 「带不走的东西」清单。

    **只读**：一行库都不改。落库全部发生在 `import_scene`，且只走已有的写方法。
    """
    proj = project_of(pid)
    db = db_of(pid)
    scene = await fetch(db, Scene, sid, "场景")
    all_shots = await fetch_all(db, Shot, where=Shot.scene_id == sid, order_by=Shot.index_no)
    shots = [s for s in all_shots if s.kind != "transition"]
    kept = {s.id for s in shots}
    dropped_transitions = len(all_shots) - len(shots)

    links = [
        r for r in await fetch_all(db, ShotLink) if r.from_shot_id in kept and r.to_shot_id in kept
    ]
    scene_cast = sorted(
        await fetch_all(db, SceneCast, where=SceneCast.scene_id == sid),
        key=lambda r: r.index_no,
    )
    scene_locs = sorted(
        await fetch_all(db, SceneLocation, where=SceneLocation.scene_id == sid),
        key=lambda r: r.index_no,
    )
    shot_cast = [r for r in await fetch_all(db, ShotCast) if r.shot_id in kept]
    shot_props = [r for r in await fetch_all(db, ShotProp) if r.shot_id in kept]

    # --- 引用到的人物：形象 + 它的祖先（派生链断了形象就没法重建）---
    appearances = {a.id: a for a in await fetch_all(db, Appearance)}
    wanted_app: list[str] = []
    for aid in [r.appearance_id for r in scene_cast] + [r.appearance_id for r in shot_cast]:
        cursor: str | None = aid
        while cursor and cursor in appearances and cursor not in wanted_app:
            wanted_app.append(cursor)
            cursor = appearances[cursor].parent_id
    sheets = await fetch_all(db, SheetVersion)
    current_sheet = {s.appearance_id: s for s in sheets if s.is_current}
    stale_sheets = sum(1 for s in sheets if not s.is_current and s.appearance_id in set(wanted_app))

    chars: dict[str, dict[str, Any]] = {}
    for aid in wanted_app:
        row = appearances[aid]
        bucket = chars.setdefault(row.character_id, {"appearances": []})
        bucket["appearances"].append(row)
    characters = []
    for cid, bucket in chars.items():
        char = await fetch(db, Character, cid, "角色")
        ordered = _ordered_by_parent(bucket["appearances"])
        characters.append(
            {
                "id": char.id,
                **{f: getattr(char, f) for f in CHARACTER_FIELDS},
                "appearances": [
                    {
                        "id": a.id,
                        "parent_id": a.parent_id,
                        "name": a.name,
                        "overrides": a.overrides,
                        "is_default": int(a.is_default),
                        **{f: getattr(a, f) for f in INHERITABLE},
                        "sheet_asset_id": (
                            current_sheet[a.id].asset_id if a.id in current_sheet else None
                        ),
                    }
                    for a in ordered
                ],
            }
        )

    # --- 引用到的地点：变体 + 它的全部参考图 ---
    variants = {v.id: v for v in await fetch_all(db, LocationVariant)}
    wanted_var = [
        vid
        for vid in [r.location_variant_id for r in scene_locs] + [scene.location_variant_id or ""]
        if vid in variants
    ]
    var_refs = await fetch_all(db, LocationReference)
    locations: list[dict[str, Any]] = []
    seen_loc: dict[str, dict[str, Any]] = {}
    for vid in wanted_var:
        var = variants[vid]
        entry = seen_loc.get(var.location_id)
        if entry is None:
            loc = await fetch(db, Location, var.location_id, "地点")
            entry = {
                "id": loc.id,
                **{f: getattr(loc, f) for f in LOCATION_FIELDS},
                "variants": [],
            }
            seen_loc[loc.id] = entry
            locations.append(entry)
        if any(v["id"] == vid for v in entry["variants"]):
            continue
        entry["variants"].append(
            {
                "id": var.id,
                **{f: getattr(var, f) for f in VARIANT_FIELDS},
                "references": [
                    {"asset_id": r.asset_id, "camera": r.camera, "note": r.note}
                    for r in var_refs
                    if r.variant_id == vid
                ],
            }
        )

    # --- 引用到的道具：只带当前那一版参考图 ---
    prop_refs = await fetch_all(db, PropReference)
    props: list[dict[str, Any]] = []
    stale_prop_refs = 0
    for prop_id in dict.fromkeys(r.prop_id for r in shot_props):
        prop = await fetch(db, Prop, prop_id, "道具")
        mine = [r for r in prop_refs if r.prop_id == prop_id]
        stale_prop_refs += sum(1 for r in mine if not r.is_current)
        props.append(
            {
                "id": prop.id,
                **{f: getattr(prop, f) for f in PROP_FIELDS},
                "references": [
                    {"asset_id": r.asset_id, "note": r.note} for r in mine if r.is_current
                ],
            }
        )

    # --- 镜头本体 ---
    versions = {v.id: v for v in await fetch_all(db, GenerationVersion)}
    outside_prev = 0
    shot_rows: list[dict[str, Any]] = []
    for s in shots:
        adopted: dict[str, Any] | None = None
        if include_generated and s.current_version_id in versions:
            ver = versions[s.current_version_id or ""]
            if ver.asset_id:
                adopted = {
                    "kind": ver.kind,
                    "asset_id": ver.asset_id,
                    "duration": ver.duration,
                    "in_point": ver.in_point,
                    "out_point": ver.out_point,
                }
        prev = s.prev_shot_id if s.prev_shot_id in kept else None
        if s.prev_shot_id and prev is None:
            outside_prev += 1
        row = {
            "id": s.id,
            "index_no": s.index_no,
            **{f: getattr(s, f) for f in SHOT_FIELDS},
            "status": _scene_shot_status(s.status, include_generated),
            "prev_shot_id": prev,
            #: Workflow 是工程内的一行记录，带过去只会指向一个不存在的 id。
            "workflow_id": None,
            "cast": [r.appearance_id for r in shot_cast if r.shot_id == s.id],
            "props": [
                {"prop_id": r.prop_id, "state": r.state} for r in shot_props if r.shot_id == s.id
            ],
            "adopted": adopted,
        }
        shot_rows.append(row)

    scene_params = load_json(scene.params_json, {})
    source_asset_id = scene.source_asset_id if include_generated else None

    # --- 要带走的文件：上面所有引用到的资产 ---
    wanted_assets: list[str] = [
        *[a["sheet_asset_id"] for c in characters for a in c["appearances"]],
        *[r["asset_id"] for loc in locations for v in loc["variants"] for r in v["references"]],
        *[r["asset_id"] for p in props for r in p["references"]],
        *[s["first_frame_asset_id"] for s in shot_rows],
        *[s["last_frame_asset_id"] for s in shot_rows],
        *[s["adopted"]["asset_id"] for s in shot_rows if s["adopted"]],
        *[str(i) for i in (scene_params.get("refs") or [])],
        source_asset_id or "",
    ]
    asset_index = {a.id: a for a in await fetch_all(db, Asset)}
    asset_rows: list[dict[str, Any]] = []
    for aid in dict.fromkeys(a for a in wanted_assets if a):
        row_a = asset_index.get(str(aid))
        if row_a is None:  # 资产行已经不在了：按「这里没有这张图」处理，不抛
            continue
        asset_rows.append(
            {
                "id": row_a.id,
                "kind": row_a.kind,
                "path": row_a.path,
                "sha1": row_a.sha1,
                "mime": row_a.mime,
                "width": row_a.width,
                "height": row_a.height,
                "duration": row_a.duration,
                "size_bytes": _size_of(proj.dir / row_a.path),
                "member": FILES_PREFIX + _safe_member(row_a.path),
                "exists": (proj.dir / row_a.path).is_file(),
                "transient": row_a.kind in TRANSIENT_KINDS,
            }
        )

    bundle = {
        "index_no": scene.index_no,
        "scene": {
            **{f: getattr(scene, f) for f in SCENE_FIELDS},
            "params": scene_params,
            "source_asset_id": source_asset_id,
        },
        "shots": shot_rows,
        "shot_links": [
            {
                "from": r.from_shot_id,
                "to": r.to_shot_id,
                "mode": r.mode,
                "duration": r.duration,
                "prompt": r.prompt,
            }
            for r in links
        ],
        "scene_cast": [r.appearance_id for r in scene_cast],
        "scene_locations": [r.location_variant_id for r in scene_locs],
        "characters": characters,
        "locations": locations,
        "props": props,
        "assets": asset_rows,
    }
    omitted = [_omitted(kind, label, reason) for kind, label, reason in SCENE_ALWAYS_OMITTED]
    if dropped_transitions:
        omitted = [
            ({**o, "count": dropped_transitions} if o["kind"] == "transition_shot" else o)
            for o in omitted
        ]
    if outside_prev:
        omitted.append(
            _omitted(
                "prev_shot",
                "指向幕外镜头的「续接上游末帧」",
                "上游镜头不在这个包里，导入后这一项为空",
                outside_prev,
            )
        )
    if stale_sheets:
        omitted.append(
            _omitted("sheet_version", "角色表的历史版本", "只带当前采用的那一版", stale_sheets)
        )
    if stale_prop_refs:
        omitted.append(
            _omitted("prop_reference", "道具参考图的历史版本", "只带当前那一版", stale_prop_refs)
        )
    if not include_generated:
        omitted.append(
            _omitted("generations", "已采用的成片", "本次导出没有勾选「带上成片」；设定照旧完整")
        )
    omitted.append(
        _omitted("presets", "ComfyUI 预设图", "预设属于「这台机器怎么调模型」，包里只带要求清单")
    )
    omitted.append(_omitted("settings", "服务地址与 API Key", "应用级配置，一律不进包"))
    return bundle, omitted


def _appearance_patch_of(row: dict[str, Any]) -> dict[str, Any]:
    """派生形象只落包里明确覆写过的字段，其余留空继续继承父形象。

    照 `adopt._appearance_patch` 的规矩：`overrides` 才是真源，不靠「值是不是空」猜——
    猜的话，一个刻意留空以继承父形象的字段会在导入后变成「自己填了空字符串」。
    """
    fields: tuple[str, ...] = INHERITABLE
    if row.get("parent_id"):
        marked = {f for f in str(row.get("overrides") or "").split(",") if f}
        fields = tuple(f for f in INHERITABLE if f in marked)
    return {"name": row.get("name"), **{f: row.get(f) for f in fields}}


def _ordered_by_parent(rows: list[Appearance]) -> list[Appearance]:
    """父在子前。照 `adopt._ordered_appearances` 的做法，成环时不卡死。"""
    remaining = list(rows)
    done: set[str] = set()
    out: list[Appearance] = []
    while remaining:
        ready = [r for r in remaining if not r.parent_id or r.parent_id in done]
        if not ready:
            out.extend(remaining)
            break
        for row in ready:
            out.append(row)
            done.add(row.id)
        remaining = [r for r in remaining if r.id not in done]
    return out


def _scene_counts(bundle: dict[str, Any]) -> dict[str, int]:
    return {
        "shots": len(bundle["shots"]),
        "shot_links": len(bundle["shot_links"]),
        "characters": len(bundle["characters"]),
        "appearances": sum(len(c["appearances"]) for c in bundle["characters"]),
        "locations": len(bundle["locations"]),
        "variants": sum(len(loc["variants"]) for loc in bundle["locations"]),
        "props": len(bundle["props"]),
        "assets": len(bundle["assets"]),
    }


class PackageService:
    """导入导出的编排层。自己不写库，只调已有的写路径。"""

    # --- 工程包：导出 ---

    async def plan_project(self, pid: str, include_generated: bool = False) -> dict[str, Any]:
        """导出前的账单：多大、几个文件、有几条资产行的文件已经不在磁盘上、要什么环境。"""
        proj = project_of(pid)
        db = db_of(pid)
        groups: list[dict[str, Any]] = []
        for sub in PACK_DIRS:
            files = _walk(proj.dir / sub)
            groups.append(
                {
                    "dir": sub,
                    "files": len(files),
                    "bytes": sum(_size_of(p) for p in files),
                    "included": sub != "generations" or include_generated,
                }
            )
        db_bytes = _size_of(proj.dir / DB_NAME)
        asset_rows = await fetch_all(db, Asset)
        # 已经不在磁盘上的资产：导出不会失败（库里那一行照旧带走），但用户该知道
        # 这个包到了另一台机器上仍然会缺这几张图。
        missing = [
            {"id": r.id, "kind": r.kind, "path": r.path}
            for r in asset_rows
            if r.kind not in TRANSIENT_KINDS and not (proj.dir / r.path).exists()
        ]
        scenes = await fetch_all(db, Scene)
        shots = await fetch_all(db, Shot)
        return {
            "scope": SCOPE_PROJECT,
            "project": {"id": proj.id, "name": proj.name, "dir": proj.dir.as_posix()},
            "include_generated": include_generated,
            "db_bytes": db_bytes,
            "groups": groups,
            "files": sum(g["files"] for g in groups if g["included"]),
            "total_bytes": db_bytes + sum(g["bytes"] for g in groups if g["included"]),
            "counts": {
                "scenes": len(scenes),
                "shots": len(shots),
                "assets": len(asset_rows),
            },
            "missing": missing,
            "env": await _env_of(pid),
            "omitted": _project_omitted(include_generated),
            "suggested_filename": f"{_safe_stem(proj.name)}{PACKAGE_EXT}",
        }

    async def export_project(
        self,
        pid: str,
        out_dir: str,
        filename: str = "",
        include_generated: bool = False,
    ) -> dict[str, Any]:
        proj = project_of(pid)
        plan = await self.plan_project(pid, include_generated)
        target = _resolve_out(out_dir, filename or plan["suggested_filename"])
        files: list[tuple[str, Path]] = []
        for group in plan["groups"]:
            if not group["included"]:
                continue
            for path in _walk(proj.dir / str(group["dir"])):
                rel = path.relative_to(proj.dir).as_posix()
                files.append((f"{FILES_PREFIX}{rel}", path))

        manifest = {
            "kind": PACKAGE_KIND,
            "package_version": PACKAGE_VERSION,
            "package_id": new_id("package"),
            "scope": SCOPE_PROJECT,
            "app": settings.app_name,
            "schema_version": settings.schema_version,
            "created_at": utc_now(),
            "include_generated": include_generated,
            "project": {
                "name": proj.name,
                "width": proj.width,
                "height": proj.height,
                "fps": proj.fps,
                "duration_unit": proj.duration_unit,
                "aspect_ratio": aspect_ratio(proj.width, proj.height),
            },
            "counts": {**plan["counts"], "files": len(files)},
            "env": plan["env"],
        }
        size = await asyncio.to_thread(
            _write_project_package, proj.dir / DB_NAME, target, manifest, files, pid
        )
        log.info("package.exported", scope=SCOPE_PROJECT, path=target.as_posix(), bytes=size)
        bus.emit(
            Channel.SYSTEM,
            "package.exported",
            {"scope": SCOPE_PROJECT, "path": target.as_posix(), "bytes": size},
            project_id=pid,
        )
        return {
            "path": target.as_posix(),
            "bytes": size,
            "files": len(files),
            "manifest": manifest,
            "missing": plan["missing"],
            "omitted": plan["omitted"],
        }

    # --- 下载到用户那台机器（两个 scope 共用的落点） ---

    async def download_project(
        self, pid: str, include_generated: bool = False, filename: str = ""
    ) -> dict[str, Any]:
        """导出工程包并**交给浏览器下载**：包写在临时目录，流完就删。

        与 `export_project` 的差别只有落点：那边写进用户在**后端机器**上选的目录，这边
        根本不问目录——界面跑在浏览器里，拿不到那台机器的路径，用户要的也是「存到我自己
        这台电脑上」。写包本身完全是同一段代码，所以这里只负责摆一个临时落点。

        回的字典里多一个 `temp_dir`：**调用方（`api/packages.py`）流完必须删掉它**。
        """
        return await self._to_temp(
            lambda out_dir: self.export_project(pid, out_dir, filename, include_generated)
        )

    async def download_scene(
        self, pid: str, sid: str, include_generated: bool = False, filename: str = ""
    ) -> dict[str, Any]:
        """导出场景包并交给浏览器下载。规矩同 `download_project`。"""
        return await self._to_temp(
            lambda out_dir: self.export_scene(pid, sid, out_dir, filename, include_generated)
        )

    @staticmethod
    async def _to_temp(run: Callable[[str], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """在一个临时目录里跑一次导出。**失败要把临时目录带走**，不然攒一堆半截包。"""
        tmp = Path(tempfile.mkdtemp(prefix="aivs-pkg-dl-"))
        try:
            out = await run(tmp.as_posix())
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return {**out, "temp_dir": tmp.as_posix()}

    # --- 看一眼包里是什么（不解包） ---

    async def inspect(self, path: str) -> dict[str, Any]:
        """只读清单：范围、数量、环境要求，以及**本机的比对结果**。

        导入前必须先看这一份：缺哪份预设、provider 配没配、schema 能不能吃——
        等到入队时才报「选中的预设不存在」，用户已经忘了自己导入过什么。
        """
        target = _resolve_pkg(path)
        manifest = await asyncio.to_thread(_read_manifest, target)
        env = manifest.get("env") or {}
        return {
            "path": target.as_posix(),
            "bytes": _size_of(target),
            "scope": manifest.get("scope"),
            "package_version": manifest.get("package_version"),
            "package_id": manifest.get("package_id"),
            "app": manifest.get("app"),
            "created_at": manifest.get("created_at"),
            "schema_version": int(manifest.get("schema_version") or 0),
            "include_generated": bool(manifest.get("include_generated")),
            "project": manifest.get("project") or {},
            "scene": manifest.get("scene") or {},
            "counts": manifest.get("counts") or {},
            "omitted": manifest.get("omitted") or [],
            "env": env,
            "env_check": _env_check(manifest),
        }

    # --- 从用户那台机器传一个包上来（导入的主入口） ---

    async def stage(self, filename: str, chunks: AsyncIterator[bytes]) -> dict[str, Any]:
        """接收上传的包，落进暂存区，然后照 `inspect` 出**同一份**清单。

        浏览器能给的只有一个文件对象，没有后端机器上的路径，所以先落盘再走原来那几道门
        ——回的 `path` 就是暂存副本的路径，导入那两个入口原样收它，一行都不用改。

        三条：
          · **分片写**：一个带成片的工程包动辄几个 G，整包读进内存会把后端顶掉；
          · **看不懂就地删**：不是包 / 空文件时连暂存副本一起带走，不留垃圾；
          · **导入完（或用户取消）也要删**——见 `_drop_staged` 与 `discard_staged`。
        """
        _prune_uploads()
        holder = _upload_root() / new_id("package")
        target = holder / _upload_name(filename)
        written = 0
        try:
            holder.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as out:
                async for chunk in chunks:
                    if chunk:
                        out.write(chunk)
                        written += len(chunk)
            if written == 0:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "上传的包是空文件",
                    f"{filename or '(未命名)'} 一个字节都没有。",
                    ["确认选中的是导出的 .aivspkg 文件", "重新导出一份包再上传"],
                )
            info = await self.inspect(target.as_posix())
        except OSError as exc:
            shutil.rmtree(holder, ignore_errors=True)
            raise _disk_error(target, exc, "接收上传的包") from exc
        except BaseException:
            shutil.rmtree(holder, ignore_errors=True)
            raise
        log.info("package.staged", path=target.as_posix(), bytes=written)
        return {**info, "staged": True, "name": target.name}

    async def discard_staged(self, path: str) -> dict[str, Any]:
        """丢掉一份暂存副本（用户看了账单又取消）。

        **只认暂存区里的路径**：这个入口会删文件，指到别处一律拒绝——磁盘上那份包是
        用户自己的东西，不该被一个「取消导入」顺手删掉。
        """
        target = _resolve_pkg(path)
        if not _is_staged(target):
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "只能丢弃上传上来的临时副本",
                f"{target} 不在暂存区里。",
                ["这个入口只用来清理上传的包", "磁盘上的包请自己删"],
                {"path": target.as_posix()},
            )
        _drop_staged(target)
        return {"ok": True, "discarded": target.as_posix()}

    # --- 工程包：导入 ---

    async def import_project(self, path: str, directory: str) -> dict[str, Any]:
        """把工程包还原成一个目录并打开它。四道门都在这里，绝不覆盖用户的文件。"""
        src = _resolve_pkg(path)
        manifest = await asyncio.to_thread(_read_manifest, src)
        if manifest.get("scope") != SCOPE_PROJECT:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这不是一个工程包",
                f"{src.name} 的范围是 {manifest.get('scope')!r}，不能还原成一个工程。",
                ["先打开一个工程，再用「导入一幕」导入场景包"],
            )
        stored = int(manifest.get("schema_version") or 0)
        if stored > settings.schema_version:
            raise AppError(
                ErrorCode.SCHEMA_MISMATCH,
                "包由更新版本的应用创建",
                f"包的 schema {stored}，当前应用只支持 {settings.schema_version}。",
                ["升级应用后再导入", "或用创建它的版本导入"],
                {"path": src.as_posix()},
            )

        target = _resolve_target(directory)
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
                ["换一个空目录导入", "或直接用「打开项目」打开它"],
                {"dir": target.as_posix()},
            )
        if (target / DB_NAME).exists():
            raise AppError(
                ErrorCode.CONFLICT,
                "目录已被占用",
                f"{target} 存在无法识别的 {DB_NAME}。",
                OCCUPIED_SUGGESTIONS,
                {"dir": target.as_posix()},
            )

        unpacked = await asyncio.to_thread(_unpack, src, target, None, want_db=True)
        db_path = target / DB_NAME
        if not migrate.is_our_db(db_path):
            db_path.unlink(missing_ok=True)  # 只删我们刚写进去的那一个文件
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "包里的库不是工程库",
                f"{DB_MEMBER} 缺少 alembic_version 或 project 表。",
                ["确认这个包是本应用导出的", "重新导出一份包"],
            )
        row = await asyncio.to_thread(_reid_project, db_path)
        _write_json(target / MANIFEST_NAME, _project_manifest(row, stored))
        proj = await projects.open(target.as_posix())
        # 包已经还原成一个目录了，上传上来的那份临时副本没人再需要（几个 G 的东西）
        _drop_staged(src)
        log.info(
            "package.imported",
            scope=SCOPE_PROJECT,
            path=src.as_posix(),
            dir=target.as_posix(),
            project=proj.id,
        )
        return {
            "project": proj.to_dict(),
            "files": unpacked["files"],
            "package": {
                "path": src.as_posix(),
                "package_id": manifest.get("package_id"),
                "include_generated": bool(manifest.get("include_generated")),
            },
            "migrated_from": proj.migrated_from,
            "env_check": _env_check(manifest),
        }

    # --- 场景包：导出一幕的设定 ---

    async def plan_scene(
        self, pid: str, sid: str, include_generated: bool = False
    ) -> dict[str, Any]:
        bundle, omitted = await _scene_bundle(pid, sid, include_generated)
        files = [a for a in bundle["assets"] if a["exists"]]
        return {
            "scope": SCOPE_SCENE,
            "project": {"id": pid, "name": project_of(pid).name},
            "scene": {
                "id": sid,
                "title": bundle["scene"].get("title"),
                "index_no": bundle["index_no"],
            },
            "include_generated": include_generated,
            "counts": _scene_counts(bundle),
            "files": len(files),
            "total_bytes": sum(int(a["size_bytes"] or 0) for a in files),
            "missing": [
                {"id": a["id"], "kind": a["kind"], "path": a["path"]}
                for a in bundle["assets"]
                if not a["exists"]
            ],
            "env": await _env_of(pid),
            "omitted": omitted,
            "suggested_filename": (
                f"{_safe_stem(bundle['scene'].get('title') or '场景')}{PACKAGE_EXT}"
            ),
        }

    async def export_scene(
        self,
        pid: str,
        sid: str,
        out_dir: str,
        filename: str = "",
        include_generated: bool = False,
    ) -> dict[str, Any]:
        proj = project_of(pid)
        bundle, omitted = await _scene_bundle(pid, sid, include_generated)
        plan = await self.plan_scene(pid, sid, include_generated)
        target = _resolve_out(out_dir, filename or plan["suggested_filename"])
        files = [
            (str(a["member"]), proj.dir / str(a["path"])) for a in bundle["assets"] if a["exists"]
        ]
        manifest = {
            "kind": PACKAGE_KIND,
            "package_version": PACKAGE_VERSION,
            "package_id": new_id("package"),
            "scope": SCOPE_SCENE,
            "app": settings.app_name,
            "schema_version": settings.schema_version,
            "created_at": utc_now(),
            "include_generated": include_generated,
            "project": {"name": proj.name, "fps": proj.fps},
            "scene": {"title": bundle["scene"].get("title"), "index_no": bundle["index_no"]},
            "counts": {**plan["counts"], "files": len(files)},
            "omitted": omitted,
            "env": plan["env"],
        }
        texts = {
            MANIFEST_MEMBER: json.dumps(manifest, ensure_ascii=False, indent=2),
            SCENE_MEMBER: json.dumps(bundle, ensure_ascii=False, indent=2),
        }
        size = await asyncio.to_thread(_write_zip, target, texts, files, pid)
        log.info("package.exported", scope=SCOPE_SCENE, path=target.as_posix(), bytes=size)
        bus.emit(
            Channel.SYSTEM,
            "package.exported",
            {"scope": SCOPE_SCENE, "path": target.as_posix(), "bytes": size},
            project_id=pid,
        )
        return {
            "path": target.as_posix(),
            "bytes": size,
            "files": len(files),
            "manifest": manifest,
            "missing": plan["missing"],
            "omitted": omitted,
        }

    # --- 场景包：导入到任意已打开的工程 ---

    async def _scene_package(self, path: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        """读清单 + `data/scene.json`，并过掉两道门（范围、schema）。"""
        src = _resolve_pkg(path)
        manifest = await asyncio.to_thread(_read_manifest, src)
        if manifest.get("scope") != SCOPE_SCENE:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这不是一个场景包",
                f"{src.name} 的范围是 {manifest.get('scope')!r}。",
                ["工程包请在起始页用「导入工程包」"],
            )
        stored = int(manifest.get("schema_version") or 0)
        if stored > settings.schema_version:
            raise AppError(
                ErrorCode.SCHEMA_MISMATCH,
                "包由更新版本的应用创建",
                f"包的 schema {stored}，当前应用只支持 {settings.schema_version}。",
                ["升级应用后再导入", "或用创建它的版本导入"],
                {"path": src.as_posix()},
            )
        bundle = await asyncio.to_thread(_read_member_json, src, SCENE_MEMBER)
        return src, manifest, bundle

    async def plan_scene_import(
        self, path: str, pid: str, reuse_by_name: bool = True
    ) -> dict[str, Any]:
        """导入前账单：新建几个 / 复用几个，以及包里明确带不走的那些东西。"""
        src, manifest, bundle = await self._scene_package(path)
        db = db_of(pid)
        local_chars = {c.name for c in await fetch_all(db, Character)}
        local_locs = {r.name for r in await fetch_all(db, Location)}
        local_props = {r.name for r in await fetch_all(db, Prop)}
        local_sha1 = {r.sha1 for r in await fetch_all(db, Asset) if r.sha1}

        def action(name: str, pool: set[str]) -> str:
            return "reuse" if reuse_by_name and name in pool else "create"

        entities = [
            *[
                {
                    "kind": "character",
                    "name": c.get("name") or "",
                    "action": action(c.get("name") or "", local_chars),
                }
                for c in bundle.get("characters") or []
            ],
            *[
                {
                    "kind": "location",
                    "name": r.get("name") or "",
                    "action": action(r.get("name") or "", local_locs),
                }
                for r in bundle.get("locations") or []
            ],
            *[
                {
                    "kind": "prop",
                    "name": r.get("name") or "",
                    "action": action(r.get("name") or "", local_props),
                }
                for r in bundle.get("props") or []
            ],
        ]
        asset_rows = bundle.get("assets") or []
        reused_files = [a for a in asset_rows if a.get("sha1") and a["sha1"] in local_sha1]
        return {
            "scope": SCOPE_SCENE,
            "path": src.as_posix(),
            "target_project": {"id": pid, "name": project_of(pid).name},
            "scene": manifest.get("scene") or {},
            "reuse_by_name": reuse_by_name,
            "counts": _scene_counts(bundle),
            "entities": entities,
            "assets": {
                "total": len(asset_rows),
                "reuse": len(reused_files),
                "copy": len(asset_rows) - len(reused_files),
            },
            "omitted": manifest.get("omitted") or [],
            "env": manifest.get("env") or {},
            "env_check": _env_check(manifest),
        }

    async def _import_assets(
        self,
        pid: str,
        bundle: dict[str, Any],
        staging: Path,
        package_id: str,
        tally: dict[str, int],
    ) -> dict[str, str]:
        """包里的文件 → 目标工程的资产登记。返回 `old_id → new_id`。

        与 `adopt._adopt_file` 一字不差的做法：sha1 命中就复用那条登记（同一部片子里
        同一张角色表被两个包带过来是常态，复制第二份只是浪费磁盘），否则
        `register_path(copy=True)`；出处一律只记在 `meta_json` 里，**不加任何列、不加外键**
        ——库 / 包关掉之后工程照常打开，这一点与采用是同一条规矩。
        """
        out: dict[str, str] = {}
        for row in bundle.get("assets") or []:
            src = staging / str(row.get("path") or "")
            sha1 = str(row.get("sha1") or "")
            hit = await assets.by_sha1(pid, sha1) if sha1 else None
            if hit is not None:
                asset = hit
                tally["assets_reused"] += 1
            elif src.is_file():
                asset = await assets.register_path(
                    pid, str(row.get("kind") or "image"), src.as_posix(), source="imported"
                )
                tally["assets_new"] += 1
            else:  # 包里没有这个文件（导出时它就已经不在磁盘上了）——跳过，不抛
                tally["assets_missing"] += 1
                continue
            await assets.merge_meta(
                pid,
                asset["id"],
                {
                    "package_id": package_id,
                    "package_sha1": sha1 or None,
                    "imported_at": utc_now(),
                },
            )
            out[str(row.get("id"))] = asset["id"]
        return out

    async def _import_characters(
        self,
        pid: str,
        bundle: dict[str, Any],
        amap: dict[str, str],
        reuse_by_name: bool,
        report: list[dict[str, Any]],
    ) -> dict[str, str]:
        """人物 → `appearance_id` 重映射表。默认按名字精确匹配复用已有实体。

        同一部片子里多幕引用同一个「林小雨」是常态，每导一幕多出一个同名角色才是 bug。
        复用时形象也按名字对——对不上的才新建，所以重复导入同一个包不会长出第二套形象。
        """
        db = db_of(pid)
        local = {c.name: c for c in await fetch_all(db, Character)}
        appearances = await fetch_all(db, Appearance)
        amap_out: dict[str, str] = {}
        for row in bundle.get("characters") or []:
            name = str(row.get("name") or "").strip()
            existing = local.get(name) if reuse_by_name else None
            if existing is not None:
                cid = existing.id
                slot: dict[str, Any] | None = None
                by_name = {a.name: a.id for a in appearances if a.character_id == cid}
            else:
                char = await cast.create_character(pid, {f: row.get(f) for f in CHARACTER_FIELDS})
                cid = char["id"]
                # create_character 顺手建了一个空的「默认形象」：包里的第一个根形象落在
                # 这个空位上，否则工程里会多出一个谁也没填的形象（照 adopt 的做法）。
                slot = next(
                    (a for a in await cast.list_appearances(pid, cid) if a["is_default"]), None
                )
                by_name = {}
            report.append(
                {
                    "kind": "character",
                    "name": name,
                    "action": "reuse" if existing is not None else "create",
                    "target_id": cid,
                }
            )
            for app_row in row.get("appearances") or []:
                app_name = str(app_row.get("name") or "").strip()
                patch = _appearance_patch_of(app_row)
                hit = by_name.get(app_name)
                if hit is not None:
                    amap_out[str(app_row.get("id"))] = hit
                    continue
                parent = amap_out.get(str(app_row.get("parent_id") or ""))
                if slot is not None and not app_row.get("parent_id"):
                    created = await cast.update_appearance(pid, slot["id"], patch)
                    slot = None
                else:
                    created = await cast.create_appearance(pid, cid, patch, parent_id=parent)
                amap_out[str(app_row.get("id"))] = created["id"]
                by_name[app_name] = created["id"]
                sheet = amap.get(str(app_row.get("sheet_asset_id") or ""))
                if sheet:
                    await cast.add_sheet(pid, created["id"], sheet, source="imported")
                if app_row.get("is_default"):
                    await cast.set_default_appearance(pid, created["id"])
        return amap_out

    async def _import_locations(
        self,
        pid: str,
        bundle: dict[str, Any],
        amap: dict[str, str],
        reuse_by_name: bool,
        report: list[dict[str, Any]],
    ) -> dict[str, str]:
        """地点 → `location_variant_id` 重映射表。复用规则与人物同一套。"""
        db = db_of(pid)
        local = {r.name: r for r in await fetch_all(db, Location)}
        variants = await fetch_all(db, LocationVariant)
        vmap: dict[str, str] = {}
        for row in bundle.get("locations") or []:
            name = str(row.get("name") or "").strip()
            existing = local.get(name) if reuse_by_name else None
            if existing is not None:
                lid = existing.id
                by_name = {v.name: v.id for v in variants if v.location_id == lid}
            else:
                loc = await world.create_location(pid, {f: row.get(f) for f in LOCATION_FIELDS})
                lid = loc["id"]
                by_name = {}
            report.append(
                {
                    "kind": "location",
                    "name": name,
                    "action": "reuse" if existing is not None else "create",
                    "target_id": lid,
                }
            )
            for var in row.get("variants") or []:
                var_name = str(var.get("name") or "").strip()
                hit = by_name.get(var_name)
                if hit is not None:
                    vmap[str(var.get("id"))] = hit
                    continue
                created = await world.create_variant(
                    pid, lid, {f: var.get(f) for f in VARIANT_FIELDS}
                )
                vmap[str(var.get("id"))] = created["id"]
                by_name[var_name] = created["id"]
                for ref in var.get("references") or []:
                    asset_id = amap.get(str(ref.get("asset_id") or ""))
                    if not asset_id:
                        continue
                    await world.add_variant_reference(
                        pid, created["id"], asset_id, ref.get("camera"), ref.get("note")
                    )
        return vmap

    async def _import_props(
        self,
        pid: str,
        bundle: dict[str, Any],
        amap: dict[str, str],
        reuse_by_name: bool,
        report: list[dict[str, Any]],
    ) -> dict[str, str]:
        db = db_of(pid)
        local = {r.name: r for r in await fetch_all(db, Prop)}
        pmap: dict[str, str] = {}
        for row in bundle.get("props") or []:
            name = str(row.get("name") or "").strip()
            existing = local.get(name) if reuse_by_name else None
            if existing is not None:
                pmap[str(row.get("id"))] = existing.id
                report.append(
                    {"kind": "prop", "name": name, "action": "reuse", "target_id": existing.id}
                )
                continue
            prop = await world.create_prop(pid, {f: row.get(f) for f in PROP_FIELDS})
            pmap[str(row.get("id"))] = prop["id"]
            report.append(
                {"kind": "prop", "name": name, "action": "create", "target_id": prop["id"]}
            )
            for ref in row.get("references") or []:
                asset_id = amap.get(str(ref.get("asset_id") or ""))
                if asset_id:
                    await world.add_prop_reference(pid, prop["id"], asset_id, ref.get("note"))
        return pmap

    async def import_scene(self, path: str, pid: str, reuse_by_name: bool = True) -> dict[str, Any]:
        """把一幕的设定落进**任意已打开的工程**。

        全程只转调已有的写方法（`story` / `sequence` / `cast` / `world` / `assets` /
        `generation`），自己一行 SQL 都不写——包不该成为第二条写库路径，那样每加一个字段
        都得改两处，迟早分叉。id 全部重映射：包里的 id 属于原工程，直接落库会撞。
        """
        src, manifest, bundle = await self._scene_package(path)
        proj = project_of(pid)
        package_id = str(manifest.get("package_id") or "")
        tally = {"assets_new": 0, "assets_reused": 0, "assets_missing": 0}
        report: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="aivs-pkg-") as tmp:
            staging = Path(tmp)
            await asyncio.to_thread(_unpack, src, staging, pid, want_db=False)
            amap = await self._import_assets(pid, bundle, staging, package_id, tally)
            app_map = await self._import_characters(pid, bundle, amap, reuse_by_name, report)
            var_map = await self._import_locations(pid, bundle, amap, reuse_by_name, report)
            prop_map = await self._import_props(pid, bundle, amap, reuse_by_name, report)

            scene_in = dict(bundle.get("scene") or {})
            params = dict(scene_in.pop("params", None) or {})
            if params.get("refs"):
                params["refs"] = [amap[r] for r in params["refs"] if r in amap]
            scene_patch = {f: scene_in.get(f) for f in SCENE_FIELDS}
            scene_patch["location_variant_id"] = var_map.get(
                str(scene_in.get("location_variant_id") or "")
            )
            if params:
                scene_patch["params"] = params
            scene_patch["source_asset_id"] = amap.get(str(scene_in.get("source_asset_id") or ""))
            scene = await story.create_scene(pid, scene_patch)
            sid = scene["id"]

            wanted_cast = [app_map[a] for a in bundle.get("scene_cast") or [] if a in app_map]
            if wanted_cast:
                await story.set_scene_cast(pid, sid, wanted_cast)
            wanted_locs = [var_map[v] for v in bundle.get("scene_locations") or [] if v in var_map]
            if wanted_locs:
                await story.set_scene_locations(pid, sid, wanted_locs)

            shot_map: dict[str, str] = {}
            for row in bundle.get("shots") or []:
                patch = {f: row.get(f) for f in SHOT_FIELDS}
                #: 三处 id 必须重映射，且**先落成空**：上游镜头这一趟可能还没建出来。
                patch["prev_shot_id"] = None
                patch["workflow_id"] = None
                for slot in ("first_frame_asset_id", "last_frame_asset_id"):
                    patch[slot] = amap.get(str(row.get(slot) or "")) or None
                shot = await story.create_shot(pid, sid, patch)
                shot_map[str(row.get("id"))] = shot["id"]
                cast_ids = [app_map[a] for a in row.get("cast") or [] if a in app_map]
                if cast_ids:
                    await story.set_shot_cast(pid, shot["id"], cast_ids)
                props_in = [
                    {"prop_id": prop_map[p["prop_id"]], "state": p.get("state") or "present"}
                    for p in row.get("props") or []
                    if p.get("prop_id") in prop_map
                ]
                if props_in:
                    await story.set_shot_props(pid, shot["id"], props_in)

            # 镜头全部建好之后再补「续接上游末帧」与镜头之间那条线（都要两头都在）。
            for row in bundle.get("shots") or []:
                prev = shot_map.get(str(row.get("prev_shot_id") or ""))
                if prev:
                    await story.update_shot(
                        pid, shot_map[str(row.get("id"))], {"prev_shot_id": prev}
                    )
            links = 0
            for link in bundle.get("shot_links") or []:
                a, b = shot_map.get(str(link.get("from"))), shot_map.get(str(link.get("to")))
                if not a or not b:
                    continue
                await sequence.set_shot_link(
                    pid,
                    a,
                    b,
                    mode=str(link.get("mode") or "cut"),
                    duration=link.get("duration"),
                    prompt=link.get("prompt"),
                )
                links += 1

            # 带成片的包：走**已有的**手动版本入口，不伪造 job 血缘（硬约束 3 不受影响）。
            adopted = 0
            for row in bundle.get("shots") or []:
                info = row.get("adopted")
                asset_id = amap.get(str((info or {}).get("asset_id") or ""))
                if not info or not asset_id:
                    continue
                await generation.add_version(
                    pid,
                    shot_map[str(row.get("id"))],
                    asset_id=asset_id,
                    kind=str(info.get("kind") or "video"),
                    source="manual",
                    duration=info.get("duration"),
                    in_point=info.get("in_point"),
                    out_point=info.get("out_point"),
                    params={"imported_from_package": package_id},
                )
                adopted += 1

        # 落库全部完成，上传上来的那份临时副本可以走了（走成 `_drop_staged` 的空操作也无所谓）
        _drop_staged(src)
        log.info(
            "package.scene_imported",
            project=pid,
            scene=sid,
            shots=len(shot_map),
            assets_new=tally["assets_new"],
        )
        bus.emit(
            Channel.SYSTEM,
            "package.imported",
            {"scope": SCOPE_SCENE, "project_id": pid, "scene_id": sid, "path": src.as_posix()},
            project_id=pid,
        )
        return {
            "scope": SCOPE_SCENE,
            "project": {"id": pid, "name": proj.name},
            "scene": {"id": sid, "title": scene["title"], "index_no": scene["index_no"]},
            "shots": len(shot_map),
            "shot_links": links,
            "adopted_versions": adopted,
            "assets": tally,
            "entities": report,
            "reuse_by_name": reuse_by_name,
            "omitted": manifest.get("omitted") or [],
            "env_check": _env_check(manifest),
        }


packages = PackageService()
