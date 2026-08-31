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
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.core.errors import AppError
from app.generation.providers import presets, registry
from app.generation.providers.base import RefAsset, VideoRequest
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

    async def upload_input(self, filename: str, data: bytes, subfolder: str = "aivs") -> str:
        """参考素材不只有图（视频 / 音频走同一个 `/upload/image` 端点），所以这里叫 input。"""
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


def with_ref_slots(count: int) -> dict[str, Any]:
    """在 GRAPH 上加 count 个参考图槽位（AIVS_REF_1…）。"""
    graph = {k: dict(v) for k, v in GRAPH.items()}
    for i in range(1, count + 1):
        graph[f"1{i}"] = {
            "class_type": "LoadImage",
            "inputs": {"image": f"占位{i}.png"},
            "_meta": {"title": f"AIVS_REF_{i}"},
        }
    return graph


def make_refs(tmp_path: Path, *labels: str) -> list[RefAsset]:
    out: list[RefAsset] = []
    for i, label in enumerate(labels, 1):
        path = tmp_path / f"ref{i}.png"
        path.write_bytes(b"R")
        out.append(RefAsset(path=path, label=label, kind="character_sheet"))
    return out


def media_ref(tmp_path: Path, name: str, media: str, label: str) -> RefAsset:
    """一个非图片的参考素材。媒体来自后缀，这里显式传是因为测试不经过 `kind_of_suffix`。"""
    path = tmp_path / name
    path.write_bytes(b"M")
    return RefAsset(path=path, label=label, kind="manual", media=media)


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


def with_media_slots(images: int = 1, videos: int = 0, audios: int = 0) -> dict[str, Any]:
    """在 GRAPH 上加三种媒体的参考素材槽位。视频 / 音频节点的输入键也各不相同。"""
    graph = with_ref_slots(images)
    for i in range(1, videos + 1):
        graph[f"2{i}"] = {
            "class_type": "VHS_LoadVideoPath",
            "inputs": {"video": f"占位{i}.mp4"},
            "_meta": {"title": f"AIVS_REF_VIDEO_{i}"},
        }
    for i in range(1, audios + 1):
        graph[f"3{i}"] = {
            "class_type": "LoadAudio",
            "inputs": {"audio": f"占位{i}.wav"},
            "_meta": {"title": f"AIVS_REF_AUDIO_{i}"},
        }
    return graph


async def test_each_media_goes_into_its_own_family_of_slots(tmp_path: Path) -> None:
    """一段 `.mp4` 填进 LoadImage 既不报错也出不了片，所以三种媒体各走各的槽位。"""
    write_preset("图视音", with_media_slots(images=1, videos=1, audios=1))
    first = tmp_path / "first.png"
    first.write_bytes(b"A")
    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=first,
        refs=[
            *make_refs(tmp_path, "林小雨（常服）"),
            media_ref(tmp_path, "动作.mp4", "video", "推门的动作"),
            media_ref(tmp_path, "对白.wav", "audio", "林小雨的台词"),
        ],
        extra={"preset": "图视音"},
    )

    await provider.submit(req, client_id="aivs-test")
    graph = fake.submitted or {}
    assert graph["11"]["inputs"]["image"] == "aivs/ref1.png"
    assert graph["21"]["inputs"]["video"] == "aivs/动作.mp4", "视频进 AIVS_REF_VIDEO_1 那个输入键"
    assert graph["31"]["inputs"]["audio"] == "aivs/对白.wav"
    assert fake.uploaded == ["first.png", "ref1.png", "动作.mp4", "对白.wav"]
    # 序号按媒体各自从 1 数：和真正填进去的槽位一一对应
    text = graph["3"]["inputs"]["text"]
    assert "参考图1=林小雨（常服）" in text
    assert "参考视频1=推门的动作" in text and "参考音频1=林小雨的台词" in text


