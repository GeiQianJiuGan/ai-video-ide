"""长视频导入与切段（生成层的另一半）：**导入一段成片，自动切段，再逐段处理。**

这条路上**没有剧本、没有 LLM、也不出新画面**。它要解决的是另一个问题：手上已经有一段
长片（别处渲的、别人给的、上一版导出的），想把它切成能一段段处理的镜头。

三个设计取舍，每一个都是为了不和已有的东西打架：

  · **零文件复制**。切段不切文件：一幕下面 N 个镜头各挂一版 `GenerationVersion`，
    `asset_id` 全部指向**同一个源文件**，各自带 `in_point` / `out_point`
    （迁移 `0016_version_lineage_range`）。装配时抄进 `TimelineClip`，导出时 ffmpeg 用
    `-ss/-to` 各取一段——真去切 60 段文件的话，磁盘翻倍、切点还会被关键帧对齐悄悄挪动。
  · **它就是一幕普通的幕**，只是 `kind="ingested"`（迁移 `0015_scene_kind_params`）。
    于是顺序、拖拽、时间线装配、导出、二次处理全部复用——用户可以把导入的幕和剧本拆出来的幕
    随意穿插（默认新幕排在最后，要放到剧本前面就用已有的 `reorder_scenes`）。
    完整性要求查表（`params.SCENE_REQUIRED["ingested"] == ()`）：**什么都不必填**，
    不然会得到「请给这一幕写 prompt」这种荒谬的门槛。
  · **共用参数是默认**（`param_mode="shared"`）：用户原话是「我不想每个 SHOT 都去调整」。
    幕上填一份 prompt / 素材，镜头上留空 = 继承（`services/params.py`），改一处三十段跟着变；
    要某一段单独调，就在那个镜头上填一项——**不需要切换模式**。

切点怎么找（全靠 FFmpeg，三级降级，每一级都说得出自己是哪一级）：
  1. `select='gt(scene,阈值)'` + `showinfo` —— 认画面切换，成片最常见的切法；
  2. `silencedetect` —— 画面连续但对白分段的素材（访谈、口播）靠它；
  3. 固定窗口 —— 上面两级都认不出来时按 `ingest_chunk_seconds` 铺满。
  比 `ingest_min_segment` 短的相邻切点合并：切出一堆半秒的碎片没人处理得了。

**先账单再动手**：`plan()` 只读地列出「切成几段、每段多长、哪几段太短被合并了、用哪一级
方法认出来的」，`run()` 才落库。切点还能在账单上手改（`run(cuts=[...])`）——自动切点是
建议，不是判决。
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.core import ffmpeg as ffmpeg_tool
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.events.bus import Channel, bus
from app.persistence.models import utc_now
from app.persistence.models_frames import FrameCard
from app.persistence.models_gen import GenerationVersion
from app.persistence.models_story import Scene, Shot
from app.persistence.models_world import Asset, AssetRef
from app.services.assets import assets, kind_of_suffix
from app.services.audio import audio as audio_service
from app.services.base import as_dict, db_of, dump_json, fetch, project_of
from app.services.story import story

log = get_logger("ingest")

#: `showinfo` 那行里的时间戳。`pts_time:12.345`
_PTS = re.compile(r"pts_time:([0-9]+\.?[0-9]*)")
#: `silencedetect` 报的静音结束点——下一段从这里开始。
_SILENCE_END = re.compile(r"silence_end:\s*([0-9]+\.?[0-9]*)")

#: 三级切点方法 → 人话。账单里必须说清用的是哪一级，否则「为什么切成这样」无从解释。
METHOD_LABEL = {
    "scene": "按画面切换（scene detect）",
    "silence": "按对白停顿（silence detect）",
    "fixed": "按固定长度铺满（认不出切点时的兜底）",
    "manual": "手动给的切点",
}


class IngestService:
    def __init__(self) -> None:
        self._scan_cache: OrderedDict[tuple, list[float]] = OrderedDict()

    # --- 第一步：把源文件登记进来 ---

    async def register(self, pid: str, src: str, *, copy: bool = True) -> dict[str, Any]:
        """登记一段成片。**默认复制进工程**，`copy=False` 是原地引用。

        为什么默认复制：工程目录整个拷走仍然有效是这个项目的落盘规矩（`Asset.path`
        相对工程目录存）。原地引用省磁盘，但**代价必须说出来**——源文件一移动、一改名、
        一拔外置硬盘，这几十段全部变成「文件不在磁盘上」。所以 `copy=False` 时资产的
        `path` 是绝对路径，账单与界面都要标出「引用自工程外」。
        """
        if src.startswith("ast_"):
            db = db_of(pid)
            asset_row = await fetch(db, Asset, src, "资产")
            from app.services.base import as_dict

            asset = as_dict(asset_row)
        else:
            asset = await assets.register_path(pid, "upload", src, source="ingest", copy=copy)
        if kind_of_suffix(Path(asset["path"]).suffix) != "video":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这不是一个视频文件",
                f"{asset['path']} 的后缀看起来不是视频（支持 mp4 / mov / mkv / webm / avi）。",
                ["选一段视频文件", "音频请用「导入音频」，图片请用素材导入"],
                {"asset_id": asset["id"]},
            )
        probe = await audio_service.peek(project_of(pid).dir / asset["path"])
        return {
            **asset,
            "duration": probe.duration if probe else None,
            "has_audio": probe.has_audio if probe else None,
            #: 引用原地时这句话要一路带到界面上：可移植性是真的丢了。
            "external": not copy,
            "warnings": (
                []
                if copy
                else [
                    "这段视频没有复制进工程，只是被引用。"
                    "源文件一移动 / 改名 / 拔盘，从它切出来的所有镜头都会变成「文件不在磁盘上」。"
                ]
            )
            + (
                []
                if probe and probe.duration
                else ["ffprobe 没能探出这段视频的长度，切段只能靠切点本身，最后一段的结尾未知。"]
            ),
        }

    # --- 第二步：账单 ---

    async def plan(
        self,
        pid: str,
        asset_id: str,
        *,
        method: str = "auto",
        threshold: float | None = None,
        min_segment: float | None = None,
        max_segment: float | None = None,
        chunk_seconds: float | None = None,
        cuts: list[float] | None = None,
        range_in: float | None = None,
        range_out: float | None = None,
        fast_scan: bool = True,
    ) -> dict[str, Any]:
        """切成几段、每段哪儿到哪儿、用哪一级方法认出来的。**一行都不落库。**

        `cuts` 给了就是手动切点（自动那三级一概不跑）：自动切点是建议不是判决，
        用户在账单上拖过切点之后要能原样落下去。

        `range_in` / `range_out` 是**片头片尾**：几乎每段成片前面都有一截台标 / 倒计时，
        后面还有一截字幕，它们不该变成两个镜头再让用户一个个删掉。所以切段只在这个区间
        里进行——**区间外的时间不属于任何镜头**，但源文件一帧都不动（区间只是一对数字，
        与「零文件复制」那条一致，改主意重新出一次账单就行）。
        """
        db = db_of(pid)
        asset = await fetch(db, Asset, asset_id, "资产")
        path = project_of(pid).dir / asset.path
        if not await asyncio.to_thread(path.is_file):
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "源视频不在磁盘上",
                f"{asset.path} 找不到（引用原地的文件被移走了？）。",
                ["确认源文件还在原处", "或重新导入一次（这次让它复制进工程）"],
                {"asset_id": asset_id},
            )
        probe = await audio_service.peek(path)
        total = float(probe.duration) if probe and probe.duration else 0.0
        floor = float(min_segment if min_segment is not None else settings.ingest_min_segment)
        ceil_dur = float(max_segment) if (max_segment is not None and max_segment > 0) else None
        window = float(
            chunk_seconds if chunk_seconds is not None else settings.ingest_chunk_seconds
        )
        low, high, range_notes = self._range(total, range_in, range_out, asset_id)
        if cuts is not None:
            points, used = sorted({max(0.0, float(c)) for c in cuts}), "manual"
        else:
            points, used = await self._detect(
                path, method, threshold, window, low, high, fast_scan=fast_scan
            )
        #: 片头片尾里的切点直接扔掉：那一截不属于任何镜头，留着只会切出一个空段。
        points = [p for p in points if low < p < (high or p + 1)]
        merged, dropped = self._merge(points, floor, high, low)
        segments = self._segments(merged, high, window, max_segment=ceil_dur, start=low)
        effective_cuts = [seg["in_point"] for seg in segments[1:]] if len(segments) > 1 else []
        size_mb = round((asset.size_bytes or 0) / (1024 * 1024), 1)
        return {
            "asset_id": asset_id,
            "path": asset.path,
            "duration": total or None,
            "size_mb": size_mb,
            "method": used,
            "fast_scan": fast_scan,
            "method_label": METHOD_LABEL.get(used, used),
            "threshold": float(
                threshold if threshold is not None else settings.ingest_scene_threshold
            ),
            "min_segment": floor,
            "max_segment": ceil_dur,
            "chunk_seconds": window,
            #: 切段只发生在这个区间里。`range_out` 未知（探不出长度）时是 None。
            "range_in": low,
            "range_out": high or None,
            #: 被片头 / 片尾挡在外面的秒数——「少了一截」必须是账单上看得见的一句话。
            "trimmed_head": low,
            "trimmed_tail": round(total - high, 3) if total and high else 0.0,
            "cuts": effective_cuts or merged,
            #: 太短被合并掉的切点。**不是错误**，但要说出来——否则「我明明看到那里有个切换」
            #: 会变成一桩查不到的怪事。
            "merged_away": dropped,
            "segments": segments,
            "total": len(segments),
            "warnings": self._warnings(total, segments, size_mb, asset) + range_notes,
        }

    def _range(
        self, total: float, range_in: float | None, range_out: float | None, asset_id: str
    ) -> tuple[float, float, list[str]]:
        """片头片尾 → 一对可用的边界 + 要说出来的话。

        探不出长度时 `high` 是 0（= 未知，段的结尾照旧靠切点与窗口估），此时给的
        `range_out` 仍然照用——用户在预览里拖出来的位置比我们的猜测可信。
        """
        notes: list[str] = []
        low = max(0.0, float(range_in or 0.0))
        high = float(range_out) if range_out is not None and range_out > 0 else (total or 0.0)
        if total and high > total + 0.05:
            notes.append(f"片尾位置标到 {high:.2f}s，但这段视频只有 {total:.2f}s，按文件末尾处理。")
            high = total
        #: 这一条先判：片头本身就超出了文件，说「什么都不剩」不算错但没指出真正的毛病。
        if total and low > total:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "片头切掉的比整段视频还长",
                f"片头标在 {low:.2f}s，视频只有 {total:.2f}s。",
                ["把片头边界拖回视频范围内"],
                {"asset_id": asset_id, "duration": total},
            )
        if high and high - low < 0.5:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "去掉片头片尾之后什么都不剩",
                f"保留区间是 {low:.2f}s ~ {high:.2f}s，不足 0.5 秒，切不出任何镜头。",
                [
                    "把片头 / 片尾的边界拖回来一些",
                    "确认要处理的是这一段视频",
                ],
                {"asset_id": asset_id, "range_in": low, "range_out": high},
            )
        if low > 0 or (total and high and high < total - 0.05):
            tail = round(total - high, 3) if total and high else 0.0
            notes.append(
                f"片头 {low:.2f} 秒、片尾 {tail:.2f} 秒不进任何镜头（源文件没有被裁，"
                "改主意重新出一次账单就行）。"
            )
        return low, high, notes

    async def run(
        self,
        pid: str,
        asset_id: str,
        *,
        title: str | None = None,
        prompt: str | None = None,
        method: str = "auto",
        threshold: float | None = None,
        min_segment: float | None = None,
        max_segment: float | None = None,
        chunk_seconds: float | None = None,
        cuts: list[float] | None = None,
        range_in: float | None = None,
        range_out: float | None = None,
        param_mode: str = "shared",
        position: int | None = None,
        production_mode: str = "direct",
        frames_per_shot: int = 2,
        image_prompt: str = "",
        video_prompt: str = "",
        fast_scan: bool = True,
    ) -> dict[str, Any]:
        """按账单落库：一幕 `kind="ingested"` + 每段一个镜头 + 每段一版（带区间）。

        产出的每一版 `source="imported"`、`asset_id` 全部指向同一个源文件，**零文件复制**。
        新幕默认排在最后；`position` 给了就插到那一位并把后面的幕顺序后移（会在返回值里
        说清「已有 N 幕顺序后移」——顺序是用户的东西，动了它必须说）。
        """
        from app.services.generation import generation  # 延迟导入：generation 反向依赖 story

        bill = await self.plan(
            pid,
            asset_id,
            method=method,
            threshold=threshold,
            min_segment=min_segment,
            max_segment=max_segment,
            chunk_seconds=chunk_seconds,
            cuts=cuts,
            range_in=range_in,
            range_out=range_out,
            fast_scan=fast_scan,
        )
        if not bill["segments"]:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "没有切出任何一段",
                "账单里一段都没有（源视频长度未知且没有认出任何切点）。",
                [
                    "手动给几个切点（在账单上拖动）",
                    "或确认这段视频能被 FFmpeg 读取",
                ],
                {"asset_id": asset_id},
            )
        # 先抽取全部参考画面；抽帧失败时不留下半套幕和镜头。
        extracted: list[list[dict]] = []
        if production_mode == "frames":
            from app.services.frames import frames as extractor

            if not 1 <= frames_per_shot <= 8:
                raise ValueError("frames_per_shot must be between 1 and 8")
            all_positions = []
            for seg in bill["segments"]:
                low, high = seg["in_point"], seg["out_point"]
                # 留在每段区间内，避开结尾切点，防止抽到下一段。
                span = max(0.0, high - low - min(0.1, (high - low) / 2))
                for i in range(frames_per_shot):
                    at = round(low + span * i / max(1, frames_per_shot - 1), 3)
                    all_positions.append(at)
            all_frames = await extractor.extract_many(pid, asset_id, all_positions)
            for offset in range(0, len(all_frames), frames_per_shot):
                extracted.append(
                    [
                        {"asset_id": frame["id"], "at": at}
                        for frame, at in zip(
                            all_frames[offset : offset + frames_per_shot],
                            all_positions[offset : offset + frames_per_shot],
                            strict=True,
                        )
                    ]
                )
        async with db_of(pid).atomic():
            scene = await story.create_scene(
                pid,
                {
                    "title": (title or "").strip() or f"导入：{Path(bill['path']).name}",
                    "kind": "ingested",
                    #: **共用参数是默认**：镜头上留空 = 继承幕级那一份，改一处三十段跟着变。
                    "param_mode": param_mode,
                    "prompt": prompt or "",
                    "source_asset_id": asset_id,
                    "notes": (
                        f"从 {Path(bill['path']).name} 自动切段（{bill['method_label']}），"
                        f"共 {bill['total']} 段。"
                        #: 片头片尾是「为什么第一段不是从 0 秒开始」的唯一解释，记在幕上，
                        #: 不然过两天回来看只会觉得少了一截。
                        + (
                            f"已去掉片头 {bill['trimmed_head']:.2f} 秒"
                            f" / 片尾 {bill['trimmed_tail']:.2f} 秒。"
                            if bill["trimmed_head"] or bill["trimmed_tail"]
                            else ""
                        )
                    ),
                },
            )
            if production_mode == "frames":
                from app.services.frame_authoring import frames

                await frames.save_shared_prompts(
                    pid,
                    scene["id"],
                    {
                        "image_prompt": image_prompt,
                        "video_prompt": video_prompt,
                    },
                )
            # 同一源片的镜头、版本、引用和画面一次写入，避免每段重查整个镜头表。
            from app.services.params import prefill

            rows, versions, references, cards = [], [], [], []
            async with db_of(pid).write() as session:
                highest = await session.scalar(select(func.max(Shot.index_no))) or 0
                scene_row = await session.get(Scene, scene["id"])
                defaults = prefill(scene_row)
                for index, seg in enumerate(bill["segments"], start=1):
                    shot_id, version_id = new_id("shot"), new_id("generation_version")
                    shot = Shot(
                        id=shot_id,
                        scene_id=scene["id"],
                        index_no=highest + index,
                        title=f"第 {index} 段",
                        **{k: v for k, v in defaults.items() if k != "duration"},
                        duration=seg["duration"],
                        status="generated",
                        current_version_id=version_id,
                        created_at=utc_now(),
                        updated_at=utc_now(),
                    )
                    rows.append(shot)
                    versions.append(
                        GenerationVersion(
                            id=version_id,
                            shot_id=shot_id,
                            version_no=1,
                            asset_id=asset_id,
                            kind="video",
                            source="imported",
                            status="done",
                            duration=seg["duration"],
                            in_point=seg["in_point"],
                            out_point=seg["out_point"],
                            params_json=dump_json(
                                {
                                    "imported_from_asset_id": asset_id,
                                    "segment_index": index,
                                    "cut_method": bill["method"],
                                }
                            ),
                        )
                    )
                    references.append(
                        AssetRef(
                            id=new_id("asset_ref"),
                            asset_id=asset_id,
                            owner_kind="shot",
                            owner_id=shot_id,
                            role="version",
                        )
                    )
                    if production_mode == "frames":
                        for frame_index, frame in enumerate(extracted[index - 1]):
                            role = (
                                "first"
                                if frame_index == 0
                                else "last"
                                if frame_index == frames_per_shot - 1
                                else "key"
                            )
                            cards.append(
                                FrameCard(
                                    id=new_id("frame"),
                                    shot_id=shot_id,
                                    position=frame_index,
                                    title=f"源片画面 {frame_index + 1}",
                                    role=role,
                                    refs_json=dump_json([frame["asset_id"]]),
                                    story=f"源视频 {frame['at']:.3f} 秒的画面（本段 {seg['in_point']:.3f}–{seg['out_point']:.3f} 秒）",
                                )
                            )
                session.add_all(rows)
                await session.flush()
                session.add_all([*versions, *references, *cards])
            shots = [
                {**as_dict(shot), "version_id": version.id, **seg}
                for shot, version, seg in zip(rows, versions, bill["segments"], strict=True)
            ]
            moved = 0
            if position is not None:
                moved = await self._insert_at(pid, scene["id"], position)
            result = {
                "plan": bill,
                "scene": await story.get_scene(pid, scene["id"]),
                "shots": shots,
                "created": len(shots),
                #: 插到中间时有多少幕被推后。顺序是用户的东西，动了必须说出来。
                "scenes_shifted": moved,
            }
        for version in versions:
            bus.emit(
                Channel.VERSION,
                "version.created",
                {"id": version.id, "shot_id": version.shot_id, "version_no": 1},
                project_id=pid,
            )
        generation.ensure_pump(pid)
        return result

    # --- 切点 ---

    async def _detect(
        self,
        path: Path,
        method: str,
        threshold: float | None,
        window: float,
        low: float,
        high: float,
        *,
        fast_scan: bool = True,
    ) -> tuple[list[float], str]:
        """三级降级找切点。**每一级都要能说出自己是哪一级**（账单里的 `method`）。

        三种方法都只处理保留区间；检测返回源文件绝对时间。
        """
        want = method if method in ("scene", "silence", "fixed") else "auto"
        level = float(threshold if threshold is not None else settings.ingest_scene_threshold)
        if want in ("auto", "scene"):
            scale = (
                "scale=w='min(640,iw)':h='min(360,ih)':force_original_aspect_ratio=decrease,"
                if fast_scan
                else ""
            )
            points = await self._scan(
                path, f"{scale}select='gt(scene,{level})',showinfo", _PTS, low=low, high=high
            )
            if points or want == "scene":
                return points, "scene"
        if want in ("auto", "silence"):
            points = await self._scan(
                path, "silencedetect=n=-30dB:d=0.6", _SILENCE_END, audio=True, low=low, high=high
            )
            if points or want == "silence":
                return points, "silence"
        if high <= low:
            return [], "fixed"
        step = max(0.5, window)
        span = high - low
        return [round(low + step * i, 3) for i in range(1, int(span // step) + 1)], "fixed"

    async def _scan(
        self,
        path: Path,
        filter_spec: str,
        pattern: re.Pattern[str],
        *,
        audio: bool = False,
        low: float = 0,
        high: float = 0,
    ) -> list[float]:
        """跑一遍 FFmpeg 只为读 stderr 上的时间戳（`-f null` 不写任何文件）。

        **认不出切点不是失败**：回空列表让上一级往下降。真正的失败只有「FFmpeg 不在」
        （`require` 抛 `FFMPEG_MISSING`）和「这文件读不了」——后者由退出码 + 空结果一起
        表现，此时同样往下降到固定窗口，因为固定窗口对一段读不了的文件也没意义，
        最终会在 `plan` 的 warnings 里说出来。
        """
        binary = ffmpeg_tool.require("ffmpeg")
        stat = await asyncio.to_thread(path.stat)
        key = (str(path), stat.st_size, stat.st_mtime_ns, filter_spec, audio, low, high)
        if key in self._scan_cache:
            self._scan_cache.move_to_end(key)
            return list(self._scan_cache[key])
        args = [
            binary,
            "-hide_banner",
            "-ss",
            str(low),
            "-i",
            str(path),
            *(["-t", str(high - low)] if high > low else []),
            "-vn" if audio else "-an",
            "-af" if audio else "-vf",
            filter_spec,
            "-f",
            "null",
            "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr = await proc.communicate()
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise
        text = (stderr or b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            log.info("ingest.detect_failed", path=path.name, filter=filter_spec)
            return []
        points = sorted({round(low + float(m), 3) for m in pattern.findall(text)})
        self._scan_cache[key] = points
        while len(self._scan_cache) > 16:
            self._scan_cache.popitem(last=False)
        return list(points)

    def _merge(
        self, points: list[float], floor: float, total: float, start: float = 0.0
    ) -> tuple[list[float], list[float]]:
        """把挨得太近的切点合并掉。返回 (留下的, 合并掉的)。

        合并的是**后一个**切点（前一个先出现，段的开头更可信），所以「切出一堆半秒碎片」
        变成「这里的两个切换被当成一个」——后者用户看一眼账单就明白。
        `start` 是保留区间的起点：第一段的长度要从那里量，不是从第 0 秒。
        """
        kept: list[float] = []
        dropped: list[float] = []
        last = start
        for point in points:
            if total and point >= total - floor:
                dropped.append(point)  # 结尾那一点点不值得单独成段
                continue
            if point - last < floor:
                dropped.append(point)
                continue
            kept.append(point)
            last = point
        return kept, dropped

    def _segments(
        self,
        cuts: list[float],
        total: float,
        window: float,
        max_segment: float | None = None,
        start: float = 0.0,
    ) -> list[dict[str, Any]]:
        """切点 → 段。如果有 max_segment 限制，超过 max_segment 的超长段自动细分。

        `start` 是保留区间的起点（去掉片头之后第一段从哪里开始），`total` 是它的终点。
        """
        edges = [start, *cuts]
        raw_intervals: list[tuple[float, float]] = []
        for i, start in enumerate(edges):
            end = edges[i + 1] if i + 1 < len(edges) else (total or start + max(0.5, window))
            if end > start:
                raw_intervals.append((start, end))

        out: list[dict[str, Any]] = []
        max_dur = float(max_segment) if (max_segment is not None and max_segment > 0) else None

        for start, end in raw_intervals:
            length = round(end - start, 3)
            if length <= 0:
                continue
            if max_dur and length > max_dur:
                sub_start = start
                while sub_start < end:
                    sub_end = min(end, round(sub_start + max_dur, 3))
                    sub_len = round(sub_end - sub_start, 3)
                    if sub_len > 0:
                        out.append(
                            {
                                "index_no": len(out) + 1,
                                "in_point": round(sub_start, 3),
                                "out_point": round(sub_end, 3),
                                "duration": sub_len,
                            }
                        )
                    sub_start = sub_end
            else:
                out.append(
                    {
                        "index_no": len(out) + 1,
                        "in_point": round(start, 3),
                        "out_point": round(end, 3),
                        "duration": length,
                    }
                )
        return out

    def _warnings(
        self, total: float, segments: list[dict[str, Any]], size_mb: float, asset: Asset
    ) -> list[str]:
        """拿不准的事情一律写出来（与导出预检同一个作风），绝不当作已知。"""
        out: list[str] = []
        if not total:
            out.append(
                "ffprobe 没能探出这段视频的长度，最后一段的结尾是估的——落库后请检查最后一个镜头。"
            )
        if size_mb and size_mb > settings.ingest_copy_warn_mb:
            out.append(
                f"源文件有 {size_mb:.0f} MB，复制进工程会比较慢；"
                "空间紧张时可以在导入时选「引用原地」，代价是工程不能整个拷走。"
            )
        if Path(asset.path).is_absolute():
            out.append("这段视频在工程目录之外（引用原地），源文件一移动这些镜头就会失效。")
        if len(segments) > 60:
            out.append(
                f"切出了 {len(segments)} 段——批量处理会排很久的队。"
                "可以把「一段最短多少秒」调大一些再重新出账单。"
            )
        return out

    async def _insert_at(self, pid: str, scene_id: str, position: int) -> int:
        """把这一幕插到第 `position` 位（1 起）。返回被推后的幕数。

        复用已有的 `story.reorder_scenes`——顺序只有一套写法，不在这里另写一份。
        """
        rows = await story.list_scenes(pid)
        order = [s["id"] for s in rows if s["id"] != scene_id]
        index = max(0, min(len(order), position - 1))
        order.insert(index, scene_id)
        await story.reorder_scenes(pid, order)
        return len(order) - index - 1


ingest = IngestService()
