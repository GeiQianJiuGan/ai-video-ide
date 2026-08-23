"""生成队列与版本（Step 7）。

三条硬约束在这里同时落地：
  1. 依赖可解释——等上游末帧的任务写明「等待上游 Shot 14 完成（需要末帧）」，
     而不是让人以为卡住了；
  2. 版本永不覆盖——每次生成新增一条 GenerationVersion，冻结当次参数与上下文账单；
  3. 失败绝不静默——失败现场存结构化错误 + 原始报错 + 节点图快照，并给出下一步动作。

调度器是进程内的：每个工程一个 pump，按 worker_limit 控制并发。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.events.bus import Channel, bus
from app.generation.comfy.client import comfy, outputs_of
from app.generation.providers import registry
from app.generation.providers.base import RefImage, TaskState, VideoRequest
from app.persistence.models import Project, utc_now
from app.persistence.models_gen import GenerationVersion, Job
from app.persistence.models_global import GlobalWorkflow
from app.persistence.models_story import Scene, Shot
from app.persistence.models_world import Asset
from app.services.assets import assets, kind_of_suffix
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, load_json, project_of
from app.services.context import context
from app.services.frames import start_frame_index
from app.services.global_registry import global_registry
from app.services.workflows import apply_bindings, parse_graph, workflows

log = get_logger("queue")

ACTIVE = ("queued", "waiting", "running")
POLL_INTERVAL = 1.0
POLL_LIMIT = 1800  # 30 分钟上限，超时也要给出结构化错误而不是永远转圈

#: 版本没有资产（失败现场、占位版本）时的媒体字段。**两个键永远都在**——
#: 前端按「哪个非空」决定画 `<video>` 还是 `<img>`，键时有时无就得到处写可选判断。
_NO_MEDIA: dict[str, Any] = {"video_path": None, "thumbnail_path": None}


def drop_entry(shot: Shot, capacity: dict[str, Any]) -> dict[str, Any] | None:
    """这个镜头会不会有参考图喂不进去。装得下时回 None。

    `capacity` 就是账单里的那一块（`context._capacity_of`）——**能收几张由适配层回答**，
    这里不再有任何应用级上限可看。
    """
    if not capacity.get("over"):
        return None
    return {
        "shot_id": shot.id,
        "index_no": shot.index_no,
        "title": shot.title,
        "ref_count": int(capacity.get("ref_count") or 0),
        "limit": capacity.get("limit"),
        "dropped": int(capacity.get("dropped") or 0),
        "labels": [str(x) for x in (capacity.get("dropped_labels") or [])],
        "source": str(capacity.get("source") or ""),
    }


def over_capacity_error(drops: list[dict[str, Any]]) -> AppError:
    """「会丢几张图，还继续吗」——**这不是失败，是一次确认**。

    所以它必须在入队**任何一个任务之前**抛出来（批量路径先整体扫一遍再动手）：
    一半已经入队、另一半等确认的话，用户点了确认就会把前一半再入队一遍。
    确认的样子是重新调一次同一个入口并带上 `allow_ref_drop=true`，
    之后按槽位顺序喂前 N 张，少喂了哪几张照旧记进 `params.ref_notes`。
    """
    total = sum(int(d["dropped"]) for d in drops)
    lines = "；".join(
        f"Shot {d['index_no']} 采用了 {d['ref_count']} 张参考图，这里只能喂 {d['limit']} 张，"
        f"会丢 {d['dropped']} 张" + (f"（{'、'.join(d['labels'])}）" if d["labels"] else "")
        for d in drops
    )
    source = next((str(d["source"]) for d in drops if d.get("source")), "")
    where = f"（能收几张由{source}决定）" if source else ""
    return AppError(
        ErrorCode.REF_OVER_CAPACITY,
        f"有 {len(drops)} 个镜头的参考图装不下，会丢 {total} 张",
        f"{lines}{where}。丢的是账单里优先级最低的那几张——角色图最先保住，道具图最先被挤掉。",
        [
            "确认后继续：按槽位顺序喂前几张，丢掉哪几张会记进版本参数，事后查得到",
            "不想丢：在 ComfyUI 里给这份图多标几个 AIVS_REF_n 标题，重新上传预设",
            "或在上下文检查器里手动移除不重要的参考图，自己决定丢哪几张",
        ],
        {
            "shot_ids": [str(d["shot_id"]) for d in drops],
            "dropped": total,
            #: 前端据此知道「这条错误可以确认过去」——带上这个参数再调一次同一个入口。
            "confirm": "allow_ref_drop",
        },
    )


class GenerationService:
    def __init__(self) -> None:
        self._pumps: dict[str, asyncio.Task[None]] = {}
        self._paused: set[str] = set()
        self._cancelled: set[str] = set()

    # --- 入队 ---

    async def enqueue_shot(
        self,
        pid: str,
        shot_id: str,
        *,
        kind: str | None = None,
        priority: int = 100,
        workflow_id: str | None = None,
        check_context: bool = True,
        allow_ref_drop: bool = False,
        first_frame_asset_id: str | None = None,
        last_frame_asset_id: str | None = None,
        wait_for_job_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        db = db_of(pid)
        shot = await fetch(db, Shot, shot_id, "镜头")
        # 幕级 prompt 是镜头没写 prompt 时的兜底（流程图上那个必填的小节点），
        # 取值口径与 context.resolve 的 problems 一致，否则会出现「账单说齐了，冻结进
        # 版本里的 prompt 却是空的」。
        scene_of_shot = await fetch(db, Scene, shot.scene_id, "场景")
        prompt = shot.prompt or shot.description or scene_of_shot.prompt or ""
        capability = kind or ("first_last_frame" if shot.prev_shot_id else "image2video")
        # 走适配层时不需要工作流：模型端那份图由模型端维护（预设 / 通用 REST 合同）。
        # 只有旧的 comfy_workflow 兼容路径才必须先解析出一份已校验的工作流。
        project = (await fetch_all(db, Project))[0]
        generation_mode = "comfy_preset"
        preset_name = project.preset_name or settings.video_preset
        # 保留旧设置的明确错误提示；正常项目生成始终走项目选择的预设。
        if registry.is_legacy():
            await workflows.resolve(pid, capability, workflow_id or shot.workflow_id)
        if check_context:
            await context.require_complete(pid, shot_id)
        snapshot = await context.snapshot(pid, shot_id)
        # 参考图比模型端那份图能收的多时先问一句。**不是失败**：确认（allow_ref_drop）后
        # 照旧生成，只是按槽位顺序喂前几张。悄悄少喂两张图，事后没人查得出形象为什么跑偏。
        if not allow_ref_drop:
            over = drop_entry(shot, snapshot.get("capacity") or {})
            if over:
                raise over_capacity_error([over])

        depends_on, wait_reason = None, None
        if shot.prev_shot_id:
            prev = await fetch(db, Shot, shot.prev_shot_id, "上游镜头")
            if wait_for_job_id:
                # 单线程续接等待的是本次编排中的上一条任务，而不是旧的当前版本。
                # 否则上游已有旧版本时，下游会提前并发启动，拿不到本次生成的真末帧。
                depends_on = prev.id
                wait_reason = f"等待上游 Shot {prev.index_no} 完成本次生成（需要末帧）"
            elif not prev.current_version_id:
                depends_on = prev.id
                wait_reason = f"等待上游 Shot {prev.index_no} 完成（需要末帧）"

        row = Job(
            id=new_id("job"),
            shot_id=shot_id,
            kind=capability,
            status="waiting" if depends_on else "queued",
            priority=priority,
            depends_on=depends_on,
            wait_reason=wait_reason,
            workflow_id=None,
            params_json=dump_json(
                {
                    "prompt": prompt,
                    "negative_prompt": shot.negative_prompt,
                    "seed": shot.seed,
                    "steps": shot.steps,
                    "duration": shot.duration,
                    "context": snapshot,
                    "first_frame_asset_id": first_frame_asset_id,
                    "last_frame_asset_id": last_frame_asset_id,
                    "wait_for_job_id": wait_for_job_id,
                    "extra": extra or {},
                    "generation_mode": generation_mode,
                    "preset": preset_name,
                }
            ),
            created_at=utc_now(),
        )
        async with db.write() as session:
            session.add(row)
        bus.emit(
            Channel.QUEUE,
            "job.enqueued",
            {"id": row.id, "shot_id": shot_id, "status": row.status},
            project_id=pid,
        )
        self.ensure_pump(pid)
        return as_dict(row)

    async def ref_drops(self, pid: str, shot_ids: list[str]) -> list[dict[str, Any]]:
        """这些镜头里哪几个的参考图装不进模型端那份图。只读，给「先账单再动手」用。

        某个镜头连账单都算不出来（缺地点、被删了）时跳过它：那件事有它自己的四要素错误，
        真入队时照旧会被跳过并说明原因，不该在这里被含含糊糊地归成「图太多」。
        """
        db = db_of(pid)
        out: list[dict[str, Any]] = []
        for shot_id in shot_ids:
            try:
                shot = await fetch(db, Shot, shot_id, "镜头")
                ctx = await context.resolve(pid, shot_id)
            except AppError:
                continue
            entry = drop_entry(shot, ctx["capacity"])
            if entry:
                out.append(entry)
        return out

    async def enqueue_scene(
        self, pid: str, scene_id: str, priority: int = 100, *, allow_ref_drop: bool = False
    ) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Scene, scene_id, "场景")
        shots = [
            s for s in await fetch_all(db, Shot, order_by=Shot.index_no) if s.scene_id == scene_id
        ]
        if not shots:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "该场景还没有镜头",
                "没有可以生成的对象。",
                ["先在分镜板里添加镜头"],
                {"scene_id": scene_id},
            )
        # 整幕先扫一遍再动手：一半入队、一半等确认的话，用户点了确认会把前一半再入队一遍。
        if not allow_ref_drop:
            drops = await self.ref_drops(pid, [s.id for s in shots])
            if drops:
                raise over_capacity_error(drops)
        queued, skipped = [], []
        for shot in shots:
            try:
                job = await self.enqueue_shot(
                    pid, shot.id, priority=priority, allow_ref_drop=allow_ref_drop
                )
                queued.append(job["id"])
            except AppError as err:
                skipped.append(
                    {"shot_id": shot.id, "index_no": shot.index_no, "error": err.to_dict()}
                )
        return {"queued": queued, "skipped": skipped, "total": len(shots)}

    # --- 队列视图与控制 ---

    async def list_jobs(self, pid: str, status: str | None = None) -> list[dict[str, Any]]:
        db = db_of(pid)
        rows = await fetch_all(db, Job, order_by=Job.created_at)
        shots = {s.id: s for s in await fetch_all(db, Shot)}
        out = []
        for row in rows:
            if status and row.status != status:
                continue
            shot = shots.get(row.shot_id)
            out.append(
                {
                    **as_dict(row),
                    "shot_index_no": shot.index_no if shot else None,
                    "shot_title": shot.title if shot else None,
                    "params": load_json(row.params_json, {}),
                    "error": load_json(row.error_json, None),
                }
            )
        out.sort(
            key=lambda j: (
                ACTIVE.index(j["status"]) if j["status"] in ACTIVE else 9,
                -int(j["priority"]),
                str(j["created_at"]),
            )
        )
        return out

    async def queue_state(self, pid: str) -> dict[str, Any]:
        jobs = await self.list_jobs(pid)
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        return {
            "paused": pid in self._paused,
            "worker_limit": settings.worker_limit,
            "counts": counts,
            "active": sum(counts.get(s, 0) for s in ACTIVE),
            "jobs": jobs,
        }

    async def pause(self, pid: str) -> dict[str, Any]:
        self._paused.add(pid)
        bus.emit(Channel.QUEUE, "queue.paused", {"project_id": pid}, project_id=pid)
        return await self.queue_state(pid)

    async def resume(self, pid: str) -> dict[str, Any]:
        self._paused.discard(pid)
        bus.emit(Channel.QUEUE, "queue.resumed", {"project_id": pid}, project_id=pid)
        self.ensure_pump(pid)
        return await self.queue_state(pid)

    async def cancel(self, pid: str, job_id: str) -> dict[str, Any]:
        db = db_of(pid)
        row = await fetch(db, Job, job_id, "任务")
        if row.status in ("done", "failed", "canceled"):
            raise AppError(
                ErrorCode.CONFLICT,
                "该任务已经结束",
                f"当前状态是 {row.status}，无法取消。",
                ["若想重做，请用「重试」", "或重新入队一个新任务"],
                {"job_id": job_id},
            )
        self._cancelled.add(job_id)
        await self._set(pid, job_id, status="canceled", finished_at=utc_now())
        return await self._one(pid, job_id)

    async def set_priority(self, pid: str, job_id: str, priority: int) -> dict[str, Any]:
        await fetch(db_of(pid), Job, job_id, "任务")
        await self._set(pid, job_id, priority=priority)
        return await self._one(pid, job_id)

    async def retry(self, pid: str, job_id: str) -> dict[str, Any]:
        db = db_of(pid)
        row = await fetch(db, Job, job_id, "任务")
        if row.status not in ("failed", "canceled"):
            raise AppError(
                ErrorCode.CONFLICT,
                "只有失败或已取消的任务可以重试",
                f"当前状态是 {row.status}。",
                ["等它跑完", "或先取消再重试"],
                {"job_id": job_id},
            )
        self._cancelled.discard(job_id)
        await self._set(
            pid, job_id, status="queued", error_json=None, progress=0.0, finished_at=None
        )
        self.ensure_pump(pid)
        return await self._one(pid, job_id)

    async def retry_failed(self, pid: str) -> dict[str, Any]:
        jobs = [j for j in await self.list_jobs(pid) if j["status"] == "failed"]
        for job in jobs:
            await self.retry(pid, job["id"])
        return {"retried": [j["id"] for j in jobs]}

    async def _one(self, pid: str, job_id: str) -> dict[str, Any]:
        jobs = await self.list_jobs(pid)
        found = next((j for j in jobs if j["id"] == job_id), None)
        if found is None:  # pragma: no cover - fetch 已经保证存在
            raise AppError(ErrorCode.NOT_FOUND, "任务不存在", job_id, ["刷新队列后重试"])
        return found

    async def _set(self, pid: str, job_id: str, **fields: Any) -> None:
        db = db_of(pid)
        async with db.write() as session:
            row = await session.get(Job, job_id)
            if row is None:
                return
            for key, value in fields.items():
                setattr(row, key, value)
        bus.emit(Channel.JOB, "job.updated", {"id": job_id, **fields}, project_id=pid)

    # --- 调度 ---

    def ensure_pump(self, pid: str) -> None:
        task = self._pumps.get(pid)
        if task is None or task.done():
            self._pumps[pid] = asyncio.create_task(self._pump(pid))

    async def stop_pump(self, pid: str) -> None:
        task = self._pumps.pop(pid, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关停不该再抛
                pass

    async def _pump(self, pid: str) -> None:
        """取一批可跑的任务并发执行，直到队列里没有可跑的东西。"""
        try:
            while True:
                if pid in self._paused:
                    return
                ready = await self._claim(pid)
                if not ready:
                    return
                await asyncio.gather(*(self._run(pid, job_id) for job_id in ready))
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as exc:  # noqa: BLE001 - pump 崩了必须说出来
            log.error("queue.pump_failed", project_id=pid, error=str(exc))
            bus.emit(Channel.ERROR, "queue.pump_failed", {"error": str(exc)}, project_id=pid)

    async def _claim(self, pid: str) -> list[str]:
        """把 waiting 里依赖已满足的转成 queued，然后按优先级领取。"""
        db = db_of(pid)
        jobs = await fetch_all(db, Job)
        shots = {s.id: s for s in await fetch_all(db, Shot)}
        running = [j for j in jobs if j.status == "running"]
        slots = max(0, settings.worker_limit - len(running))

        by_id = {j.id: j for j in jobs}
        for job in [j for j in jobs if j.status == "waiting"]:
            params = load_json(job.params_json, {})
            wait_for_job_id = params.get("wait_for_job_id")
            if wait_for_job_id:
                upstream_job = by_id.get(str(wait_for_job_id))
                if upstream_job is not None and upstream_job.status == "done" and upstream_job.version_id:
                    await self._set(pid, job.id, status="queued", wait_reason=None)
                elif upstream_job is not None and upstream_job.status in ("failed", "canceled"):
                    await self._set(
                        pid,
                        job.id,
                        status="failed",
                        error_json=dump_json(
                            {
                                "code": ErrorCode.UPSTREAM_NOT_READY,
                                "title": "上游 Shot 生成失败",
                                "detail": "本次续接不会跳过失败的上游任务，因此没有启动当前 Shot。",
                                "suggestions": ["先重试上游 Shot", "上游完成后重新执行单线程续接"],
                                "related_ids": {"job_id": str(wait_for_job_id)},
                            }
                        ),
                        finished_at=utc_now(),
                        wait_reason=None,
                    )
                continue
            upstream = shots.get(job.depends_on or "")
            if upstream is None or upstream.current_version_id:
                await self._set(pid, job.id, status="queued", wait_reason=None)
        if slots <= 0:
            return []
        fresh = await fetch_all(db, Job)
        queued = sorted(
            [j for j in fresh if j.status == "queued"],
            key=lambda j: (-j.priority, j.created_at),
        )
        return [j.id for j in queued[:slots]]

    async def _run(self, pid: str, job_id: str) -> None:
        db = db_of(pid)
        job = await fetch(db, Job, job_id, "任务")
        if job_id in self._cancelled:
            return
        await self._set(
            pid, job_id, status="running", started_at=utc_now(), attempt=job.attempt + 1
        )
        bus.emit(Channel.JOB, "job.started", {"id": job_id, "shot_id": job.shot_id}, project_id=pid)
        params = load_json(job.params_json, {})
        try:
            version = await self._execute(pid, job, params)
        except AppError as err:
            await self._fail(pid, job_id, err, params)
            return
        except Exception as exc:  # noqa: BLE001 - 未预期异常同样要变成结构化失败
            await self._fail(
                pid,
                job_id,
                AppError(
                    ErrorCode.INTERNAL,
                    "生成过程出现未预期错误",
                    f"{type(exc).__name__}: {exc}",
                    ["查看后端日志 .runtime/logs", "重试该任务"],
                ),
                params,
            )
            return
        await self._set(
            pid,
            job_id,
            status="done",
            progress=1.0,
            version_id=version["id"],
            finished_at=utc_now(),
        )
        bus.emit(
            Channel.QUEUE,
            "job.done",
            {"id": job_id, "shot_id": job.shot_id, "version_id": version["id"]},
            project_id=pid,
        )

    async def _fail(self, pid: str, job_id: str, err: AppError, params: dict[str, Any]) -> None:
        payload = {
            **err.to_dict(),
            "raw": err.related_ids.get("raw"),
            "graph_snapshot": params.get("graph_snapshot"),
        }
        await self._set(
            pid, job_id, status="failed", error_json=dump_json(payload), finished_at=utc_now()
        )
        bus.emit(Channel.ERROR, "job.failed", {"id": job_id, **err.to_dict()}, project_id=pid)

    async def _execute(self, pid: str, job: Job, params: dict[str, Any]) -> dict[str, Any]:
        """跑一次生成。

        两条路：**默认走生成适配层**（`app/generation/providers/*`，本工具不维护模型端的图），
        旧的节点绑定路径只在「设置里选了 comfy_workflow 且这个任务确实绑了工作流」时才走
        ——它是兼容选项，不是主路。产物登记与 `add_version` 两条路完全共用。
        """
        if job.workflow_id:
            filename, data, workflow_id = await self._run_legacy(pid, job, params)
        else:
            filename, data, workflow_id = await self._run_provider(pid, job, params)
        kind = (
            "generated_video"
            if filename.lower().endswith((".mp4", ".webm", ".mov", ".gif"))
            else "generated_image"
        )
        asset = await assets.register_bytes(pid, kind, filename, data, source="generated")
        return await self.add_version(
            pid,
            job.shot_id,
            asset_id=asset["id"],
            kind="video" if kind == "generated_video" else "image",
            workflow_id=workflow_id,
            params={k: v for k, v in params.items() if k != "context"},
            context_snapshot=params.get("context"),
            source="generated",
        )

    async def _run_provider(
        self, pid: str, job: Job, params: dict[str, Any]
    ) -> tuple[str, bytes, str | None]:
        """默认路径：把请求交给适配器，轮询到出片。service 层不认识任何具体模型。"""
        provider = registry.provider("comfy_preset")
        first, last, refs = await self._images_of(pid, job, params)
        mode = "flf" if last is not None else "i2v"
        if mode == "i2v" and first is None:
            raise AppError(
                ErrorCode.MISSING_INPUT,
                "这个镜头还没有首帧",
                "本轮只做图生视频（R2V），必须有一张首帧图才能生成。",
                [
                    "在场景工作台里给它挑一张首帧（角色表 / 地点参考 / 上传的图都行）",
                    "或把上一幕的衔接改成「续接末帧」，让上一幕的末帧当它的首帧",
                ],
                {"shot_id": job.shot_id},
            )
        req = VideoRequest(
            mode=mode,
            prompt=str(params.get("prompt") or ""),
            negative=str(params.get("negative_prompt") or ""),
            first_frame=first,
            last_frame=last,
            refs=refs,
            duration=float(params.get("duration") or 4.0),
            seed=params.get("seed"),
            extra={**(params.get("extra") or {}), "preset": params.get("preset")},
        )
        task_id = await provider.submit(req, client_id=f"aivs-{pid}")
        # 冻结「实际喂了哪几张参考图」与适配器的降级说明。`_execute` 在这之后才收集
        # params，所以这里改它就会跟着版本一起存下来——账单说要喂 5 张、图只收了 3 张，
        # 事后必须在版本里看得见这件事（绝不静默失败）。
        params["refs"] = [{"label": r.label, "kind": r.kind, "file": r.path.name} for r in refs]
        if req.notes:
            params["ref_notes"] = list(req.notes)
        state = TaskState("queued")
        for tick in range(POLL_LIMIT):
            self._require_not_cancelled(job.id)
            state = await provider.poll(task_id)
            if state.status == "done":
                break
            if state.status == "failed":
                raise AppError(
                    ErrorCode.WORKFLOW_ERROR,
                    "生成失败",
                    state.detail or "服务端报告任务失败。",
                    [
                        "展开原始报错查看服务端给的信息",
                        "在设置页「测试连接」确认服务与预设仍然可用",
                        "调低并发生成数后重试（显存不足时常见）",
                    ],
                    {"task_id": task_id, "raw": str(state.raw)[:2000]},
                )
            if tick % 5 == 0:
                bus.emit(
                    Channel.JOB,
                    "job.progress",
                    {
                        "id": job.id,
                        "progress": max(state.progress, min(0.95, tick / 120)),
                        "detail": state.detail,
                    },
                    project_id=pid,
                )
            await asyncio.sleep(POLL_INTERVAL)
        else:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "等生成结果超时",
                f"等了 {POLL_LIMIT} 次仍未完成（最后状态：{state.detail or state.status}）。",
                ["确认服务端仍在跑这个任务", "重试该任务", "或调大设置里的单次超时"],
                {"task_id": task_id},
            )
        filename, data = await provider.fetch(task_id)
        return filename, data, None

    async def _images_of(
        self, pid: str, job: Job, params: dict[str, Any]
    ) -> tuple[Path | None, Path | None, list[RefImage]]:
        """定首帧、末帧，以及**首尾帧之外的那些参考图**。

        这里只挑「哪几张图、谁是谁」，不管模型怎么用它们——差异在适配器里。
        「哪一张当首帧」这条规则不在这里，在 `services/context.py::_assign_roles`
        （账单上标的 `role`）：两边各挑一次的话，检查器里标的和真正喂进去的会分叉。

        剩下的采用条目全部当参考图带走，**这就是「角色/场景真正被引入」的那一步**——
        以前它们算进了账单却在这里被丢掉，于是只剩一张首帧，人物形象自然跑偏。
        显式指定的首/末帧如果本身就在账单里，不会再重复当一次参考图。

        有一处不能用入队时冻结的那份账单：**要接上游末帧的镜头**。那张图在入队的时刻
        还不存在（上游还没出片），所以对这种镜头在真正要跑的时候重新结一次账并按需抽帧
        ——否则会拿一段视频、或者随便一张角色表去当首帧。
        """
        db = db_of(pid)
        proj = project_of(pid)
        explicit_first = params.get("first_frame_asset_id")
        explicit_last = params.get("last_frame_asset_id")
        included = [i for i in (params.get("context") or {}).get("included") or []]
        shot = await fetch(db, Shot, job.shot_id, "镜头")
        if shot.prev_shot_id and not explicit_first:
            fresh = await context.ensure_frames(pid, job.shot_id)
            included = [i for i in fresh["items"] if i.get("included")]
        usable = [i for i in included if i.get("asset_id")]
        if not explicit_first:
            chosen = next((i for i in usable if i.get("role") == "first_frame"), None)
            if chosen is None:  # 旧版本冻结的账单里没有 role，退回原来的挑法
                chosen = next((i for i in usable if i.get("kind") == "prev_frame"), None) or next(
                    iter(usable), None
                )
            explicit_first = (chosen or {}).get("asset_id")
        spent = {explicit_first, explicit_last} - {None, ""}

        async def resolve(asset_id: str | None) -> Path | None:
            if not asset_id:
                return None
            asset = await fetch(db, Asset, asset_id, "资产")
            return proj.dir / asset.path

        refs: list[RefImage] = []
        for item in usable:
            asset_id = str(item["asset_id"])
            if asset_id in spent:
                continue
            spent.add(asset_id)  # 同一张图在账单里出现两次时只喂一次
            path = await resolve(asset_id)
            if path is None:
                continue
            refs.append(
                RefImage(path=path, label=str(item.get("label") or ""), kind=str(item["kind"]))
            )
        return await resolve(explicit_first), await resolve(explicit_last), refs

    def _require_not_cancelled(self, job_id: str) -> None:
        if job_id in self._cancelled:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "任务已取消",
                "在等待生成结果时被取消。",
                ["需要的话重新入队"],
            )

    async def _run_legacy(
        self, pid: str, job: Job, params: dict[str, Any]
    ) -> tuple[str, bytes, str | None]:
        """Workflow API 路径：上传输入图片，再按 AIVS_* 标题写入图。"""
        workflow = await fetch(
            await global_registry.start(), GlobalWorkflow, job.workflow_id or "", "工作流"
        )
        graph = parse_graph(workflow.api_json)
        raw_bindings: dict[str, Any] = load_json(workflow.bindings_json, {})
        bindings = {
            key: str(value)
            for key, value in raw_bindings.items()
            if key != "reference_image_slots" and isinstance(value, str) and "." in value
        }
        first, last, refs = await self._images_of(pid, job, params)

        async def upload(path: Path | None) -> str | None:
            if path is None:
                return None
            data = await asyncio.to_thread(path.read_bytes)
            return await comfy.upload_image(path.name, data)

        first_name = await upload(first)
        last_name = await upload(last)
        ref_names = [name for name in [await upload(ref.path) for ref in refs] if name]
        values = {
            "prompt": params.get("prompt"),
            "negative_prompt": params.get("negative_prompt"),
            "seed": params.get("seed"),
            "steps": params.get("steps"),
            "duration": params.get("duration"),
            "first_frame": first_name,
            "last_frame": last_name,
            "source_image": first_name or (ref_names[0] if ref_names else None),
            "reference_image": ref_names[0] if ref_names else first_name,
        }
        ref_slots = raw_bindings.get("reference_image_slots")
        if isinstance(ref_slots, list):
            for index, target in enumerate(ref_slots):
                if index >= len(ref_names) or not isinstance(target, str) or "." not in target:
                    continue
                values[f"__ref_{index}"] = ref_names[index]
                bindings[f"__ref_{index}"] = target
            if len(ref_names) > len(ref_slots):
                params["ref_notes"] = [
                    f"Workflow 只有 {len(ref_slots)} 个 AIVS_REF_* 槽位，"
                    f"已省略 {len(ref_names) - len(ref_slots)} 张参考图。"
                ]
        graph = apply_bindings(graph, bindings, values)
        prompt_id = await comfy.submit(graph, client_id=f"aivs-{pid}")

        history: dict[str, Any] = {}
        for tick in range(POLL_LIMIT):
            self._require_not_cancelled(job.id)
            history = await comfy.history(prompt_id)
            if history:
                break
            if tick % 5 == 0:
                bus.emit(
                    Channel.JOB,
                    "job.progress",
                    {"id": job.id, "progress": min(0.95, tick / 120)},
                    project_id=pid,
                )
            await asyncio.sleep(POLL_INTERVAL)
        files = outputs_of(history)
        if not files:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "ComfyUI 没有产出任何文件",
                f"prompt_id={prompt_id}，history 里没有 images / videos 输出。",
                [
                    "确认工作流末端有 SaveImage / VHS 之类的保存节点",
                    "在 ComfyUI 界面里手动跑一次同一份工作流确认能出图",
                ],
                {"raw": str(history)[:2000]},
            )
        chosen = files[-1]
        data = await comfy.download(chosen["filename"], chosen["subfolder"], chosen["type"])
        return chosen["filename"], data, workflow.id

    # --- 版本（只增不改） ---

    async def add_version(
        self,
        pid: str,
        shot_id: str,
        *,
        asset_id: str | None,
        kind: str = "video",
        workflow_id: str | None = None,
        params: dict[str, Any] | None = None,
        context_snapshot: Any = None,
        source: str = "generated",
        duration: float | None = None,
    ) -> dict[str, Any]:
        db = db_of(pid)
        await fetch(db, Shot, shot_id, "镜头")
        existing = await fetch_all(
            db, GenerationVersion, where=GenerationVersion.shot_id == shot_id
        )
        row = GenerationVersion(
            id=new_id("generation_version"),
            shot_id=shot_id,
            version_no=max((v.version_no for v in existing), default=0) + 1,
            kind=kind,
            status="done",
            asset_id=asset_id,
            workflow_id=workflow_id,
            params_json=dump_json(params or {}),
            context_json=dump_json(context_snapshot) if context_snapshot is not None else None,
            duration=duration,
            source=source,
            created_at=utc_now(),
        )
        async with db.write() as session:
            session.add(row)
            shot = await session.get(Shot, shot_id)
            if shot is not None:
                # 新版本自动成为当前版本；旧版本一条都不删
                shot.current_version_id = row.id
                shot.status = "generated"
                shot.updated_at = utc_now()
        if asset_id:
            await assets.link(pid, asset_id, "shot", shot_id, role="version")
        bus.emit(
            Channel.VERSION,
            "version.created",
            {"id": row.id, "shot_id": shot_id, "version_no": row.version_no},
            project_id=pid,
        )
        self.ensure_pump(pid)  # 上游出版本了，等末帧的任务可以走了
        return as_dict(row)

    async def list_versions(self, pid: str, shot_id: str) -> list[dict[str, Any]]:
        db = db_of(pid)
        shot = await fetch(db, Shot, shot_id, "镜头")
        rows = await fetch_all(
            db,
            GenerationVersion,
            where=GenerationVersion.shot_id == shot_id,
            order_by=GenerationVersion.version_no.desc(),
        )
        media = await self._version_media(pid, rows)
        return [
            {
                **as_dict(r),
                "is_current": r.id == shot.current_version_id,
                "params": load_json(r.params_json, {}),
                "context": load_json(r.context_json, None),
                "error": load_json(r.error_json, None),
                **media.get(r.id, _NO_MEDIA),
            }
            for r in rows
        ]

    async def _version_media(
        self, pid: str, rows: list[GenerationVersion]
    ) -> dict[str, dict[str, Any]]:
        """每个版本「能播的那一段」与「能当图显示的那一张」。

        和分镜板卡片（`story._shot_media`）同一条规矩，理由也同一个：版本的资产
        绝大多数是 `.mp4`，只回一个 `asset_id` 的话前端只能把它塞进 `<img>`，
        得到的就是一个坏图标。所以这里分成两个字段——**`video_path` 才是视频，
        `thumbnail_path` 只会是图片**，前端按哪个非空决定画 `<video>` 还是 `<img>`。

        缩略图的来源只有两种：版本本身就是图片，或者这段视频**已经抽过首帧**
        （`frames.start_frame_index`）。一张都没有时只有 `video_path`——播放器自己
        会画第一帧，**读版本轨绝不顺手起 FFmpeg**（要补抽走分镜板那个显式入口）。
        """
        wanted = {r.asset_id for r in rows if r.asset_id}
        if not wanted:
            return {}
        db = db_of(pid)
        all_assets = await fetch_all(db, Asset)
        by_id = {a.id: a for a in all_assets}
        posters = start_frame_index(all_assets)
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            asset = by_id.get(row.asset_id or "")
            if asset is None:
                out[row.id] = dict(_NO_MEDIA)
                continue
            is_video = kind_of_suffix(Path(asset.path).suffix) == "video"
            poster = posters.get(asset.id) if is_video else asset
            out[row.id] = {
                "video_path": asset.path if is_video else None,
                "thumbnail_path": poster.path if poster is not None else None,
            }
        return out

    async def set_current_version(self, pid: str, version_id: str) -> dict[str, Any]:
        db = db_of(pid)
        row = await fetch(db, GenerationVersion, version_id, "生成版本")
        async with db.write() as session:
            shot = await session.get(Shot, row.shot_id)
            if shot is not None:
                shot.current_version_id = version_id
                shot.updated_at = utc_now()
        bus.emit(
            Channel.VERSION,
            "version.current_changed",
            {"shot_id": row.shot_id, "version_id": version_id},
            project_id=pid,
        )
        self.ensure_pump(pid)
        return as_dict(row)


generation = GenerationService()