async def test_one_media_without_slots_does_not_drop_the_others(tmp_path: Path) -> None:
    """图片槽位够、视频槽位是 0：只有那段视频喂不进去，而且必须说出来。"""
    write_preset("只收图", with_media_slots(images=2))
    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        refs=[
            *make_refs(tmp_path, "林小雨（常服）"),
            media_ref(tmp_path, "动作.mp4", "video", "推门的动作"),
        ],
        extra={"preset": "只收图"},
    )

    await provider.submit(req, client_id="aivs-test")
    assert fake.uploaded == ["ref1.png"], "没有视频槽位就别把它传上去"
    note = next(n for n in req.notes if "AIVS_REF_VIDEO" in n)
    assert "推门的动作" in note and "参考视频" in note
    assert (fake.submitted or {})["11"]["inputs"]["image"] == "aivs/ref1.png", "图照喂"


def test_preset_inspection_counts_each_media_separately() -> None:
    write_preset("图视音", with_media_slots(images=3, videos=2, audios=1))
    row = next(r for r in presets.listing() if r["name"] == "图视音")
    assert (row["ref_slots"], row["ref_video_slots"], row["ref_audio_slots"]) == (3, 2, 1)
    assert row["ref_slots_by_media"] == {"image": 3, "video": 2, "audio": 1}
    assert "参考视频" in row["ref_hint"] and "参考音频" in row["ref_hint"]


def test_capacity_is_per_media(monkeypatch: pytest.MonkeyPatch) -> None:
    """三种媒体折成一个数字的话，「还能再喂 1 个」到底指图还是音频就说不清了。"""
    presets.save("图视音", json.dumps(with_media_slots(images=3, audios=1), ensure_ascii=False))
    monkeypatch.setattr(settings, "video_provider", "comfy_preset")
    monkeypatch.setattr(settings, "video_preset", "图视音")
    registry.reset()
    cap = registry.ref_capacity()
    assert (cap.limit, cap.video, cap.audio) == (3, 0, 1)
    assert (cap.limit_of("image"), cap.limit_of("audio")) == (3, 1)
    assert cap.dropped_of("video", 1) == 1, "没有视频槽位 = 一段都收不了"
    assert cap.dropped_of("audio", 1) == 0
    assert "参考音频 1 个" in cap.detail, "「另外还能收什么」得写在界面看得见的那句话里"


async def test_preset_feeds_reference_images_into_the_ref_slots(tmp_path: Path) -> None:
    """账单里的角色表 / 地点参考图必须真的进到图里——「人物形象丢失」就是这一步漏了。"""
    write_preset("多参考图", with_ref_slots(2))
    first = tmp_path / "first.png"
    first.write_bytes(b"A")
    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=first,
        refs=make_refs(tmp_path, "林小雨（常服）", "雨夜巷口"),
        extra={"preset": "多参考图"},
    )

    await provider.submit(req, client_id="aivs-test")
    graph = fake.submitted or {}
    assert graph["11"]["inputs"]["image"] == "aivs/ref1.png"
    assert graph["12"]["inputs"]["image"] == "aivs/ref2.png"
    assert fake.uploaded == ["first.png", "ref1.png", "ref2.png"]
    # 顺序即语义：ComfyUI 那类图收不到标签，只能把对应关系写进 prompt
    assert graph["3"]["inputs"]["text"].startswith("雨夜推门")
    assert "参考图1=林小雨（常服）" in graph["3"]["inputs"]["text"]
    assert "参考图2=雨夜巷口" in graph["3"]["inputs"]["text"]
    assert any("参考素材对应关系" in n for n in req.notes)


async def test_ref_labels_can_be_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "video_ref_labels", False)
    write_preset("多参考图", with_ref_slots(1))
    first = tmp_path / "first.png"
    first.write_bytes(b"A")
    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=first,
        refs=make_refs(tmp_path, "林小雨（常服）"),
        extra={"preset": "多参考图"},
    )

    await provider.submit(req, client_id="aivs-test")
    graph = fake.submitted or {}
    assert graph["11"]["inputs"]["image"] == "aivs/ref1.png", "关掉标签不影响图照样喂进去"
    assert graph["3"]["inputs"]["text"] == "雨夜推门", "关掉了就绝不动 prompt"


