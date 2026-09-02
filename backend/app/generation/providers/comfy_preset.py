"""ComfyUI 预设适配器（默认，核心路径）。

与旧的 Workflow 绑定路径的区别只有一处，但很关键：**我们不维护图**。
预设里已经按标题标好了入口（见 `presets.py`），这里做四件事：

  1. 把首/末帧与参考素材上传到 ComfyUI 的 input 目录（文件在我们这边，ComfyUI 只认它自己的文件名）；
  2. 按标题把首帧 / 末帧 / 参考素材 / prompt / 负向 / 时长 / 种子填进去；
  3. 提交，拿 prompt_id 当 task_id；
  4. 轮询 history，取最后一个产物。

图里的 lora、加速节点、采样器我们不看也不校验——那是模型端的事。

参考素材（`AIVS_REF_*` / `AIVS_REF_VIDEO_*` / `AIVS_REF_AUDIO_*`）与首尾帧分开处理，
**按媒体各填各的槽位**（图片、视频、音频接的节点根本不是一类），且**槽位不够不是失败**：
图里只标了 3 个图片槽、账单给了 5 张，就填前 3 张并把这件事写进 `req.notes` 冻结进版本——
降级要说出来，但不该让整个任务跑不了。
"""

from __future__ import annotations

import copy
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.generation.providers import base, presets
from app.generation.providers.base import VideoRequest
from app.generation.providers.comfy_base import ComfyTasks

log = get_logger("provider.comfy_preset")


