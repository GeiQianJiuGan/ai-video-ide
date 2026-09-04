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

**免确认模式（设置项 `director.auto_apply`）改的只是「谁按下那一下」。** 开着时 `chat()` /
`chat_stream()` 在**同一个请求里**接着调那一个 `apply()`——不是第二条写路径，写工具照旧
永不落库。所以上面第 2 条边界一个字都没松，松的是「改动要等用户点一次」。落成了什么照旧
一条条回（`applied` / `failed`），接不上的名字与没排上的图也照旧写在各自那条里。

**一键全流程（`autopilot()`）是把免确认模式连成四步**：核心剧本 → 人物 / 地点 / 道具 →
拆幕 → 按幕拆分镜（`AUTO_STAGES`）。顺序不能换，因为后一步要用前一步**真落进库**的 id
（拆分镜要 `scene_id`，按名字接线要形象 id），所以它**以免确认开着为前提**——关着时抛四要素
错误，绝不偷偷写几十行数据。每一步都是「一句话 → `agent.propose()` → `_persist()` →
`apply()`」，与 `chat()` 逐字同构（阶段提示词是**普通 user message**，不新增第二层系统提示词），
于是整条链一行新的写库逻辑都没有，前几步的结论也靠 `_llm_history()` 自然带到后面几步。
某一步失败时**已经落的不回滚**：还什么都没写就直接抛，写过东西之后一律 200 +
`stages[].error`（跳过不是失败，但必须说出来）。
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

#: 「一键全流程」的四步与它们在界面上的名字。**顺序不能换**：后一步要用前一步真落进库的
#: id（拆分镜要 `scene_id`，按名字接线要形象 id），所以每一步都得先落库再走下一步。
AUTO_STAGES: tuple[tuple[str, str], ...] = (
    ("digest", "核心剧本"),
    ("materials", "人物 / 地点 / 道具"),
    ("scenes", "拆幕"),
    ("shots", "拆分镜"),
)

#: 第一步只出文字、不出提案：先把这一章读完并说清主线。它同时是后面三步的上下文
#: （`_llm_history()` 会把它带过去），所以要它把人名 / 地名 / 道具名按**原文**列出来。
_AUTO_DIGEST = """现在开始「一键全流程」的第一步：把剧本读完，只说结论，先不要提任何提案。

这样做：
1. 用 read_script 从 offset=0 开始分段读，每次 limit 尽量给大（比如 6000），
   一直读到返回里的 done 是 true。原文已经在这个工程里，不用问我要。
2. 读完写一份中文「核心剧本」：一句话主线、按时间顺序的关键情节（不超过 12 条）、
   出场人物名单、出现的地点名单、有戏份的关键道具名单。

人名、地名、道具名一律用剧本里的原文，不要改写也不要音译，更不要编原文里没有的。
这一步**只说话**：add_scene / add_shot / add_character 这类写工具一个都不要调。"""

#: 第二步：素材。出图那一句按三种情况分岔（`_image_hint`），其余一个字不变。
_AUTO_MATERIALS = """第二步：把上面那份核心剧本里的人物、地点、道具建成素材。

先用 list_characters、list_locations、list_props 看库里已经有什么，**已经有的不要再建一遍**
（同名或明显是同一个就跳过，并在回答里说跳过了谁）。还缺的每个提一条：

· 人物 → add_character：name 用原文，description 写年龄段、外貌、气质、常穿什么
· 地点 → add_location：name 是地点本身，variant 是它的一种样子（「雨夜」「清晨」…），
  time_of_day 填时间，description 写环境与光线
· 关键道具 → add_prop：只建真的有戏份的，别把布景里每样东西都建成道具
{image_hint}
这一步只提这三种提案：先不要拆幕，也不要加镜头。"""

