"""项目概览与连续性检查（Step 9）。

概览页的唯一职责：让人一眼知道「现在到哪了、下一步做什么、哪里不对」。
连续性检查只报事实与坐标，不自动改数据——判断权在导演手里。
"""

from __future__ import annotations

from typing import Any

from app.core import ffmpeg as ffmpeg_tool
from app.core.errors import AppError
from app.generation.comfy.client import comfy
from app.generation.providers import registry
from app.persistence.models_cast import Appearance, Character, SheetVersion
from app.persistence.models_edit import ExportRecord, TimelineClip
from app.persistence.models_gen import GenerationVersion, Job
from app.persistence.models_global import GlobalWorkflow
from app.persistence.models_story import Scene, Shot, ShotCast, ShotProp
from app.persistence.models_world import Location, LocationVariant, Prop, PropReference
from app.services import route
from app.services.base import db_of, fetch_all, load_json, project_of
from app.services.global_registry import global_registry
from app.services.workflows import workflows

#: 状态中文名，前后端共用一套口径。
STATUS_LABEL = {
    "draft": "草稿",
    "ready": "就绪",
    "queued": "队列中",
    "generated": "已生成",
    "failed": "失败",
    "review": "待审",
}


def _bound_of(row: dict[str, Any]) -> str:
    """这条路上到底绑了什么：预设名 / 那份图的名字 / 那个地址。没绑上是空串。

    **按事实读，不按名字分岔**（硬约束 1）：哪一个字段有值就说它，所以以后加一条路
    （比如另一个云服务）只要 `route.Route` 上有它绑的东西，这里一行都不用改。
    """
    return str(row.get("preset") or row.get("workflow_name") or row.get("base_url") or "")


def _route_detail(label: str, r2v: dict[str, Any], flf: dict[str, Any]) -> str:
    """环境栏里那一行话：走哪条路、两种镜头各绑了什么。

    以前这句是 `R2V：x；FL2VA：y`，只在预设那条路上说得通——走 REST 的工程读到的是
    「项目尚未绑定预设 Workflow」，而那条路根本没有预设这回事。
    """
    parts = [
        f"普通镜头：{_bound_of(r2v) or '未绑定'}",
        f"衔接与转场：{_bound_of(flf) or '未绑定'}",
    ]
    return f"{label} · " + "；".join(parts)