async def test_preset_with_too_few_ref_slots_degrades_and_says_which_were_dropped(
    tmp_path: Path,
) -> None:
    """槽位不够只降级不失败——图是模型端维护的，但少喂了哪几张必须说出来。"""
    write_preset("只有一个槽位", with_ref_slots(1))
    first = tmp_path / "first.png"
    first.write_bytes(b"A")
    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=first,
        refs=make_refs(tmp_path, "林小雨（常服）", "雨夜巷口", "旧怀表"),
        extra={"preset": "只有一个槽位"},
    )

    await provider.submit(req, client_id="aivs-test")
    assert fake.uploaded == ["first.png", "ref1.png"]
    dropped = next(n for n in req.notes if "没喂进去" in n)
    assert "雨夜巷口" in dropped and "旧怀表" in dropped
    assert "只有 1 个参考图槽位" in dropped


async def test_preset_without_ref_slots_still_runs_but_explains_the_risk(tmp_path: Path) -> None:
    write_preset("没有槽位", GRAPH)
    first = tmp_path / "first.png"
    first.write_bytes(b"A")
    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=first,
        refs=make_refs(tmp_path, "林小雨（常服）"),
        extra={"preset": "没有槽位"},
    )

    task_id = await provider.submit(req, client_id="aivs-test")
    assert task_id == "pid-1", "没有参考图槽位不该让整个任务跑不了"
    assert fake.uploaded == ["first.png"]
    assert any("AIVS_REF_" in n and "只能靠首帧带" in n for n in req.notes)


def test_preset_inspection_reports_how_many_reference_images_it_takes() -> None:
    write_preset("三个槽位", with_ref_slots(3))
    write_preset("没有槽位", GRAPH)
    rows = {r["name"]: r for r in presets.listing()}
    assert rows["三个槽位"]["ref_slots"] == 3
    assert "3 张参考图" in rows["三个槽位"]["ref_hint"]
    assert rows["没有槽位"]["ref_slots"] == 0
    assert rows["没有槽位"]["ready"] is True, "没有参考图槽位不算体检不过"
    assert "AIVS_REF_1" in rows["没有槽位"]["ref_hint"], "要告诉用户怎么支持参考图"


def test_preset_missing_required_titles_is_rejected_on_save() -> None:
    """必需的只剩提示词入口：连 AIVS_PROMPT 都没有的图填不进任何东西，才算体检不过。"""
    with pytest.raises(AppError) as caught:
        presets.save("缺入口", json.dumps({"1": {"class_type": "KSampler", "inputs": {"seed": 0}}}))
    err = caught.value
    assert err.code == "INVALID_WORKFLOW"
    assert "AIVS_PROMPT" in err.detail
    assert presets.listing() == [], "体检不过的图绝不留在预设目录里"


def test_preset_without_frame_titles_is_accepted_and_says_the_first_frame_becomes_a_ref() -> None:
    """R2V 出正片的图常常一个首尾帧入口都没有——这种图必须能存、能选、能生成。

    分工是「首尾帧那类模型补转场，R2V 出正片」，所以缺首尾帧入口不再是体检不过；
    它只影响两件事：不能拿它补转场（严格首尾帧），以及那张首帧会当参考图 1 送进去。
    """
    r2v = {k: v for k, v in with_ref_slots(2).items() if k not in {"1", "2"}}
    presets.save("纯R2V", json.dumps(r2v, ensure_ascii=False))
    row = next(r for r in presets.listing() if r["name"] == "纯R2V")
    assert row["ready"] is True, "没有首尾帧入口不算体检不过"
    assert row["first_frame_ok"] is False
    assert row["flf_ready"] is False, "补转场要的是严格首尾帧，这份图做不了"
    assert row["r2v_ready"] is True
    assert "参考图 1" in row["ref_hint"], "要说清首帧会怎么被喂进去"


async def test_preset_without_a_first_frame_title_sends_it_as_reference_one(tmp_path: Path) -> None:
    """降级要说出来：首帧当了参考图 1，这句话进 req.notes 一起冻结进版本。"""
    r2v = {k: v for k, v in with_ref_slots(2).items() if k not in {"1", "2"}}
    write_preset("纯R2V", r2v)
    first = tmp_path / "first.png"
    first.write_bytes(b"A")
    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=first,
        refs=make_refs(tmp_path, "林小雨（常服）"),
        extra={"preset": "纯R2V"},
    )

    await provider.submit(req, client_id="aivs-test")
    graph = fake.submitted or {}
    assert graph["11"]["inputs"]["image"] == "aivs/first.png", "首帧插到参考图 1"
    assert graph["12"]["inputs"]["image"] == "aivs/ref1.png"
    assert fake.uploaded == ["first.png", "ref1.png"], "一张都不能丢"
    assert any("当作参考图 1" in n for n in req.notes), "降级绝不静默"


