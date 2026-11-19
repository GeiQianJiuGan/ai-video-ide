"""图片缩略图：清单与卡片上的小方块不该去下载几 MB 的原图。

AI 出图几乎都是 1–4 K 的 PNG（实测中位数 3.6 MB），而界面上画它的格子只有 40–200 px。
线上部署时一屏几十张原图要下几十到上百 MB——这是「网页很卡」的另一半原因。

做法：`GET /files/{rel}?w=256` 时用 FFmpeg 缩一份 WebP 落进 `cache/thumbs/`（派生物，
工程包不带），之后直接读缓存。缓存键带源文件的 mtime + 大小，原图被替换后自动失效。
宽度归到几档固定值，避免任意 w 把缓存撑爆。任何一步失败都退回原图——缩略图只是加速，
不能让一张图因为它显示不出来。
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from pathlib import Path

from app.core import ffmpeg as ffmpeg_tool
from app.core.logging import get_logger

log = get_logger("thumbs")

#: 允许的宽度档位（px）。请求的 w 向上取到最近一档，超过最大档按最大档。
WIDTHS = (128, 256, 512, 1024)
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
CACHE_DIR = Path("cache") / "thumbs"


def bucket(width: int) -> int:
    return next((w for w in WIDTHS if w >= width), WIDTHS[-1])


class ThumbService:
    def __init__(self) -> None:
        #: 一屏几十张图同时进来时，最多两个 FFmpeg 并行，别把 CPU 打满。
        self._gate = asyncio.Semaphore(2)
        self._locks: dict[Path, asyncio.Lock] = {}

    def cached(self, root: Path, src: Path, width: int) -> Path | None:
        """缓存命中就给出路径；不是图片或还没生成给 None。"""
        target = self._target(root, src, width)
        return target if target is not None and target.is_file() else None

    def _target(self, root: Path, src: Path, width: int) -> Path | None:
        if src.suffix.lower() not in IMAGE_SUFFIXES:
            return None
        try:
            stat = src.stat()
            rel = src.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            return None
        key = hashlib.sha1(f"{rel}|{stat.st_mtime_ns}|{stat.st_size}".encode()).hexdigest()[:24]
        return root / CACHE_DIR / f"{key}_{bucket(width)}.webp"

    async def thumb(self, root: Path, src: Path, width: int) -> Path | None:
        """给 src 一份宽度不超过 `bucket(width)` 的 WebP；做不出来给 None（调用方回原图）。"""
        target = self._target(root, src, width)
        if target is None:
            return None
        if target.is_file():
            return target
        found = ffmpeg_tool.locate("ffmpeg")
        if not found.available or not found.path:
            return None
        lock = self._locks.setdefault(target, asyncio.Lock())
        try:
            async with lock:
                if target.is_file():
                    return target
                async with self._gate:
                    return await self._render(found.path, src, target, bucket(width))
        finally:
            if not lock.locked():
                self._locks.pop(target, None)

    async def _render(self, binary: str, src: Path, target: Path, width: int) -> Path | None:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{target.stem}.{secrets.token_hex(4)}.part.webp")
        args = [
            binary,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({width},iw)':-1",
            "-c:v",
            "libwebp",
            "-quality",
            "80",
            str(partial),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
        except OSError as exc:
            log.warning("thumbs.spawn_failed", src=str(src), error=str(exc))
            return None
        if proc.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            log.warning(
                "thumbs.render_failed",
                src=str(src),
                code=proc.returncode,
                raw=(stderr or b"").decode("utf-8", "replace")[-500:],
            )
            return None
        partial.replace(target)
        return target


thumbs = ThumbService()
