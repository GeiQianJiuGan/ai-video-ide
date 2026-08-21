"""LLM 协议适配层 + 模型自动获取。

盯的是**我们这一侧的翻译**，不是某个模型的脾气：所有出网请求都从
`protocols._client` 这个唯一出口换成 `httpx.MockTransport`，一个字节都不出机器。

四件事：

  1. **模型列表认得出四种方言**——「自动获取」的全部价值就是省掉手抄模型名，
     Gemini 的 `models/` 前缀、Anthropic 的 display_name、Ollama 的体积都得对；
  2. **密钥只走请求头，绝不进 URL**——进了 URL 就会跟着日志与四要素错误一起漏出去；
  3. **翻译不能让请求 400**：Anthropic 的 system 要提到顶层、相邻工具结果并成一条；
     Gemini 的 `functionResponse` 认名字不认 id，无参工具要整个省掉 `parameters`；
  4. **先试再存**：`POST /settings/models` 带的是还没保存的那份配置，它绝不落盘。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai.llm import protocols
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from tests.conftest import error_of

API = "/api/v1"
#: 故意是个一眼能认出来的假密钥：断言「它没出现在 URL / 响应里」时看得清。
KEY = "sk-never-leaks-9x7"


def fake_http(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """换掉唯一的 HTTP 出口，并把发出去的请求都记下来。"""
    seen: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def build(timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=timeout)

    monkeypatch.setattr(protocols, "_client", build)
    return seen


def replies(payload: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=payload)


def cfg(name: str, **over: Any) -> protocols.LlmConfig:
    """地址一律留空，于是同时测到「留空就用协议默认地址」。"""
    return protocols.config(provider=name, base_url=over.pop("base_url", ""), api_key=KEY, **over)


def tool_call(cid: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


#: 一轮真实的协作：system + user + （assistant 带两个工具调用）+ 两条工具结果。
#: 这是最难翻的形状——两家都要求把那两条结果并成一条消息。
CONVERSATION: list[dict[str, Any]] = [
    {"role": "system", "content": "你是导演助理"},
    {"role": "user", "content": "补一幕"},
    {
        "role": "assistant",
        "content": "先看看现状",
        "tool_calls": [
            tool_call("c1", "list_scenes", {}),
            tool_call("c2", "get_scene", {"scene_id": "scn_1"}),
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "[]"},
    {"role": "tool", "tool_call_id": "c2", "content": "{}"},
]

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_scenes",
            "description": "列出所有幕",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scene",
            "description": "看一幕",
            "parameters": {
                "type": "object",
                "properties": {"scene_id": {"type": "string"}},
                "required": ["scene_id"],
            },
        },
    },
]


@pytest.mark.parametrize(
    ("name", "payload", "expected", "path"),
    [
        (
            "openai_compatible",
            # 自建端有直接给字符串数组的；重复的那条只留一次
            {"data": [{"id": "gpt-4o"}, "qwen-max", {"id": "gpt-4o"}]},
            [("gpt-4o", "gpt-4o"), ("qwen-max", "qwen-max")],
            "/models",
        ),
        (
            "anthropic",
            {"data": [{"id": "claude-opus-4-1", "display_name": "Claude Opus 4.1"}]},
            [("claude-opus-4-1", "Claude Opus 4.1")],
            "/models",
        ),
        (
            "gemini",
            {
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "displayName": "Gemini 2.5 Pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    # 只会做 embedding 的模型列出来只会让人挑错
                    {
                        "name": "models/text-embedding-004",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
            [("gemini-2.5-pro", "Gemini 2.5 Pro")],
            "/models",
        ),
        (
            "ollama",
            {"models": [{"name": "qwen3:8b", "size": 5 * 1024**3}]},
            [("qwen3:8b", "qwen3:8b · 5.0 GB")],
            "/api/tags",
        ),
    ],
)
async def test_model_listing_speaks_each_dialect(
    name: str,
    payload: Any,
    expected: list[tuple[str, str]],
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = fake_http(monkeypatch, replies(payload))
    proto = protocols.BY_NAME[name]
    rows = await proto.list_models(cfg(name))
    assert [(r["id"], r["label"]) for r in rows] == expected
    assert seen[0].url.path.endswith(path)
    assert seen[0].url.host == httpx.URL(proto.default_base_url).host, "留空地址就该用协议默认地址"


@pytest.mark.parametrize(
    ("name", "header"),
    [
        ("openai_compatible", "authorization"),
        ("anthropic", "x-api-key"),
        ("gemini", "x-goog-api-key"),
    ],
)
async def test_the_key_never_rides_in_the_url(
    name: str, header: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = fake_http(monkeypatch, replies({"data": [], "models": []}))
    await protocols.BY_NAME[name].list_models(cfg(name))
    request = seen[0]
    assert KEY in request.headers[header]
    assert KEY not in str(request.url), "密钥进了 URL 就会跟着日志与报错一起漏出去"


async def test_anthropic_hoists_system_and_merges_tool_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = fake_http(
        monkeypatch,
        replies(
            {
                "content": [
                    {"type": "text", "text": "提一条"},
                    {
                        "type": "tool_use",
                        "id": "c3",
                        "name": "add_scene",
                        "input": {"title": "雨夜追车"},
                    },
                ]
            }
        ),
    )
    out = await protocols.BY_NAME["anthropic"].complete_tools(
        cfg("anthropic", model="claude-opus-4-1"), CONVERSATION, TOOLS
    )
    body = json.loads(seen[0].content)
    assert body["system"] == "你是导演助理", "system 是顶层字段，留在 messages 里会直接 400"
    assert body["max_tokens"], "/messages 的 max_tokens 是必填的"
    assert seen[0].headers["anthropic-version"], "缺这个头 /messages 也是 400"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"], "角色必须交替"
    assert [b["type"] for b in body["messages"][1]["content"]] == ["text", "tool_use", "tool_use"]
    results = body["messages"][2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result"], "两条结果并成一条消息"
    assert [b["tool_use_id"] for b in results] == ["c1", "c2"]
    assert body["tools"][1]["input_schema"]["properties"]["scene_id"]["type"] == "string"
    # 回来的形状必须是内部规范那一套，于是 agent 一行都不用改
    assert out["content"] == "提一条"
    assert out["tool_calls"] == [
        {"id": "c3", "name": "add_scene", "arguments": {"title": "雨夜追车"}}
    ]


async def test_gemini_answers_tool_results_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = fake_http(
        monkeypatch,
        replies(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "提一条"},
                                {
                                    "functionCall": {
                                        "name": "add_scene",
                                        "args": {"title": "码头夜戏"},
                                    }
                                },
                            ]
                        }
                    }
                ]
            }
        ),
    )
    out = await protocols.BY_NAME["gemini"].complete_tools(
        cfg("gemini", model="gemini-2.5-pro"), CONVERSATION, TOOLS
    )
    body = json.loads(seen[0].content)
    assert body["systemInstruction"]["parts"][0]["text"] == "你是导演助理"
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"], "它的角色叫 model"
    answers = [p["functionResponse"]["name"] for p in body["contents"][2]["parts"]]
    assert answers == ["list_scenes", "get_scene"], "functionResponse 认函数名不认 id"
    decls = body["tools"][0]["functionDeclarations"]
    assert "parameters" not in decls[0], "无参工具给一个空 properties 会被判成非法 schema"
    assert decls[1]["parameters"]["properties"]["scene_id"]["type"] == "STRING", "type 要大写"
    assert seen[0].url.path.endswith("/models/gemini-2.5-pro:generateContent")
    assert out["content"] == "提一条"
    assert out["tool_calls"][0]["id"], "它不给 id，得自己编一个让 agent 原样回传"
    assert out["tool_calls"][0]["name"] == "add_scene"
    assert out["tool_calls"][0]["arguments"] == {"title": "码头夜戏"}


async def test_gemini_safety_block_is_not_a_format_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """整条被安全过滤挡掉时没有 candidates——那不是「格式不认识」，得说出原因。"""
    fake_http(monkeypatch, replies({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(AppError) as caught:
        await protocols.BY_NAME["gemini"].complete_json(
            cfg("gemini", model="gemini-2.5-pro"), "系统", "用户"
        )
    assert caught.value.code is ErrorCode.LLM_INVALID_OUTPUT
    assert "SAFETY" in caught.value.detail
    assert any("换一种说法" in s for s in caught.value.suggestions)


async def test_ollama_refuses_tools_but_names_the_way_around(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不支持工具不等于用不了：错误里必须写清会退化成一次性产出提案。"""
    fake_http(monkeypatch, replies({}))
    with pytest.raises(AppError) as caught:
        await protocols.BY_NAME["ollama"].complete_tools(cfg("ollama"), CONVERSATION, TOOLS)
    err = caught.value
    assert err.code is ErrorCode.LLM_UNAVAILABLE
    assert any("退化" in s for s in err.suggestions)
    assert protocols.MANUAL_WAY_OUT in err.suggestions


