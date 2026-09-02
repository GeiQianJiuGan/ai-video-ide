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

from sqlalchemy import select

from app.core import ffmpeg as ffmpeg_tool
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.events.bus import Channel, bus
from app.generation.providers import registry
from app.generation.providers.base import (
    AudioRequest,
    ImageRequest,
    RefAsset,
    TaskState,
    VideoRequest,
    WorkflowSpec,
)
from app.persistence.models import Project, utc_now
from app.persistence.models_gen import (
    AUDIO_KINDS,
    IMAGE_KINDS,
    JOB_KINDS,
    REFINE_KINDS,
    GenerationVersion,
    Job,
)
from app.persistence.models_global import GlobalWorkflow
from app.persistence.models_story import Scene, Shot
from app.persistence.models_world import Asset
from app.services import params, route
from app.services.assets import assets, kind_of_suffix
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, load_json, project_of
from app.services.context import context
from app.services.frames import frame_key, poster_at, start_frame_index
from app.services.global_registry import global_registry
from app.services.images import images

log = get_logger("queue")

ACTIVE = ("queued", "waiting", "running")
POLL_INTERVAL = 1.0

#: 版本没有资产（失败现场、占位版本）时的媒体字段。三个键永远都在——
#: 前端按「哪个非空」决定画 `<video>` / `<img>` / `<audio>`，键时有时无就得到处写可选判断。
_NO_MEDIA: dict[str, Any] = {"video_path": None, "thumbnail_path": None, "audio_path": None}

#: 参考素材的量词：图片是「张」，视频 / 音频是「段」。只用在四要素错误的文案里。
_UNIT = {"image": "张", "video": "段", "audio": "段"}


def _unit_of(block: dict[str, Any]) -> str:
    return _UNIT.get(str(block.get("media") or "image"), "个")


def drop_entry(shot: Shot, capacity: dict[str, Any]) -> dict[str, Any] | None:
    """这个镜头会不会有参考素材喂不进去。装得下时回 None。

    `capacity` 就是账单里的那一块（`context._capacity_of`）——**能收几个由适配层回答**，
    这里不再有任何应用级上限可看。**按媒体分开报**：图片槽位与视频 / 音频槽位是三族
    不同的标题（`AIVS_REF_*` / `AIVS_REF_VIDEO_*` / `AIVS_REF_AUDIO_*`），
    混成一个数字的话「图装得下、那段音频没槽位」会被算成装得下，然后音频被安静地丢掉。
    顶层字段仍是参考图那一份（历史口径，旧版本冻结的账单没有 `media` 子块）。
    """
    if not capacity.get("over"):
        return None
    per_media = capacity.get("media") or {}
    blocks = [
        {
            "media": media,
            "label": str(block.get("label") or media),
            "ref_count": int(block.get("ref_count") or 0),
            "limit": block.get("limit"),
            "dropped": int(block.get("dropped") or 0),
            "labels": [str(x) for x in (block.get("dropped_labels") or [])],
        }
        for media, block in per_media.items()
        if isinstance(block, dict) and block.get("over")
    ]
    if not blocks:  # 旧账单：只有顶层那一份（参考图）
        blocks = [
            {
                "media": "image",
                "label": "参考图",
                "ref_count": int(capacity.get("ref_count") or 0),
                "limit": capacity.get("limit"),
                "dropped": int(capacity.get("dropped") or 0),
                "labels": [str(x) for x in (capacity.get("dropped_labels") or [])],
            }
        ]
    return {
        "shot_id": shot.id,
        "index_no": shot.index_no,
        "title": shot.title,
        "ref_count": int(capacity.get("ref_count") or 0),
        "limit": capacity.get("limit"),
        "dropped": sum(b["dropped"] for b in blocks),
        "labels": [label for b in blocks for label in b["labels"]],
        "source": str(capacity.get("source") or ""),
        #: 三种媒体里哪几族装不下，各丢几个。界面与错误文案照它逐族说清。
        "media": blocks,
    }


def over_capacity_error(drops: list[dict[str, Any]]) -> AppError:
    """「会丢几个素材，还继续吗」——**这不是失败，是一次确认**。

    所以它必须在入队**任何一个任务之前**抛出来（批量路径先整体扫一遍再动手）：
    一半已经入队、另一半等确认的话，用户点了确认就会把前一半再入队一遍。
    确认的样子是重新调一次同一个入口并带上 `allow_ref_drop=true`，
    之后按槽位顺序喂前 N 个，少喂了哪几个照旧记进 `params.ref_notes`。
    """
    total = sum(int(d["dropped"]) for d in drops)
    lines = "；".join(
        "，".join(
            f"Shot {d['index_no']} 采用了 {b['ref_count']}{_unit_of(b)}{b['label']}，"
            f"这里只能喂 {b['limit']}{_unit_of(b)}，会丢 {b['dropped']}{_unit_of(b)}"
            + (f"（{'、'.join(b['labels'])}）" if b["labels"] else "")
            for b in d.get("media") or []
        )
        for d in drops
    )
    source = next((str(d["source"]) for d in drops if d.get("source")), "")
    where = f"（能收几个由{source}决定）" if source else ""
    return AppError(
        ErrorCode.REF_OVER_CAPACITY,
        f"有 {len(drops)} 个镜头的参考素材装不下，会丢 {total} 个",
        f"{lines}{where}。丢的是账单里优先级最低的那几条——角色图最先保住，道具图最先被挤掉。",
        [
            "确认后继续：按槽位顺序喂前几个，丢掉哪几个会记进版本参数，事后查得到",
            "不想丢：在 ComfyUI 里给这份图多标几个 AIVS_REF_n 标题（视频 / 音频是"
            " AIVS_REF_VIDEO_n / AIVS_REF_AUDIO_n），重新上传预设",
            "或在上下文检查器里手动移除不重要的参考素材，自己决定丢哪几个",
        ],
        {
            "shot_ids": [str(d["shot_id"]) for d in drops],
            "dropped": total,
            #: 前端据此知道「这条错误可以确认过去」——带上这个参数再调一次同一个入口。
            "confirm": "allow_ref_drop",
        },
    )


