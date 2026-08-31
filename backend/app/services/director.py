"""AI 导演的会话与提案落库（两级场景系统第一级的协作栏）。

这一层只解决三件事，业务改动一件都不自己写：

  1. **会话要活过刷新**。对话与提案都落 `DirectorTurn`（只增不改）。用户审阅到一半
     刷新页面，提案还在——否则那一半功夫就白费了。
  2. **提案不是改动**。`chat()` 把提案存下来，但**一行库都不改**；只有 `apply()`
     被调用时才动，而且只落 `op != "reject"` 的条目。
  3. **落库一律转调已有方法**。`story` / `sequence` / `world` 已经有完整的写路径
     （带校验、带重排、带事件），这里绝不另写一份——那样迟早两边行为不一致。

失败的处理刻意不是「一条挂了整批回滚」：每条独立落，失败的连四要素错误一起回给前端。
一条角色名对不上，不该让另外四条通过审阅的改动也进不去。
"""

from __future__ import annotations

from typing import Any

from app.ai.director import agent
from app.ai.llm import client as llm
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.generation.providers import registry
from app.persistence.models import utc_now
from app.persistence.models_flow import DirectorTurn
from app.services.base import as_dict, db_of, dump_json, fetch_all, load_json
from app.services.cast import cast
from app.services.images import images
from app.services.sequence import sequence
from app.services.story import story
from app.services.world import world

log = get_logger("director")

#: 回喂给模型的对话轮数上限。再往前的内容对「现在要改什么」没有帮助，
#: 而现状是靠读工具查的，不靠聊天记录记着。
HISTORY_TURNS = 10


