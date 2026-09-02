"""ComfyUI api graph 的纯函数：解析一份 `workflow_api.json`、把参数值写进它的副本。

**为什么在这一层**：`app/generation/providers/*` 只能 import `app.core.*` 与 `app.generation.*`
（provider 层绝不 import service 层，`presets.py` / `image.py` 都是这个规矩）。工作流绑定那条路
的适配器（`providers/comfy_workflow.py`）要用这两个函数，所以它们不能留在
`services/workflows.py` 里——那边只是重新导出这三个名字，老调用点一行不用改。

这里**只做形状与写值**，不认识任何具体模型：绑定表长什么样、哪些槽位是必需的，
仍然是 `services/workflows.py` 与 `persistence/models_gen.py` 的事。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.errors import AppError, ErrorCode

#: 可绑定的输入槽。前四个是素材/文本，后面是采样参数。
SLOTS = (
    "prompt",
    "negative_prompt",
    "reference_image",
    "first_frame",
    "last_frame",
    "source_image",
    "seed",
    "steps",
    "width",
    "height",
    "duration",
)


def parse_graph(raw: str) -> dict[str, Any]:
    """解析 workflow_api.json。必须是 {节点id: {class_type, inputs}} 形状。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(
            ErrorCode.INVALID_WORKFLOW,
            "不是合法的 JSON",
            f"第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}",
            [
                "确认导出的是 ComfyUI 的「API 格式」而不是界面工作流",
                "用文本编辑器确认文件没有被截断",
            ],
        ) from exc
    if not isinstance(data, dict) or not data:
        raise AppError(
            ErrorCode.INVALID_WORKFLOW,
            "不是 ComfyUI 的 API 格式",
            "顶层应是「节点 id → 节点」的对象，实际拿到的是空对象或数组。",
            ["在 ComfyUI 里用「Save (API Format)」重新导出", "确认没有把界面工作流当成 API 格式"],
        )
    # 界面工作流的顶层是 {id, revision, last_node_id, last_link_id, nodes: [...], links: [...]}。
    # 它和 API 格式差得远，但下面那条「缺少 class_type」的报错会把这几个顶层键当成节点名列出来
    # ——用户看到的是一串不认识的字段，而真正的原因是导出格式选错了。所以先认出这一种。
    if isinstance(data.get("nodes"), list):
        raise AppError(
            ErrorCode.INVALID_WORKFLOW,
            "这是界面工作流，不是 API 格式",
            f"顶层键是 {'、'.join(list(data)[:6])}，{len(data['nodes'])} 个节点在 nodes 数组里。"
            "API 格式的顶层直接是「节点 id → {class_type, inputs}」，没有 nodes / links。",
            [
                "在 ComfyUI 里打开这份工作流 → 菜单「工作流 / Workflow」→「导出 (API)」",
                "旧版前端：设置里打开「Enable Dev mode Options」后会出现「Save (API Format)」按钮",
                "user/default/workflows/ 里存的都是界面格式，不能直接上传",
                "节点标题（AIVS_*）不用重设，导出格式换对就行",
            ],
            {"node_count": len(data["nodes"])},
        )
    bad = [k for k, v in data.items() if not isinstance(v, dict) or "class_type" not in v]
    if bad:
        raise AppError(
            ErrorCode.INVALID_WORKFLOW,
            "节点结构不完整",
            f"以下节点缺少 class_type：{'、'.join(bad[:5])}。",
            ["重新用「Save (API Format)」导出", "确认文件没有被手工改坏"],
            {"nodes": bad[:20]},
        )
    return data


def apply_bindings(
    graph: dict[str, Any], bindings: dict[str, str], values: dict[str, Any]
) -> dict[str, Any]:
    """把参数值写进 api graph 的副本。Adapter 的核心，也是唯一知道节点细节的地方。"""
    out = json.loads(json.dumps(graph))
    for slot, target in bindings.items():
        if slot not in values or values[slot] is None:
            continue
        node_id, field = target.split(".", 1)
        node = out.get(node_id)
        if isinstance(node, dict):
            node.setdefault("inputs", {})[field] = values[slot]
    return out