def _batch_fields(batch: dict[str, Any] | None) -> dict[str, Any]:
    """把一次编排的身份摊成 Job 的四列。

    **没有编排就是四个空值**——单个镜头的生成不属于任何一批，队列里照旧一行一条。
    调用方给的 `seq` 允许缺（转场这种「补几段」的批次没有严格的第几步），此时界面
    按 `created_at` 的先后当步序。
    """
    if not batch or not batch.get("id"):
        return {}
    return {
        "batch_id": str(batch["id"]),
        "batch_label": str(batch.get("label") or "")[:200] or None,
        "batch_kind": str(batch.get("kind") or "")[:30] or None,
        "batch_seq": batch.get("seq"),
    }


def _provider_of(params: dict[str, Any]) -> str:
    """这个任务当时要走哪条路。**只读入队时冻结的那一份，绝不重新解析。**

    以前这里是写死的 `registry.provider("comfy_preset")`：工程选了「通用 REST API」
    也照旧提交给 ComfyUI，报出来的还是「ComfyUI 未连接」——选了等于没选，
    而冻结进版本的参数里写着用户选的那条路（破硬约束 3、4）。

    三级回退，各有各的理由：

      1. `params["route"]["provider"]`——`route.require()` 在入队那一刻解析并冻结的那一份，
         新任务都走这里。**重试不重新解析**：中途在设置页改了调用方式，「重试」就该重跑
         同一条路，而不是换个后端跑一遍（版本参数上写的还是旧那条，硬约束 3）。
      2. `params["generation_mode"]`——这一轮之前入队的老 job / 老版本。那时这一列的值是
         写死的 `comfy_preset`，但它至少是**当时真的会走的那条**，重试照旧能跑。
      3. `settings.video_provider`——参数里两个键都没有（更老的 job）。这是最后一层兜底，
         不是正常路径。

    未知名字不在这里挡：`registry.provider()` 会抛「不认识的视频调用方式」四要素错误，
    比在这儿静默换成默认那条要诚实得多。
    """
    frozen = params.get("route")
    if isinstance(frozen, dict):
        chosen = str(frozen.get("provider") or "").strip()
        if chosen:
            return chosen
    return str(params.get("generation_mode") or "").strip() or str(settings.video_provider or "")


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
        batch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        db = db_of(pid)
        shot = await fetch(db, Shot, shot_id, "镜头")
        # 幕级 prompt 是镜头没写 prompt 时的兜底（流程图上那个必填的小节点）。
        # 取值口径只有一份（`services/params.py::prompt_of`），`context.resolve` 的
        # problems 与分镜卡片上的黄色感叹号读的是同一个函数——四处各写一遍的时候，
        # 只要有一处漏了一级就会出现「账单说齐了，冻结进版本里的 prompt 却是空的」。
        scene_of_shot = await fetch(db, Scene, shot.scene_id, "场景")
        prompt = params.prompt_of(shot, scene_of_shot)
        # 直接调用旧接口时保留 prev_shot_id 的续接语义；新的批量编排会显式传
        # image2video，让所有普通 SHOT 先独立生成，再单独创建 FL2VA 衔接任务。
        # 判定只有一份实现（`route.capability_of`）——参考素材账单那侧读的是同一个函数，
        # 两处各写一遍的时候，首尾帧镜头的账单数的是 R2V 预设、提交的却是 FLF 那份图。
        capability = route.capability_of(shot, kind)
        project = (await fetch_all(db, Project))[0]
        # **入队那道门槛。** 这个工程这个能力走哪条路、这条路要绑什么、绑没绑上，只有
        # `services/route.py` 一份口径：缺地址 / 缺预设 / 没绑图在这里就是四要素错误，
        # 而不是排进队列再由 pump 一条条失败（那时用户看到的只剩「ComfyUI 未连接」这种
        # 与真正原因差一层的话）。解析出来的这一份下面会整个冻结进 `params["route"]`。
        chosen = await route.require(
            pid, capability, project=project, workflow_id=workflow_id or shot.workflow_id
        )
        use_prev_frame = capability in route.FLF_CAPABILITIES
        if check_context:
            await context.require_complete(pid, shot_id, include_prev=use_prev_frame)
        snapshot = await context.snapshot(
            pid, shot_id, include_prev=use_prev_frame, capability=capability
        )
        # 参考图比模型端那份图能收的多时先问一句。**不是失败**：确认（allow_ref_drop）后
        # 照旧生成，只是按槽位顺序喂前几张。悄悄少喂两张图，事后没人查得出形象为什么跑偏。
        if not allow_ref_drop:
            over = drop_entry(shot, snapshot.get("capacity") or {})
            if over:
                raise over_capacity_error([over])

        depends_on, wait_reason = None, None
        if shot.prev_shot_id and capability in route.FLF_CAPABILITIES:
            prev = await fetch(db, Shot, shot.prev_shot_id, "上游镜头")
            if wait_for_job_id:
                # 单线程续接等待的是本次编排中的上一条任务，而不是旧的当前版本。
                # 否则上游已有旧版本时，下游会提前并发启动，拿不到本次生成的真末帧。
                depends_on = prev.id
                wait_reason = f"等待上游 Shot {prev.index_no} 完成本次生成（需要末帧）"
            elif not prev.current_version_id:
                depends_on = prev.id
                wait_reason = f"等待上游 Shot {prev.index_no} 完成（需要末帧）"

        # 这个镜头是从长视频切段来的吗？是的话把「用哪一段」在入队这一刻就冻结下来。
        # 不能等到执行时再去问 `shot.current_version_id`：新版本入库即成为当前版本，
        # 第二次生成就会拿上一次的产物（整段、没有区间）当输入——那正是「传进去的片段
        # 明显不对」的由来。
        segment_version_id = await self._segment_version_id(pid, shot_id)

        row = Job(
            id=new_id("job"),
            shot_id=shot_id,
            kind=capability,
            status="waiting" if depends_on else "queued",
            priority=priority,
            depends_on=depends_on,
            wait_reason=wait_reason,
            #: 绑定那条路才有值（那份图的 id）。**执行时的装配条件是「这个任务有绑定的图」**，
            #: 不是「这条路叫什么名字」——硬约束 1 不许业务层认路。
            workflow_id=chosen.workflow_id,
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
                    #: 兼容旧读法（老 job / 老版本参数里就这一个键）。值第一次是真的：
                    #: 以前这里恒等于 `comfy_preset`，用户在界面上选的那条根本没被读过。
                    "generation_mode": chosen.provider,
                    "preset": chosen.preset,
                    #: **冻结的那一份路由**（硬约束 3）：重试只读它，绝不重新解析——否则中途
                    #: 改了设置会让「重试」变成「换个后端跑一遍」，而版本上写的还是旧那条。
                    "route": chosen.frozen(),
                    #: 「这个镜头的画面素材是哪一段」。长视频切段的镜头才有值。
                    "source_version_id": segment_version_id,
                }
            ),
            created_at=utc_now(),
            **_batch_fields(batch),
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

    async def _segment_version_id(self, pid: str, shot_id: str) -> str | None:
        """这个镜头「从长视频里切来的那一段」是哪一版（没有就是 None）。

        认的是**导入进来的那一版**（`source == "imported"` 且带区间）：一个镜头后来可能
        生成过很多版，那些产物都没有区间，也都不是这个镜头的素材来源。

        顺序是「当前采用的那一版（如果它就是导入的那一段）→ 否则最新的那一条导入版本」。
        拆分过的镜头（`story.split_shot`，只增不改）会有两条导入版本：原来那条整段的
        + 拆完之后本段的，取最新那条才是「现在这个镜头对应的那一段」。
        """
        db = db_of(pid)
        shot = await fetch(db, Shot, shot_id, "镜头")
        rows = await fetch_all(db, GenerationVersion, where=GenerationVersion.shot_id == shot_id)
        mine = [
            v
            for v in rows
            if v.source == "imported" and v.in_point is not None and v.out_point is not None
        ]
        if not mine:
            return None
        current = next((v for v in mine if v.id == shot.current_version_id), None)
        if current is not None:
            return current.id
        mine.sort(key=lambda v: v.version_no)
        return mine[-1].id

    async def enqueue_task(
        self,
        pid: str,
        shot_id: str,
        *,
        kind: str,
        priority: int = 100,
        params: dict[str, Any] | None = None,
        batch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """入队一条**不出新画面**的任务：二次处理（`REFINE_KINDS`）或出声音（`AUDIO_KINDS`）。

        与 `enqueue_shot` 分开，因为那一套前置检查在这里全部答非所问：上下文完整性
        （画面已经有了）、参考素材装不装得下（这条路不喂参考素材）、上游末帧依赖
        （不出新画面就不必等谁的末帧）。共用的是后半程——同一张 `job` 表、同一个 pump、
        同一套取消 / 重试 / 优先级、同一个 `add_version`，所以队列面板与版本轨一行不用改。
        """
        if kind not in REFINE_KINDS and kind not in AUDIO_KINDS:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这不是一种二次处理或音源任务",
                f"kind={kind!r} 不在 {'、'.join((*REFINE_KINDS, *AUDIO_KINDS))} 里。",
                ["出新画面请走镜头生成入口", f"可用的 kind：{'、'.join(JOB_KINDS)}"],
                {"kind": kind},
            )
        db = db_of(pid)
        await fetch(db, Shot, shot_id, "镜头")
        row = Job(
            id=new_id("job"),
            shot_id=shot_id,
            kind=kind,
            status="queued",
            priority=priority,
            params_json=dump_json(params or {}),
            created_at=utc_now(),
            **_batch_fields(batch),
        )
        async with db.write() as session:
            session.add(row)
        bus.emit(
            Channel.QUEUE,
            "job.enqueued",
            {"id": row.id, "shot_id": shot_id, "status": row.status, "kind": kind},
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
        scene = await fetch(db, Scene, scene_id, "场景")
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
        # 整幕生成也是「一次编排」：队列里合并成一条可展开的任务，
        # 只有一个镜头时不建批次（为一条任务画一个壳只是多一层）。
        batch_id = new_id("job_batch") if len(shots) > 1 else None
        label = f"整幕生成 · 第 {scene.index_no} 幕 · {len(shots)} 个镜头"
        for i, shot in enumerate(shots):
            try:
                job = await self.enqueue_shot(
                    pid,
                    shot.id,
                    priority=priority,
                    allow_ref_drop=allow_ref_drop,
                    batch=(
                        {"id": batch_id, "label": label, "kind": "scene", "seq": i + 1}
                        if batch_id
                        else None
                    ),
                )
                queued.append(job["id"])
            except AppError as err:
                skipped.append(
                    {"shot_id": shot.id, "index_no": shot.index_no, "error": err.to_dict()}
                )
        return {"queued": queued, "skipped": skipped, "total": len(shots), "batch_id": batch_id}

    # --- 队列视图与控制 ---

    async def list_jobs(self, pid: str, status: str | None = None) -> list[dict[str, Any]]:
        db = db_of(pid)
        rows = await fetch_all(db, Job, order_by=Job.created_at)
        shots = {s.id: s for s in await fetch_all(db, Shot)}
        # 图片任务不属于任何镜头（`shot_id` 是空的），所以它在控制台里要有自己的那句话
        # 「角色 · 阿岚 · 默认形象 四视图」——否则整行都是空白，看不出这是在生成什么。
        # 口径只有一份（`services/images.py::target_label`），认不出来时回 None，不抛。
        labels: dict[tuple[str, str], str | None] = {}
        out = []
        for row in rows:
            if status and row.status != status:
                continue
            shot = shots.get(row.shot_id)
            key = (row.target_kind or "", row.target_id or "")
            if key != ("", "") and key not in labels:
                labels[key] = await images.target_label(pid, row.target_kind, row.target_id)
            out.append(
                {
                    **as_dict(row),
                    "shot_index_no": shot.index_no if shot else None,
                    "shot_title": shot.title if shot else None,
                    "target_label": labels.get(key),
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
            "batches": self.batches_of(jobs),
        }

    def batches_of(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把一次编排入队的那一批任务折成一条。**纯计算，不读库。**

        为什么不存一张 batch 表：一次编排没有任何独立于成员任务的状态——总数、走到第几步、
        失败在哪一条，全部是成员算出来的。存第二份只会多一个可能和任务表对不上的真相。

        进度是**第 N/M 步**，不是百分比：ComfyUI 不回显进度，那个百分比是本地按秒数编的
        （`_await_task` 里的假爬升），把它画成进度条等于拿编出来的数字骗人。这里只说
        「12 步里做完了 3 步」——这句话是真的。

        `status` 是这一批的聚合结论，按「有没有还没了结的」优先，因为用户问的是
        「我还要不要等」：running / waiting / queued → 还在跑；一条都不剩时 failed >
        canceled > done。
        """
        groups: dict[str, list[dict[str, Any]]] = {}
        for job in jobs:
            bid = job.get("batch_id")
            if bid:
                groups.setdefault(str(bid), []).append(job)
        out: list[dict[str, Any]] = []
        for bid, members in groups.items():
            members.sort(key=lambda j: (j.get("batch_seq") or 0, str(j["created_at"])))
            counts: dict[str, int] = {}
            for m in members:
                counts[m["status"]] = counts.get(m["status"], 0) + 1
            settled = sum(counts.get(s, 0) for s in ("done", "failed", "canceled"))
            if counts.get("running"):
                status = "running"
            elif counts.get("waiting") or counts.get("queued") or counts.get("paused"):
                status = "queued"
            elif counts.get("failed"):
                status = "failed"
            elif counts.get("canceled"):
                status = "canceled"
            else:
                status = "done"
            running = next((m for m in members if m["status"] == "running"), None)
            failed = [m for m in members if m["status"] == "failed"]
            first = members[0]
            finished = [str(m["finished_at"]) for m in members if m.get("finished_at")]
            out.append(
                {
                    "id": bid,
                    "label": first.get("batch_label") or "一批生成任务",
                    "kind": first.get("batch_kind") or "",
                    "total": len(members),
                    "counts": counts,
                    "status": status,
                    #: 做完了几步（含失败与取消——它们也不会再动了）
                    "settled": settled,
                    #: 正在做第几步（1 起）。没有正在跑的就是已了结的条数，
                    #: 于是界面上那句「第 N/M 步」在跑完之后停在 M 上，而不是回到 0。
                    "step": (members.index(running) + 1) if running else settled,
                    "running_job_id": running["id"] if running else None,
                    "running_label": (
                        f"{running.get('shot_index_no') or '?'}. "
                        f"{running.get('shot_title') or running['shot_id']}"
                        if running
                        else None
                    ),
                    #: 第一条失败的四要素直接给出来：合并之后不展开也得看得见失败原因
                    "error": failed[0]["error"] if failed else None,
                    "failed_count": len(failed),
                    #: 有失败或被取消的成员才能整批重跑；已完成的成员一条都不会重做
                    "retryable": bool(counts.get("failed") or counts.get("canceled")),
                    "job_ids": [m["id"] for m in members],
                    "created_at": first["created_at"],
                    "finished_at": max(finished) if finished and status != "running" else None,
                }
            )
        out.sort(key=lambda b: str(b["created_at"]), reverse=True)
        return out

    async def retry_batch(self, pid: str, batch_id: str) -> dict[str, Any]:
        """整批重跑：把这一批里失败 / 已取消的成员重新排上去。

        单线程续接一条失败会连带停掉后面全部（`_claim` 里的 `UPSTREAM_NOT_READY`），
        于是「重跑」必须是一次动作而不是几十次点击——这就是那一次动作。

        两条刻意的规矩：
          · **已完成的成员一条都不重做**——版本永不覆盖，重跑它只会凭空多一版；
          · **等上游的仍然回到 waiting**——链条的先后是这一批的意义所在，全部塞成
            queued 会让它们一拥而上，下游拿不到上游这次的真末帧。
        """
        db = db_of(pid)
        rows = [j for j in await fetch_all(db, Job) if j.batch_id == batch_id]
        if not rows:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "找不到这一批任务",
                f"batch_id={batch_id} 下面一条任务都没有（可能已经被清理掉了）。",
                ["刷新任务框后重试", "或重新执行一次编排"],
                {"batch_id": batch_id},
            )
        targets = [r for r in rows if r.status in ("failed", "canceled")]
        if not targets:
            raise AppError(
                ErrorCode.CONFLICT,
                "这一批没有需要重跑的任务",
                "成员里没有失败或已取消的任务；已经完成的不会重做（版本永不覆盖）。",
                ["想再生成一版请重新执行一次编排", "或在版本轨上挑一版已有的成片"],
                {"batch_id": batch_id},
            )
        retried: list[str] = []
        for row in targets:
            params = load_json(row.params_json, {})
            waits = bool(params.get("wait_for_job_id")) or bool(row.depends_on)
            self._cancelled.discard(row.id)
            await self._set(
                pid,
                row.id,
                status="waiting" if waits else "queued",
                error_json=None,
                progress=0.0,
                finished_at=None,
                started_at=None,
                wait_reason="等待这一批里上一条任务完成（需要末帧）" if waits else None,
            )
            retried.append(row.id)
        bus.emit(
            Channel.QUEUE,
            "queue.batch_retried",
            {"batch_id": batch_id, "count": len(retried)},
            project_id=pid,
        )
        self.ensure_pump(pid)
        return {"batch_id": batch_id, "retried": retried, "count": len(retried)}

    async def cancel_batch(self, pid: str, batch_id: str) -> dict[str, Any]:
        """整批取消：这一批里还没了结的成员一起停。已经出的版本一条都不动。"""
        db = db_of(pid)
        rows = [j for j in await fetch_all(db, Job) if j.batch_id == batch_id]
        if not rows:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "找不到这一批任务",
                f"batch_id={batch_id} 下面一条任务都没有。",
                ["刷新任务框后重试"],
                {"batch_id": batch_id},
            )
        cancelled: list[str] = []
        for row in [r for r in rows if r.status in ACTIVE]:
            self._cancelled.add(row.id)
            await self._set(pid, row.id, status="canceled", finished_at=utc_now())
            cancelled.append(row.id)
        bus.emit(
            Channel.QUEUE,
            "queue.batch_cancelled",
            {"batch_id": batch_id, "count": len(cancelled)},
            project_id=pid,
        )
        return {"batch_id": batch_id, "cancelled": cancelled, "count": len(cancelled)}

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

    async def cancel_all(self, pid: str) -> dict[str, Any]:
        """一键取消当前项目中所有进行中/排队中/等待中的任务。"""
        jobs = [j for j in await self.list_jobs(pid) if j["status"] in ACTIVE]
        cancelled_ids: list[str] = []
        for j in jobs:
            job_id = str(j["id"])
            self._cancelled.add(job_id)
            await self._set(pid, job_id, status="canceled", finished_at=utc_now())
            cancelled_ids.append(job_id)
        bus.emit(
            Channel.QUEUE, "queue.cancelled_all", {"count": len(cancelled_ids)}, project_id=pid
        )
        return {"cancelled": cancelled_ids, "count": len(cancelled_ids)}

    async def clear_failed(self, pid: str) -> dict[str, Any]:
        """清理所有失败的任务记录。"""
        db = db_of(pid)
        count = 0
        async with db.write() as session:
            stmt = select(Job).where(Job.status == "failed")
            failed_jobs = (await session.scalars(stmt)).all()
            count = len(failed_jobs)
            for job in failed_jobs:
                await session.delete(job)
        bus.emit(Channel.QUEUE, "queue.cleared_failed", {"count": count}, project_id=pid)
        return {"cleared": count}

    async def delete_job(self, pid: str, job_id: str) -> dict[str, Any]:
        """删除一条已结束（已完成/失败/已取消）的任务记录。"""
        db = db_of(pid)
        row = await fetch(db, Job, job_id, "任务")
        if row.status in ACTIVE:
            raise AppError(
                ErrorCode.CONFLICT,
                "正在运行或排队中的任务不能直接删除",
                f"当前状态是 {row.status}，请先取消任务后再删除记录。",
                ["先点击取消", "或等待任务执行完成"],
                {"job_id": job_id},
            )
        async with db.write() as session:
            job_row = await session.get(Job, job_id)
            if job_row is not None:
                await session.delete(job_row)
        bus.emit(Channel.QUEUE, "queue.job_deleted", {"id": job_id}, project_id=pid)
        return {"deleted": job_id}

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
                if (
                    upstream_job is not None
                    and upstream_job.status == "done"
                    and upstream_job.version_id
                ):
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

        四条路，**按 `job.kind` 分**（`models_gen.JOB_KINDS` 是唯一那张表）：
          · 出图（`IMAGE_KINDS`）——图片素材那条链，产出的是一张参考图而不是版本；
          · 出声音（`AUDIO_KINDS`）——音源那条链，产出 `kind="audio"` 的版本，画面不重跑；
          · 二次处理（`REFINE_KINDS`）——输入是已经出好的那一版，产出同一个镜头上的新版本
            并记下 `parent_version_id`；
          · 出画面（默认）——走生成适配层（`app/generation/providers/*`）。

        **出画面只有一支了。** 以前这里多一支 `elif job.workflow_id: _run_legacy`：工作流
        绑定那条路长在 service 层里，靠那一列非空来触发——而它从来没被写过值，于是整支是
        死代码（界面上选了「ComfyUI 工作流绑定」等于什么都没选）。那条路现在是
        `providers/comfy_workflow.py`，与另外两条一样由 `registry.provider()` 挑出来，
        业务层不再认识它（硬约束 1）。

        产物登记与 `add_version` 四条路完全共用。
        """
        if job.kind in IMAGE_KINDS:
            return await self._run_image(pid, job, params)
        if job.kind in AUDIO_KINDS:
            return await self._run_audio(pid, job, params)
        parent_version_id: str | None = None
        source = "generated"
        if job.kind in REFINE_KINDS:
            filename, data, workflow_id = await self._run_refine(pid, job, params)
            parent_version_id = str(params.get("source_version_id") or "") or None
            source = "refined"
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
            source=source,
            parent_version_id=parent_version_id,
        )

    async def _run_provider(
        self, pid: str, job: Job, params: dict[str, Any]
    ) -> tuple[str, bytes, str | None]:
        """默认路径：把请求交给适配器，轮询到出片。service 层不认识任何具体模型。

        **走哪条路只读入队时冻结的那一份**（`_provider_of`），不重新解析（硬约束 3）：
        中途在设置页改了调用方式，「重试」就不该变成「换个后端跑一遍」。
        """
        provider = registry.provider(_provider_of(params))
        spec = await self._workflow_spec_of(job, params)
        first, last, refs = await self._images_of(pid, job, params)
        source_video, version = await self._source_video_of(pid, params, shot_id=job.shot_id)
        if source_video is not None and not any(r.media == "video" for r in refs):
            refs.append(
                RefAsset(
                    path=source_video,
                    label="分镜视频切段",
                    kind="source_video",
                    media="video",
                )
            )
        mode = (
            "flf"
            if job.kind in {"first_last_frame", "transition", "fl2va"} or last is not None
            else "i2v"
        )
        if mode == "i2v" and first is None and not refs and source_video is None:
            raise AppError(
                ErrorCode.MISSING_INPUT,
                "这个镜头既没有首帧也没有参考素材",
                "本轮只做图生视频（R2V）：要么给它一张首帧，要么至少给一条参考素材"
                "（角色图 / 场景图 / 参考视频 / 对白音频），否则只有提示词，出来的画面与本片无关。",
                [
                    "在镜头编辑器的「首帧」槽位里挑一张图（画面的第一格就是它）",
                    "或给这一幕 / 这个镜头挂上人物与地点，账单会把它们当参考素材喂进去",
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
            source_video=source_video,
            refs=refs,
            duration=float(
                params.get("duration") or (version.duration if version else None) or 4.0
            ),
            seed=params.get("seed"),
            workflow=spec,
            extra={**(params.get("extra") or {}), "preset": params.get("preset")},
        )
        task_id = await provider.submit(req, client_id=f"aivs-{pid}")
        # 冻结「实际喂了哪几个参考素材」与适配器的降级说明。`_execute` 在这之后才收集
        # params，所以这里改它就会跟着版本一起存下来——账单说要喂 5 个、图只收了 3 个，
        # 事后必须在版本里看得见这件事（绝不静默失败）。
        params["refs"] = [
            {
                "label": r.label,
                "kind": r.kind,
                "media": r.media,
                "file": r.path.name,
                #: 当时喂进 prompt 的那句说明（全文，`ref_hint` 里才截断）。素材描述
                #: 事后能改，所以「这一版到底带了哪句话」只有冻结下来才回答得了。
                "desc": r.desc,
            }
            for r in refs
        ]

        if req.notes:
            params["ref_notes"] = list(req.notes)
        filename, data = await self._await_task(pid, job, provider, task_id)
        return filename, data, spec.id if spec is not None else None

    async def _workflow_spec_of(self, job: Job, params: dict[str, Any]) -> WorkflowSpec | None:
        """这个任务绑了哪份图（绑了才取）。**装配条件是事实而不是名字**（硬约束 1）。

        判的是「`job.workflow_id` 非空」而不是「`if provider == "comfy_workflow"`」：不吃这个
        字段的适配器忽略它就行，业务层从此不认识哪条路要绑图。id 是入队时解析并冻结的
        （`route.require()` → `Route.workflow_id`），所以这里只按 id 取一次，
        **不重新解析绑定**——中途改了绑定表不该让「重试」换一份图。

        图与绑定表刻意**不进 `params_json`**（`WorkflowSpec` 那段注释说的就是这件事）：
        一份 api_json 动辄几十 KB，每个版本存一份会把工程库撑起来。冻结的是 id。
        """
        wid = str(job.workflow_id or (params.get("route") or {}).get("workflow_id") or "")
        if not wid:
            return None
        row = await fetch(await global_registry.start(), GlobalWorkflow, wid, "工作流")
        return WorkflowSpec(
            id=row.id,
            name=row.name,
            api_json=row.api_json,
            bindings=load_json(row.bindings_json, {}),
        )

    async def _await_task(
        self, pid: str, job: Job, provider: Any, task_id: str
    ) -> tuple[str, bytes]:
        """轮询到出片，然后把产物取回来。

        **出画面、二次处理、出声音共用这一份**：三条链的适配器形状完全一样
        （`submit` / `poll` / `fetch`），各写一遍轮询只会在「取消怎么响应」「失败怎么翻译」
        上分叉——而那正是「绝不静默失败」最容易破功的地方。
        """
        state = TaskState("queued")
        tick = 0
        while True:
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
            tick += 1
        return await provider.fetch(task_id)

    # --- 二次处理（输入是已经出好的那一版）---

    async def _ensure_slice(
        self, proj_dir: Path, src_path: Path, in_point: float, out_point: float, version_id: str
    ) -> Path:
        """从长视频里按 [in_point, out_point] 切出这一段专属的文件喂给模型端。

        两条规矩：

          · 落 `cache/slices/`（可再生的临时文件，与抽出来的首尾帧同一个待遇），
            **不进 `assets/`**——它不是工程资产，也不该出现在资产总账里；
          · **切不出来就报错**。以前这里在 FFmpeg 失败时 `return src_path`，于是模型端
            收到的是**整段长视频**：既不报错也看不出来，用户只会发现「传进去的片段明显不对」。
            切段是这条链的全部意义，切不出来必须停下来说话（绝不静默失败）。
        """
        out_dir = proj_dir / "cache" / "slices"
        out_dir.mkdir(parents=True, exist_ok=True)
        dur = round(max(0.1, out_point - in_point), 3)
        slice_target = out_dir / f"slice_{version_id}_{in_point:.2f}_{out_point:.2f}.mp4"
        if slice_target.is_file() and slice_target.stat().st_size > 0:
            return slice_target

        binary = ffmpeg_tool.require("ffmpeg")
        cmd = [
            binary,
            "-y",
            "-ss",
            f"{in_point:.3f}",
            "-i",
            str(src_path),
            "-t",
            f"{dur:.3f}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-avoid_negative_ts",
            "make_zero",
            str(slice_target),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        raw = (stderr or b"").decode("utf-8", "replace")
        ok = proc.returncode == 0 and slice_target.is_file() and slice_target.stat().st_size > 0
        if not ok:
            slice_target.unlink(missing_ok=True)  # 半成品必须删掉，否则下次会被当成切好的
            log.warning("slice.extract_failed", version=version_id, stderr=raw[-500:])
            raise AppError(
                ErrorCode.FFMPEG_ERROR,
                "切不出这一段视频",
                f"FFmpeg 退出码 {proc.returncode}："
                f"{src_path.name} 的 {in_point:.2f}s ~ {out_point:.2f}s 没能切出来。"
                "这一段不会拿整段长视频去顶替——那样出来的画面与本镜头无关。",
                [
                    "确认源视频还在磁盘上且能被 FFmpeg 读取（播放器里能拖到这个位置）",
                    "在设置页看一眼用的是哪一份 FFmpeg",
                    "或重新导入这段长视频（导入时让它复制进工程）",
                ],
                {"version_id": version_id, "raw": raw[-2000:]},
            )
        return slice_target

    async def _source_video_of(
        self,
        pid: str,
        params: dict[str, Any],
        shot_id: str | None = None,
        *,
        fallback_current: bool = False,
    ) -> tuple[Path | None, Any]:
        """要处理的那一段画面在磁盘上的位置 + 它那一版（带区间时自动切出本段）。

        `fallback_current` 只有二次处理那条链会开：它的输入本来就是「这个镜头现在采用的
        那一版」。**出画面那条链绝不能退回 `shot.current_version_id`**——新版本入库时会
        自动成为当前版本（硬约束 3 那扇门），于是「重新生成一次」拿到的就是上一次的产物
        （整段、没有区间），而不是导入时那一段。那正是「删了几个分镜再生成，传进去的片段
        明显不对」的由来。出画面要用哪一段由 `enqueue_shot` 在入队时冻结进
        `params.source_version_id`。
        """
        db = db_of(pid)
        version_id = str(params.get("source_version_id") or "")
        if not version_id and shot_id and fallback_current:
            shot = await fetch(db, Shot, shot_id, "镜头")
            version_id = str(shot.current_version_id or "")
        if not version_id:
            return None, None
        version = await fetch(db, GenerationVersion, version_id, "要处理的版本")
        if not version or not version.asset_id:
            return None, None
        asset = await fetch(db, Asset, version.asset_id, "资产")
        path = project_of(pid).dir / asset.path
        if not await asyncio.to_thread(path.is_file):
            return None, None
        # 若带有区间信息（从长视频切段而来），按区间提取出当前镜头的切片分段
        if version.in_point is not None and version.out_point is not None:
            slice_path = await self._ensure_slice(
                project_of(pid).dir, path, version.in_point, version.out_point, version_id
            )
            return slice_path, version
        return path, version

    async def _run_refine(
        self, pid: str, job: Job, params: dict[str, Any]
    ) -> tuple[str, bytes, str | None]:
        """二次处理：**画面不重生成，只把已经出好的那一段再过一遍图。**

        输入是 `source_version_id` 那一版的文件，填进预设的 `AIVS_SOURCE_VIDEO`
        （与参考视频严格分开，见 `providers/presets.py`）。产出仍然是同一个镜头上的一个新版本，
        于是「只增不改」「随时回退到未处理那一版」「采用入口只有一个」全都一行不用改。

        走哪条路同样只读冻结的那一份：REST 那条路上二次处理是合同里的 `source_video`
        （`providers/http_api.py`），预设那条路上是 `AIVS_SOURCE_VIDEO` 那个入口——
        service 层两条都不认识。
        """
        provider = registry.provider(_provider_of(params))
        source, version = await self._source_video_of(
            pid, params, shot_id=job.shot_id, fallback_current=True
        )
        if not source or not version:
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "要处理的那段视频不存在",
                "找不到这个镜头已采用的视频版本，无法做二次处理。",
                ["确认该镜头已有视频版本", "或先生成一次该镜头"],
                {"shot_id": job.shot_id},
            )
        req = VideoRequest(
            mode="refine",
            prompt=str(params.get("prompt") or ""),
            negative=str(params.get("negative_prompt") or ""),
            source_video=source,
            duration=float(params.get("duration") or version.duration or 4.0),
            seed=params.get("seed"),
            extra={**(params.get("extra") or {}), "preset": params.get("preset")},
        )
        task_id = await provider.submit(req, client_id=f"aivs-{pid}")
        if req.notes:
            params["ref_notes"] = list(req.notes)
        filename, data = await self._await_task(pid, job, provider, task_id)
        return filename, data, None

    # --- 音源（同一个镜头上的另一版，画面一个字节都不重跑）---

    async def _run_audio(self, pid: str, job: Job, params: dict[str, Any]) -> dict[str, Any]:
        """出声音。产出 `kind="audio"` 的版本并**自动成为这个镜头采用的那条音轨**。

        与出画面完全同构（提交 → 轮询 → 取回 → 登记成版本），只有三处不同：
        另一个适配器（`registry.audio_provider()`）、另一种版本 kind、另一个采用指针
        （`Shot.current_audio_version_id`，落在 `add_version` 里）。
        """
        provider = registry.audio_provider()
        db = db_of(pid)
        shot = await fetch(db, Shot, job.shot_id, "镜头")
        voice_ref = await self._asset_path(pid, params.get("voice_ref_asset_id"))
        source_video: Path | None = None
        if params.get("source_version_id"):
            source_video, _ = await self._source_video_of(pid, params)
        req = AudioRequest(
            text=str(params.get("text") or ""),
            prompt=str(params.get("prompt") or ""),
            negative=str(params.get("negative_prompt") or ""),
            voice_ref=voice_ref,
            source_video=source_video,
            duration=float(params.get("duration") or shot.duration or 4.0),
            seed=params.get("seed"),
            extra={**(params.get("extra") or {}), "preset": params.get("preset")},
        )
        filename, data = await self._await_task(
            pid, job, provider, await provider.submit(req, client_id=f"aivs-{pid}")
        )
        if req.notes:
            params["ref_notes"] = list(req.notes)
        asset = await assets.register_bytes(
            pid, "generated_audio", filename, data, source="generated"
        )
        return await self.add_version(
            pid,
            job.shot_id,
            asset_id=asset["id"],
            kind="audio",
            params={k: v for k, v in params.items() if k != "context"},
            source="generated",
            duration=req.duration,
            parent_version_id=str(params.get("source_version_id") or "") or None,
        )

    # --- 图片素材（不属于任何镜头，产出的是一张参考图而不是版本）---

    async def _run_image(self, pid: str, job: Job, params: dict[str, Any]) -> dict[str, Any]:
        """出一张参考图（角色四视图 / 地点参考图 / 道具图 / 镜头首尾帧候选）。

        与出画面、出声音**同构**（提交 → 轮询 → 取回），三处不同：另一个适配器
        （`registry.image_provider()`）、另一条落地路径（`images.land()` 转调
        `cast.add_sheet` / `world.add_*_reference`，都是 append-only）、
        **不写 `GenerationVersion`**——素材图的「永不覆盖」由那几张表自己的
        `version_no` + `is_current` 保证，再存一份版本只会多一个可能对不上的真相。

        prompt 与负向 prompt 是**入队那一刻就拼好冻结**的（`services/images.py`），
        这里一个字都不再拼：SKILL 之后改了，已经排在队列里的这一张也不该变样。
        """
        provider = registry.image_provider()
        refs = await images.refs_of(pid, params)
        req = ImageRequest(
            prompt=str(params.get("prompt") or ""),
            negative=str(params.get("negative_prompt") or ""),
            size=str(params.get("size") or settings.image_size),
            refs=refs,
            seed=params.get("seed"),
            extra={**(params.get("extra") or {}), "preset": params.get("preset")},
        )
        filename, data = await self._await_task(
            pid, job, provider, await provider.submit(req, client_id=f"aivs-{pid}")
        )
        # 适配层的降级说明（参考图装不下、这个端收不了参考图）要留在任务上——
        # 少喂了几张图这件事必须看得见（绝不静默失败）。
        if req.notes:
            params["ref_notes"] = list(req.notes)
            await self._set(pid, job.id, params_json=dump_json(params))
        return await images.land(pid, job, filename, data)

    async def _asset_path(self, pid: str, asset_id: Any) -> Path | None:
        if not asset_id:
            return None
        asset = await fetch(db_of(pid), Asset, str(asset_id), "资产")
        return project_of(pid).dir / asset.path

    async def _images_of(
        self, pid: str, job: Job, params: dict[str, Any]
    ) -> tuple[Path | None, Path | None, list[RefAsset]]:
        """定首帧、末帧，以及**首尾帧之外的那些参考素材**（图 / 视频 / 音频）。

        这里只挑「哪几个、谁是谁」，不管模型怎么用它们——差异在适配器里。
        「哪一张当首帧」这条规则不在这里，在 `services/context.py::_assign_roles`
        （账单上标的 `role`）：两边各挑一次的话，检查器里标的和真正喂进去的会分叉。

        首尾帧的来源只有三处，按这个顺序：入队时显式传的（编排路径）→ 账单上标了
        `first_frame` / `last_frame` 的那一条（镜头上指定的槽位，或要续接时的上游末帧）→
        镜头上那两列（账单冻结之后才指定的情况）。**到此为止**——都没有就是这个镜头
        没有首帧，绝不像以前那样把优先级最高的参考图顶上来当第一格画面。

        剩下的采用条目全部当参考素材带走，**这就是「角色/场景真正被引入」的那一步**——
        以前它们算进了账单却在这里被丢掉，于是只剩一张首帧，人物形象自然跑偏。
        显式指定的首/末帧如果本身就在账单里，不会再重复当一次参考素材。
        每一条带上 `media`（只看后缀），适配器照它分流到 `AIVS_REF_*` /
        `AIVS_REF_VIDEO_*` / `AIVS_REF_AUDIO_*`——一段 `.mp4` 填进 LoadImage 不出片。

        有一处不能用入队时冻结的那份账单：**要接上游末帧的镜头**。那张图在入队的时刻
        还不存在（上游还没出片），所以对这种镜头在真正要跑的时候重新结一次账并按需抽帧
        ——否则会拿一段视频、或者随便一张角色表去当首帧。
        """
        db = db_of(pid)
        proj = project_of(pid)
        shot = await fetch(db, Shot, job.shot_id, "镜头")
        explicit_first = params.get("first_frame_asset_id")
        explicit_last = params.get("last_frame_asset_id")
        included = [i for i in (params.get("context") or {}).get("included") or []]
        use_prev_frame = job.kind in {"first_last_frame", "transition", "fl2va"}
        if use_prev_frame and shot.prev_shot_id and not explicit_first:
            fresh = await context.ensure_frames(pid, job.shot_id, include_prev=True)
            included = [i for i in fresh["items"] if i.get("included")]
        usable = [i for i in included if i.get("asset_id")]

        def by_role(role: str, kinds: tuple[str, ...]) -> str | None:
            hit = next((i for i in usable if i.get("role") == role), None)
            if hit is None:  # 旧版本冻结的账单里没有 role，只认得出上游末帧那一条
                hit = next((i for i in usable if i.get("kind") in kinds), None)
            return (hit or {}).get("asset_id")

        explicit_first = (
            explicit_first
            or by_role("first_frame", ("first_frame", "prev_frame"))
            or shot.first_frame_asset_id
        )
        explicit_last = explicit_last or by_role("last_frame", ("last_frame",))
        explicit_last = explicit_last or shot.last_frame_asset_id
        spent = {explicit_first, explicit_last} - {None, ""}

        async def resolve(asset_id: str | None) -> Path | None:
            if not asset_id:
                return None
            asset = await fetch(db, Asset, asset_id, "资产")
            return proj.dir / asset.path

        refs: list[RefAsset] = []
        for item in usable:
            asset_id = str(item["asset_id"])
            if asset_id in spent:
                continue
            spent.add(asset_id)  # 同一份素材在账单里出现两次时只喂一次
            path = await resolve(asset_id)
            if path is None:
                continue
            media = kind_of_suffix(path.suffix)
            if media == "other":
                continue  # 认不出后缀的不喂：填进哪个槽位都是错的（账单也没采用它）
            refs.append(
                RefAsset(
                    path=path,
                    label=str(item.get("label") or ""),
                    kind=str(item["kind"]),
                    media=media,
                    #: 那句「长什么样」，来自账单（`context._desc_of`）。它最终由
                    #: `providers/base.py::ref_hint()` 渲染进 prompt——空就只剩一个名字。
                    desc=str(item.get("desc") or ""),
                )
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
        parent_version_id: str | None = None,
        in_point: float | None = None,
        out_point: float | None = None,
    ) -> dict[str, Any]:
        """记一版。**只增不改**（硬约束 3），三件事按 `kind` 分岔：

        · `kind="audio"` 落的是**第二个指针** `Shot.current_audio_version_id`，
          绝不碰 `current_version_id` / `status`——「换一条音轨」不该让画面那一版易主，
          也不该把一个还没出画面的镜头标成 `generated`；
        · `parent_version_id` 记血缘（超分 / 插帧 / 换音频从哪一版来），版本轨据此
          画出「原始 v1 → 超分 v2」而不是两条互不相干的版本；
        · `in_point` / `out_point` 是「只用源文件的这一段」（长视频切段），两个都空
          = 整个文件，所以老路径行为完全不变。
        """
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
            parent_version_id=parent_version_id,
            in_point=in_point,
            out_point=out_point,
            created_at=utc_now(),
        )
        async with db.write() as session:
            session.add(row)
            shot = await session.get(Shot, shot_id)
            if shot is not None:
                # 新版本自动成为当前版本；旧版本一条都不删
                if kind == "audio":
                    shot.current_audio_version_id = row.id
                else:
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
        #: 音频版本比的是**另一个指针**：镜头上画面与声音各采用一版，拿
        #: `current_version_id` 去比音频版本的话它永远显示「未采用」。
        adopted = {"audio": shot.current_audio_version_id}
        return [
            {
                **as_dict(r),
                "is_current": r.id == adopted.get(r.kind, shot.current_version_id),
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
            kind = kind_of_suffix(Path(asset.path).suffix)
            is_video = kind == "video"
            is_audio = kind == "audio"
            poster = (
                posters.get(frame_key(asset.id, poster_at(row.in_point)))
                if is_video
                else (None if is_audio else asset)
            )
            out[row.id] = {
                "video_path": asset.path if is_video else None,
                "thumbnail_path": poster.path if poster is not None else None,
                "audio_path": asset.path if is_audio else None,
            }
        return out

    async def set_current_version(self, pid: str, version_id: str) -> dict[str, Any]:
        """采用某一版。**全工程唯一的采用入口**（硬约束 3），音频走的是同一扇门。

        `kind="audio"` 落 `current_audio_version_id`：镜头上有两个指针（画面一个、
        声音一个），但采用只有这一个动作——另开一个 `/audio-current` 端点的话，
        前端就得先判断这一版是什么再决定打哪儿，两处判断迟早分叉。
        """
        db = db_of(pid)
        row = await fetch(db, GenerationVersion, version_id, "生成版本")
        async with db.write() as session:
            shot = await session.get(Shot, row.shot_id)
            if shot is not None:
                if row.kind == "audio":
                    shot.current_audio_version_id = version_id
                else:
                    shot.current_version_id = version_id
                shot.updated_at = utc_now()
        bus.emit(
            Channel.VERSION,
            "version.current_changed",
            {"shot_id": row.shot_id, "version_id": version_id, "kind": row.kind},
            project_id=pid,
        )
        self.ensure_pump(pid)
        return as_dict(row)


generation = GenerationService()
