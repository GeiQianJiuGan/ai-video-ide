"""Workflow 能力层（Step 4）。

这是「业务层不绑定具体视频模型」这条硬约束的落点：Shot 只写 capability，
换模型只换 workflow 行。绑定表把抽象槽位（prompt / reference_image / seed …）
映射到具体节点字段（"6.text"），Adapter 据此把图改写成可提交的 api graph。

校验分两层：
  1. 绑定完整性——纯本地，不需要 ComfyUI；
  2. 自定义节点探测——需要 ComfyUI，离线时明确说「未能探测」而不是假装通过。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.generation.comfy.client import comfy

# 这三个是**纯函数**，落在 provider 层能 import 到的地方（`app.generation.*`），
# 因为工作流绑定那条路的适配器要用它们；这里重新导出，老调用点与测试一行不用改。
from app.generation.comfy.graph import SLOTS, apply_bindings, parse_graph
from app.persistence.models import Project, utc_now
from app.persistence.models_gen import CAPABILITIES, REQUIRED_SLOTS, Workflow
from app.persistence.models_global import GlobalWorkflow
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, load_json
from app.services.global_registry import global_registry

__all__ = ["SLOTS", "apply_bindings", "parse_graph", "workflows"]

#: ComfyUI 官方节点前缀之外的一律视为自定义节点（探测缺失用）。
BUILTIN_HINT = ("CLIPTextEncode", "KSampler", "CheckpointLoaderSimple", "VAEDecode", "SaveImage")


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


def auto_bindings(
    graph: dict[str, Any], capability: str, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Auto-bind fields from the documented AIVS_* node titles."""
    bindings = dict(existing or {})
    titled: dict[str, str] = {}
    ref_titled: list[tuple[int, str]] = []
    for node_id, node in graph.items():
        title = str((node.get("_meta") or {}).get("title") or "").strip().upper()
        if not title:
            continue
        for field, value in (node.get("inputs") or {}).items():
            if isinstance(value, list):
                continue
            target = f"{node_id}.{field}"
            titled.setdefault(title, target)
            match = re.fullmatch(r"AIVS_REF_(\d+)", title)
            if match and not any(index == int(match.group(1)) for index, _ in ref_titled):
                ref_titled.append((int(match.group(1)), target))
    aliases = {
        "prompt": ("AIVS_PROMPT",),
        "negative_prompt": ("AIVS_NEGATIVE", "AIVS_NEGATIVE_PROMPT"),
        "first_frame": ("AIVS_FIRST_FRAME",),
        "last_frame": ("AIVS_LAST_FRAME",),
        "source_image": ("AIVS_SOURCE_IMAGE", "AIVS_FIRST_FRAME"),
        "seed": ("AIVS_SEED",),
        "steps": ("AIVS_STEPS",),
        "width": ("AIVS_WIDTH",),
        "height": ("AIVS_HEIGHT",),
        "duration": ("AIVS_DURATION",),
    }
    for slot, names in aliases.items():
        if slot not in bindings:
            target = next((titled[name] for name in names if name in titled), None)
            if target:
                bindings[slot] = target
    if "reference_image" not in bindings:
        refs = sorted(ref_titled)
        if refs:
            bindings["reference_image"] = refs[0][1]
            bindings["reference_image_slots"] = [target for _, target in refs]
        elif capability == "image2video" and "AIVS_FIRST_FRAME" in titled:
            bindings["reference_image"] = titled["AIVS_FIRST_FRAME"]
    elif ref_titled:
        bindings.setdefault("reference_image_slots", [target for _, target in sorted(ref_titled)])

    if existing is not None:
        return bindings

    # 智能启发式回退：当没有显式提供绑定且节点标题没有标注 AIVS_* 前缀时，通过 class_type / 字段名启发式推测
    prompt_candidates: list[str] = []
    neg_prompt_candidates: list[str] = []
    load_images: list[tuple[str, str, str, str]] = []
    samplers: list[tuple[str, dict[str, Any]]] = []
    latents: list[tuple[str, dict[str, Any]]] = []

    for node_id, node in graph.items():
        class_type = str(node.get("class_type") or "")
        title = str((node.get("_meta") or {}).get("title") or "").strip()
        inputs = node.get("inputs") or {}
        upper_title = title.upper()

        if (
            class_type
            in (
                "CLIPTextEncode",
                "CLIPTextEncodeSDXL",
                "ShowText",
                "PrimitiveNode",
                "CR Prompt Text",
                "Text Multiline",
                "Text",
            )
            or "text" in inputs
        ):
            if "text" in inputs and not isinstance(inputs["text"], list):
                target = f"{node_id}.text"
                if any(w in upper_title for w in ("NEG", "负", "NEGATIVE", "反向")):
                    neg_prompt_candidates.append(target)
                else:
                    prompt_candidates.append(target)
            elif "prompt" in inputs and not isinstance(inputs["prompt"], list):
                target = f"{node_id}.prompt"
                if any(w in upper_title for w in ("NEG", "负", "NEGATIVE", "反向")):
                    neg_prompt_candidates.append(target)
                else:
                    prompt_candidates.append(target)

        if "LoadImage" in class_type or "image" in inputs:
            for f in ("image", "image_path", "file_path"):
                if f in inputs and not isinstance(inputs[f], list):
                    load_images.append((node_id, f, upper_title, f"{node_id}.{f}"))
                    break

        if "Sampler" in class_type or "KSampler" in class_type:
            samplers.append((node_id, inputs))

        if any(
            w in class_type
            for w in ("Latent", "EmptyLatentImage", "EmptyHunyuan", "EmptyWan", "EmptyLTXV")
        ):
            latents.append((node_id, inputs))

    if "prompt" not in bindings and prompt_candidates:
        bindings["prompt"] = prompt_candidates[0]
        if (
            len(prompt_candidates) > 1
            and "negative_prompt" not in bindings
            and not neg_prompt_candidates
        ):
            bindings["negative_prompt"] = prompt_candidates[1]

    if "negative_prompt" not in bindings and neg_prompt_candidates:
        bindings["negative_prompt"] = neg_prompt_candidates[0]

    if load_images:
        first_img = next(
            (
                tgt
                for _, _, t, tgt in load_images
                if any(w in t for w in ("FIRST", "首", "START", "起始"))
            ),
            None,
        )
        last_img = next(
            (
                tgt
                for _, _, t, tgt in load_images
                if any(w in t for w in ("LAST", "末", "尾", "END", "结束"))
            ),
            None,
        )
        ref_img = next(
            (tgt for _, _, t, tgt in load_images if any(w in t for w in ("REF", "参考"))), None
        )

        if capability == "first_last_frame":
            if "first_frame" not in bindings:
                bindings["first_frame"] = first_img or load_images[0][3]
            if "last_frame" not in bindings:
                if last_img:
                    bindings["last_frame"] = last_img
                elif len(load_images) > 1:
                    bindings["last_frame"] = load_images[1][3]
        elif capability == "image2video":
            tgt = first_img or load_images[0][3]
            if "first_frame" not in bindings:
                bindings["first_frame"] = tgt
            if "reference_image" not in bindings:
                bindings["reference_image"] = tgt
            if "source_image" not in bindings:
                bindings["source_image"] = tgt
        elif capability == "text2image":
            if "reference_image" not in bindings:
                bindings["reference_image"] = ref_img or load_images[0][3]
        elif capability == "upscale":
            if "source_image" not in bindings:
                bindings["source_image"] = first_img or load_images[0][3]

    if samplers:
        s_id, s_inputs = samplers[0]
        if "seed" not in bindings and "seed" in s_inputs and not isinstance(s_inputs["seed"], list):
            bindings["seed"] = f"{s_id}.seed"
        elif (
            "seed" not in bindings
            and "noise_seed" in s_inputs
            and not isinstance(s_inputs["noise_seed"], list)
        ):
            bindings["seed"] = f"{s_id}.noise_seed"
        if (
            "steps" not in bindings
            and "steps" in s_inputs
            and not isinstance(s_inputs["steps"], list)
        ):
            bindings["steps"] = f"{s_id}.steps"

    if latents:
        l_id, l_inputs = latents[0]
        if (
            "width" not in bindings
            and "width" in l_inputs
            and not isinstance(l_inputs["width"], list)
        ):
            bindings["width"] = f"{l_id}.width"
        if (
            "height" not in bindings
            and "height" in l_inputs
            and not isinstance(l_inputs["height"], list)
        ):
            bindings["height"] = f"{l_id}.height"
        if "duration" not in bindings:
            for f in ("length", "num_frames", "frames", "duration"):
                if f in l_inputs and not isinstance(l_inputs[f], list):
                    bindings["duration"] = f"{l_id}.{f}"
                    break

    return bindings


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


