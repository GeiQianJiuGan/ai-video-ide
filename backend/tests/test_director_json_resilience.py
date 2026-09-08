"""测试 AI 导演台对 JSON 参数格式异常、截断与容错续行能力的鲁棒性。

核心覆盖：
  1. json_repair 对 Markdown 代码块包裹、未转义换行、末尾悬挂逗号的清理与修复；
  2. json_repair 对因 Token 溢出导致的截断 JSON 的自动闭合修复；
  3. AI 导演在工具参数严重损坏时，向模型提供详细错误原因与分批解决指引，不强行终止；
  4. 模型收到错误反馈后在下一轮自纠并成功提出提案；
  5. 未知工具调用的防御与提示；
  6. 连续参数错误时的优雅收尾保护。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai.llm import client as llm
from app.ai.llm import json_repair, protocols
from app.core.config import settings

API = "/api/v1"


def test_json_repair_syntax_and_markdown() -> None:
    """测试常规 Markdown 剥离、未转义换行、悬挂逗号修复。"""
    # 1. 带 Markdown 代码块
    raw_md = "```json\n{\"title\": \"雨夜追车\", \"duration\": 5}\n```"
    parsed, err = json_repair.parse_lenient_json(raw_md)
    assert err is None
    assert parsed == {"title": "雨夜追车", "duration": 5}

    # 2. 带尾部悬挂逗号
    raw_trailing = '{"a": 1, "b": [2, 3,], "c": {"d": "ok",},}'
    parsed, err = json_repair.parse_lenient_json(raw_trailing)
    assert err is None
    assert parsed == {"a": 1, "b": [2, 3], "c": {"d": "ok"}}

    # 3. 带 Python 字面量
    raw_py = '{"active": True, "disabled": False, "meta": None}'
    parsed, err = json_repair.parse_lenient_json(raw_py)
    assert err is None
    assert parsed == {"active": True, "disabled": False, "meta": None}


def test_json_repair_truncated_structures_are_rejected() -> None:
    """测试因 Token 截断导致的不完整 JSON 直接判定解析失败，不盲目补全残缺数据。"""
    # 1. 字符串未闭合、对象未闭合 -> 判定为未完整生成，返回错误
    truncated_1 = '{"title": "雨夜追车", "why": "剧情需要动作戏'
    parsed, err = json_repair.parse_lenient_json(truncated_1)
    assert parsed is None
    assert err is not None
    assert "Unterminated string" in err

    # 2. 嵌套数组被截断在中间某元素 -> 判定失败
    truncated_2 = '{"scene_id": "sc_1", "shots": [{"title": "全景", "duration": 4}, {"title": "近景'
    parsed, err = json_repair.parse_lenient_json(truncated_2)
    assert parsed is None
    assert err is not None

    # 3. 截断在孤立键上 -> 判定失败
    truncated_3 = '{"title": "雨夜追车", "why": "需要过渡", "dangling":'
    parsed, err = json_repair.parse_lenient_json(truncated_3)
    assert parsed is None
    assert err is not None


def test_protocol_tool_args_marks_truncated_as_incomplete() -> None:
    """测试 protocols._tool_args 将被截断的不完整参数标记为 __incomplete__。"""
    raw_truncated = '{"title": "雨夜追车", "why": "补充动作场面'
    res = protocols._tool_args("add_scene", raw_truncated)
    assert res.get("__incomplete__") is True
    assert res.get("__raw__") == raw_truncated
    assert "Unterminated string" in res.get("__error__", "")


def test_protocol_tool_args_records_error_when_unrepairable() -> None:
    """彻底无法解析的损坏参数返回 __incomplete__ 与详细原因。"""
    garbage = ":::not a json at all:::"
    res = protocols._tool_args("add_scene", garbage)
    assert res.get("__incomplete__") is True
    assert res.get("__raw__") == garbage
    assert res.get("__error__")


def test_director_discards_truncated_arguments_and_asks_for_regeneration(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型输出截断的参数时，损坏数据被丢弃，AI 收到错误提示并在下一轮重新生成完整参数。"""
    tool_inputs: list[list[dict[str, Any]]] = []

    async def fake_stream_complete(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        tool_inputs.append(messages)
        if len(tool_inputs) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "c_trunc",
                        "name": "add_scene",
                        # 模拟模型输出截断
                        "arguments": '{"title": "被截断的幕", "why": "写到一半突然截断',
                    }
                ],
            }
        elif len(tool_inputs) == 2:
            # 确认模型收到丢弃与重新生成指引
            tool_msg = next((m for m in messages if m.get("role") == "tool"), None)
            assert tool_msg is not None
            assert "该次损坏调用已直接丢弃" in str(tool_msg.get("content"))
            return {
                "content": "刚才输出被截断，现在重新完整生成：",
                "tool_calls": [
                    {
                        "id": "c_full",
                        "name": "add_scene",
                        "arguments": {"title": "重新生成的完整幕", "why": "完整无损"},
                    }
                ],
            }
        return {"content": "已添加。", "tool_calls": []}

    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "fake-model")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(llm, "complete_tools", fake_stream_complete)

    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "加一幕"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["ops"]) == 1
    assert body["ops"][0]["op"] == "add_scene"
    assert body["ops"][0]["after"]["title"] == "重新生成的完整幕"
    assert body["ops"][0]["why"] == "完整无损"