#: 第三步：拆幕。**只提 `add_scene`**——`set_link` 要真的幕 id，而这时候幕还没落库。
_AUTO_SCENES = """第三步：把核心剧本拆成幕（一幕 = 一个连续的时空段落）。

用 add_scene 提，每一幕：title 一句话说清这一幕发生了什么、summary 写剧情要点、
time_of_day 填时间、location_name 用剧本里的地点原文（上一步刚建的那些按名字就能对上）。

最多 {max_scenes} 幕。这一章的内容多于这个数时，只拆最重要的前 {max_scenes} 幕，
并在回答里说明剩下的还没拆。

**只提 add_scene**：不要带 shots，也不要提 set_link——幕之间默认硬切，衔接等幕真的建好了
再单独配（现在还没有幕 id 可用）。"""

#: 第四步：**按幕各来一轮**。一次把所有幕的镜头都拆完必然超时或被截断，
#: 所以这一步在 `autopilot()` 里循环，每一幕一句话、一次 `propose()`、一次 `apply()`。
_AUTO_SHOTS = """第四步（第 {index}/{total} 幕）：给这一幕拆分镜。

这一幕的 scene_id 是 {scene_id}，标题「{title}」。先用 get_scene 看它现在什么样，
再用 read_skill 取一份镜头提示词的写法（这一轮的镜头还没有指定首尾帧，取 ref 那一份）。

然后用 add_shot 提 {hint} 个镜头，每一镜：

· scene_id 一律填 {scene_id}
· title 一句话说清这一镜拍什么，description 写画面内容
· duration 2~8 秒
· camera 写景别（近景 / 中景 / 全景…），movement 写运镜（推 / 摇 / 固定…）
· character_names 用剧本里的人名原文列出这一镜谁出场（刚建好的角色会自动接上）
· camera_motion / visual_prompt / audio_dialogue 三段照 SKILL 的写法写，skill 填你取的那份

镜头按时间顺序提，不用填 position。这一幕有关键道具出场时，最后再提一条
set_scene_props（道具是挂在幕上的，镜头上没有这一项）。

**别提 set_scene_prompt**（它会把这一幕每个镜头的 prompt 覆盖成同一段），也别改别的幕。"""

#: 一幕拆几个镜头。给的是范围而不是定数：一幕本来就有长有短，钉死一个数只会逼模型
#: 把两镜的内容硬塞进一镜，或者为了凑数编出没有的画面。
_AUTO_SHOT_HINT = "3~6"