class WorkflowService:
    async def _global_db(self):
        return await global_registry.start()

    def _global_shape(self, row: GlobalWorkflow) -> dict[str, Any]:
        data = as_dict(row)
        raw_bindings = load_json(row.bindings_json, {})
        data["bindings"] = {
            key: value for key, value in raw_bindings.items() if key != "reference_image_slots"
        }
        data["reference_image_slots"] = raw_bindings.get("reference_image_slots", [])
        data["reference_image_count"] = len(data["reference_image_slots"])
        data["nodes"] = load_json(row.nodes_json, [])
        data["required_nodes"] = load_json(row.required_nodes_json, [])
        data["validation"] = load_json(row.validation_json, None)
        data["missing_slots"] = sorted(
            set(REQUIRED_SLOTS.get(row.capability, ())) - set(raw_bindings)
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

    async def list_global(self) -> list[dict[str, Any]]:
        db = await self._global_db()
        rows = await fetch_all(db, GlobalWorkflow, order_by=GlobalWorkflow.created_at)
        return [self._global_shape(r) for r in rows]

    async def get_global(self, wid: str) -> dict[str, Any]:
        db = await self._global_db()
        row = await fetch(db, GlobalWorkflow, wid, "工作流")
        data = self._global_shape(row)
        data["api_json"] = row.api_json
        return data

    async def _global_row(self, wid: str) -> GlobalWorkflow:
        return await fetch(await self._global_db(), GlobalWorkflow, wid, "工作流")

    async def project_bindings(self, pid: str) -> dict[str, str | None]:
        from app.services import route  # 延迟导入：route 在模块级 import 了这个模块

        project = (await fetch_all(db_of(pid), Project))[0]
        global_db = await self._global_db()
        async with global_db.read() as session:

            async def valid_id(wid: str | None) -> str | None:
                if not wid:
                    return None
                return wid if (await session.get(GlobalWorkflow, wid)) is not None else None

            return {
                #: **读出来先归一**：老库里这一列写的是 `workflow_api`，registry 与设置页叫
                #: `comfy_workflow`——同一条路两个名字、中间从来没有映射，前端于是拿它和
                #: registry 给的候选比对不上。空串 = 跟随设置页（`route.INHERIT`）。
                #: 坏值原样回（`_safe_normalize` 不抛）：说清哪里不对是 `GET /route` 的事，
                #: 这份绑定表不该因为一个坏字符串整个读不出来。
                "generation_mode": route._safe_normalize(project.generation_mode),
                "text2image": await valid_id(project.default_image_workflow_id),
                "image2video": await valid_id(project.default_video_workflow_id),
                "first_last_frame": await valid_id(project.default_first_last_workflow_id),
                "upscale": await valid_id(project.default_upscale_workflow_id),
            }

    async def set_project_bindings(
        self, pid: str, bindings: dict[str, str | None]
    ) -> dict[str, str | None]:
        from app.services import route  # 延迟导入：route 在模块级 import 了这个模块

        db = db_of(pid)
        global_db = await self._global_db()
        #: **写之前先归一，未知值直接抛。** 以前这里是一张写死的白名单
        #: `{"comfy_preset", "http_api", "workflow_api"}`，不在里面的值**静默丢弃**——
        #: 用户选了一条路、请求成功返回、库里那一列没变，界面上还显示着他选的那个。
        #: `route.normalize()` 收别名（`workflow_api` → `comfy_workflow`）、把未知值报成
        #: 四要素 `VALIDATION_ERROR`（写入侧该挡就挡），空串 = 跟随设置页。
        mode = (
            route.normalize(bindings["generation_mode"]) if "generation_mode" in bindings else None
        )
        valid_bindings: dict[str, str | None] = {}
        for capability, wid in bindings.items():
            if capability == "generation_mode":
                continue
            if not wid:
                valid_bindings[capability] = None
                continue
            async with global_db.read() as session:
                row = await session.get(GlobalWorkflow, wid)
            if row is None:
                valid_bindings[capability] = None
                continue
            if row.capability != capability:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "Workflow 能力不匹配",
                    f"{row.name} 提供的是 {row.capability}，不能绑定到 {capability}。",
                    ["选择同能力 Workflow"],
                )
            if row.status != "ready":
                raise AppError(
                    ErrorCode.INVALID_WORKFLOW,
                    "Workflow 尚未就绪",
                    f"{row.name} 当前状态为 {row.status}。",
                    ["先校验 Workflow"],
                )
            valid_bindings[capability] = wid
        async with db.write() as session:
            project = (await session.execute(select(Project))).scalars().first()
            assert project is not None
            if mode is not None:
                project.generation_mode = mode
            if "text2image" in bindings:
                project.default_image_workflow_id = valid_bindings.get("text2image")
            if "image2video" in bindings:
                project.default_video_workflow_id = valid_bindings.get("image2video")
            if "first_last_frame" in bindings:
                project.default_first_last_workflow_id = valid_bindings.get("first_last_frame")
            if "upscale" in bindings:
                project.default_upscale_workflow_id = valid_bindings.get("upscale")
            project.updated_at = utc_now()
        return await self.project_bindings(pid)

    async def import_global(
        self,
        *,
        name: str,
        capability: str,
        api_json: str,
        bindings: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if capability not in CAPABILITIES:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的能力类型",
                f"{capability} 不在支持列表里。",
                ["选择已支持的能力"],
            )
        graph = parse_graph(api_json)
        bindings = auto_bindings(graph, capability, bindings)
        db = await self._global_db()
        now = utc_now()
        row = GlobalWorkflow(
            id=new_id("workflow"),
            name=(name or "").strip() or "未命名工作流",
            capability=capability,
            api_json=api_json,
            bindings_json=dump_json(bindings),
            nodes_json=dump_json(node_summary(graph)),
            required_nodes_json=dump_json(custom_nodes(graph)),
            status="draft",
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        async with db.write() as session:
            session.add(row)
        return self._global_shape(row)

    async def bind_global(self, wid: str, bindings: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(bindings) - set(SLOTS) - {"reference_image_slots"})
        if unknown:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的输入槽",
                f"{'、'.join(unknown)} 不是可绑定的槽位。",
                ["检查槽位名拼写"],
            )
        db = await self._global_db()
        row = await self._global_row(wid)
        graph = parse_graph(row.api_json)
        bindings = auto_bindings(graph, row.capability, bindings)
        async with db.write() as session:
            fresh = await session.get(GlobalWorkflow, row.id)
            assert fresh is not None
            fresh.bindings_json = dump_json(bindings)
            fresh.status = "draft"
            fresh.validation_json = None
            fresh.updated_at = utc_now()
        return await self.get_global(wid)

    async def validate_global(self, wid: str, *, probe: bool = True) -> dict[str, Any]:
        db = await self._global_db()
        row = await self._global_row(wid)
        graph = parse_graph(row.api_json)
        bindings: dict[str, Any] = load_json(row.bindings_json, {})
        problems = [
            f"{slot}: 该能力必须绑定此槽位"
            for slot in sorted(set(REQUIRED_SLOTS.get(row.capability, ())) - set(bindings))
        ]
        for slot, target in bindings.items():
            if slot == "reference_image_slots":
                if isinstance(target, list):
                    problems += [
                        issue
                        for index, item in enumerate(target, 1)
                        if (issue := _check_binding(graph, f"reference_image[{index}]", str(item)))
                    ]
                continue
            if slot in SLOTS and (issue := _check_binding(graph, slot, str(target))):
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
        result = {
            "ok": not problems and not missing_nodes,
            "problems": problems,
            "missing_slots": sorted(set(REQUIRED_SLOTS.get(row.capability, ())) - set(bindings)),
            "required_nodes": required,
            "missing_nodes": missing_nodes,
            "probe": probe_detail,
            "checked_at": utc_now(),
        }
        async with db.write() as session:
            fresh = await session.get(GlobalWorkflow, wid)
            assert fresh is not None
            fresh.validation_json = dump_json(result)
            fresh.status = "ready" if result["ok"] else "invalid"
            fresh.updated_at = utc_now()
        if not result["ok"]:
            if missing_nodes and not problems:
                raise AppError(
                    ErrorCode.COMFY_NODE_MISSING,
                    "缺少 ComfyUI 节点",
                    "；".join(missing_nodes[:8]),
                    ["安装缺少的自定义节点后重新探测", "或关闭节点探测，仅检查绑定"],
                    {"workflow_id": wid, **result},
                )
            raise AppError(
                ErrorCode.INVALID_WORKFLOW,
                "绑定校验未通过",
                "；".join(problems[:8]) or "缺少 ComfyUI 节点",
                ["按规范标题自动绑定后重试"],
                {"workflow_id": wid, **result},
            )
        return result

    async def delete_global(self, wid: str) -> None:
        db = await self._global_db()
        row = await self._global_row(wid)
        async with db.write() as session:
            fresh = await session.get(GlobalWorkflow, row.id)
            if fresh is not None:
                await session.delete(fresh)

    async def set_default_global(self, wid: str) -> dict[str, Any]:
        db = await self._global_db()
        row = await self._global_row(wid)
        async with db.write() as session:
            for other in await fetch_all(
                db, GlobalWorkflow, where=GlobalWorkflow.capability == row.capability
            ):
                fresh = await session.get(GlobalWorkflow, other.id)
                if fresh is not None:
                    fresh.is_default = 1 if other.id == wid else 0
        return await self.get_global(wid)

    async def _resolve_global(self, capability: str, wid: str | None = None) -> GlobalWorkflow:
        db = await self._global_db()
        if wid:
            row = await self._global_row(wid)
            if row.capability != capability:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "Workflow 能力不匹配",
                    f"「{row.name}」提供的是 {row.capability}，当前任务需要 {capability}。",
                    ["选择与镜头能力一致的 Workflow"],
                    {"workflow_id": wid, "capability": capability},
                )
            if row.status != "ready":
                raise AppError(
                    ErrorCode.INVALID_WORKFLOW,
                    "指定的工作流未就绪",
                    f"「{row.name}」当前状态是 {row.status}。",
                    ["先校验 Workflow"],
                )
            return row
        rows = [
            r
            for r in await fetch_all(
                db, GlobalWorkflow, where=GlobalWorkflow.capability == capability
            )
            if r.status == "ready"
        ]
        if not rows:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                f"能力「{capability}」不可用",
                _impact(capability),
                ["在应用级 Workflow 管理中导入并校验"],
                {"capability": capability},
            )
        return next((r for r in rows if r.is_default), rows[0])

    async def list_workflows(self, pid: str) -> list[dict[str, Any]]:
        return await self.list_global()

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
        return await self.get_global(wid)

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
        return await self.import_global(
            name=name,
            capability=capability,
            api_json=api_json,
            bindings=bindings,
            notes=notes,
        )

    async def bind(self, pid: str, wid: str, bindings: dict[str, str]) -> dict[str, Any]:
        return await self.bind_global(wid, bindings)

    async def update(self, pid: str, wid: str, patch: dict[str, Any]) -> dict[str, Any]:
        db = await self._global_db()
        await self._global_row(wid)
        async with db.write() as session:
            row = await session.get(GlobalWorkflow, wid)
            assert row is not None
            for key in ("name", "notes", "capability"):
                if key in patch:
                    setattr(row, key, patch[key])
            if patch.get("status") in ("disabled", "draft"):
                row.status = patch["status"]
            row.updated_at = utc_now()
        return await self.get_global(wid)

    async def delete(self, pid: str, wid: str) -> None:
        await self.delete_global(wid)

    async def set_default(self, pid: str, wid: str) -> dict[str, Any]:
        return await self.set_default_global(wid)

    async def validate(self, pid: str, wid: str, *, probe: bool = True) -> dict[str, Any]:
        return await self.validate_global(wid, probe=probe)

    async def capability_matrix(self, pid: str) -> dict[str, Any]:
        """四行能力矩阵。没有任何镜头时就能回答「以后哪种镜头做不出来」。

        多回一个 `route` 块（打开了工程才有）：**这张矩阵说的是绑定表齐不齐，而绑定表只在
        「ComfyUI 工作流绑定」那条路上有意义**。走预设或 REST 的工程看着一屏红色的
        「未绑定」会去修一件根本不影响自己出片的事——`route.binds_workflow` 就是界面上
        那句「当前这条路不需要这些绑定」的来源。`comfy` 键保留给现有界面。
        """
        rows = await fetch_all(await self._global_db(), GlobalWorkflow)
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
        return {
            "capabilities": matrix,
            "comfy": await comfy.ping(),
            #: 没有工程（应用级那张矩阵）时是 `None`：那时「这个工程走哪条路」还没有答案，
            #: 编一个默认值出来只会让界面说错。
            "route": await self._route_block(pid) if pid else None,
        }

    async def _route_block(self, pid: str) -> dict[str, Any] | None:
        """这个工程当前走哪条路（矩阵上那一句提示）。**绝不抛**——矩阵是只读界面。"""
        from app.services import route  # 延迟导入：route 在模块级 import 了这个模块

        try:
            resolved = await route.resolve(pid, "image2video")
        except AppError:  # pragma: no cover - 工程没打开时（`db_of` 抛）
            return None
        return {
            "provider": resolved.provider,
            "label": resolved.label,
            "source": resolved.source,
            "binds_workflow": resolved.binds_workflow,
        }

    async def global_capability_matrix(self) -> dict[str, Any]:
        return await self.capability_matrix("")

    async def project_capabilities(self, pid: str) -> dict[str, Any]:
        matrix = await self.capability_matrix(pid)
        matrix["project_bindings"] = await self.project_bindings(pid)
        return matrix

    async def resolve(self, pid: str, capability: str, wid: str | None = None) -> GlobalWorkflow:
        """按能力挑一条可用的工作流。挑不到就报「能力缺失」而不是随便找一条。"""
        # 新路径：Workflow 是应用级资源；项目只通过 Project.default_* 选择它。
        if wid:
            row = await self._resolve_global(capability, wid)
            return row
        project = (await fetch_all(db_of(pid), Project))[0]
        selected = {
            "text2image": project.default_image_workflow_id,
            "image2video": project.default_video_workflow_id,
            "first_last_frame": project.default_first_last_workflow_id,
            "upscale": project.default_upscale_workflow_id,
        }.get(capability)
        if selected:
            global_db = await self._global_db()
            async with global_db.read() as session:
                wf_row = await session.get(GlobalWorkflow, selected)
            if wf_row is not None:
                return await self._resolve_global(capability, selected)
            selected = None
        explicit = any(
            (
                project.default_image_workflow_id,
                project.default_video_workflow_id,
                project.default_first_last_workflow_id,
                project.default_upscale_workflow_id,
            )
        )
        if explicit and not selected:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                f"项目未绑定「{capability}」Workflow",
                _impact(capability),
                ["到应用级 Workflow 管理中为项目绑定对应能力"],
                {"capability": capability, "project_id": pid},
            )
        return await self._resolve_global(capability)


def _impact(cap: str) -> str:
    return {
        "text2image": "缺少文生图，角色表与首帧无法出图。",
        "image2video": "缺少图生视频，绝大多数镜头无法生成。",
        "first_last_frame": "首尾帧缺失，依赖它的镜头无法生成。",
        "upscale": "缺少放大，成片只能停留在生成分辨率。",
    }.get(cap, f"能力 {cap} 尚未就绪。")


workflows = WorkflowService()
