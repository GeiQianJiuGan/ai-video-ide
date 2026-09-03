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

**附件（`attach()`）只是输入法**：一份 Word 剧本 / Excel 分镜表在这里被抽成纯文本填进
输入框，不落库、不落盘、不出网，`chat()` 那侧一个字都不用改——它收到的仍然只是一句话。

**流式（`chat_stream()`）与不流式（`chat()`）落的是同一份记录。** 两条路都走
`agent.collaborate()` 那一个循环，落库那几步也只有一份实现（`_persist()`）——
于是「刷新页面提案还在」这件事不会因为走了哪条路而不一样。三条边界：

  · **能先报的错先报**（`stream_precheck()`）：消息空的、LLM 没配、工程没打开，
    这些在 SSE 的 200 头发出去之前就抛，前端拿到的是正常的 JSON 四要素错误；
  · **半路挂了也不白干**：已经说过的话与已经攒出的提案先落成记录，再吐 `error`
    事件——和「转满轮数」那条老规矩同一个理由；
  · **`error` 之后没有 `done`**：两者互斥，前端收到任一个都该重拉一次历史。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.ai.director import agent
from app.ai.llm import client as llm
from app.core import doctext
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.generation.providers import registry
from app.persistence.models import utc_now
from app.persistence.models_flow import DirectorTurn
from app.services.assets import assets
from app.services.base import as_dict, db_of, dump_json, fetch_all, load_json
from app.services.cast import cast
from app.services.describe import DESC_TARGETS
from app.services.images import images
from app.services.sequence import sequence
from app.services.story import story
from app.services.world import world

log = get_logger("director")

#: 回喂给模型的对话轮数上限。再往前的内容对「现在要改什么」没有帮助，
#: 而现状是靠读工具查的，不靠聊天记录记着。
HISTORY_TURNS = 10

#: 三种「等这一批新建的素材」的接线各自叫什么。落库结果里的
#: `<kind>_wired` / `<kind>_skipped` 两个键就是拿它拼的，前端照键显示，不猜。
_WIRE_WORD = {"cast": "角色", "props": "道具", "location": "地点"}


@dataclass(slots=True)
class _Waiting:
    """一条等着「同一批里新建的素材」的接线。

    `ids` 是提案里**现在就有**的那几个 id：接线时要和刚建出来的一起写回去——
    `set_shot_cast` / `set_shot_props` 是整份覆盖而不是追加。
    """

    kind: str
    #: 落点。`cast` / `props` 是镜头 id（可能是一幕里的一串），`location` 是幕 id。
    targets: list[str]
    ids: list[str]
    names: list[str]
    #: 这一条接线属于哪条提案的落库结果——接上了没接上都写回它，贴在它自己那张卡上。
    entry: dict[str, Any] | None = None


@dataclass(slots=True)
class _Batch:
    """一次 `apply()` 里「谁刚被建出来」的那本账。

    提案之间**没有引用机制**（`temp_id` 只是给人看的标号），所以「同一批里新建的角色 /
    地点 / 道具可以直接按名字用」这件事只能在落库这一层按名字对：素材落成时把
    名字 → id 记在这里，整批落完再统一接线（`_wire_pending`）。

    **刻意分两步**：提案的先后由模型决定，`add_shot` 完全可能排在 `add_character`
    前面。落一条就立刻接一次的话，顺序不同结果就不同。
    """

    cast: dict[str, str] = field(default_factory=dict)
    props: dict[str, str] = field(default_factory=dict)
    location: dict[str, str] = field(default_factory=dict)
    waiting: list[_Waiting] = field(default_factory=list)

    def born(self, kind: str, name: Any, target_id: Any) -> None:
        """记一笔「这个名字现在有了」。名字或 id 空着就当没这回事（不占位）。"""
        key = str(name or "").strip()
        if key and target_id:
            getattr(self, kind)[key] = str(target_id)

    def lookup(self, kind: str, name: str) -> str:
        return str(getattr(self, kind).get(str(name or "").strip()) or "")

    def wait(self, kind: str, targets: list[str], ids: list[str], names: list[str]) -> None:
        if names:
            self.waiting.append(_Waiting(kind, list(targets), list(ids), list(names)))

    def claim(self, entry: dict[str, Any]) -> None:
        """刚落成的那一条领走它自己登记的接线——结果要写回它的卡片。"""
        for wait in self.waiting:
            if wait.entry is None:
                wait.entry = entry