def test_director_informs_ai_of_malformed_json_and_continues(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """当模型工具参数彻底损坏时，回传错误原因与指导建议，模型在下一轮自纠并成功产生提案。"""
    tool_inputs: list[list[dict[str, Any]]] = []

    async def fake_complete(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        # 记录每轮收到的消息
        tool_inputs.append(messages)
        if len(tool_inputs) == 1:
            # 第 1 轮：模型输出彻底损坏的非 JSON 参数
            return {
                "content": "我正在尝试添加镜头...",
                "tool_calls": [
                    {
                        "id": "call_bad",
                        "name": "add_scene",
                        "arguments": "!!! 완전히 깨진 매개변수 [not a valid json] !!!",
                    }
                ],
            }
        elif len(tool_inputs) == 2:
            # 第 2 轮：模型收到第 1 轮的错误反馈，自纠并返回正确的参数
            # 断言第 2 轮 messages 里包含给模型的错误原因回传
            tool_msg = next((m for m in messages if m.get("role") == "tool"), None)
            assert tool_msg is not None, "必须有 role: tool 的错误反馈消息"
            assert "【参数不完整 / 格式损坏】" in str(tool_msg.get("content"))
            assert "该次损坏调用已直接丢弃" in str(tool_msg.get("content"))
            assert "add_scene" in str(tool_msg.get("content"))

            return {
                "content": "刚才参数格式有误被截断，现已调整为标准格式重新调用：",
                "tool_calls": [
                    {
                        "id": "call_good",
                        "name": "add_scene",
                        "arguments": {"title": "自纠成功的幕", "why": "收到错误原因后重试"},
                    }
                ],
            }
        else:
            return {"content": "已成功提议。", "tool_calls": []}

    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "fake-model")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(llm, "complete_tools", fake_complete)

    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "帮我加一幕"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 验证对话没有强行终止，并且最终成功获得了自纠后的提案
    assert len(body["ops"]) == 1
    assert body["ops"][0]["after"]["title"] == "自纠成功的幕"
    assert body["ops"][0]["why"] == "收到错误原因后重试"


def test_director_unknown_tool_feedback(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """当模型幻觉调用不存在的工具时，返回明确的未知工具错误反馈并列出可用工具。"""
    seen_messages: list[list[dict[str, Any]]] = []

    async def fake_unknown(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        seen_messages.append(messages)
        if len(seen_messages) == 1:
            return {
                "content": "",
                "tool_calls": [{"id": "c_unknown", "name": "make_movie_magic", "arguments": {}}],
            }
        elif len(seen_messages) == 2:
            tool_msg = next((m for m in messages if m.get("role") == "tool"), None)
            assert tool_msg is not None
            assert "不存在名为「make_movie_magic」的工具" in str(tool_msg.get("content"))
            assert "add_scene" in str(tool_msg.get("content"))
            return {
                "content": "改用正确的工具：",
                "tool_calls": [
                    {
                        "id": "c_correct",
                        "name": "add_scene",
                        "arguments": {"title": "已知工具的幕", "why": "纠正工具名"},
                    }
                ],
            }
        return {"content": "已添加完成。", "tool_calls": []}

    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "fake-model")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(llm, "complete_tools", fake_unknown)

    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "调用魔法工具"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["ops"]) == 1
    assert body["ops"][0]["after"]["title"] == "已知工具的幕"


def test_director_consecutive_errors_guard(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """当模型连续 3 轮都输出破损非 JSON 参数时，触发安全熔断并友好收尾，不无限循环。"""
    round_count = 0

    async def fake_bad_loop(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        nonlocal round_count
        round_count += 1
        return {
            "content": f"第 {round_count} 次尝试",
            "tool_calls": [
                {
                    "id": f"call_{round_count}",
                    "name": "add_scene",
                    "arguments": f"broken_syntax_{round_count}",
                }
            ],
        }

    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "fake-model")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(llm, "complete_tools", fake_bad_loop)

    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "一直输出坏参数"})
    assert resp.status_code == 201, resp.text
    # 验证在达到 3 次连续错误后停止，没有跑满 MAX_ROUNDS (16)
    assert round_count == 3
    turns = client.get(f"{API}/projects/{pid}/director").json()["turns"]
    assistant_turn = next(t for t in reversed(turns) if t["role"] == "assistant")
    assert "连续多次生成的参数均因格式错误或截断未能解析" in assistant_turn["content"]["text"]
