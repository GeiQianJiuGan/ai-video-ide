"""AI 编剧 / 导演的流式那条路（`POST /director/chat/stream`）。

这个文件盯的还是**边界**，不是「AI 说得好不好」：

  1. **流式与不流式落的是同一份记录**：一轮跑完，`DirectorTurn` 里该有 assistant +
     proposal 两条，刷新页面提案还在；
  2. **提案照旧一行库都不改**：流里 `op` 事件一条条来，但幕数一个都不能多；
  3. **能先报的错先报**：消息是空的 / 没配 LLM，拿到的是正常的 JSON 四要素错误，
     不是一个 200 然后夹在事件流里的 error——前端不该为此写两套错误处理；
  4. **半路挂了也不白干**：已经说过的话与攒出的提案先落成记录，再吐 `error`；
     `done` 与 `error` 互斥且必有其一。

LLM 一律 monkeypatch 掉。流式那条的桩子刻意**复用非流式那串回合**（过一遍
`protocols.one_chunk`）：两条路的期望输出于是不可能在测试里分叉。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai.llm import client as llm
from app.ai.llm import protocols
from tests.conftest import error_of
from tests.test_director_agent import call, scene, use_fake_llm

API = "/api/v1"


def use_fake_stream(monkeypatch: pytest.MonkeyPatch, rounds: list[dict[str, Any]]) -> list[int]:
    """把两条路一起换掉：非流式走 `complete_tools`，流式把同一份回合吐成事件。"""
    calls = use_fake_llm(monkeypatch, rounds)

    async def fake_stream(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        for event in protocols.one_chunk(await llm.complete_tools(messages, tools)):
            yield event

    monkeypatch.setattr(llm, "stream_tools", fake_stream)
    return calls


def stream(
    client: TestClient, pid: str, message: str, scope: str = "script"
) -> list[tuple[str, Any]]:
    """跑一轮流式，把 `(event, data)` 按到达顺序收下来。"""
    url = f"{API}/projects/{pid}/director/chat/stream"
    with client.stream("POST", url, json={"message": message, "scope": scope}) as resp:
        assert resp.status_code == 200, resp.read()
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers.get("x-accel-buffering") == "no"
        out: list[tuple[str, Any]] = []
        name = ""
        for raw in resp.iter_lines():
            line = raw.rstrip("\r")
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                out.append((name, json.loads(line[5:].strip())))
    return out


def test_stream_narrates_then_proposes_and_lands_nothing(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事件按顺序到、提案一行库都不改、落的记录和不流式那条一模一样。"""
    scene(client, pid, "第一幕")
    use_fake_stream(
        monkeypatch,
        [
            {"content": "我先看一下现在几幕。", "tool_calls": [call("list_scenes")]},
            {
                "content": "",
                "tool_calls": [call("add_scene", title="雨夜追车", why="第一幕之后缺一个动作段落")],
            },
            {"content": "提了一条：加一幕雨夜追车。", "tool_calls": []},
        ],
    )
    events = stream(client, pid, "在第一幕后面加一幕雨夜追车")
    kinds = [name for name, _ in events]
    #: 第一轮先说话再查现状；第二轮一个字都没说，直接提一条（`op` 夹在 start 与 done 之间，
    #: 于是「提案产出即可见」不用等这一轮说完）；第三轮说完总结、不再调工具，收尾。
    assert kinds == [
        "delta",
        "tool",
        "tool",
        "tool",
        "op",
        "tool",
        "delta",
        "done",
    ], kinds

    tools = [d for name, d in events if name == "tool"]
    assert [t["name"] for t in tools] == ["list_scenes", "list_scenes", "add_scene", "add_scene"]
    assert [t["phase"] for t in tools] == ["start", "done", "start", "done"]
    assert all(t["ok"] for t in tools if t["phase"] == "done")

    ops = [d for name, d in events if name == "op"]
    assert len(ops) == 1
    assert ops[0]["op"] == "add_scene" and ops[0]["before"] is None
    assert ops[0]["after"]["title"] == "雨夜追车"

    done = [d for name, d in events if name == "done"]
    assert len(done) == 1 and done[0]["degraded"] is False and done[0]["rounds"] == 3
    assert [t["role"] for t in done[0]["turns"]] == ["assistant", "proposal"]
    assert len(done[0]["ops"]) == 1
    assert not [name for name, _ in events if name == "error"], "有 done 就不该再有 error"

    # 库里一行没变
    assert len(client.get(f"{API}/projects/{pid}/scenes").json()) == 1

    # 刷新页面提案还在，中途说过的话也没被最后一句盖掉
    history = client.get(f"{API}/projects/{pid}/director").json()
    assert [t["role"] for t in history["turns"]] == ["user", "assistant", "proposal"]
    said = str(history["turns"][1]["content"]["text"])
    assert "我先看一下现在几幕。" in said and "提了一条" in said
    assert len(history["turns"][2]["content"]["ops"]) == 1


