"""图片协议适配层（第三条生成链的方言层）。

盯的是**我们这一侧的翻译**，不是某家端的脾气：所有出网请求都从
`providers/image.py::_client` 这个唯一出口换成 `httpx.MockTransport`，一个字节都不出机器
（照 `tests/test_llm_protocols.py` 的作风）。

五件事：

  1. **四种方言各走一遍 `submit` / `poll` / `fetch`**——那三个方法与 `VideoProvider` 同名同形
     是复用 `generation._await_task()` 的前提，签名走歪了图片链就跑不起来；
  2. **密钥只在请求头里，URL 里一个字符都没有**——进了 URL 就会跟着日志与四要素错误一起
     漏出去（Gemini 那支尤其：它刻意不用 `?key=`）；
  3. **降级要说出来、不许抛**：收不了参考图的端把那几张写进 `req.notes`；
     没有负向字段的端把负向并进正向并留一条 note；
  4. **`provider="none"` 时说得清楚**：`image_configured()` 为假，`image_provider()` 报
     `MISSING_CAPABILITY` 且建议里有手动路径（硬约束 2）；
  5. **协议表是唯一真源**：`names()` / `labels()` / `listing()` 一一对应，加一家 API
     只改那一个 dict。
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.generation.providers import image as image_protocols
from app.generation.providers import registry
from app.generation.providers.base import ImageRequest, RefAsset

#: 故意是个一眼能认出来的假密钥：断言「它没出现在 URL 里」时看得清。
KEY = "sk-image-never-leaks-4f2"

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG).decode()


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

    monkeypatch.setattr(image_protocols, "_client", build)
    return seen


def use_settings(protocol: str, **over: Any) -> None:
    """`submit()` 读的是当前设置（`config()`），所以方言测试要先把设置摆好。

    地址一律留空，于是同时测到「留空就用协议默认地址」。
    """
    settings.image_provider = protocol
    settings.image_base_url = str(over.pop("base_url", ""))
    settings.image_api_key = str(over.pop("api_key", KEY))
    settings.image_model = str(over.pop("model", "some-image-model"))
    settings.image_size = str(over.pop("size", "1024x1024"))
    settings.image_preset = str(over.pop("preset", ""))


def ref_file(tmp_path: Path, name: str = "ref.png") -> RefAsset:
    path = tmp_path / name
    path.write_bytes(PNG)
    return RefAsset(path=path, label="阿岚 定妆图", kind="character_sheet", media="image")


def no_key_in_url(seen: list[httpx.Request]) -> None:
    """密钥一个字符都不许进 URL——查的是全文，不只是 `?key=`。"""
    assert seen, "一个请求都没发出去，这条断言就没有意义"
    for request in seen:
        assert KEY not in str(request.url)
        assert "key=" not in (request.url.query or b"").decode()


async def run_once(proto: image_protocols.ImageProtocol, req: ImageRequest) -> tuple[str, bytes]:
    """走完 `submit` → `poll` → `fetch` 这一趟（`generation._await_task()` 走的就是它）。"""
    task_id = await proto.submit(req, client_id="test")
    assert task_id.startswith("img-")
    state = await proto.poll(task_id)
    assert (state.status, state.progress) == ("done", 1.0)
    filename, data = await proto.fetch(task_id)
    # 弹出即清：同一个 task_id 不该能取第二次
    again = await proto.poll(task_id)
    assert again.status == "failed"
    return filename, data


# --- OpenAI 兼容（/v1/images）---


async def test_openai_images_generations(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有参考图 → `/v1/images/generations`，密钥走 `Authorization`。"""
    use_settings("openai_images")
    seen = fake_http(
        monkeypatch, lambda _r: httpx.Response(200, json={"data": [{"b64_json": PNG_B64}]})
    )
    proto = image_protocols.require("openai_images")

    req = ImageRequest(prompt="一位二十出头的女性四视图", negative="text, watermark")
    filename, data = await run_once(proto, req)

    assert data == PNG
    assert filename.endswith(".png")
    request = seen[-1]
    assert str(request.url) == "https://api.openai.com/v1/images/generations"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    no_key_in_url(seen)
    # 这套没有负向字段：并进正向并说出来，绝不静默丢掉
    body = request.read().decode()
    assert "避免出现" in body and "text, watermark" in body
    assert any("负向" in note for note in req.notes)
    # `response_format` 刻意不发（新模型会因为它直接 400）
    assert "response_format" not in body


