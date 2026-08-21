"""Step 2 验收：生成适配层。

这一层存在的理由是「本工具不维护模型端的图」，所以测的重点不是「能不能出片」，
而是这两条约定成立：

  1. comfy_preset **只按节点标题注入**——命中的填、没标的不动、必需标题缺了就报
     INVALID_WORKFLOW 并告诉用户去 ComfyUI 里改标题；
  2. http_api 的三个端点按合同走，任何一处不合同都变成带建议的错误，
     绝不把「响应看不懂」当成「还在跑」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.core.errors import AppError
from app.generation.providers import presets, registry
from app.generation.providers.base import VideoRequest
from app.generation.providers.comfy_preset import ComfyPresetProvider
from app.generation.providers.http_api import HttpApiProvider

# --- comfy_preset ---

GRAPH: dict[str, Any] = {
    "1": {
        "class_type": "LoadImage",
        "inputs": {"image": "原来的.png"},
        "_meta": {"title": "AIVS_FIRST_FRAME"},
    },
    "2": {
        "class_type": "LoadImage",
        "inputs": {"image": "原来的末.png"},
        "_meta": {"title": "AIVS_LAST_FRAME"},
    },
    "3": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "旧提示词"},
        "_meta": {"title": "AIVS_PROMPT"},
    },
    "4": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "旧负向"},
        "_meta": {"title": "AIVS_NEGATIVE"},
    },
    "5": {
        "class_type": "KSampler",
        "inputs": {"seed": 1, "steps": 20},
        "_meta": {"title": "AIVS_SEED"},
    },
    # 模型端自己加的东西：我们既不认识也不该动它
    "9": {"class_type": "LoraLoaderModelOnly", "inputs": {"lora_name": "加速.safetensors"}},
}


class FakeComfy:
    base_url = "http://127.0.0.1:8188"

    def __init__(self, history: dict[str, Any] | None = None) -> None:
        self.submitted: dict[str, Any] | None = None
        self.uploaded: list[str] = []
        self._history = history or {}

    async def ping(self) -> dict[str, Any]:
        return {"online": True, "base_url": self.base_url, "detail": "已连接"}

    async def upload_image(self, filename: str, data: bytes, subfolder: str = "aivs") -> str:
        self.uploaded.append(filename)
        return f"aivs/{filename}"

    async def submit(self, graph: dict[str, Any], client_id: str) -> str:
        self.submitted = graph
        return "pid-1"

    async def history(self, prompt_id: str) -> dict[str, Any]:
        return self._history

    async def download(self, filename: str, subfolder: str = "", kind: str = "output") -> bytes:
        return b"MP4"


def write_preset(name: str, graph: dict[str, Any]) -> None:
    presets.presets_dir().joinpath(f"{name}.json").write_text(
        json.dumps(graph, ensure_ascii=False), encoding="utf-8"
    )


async def test_preset_injection_hits_titles_and_leaves_the_rest_alone(tmp_path: Path) -> None:
    write_preset("wan-flf", GRAPH)
    first = tmp_path / "first.png"
    last = tmp_path / "last.png"
    first.write_bytes(b"A")
    last.write_bytes(b"B")
    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]

    task_id = await provider.submit(
        VideoRequest(
            mode="flf",
            prompt="雨夜推门",
            negative="模糊",
            first_frame=first,
            last_frame=last,
            duration=2.0,
            seed=42,
            extra={"preset": "wan-flf"},
        ),
        client_id="aivs-test",
    )
    assert task_id == "pid-1"
    graph = fake.submitted or {}
    assert graph["1"]["inputs"]["image"] == "aivs/first.png"
    assert graph["2"]["inputs"]["image"] == "aivs/last.png"
    assert graph["3"]["inputs"]["text"] == "雨夜推门"
    assert graph["4"]["inputs"]["text"] == "模糊"
    assert graph["5"]["inputs"]["seed"] == 42
    assert graph["5"]["inputs"]["steps"] == 20, "没标标题的字段一律保持原样"
    assert graph["9"] == GRAPH["9"], "模型端的 lora / 加速节点绝不能被改写"
    assert fake.uploaded == ["first.png", "last.png"], "图在我们这边，必须先传给 ComfyUI"


async def test_preset_without_the_last_frame_title_says_how_to_fix_it(tmp_path: Path) -> None:
    graph = {k: v for k, v in GRAPH.items() if k != "2"}
    write_preset("只支持首帧", graph)
    first = tmp_path / "first.png"
    last = tmp_path / "last.png"
    first.write_bytes(b"A")
    last.write_bytes(b"B")
    provider = ComfyPresetProvider(client=FakeComfy())  # type: ignore[arg-type]

    with pytest.raises(AppError) as caught:
        await provider.submit(
            VideoRequest(
                mode="flf",
                first_frame=first,
                last_frame=last,
                extra={"preset": "只支持首帧"},
            ),
            client_id="aivs-test",
        )
    err = caught.value
    assert err.code == "INVALID_WORKFLOW"
    assert "AIVS_LAST_FRAME" in err.detail
    assert any("Title" in s for s in err.suggestions), "必须告诉用户去 ComfyUI 里改标题"


def test_preset_missing_required_titles_is_rejected_on_save() -> None:
    with pytest.raises(AppError) as caught:
        presets.save("缺入口", json.dumps({"1": {"class_type": "KSampler", "inputs": {"seed": 0}}}))
    err = caught.value
    assert err.code == "INVALID_WORKFLOW"
    assert "AIVS_FIRST_FRAME" in err.detail
    assert presets.listing() == [], "体检不过的图绝不留在预设目录里"


def test_preset_listing_shows_broken_files_instead_of_hiding_them() -> None:
    presets.presets_dir().joinpath("坏的.json").write_text("{不是 json", encoding="utf-8")
    write_preset("好的", GRAPH)
    rows = {r["name"]: r for r in presets.listing()}
    assert rows["好的"]["ready"] is True
    assert rows["坏的"]["ready"] is False
    assert rows["坏的"]["impact"], "坏文件要写清为什么用不了"


async def test_preset_submit_without_a_chosen_preset_points_at_the_settings_page() -> None:
    provider = ComfyPresetProvider(client=FakeComfy())  # type: ignore[arg-type]
    with pytest.raises(AppError) as caught:
        await provider.submit(VideoRequest(mode="i2v"), client_id="aivs-test")
    assert caught.value.code == "MISSING_CAPABILITY"
    assert any("设置页" in s for s in caught.value.suggestions)


async def test_preset_poll_reports_comfy_execution_errors_in_plain_words() -> None:
    history = {
        "status": {
            "status_str": "error",
            "messages": [
                ["execution_error", {"node_type": "KSampler", "exception_message": "显存不足"}]
            ],
        }
    }
    provider = ComfyPresetProvider(client=FakeComfy(history))  # type: ignore[arg-type]
    state = await provider.poll("pid-1")
    assert state.status == "failed"
    assert "KSampler" in state.detail and "显存不足" in state.detail


# --- http_api ---


def stub_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """把 http_api 里新建的 AsyncClient 都挂到一个内存 stub 上。"""
    real = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def happy_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/health"):
        return httpx.Response(200, json={"ok": True})
    if path.endswith("/submit"):
        body = json.loads(request.content)
        assert body["mode"] == "i2v"
        assert body["first_frame"], "首帧必须以 base64 带过去——图在我们这边"
        return httpx.Response(200, json={"task_id": "t-7"})
    if path.endswith("/tasks/t-7"):
        return httpx.Response(
            200, json={"status": "done", "progress": 1.0, "output_url": "/files/out.mp4"}
        )
    if path.endswith("/out.mp4"):
        return httpx.Response(200, content=b"MP4BYTES")
    return httpx.Response(404, json={"error": "no"})


async def test_http_api_full_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_transport(monkeypatch, happy_handler)
    monkeypatch.setattr(settings, "video_base_url", "http://127.0.0.1:9100")
    provider = HttpApiProvider()
    first = tmp_path / "first.png"
    first.write_bytes(b"A")

    assert (await provider.probe())["ok"] is True
    task_id = await provider.submit(
        VideoRequest(mode="i2v", prompt="雨夜", first_frame=first), client_id="aivs-test"
    )
    assert task_id == "t-7"
    state = await provider.poll(task_id)
    assert (state.status, state.progress) == ("done", 1.0)
    name, data = await provider.fetch(task_id)
    assert (name, data) == ("out.mp4", b"MP4BYTES")


async def test_http_api_without_an_address_points_at_the_settings_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "video_base_url", "")
    with pytest.raises(AppError) as caught:
        await HttpApiProvider().probe()
    err = caught.value
    assert err.code == "MISSING_CAPABILITY"
    assert any("设置页" in s for s in err.suggestions)
    assert any("submit" in s for s in err.suggestions), "要把合同写给用户看"


async def test_http_api_bad_shapes_never_look_like_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            return httpx.Response(200, json={"nope": 1})
        if request.url.path.endswith("/tasks/t-1"):
            return httpx.Response(200, json={"status": "在跑呢"})
        if request.url.path.endswith("/tasks/t-2"):
            return httpx.Response(200, text="<html>不是 JSON</html>")
        return httpx.Response(500, text="炸了")

    stub_transport(monkeypatch, handler)
    monkeypatch.setattr(settings, "video_base_url", "http://127.0.0.1:9100")
    provider = HttpApiProvider()

    with pytest.raises(AppError) as caught:
        await provider.submit(VideoRequest(mode="i2v"), client_id="x")
    assert "task_id" in caught.value.detail

    with pytest.raises(AppError) as caught:
        await provider.poll("t-1")
    assert "不认识的状态" in caught.value.title

    with pytest.raises(AppError) as caught:
        await provider.poll("t-2")
    assert "不是 JSON" in caught.value.title

    with pytest.raises(AppError) as caught:
        await provider.probe()
    assert caught.value.code == "COMFY_OFFLINE"


async def test_http_api_failed_task_always_has_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "failed"})

    stub_transport(monkeypatch, handler)
    monkeypatch.setattr(settings, "video_base_url", "http://127.0.0.1:9100")
    state = await HttpApiProvider().poll("t-9")
    assert state.status == "failed"
    assert state.detail, "失败必须带一句话，哪怕是「服务端没给原因」"


# --- registry ---


def test_registry_refuses_the_legacy_path_and_names_the_way_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "video_provider", "comfy_workflow")
    registry.reset()
    assert registry.is_legacy() is True
    with pytest.raises(AppError) as caught:
        registry.provider()
    assert any("ComfyUI 预设" in s for s in caught.value.suggestions)

    monkeypatch.setattr(settings, "video_provider", "wan")
    with pytest.raises(AppError) as caught:
        registry.provider()
    assert caught.value.code == "VALIDATION_ERROR"

    monkeypatch.setattr(settings, "video_provider", "comfy_preset")
    assert registry.provider().name == "comfy_preset"
