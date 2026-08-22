"""从视频里拆出声音（时间线的音频轨靠它）。

「把某一块视频的声音拆出来成为独立的音频轨」有两种做法：让音频轨直接指回那段视频文件
（什么都不生成），或者真的抽出一份音频。这里选后者，理由三条：

  · **能不能拆必须先说清**——AI 生成的视频绝大多数根本没有音轨。指回去只会得到一条
    「看着有片段却一点声音都没有」的假轨，而 ffprobe 问一句就知道；没有音轨时报
    结构化错误，不造一条静音轨糊过去（硬约束 4）；
  · 浏览器里 `<audio src="x.mp4">` 各家行为不一致，预览器要的是一份谁都能解的音频；
  · 导出时音频轨与画面各是一路输入，混音图不用再猜「某个输入有没有第二条流」。

拆出来的音频**和抽出来的帧是同一种东西：临时资源，不是工程资产**——落
`cache/audio/`（`assets.KIND_DIR["clip_audio"]`）、在 `assets.TRANSIENT_KINDS` 里，
不进资产总账也不算孤儿，而且**源成片一删它就跟着删**（`assets.delete` 走下面的
`derived_audio`）。它仍然是一行 `Asset`：时间线片段是靠 `asset_id → path` 找文件的，
从登记里拿掉就得另造一套解析。用户自己导入的音乐不走这里，那是 `kind="audio"` 的真资产。

探测（`peek`）与拆分（`extract`）刻意分成两个能力：

  · `extract` 缺 FFmpeg 就是明确失败——用户按了「拆出声音」，不能假装拆了；
  · `peek` **永不抛**，探不了回 `None`。导出预检要靠它判断「这段画面自带声音吗」，
    ffprobe 不在时那里应该退化成一条警告，而不是让整个导出预检崩掉。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core import ffmpeg as ffmpeg_tool
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.persistence.models_world import Asset
from app.services import assets as asset_module
from app.services.assets import assets as asset_service
from app.services.base import db_of, fetch, load_json, project_of

log = get_logger("audio")

#: 拆出来的音频统一转成 aac/m4a：浏览器全都能播，FFmpeg 混音时也不用再转一遍。
AUDIO_SUFFIX = ".m4a"
AUDIO_BITRATE = "192k"


@dataclass(frozen=True)
class Probe:
    """ffprobe 看到的东西。`duration` 认不出来时是 `None`，绝不填一个猜的数。"""

    has_audio: bool
    duration: float | None
    codec: str | None


def derived_audio(rows: list[Asset], source_asset_id: str) -> list[Asset]:
    """从某个资产拆出来的临时音频。

    `services/assets.py::delete` 靠它做级联清理：**成片删了，从它拆的声音也得删**。
    和 `frames.derived_frames` 一样，`meta_json.from_asset_id` 的解读只放在本模块。
    """
    out: list[Asset] = []
    for asset in rows:
        if asset.kind != "clip_audio":
            continue
        if load_json(asset.meta_json, {}).get("from_asset_id") == source_asset_id:
            out.append(asset)
    return out


class AudioService:
    def __init__(self) -> None:
        #: (路径, 大小, mtime) → 探测结果。同一个文件在一次导出预检里会被问很多次，
        #: 每次起一个 ffprobe 太贵；把 mtime 放进 key，文件被换掉时自然失效。
        self._probe_cache: dict[tuple[str, int, int], Probe] = {}

    # --- 探测 ---

    async def peek(self, path: Path) -> Probe | None:
        """探测一个文件，**永不抛**：ffprobe 不在、文件读不了、输出解不开都回 `None`。

        调用方拿到 `None` 的正确反应是「这件事我不知道」——比如导出预检就把它变成一条
        警告写进 plan，而不是断言这段没有声音。
        """
        try:
            return await self.probe_file(path)
        except AppError:
            return None

    async def probe_file(self, path: Path) -> Probe:
        """探测一个文件。ffprobe 缺失是 `FFMPEG_MISSING`，输出解不开是 `FFMPEG_ERROR`。"""
        try:
            stat = path.stat()  # noqa: ASYNC240 - 本地文件属性，开销可忽略
            key = (str(path), stat.st_size, int(stat.st_mtime))
        except OSError as exc:
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "文件读不到",
                f"{path.name}: {type(exc).__name__}: {exc}",
                ["确认文件还在磁盘上", "或重新生成 / 重新导入这段素材"],
            ) from exc
        cached = self._probe_cache.get(key)
        if cached is not None:
            return cached
        binary = ffmpeg_tool.require("ffprobe")
        args = [
            binary,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise AppError(
                ErrorCode.FFMPEG_ERROR,
                "读不出这个文件的信息",
                f"ffprobe 退出码 {proc.returncode}（{path.name}）。",
                ["确认这个文件能被播放器打开", "文件可能只下载了一半，重新生成一次"],
                {"raw": (stderr or b"").decode("utf-8", "replace")[-2000:]},
            )
        try:
            data = json.loads(stdout.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise AppError(
                ErrorCode.FFMPEG_ERROR,
                "ffprobe 的输出看不懂",
                f"{path.name}: {exc}",
                ["确认使用的是官方 FFmpeg（设置页可以看用的是哪一份）"],
            ) from exc
        probe = _parse(data)
        self._probe_cache[key] = probe
        return probe

    # --- 拆分 ---

    async def extract(self, pid: str, asset_id: str) -> dict[str, Any]:
        """把一段视频的声音拆成独立音频文件并登记成临时资源。

        幂等：同一个源资产拆出来的文件名固定，已经在磁盘上就直接复用（返回 `reused`），
        不重复起进程——同一段画面被反复拆的时候不该攒出一堆一模一样的 m4a。
        """
        db = db_of(pid)
        proj = project_of(pid)
        asset = await fetch(db, Asset, asset_id, "资产")
        src = proj.dir / asset.path
        if not src.is_file():
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "源视频不在磁盘上",
                f"{asset.path} 找不到，无法从它拆出声音。",
                ["从备份恢复该文件，或重新生成这个镜头", "也可以直接导入一段音频放到音频轨上"],
                {"asset_id": asset_id},
            )
        if asset_module.kind_of_suffix(src.suffix) != "video":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这个资产不是视频",
                f"{asset.path} 的后缀是 {src.suffix or '（无）'}，拆声音只能对视频做。",
                ["在视频轨上选一段生成出来的视频", "音频文件请用「导入音频」直接放到音频轨"],
                {"asset_id": asset_id},
            )
        probe = await self.probe_file(src)
        if not probe.has_audio:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这段视频没有声音",
                f"{asset.path} 里没有音频流——大多数 AI 生成的视频都是无声的。",
                [
                    "导入一段音频（配乐 / 配音）放到音频轨上",
                    "或者换一段带声音的素材再拆",
                ],
                {"asset_id": asset_id},
            )

        target_dir = asset_module.ensure_dir(
            proj.dir / asset_module.KIND_DIR["clip_audio"], "音频目录"
        )
        target = target_dir / f"{Path(asset.path).stem}_audio{AUDIO_SUFFIX}"
        on_disk = target.is_file()
        if on_disk:
            known = await asset_service.by_sha1(pid, asset_module.sha1_of_file(target))
            if known is not None:
                return {**known, "reused": True, "duration": probe.duration}
            # 文件在、登记没了（库被手动改过）：不重跑 FFmpeg，直接往下补登记。
        else:
            await self._run(asset_id, src, target)

        registered = await asset_service.register_path(
            pid, "clip_audio", str(target), source="extract", copy=False
        )
        # sha1 去重命中了别的位置：刚拆的这一份是多余副本，删掉它，
        # 不然磁盘上会静静躺着两份一样的音频。
        if registered["path"] != target.relative_to(proj.dir).as_posix():
            target.unlink(missing_ok=True)  # noqa: ASYNC240 - 本地文件操作，开销可忽略
        merged = await asset_service.merge_meta(
            pid,
            registered["id"],
            {
                "from_asset_id": asset_id,
                "stream": "audio",
                "codec": probe.codec,
                "duration": probe.duration,
            },
        )
        log.info("audio.extracted", project_id=pid, asset_id=asset_id)
        return {**merged, "reused": False, "duration": probe.duration}

    async def _run(self, asset_id: str, src: Path, target: Path) -> None:
        binary = ffmpeg_tool.require("ffmpeg")
        args = [
            binary,
            "-y",
            "-i",
            str(src),
            "-vn",
            "-map",
            "0:a:0",
            "-c:a",
            "aac",
            "-b:a",
            AUDIO_BITRATE,
            str(target),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not target.is_file():  # noqa: ASYNC240 - 本地文件检查，开销可忽略
            # 半成品必须删掉，否则下次会被当成「已经拆好了」直接复用。
            target.unlink(missing_ok=True)  # noqa: ASYNC240 - 同上
            raise AppError(
                ErrorCode.FFMPEG_ERROR,
                "拆出声音失败",
                f"FFmpeg 退出码 {proc.returncode}（{src.name}）。",
                [
                    "确认这段视频能被 FFmpeg 读取（在播放器里能听到声音）",
                    "也可以导入一段音频直接放到音频轨上",
                ],
                {"asset_id": asset_id, "raw": (stderr or b"").decode("utf-8", "replace")[-2000:]},
            )


def _parse(data: dict[str, Any]) -> Probe:
    streams = data.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    duration = _seconds((data.get("format") or {}).get("duration"))
    if duration is None:
        for stream in streams:
            duration = _seconds(stream.get("duration"))
            if duration is not None:
                break
    codec = audio[0].get("codec_name") if audio else None
    return Probe(has_audio=bool(audio), duration=duration, codec=codec)


def _seconds(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


audio = AudioService()
