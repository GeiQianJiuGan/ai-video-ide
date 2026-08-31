"""「照着这张素材写一句描述」那条链（`services/describe.py` + 四种方言的看图编码）。

一个字节都不出机器：出网口子只有 `protocols._client` 一个，全部换成
`httpx.MockTransport`（照 `tests/test_llm_protocols.py` 的做法）。

盯五件事：

  1. **四种方言各自把图放对位置**——放错了端不会告诉你「图我没看见」，它会照着文件名
     编一段像样的描述，而那正是最难发现的失败；
  2. **密钥只在请求头，URL 里一个字符都没有**（Gemini 尤其不许 `?key=`）；
  3. **端不能看图不等于这件事走不通**：四要素错误里必须有手填那条路（硬约束 2）；
  4. **`plan()` 与 `suggest()` 一行库都不改**——落库只有「用户按保存」那一条路；
  5. **非图片素材在调用之前就跳过并说清原因**，绝不把整段视频 base64 塞给 LLM。
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
from app.services.describe import MANUAL_WAY_OUT, describe
from tests.conftest import error_of, upload_png

API = "/api/v1"
#: 一眼能认出来的假密钥：断言「它没出现在 URL 里」时看得清。
KEY = "sk-describe-never-leaks-4b2"
SENTENCE = "褪色军绿夹克，短发，左颊一道旧疤，正午平光，纯白背景"


def fake_http(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def build(timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=timeout)

    monkeypatch.setattr(protocols, "_client", build)
    return seen


def use_llm(monkeypatch: pytest.MonkeyPatch, provider: str, model: str = "看图的模型") -> None:
    """把 LLM 设成「配好了」。地址留空，于是同时测到「留空就用协议默认地址」。"""
    monkeypatch.setattr(settings, "llm_provider", provider)
    monkeypatch.setattr(settings, "llm_model", model)
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_api_key", KEY)


#: 四种方言各自的响应形状 + 「图该出现在请求体的哪儿」。
#: 断言的是**我们这一侧的编码**，不是某个模型的脾气。
DIALECTS: dict[str, dict[str, Any]] = {
    "openai_compatible": {
        "reply": {"choices": [{"message": {"content": SENTENCE}}]},
        "header": "authorization",
    },
    "anthropic": {
        "reply": {"content": [{"type": "text", "text": SENTENCE}]},
        "header": "x-api-key",
    },
    "gemini": {
        "reply": {"candidates": [{"content": {"parts": [{"text": SENTENCE}]}}]},
        "header": "x-goog-api-key",
    },
    "ollama": {"reply": {"message": {"content": SENTENCE}}, "header": None},
}


def _images_in(provider: str, body: dict[str, Any]) -> list[Any]:
    """把「图放哪儿了」按方言取出来。取不到就是编码写错了，测试该红。"""
    if provider == "openai_compatible":
        content = body["messages"][1]["content"]
        return [b for b in content if b.get("type") == "image_url"]
    if provider == "anthropic":
        content = body["messages"][0]["content"]
        return [b for b in content if b.get("type") == "image"]
    if provider == "gemini":
        return [p for p in body["contents"][0]["parts"] if "inline_data" in p]
    return list(body["messages"][1].get("images") or [])


@pytest.mark.parametrize("provider", list(DIALECTS))
def test_each_dialect_puts_the_bytes_where_that_end_looks_for_them(
    provider: str, client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = DIALECTS[provider]
    use_llm(monkeypatch, provider)
    seen = fake_http(monkeypatch, lambda _r: httpx.Response(200, json=spec["reply"]))
    aid = upload_png(client, pid, name="alan.png")

    resp = client.post(f"{API}/projects/{pid}/describe/suggest", json={"asset_ids": [aid]})
    assert resp.status_code == 200, resp.text
    row = resp.json()["items"][0]
    assert row["suggestion"] == SENTENCE
    assert row["source"] == "vision", "端能看图就该真的把字节送出去"
    assert row["error"] is None

    body = json.loads(seen[0].content)
    images = _images_in(provider, body)
    assert len(images) == 1, f"{provider}: 图没有落在这个端会去看的那个位置"
    #: Ollama 的 `images[]` 是**纯 base64**，带上 `data:` 前缀它会把前缀一起当图片数据解。
    if provider == "ollama":
        assert not images[0].startswith("data:")
    if provider == "openai_compatible":
        assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    if provider == "anthropic":
        assert images[0]["source"]["media_type"] == "image/png"
        types = [b["type"] for b in body["messages"][0]["content"]]
        assert types == ["image", "text"], "Anthropic 上图排在文字前，反了它常只答「我看到一张图」"
    if provider == "gemini":
        assert images[0]["inline_data"]["mime_type"] == "image/png"


@pytest.mark.parametrize("provider", ["openai_compatible", "anthropic", "gemini"])
def test_the_key_never_rides_in_the_url_when_looking_at_an_image(
    provider: str, client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """密钥进了 URL 就会跟着日志与四要素错误一起漏出去。Gemini 刻意不用 `?key=`。"""
    spec = DIALECTS[provider]
    use_llm(monkeypatch, provider)
    seen = fake_http(monkeypatch, lambda _r: httpx.Response(200, json=spec["reply"]))
    aid = upload_png(client, pid, name="key.png")
    resp = client.post(f"{API}/projects/{pid}/describe/suggest", json={"asset_ids": [aid]})
    assert resp.status_code == 200, resp.text

    request = seen[0]
    assert KEY in request.headers[str(spec["header"])]
    assert KEY not in str(request.url)
    assert "key=" not in (request.url.query or b"").decode()
    assert KEY not in resp.text, "密钥永不回明文"


async def test_a_blind_end_reports_four_elements_with_a_manual_path() -> None:
    """`supports_vision=False` 的端走基类那个默认实现（硬约束 2：得给手动路径）。"""

    class Blind(protocols.LlmProtocol):
        name = "blind"
        label = "看不了图的端"
        supports_vision = False

    with pytest.raises(AppError) as caught:
        await Blind().describe_image(protocols.LlmConfig(provider="blind"), "系统", "用户", [])
    err = caught.value
    assert err.code is ErrorCode.LLM_UNAVAILABLE
    assert "看图" in err.title
    assert any("手填" in s for s in err.suggestions), "看不了图不该让「写一句描述」走不通"
    assert protocols.MANUAL_WAY_OUT in err.suggestions


def test_a_text_only_end_still_writes_from_the_clues(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """看不到图也照旧能按名字与已有设定写一句——那比什么都没有好，但要标明 `source=text`。"""
    use_llm(monkeypatch, "openai_compatible")
    monkeypatch.setattr(protocols.BY_NAME["openai_compatible"], "supports_vision", False)
    seen = fake_http(
        monkeypatch,
        lambda _r: httpx.Response(200, json=DIALECTS["openai_compatible"]["reply"]),
    )
    aid = upload_png(client, pid, name="blind.png")

    plan = client.post(f"{API}/projects/{pid}/describe/plan", json={"asset_ids": [aid]}).json()
    assert plan["can_run"] is True
    assert plan["vision_count"] == 0, "端不认图时账单就要说清「只按名字写」"
    assert any("不能看图" in w for w in plan["items"][0]["warnings"])

    row = client.post(f"{API}/projects/{pid}/describe/suggest", json={"asset_ids": [aid]}).json()[
        "items"
    ][0]
    assert row["source"] == "text"
    body = json.loads(seen[0].content)
    assert _images_in("openai_compatible", body) == [], "端不认图就一个字节都别送"
    assert "没有图片可看" in body["messages"][1]["content"][0]["text"]


def test_plan_and_suggest_change_nothing_in_the_database(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两头都是只读的：落库只有「用户在素材页按保存」那一条路。"""
    use_llm(monkeypatch, "openai_compatible")
    fake_http(
        monkeypatch,
        lambda _r: httpx.Response(200, json=DIALECTS["openai_compatible"]["reply"]),
    )
    aid = upload_png(client, pid, name="readonly.png")
    before = client.get(f"{API}/projects/{pid}/assets").json()

    client.post(f"{API}/projects/{pid}/describe/plan", json={"asset_ids": [aid]})
    client.post(f"{API}/projects/{pid}/describe/suggest", json={"asset_ids": [aid]})

    after = client.get(f"{API}/projects/{pid}/assets").json()
    assert after == before, "出建议不等于落库"
    assert after[0]["description"] is None


