"""ComfyUI 预设适配器（默认，核心路径）。

与旧的 Workflow 绑定路径的区别只有一处，但很关键：**我们不维护图**。
预设里已经按标题标好了入口（见 `presets.py`），这里做四件事：

  1. 把首/末帧上传到 ComfyUI 的 input 目录（图在我们这边，ComfyUI 只认它自己的文件名）；
  2. 按标题把首帧 / 末帧 / prompt / 负向 / 时长 / 种子填进去；
  3. 提交，拿 prompt_id 当 task_id；
  4. 轮询 history，取最后一个产物。

图里的 lora、加速节点、采样器我们不看也不校验——那是模型端的事。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.generation.comfy.client import ComfyClient, comfy, outputs_of
from app.generation.providers import presets
from app.generation.providers.base import TaskState, VideoRequest

log = get_logger("provider.comfy_preset")


class ComfyPresetProvider:
    name = "comfy_preset"

    def __init__(self, client: ComfyClient | None = None) -> None:
        self._client = client or comfy
        #: prompt_id → 提交时选的预设名，只为报错时能说清「哪一份图没出片」。
        self._used: dict[str, str] = {}

    # --- 探测 ---

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
        for marker, path in (
            ("AIVS_FIRST_FRAME", req.first_frame),
            ("AIVS_LAST_FRAME", req.last_frame),
        ):
            if path is None:
                continue
            if marker not in points:
                if marker == "AIVS_LAST_FRAME":
                    raise AppError(
                        ErrorCode.INVALID_WORKFLOW,
                        "这份预设不支持首尾帧",
                        f"预设 {name} 里没有标题为 {marker} 的节点，无法接收末帧。",
                        [
                            "换一份支持首尾帧的预设（转场与单线程续接都要用它）",
                            *presets.HOW_TO,
                        ],
                        {"preset": name, "found": sorted(points)},
                    )
                continue
            values[marker] = await self._upload(path)
        for marker, spot in points.items():
            value = values.get(marker)
            if value is None or value == "":
                continue  # 没给的项保持图里原来的值，不要用空串把它冲掉
            graph[spot["node_id"]]["inputs"][spot["field"]] = value
        prompt_id = await self._client.submit(graph, client_id=client_id)
        self._used[prompt_id] = name
        log.info("provider.submitted", preset=name, prompt_id=prompt_id, mode=req.mode)
        return prompt_id

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
