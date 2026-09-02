"""两个 ComfyUI 适配器（预设 / 工作流绑定）共用的那一半：上传、轮询、取回产物。

分出来的理由很实在：这些动作与「图是谁维护的」无关——都是「把本地文件塞进 ComfyUI 的
input 目录」「读 history 判断跑完没有」「把最后一个产物下载回来」。各写一份的话，
「跑完了但没有任何产物」这句四要素错误就会有两份，而它恰好是最需要口径一致的一句
（用户看到它时要去检查图的末端有没有保存节点）。

真正分岔的只有 `submit()`：预设那条按 `AIVS_*` 标题填，绑定那条按绑定表填。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.errors import AppError, ErrorCode
from app.generation.comfy.client import ComfyClient, comfy, outputs_of
from app.generation.providers.base import TaskState


class ComfyTasks:
    """ComfyUI 那半条链。子类必须自己实现 `submit()` 与 `ref_capacity()`。"""

    #: 子类覆盖，用于日志与报错文案。
    name = "comfy"

    def __init__(self, client: ComfyClient | None = None) -> None:
        self._client = client or comfy
        #: task_id → 提交时用的那份图（预设名 / 工作流名），只为报错时能说清「哪一份图没出片」。
        self._used: dict[str, str] = {}

    async def _upload(self, path: Path) -> str:
        if not path.is_file():  # noqa: ASYNC240 - 本地文件检查，开销可忽略
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "参考素材不在磁盘上",
                f"{path} 找不到。",
                ["确认该资产文件还在工程目录里", "或重新挑一个参考素材"],
                {"path": path.as_posix()},
            )
        # 参考素材是本地文件，读它不值得再包一层线程（大段视频也就是一次同步读）
        return await self._client.upload_input(path.name, path.read_bytes(), subfolder="")  # noqa: ASYNC240

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
                f"prompt_id={task_id}，用的是 {self._used.get(task_id, '?')}。",
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


def detached_submit_error(exc: AppError, source: str, removed: list[dict[str, str]]) -> AppError:
    """提交被 ComfyUI 拒绝、而这一次又摘过节点时，多给一条指向那件事的建议。**两条路共用。**

    摘掉一个这次用不上的媒体入口（`comfy/graph.py::detach`）有一种会咬人的情形：图里那一格是
    **必填**的（例如 `ImageBatch.image1`）。这时 ComfyUI 回的是「Required input is missing」，
    而那个输入正是我们刚切掉的——不点出来的话，用户只会看着一份自己明明存好的图发愣。

    **只有真摘过、且真是「ComfyUI 拒绝了这份图」时才加**：离线或超时那类失败与摘节点无关，
    多这两句只会把真正的原因埋掉（硬约束 4 要的是说清，不是多说）。
    """
    if not removed or exc.code != ErrorCode.WORKFLOW_ERROR:
        return exc
    which = "、".join(f"{r['title'] or r['class_type']}#{r['node_id']}" for r in removed[:6])
    return AppError(
        exc.code,
        exc.title,
        exc.detail,
        [
            *exc.suggestions,
            f"这一次从{source}里摘掉了 {len(removed)} 个这一版用不上的节点（{which}）："
            "它们是登记为入口、但这个镜头没有值的媒体格子，以及只为它们服务的中间节点",
            "如果上面的报错说某个输入缺失，说明图里那一格是必填的——把这个入口从图里删掉，"
            "或者改成不依赖它的接法（例如末帧那一支单独存一份图）",
        ],
        exc.related_ids,
    )
