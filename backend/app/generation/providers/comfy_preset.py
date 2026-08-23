"""ComfyUI 预设适配器（默认，核心路径）。

与旧的 Workflow 绑定路径的区别只有一处，但很关键：**我们不维护图**。
预设里已经按标题标好了入口（见 `presets.py`），这里做四件事：

  1. 把首/末帧与参考图上传到 ComfyUI 的 input 目录（图在我们这边，ComfyUI 只认它自己的文件名）；
  2. 按标题把首帧 / 末帧 / 参考图 / prompt / 负向 / 时长 / 种子填进去；
  3. 提交，拿 prompt_id 当 task_id；
  4. 轮询 history，取最后一个产物。

图里的 lora、加速节点、采样器我们不看也不校验——那是模型端的事。

参考图（`AIVS_REF_1…`）与首尾帧分开处理，且**槽位不够不是失败**：图里只标了 3 个槽位、
账单给了 5 张，就填前 3 张并把这件事写进 `req.notes` 冻结进版本——降级要说出来，
但不该让整个任务跑不了。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.generation.comfy.client import ComfyClient, comfy, outputs_of
from app.generation.providers import base, presets
from app.generation.providers.base import TaskState, VideoRequest

log = get_logger("provider.comfy_preset")


class ComfyPresetProvider:
    name = "comfy_preset"

    def __init__(self, client: ComfyClient | None = None) -> None:
        self._client = client or comfy
        #: prompt_id → 提交时选的预设名，只为报错时能说清「哪一份图没出片」。
        self._used: dict[str, str] = {}

    # --- 探测 ---

    def ref_capacity(self) -> base.RefCapacity:
        """能收几张参考图 = 默认预设里标了几个 `AIVS_REF_*`。

        看的是**设置里的默认预设**：问这句话的时机（算账单、给警告）都在入队之前，
        那时还没有任务、也就没有 `extra["preset"]`。某个任务临时换了预设时，真正喂了
        几张仍然由 `_refs` 如实写进 `params.ref_notes`——账单说的是「按当前设置会怎样」。
        """
        name = str(settings.video_preset or "")
        count = presets.slot_count(name)
        if count is None:
            return base.RefCapacity(
                None,
                name,
                (
                    f"读不到预设 {name}（文件不在或填不进去），这里先不限张数；"
                    "真正生成时会先报出这份图的问题。"
                    if name
                    else "还没有选默认预设，这里先不限张数；真正生成时会报「还没有选生成预设」。"
                ),
            )
        if count == 0:
            return base.RefCapacity(
                0,
                name,
                f"预设 {name} 里一个 AIVS_REF_* 都没标——角色图 / 场景图全都喂不进去，"
                "人物形象只能靠首帧带。",
            )
        return base.RefCapacity(
            count,
            name,
            f"预设 {name} 标了 {count} 个 AIVS_REF_* 槽位，一次最多喂 {count} 张参考图。",
        )

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
        """把账单里的参考图按序号填进 `AIVS_REF_*`，顺便把「谁是谁」告诉模型。

        四条取舍：
          · **槽位不够只降级，不失败**——图是模型端维护的，我们没资格因为它只标了 3 个槽位
            就拒绝生成；少喂的那几张写进 `req.notes`，跟着版本一起冻结，事后查得到。
          · **图里没有首帧入口时，首帧插到参考图 1**：分工是「首尾帧那类模型补转场，
            R2V 出正片」，而多参考图的 R2V 图常常没有 `AIVS_FIRST_FRAME`。这时把首帧
            当第一张参考图送进去，并写一条 note——绝不静默丢掉那一张。
          · **一个槽位都没有时也照样跑**，但要留一条 note：那种图只能靠首帧带形象，
            这正是「人物形象丢失」的现场，用户得看得见原因。
          · **顺序即语义**：账单已按优先级排好，1 号槽放优先级最高的那张；
            要不要在 prompt 末尾附一句「参考图1=林小雨」由设置里的 `video.ref_labels` 决定
            （ComfyUI 这类图收不到标签，只能靠这句话对上号）。
        """
        slots = presets.ref_slots(points)
        refs = list(req.refs)
        first_as_ref = req.first_frame is not None and "AIVS_FIRST_FRAME" not in points
        if first_as_ref and req.first_frame is not None:
            refs.insert(0, base.RefImage(req.first_frame, "首帧", "first_frame"))
        if not refs:
            return {}
        if not slots:
            if first_as_ref:
                req.notes.append(
                    f"预设 {name} 既没有 AIVS_FIRST_FRAME 也没有 AIVS_REF_* 槽位，"
                    f"首帧与账单里 {len(req.refs)} 张参考图一张都没喂进去——这一版只有提示词起作用。"
                )
            else:
                req.notes.append(
                    f"预设 {name} 里没有 AIVS_REF_* 槽位，账单里 {len(req.refs)} 张参考图"
                    "（角色表 / 地点参考图）没有喂进去——人物形象只能靠首帧带。"
                )
            log.info("provider.refs_unsupported", preset=name, refs=len(refs))
            return {}
        if first_as_ref:
            req.notes.append(
                f"预设 {name} 没有 AIVS_FIRST_FRAME 槽位，首帧已当作参考图 1 送进去"
                "（这份图做不了严格首尾帧，补转场请另选一份）。"
            )
        sent = refs[: len(slots)]
        if len(refs) > len(slots):
            dropped = "、".join(r.label or r.path.name for r in refs[len(slots) :])
            req.notes.append(
                f"预设 {name} 只有 {len(slots)} 个参考图槽位，账单里这几张没喂进去：{dropped}。"
            )
            log.info("provider.refs_truncated", preset=name, slots=len(slots), refs=len(refs))
        values: dict[str, Any] = {}
        for marker, ref in zip(slots, sent, strict=False):
            values[marker] = await self._upload(ref.path)
        hint = base.ref_hint(sent) if settings.video_ref_labels else ""
        if hint:
            values["AIVS_PROMPT"] = f"{req.prompt}\n{hint}".strip()
            req.notes.append(f"已把参考图对应关系写进 prompt：{hint}")
        return values

    async def _upload(self, path: Path) -> str:
        if not path.is_file():  # noqa: ASYNC240 - 本地文件检查，开销可忽略
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "参考图不在磁盘上",
                f"{path} 找不到。",
                ["确认该资产文件还在工程目录里", "或重新挑一张参考图"],
                {"path": path.as_posix()},
            )
        # 参考图是本地小文件，读它不值得再包一层线程
        return await self._client.upload_image(path.name, path.read_bytes())  # noqa: ASYNC240

    async def poll(self, task_id: str) -> TaskState:
        history = await self._client.history(task_id)
        if not history:
            return TaskState("running", 0.0, "ComfyUI 正在跑")
        status = ((history.get("status") or {}) if isinstance(history, dict) else {}) or {}
        if str(status.get("status_str") or "") == "error":
            return TaskState("failed", 1.0, _error_detail(status), raw=history)
        if not outputs_of(history):
            if str(status.get("status_str") or "").lower() not in {
                "success",
                "completed",
                "complete",
            }:
                return TaskState("running", 0.0, "ComfyUI 正在跑", raw=history)
            return TaskState(
                "failed",
                1.0,
                "跑完了但没有任何产物——图的末端可能没有保存节点。",
                raw=history,
            )
        return TaskState("done", 1.0, "已出片", raw=history)

    async def fetch(self, task_id: str) -> tuple[str, bytes]:
        history = await self._client.history(task_id)
        files = outputs_of(history)
        if not files:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "ComfyUI 没有产出任何文件",
                f"prompt_id={task_id}，预设={self._used.get(task_id, '?')}。",
                [
                    "确认图的末端有 SaveImage / VHS 之类的保存节点",
                    "在 ComfyUI 界面里手动跑一次同一份图确认能出片",
                ],
                {"raw": str(history)[:2000]},
            )
        chosen = files[-1]
        data = await self._client.download(chosen["filename"], chosen["subfolder"], chosen["type"])
        return chosen["filename"], data


def _error_detail(status: dict[str, Any]) -> str:
    """把 ComfyUI 的 messages 里那条 execution_error 摘出来，别让人去翻原始 JSON。"""
    for entry in status.get("messages") or []:
        if isinstance(entry, list) and len(entry) == 2 and entry[0] == "execution_error":
            info = entry[1] if isinstance(entry[1], dict) else {}
            return (
                f"{info.get('node_type') or '节点'} 执行失败："
                f"{info.get('exception_message') or '（ComfyUI 没有给原因）'}"
            )
    return "ComfyUI 报告任务失败，但没有给出原因。"