async def test_ollama_asks_for_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = fake_http(monkeypatch, replies({"message": {"content": '前言 {"ops": []} 后记'}}))
    out = await protocols.BY_NAME["ollama"].complete_json(
        cfg("ollama", model="qwen3:8b"), "系统提示", "用户要求"
    )
    body = json.loads(seen[0].content)
    assert body["format"] == "json" and body["stream"] is False
    assert seen[0].url.path == "/api/chat"
    assert out == {"ops": []}, "模型爱在 JSON 外面裹解释文字，截花括号要兜住"


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "API Key"), (404, "/v1"), (429, "额度"), (500, "服务端日志")],
)
async def test_a_rejected_request_names_the_next_move(
    status: int, needle: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_http(monkeypatch, replies({"error": "nope"}, status))
    with pytest.raises(AppError) as caught:
        await protocols.BY_NAME["openai_compatible"].list_models(cfg("openai_compatible"))
    err = caught.value
    assert str(status) in err.title
    assert any(needle in s for s in err.suggestions), f"HTTP {status} 没给出对得上的下一步"
    assert protocols.MANUAL_WAY_OUT in err.suggestions


async def test_unreachable_endpoint_points_at_the_default_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    fake_http(monkeypatch, refuse)
    with pytest.raises(AppError) as caught:
        await protocols.BY_NAME["ollama"].list_models(
            protocols.config(provider="ollama", base_url="http://127.0.0.1:9", api_key="")
        )
    err = caught.value
    assert err.code is ErrorCode.LLM_UNAVAILABLE
    assert any("11434" in s for s in err.suggestions), "要说出默认地址，不然用户不知道该填什么"


def test_models_endpoint_tries_before_saving(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """带上还没保存的协议 / 地址 / 密钥先列一遍模型——不然得先存一份可能是错的配置。"""
    fake_http(
        monkeypatch,
        replies({"data": [{"id": "claude-opus-4-1", "display_name": "Claude Opus 4.1"}]}),
    )
    resp = client.post(
        f"{API}/settings/models",
        json={"what": "llm", "provider": "anthropic", "base_url": "", "api_key": KEY},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "anthropic" and body["count"] == 1
    assert body["items"][0] == {"id": "claude-opus-4-1", "label": "Claude Opus 4.1"}
    assert body["target"].endswith("/models")
    assert KEY not in resp.text, "密钥永不回明文"
    # 只是「先试一下」：设置没被改，也没落盘
    assert settings.llm_provider == "none"
    assert not (settings.runtime_dir / "settings.json").exists()


def test_nothing_else_can_be_auto_fetched(client: TestClient) -> None:
    resp = client.post(f"{API}/settings/models", json={"what": "video"})
    assert resp.status_code == 422
    assert error_of(resp)["code"] == "VALIDATION_ERROR"


def test_fetching_without_a_protocol_points_at_the_manual_path(client: TestClient) -> None:
    """默认 provider = none：这不是一个红叉，是一条带手动出路的结构化错误。"""
    resp = client.post(f"{API}/settings/models", json={"what": "llm"})
    assert resp.status_code == 503
    err = error_of(resp)
    assert err["code"] == "LLM_UNAVAILABLE"
    assert any("手动" in s for s in err["suggestions"])


def test_probe_flags_a_model_that_is_not_there(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "gpt-4o-mini")
    fake_http(monkeypatch, replies({"data": [{"id": "gpt-4o"}]}))
    body = client.post(f"{API}/settings/probe", json={"what": "llm"}).json()
    assert body["ok"] is True and body["model_count"] == 1
    assert body["model_present"] is False, "连得上但模型不在，调用时才失败就太晚了"
    assert "gpt-4o-mini" in body["detail"]


def test_settings_page_is_drawn_from_the_protocol_table(client: TestClient) -> None:
    """协议表是唯一真源：加一个协议不该让前端也改一遍。"""
    body = client.get(f"{API}/settings").json()
    fields = {f["key"]: f for f in body["fields"]}
    assert fields["llm.provider"]["choices"] == protocols.names()
    assert len(fields["llm.provider"]["choice_labels"]) == len(protocols.names())
    assert fields["llm.model"]["fetch"] == "llm", "设置页照这一项画那个「自动获取」按钮"
    assert fields["llm.base_url"]["fetch"] == "", "只有模型能自动获取"
    assert fields["llm.api_key"]["value"] is None and fields["llm.api_key"]["has_value"] is False
    protos = {p["name"]: p for p in body["llm_protocols"]}
    assert set(protos) == {"none", *protocols.BY_NAME}
    assert protos["gemini"]["default_base_url"].startswith("https://")
    assert protos["gemini"]["supports_tools"] is True
    assert protos["ollama"]["needs_key"] is False, "本机端不要密钥，设置页别标成必填"
    assert protos["ollama"]["supports_tools"] is False
    assert body["llm"]["configured"] is False and body["llm"]["label"] == protocols.NONE_LABEL


def test_deps_does_not_ask_ollama_for_a_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(settings, "llm_model", "qwen3:8b")
    monkeypatch.setattr(settings, "llm_api_key", "")
    rows = {row["name"]: row for row in client.get(f"{API}/system/deps").json()}
    assert rows["llm"]["ok"] is True, "没填 Key 不等于没配好——Ollama 本来就不要"
    assert "qwen3:8b" in rows["llm"]["detail"]


def test_deps_is_honest_about_an_unknown_protocol(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_provider", "openai")  # 旧写法，现在不认识
    rows = {row["name"]: row for row in client.get(f"{API}/system/deps").json()}
    assert rows["llm"]["ok"] is False
    assert "openai" in rows["llm"]["detail"]
    assert "anthropic" in rows["llm"]["hint"], "得把可用的协议名列出来"