def test_preset_rejects_the_ui_workflow_with_the_real_reason() -> None:
    """界面工作流是最常见的上传错误，报错必须说「格式选错了」而不是列一串顶层键。"""
    ui_format = {
        "id": "3f1c",
        "revision": 0,
        "last_node_id": 5549,
        "last_link_id": 9001,
        "nodes": [{"id": 1, "type": "LoadImage", "title": "AIVS_FIRST_FRAME"}],
        "links": [],
    }
    with pytest.raises(AppError) as caught:
        presets.save("界面格式", json.dumps(ui_format))
    err = caught.value
    assert err.code == "INVALID_WORKFLOW"
    assert "界面工作流" in err.title
    assert any("导出 (API)" in s for s in err.suggestions), "必须指出去哪儿换导出格式"
    assert any("AIVS_" in s for s in err.suggestions), "标题没白改，要写明不用重设"
    assert presets.listing() == [], "格式不对的图绝不落盘"


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


async def test_http_api_carries_the_asset_description_in_the_refs_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """这一族收得到结构化字段，所以描述走 `refs[].desc`，不靠 prompt 里那句对号。

    ComfyUI 那类图只能把描述拼进 prompt（`ref_hint`），而这条合同由我们定：描述是「这张素材
    长什么样」，属于素材本身而不是提示词，混进 prompt 只会让服务端还得再解析一遍。
    没写描述时是空串——键照旧在，服务端不用分「缺键」和「空」两种情况。
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"task_id": "t-9"})
        return httpx.Response(404, json={"error": "no"})

    stub_transport(monkeypatch, handler)
    monkeypatch.setattr(settings, "video_base_url", "http://127.0.0.1:9100")
    described, plain = make_refs(tmp_path, "林小雨（常服）", "雨夜巷口")
    described = replace(described, desc="褪色军绿夹克，\n短发")

    await HttpApiProvider().submit(
        VideoRequest(mode="i2v", prompt="雨夜", refs=[described, plain]), client_id="aivs-test"
    )

    assert [r["desc"] for r in seen["refs"]] == ["褪色军绿夹克， 短发", ""]  # 换行压成空格
    assert [r["label"] for r in seen["refs"]] == ["林小雨（常服）", "雨夜巷口"]


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


async def test_preset_injects_source_video_and_ref_video(tmp_path: Path) -> None:
    graph = {
        "1": {
            "class_type": "VHS_LoadVideo",
            "inputs": {"video": "default.mp4"},
            "_meta": {"title": "AIVS_SOURCE_VIDEO"},
        },
        "2": {
            "class_type": "VHS_LoadVideoPath",
            "inputs": {"video": "ref_default.mp4"},
            "_meta": {"title": "AIVS_REF_VIDEO_1"},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "原提示词"},
            "_meta": {"title": "AIVS_PROMPT"},
        },
    }
    write_preset("测试视频传入", graph)
    source_slice = tmp_path / "slice_01_10.00_15.00.mp4"
    source_slice.write_bytes(b"VIDEO_SLICE")
    ref_slice = tmp_path / "ref_action.mp4"
    ref_slice.write_bytes(b"REF_VIDEO")

    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="分镜重绘",
        source_video=source_slice,
        refs=[RefAsset(path=ref_slice, label="动作", kind="source_video", media="video")],
        extra={"preset": "测试视频传入"},
    )
    await provider.submit(req, client_id="aivs-test")
    submitted = fake.submitted or {}
    assert submitted["1"]["inputs"]["video"] == "aivs/slice_01_10.00_15.00.mp4"
    assert submitted["2"]["inputs"]["video"] == "aivs/ref_action.mp4"
    assert fake.uploaded == ["slice_01_10.00_15.00.mp4", "ref_action.mp4"]
