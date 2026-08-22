"""从视频里抽真帧（Step 2）。

单线程续接的整个前提是「上一段的**真末帧**当下一段的首帧」。以前 context 里的
`prev_frame` 指的是上游那**整段视频**的资产——模型端拿到一段视频是没法当首帧用的，
所以这里补上真正缺的那一步：用 FFmpeg 抽一张 PNG。

抽出来的帧是**临时资源，不是工程资产**：它登记成 `Asset(kind="frame")`（生成层要靠
`asset_id → path` 才能把它喂给模型），但落在 `cache/frames/` 而不是 `assets/`，
资产页与孤儿扫描都不列它（`assets.TRANSIENT_KINDS`），而且**源成片一删它就跟着删**
（`assets.delete` 调下面的 `derived_frames`）。理由很简单：它是派生的、随时能重抽的，
没有源片就没有意义——留在资产里只会让人以为自己多了一堆不知从哪来的图。

三条约定：

  · **幂等**：同一个 (asset, at) 抽出来的文件名固定，已经在磁盘上就直接复用，
    不重复起进程（一次编排里同一个末帧会被问很多次）；
  · **末帧用 `-sseof`**：容器时长与真实最后一帧常有几十毫秒的偏差，
    往回退一点点比 seek 到结尾更可靠；
  · 失败是 `FFMPEG_ERROR`，建议里必须给出**另一条路**——把衔接改成转场，
    那条路不需要末帧。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.core import ffmpeg as ffmpeg_tool
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.persistence.models_world import Asset
from app.services import assets as asset_module
from app.services.assets import assets as asset_service
from app.services.base import db_of, fetch, load_json, project_of

log = get_logger("frames")

#: 抽末帧时往回退的秒数。这里配合 `-update 1`（见 `_run`）：退回去解出来的每一帧都
#: 覆盖写同一个文件，最后留下的就是真正的最后一帧。退太少反而危险——`-sseof` 的定位点
#: 一旦落在最后一帧之后，FFmpeg 会「成功」地什么都不输出。
TAIL_BACKOFF = 0.5


def _label(at: str | float) -> str:
    if at == "end":
        return "end"
    if at == "start":
        return "start"
    return f"t{float(at):.3f}".replace(".", "_")


def start_frame_index(rows: list[Asset]) -> dict[str, Asset]:
    """源视频 asset id → 已经从它抽出来的**首帧**图。

    `extract(pid, asset_id, "start")` 把出处写进 `Asset.meta_json`
    （`{from_asset_id, at}`），所以读路径不用再问 FFmpeg，只按这条线索认出
    「这段视频的首帧抽过了」。**抽帧是写操作，读路径绝不顺手起 FFmpeg**——
    要补抽走各自的显式入口（分镜板的 `POST /storyboard/posters`）。

    这条线索的解读只放在这一处：分镜板卡片（`story._shot_media`）与版本轨
    （`generation.list_versions`）都从这里拿，两边各写一遍迟早对不上。
    """
    out: dict[str, Asset] = {}
    for asset in rows:
        if asset.kind != "frame":
            continue
        meta = load_json(asset.meta_json, {})
        src = meta.get("from_asset_id")
        if meta.get("at") == "start" and isinstance(src, str):
            out[src] = asset
    return out


def derived_frames(rows: list[Asset], source_asset_id: str) -> list[Asset]:
    """从某个资产抽出来的所有临时帧（首帧 / 末帧 / 任意时间点）。

    `services/assets.py::delete` 靠它做级联清理：**成片删了，从它抽的帧也得删**。
    和 `start_frame_index` 一样，`meta_json.from_asset_id` 的解读只放在这个模块里。
    """
    out: list[Asset] = []
    for asset in rows:
        if asset.kind != "frame":
            continue
        meta = load_json(asset.meta_json, {})
        if meta.get("from_asset_id") == source_asset_id:
            out.append(asset)
    return out


class FrameService:
    async def extract(self, pid: str, asset_id: str, at: str | float = "end") -> dict[str, Any]:
        """抽一帧并登记成临时资源。`at` 是 "end" / "start" / 秒数。返回资产字典 + `reused`。"""
        db = db_of(pid)
        proj = project_of(pid)
        asset = await fetch(db, Asset, asset_id, "资产")
        src = proj.dir / asset.path
        if not src.is_file():
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "源视频不在磁盘上",
                f"{asset.path} 找不到，无法从它抽帧。",
                [
                    "从备份恢复该文件，或重新生成上游镜头",
                    "或把这段衔接改成「转场」——转场不需要上游末帧",
                ],
                {"asset_id": asset_id},
            )
        if asset_module.kind_of_suffix(src.suffix) != "video":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这个资产不是视频",
                f"{asset.path} 的后缀是 {src.suffix or '（无）'}，抽帧只能对视频做。",
                ["选一个生成出来的视频版本", "图片资产可以直接当首帧使用"],
                {"asset_id": asset_id},
            )

        target_dir = asset_module.ensure_dir(proj.dir / asset_module.KIND_DIR["frame"], "帧目录")
        target = target_dir / f"{Path(asset.path).stem}_{_label(at)}.png"
        if target.is_file():
            known = await asset_service.by_sha1(pid, asset_module.sha1_of_file(target))
            if known is not None:
                return {**known, "reused": True}
        else:
            await self._run(asset_id, src, target, at)

        registered = await asset_service.register_path(
            pid, "frame", str(target), source="extract", copy=False
        )
        # sha1 去重命中了别的位置（老工程里的帧还在 assets/frames/ 下）：刚抽的这一份是
        # 多余副本，删掉它——不然升级后磁盘上会静静躺着两份一样的 PNG。
        if registered["path"] != target.relative_to(proj.dir).as_posix():
            target.unlink(missing_ok=True)  # noqa: ASYNC240 - 本地文件操作，开销可忽略
        # 出处（从哪段视频、抽的哪个位置）必须留在资产上：context 就是靠它认出
        # 「这张图是上游那段的末帧」，而不是又去抽一次；`assets.delete` 也靠它做级联清理。
        merged = await asset_service.merge_meta(
            pid,
            registered["id"],
            {"from_asset_id": asset_id, "at": at if isinstance(at, str) else float(at)},
        )
        log.info("frames.extracted", project_id=pid, asset_id=asset_id, at=str(at))
        return {**merged, "reused": False}

    async def _run(self, asset_id: str, src: Path, target: Path, at: str | float) -> None:
        binary = ffmpeg_tool.require("ffmpeg")
        seek: list[str] = []
        # 末帧：退一点点进去，把这段里的每一帧都覆盖写到同一个文件上，剩下的就是最后一帧。
        # 其余位置是精确定位，取一帧就够。
        pick = ["-frames:v", "1"]
        if at == "end":
            seek = ["-sseof", f"-{TAIL_BACKOFF}"]
            pick = ["-update", "1"]
        elif at != "start":
            seek = ["-ss", f"{float(at):.3f}"]
        args = [binary, "-y", *seek, "-i", str(src), *pick, "-q:v", "2", str(target)]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not target.is_file():  # noqa: ASYNC240 - 本地文件检查，开销可忽略
            target.unlink(missing_ok=True)  # noqa: ASYNC240 - 同上；半成品必须删掉，否则下次会被当成已抽好的帧
            raise AppError(
                ErrorCode.FFMPEG_ERROR,
                "抽帧失败",
                f"FFmpeg 退出码 {proc.returncode}（{src.name} 的 {at} 位置）。",
                [
                    "确认这段视频能被 FFmpeg 读取（在播放器里能拖到结尾）",
                    "或把这段衔接改成「转场」——转场不需要上游末帧",
                    "也可以手动上传一张图当下一幕的首帧",
                ],
                {"asset_id": asset_id, "raw": (stderr or b"").decode("utf-8", "replace")[-2000:]},
            )


frames = FrameService()
