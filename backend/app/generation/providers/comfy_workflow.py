"""ComfyUI 工作流绑定适配器：按**用户自己那份图 + 绑定表**填参数。

与 `comfy_preset` 的区别只有一处：入口不是节点标题（`AIVS_*`），而是绑定表里那一行
`槽位 → "节点id.字段"`（`services/workflows.py` 那套自动检测 / 手工绑定的产物）。
上传 / 轮询 / 取回那半条链两边完全一样，所以都在 `ComfyTasks` 里。

**这条路以前不是适配器**：它长在 `GenerationService._run_legacy` 里，靠
`job.workflow_id` 非空来触发；而 `job.workflow_id` 从来没被写过值，于是整支是死代码——
界面上选了「ComfyUI 工作流绑定」等于什么都没选。提成一等适配器之后，三条路在
`registry` 里一视同仁，业务层照旧只调 `provider()`（硬约束 1）。

两件**刻意保留**的降级（原样搬自 `_run_legacy`，一个字都没放宽）：

  · **只喂图片**。绑定表里只有 `reference_image_slots`，没有任何一行能接一段参考视频 /
    音频。所以非图片素材在这里跳过并写进 `req.notes` → 冻结成版本参数 `ref_notes`；
    静默丢掉的话，事后没人查得出「我挂的那段对白音频到底送没送出去」。
  · **槽位不够只截断，不失败**。图是用户自己维护的，我们没资格因为它只绑了 3 个槽位
    就拒绝生成——少喂的那几张同样写进 `req.notes`。

反过来，**这一版没有素材可填的媒体槽位不是「保持原样」而是要连节点一起摘掉**
（`_detach_idle`，理由整段写在 `comfy/graph.py::detach()`）：绑了末帧却没有末帧、绑了 9 个
参考图槽位而这个镜头只有 2 张时，那些格子里留着的是用户存图时挂着的示例文件——留着就等于
把不相干的图真喂进模型，而队列里一条错误都没有。摘了什么照旧写进 `req.notes`。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.generation.comfy.graph import MEDIA_SLOTS, apply_bindings, detach, parse_graph
from app.generation.providers import base
from app.generation.providers.base import VideoRequest
from app.generation.providers.comfy_base import ComfyTasks, detached_submit_error

log = get_logger("provider.comfy_workflow")

#: 绑定表里那个「参考图槽位」的键名。它是一个数组（`["12.image", "13.image"]`），
#: 与其余「一个槽位一行字符串」的键形状不同，所以到处都要把它单独摘出来。
REF_SLOTS_KEY = "reference_image_slots"

#: `MEDIA_SLOTS` 里那几个槽位给人看的说法。note 里得说人话，用途同预设那条路的
#: `presets.MARKER_LABEL`——两份表分别对着两套入口，但话都只写一遍。
SLOT_LABEL: dict[str, str] = {
    "first_frame": "首帧",
    "last_frame": "末帧",
    "source_image": "输入图",
    "reference_image": "参考图（单槽）",
}


def _detach_idle(
    payload: dict[str, Any],
    name: str,
    bindings: dict[str, str],
    values: dict[str, Any],
    unused_refs: list[str],
    req: VideoRequest,
) -> list[dict[str, str]]:
    """把这一版没有素材可填的**媒体**槽位连节点一起从提交的副本里摘掉。

    与 `comfy_preset._detach_idle` 是同一件事、同一个理由（整段写在
    `comfy/graph.py::detach()`），只是这条路的入口来自绑定表而不是节点标题：绑了
    `last_frame` 却没有末帧、绑了 9 个参考图槽位而这个镜头只有 2 张——那些格子里留着的是
    用户在 ComfyUI 里存图时挂着的示例文件，不摘就等于把不相干的图真喂进模型，
    而队列里一条错误都没有。

    标量槽位（seed / steps / 宽高 / 时长）没给值时照旧保持图里原来的值，那是用户有意
    存进去的默认参数。这条分界只有一张表：`comfy/graph.py::MEDIA_SLOTS`。
    """
    keep: set[str] = set()
    idle: dict[str, str] = {}
    for slot, target in bindings.items():
        node_id = target.split(".", 1)[0]
        if slot in MEDIA_SLOTS and values.get(slot) is None:
            idle[node_id] = SLOT_LABEL.get(slot, slot)
        else:
            #: 填了值的、以及所有标量槽位那几个节点：绝不能被连带摘掉。
            #: 一个节点同时被两行绑定指着时（单槽参考图与 `__ref_0` 常常是同一个节点），
            #: 只要有一行填上了值，这个节点就得留。
            keep.add(node_id)
    for target in unused_refs:
        idle.setdefault(target.split(".", 1)[0], "参考图槽位")
    idle = {node_id: label for node_id, label in idle.items() if node_id not in keep}
    if not idle:
        return []
    removed = detach(payload, list(idle), keep=keep)
    counts: dict[str, int] = {}
    for label in idle.values():
        counts[label] = counts.get(label, 0) + 1
    which = "、".join(f"{n} 个{label}" if n > 1 else label for label, n in counts.items())
    cascade = len(removed) - len(idle)
    req.notes.append(
        f"工作流 {name} 这一版没有用到这几个槽位：{which}。它们已经从提交的那份图里摘掉"
        + (f"（连带 {cascade} 个只为它们服务的中间节点）" if cascade > 0 else "")
        + "——图里挂在这些节点上的示例文件一个都没有送进 ComfyUI。"
    )
    log.info(
        "provider.entries_detached",
        workflow=name,
        idle=len(idle),
        removed=len(removed),
    )
    return removed


class ComfyWorkflowProvider(ComfyTasks):
    """按绑定表填参数。哪个节点的哪个字段收首帧由绑定表说，其余一律原样提交。"""

    name = "comfy_workflow"

    # --- 探测 ---

    def ref_capacity(self) -> base.RefCapacity:
        """**图片能喂几张取决于这个能力绑的那份图**，所以这一层答不上来（回「不限制」）。

        真正的数由 `services/route.py::capacity()` 数出来：它按工程 + 能力解析出绑的是
        哪一份图，再数 `bindings["reference_image_slots"]` 有几行。这里是应用级那一问
        （还没有工程上下文），照现有约定**绝不抛错、也不凭空造上限**。

        视频 / 音频**确定是 0**，不是「不知道」：绑定表里根本没有能接它们的槽位。
        这个 0 会让账单如实说出「你挂的那段对白音频这条路喂不进去」——回 `None` 的话
        用户会以为送出去了。
        """
        return base.RefCapacity(
            None,
            "工作流绑定",
            "参考图能喂几张取决于这个能力绑的那份图（数它的 AIVS_REF_* 绑定行）；"
            "这条路只喂图片，参考视频 / 参考音频喂不进去——要喂它们请改用「ComfyUI 预设」"
            "或「通用 REST API」。",
            video=0,
            audio=0,
        )

    async def probe(self) -> dict[str, Any]:
        """「测试连接」= ComfyUI 在不在。**绑没绑图不在这里判**。

        一份图绑给哪个能力是**按工程**存的（`project.bindings_json`），而适配器不认识工程
        （provider 层不 import service 层）。那半句由 `route.resolve()` 回答并显示在概览页，
        两处各判一遍的话，「设置页说就绪、一按生成说没绑图」这种分叉是必然的。
        """
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
        return {
            "ok": True,
            "target": self._client.base_url,
            "detail": (
                f"ComfyUI 已连接（{self._client.base_url}）· "
                "这条路按工程里的工作流绑定提交，每个能力各绑一份图"
            ),
        }

    # --- 生成 ---

    async def submit(self, req: VideoRequest, *, client_id: str) -> str:
        spec = req.workflow
        if spec is None:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                "这个能力还没有绑定工作流",
                "「ComfyUI 工作流绑定」这条路要求先给这个能力指定一份已校验的图，本次任务上没有。",
                [
                    "在 Workflow 管理页给这个能力选一份图并校验通过",
                    "或把调用方式改成「ComfyUI 预设」（模型端那份图由模型端维护，不用绑）",
                ],
            )
        graph = parse_graph(spec.api_json)
        bindings = {
            key: str(value)
            for key, value in spec.bindings.items()
            if key != REF_SLOTS_KEY and isinstance(value, str) and "." in value
        }
        # 只喂图片：跳过的每一个都要说出来（绝不静默失败）。
        images = [r for r in req.refs if r.media == "image"]
        for ref in req.refs:
            if ref.media != "image":
                req.notes.append(
                    f"工作流绑定这条路只能喂图片，{ref.media_label}"
                    f"「{ref.label or ref.path.name}」没有送出去"
                    "（要喂它请把调用方式改成「ComfyUI 预设」或「通用 REST API」）。"
                )
        first_name = await self._upload(req.first_frame) if req.first_frame else None
        last_name = await self._upload(req.last_frame) if req.last_frame else None
        ref_names = [await self._upload(ref.path) for ref in images]
        values: dict[str, Any] = {
            "prompt": req.prompt,
            "negative_prompt": req.negative,
            "seed": req.seed,
            "steps": (req.extra or {}).get("steps"),
            "duration": req.duration,
            "first_frame": first_name,
            "last_frame": last_name,
            # 这两个槽位是老绑定表里的单张入口：图里只有一个「输入图」时它接首帧，
            # 没有首帧（多参考图的 R2V 图）时退回第一张参考图——顺序即优先级。
            "source_image": first_name or (ref_names[0] if ref_names else None),
            "reference_image": ref_names[0] if ref_names else first_name,
        }
        slots = spec.bindings.get(REF_SLOTS_KEY)
        #: 绑了槽位、但这一版没有第 N 张图可填的那几行。它们与「绑了末帧却没有末帧」是同一件事
        #: （见 `_detach_idle`）：不摘掉就会把图里那张示例图当成参考图喂进模型。
        unused_refs: list[str] = []
        if isinstance(slots, list):
            for index, target in enumerate(slots):
                if not isinstance(target, str) or "." not in target:
                    continue
                if index >= len(ref_names):
                    unused_refs.append(target)
                    continue
                values[f"__ref_{index}"] = ref_names[index]
                bindings[f"__ref_{index}"] = target
            if len(ref_names) > len(slots):
                dropped = "、".join(r.label or r.path.name for r in images[len(slots) :])
                req.notes.append(
                    f"工作流 {spec.name} 只绑了 {len(slots)} 个 AIVS_REF_* 槽位，"
                    f"账单里这几张没喂进去：{dropped}。"
                )
                log.info(
                    "provider.refs_truncated",
                    workflow=spec.name,
                    slots=len(slots),
                    refs=len(ref_names),
                )
        elif ref_names:
            req.notes.append(
                f"工作流 {spec.name} 没有绑定 AIVS_REF_* 参考图槽位，"
                f"账单里 {len(ref_names)} 张参考图只有第一张按「参考图」那个单槽送了出去"
                "——人物形象容易跑偏。"
            )
        payload = apply_bindings(graph, bindings, values)
        removed = _detach_idle(payload, spec.name, bindings, values, unused_refs, req)
        try:
            prompt_id = await self._client.submit(payload, client_id=client_id)
        except AppError as exc:
            raise detached_submit_error(exc, f"工作流 {spec.name}", removed) from exc
        self._used[prompt_id] = spec.name
        log.info(
            "provider.submitted",
            workflow=spec.name,
            workflow_id=spec.id,
            prompt_id=prompt_id,
            mode=req.mode,
            refs=len(ref_names),
            detached=len(removed),
        )
        return prompt_id
