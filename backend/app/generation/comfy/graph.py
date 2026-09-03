"""ComfyUI api graph 的纯函数：解析一份 `workflow_api.json`、把参数值写进它的副本、
把这次用不上的入口从副本里摘掉。

**为什么在这一层**：`app/generation/providers/*` 只能 import `app.core.*` 与 `app.generation.*`
（provider 层绝不 import service 层，`presets.py` / `image.py` 都是这个规矩）。工作流绑定那条路
的适配器（`providers/comfy_workflow.py`）要用 `parse_graph` / `apply_bindings`，`detach()` 还要
再给预设那条路（`providers/comfy_preset.py`）用一份，所以它们不能留在 `services/workflows.py`
里——那边只是重新导出前三个名字，老调用点一行不用改。

这里**只做形状、写值与摘节点**，不认识任何具体模型：绑定表长什么样、哪些槽位是必需的，
仍然是 `services/workflows.py` 与 `persistence/models_gen.py` 的事。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
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

#: `SLOTS` 里**接文件**的那几个。与标量槽位的区别不是数据类型，而是**这次没给值时该怎么办**
#: （见 `detach()`）：标量保持图里原来的值，媒体要把那个节点摘掉。
#: 预设那条路上同一件事的表是 `providers/presets.py::MEDIA_MARKERS`。
MEDIA_SLOTS = frozenset({"reference_image", "first_frame", "last_frame", "source_image"})


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


def _linked(value: Any) -> str | None:
    """这个输入是不是「连到另一个节点的输出」？是就回那个节点 id。

    api 格式里一条连线就写成 `[节点id, 输出序号]`。这是「摘节点」**唯一**需要认识的图结构
    ——class_type、lora、加速节点、采样器一个都不用认（与硬约束 1 同一条精神：
    本工具不维护模型端的图）。
    """
    if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
        return str(value[0])
    return None


def detach(
    graph: dict[str, Any],
    node_ids: Iterable[str],
    *,
    keep: Iterable[str] = (),
) -> list[dict[str, str]]:
    """把这些节点从提交的那份图里摘掉，连带只为它们服务的中间节点。**就地改**，回摘掉了谁。

    **为什么必须摘而不是「留着不填」**：标了 `AIVS_*` 标题却这一次没有值的媒体入口，图里那一格
    存的是用户在 ComfyUI 里存图时挂着的示例文件。不填就等于把那张不相干的图/那段不相干的音频
    真送进模型——用户看到的是「画面莫名其妙往那张示例图上收敛」，而队列里一条错误都没有。
    于是「多标几个入口」在事实上变成了一种风险，用户不敢在图里多摆节点，这个便利就废了。
    摘掉之后语义才对得上：**标了标题 = 「这一格由本工具填」，本工具这次没填 = 「这一格这次不用」。**

    标量入口（seed / steps / 宽高 / 时长）**不走这里**：那一格留着的是图里原本的采样参数，
    是用户有意存进去的默认值，保持原值正是想要的行为。两类的分界表是 `MEDIA_SLOTS`
    与 `providers/presets.py::MEDIA_MARKERS`。

    摘的办法只用局部信息，不问 ComfyUI 的 `/object_info`，也不认识任何 class_type：

      1. 把图里所有指向它的连线（`inputs` 里那个 `[node_id, k]`）连键一起删掉；
      2. 删掉这个节点本身——ComfyUI 是从输出节点往回走的，剩下的孤立节点既不会被校验
         也不会被执行，但留着会让「提交了什么」这份存档看不出这次到底跑了哪张图；
      3. **往下游传一层**：切完之后一个连线型输入都不剩的下游节点，说明它是专为这条链服务的
         中间件（LoadImage → ImageScale → 主节点 里的 ImageScale），跟着摘；仍然连着别的东西的
         是汇合点（WanImageToVideo 丢了 `end_image` 还连着 positive / negative / vae /
         start_image），到此为止——**只摘只为它服务的，不动共用的**。

    `keep` 是这一次真填了值的那些入口节点：它们永远不会被连带摘掉（第 3 步在它们那儿停），
    最坏情况下也只是少一个输入键，而不是把这次真正要跑的那条链摘断。

    回的每一项是 `{node_id, class_type, title}`——调用方把它写进 `req.notes`，
    一路冻进版本参数 `ref_notes` 并显示在界面上：**摘掉一个节点是降级，绝不静默**（硬约束 4）。
    """
    protected = {str(n) for n in keep}
    pending = [str(n) for n in node_ids if str(n) not in protected]
    removed: list[dict[str, str]] = []
    while pending:
        node_id = pending.pop(0)
        node = graph.pop(node_id, None)
        if not isinstance(node, dict):
            continue
        meta = node.get("_meta")
        removed.append(
            {
                "node_id": node_id,
                "class_type": str(node.get("class_type") or ""),
                "title": str((meta or {}).get("title") or "") if isinstance(meta, dict) else "",
            }
        )
        for other_id, other in graph.items():
            inputs = other.get("inputs")
            if not isinstance(inputs, dict):
                continue
            cut = [field for field, value in inputs.items() if _linked(value) == node_id]
            if not cut:
                continue
            for field in cut:
                inputs.pop(field, None)
            if other_id in protected:
                continue
            if not any(_linked(value) is not None for value in inputs.values()):
                pending.append(other_id)
    return removed
