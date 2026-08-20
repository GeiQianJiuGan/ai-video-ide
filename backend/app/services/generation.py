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
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.events.bus import Channel, bus
from app.generation.comfy.client import comfy, outputs_of
from app.persistence.models import utc_now
from app.persistence.models_gen import GenerationVersion, Job, Workflow
from app.persistence.models_story import Scene, Shot
from app.services.assets import assets
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, load_json
from app.services.context import context
from app.services.workflows import apply_bindings, parse_graph, workflows

log = get_logger("queue")

ACTIVE = ("queued", "waiting", "running")
POLL_INTERVAL = 1.0
POLL_LIMIT = 1800  # 30 分钟上限，超时也要给出结构化错误而不是永远转圈


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
    ) -> dict[str, Any]:
        db = db_of(pid)
        shot = await fetch(db, Shot, shot_id, "镜头")
        capability = kind or ("first_last_frame" if shot.prev_shot_id else "image2video")
        workflow = await workflows.resolve(pid, capability, workflow_id or shot.workflow_id)
        if check_context:
            await context.require_complete(pid, shot_id)
        snapshot = await context.snapshot(pid, shot_id)

        depends_on, wait_reason = None, None
        if shot.prev_shot_id:
            prev = await fetch(db, Shot, shot.prev_shot_id, "上游镜头")
            if not prev.current_version_id:
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
            workflow_id=workflow.id,
            params_json=dump_json(
                {
                    "prompt": shot.prompt or shot.description or "",
                    "negative_prompt": shot.negative_prompt,
                    "seed": shot.seed,
                    "steps": shot.steps,
                    "duration": shot.duration,
                    "context": snapshot,
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

    async def enqueue_scene(self, pid: str, scene_id: str, priority: int = 100) -> dict[str, Any]:
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
        queued, skipped = [], []
        for shot in shots:
            try:
                job = await self.enqueue_shot(pid, shot.id, priority=priority)
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

        for job in [j for j in jobs if j.status == "waiting"]:
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
        """提交给 ComfyUI 并等结果。这是唯一知道 ComfyUI 存在的地方。"""
        db = db_of(pid)
        workflow = await fetch(db, Workflow, job.workflow_id or "", "工作流")
        graph = parse_graph(workflow.api_json)
        bindings: dict[str, str] = load_json(workflow.bindings_json, {})
        values = {
            "prompt": params.get("prompt"),
            "negative_prompt": params.get("negative_prompt"),
            "seed": params.get("seed"),
            "steps": params.get("steps"),
            "duration": params.get("duration"),
        }
        graph = apply_bindings(graph, bindings, values)
        prompt_id = await comfy.submit(graph, client_id=f"aivs-{pid}")

        history: dict[str, Any] = {}
        for tick in range(POLL_LIMIT):
            if job.id in self._cancelled:
                raise AppError(
                    ErrorCode.WORKFLOW_ERROR,
                    "任务已取消",
                    "在等待 ComfyUI 结果时被取消。",
                    ["需要的话重新入队"],
                )
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
        kind = (
            "generated_video"
            if chosen["filename"].lower().endswith((".mp4", ".webm", ".mov", ".gif"))
            else "generated_image"
        )
        asset = await assets.register_bytes(pid, kind, chosen["filename"], data, source="generated")
        return await self.add_version(
            pid,
            job.shot_id,
            asset_id=asset["id"],
            kind="video" if kind == "generated_video" else "image",
            workflow_id=workflow.id,
            params={k: v for k, v in params.items() if k != "context"},
            context_snapshot=params.get("context"),
            source="generated",
        )

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
        return [
            {
                **as_dict(r),
                "is_current": r.id == shot.current_version_id,
                "params": load_json(r.params_json, {}),
                "context": load_json(r.context_json, None),
                "error": load_json(r.error_json, None),
            }
            for r in rows
        ]

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
