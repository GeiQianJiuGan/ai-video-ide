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

from app.ai.director.tools import TOOLS, WRITE_TOOLS, run_read, to_op, tool_specs
from app.ai.llm import client as llm
from app.core.logging import get_logger

log = get_logger("director")

#: 转多少轮就停手。六轮够它「看一眼现状 → 提三五条改动 → 说句话」，再多就是在绕圈。
MAX_ROUNDS = 6

SYSTEM = """你是一部 AI 生成短片的助理导演。你面对的是「幕流程图」：整部片子由若干幕组成，
每一幕挂着地点变体、出场角色、道具与镜头，幕与幕之间有明确的衔接方式
（cut 硬切 / transition 生成 1~2 秒转场 / tail_frame 上一幕真末帧当下一幕首帧）。

规则：
1. 动手之前先用读工具看清现状（list_scenes / list_characters / list_locations / list_props），
   不要凭空猜 id。所有 id 必须来自读工具的返回。
2. 你的写工具**不会改数据库**，只是提案，用户会逐条审阅。所以每条都要给 why：
   一句话说清为什么要这么改。
3. 宁少勿多：一次只提真正需要的几条。不要为了凑数改标题。
4. 用中文。最后用一两句话总结你提了什么，不要罗列 id。"""

FALLBACK_SYSTEM = """你是一部 AI 生成短片的助理导演。根据用户的要求与下面给出的工程现状，
输出一个 JSON 对象：{"reply": "一两句中文说明", "ops": [{"tool": "工具名", "args": {...}}]}。

可用的工具名与参数：
%s

所有 id 必须来自「工程现状」里出现过的 id。每个 args 里都要带 why 字段说明理由。
只输出 JSON，不要解释。"""


def _fallback_system() -> str:
    lines = [
        f"- {name}: {spec['desc']} 参数：{'、'.join(spec['params']) or '（无）'}"
        for name, spec in TOOLS.items()
        if spec["kind"] == "write"
    ]
    return FALLBACK_SYSTEM % "\n".join(lines)


async def _snapshot(pid: str) -> str:
    """退化路径用：模型没法自己查，就把现状喂给它。"""
    data = {
        "scenes": await run_read(pid, "list_scenes", {}),
        "characters": await run_read(pid, "list_characters", {}),
        "locations": await run_read(pid, "list_locations", {}),
        "props": await run_read(pid, "list_props", {}),
    }
    return json.dumps(data, ensure_ascii=False)


async def propose(pid: str, message: str, history: list[dict[str, Any]]) -> dict[str, Any]:
    """跑一轮协作。返回 `{reply, ops, rounds, over_limit, degraded}`，**不落库**。"""
    llm.require_configured()
    if not llm.supports_tools():
        return await _propose_without_tools(pid, message)

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}]
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


async def _propose_without_tools(pid: str, message: str) -> dict[str, Any]:
    """不支持 function calling 的端：一次性产出 ops 数组，再走同一套翻译。"""
    data = await llm.complete_json(
        _fallback_system(),
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