async def test_openai_images_edits_when_refs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """带参考图 → 改走 `/v1/images/edits`（multipart）。"""
    use_settings("openai_images")
    seen = fake_http(
        monkeypatch, lambda _r: httpx.Response(200, json={"data": [{"b64_json": PNG_B64}]})
    )
    proto = image_protocols.require("openai_images")

    await run_once(proto, ImageRequest(prompt="换成雨夜", refs=[ref_file(tmp_path)]))

    request = seen[-1]
    assert str(request.url).endswith("/v1/images/edits")
    assert request.headers["content-type"].startswith("multipart/form-data")
    no_key_in_url(seen)


async def test_openai_images_downloads_url_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """回地址的端由适配层自己下回来（`_take`），业务层拿到的一律是字节。"""
    use_settings("openai_images")

    def route(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(200, json={"data": [{"url": "https://cdn.example.com/out/a.png"}]})

    seen = fake_http(monkeypatch, route)
    filename, data = await run_once(
        image_protocols.require("openai_images"), ImageRequest(prompt="x")
    )

    assert data == PNG
    assert filename == "a.png"
    assert [r.method for r in seen] == ["POST", "GET"]


async def test_openai_models_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings("openai_images")
    seen = fake_http(
        monkeypatch,
        lambda _r: httpx.Response(
            200, json={"data": [{"id": "gpt-image-1", "owned_by": "openai"}, {"id": ""}]}
        ),
    )
    out = await image_protocols.list_models(
        protocol="openai_images", api_key=KEY, model="gpt-image-1"
    )

    assert [row["id"] for row in out["items"]] == ["gpt-image-1"]
    assert out["current_present"] is True
    no_key_in_url(seen)


# --- Gemini ---


async def test_gemini_key_only_in_header(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Gemini 刻意不用 `?key=`：密钥只走 `x-goog-api-key`。"""
    use_settings("gemini", model="gemini-2.5-flash-image")
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "这就是那张图"},
                        {"inlineData": {"mimeType": "image/jpeg", "data": PNG_B64}},
                    ]
                }
            }
        ]
    }
    seen = fake_http(monkeypatch, lambda _r: httpx.Response(200, json=payload))
    proto = image_protocols.require("gemini")

    filename, data = await run_once(
        proto, ImageRequest(prompt="雨夜的巷子", refs=[ref_file(tmp_path)])
    )

    assert data == PNG
    assert filename.endswith(".jpg")  # 后缀跟着 mime 走
    request = seen[-1]
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com"
        "/v1beta/models/gemini-2.5-flash-image:generateContent"
    )
    assert request.headers["x-goog-api-key"] == KEY
    no_key_in_url(seen)
    body = request.read().decode()
    assert "inline_data" in body  # 参考图确实喂进去了
    assert '"responseModalities"' in body  # 不写这句这个端只回文字


async def test_gemini_text_only_reply_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """只回文字不是「出了一张空图」，是一条要说清怎么办的四要素错误。"""
    use_settings("gemini", model="gemini-2.5-flash")
    fake_http(
        monkeypatch,
        lambda _r: httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "我给你描述一下"}]}}]}
        ),
    )
    with pytest.raises(AppError) as caught:
        await image_protocols.require("gemini").submit(ImageRequest(prompt="x"), client_id="t")

    err = caught.value
    assert err.code is ErrorCode.WORKFLOW_ERROR
    assert err.suggestions and any("gemini-2.5-flash-image" in s for s in err.suggestions)


async def test_gemini_models_strip_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """这个端回 `models/xxx`，设置里存的是后面那一段。"""
    use_settings("gemini")
    fake_http(
        monkeypatch,
        lambda _r: httpx.Response(
            200,
            json={
                "models": [{"name": "models/gemini-2.5-flash-image", "displayName": "Flash Image"}]
            },
        ),
    )
    out = await image_protocols.list_models(protocol="gemini", api_key=KEY)

    assert out["items"] == [{"id": "gemini-2.5-flash-image", "label": "Flash Image"}]


# --- 通用 REST 合同 ---


async def test_http_api_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """本工具定的那份合同：base64 收发，参考图连标签一起给。"""
    use_settings("http_api", base_url="http://127.0.0.1:9001", api_key="")
    seen = fake_http(monkeypatch, lambda _r: httpx.Response(200, json={"image": PNG_B64}))
    proto = image_protocols.require("http_api")

    _filename, data = await run_once(
        proto,
        ImageRequest(prompt="一把旧铜钥匙", negative="text", seed=7, refs=[ref_file(tmp_path)]),
    )

    assert data == PNG
    request = seen[-1]
    assert str(request.url) == "http://127.0.0.1:9001/images/generate"
    # 没填密钥就不发这个头——这类端大多在内网、没有鉴权
    assert "authorization" not in {k.lower() for k in request.headers}
    body = request.read().decode()
    assert '"negative_prompt":"text"' in body.replace(" ", "")
    assert "阿岚 定妆图" in body  # 标签跟着走，「第几张是谁」不必靠序号硬记


async def test_http_api_needs_base_url() -> None:
    """这个协议没有默认地址：没填就报 `MISSING_CAPABILITY`，别等到出图那一刻。"""
    use_settings("http_api", base_url="")
    with pytest.raises(AppError) as caught:
        await image_protocols.require("http_api").submit(ImageRequest(prompt="x"), client_id="t")

    err = caught.value
    assert err.code is ErrorCode.MISSING_CAPABILITY
    assert err.suggestions and any(image_protocols.MANUAL_WAY_OUT == s for s in err.suggestions)


# --- ComfyUI 那一支（表里的一行，真正干活的是另一个类）---


def test_comfy_provider_is_not_the_shim() -> None:
    """`comfy_preset` 的轮询与取回是 ComfyUI 的 history / view，
    所以 `provider()` 回的**不是** `self`——回错了就会去那层同步壳里找结果。"""
    proto = image_protocols.require("comfy_preset")
    provider = proto.provider()

    assert provider is not proto
    assert isinstance(provider, image_protocols.ComfyImageProvider)
    assert proto.wants_preset is True


async def test_comfy_submit_requires_preset() -> None:
    """没指预设时说清「出图是另一份图」，并给手动路径。"""
    use_settings("comfy_preset", preset="")
    with pytest.raises(AppError) as caught:
        await image_protocols.ComfyImageProvider().submit(ImageRequest(prompt="x"), client_id="t")

    err = caught.value
    assert err.code is ErrorCode.MISSING_CAPABILITY
    assert err.suggestions and any(image_protocols.MANUAL_WAY_OUT == s for s in err.suggestions)


# --- 降级：收不了参考图不等于用不了 ---


async def test_refs_unsupported_only_degrades(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`supports_refs=False` 的端带参考图时**只在 `notes` 里降级，不抛**
    （照 `AudioRequest` 那条规矩）。"""
    use_settings("openai_images")
    fake_http(monkeypatch, lambda _r: httpx.Response(200, json={"data": [{"b64_json": PNG_B64}]}))
    proto = image_protocols.require("openai_images")
    monkeypatch.setattr(proto, "supports_refs", False, raising=False)

    req = ImageRequest(prompt="一位女性四视图", refs=[ref_file(tmp_path)])
    _filename, data = await run_once(proto, req)

    assert data == PNG
    assert any("收不了参考图" in note and "阿岚 定妆图" in note for note in req.notes)


# --- 没配置图片服务 ---


def test_none_provider_says_what_to_do() -> None:
    """`provider="none"` 是默认值：`image_configured()` 为假，取 provider 时报
    `MISSING_CAPABILITY` 且建议里有手动路径（硬约束 2）。"""
    settings.image_provider = image_protocols.NONE
    assert registry.image_configured() is False

    with pytest.raises(AppError) as caught:
        registry.image_provider()

    err = caught.value
    assert err.code is ErrorCode.MISSING_CAPABILITY
    assert err.suggestions and image_protocols.MANUAL_WAY_OUT in err.suggestions


def test_unknown_provider_lists_the_options() -> None:
    with pytest.raises(AppError) as caught:
        image_protocols.require("midjourney")

    err = caught.value
    assert err.code is ErrorCode.MISSING_CAPABILITY
    for name in image_protocols.BY_NAME:
        assert name in err.detail


# --- 协议表本身 ---


def test_protocol_table_projects_one_to_one() -> None:
    """`names()` / `labels()` / `listing()` 一一对应：设置页那个下拉照它画，
    加一家 API 只改 `BY_NAME` 一个 dict。"""
    names, labels = image_protocols.names(), image_protocols.labels()
    assert len(names) == len(labels) == len(image_protocols.BY_NAME) + 1
    assert names[0] == image_protocols.NONE

    rows = registry.image_listing()
    assert [row["name"] for row in rows] == names
    assert [row["label"] for row in rows] == labels
    for row in rows:
        assert set(row) == {
            "name",
            "label",
            "default_base_url",
            "needs_key",
            "supports_refs",
            "wants_preset",
            "models_hint",
        }