class ComfyPresetProvider(ComfyTasks):
    """按 `AIVS_*` 标题填参数。上传 / 轮询 / 取回那半条链在 `ComfyTasks` 里。"""

    name = "comfy_preset"

    # --- 探测 ---

    def ref_capacity(self) -> base.RefCapacity:
        """能收几个参考素材 = 默认预设里标了几个 `AIVS_REF_*` / `_VIDEO_` / `_AUDIO_`。

        看的是**设置里的默认预设**，也就是「按当前应用级设置会怎样」这一问的答案。
        **工程 + 能力**那一问（这个工程的首尾帧镜头到底数哪一份预设）由
        `services/route.py::capacity()` 回答：它先把路由与预设解析出来，再拿解析后的那个
        名字问 `presets.capacity_of()`——也就是下面这同一段话，两处不各写一遍。
        """
        return presets.capacity_of(str(settings.video_preset or ""))

    async def probe(self) -> dict[str, Any]:
        ping = await self._client.ping()
        if not ping["online"]:
            raise AppError(
                ErrorCode.COMFY_OFFLINE,
                "ComfyUI 未连接",
                ping["detail"],
                [
                    "启动 ComfyUI 后重试",
                    f"确认地址正确（当前 {self._client.base_url}）",
                    "只做手动整理与时间线编辑时可以忽略",
                ],
            )
        rows = presets.listing()
        chosen = settings.video_preset
        current = next((r for r in rows if r["name"] == chosen), None)
        if chosen and current is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "选中的预设不存在",
                f"设置里的默认预设是 {chosen}，但预设目录里没有它。",
                ["在设置页重新上传这份预设", "或改选一个已有的预设"],
                {"available": [r["name"] for r in rows]},
            )
        return {
            "ok": True,
            "target": self._client.base_url,
            "preset": chosen or None,
            "preset_ready": bool(current and current.get("ready")),
            "preset_count": len(rows),
            "detail": (
                f"ComfyUI 已连接 · 预设 {chosen} 就绪"
                if current and current.get("ready")
                else f"ComfyUI 已连接 · 共 {len(rows)} 份预设"
                + ("" if chosen else "，还没有选默认预设——生成时必须指定一份")
            ),
        }

    # --- 生成 ---

    async def submit(self, req: VideoRequest, *, client_id: str) -> str:
        name = str(req.extra.get("preset") or settings.video_preset or "")
        if not name:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                "还没有选生成预设",
                "comfy_preset 方式需要一份模型端的图（API 格式）作为预设。",
                [
                    "在设置页上传一份预设并设为默认",
                    "或把调用方式改成 http_api",
                ],
            )
        graph = copy.deepcopy(presets.load(name))
        points = presets.entry_points(graph)
        values: dict[str, Any] = {
            "AIVS_PROMPT": req.prompt,
            "AIVS_NEGATIVE": req.negative,
            "AIVS_DURATION": req.duration,
            "AIVS_SEED": req.seed,
        }
        # 首帧**没有槽位就降级成参考图 1**（`_refs` 里插队）：出正片的 R2V 图往往只有
        # AIVS_REF_*，为此拒绝生成等于把这类模型整个挡在外面。
        if req.first_frame is not None and "AIVS_FIRST_FRAME" in points:
            values["AIVS_FIRST_FRAME"] = await self._upload(req.first_frame)
        # 末帧相反，要的是**严格首尾帧**，图里没这个入口就只能换一份预设——这条不降级：
        # 悄悄丢掉末帧的话，补出来的转场接不上下一镜，而界面上会显示「已生成」。
        if req.last_frame is not None:
            if "AIVS_LAST_FRAME" not in points:
                raise AppError(
                    ErrorCode.INVALID_WORKFLOW,
                    "这份预设不支持首尾帧",
                    f"预设 {name} 里没有标题为 AIVS_LAST_FRAME 的节点，无法接收末帧。",
                    [
                        "换一份支持首尾帧的预设（转场与单线程续接都要用它）",
                        *presets.HOW_TO,
                    ],
                    {"preset": name, "found": sorted(points)},
                )
            values["AIVS_LAST_FRAME"] = await self._upload(req.last_frame)
        # 二次处理的源视频同样**不降级**：图里没有 AIVS_SOURCE_VIDEO 就说明它不是一份
        # 处理图。悄悄跳过的话，超分任务会变成「凭提示词重出一段」，而版本轨上写着
        # 「从 v1 超分而来」——血缘就是假的了。
        if req.source_video is not None:
            if "AIVS_SOURCE_VIDEO" in points:
                values["AIVS_SOURCE_VIDEO"] = await self._upload(req.source_video)
            elif req.mode == "refine":
                raise AppError(
                    ErrorCode.INVALID_WORKFLOW,
                    "这份预设不能做二次处理",
                    f"预设 {name} 里没有标题为 AIVS_SOURCE_VIDEO 的节点，"
                    "接不了「要处理的那一段视频」。",
                    [
                        "在 ComfyUI 里把接视频的那个节点（VHS_LoadVideoPath / LoadVideo）"
                        "标题改成 AIVS_SOURCE_VIDEO，重新上传预设",
                        "注意它和 AIVS_REF_VIDEO_n 不是一回事：源视频是「就处理这一段」",
                        "或在设置页把二次处理预设换成一份标了它的图",
                    ],
                    {"preset": name, "found": sorted(points)},
                )
        values.update(await self._refs(req, name, points))
        for marker, spot in points.items():
            value = values.get(marker)
            if value is None or value == "":
                continue  # 没给的项保持图里原来的值，不要用空串把它冲掉
            graph[spot["node_id"]]["inputs"][spot["field"]] = value
        prompt_id = await self._client.submit(graph, client_id=client_id)
        self._used[prompt_id] = name
        log.info(
            "provider.submitted",
            preset=name,
            prompt_id=prompt_id,
            mode=req.mode,
            refs=len(req.refs),
        )
        return prompt_id

    async def _refs(
        self, req: VideoRequest, name: str, points: dict[str, dict[str, str]]
    ) -> dict[str, Any]:
        """把账单里的参考素材按媒体、按序号填进 `AIVS_REF_*`，顺便把「谁是谁」告诉模型。

        五条取舍：
          · **按媒体分开填**：图片进 `AIVS_REF_n`、视频进 `AIVS_REF_VIDEO_n`、音频进
            `AIVS_REF_AUDIO_n`。混着数会把一段 `.mp4` 填进 LoadImage，ComfyUI 那边既不
            报错也出不了片——一个媒体的槽位不够只影响它自己那一组，别的照喂。
          · **槽位不够只降级，不失败**——图是模型端维护的，我们没资格因为它只标了 3 个槽位
            就拒绝生成；少喂的那几个写进 `req.notes`，跟着版本一起冻结，事后查得到。
          · **图里没有首帧入口时，首帧插到参考图 1**：分工是「首尾帧那类模型补转场，
            R2V 出正片」，而多参考图的 R2V 图常常没有 `AIVS_FIRST_FRAME`。这时把首帧
            当第一张参考图送进去，并写一条 note——绝不静默丢掉那一张。
          · **一个槽位都没有时也照样跑**，但要留一条 note：那种图只能靠首帧带形象，
            这正是「人物形象丢失」的现场，用户得看得见原因。
          · **顺序即语义**：账单已按优先级排好，每种媒体的 1 号槽放它那组里优先级最高的那个；
            要不要在 prompt 末尾附一句「参考图1=林小雨」由设置里的 `video.ref_labels` 决定
            （ComfyUI 这类图收不到标签，只能靠这句话对上号）。
        """
        by_slots = presets.ref_slots_by_media(points)
        refs = list(req.refs)
        first_as_ref = req.first_frame is not None and "AIVS_FIRST_FRAME" not in points
        if first_as_ref and req.first_frame is not None:
            refs.insert(0, base.RefAsset(req.first_frame, "首帧", "first_frame", "image"))
        if not refs:
            return {}
        values: dict[str, Any] = {}
        sent_all: list[base.RefAsset] = []
        for media, group in base.refs_by_media(refs).items():
            if not group:
                continue
            slots = by_slots.get(media) or []
            label = presets.MEDIA_LABEL.get(media, "参考素材")
            family = presets.MARKER_FAMILY.get(media, "AIVS_REF_*")
            if not slots:
                names = "、".join(r.label or r.path.name for r in group)
                if media == "image" and first_as_ref:
                    req.notes.append(
                        f"预设 {name} 既没有 AIVS_FIRST_FRAME 也没有 AIVS_REF_* 槽位，"
                        f"首帧与账单里 {len(group) - 1} 张参考图一张都没喂进去"
                        "——这一版只有提示词起作用。"
                    )
                else:
                    req.notes.append(
                        f"预设 {name} 里没有 {family} 槽位，账单里 {len(group)} 个{label}"
                        f"没有喂进去：{names}。"
                        + ("人物形象只能靠首帧带，这一版容易跑偏。" if media == "image" else "")
                    )
                log.info("provider.refs_unsupported", preset=name, media=media, refs=len(group))
                continue
            if media == "image" and first_as_ref:
                req.notes.append(
                    f"预设 {name} 没有 AIVS_FIRST_FRAME 槽位，首帧已当作参考图 1 送进去"
                    "（这份图做不了严格首尾帧，补转场请另选一份）。"
                )
            sent = group[: len(slots)]
            if len(group) > len(slots):
                dropped = "、".join(r.label or r.path.name for r in group[len(slots) :])
                req.notes.append(
                    f"预设 {name} 只有 {len(slots)} 个{label}槽位，"
                    f"账单里这几个没喂进去：{dropped}。"
                )
                log.info(
                    "provider.refs_truncated",
                    preset=name,
                    media=media,
                    slots=len(slots),
                    refs=len(group),
                )
            for marker, ref in zip(slots, sent, strict=False):
                values[marker] = await self._upload(ref.path)
            sent_all += sent
        hint = base.ref_hint(sent_all) if settings.video_ref_labels else ""
        if hint:
            values["AIVS_PROMPT"] = f"{req.prompt}\n{hint}".strip()
            req.notes.append(f"已把参考素材对应关系写进 prompt：{hint}")
        return values