class OverviewService:
    async def summary(self, pid: str) -> dict[str, Any]:
        db = db_of(pid)
        proj = project_of(pid)
        scenes = await fetch_all(db, Scene, order_by=Scene.index_no)
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        versions = await fetch_all(db, GenerationVersion)
        jobs = await fetch_all(db, Job)
        chars = await fetch_all(db, Character)
        apps = await fetch_all(db, Appearance)
        sheets = await fetch_all(db, SheetVersion)
        props = await fetch_all(db, Prop)
        locations = await fetch_all(db, Location)
        clips = await fetch_all(db, TimelineClip)
        exports = await fetch_all(db, ExportRecord, order_by=ExportRecord.created_at.desc())

        by_status: dict[str, int] = {}
        for shot in shots:
            by_status[shot.status] = by_status.get(shot.status, 0) + 1
        generated = sum(1 for s in shots if s.current_version_id)

        return {
            "project": proj.to_dict(),
            "counts": {
                "scenes": len(scenes),
                "shots": len(shots),
                "characters": len(chars),
                "appearances": len(apps),
                "character_sheets": len(sheets),
                "locations": len(locations),
                "props": len(props),
                "versions": len(versions),
                "timeline_clips": len(clips),
                "exports": len(exports),
            },
            "shot_status": [
                {"status": key, "label": STATUS_LABEL.get(key, key), "count": count}
                for key, count in sorted(by_status.items(), key=lambda kv: -kv[1])
            ],
            "progress": {
                "generated": generated,
                "total": len(shots),
                "percent": round(generated / len(shots) * 100, 1) if shots else 0.0,
            },
            "duration_total": round(sum(float(s.duration or 0) for s in shots), 2),
            "queue": {
                "active": sum(1 for j in jobs if j.status in ("queued", "waiting", "running")),
                "failed": sum(1 for j in jobs if j.status == "failed"),
            },
            "resume": await self.resume_pointer(pid),
            "last_export": (
                {
                    "id": exports[0].id,
                    "path": exports[0].path,
                    "status": exports[0].status,
                    "created_at": exports[0].created_at,
                }
                if exports
                else None
            ),
        }

    async def resume_pointer(self, pid: str) -> dict[str, Any] | None:
        """「继续上次工作」：指向最近改动过的镜头，而不是一个笼统的入口。"""
        db = db_of(pid)
        shots = await fetch_all(db, Shot)
        if not shots:
            return None
        latest = max(shots, key=lambda s: (s.updated_at or "", s.index_no))
        scenes = {s.id: s for s in await fetch_all(db, Scene)}
        scene = scenes.get(latest.scene_id)
        return {
            "shot_id": latest.id,
            "index_no": latest.index_no,
            "title": latest.title,
            "status": latest.status,
            "status_label": STATUS_LABEL.get(latest.status, latest.status),
            "scene_id": latest.scene_id,
            "scene_title": scene.title if scene else None,
            "updated_at": latest.updated_at,
        }

    async def activity(self, pid: str, limit: int = 20) -> list[dict[str, Any]]:
        """最近活动：版本、任务、导出三条流按时间倒排合并。"""
        db = db_of(pid)
        shots = {s.id: s for s in await fetch_all(db, Shot)}
        events: list[dict[str, Any]] = []
        for version in await fetch_all(db, GenerationVersion):
            shot = shots.get(version.shot_id)
            events.append(
                {
                    "at": version.created_at,
                    "kind": "version",
                    "text": f"Shot {shot.index_no if shot else '?'} 产出 v{version.version_no}",
                    "shot_id": version.shot_id,
                }
            )
        for job in await fetch_all(db, Job):
            if job.status not in ("failed", "canceled"):
                continue
            shot = shots.get(job.shot_id)
            what = "失败" if job.status == "failed" else "被取消"
            events.append(
                {
                    "at": job.finished_at or job.created_at,
                    "kind": f"job_{job.status}",
                    "text": f"Shot {shot.index_no if shot else '?'} 任务{what}",
                    "shot_id": job.shot_id,
                }
            )
        for record in await fetch_all(db, ExportRecord):
            events.append(
                {
                    "at": record.finished_at or record.created_at,
                    "kind": f"export_{record.status}",
                    "text": f"导出 {record.status}：{record.path}",
                    "shot_id": None,
                }
            )
        events.sort(key=lambda e: str(e["at"]), reverse=True)
        return events[:limit]

    # --- 连续性检查 ---

    async def continuity(self, pid: str) -> dict[str, Any]:
        """只报事实：哪一条、在哪个镜头、为什么可疑、可以怎么处理。"""
        db = db_of(pid)
        scenes = {s.id: s for s in await fetch_all(db, Scene)}
        shots = await fetch_all(db, Shot, order_by=Shot.index_no)
        variants = {v.id: v for v in await fetch_all(db, LocationVariant)}
        locations = {loc.id: loc for loc in await fetch_all(db, Location)}
        apps = {a.id: a for a in await fetch_all(db, Appearance)}
        chars = {c.id: c for c in await fetch_all(db, Character)}
        sheets = await fetch_all(db, SheetVersion)
        props = {p.id: p for p in await fetch_all(db, Prop)}
        prop_refs = await fetch_all(db, PropReference)
        casts = await fetch_all(db, ShotCast)
        shot_props = await fetch_all(db, ShotProp)

        issues: list[dict[str, Any]] = []

        # 1. 同一镜头里同一角色出现两个形象——十有八九是选错了
        for shot in shots:
            mine = [c for c in casts if c.shot_id == shot.id]
            seen: dict[str, list[str]] = {}
            for row in mine:
                app = apps.get(row.appearance_id)
                if app is None:
                    continue
                seen.setdefault(app.character_id, []).append(app.name)
            for cid, names in seen.items():
                if len(names) > 1:
                    who = chars[cid].name if cid in chars else "角色"
                    issues.append(
                        {
                            "kind": "character_state",
                            "severity": "error",
                            "shot_id": shot.id,
                            "shot_index_no": shot.index_no,
                            "title": f"{who}在同一镜头有多个形象",
                            "detail": "、".join(names),
                            "suggestions": ["在镜头编辑器的出场角色里只保留一个形象"],
                        }
                    )

        # 2. Scene 的 time_of_day 与所选地点变体不一致
        for scene in scenes.values():
            variant = variants.get(scene.location_variant_id or "")
            if variant is None:
                continue
            if (
                scene.time_of_day
                and variant.time_of_day
                and scene.time_of_day != variant.time_of_day
            ):
                location = locations.get(variant.location_id)
                issues.append(
                    {
                        "kind": "scene_time",
                        "severity": "warning",
                        "scene_id": scene.id,
                        "shot_id": None,
                        "title": f"Scene {scene.index_no} 的时间与地点变体不一致",
                        "detail": (
                            f"Scene 是「{scene.time_of_day}」，"
                            f"{location.name if location else '地点'} · {variant.name} 是"
                            f"「{variant.time_of_day}」。"
                        ),
                        "suggestions": [
                            "改 Scene 的时间设定",
                            f"或换一个「{scene.time_of_day}」的地点变体",
                        ],
                    }
                )

        # 3. 道具在被标记丢弃之后又出场
        discarded_at: dict[str, int] = {}
        for shot in shots:
            for row in [p for p in shot_props if p.shot_id == shot.id]:
                if row.state == "discarded":
                    discarded_at.setdefault(row.prop_id, shot.index_no)
                elif row.prop_id in discarded_at and shot.index_no > discarded_at[row.prop_id]:
                    what = props[row.prop_id].name if row.prop_id in props else "道具"
                    issues.append(
                        {
                            "kind": "prop_state",
                            "severity": "error",
                            "shot_id": shot.id,
                            "shot_index_no": shot.index_no,
                            "title": f"{what}在丢弃后又出场",
                            "detail": f"它在 Shot {discarded_at[row.prop_id]} 被标为已丢弃。",
                            "suggestions": [
                                "把本镜头的道具状态改为「已丢弃」",
                                "或修正更早那个镜头的状态",
                            ],
                        }
                    )

        # 4. 出场角色没有角色表图 —— 生成时上下文必然不完整
        for shot in shots:
            for row in [c for c in casts if c.shot_id == shot.id]:
                app = apps.get(row.appearance_id)
                if app is None:
                    continue
                mine = [s for s in sheets if s.appearance_id == app.id and s.asset_id]
                if not mine:
                    issues.append(
                        {
                            "kind": "missing_sheet",
                            "severity": "warning",
                            "shot_id": shot.id,
                            "shot_index_no": shot.index_no,
                            "title": f"{app.name} 还没有角色表图",
                            "detail": "该形象在本镜头出场，但没有可用的参考图。",
                            "suggestions": [
                                "在角色页给这个形象上传角色表",
                                "或换一个已有角色表的形象",
                            ],
                        }
                    )

        # 5. 道具出场但没有参考图
        for shot in shots:
            for row in [p for p in shot_props if p.shot_id == shot.id and p.state == "present"]:
                if not [r for r in prop_refs if r.prop_id == row.prop_id]:
                    what = props[row.prop_id].name if row.prop_id in props else "道具"
                    issues.append(
                        {
                            "kind": "missing_prop_reference",
                            "severity": "info",
                            "shot_id": shot.id,
                            "shot_index_no": shot.index_no,
                            "title": f"{what}没有参考图",
                            "detail": "它在本镜头出场，生成时不会有道具参考。",
                            "suggestions": [
                                "在道具页上传一张参考图",
                                "或忽略——不是所有道具都需要参考",
                            ],
                        }
                    )

        # 6. 依赖上游末帧但上游还没有版本
        by_id = {s.id: s for s in shots}
        for shot in shots:
            if not shot.prev_shot_id:
                continue
            prev = by_id.get(shot.prev_shot_id)
            if prev is None:
                issues.append(
                    {
                        "kind": "broken_dependency",
                        "severity": "error",
                        "shot_id": shot.id,
                        "shot_index_no": shot.index_no,
                        "title": "上游镜头已不存在",
                        "detail": f"prev_shot_id={shot.prev_shot_id} 找不到。",
                        "suggestions": ["清空该镜头的上游依赖", "或重新指定一个上游镜头"],
                    }
                )
            elif not prev.current_version_id:
                issues.append(
                    {
                        "kind": "upstream_not_ready",
                        "severity": "info",
                        "shot_id": shot.id,
                        "shot_index_no": shot.index_no,
                        "title": f"等待 Shot {prev.index_no} 出片",
                        "detail": "本镜头需要上游末帧，上游还没有当前版本。",
                        "suggestions": [
                            f"先生成 Shot {prev.index_no}",
                            "或改为不依赖上游的生成方式",
                        ],
                    }
                )

        order = {"error": 0, "warning": 1, "info": 2}
        issues.sort(key=lambda i: (order.get(str(i["severity"]), 3), i.get("shot_index_no") or 0))
        return {
            "issues": issues,
            "counts": {
                key: sum(1 for i in issues if i["severity"] == key)
                for key in ("error", "warning", "info")
            },
            "clean": not issues,
        }

    # --- 环境与能力 ---

    async def environment(self, pid: str | None = None) -> dict[str, Any]:
        """状态条要的东西：这个工程走哪条路出片、ComfyUI、FFmpeg、GPU/显存。缺什么都要说清影响。

        **三条路各自诚实**：以前这一栏里 `generation.mode` 是写死的 `"comfy_preset"`、预设名
        一律去数 `r2v_preset_name`、ComfyUI 无条件 ping——于是走「通用 REST API」的工程在这儿
        看到的是一屏与自己无关的状态（「ComfyUI 离线」+「未绑定预设」），而真正的问题
        （REST 地址没配）一个字都没提，用户永远查不到原因。

        现在这一块整个来自 `services/route.py::summary()`——与 `GET /projects/{pid}/route`
        **同一份口径同一次解析**，两边不会说出不一样的话；服务在不在改由
        `registry.provider(...).probe()` 回答，业务层照旧一个 `if provider ==` 都不写
        （硬约束 1）。

        `comfy` 与 `capabilities` 两个键原样保留：状态条与能力矩阵还在读它们，而且显存那一栏
        只有 ComfyUI 答得出来——ComfyUI 在不在线对预设与工作流绑定两条路都成立，
        走 REST 的工程则由 `generation.mode_label` 说清「这一栏与这条路无关」。
        """
        ping = await comfy.ping()
        found = ffmpeg_tool.locate("ffmpeg")
        gpu: dict[str, Any] = {"available": False, "detail": "未能读取（需要 ComfyUI 在线）"}
        if ping["online"]:
            try:
                stats: dict[str, Any] | None = await comfy.system_stats()
            except Exception:  # noqa: BLE001 - 探测失败不该让概览页崩
                stats = None
            device = ((stats or {}).get("devices") or [{}])[0]
            total = device.get("vram_total")
            free = device.get("vram_free")
            if total:
                gpu = {
                    "available": True,
                    "name": device.get("name"),
                    "vram_total_mb": round(total / 1024 / 1024),
                    "vram_free_mb": round((free or 0) / 1024 / 1024),
                    "detail": f"{device.get('name')} · 空闲 {round((free or 0) / 1024**3, 1)}G",
                }
        capabilities = await workflows.capability_matrix(pid) if pid else None
        generation = await self._generation_of(pid) if pid else None
        return {
            "comfy": ping,
            "ffmpeg": {
                "available": found.available,
                "path": found.path or "",
                # 来源单独一个字段（UI 拿它当徽标），detail 就留给「具体是哪一个文件」——
                # 那是排查「为什么用的不是我以为的那份」时唯一有用的信息。
                "source": found.source or "",
                "detail": (
                    found.path
                    if found.available
                    else (
                        f"配置指向的 {found.configured_missing} 不存在"
                        if found.configured_missing
                        else "还没有内置副本，PATH 里也没有；导出会失败"
                    )
                ),
                "impact": None if found.available else "无法导出成片；其余功能不受影响。",
                "hint": "" if found.available else ffmpeg_tool.FETCH_HINT,
            },
            "gpu": gpu,
            "capabilities": capabilities,
            "generation": generation,
        }

    async def _generation_of(self, pid: str) -> dict[str, Any]:
        """「这个工程怎么出片」摆在环境栏里的那一小块。

        整块来自 `route.summary(pid)`——**与 `GET /projects/{pid}/route` 同一次解析**，
        所以概览页那一大块与状态条这一小块永远说同一句话。这里只做两件事：把两条能力挑成
        平铺的字段（状态条没地方画一张表），再补一次「服务在不在」的探测。
        """
        summary = await route.summary(pid)
        rows = {row["capability"]: row for row in summary["capabilities"]}
        r2v = rows.get("image2video") or {}
        flf = rows.get("first_last_frame") or {}
        r2v_slots = r2v.get("slots") or {}
        return {
            # --- 走哪条路：`mode` 不再是写死的字符串，`mode_source` 说清是谁给的答案 ---
            "mode": summary["provider"],
            "mode_label": summary["label"],
            #: `project`（工程显式选了）/ `settings`（跟随设置页）/ `default`（谁都没选过）。
            "mode_source": summary["source"],
            #: 界面上那句「这条路不需要工作流绑定」只看这一个布尔。
            "binds_workflow": summary["binds_workflow"],
            "workflow_name": r2v.get("workflow_name"),
            "service": await self._probe_of(summary["provider"]),
            #: 两条能力（普通镜头 / 衔接与转场）各自 ready 才算这个工程能出片。
            "ready": bool(r2v.get("ready")) and bool(flf.get("ready")),
            #: 缺什么。四要素形状，前端原样显示 suggestions（硬约束 4）。
            "issues": [*(r2v.get("issues") or []), *(flf.get("issues") or [])],
            # --- 下面这些键名沿用旧形状，但值全部来自解析结果 ---
            #: 预设那条路才有值：走 REST / 工作流绑定时是 `None`，**不再谎报「未绑定预设」**。
            "preset_name": r2v.get("preset"),
            #: 「就绪」说的是**这条路**能不能出片，不是「有没有选预设」——走 REST 的工程
            #: 配好地址就是就绪，它本来就没有预设这回事。
            "preset_ready": bool(r2v.get("ready")),
            #: 参考素材槽位按**真正会提交的那条路**数（预设那份图 / 绑定表里的参考图槽位 /
            #: REST 合同的不限量）。参考视频 / 参考音频单独给：混进 `ref_slots` 会让状态条显示
            #: 「能收 5 个参考素材」而其中 2 个只吃音频——用户照着塞图必然白跑一趟。
            "ref_slots": r2v_slots.get("image"),
            "ref_video_slots": r2v_slots.get("video"),
            "ref_audio_slots": r2v_slots.get("audio"),
            "ref_detail": r2v_slots.get("detail"),
            "r2v_name": r2v.get("preset"),
            "r2v_ready": bool(r2v.get("ready")),
            "r2v_ref_slots": r2v_slots.get("image"),
            "r2v_ref_video_slots": r2v_slots.get("video"),
            "r2v_ref_audio_slots": r2v_slots.get("audio"),
            "flf_name": flf.get("preset"),
            "flf_ready": bool(flf.get("ready")),
            "detail": _route_detail(summary["label"], r2v, flf),
        }

    async def _probe_of(self, provider_name: str) -> dict[str, Any]:
        """这条路的服务在不在——「测试连接」那一下。**绝不抛。**

        分岔交给 `registry.provider()`：这里既不知道也不需要知道被探的是 ComfyUI 还是某个
        REST 地址（硬约束 1）。探测失败本身就是这一栏要显示的内容，所以四要素原样放进
        `error`，界面照旧能显示 suggestions。

        **它只回答「服务在不在」，不回答「绑没绑上」**：一份图绑给哪个能力是按工程存的，
        适配器不认识工程（provider 层不 import service 层）。后半句在 `issues` 里，
        两处各判一遍必然分叉成「这儿说就绪、一按生成说没绑图」。
        """
        try:
            result = await registry.provider(provider_name).probe()
        except AppError as exc:
            return {"ok": False, "target": "", "detail": exc.detail, "error": exc.to_dict()}
        except Exception as exc:  # noqa: BLE001 - 探测失败不该让概览页崩
            return {"ok": False, "target": "", "detail": str(exc), "error": None}
        return {
            "ok": bool(result.get("ok")),
            "target": str(result.get("target") or ""),
            "detail": str(result.get("detail") or ""),
            "error": None,
        }

    async def workflow_health(self, pid: str) -> list[dict[str, Any]]:
        db = await global_registry.start()
        rows = await fetch_all(db, GlobalWorkflow, order_by=GlobalWorkflow.created_at)
        return [
            {
                "id": r.id,
                "name": r.name,
                "capability": r.capability,
                "status": r.status,
                "is_default": bool(r.is_default),
                "validation": load_json(r.validation_json, None),
            }
            for r in rows
        ]


overview = OverviewService()
