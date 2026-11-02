"""Checkpointed AI authoring. Models write drafts; validated bundles use existing services."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.ai.director.tools import to_op
from app.ai.llm import client as llm
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.generation import intent as gen_intent
from app.persistence.models import utc_now
from app.persistence.models_creation import CreationRun
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, load_json
from app.services.story import story


class DraftModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class NamedMaterial(DraftModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ScriptDraft(DraftModel):
    screenplay_md: str = Field(min_length=40)
    characters: list[NamedMaterial] = Field(default_factory=list)
    locations: list[NamedMaterial] = Field(default_factory=list)
    props: list[NamedMaterial] = Field(default_factory=list)


class SceneOutline(DraftModel):
    title: str = Field(min_length=1)
    summary: str = Field(min_length=10)
    location_name: str = ""
    time_of_day: str = ""


class OutlineDraft(DraftModel):
    scenes: list[SceneOutline] = Field(min_length=1, max_length=120)


class FrameDraft(DraftModel):
    title: str = Field(min_length=1)
    prompt: str = Field(min_length=10)


class ShotDraft(DraftModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=10)
    character_names: list[str] = Field(default_factory=list)
    prop_names: list[str] = Field(default_factory=list)
    intent: dict[str, Any]
    frames: list[FrameDraft] = Field(default_factory=list, max_length=8)


class SceneDraft(DraftModel):
    shots: list[ShotDraft] = Field(min_length=1, max_length=16)


class NeedsRepair(Exception):
    pass


class Paused(Exception):
    pass


def failure(detail: str, code: ErrorCode = ErrorCode.LLM_INVALID_OUTPUT) -> AppError:
    return AppError(
        code,
        "创作任务需要恢复",
        detail,
        ["点击恢复，从已保存的检查点继续", "查看草稿；已完成的内容不会重复创建"],
    )


def output(row: CreationRun) -> dict[str, Any]:
    state = load_json(row.state_json, {})
    return {**{k: v for k, v in as_dict(row).items() if k != "state_json"}, "state": state}


class CreationService:
    def __init__(self):
        self.workers: dict[tuple[str, str], asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def get(self, pid: str, rid: str) -> dict[str, Any]:
        row = await fetch(db_of(pid), CreationRun, rid, "创作任务")
        if row.status in ("running", "applying") and (pid, rid) not in self.workers:
            # The worker may have finished between reading the row and checking
            # its handle. Re-read under the write lock before declaring a restart.
            async with db_of(pid).write() as session:
                row = await session.get(CreationRun, rid)
                if row.status in ("running", "applying") and (pid, rid) not in self.workers:
                    state = load_json(row.state_json, {})
                    state["message"] = "服务已重启，点击恢复继续已保存的草稿。"
                    row.state_json = dump_json(state)
                    row.status = "paused"
                    row.updated_at = utc_now()
        return output(row)

    async def latest(self, pid: str) -> dict[str, Any] | None:
        rows = await fetch_all(db_of(pid), CreationRun, order_by=CreationRun.id.desc())
        return await self.get(pid, rows[0].id) if rows else None

    async def save(self, pid: str, rid: str, state: dict, status: str = "running") -> None:
        async with db_of(pid).write() as session:
            row = await session.get(CreationRun, rid)
            row.state_json = dump_json(state)
            row.status = status
            row.updated_at = utc_now()

    def spawn(self, pid: str, rid: str) -> None:
        async def worker():
            from app.services.access import expected_revision

            expected_revision.set(None)
            try:
                await self.run(pid, rid)
            finally:
                self.workers.pop((pid, rid), None)

        self.workers[(pid, rid)] = asyncio.create_task(worker())

    async def start(self, pid: str, message: str, goal: str, mode: str) -> dict[str, Any]:
        llm.require_configured()
        async with self.locks.setdefault(pid, asyncio.Lock()):
            previous = await self.latest(pid)
            if previous and previous["status"] == "completed":
                old = previous["state"]
                current = await story.get_story(pid)
                if (
                    old["goal"] == goal
                    and old["mode"] == mode
                    and old["request"] == message
                    and (current.get("screenplay_md") or "") == old["draft"]["screenplay_md"]
                ):
                    return previous
            if previous and previous["status"] in (
                "running",
                "applying",
                "paused",
                "failed",
                "review",
            ):
                raise failure(
                    "当前项目已有未完成的创作任务，请恢复、采用或丢弃草稿后再开始。",
                    ErrorCode.CONFLICT,
                )
            source = await story.get_story(pid)
            if not message.strip() and not (source.get("screenplay_md") or source.get("raw_text")):
                raise failure("请填写创作想法，或先提供剧本。", ErrorCode.VALIDATION_ERROR)
            row = CreationRun(
                id=new_id("creation_run"),
                status="running",
                state_json=dump_json(
                    {
                        "goal": goal,
                        "mode": mode,
                        "message": "正在编写完整剧本",
                        "request": message,
                        "source": source.get("raw_text") or "",
                        "base_screenplay": source.get("screenplay_md") or "",
                        "auto_apply": settings.director_auto_apply,
                        "draft": None,
                        "outlines": None,
                        "scenes": [],
                        "applied": [],
                        "attempts": [],
                    }
                ),
            )
            async with db_of(pid).write() as session:
                session.add(row)
            from app.services.director import director

            await director._add_turn(
                pid,
                "user",
                {"text": message or "按现有剧本制作完整分镜", "creation_run_id": row.id},
            )
            self.spawn(pid, row.id)
            return output(row)

    async def control(self, pid: str, rid: str, action: str) -> dict[str, Any]:
        async with self.locks.setdefault(pid, asyncio.Lock()):
            current = await self.get(pid, rid)
            state = current["state"]
            if action in ("pause", "discard"):
                if current["status"] in ("completed", "discarded"):
                    return current
                worker = self.workers.get((pid, rid))
                if worker:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                state = load_json(
                    (await fetch(db_of(pid), CreationRun, rid, "创作任务")).state_json, {}
                )
                await self.save(pid, rid, state, "discarded" if action == "discard" else "paused")
            elif action == "resume":
                if current["status"] not in ("paused", "failed"):
                    return current
                state.pop("error", None)
                await self.save(pid, rid, state)
                self.spawn(pid, rid)
            elif action == "apply":
                if current["status"] == "completed":
                    return current
                if current["status"] != "review":
                    raise failure("草稿尚未完成，请先恢复创作。", ErrorCode.CONFLICT)
                state["auto_apply"] = True
                await self.save(pid, rid, state)
                self.spawn(pid, rid)
            return await self.get(pid, rid)

    async def ask(
        self,
        pid: str,
        rid: str,
        state: dict,
        label: str,
        schema: type[BaseModel],
        instruction: str,
        context: dict,
    ) -> dict:
        error = ""
        for attempt in range(3):
            state["message"] = label + (f" · 自动修复 {attempt}/2" if attempt else "")
            await self.save(pid, rid, state)
            try:
                data = await llm.complete_json(
                    "你是影视编剧与分镜师。完成指定目标，不要请求用户回复继续。只输出符合给定结构的 JSON。"
                    + instruction,
                    dump_json(
                        {"context": context, "schema": schema.model_json_schema(), "repair": error}
                    ),
                )
                parsed = schema.model_validate(data).model_dump()
                if schema is SceneDraft:
                    for shot in parsed["shots"]:
                        intent = shot["intent"]
                        if not all(
                            str(intent.get(k) or "").strip()
                            for k in ("beat", "action", "shot_size", "first_frame")
                        ):
                            raise NeedsRepair(
                                "每个镜头的 intent 必须包含非空 beat、action、shot_size、first_frame"
                            )
                        shot["intent"] = gen_intent.coerce(intent)
                        duration = float(intent.get("duration") or 0)
                        if not 1 <= duration <= 15:
                            raise NeedsRepair("镜头 duration 必须为 1~15 秒")
                        if state["mode"] == "direct":
                            shot["frames"] = []
                        if state["mode"] == "frames" and not shot["frames"]:
                            raise NeedsRepair("制帧模式每个镜头至少需要一张完整画面的 frames 计划")
                    from app.services.cast import cast

                    known = {c["name"] for c in state["draft"]["characters"]} | {
                        c["name"] for c in await cast.list_characters(pid)
                    }
                    for shot in parsed["shots"]:
                        if set(shot["character_names"]) - known:
                            raise NeedsRepair(
                                "镜头出场人物必须来自剧本人物名单或已有角色，不要新造名字"
                            )
                    from app.services.world import world

                    known_props = {p["name"] for p in state["draft"]["props"]} | {
                        p["name"] for p in await world.list_props(pid)
                    }
                    for shot in parsed["shots"]:
                        if set(shot["prop_names"]) - known_props:
                            raise NeedsRepair("镜头道具必须来自剧本道具名单或已有道具")
                if schema is OutlineDraft:
                    from app.services.world import world

                    known_locations = {p["name"] for p in state["draft"]["locations"]} | {
                        p["name"] for p in await world.list_locations(pid)
                    }
                    if any(
                        s["location_name"] and s["location_name"] not in known_locations
                        for s in parsed["scenes"]
                    ):
                        raise NeedsRepair(
                            "幕的 location_name 必须使用地点名单中的原名，未命名地点可留空"
                        )
                return parsed
            except asyncio.CancelledError:
                raise
            except (AppError, ValueError, TypeError, NeedsRepair) as exc:
                if isinstance(exc, AppError) and exc.code not in (
                    ErrorCode.LLM_UNAVAILABLE,
                    ErrorCode.LLM_INVALID_OUTPUT,
                ):
                    raise
                error = str(getattr(exc, "detail", str(exc)))[:1800]
                state["attempts"].append({"stage": label, "attempt": attempt + 1, "error": error})
        raise failure(f"{label}自动恢复两次后仍未通过检查：{error}")

    async def run(self, pid: str, rid: str) -> None:
        state = load_json((await fetch(db_of(pid), CreationRun, rid, "创作任务")).state_json, {})
        try:
            if state["draft"] is None:
                state["draft"] = await self.ask(
                    pid,
                    rid,
                    state,
                    "编写完整剧本",
                    ScriptDraft,
                    "产出完整中文剧本，包含开端、发展与结局、分场动作与必要对白，而非大纲或占位。角色、地点、道具列出确实有戏份的项目。已有剧本时按照用户目标补全或忠实整理，不能擅改故事主线。",
                    {
                        "request": state["request"],
                        "source": state["source"],
                        "screenplay": state["base_screenplay"],
                    },
                )
                await self.save(pid, rid, state)
            if state["goal"] == "storyboard":
                if state["outlines"] is None:
                    data = await self.ask(
                        pid,
                        rid,
                        state,
                        "规划全部幕",
                        OutlineDraft,
                        "按时间顺序规划覆盖完整剧本的全部幕，一幕是连续时空段落。只写紧凑的幕概要，不写镜头。不得因单批数量上限省略结局或后半故事。",
                        state["draft"],
                    )
                    state["outlines"] = data["scenes"]
                    await self.save(pid, rid, state)
                while len(state["scenes"]) < len(state["outlines"]):
                    i = len(state["scenes"])
                    outline = state["outlines"][i]
                    data = await self.ask(
                        pid,
                        rid,
                        state,
                        f"制作第 {i + 1}/{len(state['outlines'])} 幕镜头",
                        SceneDraft,
                        "只完成当前幕，通常3~8镜，完整覆盖本幕概要。每镜含中文剧情description；intent含beat/action/first_frame/shot_size/angle/movement/subjects/duration/dialogue，画面与制作描述用英文，对白保留中文。character_names 与 prop_names 按原名列出本镜人物和道具。不要填写数据库ID。制帧模式还要为每镜提供frames画面计划（开场、关键动作或收尾），每个prompt描述一个完整静态画面，允许人物，禁止四视图、拼图。直出模式frames留空。",
                        {
                            "script": state["draft"],
                            "outline": outline,
                            "previous": state["scenes"][-1:] or [],
                            "mode": state["mode"],
                        },
                    )
                    state["scenes"].append({**outline, **data})
                    await self.save(pid, rid, state)
            state["message"] = "完整性检查通过：" + (
                f"{len(state['scenes'])} 幕，{sum(len(s['shots']) for s in state['scenes'])} 个镜头"
                if state["goal"] == "storyboard"
                else "完整剧本已生成"
            )
            if state["auto_apply"]:
                current = (await story.get_story(pid)).get("screenplay_md") or ""
                if not state["applied"] and current != state["base_screenplay"]:
                    state["message"] = "工程剧本已被修改，草稿已保留。请审阅后明确采用。"
                    state["base_screenplay"] = current
                    state["auto_apply"] = False
            if state["auto_apply"]:
                await self.apply(pid, rid, state)
            else:
                from app.services.director import director

                async with db_of(pid).atomic():
                    if not state.get("announced"):
                        await director._add_turn(
                            pid,
                            "assistant",
                            {
                                "text": state["message"]
                                + "。完整草稿已就绪，请在任务卡中审阅采用。",
                                "creation_run_id": rid,
                            },
                        )
                        state["announced"] = True
                    await self.save(pid, rid, state, "review")
        except asyncio.CancelledError:
            # Checkpoints already saved; control() persists the paused status.
            raise
        except Exception as exc:
            state["error"] = getattr(exc, "detail", str(exc))
            state["message"] = "任务已保留，可从当前检查点恢复"
            await self.save(pid, rid, state, "failed")

    async def apply(self, pid: str, rid: str, state: dict) -> None:
        from app.services.cast import cast
        from app.services.director import director
        from app.services.world import world

        raw_ops = [("update_screenplay", {"screenplay_md": state["draft"]["screenplay_md"]})]
        if state["goal"] == "storyboard":
            for key, tool, existing in (
                ("characters", "add_character", await cast.list_characters(pid)),
                ("locations", "add_location", await world.list_locations(pid)),
                ("props", "add_prop", await world.list_props(pid)),
            ):
                known = {r["name"].strip() for r in existing}
                for material in state["draft"][key]:
                    # Keep a stable slot even if an earlier checkpoint created this material.
                    raw_ops.append(
                        (tool, {**material, "_reuse": material["name"].strip() in known})
                    )
                    known.add(material["name"].strip())
            raw_ops.extend(("add_scene", scene) for scene in state["scenes"])
        while len(state["applied"]) < len(raw_ops):
            i = len(state["applied"])
            tool, args = raw_ops[i]
            if args.get("_reuse"):
                state["applied"].append({"reused": args["name"]})
                await self.save(pid, rid, state, "applying")
                continue
            op = await to_op(pid, tool, args, i + 1)
            if tool == "add_scene":
                for target, source in zip(op["after"]["shots"], args["shots"], strict=True):
                    target["frame_plan"] = source.get("frames", [])
                    target["creation_props"] = source.get("prop_names", [])
            # The bundle and its checkpoint commit together: no empty scenes or duplicates after a restart.
            async with db_of(pid).atomic():
                done = await director.apply(pid, [op])
                if done["failed"]:
                    raise failure(dump_json(done["failed"]))
                committed = deepcopy(state)
                committed["applied"].append(done["applied"])
                await self.save(pid, rid, committed, "applying")
            state.update(committed)
        await self.save(pid, rid, state, "completed")

    async def stop_project(self, pid: str):
        workers = [t for (p, _), t in list(self.workers.items()) if p == pid]
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def shutdown(self):
        workers = list(self.workers.values())
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


creation = CreationService()