def _split_ids(items: Any, key: str) -> tuple[list[str], list[str]]:
    """提案里那份出场表 / 道具表 → （现在就有的 id，等这一批新建的名字）。

    `ai/director/tools.py::_resolve_appearances` 对不上的名字留的是 `id 为空 +
    pending_name`，所以这里**必须把空 id 滤掉**——原样喂给 `set_shot_cast`
    会当成一个不存在的形象整条失败。
    """
    ids: list[str] = []
    names: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        got = str(item.get(key) or "").strip()
        if got:
            if got not in ids:
                ids.append(got)
            continue
        name = str(item.get("pending_name") or "").strip()
        if name and name not in names:
            names.append(name)
    return ids, names


class DirectorService:
    # --- 会话 ---

    async def history(self, pid: str) -> dict[str, Any]:
        db = db_of(pid)
        rows = await fetch_all(db, DirectorTurn, order_by=DirectorTurn.created_at)
        return {
            "turns": [{**as_dict(row), "content": load_json(row.content_json, {})} for row in rows],
            "llm": llm.status(),
            "attach": self.attach_limits(),
            "note": "提案只是提案：没点「采用」之前，数据库里什么都没变。",
        }

    def attach_limits(self) -> dict[str, Any]:
        """附件那颗按钮要知道的一切：能选什么后缀、多大、抽多少字。

        口径只有 `core/doctext.py::KINDS` 与两个设置项各一处——前端不写第二份后缀清单，
        不然「能选却传不上去」这种事迟早出现。
        """
        return {
            "kinds": [{"suffix": s, "label": doctext.KINDS[s]} for s in sorted(doctext.KINDS)],
            "accept": doctext.accept_attr(),
            "max_mb": settings.director_attach_max_mb,
            "max_chars": settings.director_attach_max_chars,
            "note": "附件只抽文字填进输入框：抽完你先过一眼，按下发送才跟着这句话一起走。",
        }

    async def attach(self, pid: str, filename: str, data: bytes) -> dict[str, Any]:
        """一份附件 → 一段能填进输入框的纯文本。**不落库、不落盘、不出网。**

        三条刻意的边界：

          · **不要求配好 LLM**：抽文字是本机做的事，用户得先看见抽出来什么才决定发不发。
            把它拦在 `require_configured()` 后面等于「没配模型连文档都打不开」；
          · **工程没打开先 404**（`db_of`）：与 `stream_precheck()` 同一个口径，
            附件是这个工程的会话素材，不该在没有工程的时候上传；
          · **不自己造 Asset**：这段文字不是落盘素材，它连一个文件都没有留下。真要把
            文档收进工程，走资产库那条上传路。
        """
        name = (filename or "").strip() or "附件"
        cap = max(1, int(settings.director_attach_max_mb)) * 1024 * 1024
        db_of(pid)
        if len(data) > cap:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "附件太大了",
                f"{name} 有 {len(data) / 1024 / 1024:.1f} MB，超过上限 "
                f"{settings.director_attach_max_mb} MB。",
                [
                    "只把要给它看的那部分另存成一份小文件再传",
                    f"上限可改：backend/.env 里设 AIVS_DIRECTOR_ATTACH_MAX_MB（当前 "
                    f"{settings.director_attach_max_mb}）",
                ],
                {"filename": name, "bytes": len(data), "max_mb": settings.director_attach_max_mb},
            )
        out = doctext.extract(name, data, limit=settings.director_attach_max_chars)
        log.info("director.attached", project_id=pid, filename=out.filename, chars=out.chars)
        return out.to_dict()

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

        这是不流式那条路（兼容 + 不支持 SSE 的调用方）。流式那条见 `chat_stream()`，
        两条共用 `_persist()` 与 `_over_limit()`。
        """
        text = await self.stream_precheck(pid, message)
        await self._add_turn(pid, "user", {"text": text})
        history = await self._llm_history(pid)
        out = await agent.propose(pid, text, history, scope=scope)
        turns = await self._persist(pid, out)
        if out["over_limit"]:
            # 提案已经落好了才报错：转够了轮数不代表这几条不能用。
            raise self._over_limit(len(out["ops"]))
        return {"turns": turns, "ops": out["ops"], "degraded": out["degraded"]}

    async def stream_precheck(self, pid: str, message: str) -> str:
        """能在开流之前报的错，就别等到流里再报。返回收拾干净的那句话。

        `api/director.py` 的流式端点先 `await` 这一下，于是「消息是空的」「LLM 没配置」
        「这个工程没打开」拿到的仍是正常的 4xx/503 JSON 四要素错误——不是一个 200
        然后夹在 `text/event-stream` 里的 `error` 事件（那种前端得写两套错误处理）。
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
        db_of(pid)  # 工程没打开就在这里 404，别等流开了才发现
        return text

    async def chat_stream(
        self, pid: str, message: str, scope: str = "flow"
    ) -> AsyncIterator[dict[str, Any]]:
        """说一句话，把过程当 SSE 事件吐出来。**一行业务数据都不改。**

        产出的是**线上形状**（`{"event": …, "data": {…}}`），`api/director.py` 只负责
        照 SSE 的格式把它们写出去——那一层照旧极薄。事件：

          · `delta` `{text}`                     模型正在写的文字；
          · `tool`  `{name, phase, ok?, error?}` 一次工具调用的开始 / 结束；
          · `op`    一条提案（形状与 `chat()` 回的 `ops[]` 里那条一模一样）；
          · `done`  `{turns, ops, degraded, rounds}`  **正常收尾**，`turns` 是刚落的记录；
          · `error` `{error: {code, title, detail, suggestions}}`  收尾的另一种。

        `done` 与 `error` **互斥且必有其一**；收到任一个前端都该重拉一次历史
        （提案已经落成记录了，刷新也不丢）。
        """
        text = await self.stream_precheck(pid, message)
        await self._add_turn(pid, "user", {"text": text})
        history = await self._llm_history(pid)
        said: list[str] = []
        ops: list[dict[str, Any]] = []
        out: dict[str, Any] = {}
        try:
            async for event in agent.collaborate(pid, text, history, scope=scope):
                kind = event["kind"]
                if kind == "delta":
                    said.append(event["text"])
                    yield {"event": "delta", "data": {"text": event["text"]}}
                elif kind == "tool":
                    data = {"name": event["name"], "phase": event["phase"]}
                    if event["phase"] == "done":
                        data["ok"] = event["ok"]
                        data["error"] = event["error"]
                    yield {"event": "tool", "data": data}
                elif kind == "op":
                    ops.append(event["op"])
                    yield {"event": "op", "data": event["op"]}
                elif kind == "result":
                    out = event["result"]
        except AppError as exc:
            # 半路挂了也不白干：说过的话与攒出的提案先落成记录，再把错误吐出去。
            await self._persist(pid, self._salvage(said, ops))
            yield {"event": "error", "data": {"error": exc.to_dict()}}
            return
        except Exception as exc:  # noqa: BLE001 —— 归一成四要素，绝不静默
            log.warning("director stream failed: %s", exc)
            await self._persist(pid, self._salvage(said, ops))
            yield {
                "event": "error",
                "data": {
                    "error": AppError(
                        ErrorCode.LLM_UNAVAILABLE,
                        "这一轮中断了",
                        f"{type(exc).__name__}: {exc}",
                        [
                            f"已产出的 {len(ops)} 条提案仍在右栏，可以照常审阅采用",
                            "重试一次（多半是连接或超时）",
                            "或在流程图上手动改——手动路径不依赖 LLM",
                        ],
                    ).to_dict()
                },
            }
            return
        if not out:  # collaborate() 保证有 result；真没有就当作中断，绝不静默收尾
            out = self._salvage(said, ops)
            out["over_limit"] = True
        turns = await self._persist(pid, out)
        if out["over_limit"]:
            yield {"event": "error", "data": {"error": self._over_limit(len(out["ops"])).to_dict()}}
            return
        yield {
            "event": "done",
            "data": {
                "turns": turns,
                "ops": out["ops"],
                "degraded": out["degraded"],
                "rounds": out["rounds"],
            },
        }

    @staticmethod
    def _salvage(said: list[str], ops: list[dict[str, Any]]) -> dict[str, Any]:
        """半路中断时手里剩下的东西，拼成 `_persist()` 认的那个形状。"""
        return {
            "reply": "".join(said).strip(),
            "ops": ops,
            "rounds": 0,
            "over_limit": False,
            "degraded": False,
        }

    async def _persist(self, pid: str, out: dict[str, Any]) -> list[dict[str, Any]]:
        """把一轮的结果落成记录：AI 说的那条 + 有提案时再一条。**只有这一份实现。**"""
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
        return turns

    @staticmethod
    def _over_limit(count: int) -> AppError:
        """转满轮数：提案已经落好了才报这个错——转够了轮数不代表这几条不能用。"""
        return AppError(
            ErrorCode.LLM_INVALID_OUTPUT,
            "AI 转了太多轮还没收尾",
            f"已经跑满 {agent.MAX_ROUNDS} 轮工具调用，这一轮就此停下。",
            [
                f"已产出的 {count} 条提案仍在右栏，可以照常审阅采用",
                "把要求说得更具体一点再试（比如指明是哪一幕）",
                "或直接在流程图上手动改——手动路径不依赖 LLM",
            ],
        )

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

        **整批落完还有一步收尾**（`_wire_pending`）：拆剧本时「这一镜有谁」和「这个人是谁」
        本来就在同一批里，所以镜头里那几个还不存在的角色 / 道具 / 地点在这一步按名字接到
        刚建出来的那一条上。接不上不算失败，只在那一条的落库结果里说清少接了谁。
        """
        applied: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        batch = _Batch()
        for op in ops:
            name = str(op.get("op") or "")
            if name == "reject" or not name:
                continue
            try:
                done = await self._one(pid, op, batch)
                entry = {"op": name, "temp_id": op.get("temp_id"), **done}
                applied.append(entry)
            except AppError as exc:
                entry = {"op": name, "temp_id": op.get("temp_id"), "error": exc.to_dict()}
                failed.append(entry)
            except Exception as exc:  # noqa: BLE001 —— 归一成四要素，绝不静默
                log.warning("director apply %s failed: %s", name, exc)
                entry = {
                    "op": name,
                    "temp_id": op.get("temp_id"),
                    "error": AppError(
                        ErrorCode.INTERNAL,
                        "这一条没落成",
                        f"{type(exc).__name__}: {exc}",
                        ["刷新流程图看看现在的状态", "或在流程图上手动做这一步"],
                    ).to_dict(),
                }
                failed.append(entry)
            #: 半路挂掉的那一条也要认领它已经登记的接线（`add_scene` 可能已经建了两个镜头
            #: 才失败）——不认领的话这几条接线会挂到**下一条**成功的卡片上，说的话就错了。
            batch.claim(entry)
        await self._wire_pending(pid, batch)
        await self._add_turn(
            pid, "applied", {"applied": applied, "failed": failed, "count": len(applied)}
        )
        return {"applied": applied, "failed": failed, "count": len(applied)}

    async def _wire_pending(self, pid: str, batch: _Batch) -> None:
        """收尾：把这一批里新建的素材接到等着它的那几个镜头 / 幕上。

        **接不上绝不算失败**：素材与镜头都已经落好了，接不上的原因往往是用户把那条
        `add_character` 丢掉了、或者模型只是把名字写岔了一个字。这一步只把结果写回那条
        提案的落库记录（`cast_wired` / `cast_skipped` / …），前端照常显示——
        少接了一个人是必须说出来的事（硬约束 4），但不该让整条镜头白落。

        接线一律**转调已有写方法**，且写的是「该有的全量」而不是追加：
        `set_shot_cast` / `set_shot_props` 都是整份覆盖。
        """
        for wait in batch.waiting:
            hit: list[str] = []
            got: list[str] = []
            miss: list[str] = []
            for name in wait.names:
                found = batch.lookup(wait.kind, name)
                if found:
                    hit.append(name)
                    got.append(found)
                else:
                    miss.append(name)
            if got:
                try:
                    await self._wire_one(pid, wait, got)
                except AppError as exc:
                    hit, miss = [], wait.names
                    log.warning("director wire %s failed: %s", wait.kind, exc.title)
            word = _WIRE_WORD[wait.kind]
            entry = wait.entry
            if entry is None:
                continue
            if hit:
                entry[f"{wait.kind}_wired"] = f"这一批新建的{word}已经接上：{'、'.join(hit)}。"
            if miss:
                entry[f"{wait.kind}_skipped"] = (
                    f"这一批里没有建成「{'、'.join(miss)}」这个{word}（提案被丢掉了，"
                    f"或者名字对不上），所以这一条没接上它。"
                    f"到{word}那一页建好之后再挂一次即可，已经落库的其余部分不受影响。"
                )

    async def _wire_one(self, pid: str, wait: _Waiting, got: list[str]) -> None:
        """一条接线怎么落。**全部转调已有写方法**，这里不碰 ORM。"""
        if wait.kind == "location":
            await story.update_scene(pid, wait.targets[0], {"location_variant_id": got[0]})
            return
        if wait.kind == "cast":
            for shot in wait.targets:
                await story.set_shot_cast(pid, shot, [*wait.ids, *got])
            return
        items = [{"prop_id": i} for i in (*wait.ids, *got)]
        for shot in wait.targets:
            await story.set_shot_props(pid, shot, items)

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

    async def _one(self, pid: str, op: dict[str, Any], batch: _Batch) -> dict[str, Any]:
        """一条提案怎么落。**全部转调已有的写方法**，这里不碰 ORM。

        `batch` 是这一批的名字账：素材落成时往里记一笔，镜头 / 幕上还对不上的名字往里
        登记一条接线，整批落完由 `_wire_pending` 统一接上（顺序无关）。
        """
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
                ids, pending = _split_ids(shot.get("cast"), "appearance_id")
                if ids:
                    await story.set_shot_cast(pid, created["id"], ids)
                batch.wait("cast", [created["id"]], ids, pending)
            if not after.get("location_variant_id") and after.get("location_name"):
                batch.wait("location", [row["id"]], [], [str(after["location_name"])])
            return {"scene_id": row["id"], "title": row["title"], "shots_created": made}

        if name == "update_scene":
            row = await story.update_scene(pid, sid, after)
            if not after.get("location_variant_id") and after.get("location_name"):
                batch.wait("location", [row["id"]], [], [str(after["location_name"])])
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
            ids, pending = _split_ids(after.get("cast"), "appearance_id")
            if ids:
                await story.set_shot_cast(pid, created["id"], ids)
            batch.wait("cast", [created["id"]], ids, pending)
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
                ids, pending = _split_ids(after.get("cast"), "appearance_id")
                await story.set_shot_cast(pid, shot_id, ids)
                batch.wait("cast", [shot_id], ids, pending)
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
                # 这一批里按名字等着这个角色的镜头，收尾时就靠这一笔接上（默认形象那一个）。
                batch.born("cast", row["name"], appearance["id"])
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
            #: 幕上挂的是变体而不是地点，所以这一笔记的是**第一个变体**——
            #: 与 `tools.py::_resolve_variant` 按名字找到已有地点时取的是同一个。
            batch.born("location", row["name"], variant["id"])
            return {
                "location_id": row["id"],
                "name": row["name"],
                "variant_id": variant["id"],
                "variant": variant["name"],
                **await self._maybe_image(pid, after, "location_variant", variant["id"]),
            }

        if name == "add_prop":
            row = await world.create_prop(pid, {k: after.get(k) for k in ("name", "description")})
            batch.born("props", row["name"], row["id"])
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

        if name == "set_description":
            return await self._set_description(pid, after)

        # 剩下三条都是「整幕覆盖」：作用到这一幕的每个正片镜头上。
        # 转场镜头不算——它是衔接生成出来的，不是导演排的戏。
        shots = await self._real_shots(pid, sid)
        if name == "set_scene_prompt":
            for shot in shots:
                await story.update_shot(pid, shot, {"prompt": str(after.get("prompt") or "")})
            return {"scene_id": sid, "shots_touched": len(shots)}
        if name == "set_scene_cast":
            ids, pending = _split_ids(after.get("cast"), "appearance_id")
            for shot in shots:
                await story.set_shot_cast(pid, shot, ids)
            batch.wait("cast", shots, ids, pending)
            return {"scene_id": sid, "shots_touched": len(shots), "cast_count": len(ids)}
        if name == "set_scene_props":
            ids, pending = _split_ids(after.get("props"), "prop_id")
            items = [{"prop_id": i} for i in ids]
            for shot in shots:
                await story.set_shot_props(pid, shot, items)
            batch.wait("props", shots, ids, pending)
            return {"scene_id": sid, "shots_touched": len(shots), "prop_count": len(items)}

        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识这条提案",
            f"op = {name}。",
            ["丢弃这一条，让 AI 重新提", "或在流程图上手动做这一步"],
        )

    async def _set_description(self, pid: str, after: dict[str, Any]) -> dict[str, Any]:
        """把提案里那一句描述落到对应的行上。**全部转调已有写方法，这里不碰 ORM。**

        写哪个字段由提案里的 `field` 说（它来自 `describe.target`，是唯一那份口径）——
        形象上没有 `description` 列，那一句要落在账单真正会读的 `traits` 上。
        """
        kind = str(after.get("target_kind") or "").strip()
        target_id = str(after.get("target_id") or "").strip()
        if kind not in DESC_TARGETS:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "不认识这种目标",
                f"target_kind = {kind or '（空）'}。",
                [f"可用的是：{'、'.join(DESC_TARGETS)}", "丢弃这一条，让 AI 重新提"],
                {"target_kind": after.get("target_kind")},
            )
        fallback = "traits" if kind == "appearance" else "description"
        field = str(after.get("field") or "") or fallback
        #: 空字符串 = 清掉那一句（不是「这次不改」）。`assign()` 会跳过 `None`，
        #: 所以这里一律给字符串，别让「清空」变成静默的无操作。
        patch = {field: str(after.get("description") or "")}
        if kind == "asset":
            await assets.update(pid, target_id, patch)
        elif kind == "character":
            await cast.update_character(pid, target_id, patch)
        elif kind == "appearance":
            await cast.update_appearance(pid, target_id, patch)
        elif kind == "location":
            await world.update_location(pid, target_id, patch)
        elif kind == "location_variant":
            await world.update_variant(pid, target_id, patch)
        else:
            await world.update_prop(pid, target_id, patch)
        return {
            "target_kind": kind,
            "target_id": target_id,
            "target_label": after.get("target_label"),
            "field": field,
            "description": patch[field],
        }

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