def test_stream_reports_a_failed_tool_without_ending_the_round(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具挂了只是 `tool` 事件上的 `ok=false`，这一轮照旧往下走。"""
    use_fake_stream(
        monkeypatch,
        [
            {"content": "", "tool_calls": [call("get_scene", scene_id="scn_nope")]},
            {"content": "那一幕不在，我先不动。", "tool_calls": []},
        ],
    )
    events = stream(client, pid, "改一下第三幕")
    failed = [d for name, d in events if name == "tool" and d["phase"] == "done"]
    assert len(failed) == 1 and failed[0]["ok"] is False
    assert failed[0]["error"], "失败的那一条必须带标题，前端要显示出来"
    assert [name for name, _ in events][-1] == "done"


def test_empty_message_fails_before_the_stream_opens(client: TestClient, pid: str) -> None:
    """开流之前能报的错就别等到流里报：这里是正常的 JSON 四要素错误。"""
    resp = client.post(f"{API}/projects/{pid}/director/chat/stream", json={"message": "   "})
    assert resp.status_code == 422
    assert not resp.headers["content-type"].startswith("text/event-stream")
    assert error_of(resp)["title"] == "说点什么"


def test_stream_without_llm_points_at_the_manual_path(client: TestClient, pid: str) -> None:
    """默认 provider = none。流式那条和不流式那条报的是同一件事。"""
    resp = client.post(f"{API}/projects/{pid}/director/chat/stream", json={"message": "加一幕"})
    assert resp.status_code == 503
    err = error_of(resp)
    assert err["code"] == "LLM_UNAVAILABLE"
    assert any("手动" in s for s in err["suggestions"])


def test_mid_flight_failure_keeps_what_it_already_proposed(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二轮断线：第一轮攒出的提案照旧落成记录，再吐 `error`。"""
    scene(client, pid, "第一幕")
    use_fake_stream(
        monkeypatch,
        [
            {
                "content": "先加一幕。",
                "tool_calls": [call("add_scene", title="雨夜追车", why="缺动作段落")],
            }
        ],
    )
    real = llm.stream_tools
    seen: list[int] = []

    async def flaky(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        seen.append(1)
        if len(seen) > 1:
            raise RuntimeError("连接被对面掐了")
        async for event in real(messages, tools):
            yield event

    monkeypatch.setattr(llm, "stream_tools", flaky)

    events = stream(client, pid, "加一幕雨夜追车")
    assert [name for name, _ in events][-1] == "error"
    assert not [name for name, _ in events if name == "done"], "有 error 就不该再有 done"
    err = [d for name, d in events if name == "error"][0]["error"]
    assert {"code", "title", "detail", "suggestions"} <= set(err)
    assert any("仍在右栏" in s for s in err["suggestions"])

    # 提案没白干，库里也一行没变
    history = client.get(f"{API}/projects/{pid}/director").json()
    assert [t["role"] for t in history["turns"]] == ["user", "assistant", "proposal"]
    assert len(history["turns"][2]["content"]["ops"]) == 1
    assert len(client.get(f"{API}/projects/{pid}/scenes").json()) == 1


def test_over_limit_is_an_error_event_after_the_proposals_are_saved(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """转满轮数：先把提案落成记录，再用 `error` 事件收尾（不流式那条是抛异常）。"""
    scene(client, pid, "第一幕")
    use_fake_stream(
        monkeypatch,
        [{"content": "", "tool_calls": [call("add_scene", title="又一幕", why="不停手")]}],
    )
    events = stream(client, pid, "一直加")
    assert [name for name, _ in events][-1] == "error"
    err = [d for name, d in events if name == "error"][0]["error"]
    assert err["code"] == "LLM_INVALID_OUTPUT"
    assert "转了太多轮" in err["title"]
    assert any("仍在右栏" in s for s in err["suggestions"])
    assert len([1 for name, _ in events if name == "op"]) >= 1
    history = client.get(f"{API}/projects/{pid}/director").json()
    assert history["turns"][-1]["role"] == "proposal"
    assert len(client.get(f"{API}/projects/{pid}/scenes").json()) == 1


def test_one_chunk_fakes_a_stream_for_ends_that_cannot_do_it() -> None:
    """不支持流式的端不是「用不了」：整段文字算一块 delta，`final` 必定收尾。"""
    events = protocols.one_chunk({"content": "一整段话", "tool_calls": []})
    assert events[0] == {"type": "delta", "text": "一整段话"}
    assert events[-1]["type"] == "final" and events[-1]["tool_calls"] == []
    # 一个字都没说、只调了工具的那一轮：没有 delta，但 final 照旧在
    only_tools = protocols.one_chunk({"content": "", "tool_calls": [{"id": "c1"}]})
    assert [e["type"] for e in only_tools] == ["final"]
    assert only_tools[0]["tool_calls"] == [{"id": "c1"}]
