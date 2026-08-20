"""目录浏览（Phase 1）。

为什么后端要做这件事：浏览器拿不到绝对路径——`showDirectoryPicker()` 只给句柄，
`<input webkitdirectory>` 只给相对名——而工程目录必须是一个能落盘的绝对路径。
所以「选文件夹」这件事只能由本机后端提供，好处是浏览器与 Tauri 壳行为完全一致。

安全边界（这是把本机目录名暴露在 HTTP 上，必须写清楚）：
  1. 只列**目录**，不列文件，也绝不返回任何文件内容；
  2. 后端只监听 127.0.0.1，开着 require_handshake 时还要带 token；
  3. 拒绝一切读不动的路径，用结构化错误说清是「不存在」还是「没权限」。
"""

from __future__ import annotations

import os
import string
from pathlib import Path
from typing import Any

from app.core.errors import AppError, ErrorCode
from app.services.library import DB_NAME as LIBRARY_DB
from app.services.library import MANIFEST_NAME as LIBRARY_MANIFEST
from app.services.projects import DB_NAME, MANIFEST_NAME

#: 一次最多列这么多子目录。素材盘里塞几万个文件夹时，UI 不该被一次性拖死。
MAX_ENTRIES = 2000


def resolve_dir(raw: str) -> Path:
    """把用户输入的路径变成绝对路径。空串与非法字符都是结构化错误。"""
    text = raw.strip().strip('"')
    if not text:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有指定目录",
            "目录路径不能为空。",
            ["从左边的驱动器或常用位置开始浏览"],
        )
    try:
        return Path(text).expanduser().resolve()
    except OSError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "目录路径无效",
            f"{text}: {type(exc).__name__}: {exc}",
            ["检查路径是否含非法字符", "改用绝对路径"],
        ) from exc


def _not_a_dir(path: Path) -> AppError:
    return AppError(
        ErrorCode.NOT_FOUND,
        "目录不存在",
        f"{path.as_posix()} 不存在，或者它是一个文件而不是目录。",
        ["检查路径拼写", "回到上一级重新选择"],
        {"path": path.as_posix()},
    )


def _denied(path: Path, exc: OSError) -> AppError:
    return AppError(
        ErrorCode.VALIDATION_ERROR,
        "没有权限读取这个目录",
        f"{path.as_posix()}: {type(exc).__name__}: {exc}",
        ["换一个你有权限的目录", "以有权限的账号运行应用"],
        {"path": path.as_posix()},
    )


def is_project_dir(path: Path) -> bool:
    """工程目录 = 有清单或有 project.db。手动删了清单的目录也还能被认出来。"""
    try:
        return (path / MANIFEST_NAME).is_file() or (path / DB_NAME).is_file()
    except OSError:
        return False


def is_library_dir(path: Path) -> bool:
    try:
        return (path / LIBRARY_MANIFEST).is_file() or (path / LIBRARY_DB).is_file()
    except OSError:
        return False


def _has_children(path: Path) -> bool:
    """只探到第一个子目录就返回——不为了画一个箭头把整棵树走一遍。"""
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    return True
    except OSError:
        return False
    return False


def _entry_of(entry: os.DirEntry[str]) -> dict[str, Any]:
    path = Path(entry.path)
    return {
        "name": entry.name,
        "path": path.as_posix(),
        "is_project": is_project_dir(path),
        "is_library": is_library_dir(path),
        "has_children": _has_children(path),
        "writable": os.access(entry.path, os.W_OK),
    }


def _windows_drives() -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:/")
        # 光驱 / 已断开的网络盘会让 exists() 慢，但这是一次性的，且没有更便宜的办法
        try:
            if not drive.exists():
                continue
        except OSError:
            continue
        roots.append({"name": f"{letter}:", "path": drive.as_posix(), "kind": "drive"})
    return roots


def roots() -> dict[str, Any]:
    """驱动器 + 常用位置。用户从这里起步，不用先知道自己在哪。"""
    home = Path.home()
    out: list[dict[str, Any]] = []
    if os.name == "nt":
        out.extend(_windows_drives())
    else:
        out.append({"name": "/", "path": "/", "kind": "drive"})

    for name, candidate in (
        ("主目录", home),
        ("桌面", home / "Desktop"),
        ("文档", home / "Documents"),
    ):
        try:
            if candidate.is_dir():
                out.append({"name": name, "path": candidate.as_posix(), "kind": "place"})
        except OSError:
            continue
    return {"roots": out, "home": home.as_posix(), "sep": "/"}


def listdir(raw: str) -> dict[str, Any]:
    """列出一个目录里的子目录。文件一律不列——这个端点不该成为文件浏览器。"""
    path = resolve_dir(raw)
    if not path.is_dir():
        raise _not_a_dir(path)

    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
                entries.append(_entry_of(entry))
    except PermissionError as exc:
        raise _denied(path, exc) from exc
    except OSError as exc:
        raise _denied(path, exc) from exc

    entries.sort(key=lambda e: e["name"].lower())
    parent = path.parent
    return {
        "path": path.as_posix(),
        # 盘符的 parent 是它自己，这时候不给「上一级」
        "parent": None if parent == path else parent.as_posix(),
        "name": path.name or path.as_posix(),
        "is_project": is_project_dir(path),
        "is_library": is_library_dir(path),
        "writable": os.access(path, os.W_OK),
        "entries": entries,
        "truncated": truncated,
        "crumbs": _crumbs(path),
    }


def _crumbs(path: Path) -> list[dict[str, str]]:
    """面包屑：从盘符一路到当前目录，每一级都能点回去。"""
    chain = [path, *path.parents]
    out = []
    for node in reversed(chain):
        out.append({"name": node.name or node.as_posix(), "path": node.as_posix()})
    return out


def mkdir(parent_raw: str, name: str) -> dict[str, Any]:
    """在 parent 下新建一个文件夹。新建工程时「在这里建个新的」要用它。"""
    parent = resolve_dir(parent_raw)
    if not parent.is_dir():
        raise _not_a_dir(parent)

    leaf = name.strip().strip('"')
    if not leaf or leaf in {".", ".."} or any(c in leaf for c in '/\\:*?"<>|'):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "文件夹名不合法",
            f'「{name}」不能作为文件夹名：不能为空，也不能含 / \\ : * ? " < > | 这些字符。',
            ["换一个只含中英文、数字、下划线的名字"],
        )

    target = parent / leaf
    if target.exists():
        raise AppError(
            ErrorCode.CONFLICT,
            "同名文件夹已存在",
            f"{target.as_posix()} 已经存在。",
            ["换一个名字", "直接选中已存在的那个文件夹"],
            {"path": target.as_posix()},
        )
    try:
        target.mkdir(parents=False)
    except PermissionError as exc:
        raise _denied(parent, exc) from exc
    except OSError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "新建文件夹失败",
            f"{target.as_posix()}: {type(exc).__name__}: {exc}",
            ["换一个有写权限的位置", "检查磁盘是否已满或只读"],
            {"path": target.as_posix()},
        ) from exc
    return {"path": target.as_posix(), "name": leaf}
