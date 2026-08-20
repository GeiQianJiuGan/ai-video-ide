"""Workflow 能力层（Step 4）。

这是「业务层不绑定具体视频模型」这条硬约束的落点：Shot 只写 capability，
换模型只换 workflow 行。绑定表把抽象槽位（prompt / reference_image / seed …）
映射到具体节点字段（"6.text"），Adapter 据此把图改写成可提交的 api graph。

校验分两层：
  1. 绑定完整性——纯本地，不需要 ComfyUI；
  2. 自定义节点探测——需要 ComfyUI，离线时明确说「未能探测」而不是假装通过。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.generation.comfy.client import comfy
from app.persistence.models import utc_now
from app.persistence.models_gen import CAPABILITIES, REQUIRED_SLOTS, Workflow
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, load_json

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
#: ComfyUI 官方节点前缀之外的一律视为自定义节点（探测缺失用）。
BUILTIN_HINT = ("CLIPTextEncode", "KSampler", "CheckpointLoaderSimple", "VAEDecode", "SaveImage")


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


def node_summary(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """给前端画节点列表：id、类型、可写字段。"""
    out = []
    for node_id, node in graph.items():
        inputs = node.get("inputs") or {}
        out.append(
            {
                "id": node_id,
                "class_type": node.get("class_type"),
                "title": (node.get("_meta") or {}).get("title"),
                # 只列标量字段：连线（[node, index]）不能被绑定覆盖
                "fields": sorted(k for k, v in inputs.items() if not isinstance(v, list)),
            }
        )
    return sorted(out, key=lambda n: n["id"])


def custom_nodes(graph: dict[str, Any]) -> list[str]:
    kinds = {str(n.get("class_type")) for n in graph.values()}
    return sorted(k for k in kinds if k not in BUILTIN_HINT)


def _check_binding(graph: dict[str, Any], slot: str, target: str) -> str | None:
    """校验一条绑定；返回人类可读的问题描述，None 表示没问题。"""
    if "." not in target:
        return f"{slot}: 绑定「{target}」不是「节点id.字段名」的形式"
    node_id, field = target.split(".", 1)
    node = graph.get(node_id)
    if node is None:
        return f"{slot}: 图里没有节点 {node_id}"
    inputs = node.get("inputs") or {}
    if field not in inputs:
        return f"{slot}: 节点 {node_id}（{node.get('class_type')}）没有字段 {field}"
    if isinstance(inputs[field], list):
        return f"{slot}: {node_id}.{field} 是连线输入，不能由参数覆盖"
    return None


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


class WorkflowService:
    async def list_workflows(self, pid: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        rows = await fetch_all(db, Workflow, order_by=Workflow.created_at)
        return [self._shape(r) for r in rows]

    def _shape(self, row: Workflow) -> dict[str, Any]:
        data = as_dict(row)
        data["bindings"] = load_json(row.bindings_json, {})
        data["nodes"] = load_json(row.nodes_json, [])
        data["required_nodes"] = load_json(row.required_nodes_json, [])
        data["validation"] = load_json(row.validation_json, None)
        data["missing_slots"] = sorted(
            set(REQUIRED_SLOTS.get(row.capability, ())) - set(data["bindings"])
        )
        for key in (
            "api_json",
            "bindings_json",
            "nodes_json",
            "required_nodes_json",
            "validation_json",
        ):
            data.pop(key, None)
        return data

    async def get(self, pid: str, wid: str) -> dict[str, Any]:
        row = await fetch(db_of(pid), Workflow, wid, "工作流")
        data = self._shape(row)
        data["api_json"] = row.api_json
        return data

    async def import_workflow(
        self,
        pid: str,
        *,
        name: str,
        capability: str,
        api_json: str,
        bindings: dict[str, str] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if capability not in CAPABILITIES:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的能力类型",
                f"{capability} 不在支持列表里：{'、'.join(CAPABILITIES)}。",
                ["选择一个已支持的能力", "若确实需要新能力，请先在能力表里登记"],
            )
        graph = parse_graph(api_json)
        db = db_of(pid)
        now = utc_now()
        row = Workflow(
            id=new_id("workflow"),
            name=(name or "").strip() or "未命名工作流",
            capability=capability,
            api_json=api_json,
            bindings_json=dump_json(bindings or {}),
            nodes_json=dump_json(node_summary(graph)),
            required_nodes_json=dump_json(custom_nodes(graph)),
            status="draft",
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        async with db.write() as session:
            session.add(row)
        return self._shape(row)

    async def bind(self, pid: str, wid: str, bindings: dict[str, str]) -> dict[str, Any]:
        """整表替换绑定。未知槽位直接拒绝，避免打错字后静默失效。"""
        unknown = sorted(set(bindings) - set(SLOTS))
        if unknown:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的输入槽",
                f"{'、'.join(unknown)} 不是可绑定的槽位。可用：{'、'.join(SLOTS)}。",
                ["检查槽位名拼写", "只绑定当前能力需要的槽位"],
            )
        db = db_of(pid)
        await fetch(db, Workflow, wid, "工作流")
        async with db.write() as session:
            row = await session.get(Workflow, wid)
            assert row is not None
            row.bindings_json = dump_json(bindings)
            row.status = "draft"  # 改过绑定就要重新校验
            row.validation_json = None
            row.updated_at = utc_now()
        return await self.get(pid, wid)

    async def update(self, pid: str, wid: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Workflow, wid, "工作流")
        async with db.write() as session:
            row = await session.get(Workflow, wid)
            assert row is not None
            for key in ("name", "notes"):
                if key in patch:
                    setattr(row, key, patch[key])
            if patch.get("status") in ("disabled", "draft"):
                row.status = patch["status"]
            row.updated_at = utc_now()
        return await self.get(pid, wid)

    async def delete(self, pid: str, wid: str) -> None:
        db = db_of(pid)
        await fetch(db, Workflow, wid, "工作流")
        async with db.write() as session:
            fresh = await session.get(Workflow, wid)
            if fresh is not None:
                await session.delete(fresh)

    async def set_default(self, pid: str, wid: str) -> dict[str, Any]:
        db = db_of(pid)
        row = await fetch(db, Workflow, wid, "工作流")
        async with db.write() as session:
            for other in await fetch_all(db, Workflow, where=Workflow.capability == row.capability):
                fresh = await session.get(Workflow, other.id)
                if fresh is not None:
                    fresh.is_default = 1 if other.id == wid else 0
        return await self.get(pid, wid)

    async def validate(self, pid: str, wid: str, *, probe: bool = True) -> dict[str, Any]:
        """校验绑定 +（可选）探测自定义节点。结果写回 validation_json 并决定 status。"""
        db = db_of(pid)
        row = await fetch(db, Workflow, wid, "工作流")
        graph = parse_graph(row.api_json)
        bindings: dict[str, str] = load_json(row.bindings_json, {})

        problems: list[str] = []
        missing = sorted(set(REQUIRED_SLOTS.get(row.capability, ())) - set(bindings))
        problems += [f"{slot}: 该能力必须绑定此槽位" for slot in missing]
        for slot, target in bindings.items():
            issue = _check_binding(graph, slot, str(target))
            if issue:
                problems.append(issue)

        required = custom_nodes(graph)
        missing_nodes: list[str] = []
        probe_detail = "已跳过节点探测"
        if probe:
            try:
                installed = await comfy.installed_nodes()
                missing_nodes = [n for n in required if n not in installed]
                probe_detail = f"已探测 {len(installed)} 个节点"
            except AppError as err:
                probe_detail = f"未能探测（{err.title}）"

        ok = not problems and not missing_nodes
        result = {
            "ok": ok,
            "problems": problems,
            "missing_slots": missing,
            "required_nodes": required,
            "missing_nodes": missing_nodes,
            "probe": probe_detail,
            "checked_at": utc_now(),
        }
        async with db.write() as session:
            fresh = await session.get(Workflow, wid)
            assert fresh is not None
            fresh.validation_json = dump_json(result)
            if fresh.status != "disabled":
                fresh.status = "ready" if ok else "invalid"
            fresh.updated_at = utc_now()

        if missing_nodes:
            raise AppError(
                ErrorCode.COMFY_NODE_MISSING,
                f"缺少自定义节点 {missing_nodes[0]}",
                "工作流用到的节点在 ComfyUI 里没装："
                + "、".join(missing_nodes)
                + f"。（{probe_detail}）",
                [
                    f"在 ComfyUI 里安装节点包 {missing_nodes[0]}",
                    "换一套不依赖该节点的 Workflow",
                    "把这条能力标为不可用，避免依赖它的镜头排队后才失败",
                ],
                {"workflow_id": wid, "missing_nodes": missing_nodes},
            )
        if problems:
            raise AppError(
                ErrorCode.INVALID_WORKFLOW,
                "绑定校验未通过",
                "；".join(problems[:8]),
                ["把红色槽位拖到对应节点字段上", "确认导出的 API 图与绑定来自同一份工作流"],
                {"workflow_id": wid, "problems": problems},
            )
        return result

    async def capability_matrix(self, pid: str) -> dict[str, Any]:
        """四行能力矩阵。没有任何镜头时就能回答「以后哪种镜头做不出来」。"""
        rows = await fetch_all(db_of(pid), Workflow)
        matrix = []
        for cap in CAPABILITIES:
            mine = [r for r in rows if r.capability == cap]
            ready = [r for r in mine if r.status == "ready"]
            chosen = next((r for r in ready if r.is_default), ready[0] if ready else None)
            matrix.append(
                {
                    "capability": cap,
                    "ready": bool(ready),
                    "workflow_count": len(mine),
                    "ready_count": len(ready),
                    "default_workflow_id": chosen.id if chosen else None,
                    "default_workflow_name": chosen.name if chosen else None,
                    "required_slots": list(REQUIRED_SLOTS.get(cap, ())),
                    "impact": None if ready else _impact(cap),
                }
            )
        return {"capabilities": matrix, "comfy": await comfy.ping()}

    async def resolve(self, pid: str, capability: str, wid: str | None = None) -> Workflow:
        """按能力挑一条可用的工作流。挑不到就报「能力缺失」而不是随便找一条。"""
        db = db_of(pid)
        if wid:
            row = await fetch(db, Workflow, wid, "工作流")
            if row.status != "ready":
                raise AppError(
                    ErrorCode.INVALID_WORKFLOW,
                    "指定的工作流未就绪",
                    f"「{row.name}」当前状态是 {row.status}。",
                    ["在流程页点「校验绑定」", "或换一条已就绪的工作流"],
                    {"workflow_id": wid},
                )
            return row
        rows = [
            r
            for r in await fetch_all(db, Workflow, where=Workflow.capability == capability)
            if r.status == "ready"
        ]
        if not rows:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                f"能力「{capability}」不可用",
                _impact(capability),
                [
                    "在流程页导入并校验一条对应能力的 workflow_api.json",
                    "或把该镜头改成使用已就绪的能力",
                ],
                {"capability": capability},
            )
        return next((r for r in rows if r.is_default), rows[0])


def _impact(cap: str) -> str:
    return {
        "text2image": "缺少文生图，角色表与首帧无法出图。",
        "image2video": "缺少图生视频，绝大多数镜头无法生成。",
        "first_last_frame": "首尾帧缺失，依赖它的镜头无法生成。",
        "upscale": "缺少放大，成片只能停留在生成分辨率。",
    }.get(cap, f"能力 {cap} 尚未就绪。")


workflows = WorkflowService()