def _drop_image_prompts(ops: list[dict[str, Any]]) -> int:
    """把这一批提案里的「顺带出一张参考图」摘掉，回摘了几条。

    **确定性地做，不靠提示词自觉**：提示词里那句「不要写 image_prompt」是建议，
    模型照旧可能写；而这里摘掉之后 `_maybe_image()` 就一定不会入队
    （它认的就是 `after["image_prompt"]`）。素材照旧建成——关掉的是图，不是素材。
    """
    dropped = 0
    for op in ops:
        after = op.get("after")
        if isinstance(after, dict) and str(after.get("image_prompt") or "").strip():
            after["image_prompt"] = ""
            after["generate_image"] = False
            dropped += 1
    return dropped


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
        auto = self.auto_status()
        return {
            "turns": [{**as_dict(row), "content": load_json(row.content_json, {})} for row in rows],
            "llm": llm.status(),
            "attach": self.attach_limits(),
            "auto": auto,
            "note": (
                "免确认模式开着：这一栏产出的提案会在同一个请求里直接落库。"
                if auto["apply"]
                else "提案只是提案：没点「采用」之前，数据库里什么都没变。"
            ),
        }

    @staticmethod
    def auto_status() -> dict[str, Any]:
        """免确认与一键全流程现在是什么口径。**只有设置页那一份**（`appsettings` 的
        `director` 组），前端不记第二份默认值。

        协作栏要用它改口：免确认开着时「提案不是改动」这句话就不成立了，界面必须先把这件事
        说出来（硬约束 4）；「一键全流程」那颗按钮也照 `apply` 禁用并写清为什么——后端本来
        就会拒（`_require_auto_apply()`），但让用户点一下才知道不如一开始就说明白。
        `image_configured` 跟着 `registry.image_configured()` 那一份口径，不在前端判第二遍。
        """
        return {
            "apply": settings.director_auto_apply,
            "image": settings.director_auto_image,
            "max_scenes": max(1, int(settings.director_max_scenes)),
            "image_configured": registry.image_configured(),
            "stages": [{"stage": stage, "label": label} for stage, label in AUTO_STAGES],
            "hint": (
                "一键全流程会照这四步连着落库：核心剧本 → 人物 / 地点 / 道具 → 拆幕 → 按幕拆分镜。"
                if settings.director_auto_apply
                else "一键全流程要先在设置页的「AI 导演」里打开免确认模式——它要连着落四批数据。"
            ),
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

        **免确认模式开着时，产出的提案在这同一个请求里就落库**（`_auto_applied()`，走的还是
        `apply()` 那一份实现）。返回体里因此多 `auto_applied` / `applied` / `failed` / `count`。
        """
        text = await self.stream_precheck(pid, message)
        await self._add_turn(pid, "user", {"text": text})
        history = await self._llm_history(pid)
        out = await agent.propose(pid, text, history, scope=scope)
        turns = await self._persist(pid, out)
        auto = await self._auto_applied(pid, out["ops"])
        if out["over_limit"]:
            # 提案已经落好了才报错：转够了轮数不代表这几条不能用。
            raise self._over_limit(len(out["ops"]), auto["auto_applied"])
        return {"turns": turns, "ops": out["ops"], "degraded": out["degraded"], **auto}

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
        auto = await self._auto_applied(pid, out["ops"])
        if out["over_limit"]:
            yield {
                "event": "error",
                "data": {
                    "error": self._over_limit(len(out["ops"]), auto["auto_applied"]).to_dict()
                },
            }
            return
        yield {
            "event": "done",
            "data": {
                "turns": turns,
                "ops": out["ops"],
                "degraded": out["degraded"],
                "rounds": out["rounds"],
                **auto,
            },
        }

    async def _auto_applied(self, pid: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        """免确认模式：把刚产出的提案在**同一个请求里**落库。

        **走的还是 `apply()` 那一份实现**——不是第二条写路径，所以校验、按名字接线、
        「素材建好了顺带排一张图」那些行为与用户手点「全部采用」逐字相同。
        关着（默认）或这一轮没有提案时什么都不做，只回一个 `auto_applied: False`
        让前端知道「没落库不是因为出错」。
        """
        if not settings.director_auto_apply or not ops:
            return {"auto_applied": False}
        done = await self.apply(pid, ops)
        log.info(
            "director.auto_applied",
            project_id=pid,
            count=done["count"],
            failed=len(done["failed"]),
        )
        return {"auto_applied": True, **done}

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
    def _over_limit(count: int, auto_applied: bool = False) -> AppError:
        """转满轮数：提案已经落好了才报这个错——转够了轮数不代表这几条不能用。

        第一条建议按免确认开没开分岔：自动落库之后还说「仍在右栏可以审阅」是句假话
        （右栏那几张卡这一刻已经落进库了），用户会去点一个不存在的按钮。
        """
        first = (
            f"已产出的 {count} 条提案**已经落库了**（免确认模式开着），到流程图上核对一下"
            if auto_applied
            else f"已产出的 {count} 条提案仍在右栏，可以照常审阅采用"
        )
        return AppError(
            ErrorCode.LLM_INVALID_OUTPUT,
            "AI 转了太多轮还没收尾",
            f"已经跑满 {agent.MAX_ROUNDS} 轮工具调用，这一轮就此停下。",
            [
                first,
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

    # --- 一键全流程（免确认模式下把四步连起来） ---

    async def autopilot(
        self,
        pid: str,
        text: str = "",
        *,
        replace_script: bool = False,
        auto_image: bool | None = None,
        max_scenes: int | None = None,
    ) -> dict[str, Any]:
        """一趟跑完四步：核心剧本 → 人物 / 地点 / 道具 → 拆幕 → 按幕拆分镜。

        `text` 是这一章的原文：非空就先存进 `Story.raw_text`（库里已经有**不同**的一份时
        绝不覆盖，报错并给出路）；空就用工程里已经有的那一份。`auto_image` / `max_scenes`
        留空表示跟随设置页那两项。

        **每一步都真落库**（所以要求免确认模式开着，见 `_require_auto_apply()`）：后一步要用
        前一步的 id——拆分镜要 `scene_id`，镜头里那几个人要接到刚建出来的形象上。

        **只在一行业务数据都还没落的时候抛错**：已经落过之后一律 200 + 回执里的
        `stages[].error` 与 `warnings`，照 `sequence.plan` / `packages.plan` 的老规矩
        「跳过不是失败，但必须说出来」。已经落的一律不回滚。

        拆分镜那几轮**不指望聊天记录记得住前面的结论**（`HISTORY_TURNS` 就 10 条），
        每一轮都让模型自己 `get_scene` 读现状，与用户手动一句一句说时走的是同一条路。
        """
        llm.require_configured()
        db_of(pid)  # 工程没打开就在这里 404，别等写到一半才发现
        self._require_auto_apply()
        want_image = settings.director_auto_image if auto_image is None else bool(auto_image)
        cap = max(1, int(settings.director_max_scenes if max_scenes is None else max_scenes))
        configured = registry.image_configured()
        source = await self._autopilot_source(pid, text, replace_script)

        stages: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        warnings: list[str] = []
        targets: list[dict[str, Any]] = []
        current = AUTO_STAGES[0]
        fail: AppError | None = None
        plan = [
            (AUTO_STAGES[0], _AUTO_DIGEST, False),
            (
                AUTO_STAGES[1],
                _AUTO_MATERIALS.format(image_hint=self._image_hint(configured, want_image)),
                not want_image,
            ),
            (AUTO_STAGES[2], _AUTO_SCENES.format(max_scenes=cap), False),
        ]

        def absorb(row: dict[str, Any]) -> None:
            stages.append(row)
            applied.extend(row["applied"])
            failed.extend(row["failed"])
            if row.get("warning"):
                warnings.append(f"{row['label']}：{row['warning']}")

        try:
            for (stage, label), prompt, strip in plan:
                current = (stage, label)
                absorb(await self._auto_step(pid, stage, label, prompt, strip_images=strip))
            made = [
                str(e["scene_id"])
                for e in stages[-1]["applied"]
                if e.get("op") == "add_scene" and e.get("scene_id")
            ]
            targets, why, total = await self._auto_shot_targets(pid, made, cap)
            if total > len(targets):
                warnings.append(
                    f"这一次只给前 {len(targets)} 幕拆了分镜（{why}一共 {total} 幕，上限 {cap}）："
                    "剩下的再点一次「一键全流程」就接着拆。"
                )
            for index, lane in enumerate(targets, 1):
                title = str(lane.get("title") or "")
                current = ("shots", f"拆分镜 · 第 {index} 幕「{title}」")
                absorb(
                    await self._auto_step(
                        pid,
                        "shots",
                        current[1],
                        _AUTO_SHOTS.format(
                            index=index,
                            total=len(targets),
                            scene_id=lane["id"],
                            title=title,
                            hint=_AUTO_SHOT_HINT,
                        ),
                    )
                )
        except AppError as exc:
            fail = exc
        except Exception as exc:  # noqa: BLE001 —— 归一成四要素，绝不静默
            log.warning("director autopilot failed: %s", exc)
            fail = AppError(
                ErrorCode.LLM_UNAVAILABLE,
                "一键全流程中断了",
                f"{type(exc).__name__}: {exc}",
                [
                    "重试一次（多半是连接或超时）",
                    "已经落进库的那几步不会消失：回执里写着走到哪一步",
                    "或在流程图上接着手动做——手动路径不依赖 LLM",
                ],
            )
        if fail is not None:
            if not applied:
                # 一行业务数据都还没落：给一个干净的四要素错误，别回一张空回执让用户自己猜
                raise fail
            stages.append(
                {
                    "stage": current[0],
                    "label": current[1],
                    "reply": "",
                    "ops": 0,
                    "applied": [],
                    "failed": [],
                    "count": 0,
                    "images_dropped": 0,
                    "error": fail.to_dict(),
                }
            )
            warnings.append(f"{current[1]}：这一步没做完（{fail.title}），前面几步已经落库了。")
        queued, skipped = self._image_tally(applied)
        log.info(
            "director.autopilot",
            project_id=pid,
            stages=len(stages),
            applied=len(applied),
            failed=len(failed),
            halted=fail is not None,
        )
        return {
            "auto_apply": True,
            "script": source,
            "stages": stages,
            "applied": applied,
            "failed": failed,
            "count": len(applied),
            "scenes": [{"id": r["id"], "title": r.get("title")} for r in targets],
            "warnings": warnings,
            "images": {
                "configured": configured,
                "auto": want_image,
                "queued": queued,
                "skipped": skipped,
                "dropped": sum(int(r.get("images_dropped") or 0) for r in stages),
            },
            "halted": fail is not None,
            "note": (
                "这四步是照免确认模式直接落库的：流程图上现在就能看到幕与镜头。"
                "参考图（如果排了）在底部控制台的任务框里跑；画面还没生成，"
                "要你自己在场景工作台上按下那一下。"
            ),
        }

    @staticmethod
    def _require_auto_apply() -> None:
        """一键全流程**以免确认模式开着为前提**，而且这句话必须在写第一行数据之前说。

        不是「顺便检查一下」：这一趟要连着落四批数据，拆分镜那一步得用前一步真落进库的
        幕 id。关着免确认时那几批只会变成右栏里几十张待审的卡，第四步拿不到 `scene_id`，
        整条链就断在中间——那种半成品比一个清楚的错误糟得多。
        """
        if settings.director_auto_apply:
            return
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "一键全流程要先开免确认模式",
            "这一趟会连着落四批数据（核心剧本 → 素材 → 幕 → 分镜），"
            "拆分镜那一步要用前一步真落进库的幕 id，所以不能只出提案。",
            [
                "去设置页的「AI 导演」打开「免确认模式（提案直接落库）」再点一次",
                "或照旧一句一句说——逐条审阅那条路一直都在，不依赖这个开关",
            ],
        )

    async def _autopilot_source(self, pid: str, text: str, replace: bool) -> dict[str, Any]:
        """确定这一趟读的是哪一份原文，**绝不悄悄盖掉工程里已经有的那份**。

        四种情况：给了原文而库里是空的（或显式勾了替换）→ 存进去；给了原文但库里已经有
        **不一样**的一份 → 报错并给两条出路（不带文字直接跑 / 勾替换）；没给而库里有 →
        就用库里那份；两边都空 → 报错说清剧本从哪里来。
        """
        fresh = str(text or "").strip()
        stored = str((await story.get_story(pid)).get("raw_text") or "").strip()
        if fresh and (not stored or replace or fresh == stored):
            if fresh != stored:
                await story.save_story(pid, {"raw_text": fresh})
            return {"chars": len(fresh), "saved": fresh != stored, "replaced": bool(stored)}
        if fresh:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "工程里已经有一份剧本原文",
                f"库里那份 {len(stored)} 字，这次带来的是 {len(fresh)} 字，两份不一样。"
                "剧本原文是这个工程的源头，不该被一次「一键全流程」悄悄换掉。",
                [
                    "不带文字直接跑：就按工程里那份原文拆",
                    "确实要换掉：勾上「替换工程里的剧本原文」再跑一次",
                ],
                {"stored_chars": len(stored), "given_chars": len(fresh)},
            )
        if not stored:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这个工程还没有剧本原文",
                "一键全流程的第一步是把剧本读完，没有原文就无从开始。",
                [
                    "把这一章的原文贴进输入框再点一次（会一并存进这个工程）",
                    "或在协作栏里一句一句说你想拍什么——那条路不需要原文",
                ],
            )
        return {"chars": len(stored), "saved": False, "replaced": False}

    @staticmethod
    def _image_hint(configured: bool, want: bool) -> str:
        """第二步那段提示词里关于「顺带出图」的一句话，按三种情况各说一种。

        没配出图服务时**照旧让模型把那句「长什么样」写出来**：素材照旧建成，图那一项在
        落库回执里写成 `image_skipped`（硬约束 2 的老作风）——用户配好服务之后在素材页
        点一下就能出，不必让 AI 再想一遍这张图该长什么样。
        """
        if not want:
            return "\n这一轮**不要**写 image_prompt：只建素材，参考图之后单独出。"
        head = (
            "\n每一条都带上 image_prompt（一句「长什么样」：外形、服装配色、材质、光线），"
            "skill 按素材类型选（人物 char_sheet / 地点 scene_simple / 道具 prop_ref）。"
        )
        if configured:
            return head + "落库时会顺带排一张参考图。"
        return head + "现在还没配出图服务，图这一次不会真出来——素材照旧建成，这句话先留着。"

    @staticmethod
    def _image_tally(rows: list[dict[str, Any]]) -> tuple[int, list[str]]:
        """数一遍「顺带排图」这件事的结果：排上了几张、跳过的原因各是什么。

        跳过的原文照抄 `_maybe_image()` 那句话——**不在这里重写一份措辞**，
        不然回执上那句和素材卡上那句会分叉。
        """
        queued = sum(1 for row in rows if row.get("job_id"))
        skipped = [str(row["image_skipped"]) for row in rows if row.get("image_skipped")]
        return queued, skipped

    async def _auto_step(
        self, pid: str, stage: str, label: str, prompt: str, *, strip_images: bool = False
    ) -> dict[str, Any]:
        """一步 = 一句话 → `propose()` → `_persist()` → `apply()`，与 `chat()` 逐字同构。

        阶段提示词是**普通 user message**（不是第二层系统提示词），所以它照样进
        `DirectorTurn`：用户回头能看到这一趟到底跟 AI 说了什么，刷新也不丢。

        **直接调 `apply()` 而不是 `_auto_applied()`**：进这个方法之前
        `_require_auto_apply()` 已经确认过意图，而用户完全可能在这一趟跑到一半时去把那个
        开关关掉——那会让第四步拿不到幕 id，整条链断在中间。

        转满轮数（`over_limit`）**不算这一步失败**：提案已经落好了，只把这件事写成
        `warning` 带回去，接着走下一步。
        """
        await self._add_turn(pid, "user", {"text": prompt, "autopilot": stage})
        history = await self._llm_history(pid)
        out = await agent.propose(pid, prompt, history, scope="flow")
        await self._persist(pid, out)
        ops: list[dict[str, Any]] = out["ops"]
        dropped = _drop_image_prompts(ops) if strip_images else 0
        done = await self.apply(pid, ops)
        row = {
            "stage": stage,
            "label": label,
            "reply": out["reply"],
            "ops": len(ops),
            "images_dropped": dropped,
            "degraded": out["degraded"],
            **done,
        }
        if out["over_limit"]:
            row["warning"] = (
                f"AI 在这一步转满了 {agent.MAX_ROUNDS} 轮工具调用才停下，"
                "已经产出的提案照旧落库了，但这一步可能没做完。"
            )
        return row

    async def _auto_shot_targets(
        self, pid: str, created: list[str], cap: int
    ) -> tuple[list[dict[str, Any]], str, int]:
        """第四步要给哪几幕拆分镜。**绝不动已经排过镜头的幕。**

        主路是「这一趟新建的那几幕」。上一步一幕都没新建时（比如剧本已经拆过了，用户只是
        想把分镜补齐）退到「库里还没有镜头的幕」——那也是用户点这一下时想要的东西，
        而已经有镜头的幕再拆一遍只会得到两套重复的分镜。
        """
        lanes = await story.storyboard(pid)
        rows = [lane for lane in lanes if lane["id"] in set(created)]
        why = "这一趟新建的幕"
        if not rows:
            rows = [
                lane
                for lane in lanes
                if not [s for s in lane.get("shots", []) if s.get("kind") != "transition"]
            ]
            why = "库里还没有镜头的幕"
        return rows[:cap], why, len(rows)

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
