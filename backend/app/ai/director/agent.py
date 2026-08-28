"""AI 导演 agent：第一级（幕流程图）的协作者。

它做的事只有一件：**把「加一幕雨夜追车」这种话，变成一份可逐条审阅的提案。**

三条设计：

  1. **读立刻执行、写只进缓冲区。** 边界在 `tools.py`。模型可以随便看这个工程里有谁、
     有哪些地点、现在几幕；但它想改什么，只会变成提案条目。数据库是用户的。
  2. **上限是硬的。** `MAX_ROUNDS` 轮之后停手——但**已经产出的提案照旧保留**，
     由 `services/director.py` 先落成一条 proposal 记录再报错。转了六轮还没说完话
     不该让用户白等一场，也不该让它无限烧 token。
  3. **不支持 tools 的端也要能用。** Ollama 那类端退化成一次性 `complete_json()`：
     先把工程现状塞进提示里（模型没法自己去查了），让它一口气吐一个 ops 数组，
     再走同一个 `to_op()` 翻译。两条路产出的提案形状一模一样。
"""

from __future__ import annotations

import json
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
    """跑一轮协作。返回 `{reply, ops, rounds, over_limit, degraded}`，**不落库**。

    `scope` 只透传给 `prompts.director()`（那一句「用户现在在哪一页」），不影响别的。
    """
    llm.require_configured()
    if not llm.supports_tools():
        return await _propose_without_tools(pid, message, scope)

    messages: list[dict[str, Any]] = [{"role": "system", "content": prompts.director(scope)}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})
    ops: list[dict[str, Any]] = []
    reply = ""
    rounds = 0
    over_limit = True
    while rounds < MAX_ROUNDS:
        rounds += 1
        out = await llm.complete_tools(messages, tool_specs())
        calls = out["tool_calls"]
        if not calls:
            reply = str(out.get("content") or "").strip()
            over_limit = False
            break
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
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": await _run_one(pid, call, ops),
                }
            )
    return {
        "reply": reply,
        "ops": ops,
        "rounds": rounds,
        "over_limit": over_limit,
        "degraded": False,
    }


async def _run_one(pid: str, call: dict[str, Any], ops: list[dict[str, Any]]) -> str:
    """执行一次工具调用，返回给模型看的那段文本。

    工具报错**不中断整轮**：把错误原样回给模型，它常常能自己纠正（比如换个对的 id）。
    一路抛出去只会让用户看到一条「AI 失败了」，什么也没拿到。
    """
    name = call["name"]
    try:
        if name in WRITE_TOOLS:
            op = await to_op(pid, name, call["arguments"], len(ops) + 1)
            ops.append(op)
            note = "已记入提案，尚未写入数据库；用户会逐条审阅。"
            if op["warnings"]:
                note += " 注意：" + "；".join(op["warnings"])
            return note
        return json.dumps(await run_read(pid, name, call["arguments"]), ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 —— 工具的任何失败都只是这一步失败
        log.info("director tool %s failed: %s", name, exc)
        title = getattr(exc, "title", type(exc).__name__)
        detail = getattr(exc, "detail", str(exc))
        return f"这个工具失败了：{title}。{detail} 请换一种做法或先用读工具确认 id。"


async def _propose_without_tools(pid: str, message: str, scope: str = "flow") -> dict[str, Any]:
    """不支持 function calling 的端：一次性产出 ops 数组，再走同一套翻译。"""
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
            ops.append(await to_op(pid, name, args, len(ops) + 1))
        except Exception as exc:  # noqa: BLE001 —— 一条不成立不该毁掉其余几条
            notes.append(f"{name}：{getattr(exc, 'title', type(exc).__name__)}")
    reply = str(data.get("reply") or "").strip()
    if notes:
        tail = f"有 {len(notes)} 条没能成立：" + "；".join(notes)
        reply = f"{reply} {tail}" if reply else tail
    return {"reply": reply, "ops": ops, "rounds": 1, "over_limit": False, "degraded": True}
