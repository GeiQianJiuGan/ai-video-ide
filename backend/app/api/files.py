"""工程与素材库的静态文件读取。

为什么需要它：Asset.path 是相对工程目录的字符串，前端拿到它没法直接显示。
浏览器不能读本机磁盘，Tauri 的 asset: 协议又要按工程逐个放行 scope——
所以缩略图、视频预览统一走这一个回环端点，浏览器与桌面壳行为完全一致，
并且 Range 由 Starlette 的 FileResponse 原生支持（时间线拖进度条要它）。

安全边界：只允许读「工程目录 / 素材库目录」之内的文件。任何解析后落在根目录
之外的路径都直接拒绝，绝不因为拼接出了合法路径就放过。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.core.errors import AppError, ErrorCode
from app.services.base import project_of
from app.services.library import library

router = APIRouter(tags=["files"])

#: 浏览器缓存时长。资产文件名带内容 sha1 前缀，改内容必然换名字，缓存是安全的。
CACHE_CONTROL = "private, max-age=3600"


def resolve_within(root: Path, rel: str) -> Path:
    """把相对路径解析到 root 之内；越界就报错而不是悄悄返回别处的文件。"""
    text = rel.strip().replace("\\", "/")
    if not text:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有指定文件",
            "文件路径不能为空。",
            ["检查 Asset.path 是否为空"],
        )
    try:
        root_real = root.resolve()
        target = (root_real / text).resolve()
    except OSError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "文件路径无效",
            f"{text}: {type(exc).__name__}: {exc}",
            ["检查路径是否含非法字符"],
        ) from exc

    # resolve() 会跟随符号链接，所以指向工程外的链接也会在这里被挡下
    if not target.is_relative_to(root_real):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "路径越界",
            f"{text} 解析后落在允许的目录之外，已拒绝。",
            ["只能读取工程目录或素材库目录内的文件"],
            {"path": text},
        )
    return target


def serve(root: Path, rel: str, what: str) -> FileResponse:
    """读取 root 之内的一个文件。缺文件是结构化 404，不是空响应。"""
    target = resolve_within(root, rel)
    if not target.exists():
        raise AppError(
            ErrorCode.NOT_FOUND,
            "文件不存在",
            f"{rel} 在{what}里找不到（可能已被手动删除或移动）。",
            ["在资产库里重新扫描孤儿资产", "确认没有在文件管理器里删掉它"],
            {"path": rel},
        )
    if not target.is_file():
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "这不是一个文件",
            f"{rel} 是目录，无法作为文件读取。",
            ["检查请求的路径"],
            {"path": rel},
        )
    mime, _ = mimetypes.guess_type(target.name)
    return FileResponse(
        target,
        media_type=mime or "application/octet-stream",
        headers={"Cache-Control": CACHE_CONTROL},
    )


@router.get("/projects/{pid}/files/{rel:path}")
async def project_file(pid: str, rel: str) -> FileResponse:
    """读工程目录内的文件，rel 就是 Asset.path。"""
    return serve(project_of(pid).dir, rel, "工程目录")


@router.get("/library/files/{rel:path}")
async def library_file(rel: str) -> FileResponse:
    """读素材库目录内的文件，rel 就是 LibAsset.path。

    没配置素材库时 `library.current()` 会给出带建议的 NOT_FOUND，不会白屏。
    """
    lib = await library.current()
    return serve(lib.dir, rel, "素材库")