def test_a_video_is_skipped_before_anything_goes_out(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整段视频既慢又多半被拒收，所以在调用之前就跳过——并且说清为什么。"""
    use_llm(monkeypatch, "openai_compatible")
    seen = fake_http(monkeypatch, lambda _r: httpx.Response(200, json={}))
    resp = client.post(
        f"{API}/projects/{pid}/assets/upload",
        files={"file": ("shot.mp4", b"not really a video", "video/mp4")},
        data={"kind": "upload"},
    )
    assert resp.status_code == 201, resp.text
    aid = resp.json()["id"]

    plan = client.post(f"{API}/projects/{pid}/describe/plan", json={"asset_ids": [aid]}).json()
    assert plan["skipped_count"] == 1
    assert plan["can_run"] is False, "只挑了一个视频时没什么可做的"
    assert any("视频不送给 LLM" in w for w in plan["items"][0]["warnings"])

    row = client.post(f"{API}/projects/{pid}/describe/suggest", json={"asset_ids": [aid]}).json()[
        "items"
    ][0]
    assert row["source"] == "skipped" and row["suggestion"] == ""
    assert seen == [], "跳过的素材不该有任何请求发出去"


def test_no_llm_at_all_is_a_four_element_error_with_the_manual_path(
    client: TestClient, pid: str
) -> None:
    """默认 provider = none：这不是红叉，是一条带手动出路的结构化错误（硬约束 2）。"""
    aid = upload_png(client, pid, name="none.png")
    plan = client.post(f"{API}/projects/{pid}/describe/plan", json={"asset_ids": [aid]}).json()
    assert plan["can_run"] is False
    assert plan["missing"], "账单要先说出「整批都做不了」，不必点一次才知道"
    assert MANUAL_WAY_OUT in plan["missing"][0]["suggestions"]

    resp = client.post(f"{API}/projects/{pid}/describe/suggest", json={"asset_ids": [aid]})
    assert resp.status_code == 503
    err = error_of(resp)
    assert err["code"] == "LLM_UNAVAILABLE"
    assert MANUAL_WAY_OUT in err["suggestions"]


def test_no_asset_ids_says_what_to_do_instead(client: TestClient, pid: str) -> None:
    resp = client.post(f"{API}/projects/{pid}/describe/plan", json={"asset_ids": []})
    assert resp.status_code == 422
    assert error_of(resp)["code"] == "VALIDATION_ERROR"


async def test_target_resolves_the_column_that_really_reaches_the_prompt(
    client: TestClient, pid: str
) -> None:
    """`set_description` 的目标解析只有这一份口径。

    形象上没有 `description` 列，那一句要落在账单真正会读的 `traits` 上
    （`context.APPEARANCE_DESC_FIELDS`）——写进 `notes` 只会存下来但一个字也到不了模型手上。
    """
    char = client.post(
        f"{API}/projects/{pid}/characters", json={"name": "阿岚", "description": "旧疤"}
    ).json()
    app_row = client.get(f"{API}/projects/{pid}/characters/{char['id']}/appearances").json()[0]

    assert (await describe.target(pid, "character", char["id"]))["field"] == "description"
    appearance = await describe.target(pid, "appearance", app_row["id"])
    assert appearance is not None
    assert appearance["field"] == "traits"
    assert appearance["label"].startswith("阿岚 · ")

    assert await describe.target(pid, "appearance", "app_nope") is None
    assert await describe.target(pid, "不认识这种", char["id"]) is None