class DirectorService:
    # --- 会话 ---

    async def history(self, pid: str) -> dict[str, Any]:
        db = db_of(pid)
        rows = await fetch_all(db, DirectorTurn, order_by=DirectorTurn.created_at)
        return {
            "turns": [{**as_dict(row), "content": load_json(row.content_json, {})} for row in rows],
            "llm": llm.status(),
            "note": "提案只是提案：没点「采用」之前，数据库里什么都没变。",
        }

    async def _add_turn(self, pid: str, role: str, content: dict[str, Any]) -> dict[str, Any]:
        db = db_of(pid)
        row = DirectorTurn(
            id=new_id("director_turn"),
            role=role,
            content_json=dump_json(content),
            created_at=utc_now(),
        )
        async with db.write() as session:
            session.add(row)
        return {**as_dict(row), "content": content}

    async def clear(self, pid: str) -> None:
        """清空这个工程的协作记录。已经落库的改动不受影响——那是库里的数据，不是聊天记录。"""
        db = db_of(pid)
        rows = await fetch_all(db, DirectorTurn)
        async with db.write() as session:
            for row in rows:
                fresh = await session.get(DirectorTurn, row.id)
                if fresh is not None:
                    await session.delete(fresh)

    async def chat(self, pid: str, message: str, scope: str = "flow") -> dict[str, Any]:
        """说一句话，拿回一份提案。**不改任何业务数据。**

        `scope` 是「用户现在开着哪一页」（`script` 剧本页 / `flow` 幕流程图页）。它**只影响
        这一次请求拼出来的系统提示词**那一句提示，不落库、不加列——两页共用同一个会话，
        换页不该让历史对话变味。
        """
        text = str(message or "").strip()
        if not text:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "说点什么",
                "消息是空的。",
                ["比如「在第 2 幕后面加一幕雨夜追车」", "或直接在流程图上手动加一幕"],
            )
        llm.require_configured()
        await self._add_turn(pid, "user", {"text": text})
        history = await self._llm_history(pid)
        out = await agent.propose(pid, text, history, scope=scope)
        turns = [
            await self._add_turn(
                pid,
                "assistant",
                {
                    "text": out["reply"] or "（这一轮没有说明文字，看右边的提案）",
                    "rounds": out["rounds"],
                    "degraded": out["degraded"],
                },
            )
        ]
        if out["ops"]:
            turns.append(
                await self._add_turn(
                    pid,
                    "proposal",
                    {"ops": out["ops"], "note": "以上为提案，尚未写入数据库。"},
                )
            )
        if out["over_limit"]:
            # 提案已经落好了才报错：转够了轮数不代表这几条不能用。
            raise AppError(
                ErrorCode.LLM_INVALID_OUTPUT,
                "AI 转了太多轮还没收尾",
                f"已经跑满 {agent.MAX_ROUNDS} 轮工具调用，这一轮就此停下。",
                [
                    f"已产出的 {len(out['ops'])} 条提案仍在右栏，可以照常审阅采用",
                    "把要求说得更具体一点再试（比如指明是哪一幕）",
                    "或直接在流程图上手动改——手动路径不依赖 LLM",
                ],
            )
        return {"turns": turns, "ops": out["ops"], "degraded": out["degraded"]}

    async def _llm_history(self, pid: str) -> list[dict[str, Any]]:
        """只把人说的和 AI 说的回喂给模型。提案那几条不回喂——
        它们是给用户看的 Diff，模型再看一遍只会重复提一次。"""
        db = db_of(pid)
        rows = await fetch_all(db, DirectorTurn, order_by=DirectorTurn.created_at)
        chat = [r for r in rows if r.role in ("user", "assistant")][-HISTORY_TURNS:]
        out = []
        for row in chat[:-1] if chat and chat[-1].role == "user" else chat:
            text = str(load_json(row.content_json, {}).get("text") or "")
            if text:
                out.append({"role": row.role, "content": text})
        return out

    # --- 落库（逐条审阅之后） ---

    async def apply(self, pid: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        """把审阅通过的条目落库。只接受 `op != "reject"` 的条目。

        每条独立落：一条失败不回滚已经成功的那几条，失败的连四要素错误一起回给前端。
        """
        applied: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for op in ops:
            name = str(op.get("op") or "")
            if name == "reject" or not name:
                continue
            try:
                done = await self._one(pid, op)
                applied.append({"op": name, "temp_id": op.get("temp_id"), **done})
            except AppError as exc:
                failed.append({"op": name, "temp_id": op.get("temp_id"), "error": exc.to_dict()})
            except Exception as exc:  # noqa: BLE001 —— 归一成四要素，绝不静默
                log.warning("director apply %s failed: %s", name, exc)
                failed.append(
                    {
                        "op": name,
                        "temp_id": op.get("temp_id"),
                        "error": AppError(
                            ErrorCode.INTERNAL,
                            "这一条没落成",
                            f"{type(exc).__name__}: {exc}",
                            ["刷新流程图看看现在的状态", "或在流程图上手动做这一步"],
                        ).to_dict(),
                    }
                )
        await self._add_turn(
            pid, "applied", {"applied": applied, "failed": failed, "count": len(applied)}
        )
        return {"applied": applied, "failed": failed, "count": len(applied)}

    #: 提案的 `after` 里能直接落库的镜头字段。**只有这一张表**——`camera_motion` /
    #: `visual_prompt` / `audio_dialogue` / `skill` 是给人看的过程量，正向 prompt 已经在
    #: `ai/director/tools.py::_shot_after` 里拼好写进 `after["prompt"]` 了。
    SHOT_PATCH_KEYS = (
        "title",
        "description",
        "duration",
        "camera",
        "movement",
        "prompt",
        "negative_prompt",
    )

    async def _one(self, pid: str, op: dict[str, Any]) -> dict[str, Any]:
        """一条提案怎么落。**全部转调已有的写方法**，这里不碰 ORM。"""
        name = str(op["op"])
        after = op.get("after") or {}
        sid = str(op.get("scene_id") or "")

        if name == "add_scene":
            row = await story.create_scene(
                pid,
                {
                    k: after.get(k)
                    for k in ("title", "summary", "time_of_day", "location_variant_id")
                },
            )
            made = 0
            for shot in after.get("shots") or []:
                created = await story.create_shot(
                    pid, row["id"], {k: shot.get(k) for k in self.SHOT_PATCH_KEYS}
                )
                made += 1
                ids = [c["appearance_id"] for c in shot.get("cast") or []]
                if ids:
                    await story.set_shot_cast(pid, created["id"], ids)
            return {"scene_id": row["id"], "title": row["title"], "shots_created": made}

        if name == "update_scene":
            row = await story.update_scene(pid, sid, after)
            return {"scene_id": row["id"], "title": row["title"]}

        if name == "delete_scene":
            await story.delete_scene(pid, sid)
            return {"scene_id": sid, "deleted": True}

        if name == "reorder_scenes":
            await story.reorder_scenes(pid, [str(i) for i in after.get("order") or []])
            return {"reordered": len(after.get("order") or [])}

        if name == "set_link":
            row = await sequence.set_link(
                pid,
                str(after.get("from_scene_id") or ""),
                str(after.get("to_scene_id") or ""),
                mode=str(after.get("mode") or "cut"),
                duration=after.get("duration"),
                prompt=after.get("prompt"),
            )
            return {"link_id": row["id"], "mode": row["mode"]}

        if name == "add_shot":
            created = await story.create_shot(
                pid,
                str(after.get("scene_id") or sid),
                {k: after.get(k) for k in self.SHOT_PATCH_KEYS},
            )
            ids = [c["appearance_id"] for c in after.get("cast") or []]
            if ids:
                await story.set_shot_cast(pid, created["id"], ids)
            #: 插在中间要走 `move_shot`（它内部重排 + 全局重排）。`position` 是 1 起的，
            #: `move_shot` 收 0 起的落点。
            if after.get("position"):
                await story.move_shot(
                    pid,
                    created["id"],
                    str(after.get("scene_id") or sid),
                    int(after["position"]) - 1,
                )
            return {"shot_id": created["id"], "title": created["title"]}

        if name == "update_shot":
            shot_id = str(op.get("shot_id") or "")
            patch = {k: after[k] for k in self.SHOT_PATCH_KEYS if k in after}
            if patch:
                row = await story.update_shot(pid, shot_id, patch)
            else:
                row = await story.get_shot(pid, shot_id)
            if "cast" in after:
                await story.set_shot_cast(
                    pid, shot_id, [c["appearance_id"] for c in after.get("cast") or []]
                )
            return {"shot_id": row["id"], "title": row["title"]}

        if name == "delete_shot":
            shot_id = str(op.get("shot_id") or "")
            await story.delete_shot(pid, shot_id)
            return {"shot_id": shot_id, "deleted": True}

        if name == "reorder_shots":
            scene_id = str(after.get("scene_id") or sid)
            #: 提案是在审阅之前算出来的，同一批里可能有一条 delete_shot 已经把某个镜头删了。
            #: 所以落库前按**现在**这一幕有哪些镜头对一遍：删掉的丢弃，没提到的排在后面——
            #: `story.reorder_shots` 收到不属于这一幕的 id 会整条失败。
            live = await self._real_shots(pid, scene_id)
            order = [str(i) for i in after.get("order") or [] if str(i) in live]
            order += [i for i in live if i not in order]
            await story.reorder_shots(pid, scene_id, order)
            return {"scene_id": scene_id, "reordered": len(order)}

        if name == "set_shot_link":
            row = await sequence.set_shot_link(
                pid,
                str(after.get("from_shot_id") or ""),
                str(after.get("to_shot_id") or ""),
                mode=str(after.get("mode") or "cut"),
                duration=after.get("duration"),
                prompt=after.get("prompt"),
            )
            return {"link_id": row["id"], "mode": row["mode"]}

        if name == "add_character":
            row = await cast.create_character(
                pid, {k: after.get(k) for k in ("name", "description")}
            )
            # `create_character` 顺手建了「默认形象」——出图挂的是形象，不是角色。
            apps = await cast.list_appearances(pid, row["id"])
            appearance = next((a for a in apps if a.get("is_default")), apps[0] if apps else None)
            out = {
                "character_id": row["id"],
                "name": row["name"],
                "appearance_id": appearance["id"] if appearance else None,
            }
            if appearance is not None:
                out.update(await self._maybe_image(pid, after, "appearance", appearance["id"]))
            return out

        if name == "add_location":
            row = await world.create_location(
                pid, {k: after.get(k) for k in ("name", "description")}
            )
            variant = await world.create_variant(
                pid,
                row["id"],
                {
                    "name": after.get("variant") or "默认场景",
                    "time_of_day": after.get("time_of_day"),
                },
            )
            return {
                "location_id": row["id"],
                "name": row["name"],
                "variant_id": variant["id"],
                "variant": variant["name"],
                **await self._maybe_image(pid, after, "location_variant", variant["id"]),
            }

        if name == "add_prop":
            row = await world.create_prop(pid, {k: after.get(k) for k in ("name", "description")})
            return {
                "prop_id": row["id"],
                "name": row["name"],
                **await self._maybe_image(pid, after, "prop", row["id"]),
            }

        if name == "generate_reference":
            return {
                "target_kind": str(after.get("target_kind") or ""),
                "target_id": str(after.get("target_id") or ""),
                "target_label": after.get("target_label"),
                **await self._maybe_image(
                    pid,
                    after,
                    str(after.get("target_kind") or ""),
                    str(after.get("target_id") or ""),
                ),
            }

        # 剩下三条都是「整幕覆盖」：作用到这一幕的每个正片镜头上。
        # 转场镜头不算——它是衔接生成出来的，不是导演排的戏。
        shots = await self._real_shots(pid, sid)
        if name == "set_scene_prompt":
            for shot in shots:
                await story.update_shot(pid, shot, {"prompt": str(after.get("prompt") or "")})
            return {"scene_id": sid, "shots_touched": len(shots)}
        if name == "set_scene_cast":
            ids = [c["appearance_id"] for c in after.get("cast") or []]
            for shot in shots:
                await story.set_shot_cast(pid, shot, ids)
            return {"scene_id": sid, "shots_touched": len(shots), "cast_count": len(ids)}
        if name == "set_scene_props":
            items = [{"prop_id": p["prop_id"]} for p in after.get("props") or []]
            for shot in shots:
                await story.set_shot_props(pid, shot, items)
            return {"scene_id": sid, "shots_touched": len(shots), "prop_count": len(items)}

        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识这条提案",
            f"op = {name}。",
            ["丢弃这一条，让 AI 重新提", "或在流程图上手动做这一步"],
        )

    async def _maybe_image(
        self, pid: str, after: dict[str, Any], target_kind: str, target_id: str
    ) -> dict[str, Any]:
        """素材建好之后顺带排一张参考图。**素材已经落了，这一步失败绝不回滚它。**

        三种结果，每一种都说出来（照硬约束 4）：没写 `image_prompt` → 什么都不做；
        图片服务没配置 → `image_skipped` 写明原因，素材照旧建成了；入队失败 →
        `image_skipped` 写那个四要素错误的标题，用户还能在素材页手点一次「生成参考图」。
        """
        text = str(after.get("image_prompt") or "").strip()
        if not text:
            return {}
        if not registry.image_configured():
            return {
                "image_skipped": (
                    "图片服务未配置（设置页的「图片生成 API」是 none）："
                    "素材已经建好了，图没有生成。配一个服务后在素材页点「生成参考图」，"
                    "或手动导入一张图。"
                )
            }
        try:
            job = await images.enqueue(
                pid,
                target_kind,
                target_id,
                prompt=text,
                skill=str(after.get("skill") or "") or None,
            )
        except AppError as exc:
            log.warning("director image enqueue failed: %s", exc.title)
            return {"image_skipped": f"{exc.title}：{exc.detail}"}
        return {"job_id": job["id"], "target_label": job.get("target_label")}

    async def _real_shots(self, pid: str, sid: str) -> list[str]:
        lane = next((la for la in await story.storyboard(pid) if la["id"] == sid), None)
        if lane is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "这一幕已经不在了",
                f"scene_id = {sid or '（空）'}，可能在审阅期间被删掉了。",
                ["刷新流程图后重新提一次"],
            )
        return [s["id"] for s in lane["shots"] if s.get("kind") != "transition"]


director = DirectorService()
