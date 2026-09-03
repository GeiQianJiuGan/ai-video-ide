"""AI 导演 agent：第一级（幕流程图）的协作者。

它做的事只有一件：**把「加一幕雨夜追车」这种话，变成一份可逐条审阅的提案。**

四条设计：

  1. **读立刻执行、写只进缓冲区。** 边界在 `tools.py`。模型可以随便看这个工程里有谁、
     有哪些地点、现在几幕；但它想改什么，只会变成提案条目。数据库是用户的。
  2. **上限是硬的。** `MAX_ROUNDS` 轮之后停手——但**已经产出的提案照旧保留**，
     由 `services/director.py` 先落成一条 proposal 记录再报错。转了六轮还没说完话
     不该让用户白等一场，也不该让它无限烧 token。
  3. **不支持 tools 的端也要能用。** Ollama 那类端退化成一次性 `complete_json()`：
     先把工程现状塞进提示里（模型没法自己去查了），让它一口气吐一个 ops 数组，
     再走同一个 `to_op()` 翻译。两条路产出的提案形状一模一样。
  4. **流式与不流式共用同一个循环。** 只有 `collaborate()` 一份实现，它是个事件流；
     `propose()` 只是把事件收干、把最后那条 `result` 拿出来。多轮工具调用的循环、
     工具失败怎么回喂、提案怎么攒——这些绝不能有第二份，两份必然分叉。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from app.ai import prompts
from app.ai.director.tools import TOOLS, WRITE_TOOLS, run_read, to_op, tool_specs
from app.ai.llm import client as llm
from app.core.logging import get_logger

log = get_logger("director")

#: 转多少轮就停手。一轮正常的拆解是「read_script 读一段 → read_skill 取一份写法 →
#: add_scene → 若干 add_shot → 说句话」，六轮根本不够；十六轮之后仍在绕圈就该停手了。
MAX_ROUNDS = 16

#: 角色与规则那一段是**可配的**（设置页「AI 提示词」→「AI 导演」），内置默认在
#: `app/ai/prompts.py::DIRECTOR_TASK`。这里只负责把它取出来用。
FALLBACK_SHAPE = """你现在没有工具可用。根据用户的要求与下面给出的工程现状，输出一个 JSON 对象：
{"reply": "一两句中文说明", "ops": [{"tool": "工具名", "args": {...}}]}。

可用的工具名与参数：
%s

所有 id 必须来自「工程现状」里出现过的 id。每个 args 里都要带 why 字段说明理由。
SKILL 全文这条路上取不到（这个端不支持工具），照清单里那句「什么时候用」判断该写哪种锚定语。
**这条路上也看不了任何一张图**（没有 look_at_image）：所以不要对素材提 set_description
（`target_kind="asset"`）——那只能是编的。缺素材描述时，在 reply 里告诉用户这个端不支持
看图补描述，问他要不要按剧本与已有设定推断着写，或者去素材页一张一张手填。
只输出 JSON，不要解释。"""

#: 退化路径喂进去的剧本原文最多多少字。这条路没法分段读，所以只给开头一段，
#: 并在提示里说清「这只是开头」——比悄悄截断然后让它以为拆完了要好。
FALLBACK_SCRIPT_CHARS = 4000


def _fallback_system(scope: str = "flow") -> str:
    """不支持工具的端用的系统提示词。

    形状那一段（`FALLBACK_SHAPE`）永远由代码拼在最后，用户改不到——和
    `prompts.breakdown()` 同一条规矩。工具清单也得由代码生成：写死一份迟早和
    `tools.py` 打架。
    """
    lines = "\n".join(
        f"- {name}: {spec['desc']} 参数：{'、'.join(spec['params']) or '（无）'}"
        for name, spec in TOOLS.items()
        if spec["kind"] == "write"
    )
    return f"{prompts.director(scope)}\n\n{FALLBACK_SHAPE % lines}"


async def _snapshot(pid: str) -> str:
    """退化路径用：模型没法自己查，就把现状喂给它。

    **剧本原文的开头一段也塞进来**：这条路没有 `read_script`，不给它原文就只能凭空编。
    只给开头是刻意的——整段塞进一次请求正是「一次性拆解」那个毛病。
    """
    data: dict[str, Any] = {
        "scenes": await run_read(pid, "list_scenes", {}),
        "characters": await run_read(pid, "list_characters", {}),
        "locations": await run_read(pid, "list_locations", {}),
        "props": await run_read(pid, "list_props", {}),
    }
    try:
        head = await run_read(pid, "read_script", {"offset": 0, "limit": FALLBACK_SCRIPT_CHARS})
    except Exception:  # noqa: BLE001 —— 没存剧本原文很正常，不是失败
        head = None
    if head:
        data["script_head"] = head
    return json.dumps(data, ensure_ascii=False)


async def propose(
    pid: str,
    message: str,
    history: list[dict[str, Any]],
    scope: str = "flow",
) -> dict[str, Any]:
    """跑一轮协作，**不落库**。返回 `{reply, ops, rounds, over_limit, degraded}`。

    非流式那条路（`POST /director/chat`）用它。实现只是把 `collaborate()` 的事件收干——
    循环只有那一份。
    """
    result: dict[str, Any] = {}
    async for event in collaborate(pid, message, history, scope=scope, live=False):
        if event["kind"] == "result":
            result = event["result"]
    return result


async def collaborate(
    pid: str,
    message: str,
    history: list[dict[str, Any]],
    scope: str = "flow",
    *,
    live: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """跑一轮协作，把过程当事件流吐出来。**一行库都不改。**

    事件（`kind`）：

      · `delta`   —— 模型正在写的文字增量（`live=False` 时一条都没有）；
      · `tool`    —— 一次工具调用的 `start` / `done`（`done` 带 `ok` 与失败标题）；
      · `op`      —— 新攒出的一条提案，**产出即可见**，不用等这一轮说完；
      · `result`  —— **必定收尾**，`propose()` 的那份返回值。

    `scope` 只透传给 `prompts.director()`（那一句「用户现在在哪一页」），不影响别的。
    `live=False` 走非流式的 `complete_tools()`：同一个循环、同一套工具执行，
    只是没有 delta——于是「提案不落库」这条边界只有一处实现，测试盯住它就够。
    """
    llm.require_configured()
    if not llm.supports_tools():
        async for event in _without_tools(pid, message, scope):
            yield event
        return

    messages: list[dict[str, Any]] = [{"role": "system", "content": prompts.director(scope)}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})
    ops: list[dict[str, Any]] = []
    #: 这一轮对话里**真的看过图**的那几个 asset_id（`look_at_image` 回了
    #: `looked_at_image=true` 才进来）。`set_description` 写到素材上时靠它判断
    #: 「这一句是看图看出来的吗」——不是的话提案上挂一句警告（`to_op(looked_at=…)`）。
    #: **只活在这一次请求里**：不落库、不跨会话，下一次 chat 得重新看一眼。
    looked: set[str] = set()
    #: 中途几轮的「我先看一下现状」也算它说过的话：用户在流里看见了，落库的记录里
    #: 就不该只剩最后一句，不然刷新页面等于把看过的内容擦掉。
    said: list[str] = []
    rounds = 0
    over_limit = True
    while rounds < MAX_ROUNDS:
        rounds += 1
        out: dict[str, Any] = {"content": "", "tool_calls": []}
        async for event in _one_round(messages, live):
            if event["kind"] == "final":
                out = event["out"]
            else:
                yield event
        calls = out["tool_calls"]
        text = str(out.get("content") or "").strip()
        if not calls:
            if text:
                said.append(text)
            over_limit = False
            break
        if text:
            said.append(text)
        messages.append(
            {
                "role": "assistant",
                "content": out.get("content") or "",
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                        },
                    }
                    for c in calls
                ],
            }
        )
        for call in calls:
            yield {"kind": "tool", "name": call["name"], "phase": "start"}
            done = await _run_one(pid, call, len(ops) + 1, looked)
            if done["op"] is not None:
                ops.append(done["op"])
                yield {"kind": "op", "op": done["op"]}
            yield {
                "kind": "tool",
                "name": call["name"],
                "phase": "done",
                "ok": done["ok"],
                "error": done["error"],
            }
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": done["text"]})
    yield {
        "kind": "result",
        "result": {
            "reply": "\n\n".join(said),
            "ops": ops,
            "rounds": rounds,
            "over_limit": over_limit,
            "degraded": False,
        },
    }


async def _one_round(messages: list[dict[str, Any]], live: bool) -> AsyncIterator[dict[str, Any]]:
    """问一次模型。yield 若干 `delta`，最后必定 yield 一条 `final`。"""
    if not live:
        out = await llm.complete_tools(messages, tool_specs())
        yield {
            "kind": "final",
            "out": {"content": out.get("content"), "tool_calls": out.get("tool_calls") or []},
        }
        return
    out = {"content": "", "tool_calls": []}
    async for event in llm.stream_tools(messages, tool_specs()):
        if event["type"] == "delta":
            yield {"kind": "delta", "text": event["text"]}
        elif event["type"] == "final":
            out = {"content": event.get("content"), "tool_calls": event.get("tool_calls") or []}
    yield {"kind": "final", "out": out}


async def _run_one(
    pid: str, call: dict[str, Any], seq: int, looked: set[str] | None = None
) -> dict[str, Any]:
    """执行一次工具调用。返回 `{text, op, ok, error}`——`text` 是回给模型看的那段。

    工具报错**不中断整轮**：把错误原样回给模型，它常常能自己纠正（比如换个对的 id）。
    一路抛出去只会让用户看到一条「AI 失败了」，什么也没拿到。

    `looked` 是调用方（`collaborate`）攒着的「这一轮真看过图的素材」，**这里会往里加**：
    `look_at_image` 真看到图时把那个 asset_id 记下来，于是后面的 `set_description`
    提案能诚实地说清「这一句是不是看图看出来的」。
    """
    name = call["name"]
    seen = looked if looked is not None else set()
    try:
        if name in WRITE_TOOLS:
            op = await to_op(pid, name, call["arguments"], seq, looked_at=seen)
            note = "已记入提案，尚未写入数据库；用户会逐条审阅。"
            if op["warnings"]:
                note += " 注意：" + "；".join(op["warnings"])
            return {"text": note, "op": op, "ok": True, "error": ""}
        out = await run_read(pid, name, call["arguments"])
        if name == "look_at_image" and isinstance(out, dict) and out.get("looked_at_image"):
            seen.add(str(out.get("asset_id") or ""))
        payload = json.dumps(out, ensure_ascii=False)
        return {"text": payload, "op": None, "ok": True, "error": ""}
    except Exception as exc:  # noqa: BLE001 —— 工具的任何失败都只是这一步失败
        log.info("director tool %s failed: %s", name, exc)
        title = str(getattr(exc, "title", type(exc).__name__))
        detail = str(getattr(exc, "detail", str(exc)))
        return {
            "text": f"这个工具失败了：{title}。{detail} 请换一种做法或先用读工具确认 id。",
            "op": None,
            "ok": False,
            "error": title,
        }


async def _without_tools(
    pid: str, message: str, scope: str = "flow"
) -> AsyncIterator[dict[str, Any]]:
    """不支持 function calling 的端：一次性产出 ops 数组，再走同一套翻译。

    它没有中途的文字增量（一次调用就出全部），但 `op` 事件照旧一条条给——
    协作栏那一侧于是不用为两条路各写一份渲染。
    """
    data = await llm.complete_json(
        _fallback_system(scope),
        f"工程现状：\n{await _snapshot(pid)}\n\n用户的要求：{message}",
    )
    ops: list[dict[str, Any]] = []
    notes: list[str] = []
    for raw in data.get("ops") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("tool") or "")
        args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
        try:
            # `looked_at` 刻意不给：这条路一次调用就出全部 ops，**从来没有看过任何一张图**。
            # 于是素材描述那种提案会自带「这一句不是看图看出来的」那句警告——正是实话。
            op = await to_op(pid, name, args, len(ops) + 1)
        except Exception as exc:  # noqa: BLE001 —— 一条不成立不该毁掉其余几条
            notes.append(f"{name}：{getattr(exc, 'title', type(exc).__name__)}")
            continue
        ops.append(op)
        yield {"kind": "op", "op": op}
    reply = str(data.get("reply") or "").strip()
    if notes:
        tail = f"有 {len(notes)} 条没能成立：" + "；".join(notes)
        reply = f"{reply} {tail}" if reply else tail
    if reply:
        yield {"kind": "delta", "text": reply}
    yield {
        "kind": "result",
        "result": {
            "reply": reply,
            "ops": ops,
            "rounds": 1,
            "over_limit": False,
            "degraded": True,
        },
    }
