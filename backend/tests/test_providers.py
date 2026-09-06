"""Step 2 验收：生成适配层。

这一层存在的理由是「本工具不维护模型端的图」，所以测的重点不是「能不能出片」，
而是这两条约定成立：

  1. comfy_preset **只按节点标题注入**——命中的填、没标的不动、必需标题缺了就报
     INVALID_WORKFLOW 并告诉用户去 ComfyUI 里改标题；
  2. http_api 的三个端点按合同走，任何一处不合同都变成带建议的错误，
     绝不把「响应看不懂」当成「还在跑」。
"""

from __future__ import annotations

import base64
import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.generation.comfy import rejection
from app.generation.comfy.graph import feed_order
from app.generation.providers import presets, registry
from app.generation.providers.base import RefAsset, VideoRequest, WorkflowSpec
from app.generation.providers.comfy_preset import ComfyPresetProvider
from app.generation.providers.comfy_workflow import ComfyWorkflowProvider
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


def with_declaration(graph: dict[str, Any], title: str = presets.DECLARE_IMAGE) -> dict[str, Any]:
    """把声明标题挂在一个**没有任何可填输入**的节点上——用户最顺手的落点就是这种。

    SaveImage 的 `filename_prefix` 不在 `MARKERS` 的任何一族里，所以这份图能证明
    「声明不是入口」：照 `entry_points()` 那条路走会直接报「没有可填的输入」。
    """
    out = {k: dict(v) for k, v in graph.items()}
    out["20"] = {
        "class_type": "SaveImage",
        "inputs": {"images": ["5", 0], "filename_prefix": "aivs"},
        "_meta": {"title": title},
    }
    return out


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
    text = graph["3"]["inputs"]["text"]
    # 图能编进 `<Picture n>`（首帧也占一个号），视频 / 音频编不进去，只能靠 summary 那句点名
    assert "<Subject 1> is the visible subject shown in <Picture 2>: 林小雨（常服）" in text
    assert "参考视频1=推门的动作" in text and "参考音频1=林小雨的台词" in text
    assert "<Picture 3>" not in text, "把一段 .mp4 编进图册会让它后面所有序号都指错人"


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
    # 顺序即语义：ComfyUI 那类图收不到标签，所以提交出去的那段 prompt 本身就是六段格式，
    # 每张图一个 `<Picture n>`、每个出场的人一个 `<Subject n>`
    text = graph["3"]["inputs"]["text"]
    assert "subject_definitions:" in text and "retention_analysis:" in text
    assert "<Subject 1> is the visible subject shown in <Picture 2>: 林小雨（常服）" in text
    assert "<Subject 2> is the visible subject shown in <Picture 3>: 雨夜巷口" in text
    assert "<Picture 1> is the first frame of the target video." in text, "首帧也占一个编号"
    assert "雨夜推门" in text, "用户手写的自由文本原样进 detailed_description"
    assert "are different people" in text, "两个人就必须说清别把他们画成同一个人"
    assert req.sent_prompt == text, "真正发出去的那段话要冻结进版本参数"
    assert any("提示词已按参考生成格式重排" in n for n in req.notes)
    book = req.book.to_dict() if req.book else {"items": []}
    assert [(p["index"], p["subject"], p["name"]) for p in book["items"]] == [
        (1, 0, "首帧"),
        (2, 1, "林小雨（常服）"),
        (3, 2, "雨夜巷口"),
    ], "图册是提示词与那几张图之间唯一的对号表"


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
    assert req.sent_prompt == "雨夜推门", "没重排过就照实说"
    # **关掉是一次降级，不是「什么都没发生」**（硬约束 4）：图册照记（界面要说得清那几张图
    # 按什么顺序喂进去的），只是模型收不到编号——这件事必须写进 notes 冻进版本参数。
    assert req.book is not None and len(req.book.items) == 2, "首帧 + 那张参考图照旧编号"
    assert any("关掉了「参考素材说明」" in n for n in req.notes)


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


def test_a_declared_image_preset_leaves_the_video_candidates() -> None:
    """出图那份图靠一句声明分出来：标了 AIVS_IMAGE 就只归「出图」那一栏。

    T2I 与 R2V 用的是**同一批入口标题**（提示词 / 负向 / 种子 / 参考图槽位），从标题分不出
    是哪一种，所以这句话只能由用户说。声明之后能存、能当出图预设选，但**从视频那两栏里
    消失**——一份 T2I 图躺在 R2V 候选里，选错一次就是一次白跑。
    """
    presets.save("四视图", json.dumps(with_declaration(with_ref_slots(2)), ensure_ascii=False))
    row = next(r for r in presets.listing() if r["name"] == "四视图")
    assert row["ready"] is True, "声明不该让它体检不过——它只是换了一栏"
    assert row["declares_image"] is True
    assert row["declared"] == [presets.DECLARE_IMAGE]
    assert row["prompt_ok"] is True, "提示词入口照旧在，只是这份图不出画面"
    assert row["t2i_ready"] is True
    assert row["r2v_ready"] is False, "声明过的图不该再出现在 R2V 候选里"
    assert row["flf_ready"] is False, "首尾帧入口齐全也一样——它声明了自己是出图那份"
    assert row["capabilities"] == ["t2i"]


def test_a_declaration_needs_no_fillable_input() -> None:
    """声明不是入口：它落在 SaveImage 这种「一个我们认得的输入都没有」的节点上也算。

    反过来说，把它写进 `MARKERS` 就会让这份图直接报「入口节点没有可填的输入」——
    而 SaveImage / 模型加载器正是用户最顺手的落点。
    """
    graph = with_declaration(GRAPH)
    assert presets.declarations(graph) == {presets.DECLARE_IMAGE}
    assert presets.DECLARE_IMAGE not in presets.entry_points(graph), "声明不占入口"
    presets.save("能存", json.dumps(graph, ensure_ascii=False))
    assert next(r for r in presets.listing() if r["name"] == "能存")["t2i_ready"] is True


def test_a_declared_image_preset_gets_its_own_hint() -> None:
    """出图那句提示不能照抄出画面那句：T2I 图上没有首帧，也不补转场。

    照那句显示只会让用户去改一个本来没问题的标题（`AIVS_FIRST_FRAME` 在这份图上无意义）。
    """
    write_preset("出图-有槽位", with_declaration(with_ref_slots(2)))
    write_preset("出图-无槽位", with_declaration(GRAPH))
    rows = {r["name"]: r for r in presets.listing()}
    with_slots = rows["出图-有槽位"]["ref_hint"]
    assert "出图那份图" in with_slots and "2 张参考图" in with_slots
    assert "首帧" not in with_slots, "出图这条链没有首尾帧这回事"
    without = rows["出图-无槽位"]["ref_hint"]
    assert "图生图做不了" in without, "一个槽位都没有要说清代价"
    assert "AIVS_WIDTH" in without, "没有画幅入口也要说一句——出来的是图里原本的画幅"
    assert rows["出图-有槽位"]["size_ok"] is False


def test_a_declared_preset_without_a_prompt_says_which_one_it_is() -> None:
    """声明了出图却没有提示词入口：这种图存不进来，而且要说清它声明的是什么。

    通用那句话（「既没有 AIVS_PROMPT 也没有 AIVS_SOURCE_VIDEO…」）会把用户往超分 / 音源
    那两条链上引，而他明明是在做一份出图的图。
    """
    bare = {
        "1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}},
        "20": {
            "class_type": "EmptyLatentImage",
            "inputs": {"batch_size": 1},
            "_meta": {"title": presets.DECLARE_IMAGE},
        },
    }
    with pytest.raises(AppError) as caught:
        presets.save("只有声明", json.dumps(bare, ensure_ascii=False))
    assert presets.DECLARE_IMAGE in caught.value.detail
    assert "AIVS_PROMPT" in caught.value.detail


def test_picking_a_declared_image_preset_for_video_says_why(tmp_path: Path) -> None:
    """在视频那一栏选中一份出图的图：错误必须说「这是出图那份图」。

    照通用的「预设不可用」说下去，用户会去 ComfyUI 里找一个根本不缺的标题——
    真正的原因是这份图自己声明了用途。这句话只有 `route.preset_error` 一份。
    """
    from app.services import route

    write_preset("四视图", with_declaration(with_ref_slots(2)))
    err = route.preset_error("四视图", "image2video")
    assert err.code == "INVALID_WORKFLOW"
    assert presets.DECLARE_IMAGE in err.detail
    assert err.related_ids.get("declares_image") is True
    assert any(presets.DECLARE_IMAGE in s for s in err.suggestions)
    assert route.preset_ready("四视图", "image2video") is False
    # 没声明的那份照旧走通用文案，一个字都没变
    write_preset("wan-i2v", GRAPH)
    assert route.preset_ready("wan-i2v", "image2video") is True
    assert route.preset_error("wan-i2v", "image2video").title == "预设不可用"


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


async def test_preset_submit_without_a_chosen_preset_points_at_the_button_by_role() -> None:
    """一份预设都没选时那句建议要**点名到角色那颗按钮**，而不是「去设置页选一份」。

    应用级默认有三格（R2V / 首尾帧 / 共用），只说「选一份默认预设」的话，用户在共用那一格里
    配了一份 R2V 图，首尾帧照旧报同一句错；按钮的原字来自 `presets.ROLE_ACTION`
    （与「预设 Workflow」页上那几颗按钮同一份字，说成别的字用户在页面上就找不到它）。
    """
    provider = ComfyPresetProvider(client=FakeComfy())  # type: ignore[arg-type]
    for mode, action in (("i2v", "设为 R2V 默认"), ("flf", "设为首尾帧默认")):
        with pytest.raises(AppError) as caught:
            await provider.submit(VideoRequest(mode=mode), client_id="aivs-test")
        assert caught.value.code == "MISSING_CAPABILITY"
        assert any(action in s for s in caught.value.suggestions), f"{mode} 那句建议要点名按钮"
        assert any("预设 Workflow" in s for s in caught.value.suggestions), "要说清去哪一页按"


async def test_preset_app_default_falls_back_from_role_slot_to_the_shared_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """应用级默认那两级只有一份口径 `presets.app_default`：角色那一格 → 共用那一格。

    `services/route.py::app_preset_of` 与适配层三处只读路径（`probe` / `ref_capacity` /
    `submit` 的兜底）问的都是它——各写一遍的话，「工程没绑就跟随设置页」在不同的页面上
    会给出不同的答案。
    """
    monkeypatch.setattr(settings, "video_preset", "共用那份")
    monkeypatch.setattr(settings, "video_r2v_preset", "")
    monkeypatch.setattr(settings, "video_flf_preset", "")
    assert presets.app_default("r2v") == "共用那份"
    assert presets.app_default("flf") == "共用那份"

    monkeypatch.setattr(settings, "video_flf_preset", "转场那份")
    assert presets.app_default("flf") == "转场那份"
    assert presets.app_default("r2v") == "共用那份", "只填首尾帧那格不该影响普通镜头"

    monkeypatch.setattr(settings, "video_preset", "")
    assert presets.app_default("flf") == "转场那份"
    assert presets.app_default("r2v") is None, "共用格空着又没填 R2V 那格就是真的没选"


async def test_preset_submit_falls_back_to_the_role_slot_not_only_the_shared_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有冻结的预设名时，兜底也走角色那一格。

    以前这里只读共用那一格（`settings.video_preset`），于是只按角色配了默认、共用那格留空的
    机器上，直接调适配层会报「还没有选生成预设」——事实是配了。
    """
    write_preset("转场专用", GRAPH)
    monkeypatch.setattr(settings, "video_preset", "")
    monkeypatch.setattr(settings, "video_flf_preset", "转场专用")
    comfy = FakeComfy()
    provider = ComfyPresetProvider(client=comfy)  # type: ignore[arg-type]
    await provider.submit(VideoRequest(mode="flf", prompt="雨夜"), client_id="aivs-test")
    assert comfy.submitted, "提交的就是首尾帧那一格指的那份图"
    assert comfy.submitted["3"]["inputs"]["text"] == "雨夜"


async def test_preset_probe_reads_both_role_slots_not_only_the_shared_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「测试连接」按角色答，**不能在配了角色默认时说「还没有选默认预设」**（硬约束 4）。

    两个角色跟同一份图且两件事都能干时收成一句话；不可用要点名缺哪个入口，
    只说「不可用」等于让用户自己去数标题。
    """
    write_preset("全能那份", GRAPH)
    write_preset("只出正片", {k: v for k, v in GRAPH.items() if k != "2"})
    provider = ComfyPresetProvider(client=FakeComfy())  # type: ignore[arg-type]

    monkeypatch.setattr(settings, "video_preset", "")
    monkeypatch.setattr(settings, "video_r2v_preset", "只出正片")
    monkeypatch.setattr(settings, "video_flf_preset", "全能那份")
    out = await provider.probe()
    assert (out["preset"], out["preset_ready"]) == ("只出正片", True)
    assert (out["preset_flf"], out["preset_flf_ready"]) == ("全能那份", True)
    assert "还没有选默认预设" not in out["detail"], "配了角色默认还这么说就是谎报"
    assert "R2V 默认 只出正片 就绪" in out["detail"]
    assert "首尾帧默认 全能那份 就绪" in out["detail"]

    # 只有一份 R2V 图的人（共用那一格）：首尾帧那一格跟着它，而它缺末帧入口——这件事要说出来，
    # 不然用户要等到补转场失败才知道。
    monkeypatch.setattr(settings, "video_r2v_preset", "")
    monkeypatch.setattr(settings, "video_flf_preset", "")
    monkeypatch.setattr(settings, "video_preset", "只出正片")
    out = await provider.probe()
    assert out["preset_ready"] is True and out["preset_flf_ready"] is False
    assert "首尾帧默认 只出正片 缺 AIVS_LAST_FRAME" in out["detail"]

    # 两个角色跟同一份图、两件事都能干：一句话说完，不必把同一个名字念两遍。
    monkeypatch.setattr(settings, "video_preset", "全能那份")
    assert "共用这一份" in (await provider.probe())["detail"]

    # 一格都没填：照旧说「还没有选默认预设」——那是这句话唯一该出现的时候。
    monkeypatch.setattr(settings, "video_preset", "")
    assert "还没有选默认预设" in (await provider.probe())["detail"]


async def test_preset_probe_says_the_real_reason_when_the_default_graph_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认那份图坏了：说「这份图读不出来」，**不要说「缺 AIVS_PROMPT」**。

    文件坏了时一个入口都找不到，照「缺哪个标题」那句去说会让用户在一份坏文件上改标题白折腾。
    """
    presets.presets_dir().joinpath("坏掉的.json").write_text("{ 这不是 json", encoding="utf-8")
    monkeypatch.setattr(settings, "video_preset", "坏掉的")
    out = await ComfyPresetProvider(client=FakeComfy()).probe()  # type: ignore[arg-type]
    assert out["preset_ready"] is False
    assert "R2V 默认 坏掉的 不可用：" in out["detail"]
    assert "缺 AIVS_PROMPT" not in out["detail"]


async def test_preset_probe_names_the_missing_default_and_the_button_to_fix_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认指到一份已经被删掉的图：点名是哪一份，并说清去哪儿改选（绝不静默失败）。"""
    write_preset("还在的", GRAPH)
    monkeypatch.setattr(settings, "video_preset", "还在的")
    monkeypatch.setattr(settings, "video_flf_preset", "被删了的")
    provider = ComfyPresetProvider(client=FakeComfy())  # type: ignore[arg-type]
    with pytest.raises(AppError) as caught:
        await provider.probe()
    assert caught.value.code == "NOT_FOUND"
    assert "被删了的" in caught.value.detail
    assert caught.value.related_ids["missing"] == ["被删了的"]
    assert any("设为首尾帧默认" in s for s in caught.value.suggestions)


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


async def test_http_api_numbers_the_pictures_in_a_field_and_leaves_the_prompt_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「第几张图是谁」这条路走 `pictures[]`，**prompt 一个字节都不重排**。

    三件事各自都能单独出错，所以一起盯：

      · **首尾帧一起编号**：它们照旧是模型看到的那一串图片里的一张，编号从 1 起，
        但它们不是「谁出场」，所以 `subject` 是 0；
      · **非图片素材编不上号但照旧送出去**：一段音频占了 `<Picture n>` 就会让它后面所有
        序号指错人，所以 `picture` / `subject` 都是 0——而 `refs[]` 里那一项必须还在，
        「送没送出去」与「模型知不知道它是谁」是两件事；
      · **prompt 原样送出**：两条 ComfyUI 路只能把图册拼进提示词，这条收得到结构化字段，
        再拼一遍只会让服务端解析两遍、两处措辞必然分叉。`sent_prompt` 也保持原样——
        说成重排过就是谎报（硬约束 4）。
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"task_id": "t-11"})
        return httpx.Response(404, json={"error": "no"})

    stub_transport(monkeypatch, handler)
    monkeypatch.setattr(settings, "video_base_url", "http://127.0.0.1:9100")
    first = tmp_path / "first.png"
    first.write_bytes(b"A")
    (sheet,) = make_refs(tmp_path, "阿岚 默认形象")
    voice = media_ref(tmp_path, "line.wav", "audio", "对白")
    req = VideoRequest(
        mode="i2v", prompt="雨夜推门", first_frame=first, refs=[replace(sheet, desc="短发"), voice]
    )

    await HttpApiProvider().submit(req, client_id="aivs-test")

    assert [(p["index"], p["subject"], p["role"], p["name"]) for p in seen["pictures"]] == [
        (1, 0, "first_frame", "首帧"),
        (2, 1, "reference", "阿岚 默认形象"),
    ], seen["pictures"]
    assert [(r["label"], r["picture"], r["subject"]) for r in seen["refs"]] == [
        ("阿岚 默认形象", 2, 1),
        ("对白", 0, 0),
    ], "音频编不上号，但那一项必须还在 refs 里"
    assert seen["prompt"] == "雨夜推门", "结构化字段那条路不动提示词"
    assert req.sent_prompt == "雨夜推门", "没重排过就照实说"
    assert req.book is not None and req.book.order_source == "contract"


async def test_http_api_refine_carries_the_source_video_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """二次处理（`mode="refine"`）必须把**那一段视频本身**带过去，不只是一句提示词。

    合同里少了 `source_video` 这一项，REST 路上的超分就变成「凭提示词重出一段」，而版本轨上
    写着「从 v1 超分而来」——血缘就是假的了。它与 `refs` 里 `media="video"` 的那些严格分开：
    那些是参考，这一条是「就处理这一段」。
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"task_id": "t-refine"})
        return httpx.Response(404, json={"error": "no"})

    stub_transport(monkeypatch, handler)
    monkeypatch.setattr(settings, "video_base_url", "http://127.0.0.1:9100")
    source = tmp_path / "shot_01_v1.mp4"
    source.write_bytes(b"MP4_SOURCE")
    reference = tmp_path / "action_ref.mp4"
    reference.write_bytes(b"MP4_REF")

    task_id = await HttpApiProvider().submit(
        VideoRequest(
            mode="refine",
            prompt="放大到 4K",
            source_video=source,
            refs=[RefAsset(path=reference, label="动作参考", kind="source_video", media="video")],
        ),
        client_id="aivs-test",
    )

    assert task_id == "t-refine"
    assert seen["mode"] == "refine"
    assert seen["source_video_name"] == "shot_01_v1.mp4"
    assert base64.b64decode(seen["source_video"]) == b"MP4_SOURCE", "要处理的那一段本身"
    assert [r["name"] for r in seen["refs"]] == ["action_ref.mp4"], "参考视频不该顶掉源视频"
    assert seen["refs"][0]["media"] == "video"


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


def test_registry_treats_the_three_routes_alike(monkeypatch: pytest.MonkeyPatch) -> None:
    """三条路在 registry 里一视同仁——`comfy_workflow` 不再是被拒的兼容路径。

    它以前长在 `GenerationService._run_legacy` 里，靠 `job.workflow_id` 非空触发，而那一列
    从来没被写过值：选了它等于什么都没选，所以 registry 直接拒绝它并劝人改回预设。现在它是
    一等适配器（`providers/comfy_workflow.py`），`is_legacy()` 也随之删掉——**「这条路绑没绑上」
    改由 `services/route.py` 按工程 + 能力回答**，不再是应用级的一句「不支持」。
    """
    monkeypatch.setattr(settings, "video_provider", "comfy_workflow")
    registry.reset()
    assert registry.provider().name == "comfy_workflow", "选了工作流绑定就得真拿到它"
    assert not hasattr(registry, "is_legacy"), "拉平之后不该再有「哪条是兼容路径」这个问题"
    assert [row["legacy"] for row in registry.listing()] == [False, False, False]

    monkeypatch.setattr(settings, "video_provider", "wan")
    with pytest.raises(AppError) as caught:
        registry.provider()
    assert caught.value.code == "VALIDATION_ERROR"
    assert caught.value.related_ids["available"] == ["comfy_preset", "http_api", "comfy_workflow"]

    monkeypatch.setattr(settings, "video_provider", "comfy_preset")
    assert registry.provider().name == "comfy_preset"


async def test_workflow_route_submits_by_bindings_and_says_what_it_dropped(
    tmp_path: Path,
) -> None:
    """工作流绑定那条路：按绑定表填、只喂图片、少喂的每一张都写进 notes。

    这两条降级原样搬自被删掉的 `_run_legacy`（一个字都没放宽）：图是用户自己维护的，
    我们没资格因为它只绑了一个参考图槽位就拒绝生成——但静默丢掉更糟，事后没人查得出
    「我挂的那段对白音频到底送没送出去」。
    """
    graph = {
        "10": {"class_type": "LoadImage", "inputs": {"image": "first.png"}},
        "11": {"class_type": "LoadImage", "inputs": {"image": "ref.png"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "原提示词"}},
    }
    head = tmp_path / "head.png"
    head.write_bytes(b"PNG_HEAD")
    sheet = tmp_path / "sheet.png"
    sheet.write_bytes(b"PNG_SHEET")
    extra = tmp_path / "extra.png"
    extra.write_bytes(b"PNG_EXTRA")
    voice = tmp_path / "line.wav"
    voice.write_bytes(b"WAV")

    fake = FakeComfy()
    provider = ComfyWorkflowProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=head,
        refs=[
            RefAsset(path=sheet, label="阿岚 默认形象", kind="character_sheet", media="image"),
            RefAsset(path=extra, label="城南旧宅 雨夜", kind="location_reference", media="image"),
            RefAsset(path=voice, label="对白", kind="dialogue_audio", media="audio"),
        ],
        workflow=WorkflowSpec(
            id="wf_1",
            name="绑定图",
            api_json=json.dumps(graph),
            bindings={
                "prompt": "6.text",
                "first_frame": "10.image",
                "reference_image_slots": ["11.image"],
            },
        ),
    )
    assert await provider.submit(req, client_id="aivs-test") == "pid-1"

    submitted = fake.submitted or {}
    assert submitted["10"]["inputs"]["image"] == "aivs/head.png", "首帧按绑定表进它那个节点"
    assert submitted["11"]["inputs"]["image"] == "aivs/sheet.png", "第一张参考图进唯一那个槽位"
    assert fake.uploaded == ["head.png", "sheet.png", "extra.png"], "音频连上传都不该发生"
    assert any("对白" in note and "只能喂图片" in note for note in req.notes)
    assert any("城南旧宅 雨夜" in note and "1 个" in note for note in req.notes)

    # 六段格式**两条 ComfyUI 路一字不差**（`comfy_base._retold` 一份实现）：这条路收到的
    # 同样只是「一串图片 + 一段文字」，绑定表那一行的行号和 AIVS_REF_2 一样只是入口名
    text = submitted["6"]["inputs"]["text"]
    assert "<Subject 1> is the visible subject shown in <Picture 2>: 阿岚 默认形象" in text
    assert "<Picture 1> is the first frame of the target video." in text, "首帧也占一个编号"
    assert "雨夜推门" in text, "用户手写的自由文本原样进 detailed_description"
    assert "参考音频1=对白" in text, "喂不进去的那段音频照旧要在 summary 里点名"
    assert req.sent_prompt == text
    book = req.book.to_dict() if req.book else {"items": []}
    assert [(p["index"], p["subject"], p["name"]) for p in book["items"]] == [
        (1, 0, "首帧"),
        (2, 1, "阿岚 默认形象"),
    ]


async def test_workflow_route_without_a_bound_graph_names_the_way_out() -> None:
    """这条路的前提就是「这个能力绑了一份图」，没有就是四要素错误（不是 500、不是静默出片）。"""
    with pytest.raises(AppError) as caught:
        await ComfyWorkflowProvider(client=FakeComfy()).submit(  # type: ignore[arg-type]
            VideoRequest(mode="i2v", prompt="x"), client_id="aivs-test"
        )
    assert caught.value.code == "MISSING_CAPABILITY"
    assert any("Workflow 管理页" in s for s in caught.value.suggestions)
    assert any("ComfyUI 预设" in s for s in caught.value.suggestions)


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


# --- 这一版用不上的媒体入口：连节点一起摘掉 ---
#
# 「标了 AIVS_* 标题却这一次没有值」以前是「保持图里原来的值」，而图里那一格存的是用户在
# ComfyUI 里存图时挂着的**示例文件**——于是不需要末帧的镜头会被真喂一张不相干的图，画面往它
# 上面收敛，队列里却一条错误都没有。结果是「多标几个入口」反过来成了风险，用户不敢在图里
# 多摆节点。这一组盯的就是新口径：**标了标题 = 这一格由本工具填，本工具这次没填 = 这一格
# 这次不用**（标量相反，保持原值才是对的）。


async def test_unused_media_entry_is_detached_instead_of_feeding_its_sample_file(
    tmp_path: Path,
) -> None:
    """只给首帧时，末帧那个节点整个不进提交的图；标量与文本入口照旧保持原值。"""
    write_preset("wan-flf", GRAPH)
    first = tmp_path / "first.png"
    first.write_bytes(b"A")
    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=first,
        extra={"preset": "wan-flf"},
    )

    await provider.submit(req, client_id="aivs-test")

    graph = fake.submitted or {}
    assert "2" not in graph, "AIVS_LAST_FRAME 这次没有值，那个节点连它的示例图一起摘掉"
    assert graph["1"]["inputs"]["image"] == "aivs/first.png"
    assert graph["3"]["inputs"]["text"] == "雨夜推门"
    assert graph["4"]["inputs"]["text"] == "旧负向", "文本入口没给值时保持图里原来的值"
    assert graph["5"]["inputs"]["seed"] == 1, "标量入口没给值时保持图里原来的值（这才是默认参数）"
    assert graph["9"] == GRAPH["9"], "模型端的 lora / 加速节点绝不能被动"
    assert fake.uploaded == ["first.png"]
    note = next(n for n in req.notes if "末帧" in n)
    assert "示例文件一个都没有送进 ComfyUI" in note, "摘一个节点是降级，必须说出来"
    assert "连带" not in note, "这份图上末帧没有下游中间节点，不该凭空说连带摘了几个"


#: 真实的图不是一排孤立的 LoadImage：末帧那一支往往还串着一个缩放 / 裁剪节点，再汇进
#: 主节点。摘节点必须跟着连线走一层——**只摘只为它服务的，不动共用的汇合点**。
LINKED_GRAPH: dict[str, Any] = {
    "1": {
        "class_type": "LoadImage",
        "inputs": {"image": "首帧示例.png"},
        "_meta": {"title": "AIVS_FIRST_FRAME"},
    },
    "2": {
        "class_type": "LoadImage",
        "inputs": {"image": "末帧示例.png"},
        "_meta": {"title": "AIVS_LAST_FRAME"},
    },
    "3": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "旧提示词", "clip": ["8", 1]},
        "_meta": {"title": "AIVS_PROMPT"},
    },
    "5": {
        "class_type": "KSampler",
        "inputs": {"seed": 1, "steps": 20, "positive": ["7", 0]},
        "_meta": {"title": "AIVS_SEED"},
    },
    # 只为末帧那一支服务的中间节点：末帧一摘，它就没有存在的理由了
    "6": {"class_type": "ImageScale", "inputs": {"image": ["2", 0], "width": 832, "height": 480}},
    # 汇合点：丢了 end_image 还连着 positive / vae / start_image，绝不能跟着摘
    "7": {
        "class_type": "WanImageToVideo",
        "inputs": {
            "positive": ["3", 0],
            "vae": ["8", 2],
            "start_image": ["1", 0],
            "end_image": ["6", 0],
            "length": 81,
        },
    },
    "8": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "wan.safetensors"}},
}


async def test_detaching_follows_the_links_one_hop_and_stops_at_a_junction(
    tmp_path: Path,
) -> None:
    """末帧那一支连着的缩放节点跟着摘；汇合点只少一个输入键，那条主链一刀都不能断。"""
    write_preset("接了线的图", LINKED_GRAPH)
    first = tmp_path / "first.png"
    first.write_bytes(b"A")
    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v", prompt="雨夜推门", first_frame=first, extra={"preset": "接了线的图"}
    )

    await provider.submit(req, client_id="aivs-test")

    graph = fake.submitted or {}
    assert "2" not in graph and "6" not in graph, "末帧与只为它服务的缩放节点一起摘"
    assert "7" in graph, "汇合点绝不能跟着摘——摘断它就等于这次什么都跑不出来"
    assert "end_image" not in graph["7"]["inputs"], "指向被摘节点的连线连键一起删"
    assert graph["7"]["inputs"]["start_image"] == ["1", 0]
    assert graph["7"]["inputs"]["positive"] == ["3", 0]
    assert (graph["7"]["inputs"]["vae"], graph["7"]["inputs"]["length"]) == (["8", 2], 81)
    assert graph["8"] == LINKED_GRAPH["8"], "不认识 class_type，也就不会去动模型加载那一支"
    assert graph["5"]["inputs"]["seed"] == 1
    note = next(n for n in req.notes if "末帧" in n)
    assert "连带 1 个只为它们服务的中间节点" in note, "连带摘了几个也得说出来"


async def test_unused_ref_slots_do_not_leave_their_placeholder_images_behind(
    tmp_path: Path,
) -> None:
    """标了 3 个参考图槽位而这一版只有 1 张：另外两个节点连占位图一起摘掉。

    这正是「不敢在图里多标槽位」的现场——多标的那几格留着的是存图时挂着的占位图，
    于是这个镜头会被喂进两张不相干的图，而账单上写着只喂了一张。
    """
    write_preset("三个槽位", with_ref_slots(3))
    first = tmp_path / "first.png"
    first.write_bytes(b"A")
    fake = FakeComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=first,
        refs=make_refs(tmp_path, "林小雨（常服）"),
        extra={"preset": "三个槽位"},
    )

    await provider.submit(req, client_id="aivs-test")

    graph = fake.submitted or {}
    assert graph["11"]["inputs"]["image"] == "aivs/ref1.png", "填上的那个槽位照旧填"
    assert "12" not in graph and "13" not in graph, "没填的槽位连它的占位图一起摘掉"
    assert fake.uploaded == ["first.png", "ref1.png"], "没有素材可填的槽位不该凭空上传什么"
    note = next(n for n in req.notes if "没有用到" in n)
    assert "末帧" in note and "2 个参考图槽位" in note, "九个槽位逐个点名会把真正要紧的那句埋掉"
    text = graph["3"]["inputs"]["text"]
    assert "<Subject 1> is the visible subject shown in <Picture 2>: 林小雨（常服）" in text
    assert "<Picture 3>" not in text, "摘掉的槽位不占编号——图册只数真喂进去的那几张"


class RejectingComfy(FakeComfy):
    """提交阶段就被 ComfyUI 拒了（图里那一格是必填的，我们刚好把它摘了）。"""

    def __init__(self, code: str = "WORKFLOW_ERROR") -> None:
        super().__init__()
        self._code = code

    async def submit(self, graph: dict[str, Any], client_id: str) -> str:
        raise AppError(
            ErrorCode(self._code),
            "ComfyUI 拒绝了本次任务",
            "Required input is missing: image1",
            ["照 ComfyUI 给的字段名去图里找那个节点"],
        )


async def test_a_rejected_submit_points_at_what_was_detached(tmp_path: Path) -> None:
    """摘掉的那一格恰好是必填的时候，报错里必须指出这件事——否则用户只会对着自己存好的图发愣。"""
    write_preset("wan-flf", GRAPH)
    first = tmp_path / "first.png"
    first.write_bytes(b"A")
    req = VideoRequest(
        mode="i2v", prompt="雨夜推门", first_frame=first, extra={"preset": "wan-flf"}
    )

    with pytest.raises(AppError) as caught:
        await ComfyPresetProvider(client=RejectingComfy()).submit(  # type: ignore[arg-type]
            req, client_id="aivs-test"
        )
    err = caught.value
    assert err.detail == "Required input is missing: image1", "ComfyUI 给的原因绝不能被我们盖掉"
    assert err.suggestions[0] == "照 ComfyUI 给的字段名去图里找那个节点", "原来的建议排在前面"
    assert any("摘掉了 1 个" in s and "AIVS_LAST_FRAME#2" in s for s in err.suggestions)
    assert any("必填" in s for s in err.suggestions), "得给出「把这个入口从图里删掉」这条出路"


async def test_a_rejected_submit_without_detaching_keeps_the_real_reason_alone(
    tmp_path: Path,
) -> None:
    """没摘过节点、或失败与提交这份图无关（离线 / 超时）时，绝不多说那两句——会把真原因埋掉。"""
    write_preset("wan-flf", GRAPH)
    first = tmp_path / "first.png"
    last = tmp_path / "last.png"
    first.write_bytes(b"A")
    last.write_bytes(b"B")
    full = VideoRequest(
        mode="flf",
        prompt="雨夜推门",
        first_frame=first,
        last_frame=last,
        extra={"preset": "wan-flf"},
    )

    with pytest.raises(AppError) as caught:
        await ComfyPresetProvider(client=RejectingComfy()).submit(  # type: ignore[arg-type]
            full, client_id="aivs-test"
        )
    assert caught.value.suggestions == ["照 ComfyUI 给的字段名去图里找那个节点"]

    only_first = replace(full, last_frame=None, notes=[])
    offline_comfy = RejectingComfy("COMFY_OFFLINE")
    with pytest.raises(AppError) as offline:
        await ComfyPresetProvider(client=offline_comfy).submit(  # type: ignore[arg-type]
            only_first, client_id="aivs-test"
        )
    assert offline.value.suggestions == ["照 ComfyUI 给的字段名去图里找那个节点"], "与摘节点无关"


async def test_workflow_route_detaches_the_slots_this_take_has_nothing_for(
    tmp_path: Path,
) -> None:
    """绑定那条路同一件事：绑了末帧却没有末帧、绑了两个参考图槽位却只有一张——都得摘掉。

    这条路的图是**用户自己维护**的，所以「多绑几个槽位」更需要没有代价：绑定表里那几行
    指着的节点，格子里留的是他在 ComfyUI 里存图时挂着的示例文件。
    标量槽位（这里的 seed）相反，没给值时保持图里原来的值。
    """
    graph = {
        "10": {"class_type": "LoadImage", "inputs": {"image": "首帧示例.png"}},
        "20": {"class_type": "LoadImage", "inputs": {"image": "末帧示例.png"}},
        "11": {"class_type": "LoadImage", "inputs": {"image": "参考示例1.png"}},
        "12": {"class_type": "LoadImage", "inputs": {"image": "参考示例2.png"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "原提示词"}},
        "5": {"class_type": "KSampler", "inputs": {"seed": 7, "steps": 20}},
    }
    head = tmp_path / "head.png"
    head.write_bytes(b"PNG_HEAD")
    sheet = tmp_path / "sheet.png"
    sheet.write_bytes(b"PNG_SHEET")

    fake = FakeComfy()
    provider = ComfyWorkflowProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=head,
        refs=[RefAsset(path=sheet, label="阿岚 默认形象", kind="character_sheet", media="image")],
        workflow=WorkflowSpec(
            id="wf_1",
            name="绑定图",
            api_json=json.dumps(graph),
            bindings={
                "prompt": "6.text",
                "first_frame": "10.image",
                "last_frame": "20.image",
                "seed": "5.seed",
                "reference_image_slots": ["11.image", "12.image"],
            },
        ),
    )

    await provider.submit(req, client_id="aivs-test")

    submitted = fake.submitted or {}
    assert "20" not in submitted, "绑了末帧而这个镜头没有末帧：那个节点连示例图一起摘掉"
    assert "12" not in submitted, "第二个参考图槽位这一版没有图可填，同样摘掉"
    assert submitted["10"]["inputs"]["image"] == "aivs/head.png"
    assert submitted["11"]["inputs"]["image"] == "aivs/sheet.png"
    assert submitted["5"]["inputs"] == {"seed": 7, "steps": 20}, "标量槽位保持图里原来的值"

    # **摘掉的槽位不占编号**：图册在 `_detach_idle` 之后才数，所以末帧与第二个参考图槽位
    # 既不在 `<Picture n>` 里，也不会让后面那张图的序号往后串。
    text = submitted["6"]["inputs"]["text"]
    assert "<Picture 1> is the first frame of the target video." in text
    assert "<Subject 1> is the visible subject shown in <Picture 2>: 阿岚 默认形象" in text
    assert "<Picture 3>" not in text, "摘掉的那两个槽位一个编号都不占"
    assert "final frame" not in text, "末帧被摘掉了，对齐句不许再说它"
    assert "雨夜推门" in text
    note = next(n for n in req.notes if "没有用到" in n)
    assert "末帧" in note and "参考图槽位" in note
    assert "示例文件一个都没有送进 ComfyUI" in note


async def test_workflow_route_never_detaches_a_node_another_slot_filled(tmp_path: Path) -> None:
    """同一个 LoadImage 常常被两行绑定同时指着（首帧 / 单槽参考图 与 `AIVS_REF_1`）：
    只要有一行填上了值，这个节点就得留。

    这条按**节点 id** 判而不是按槽位判。按槽位判的话，`first_frame` 这一版没有值就会把
    `__ref_0` 刚填好的那个节点一起摘掉——这一版真正要喂的那张图反而丢了。
    """
    graph = {
        "11": {"class_type": "LoadImage", "inputs": {"image": "参考示例1.png"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "原提示词"}},
    }
    sheet = tmp_path / "sheet.png"
    sheet.write_bytes(b"PNG_SHEET")
    fake = FakeComfy()
    provider = ComfyWorkflowProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        refs=[RefAsset(path=sheet, label="阿岚 默认形象", kind="character_sheet", media="image")],
        workflow=WorkflowSpec(
            id="wf_1",
            name="绑定图",
            api_json=json.dumps(graph),
            bindings={
                "prompt": "6.text",
                "first_frame": "11.image",
                "reference_image_slots": ["11.image"],
            },
        ),
    )

    await provider.submit(req, client_id="aivs-test")

    submitted = fake.submitted or {}
    assert submitted["11"]["inputs"]["image"] == "aivs/sheet.png", "参考图那一行填上了，节点必须留"
    assert not any("没有用到" in note for note in req.notes), "什么都没摘就别说摘了东西"

    # **同一张图占两个入口只编一个号**（`base.picture_book` 按文件去重）：这条路上
    # `source_image` / `reference_image` 那两个单槽常常与 `__ref_0` 指着同一个 LoadImage，
    # 编成两个号就等于告诉模型「这是两张不同的图」，后面所有序号跟着指错人。
    items = (req.book.to_dict() if req.book else {"items": []})["items"]
    numbered = [(p["index"], p["subject"], p["name"]) for p in items]
    assert numbered == [(1, 1, "阿岚 默认形象")], numbered


async def test_workflow_route_leaves_the_prompt_alone_when_no_row_binds_it(tmp_path: Path) -> None:
    """绑定表里**没有 `prompt` 那一行**是合法的（提示词写死在图里，用户只绑了首尾帧）。

    那种图上我们本来就改不到提示词，所以 `_retold` 的 `write` 回 False——图册照记，
    `sent_prompt` 保持原样，**绝不假装重排过**（谎报正是硬约束 4 要修的那件事）。
    """
    graph = {
        "11": {"class_type": "LoadImage", "inputs": {"image": "参考示例1.png"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "图里写死的提示词"}},
    }
    sheet = tmp_path / "sheet.png"
    sheet.write_bytes(b"PNG_SHEET")
    fake = FakeComfy()
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        refs=[RefAsset(path=sheet, label="阿岚 默认形象", kind="character_sheet", media="image")],
        workflow=WorkflowSpec(
            id="wf_1",
            name="绑定图",
            api_json=json.dumps(graph),
            bindings={"reference_image_slots": ["11.image"]},
        ),
    )

    await ComfyWorkflowProvider(client=fake).submit(req, client_id="aivs-test")  # type: ignore[arg-type]

    submitted = fake.submitted or {}
    assert submitted["6"]["inputs"]["text"] == "图里写死的提示词", "改不到就一个字节都不动"
    assert req.sent_prompt == "雨夜推门", "没重排过就照实说，sent_prompt 保持原样"
    assert req.book is not None and len(req.book.items) == 1, "图册照记（界面要说清喂了哪几张）"
    assert not any("重排" in note for note in req.notes)


# --- `<Picture n>` 的编号来自接线，不来自 AIVS_REF_n 那个 n ---
#
# 用户那句需求原文：「我是可以人工这样配置，但是能不能代码逻辑识别 workflow 自动识别 Picture
# 序号呢？这样我就不用严格的约束了」。`AIVS_REF_1` / `AIVS_REF_2` 只说明「这一格由本工具填」，
# 没有任何东西保证 1 号那个 LoadImage 真接在合批节点的 `image1` 上——图是用户自己维护的
# （硬约束 1），在 ComfyUI 里把两根线互换一下编号就反了，而队列里一条错误都没有，
# 只有成片里两个角色互相串味。这一组盯的就是 `comfy/graph.py::feed_order` 从接线上把顺序
# **测出来**，以及测不出来的时候如实说「这一份是按入口名排的」。


def batch_graph(image1: str, image2: str) -> dict[str, Any]:
    """两个 LoadImage 合批进 ImageBatch——「谁是第一张」的事实就在这两个输入键上。"""
    return {
        "11": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
        "12": {"class_type": "LoadImage", "inputs": {"image": "b.png"}},
        "13": {
            "class_type": "ImageBatch",
            "inputs": {"image1": [image1, 0], "image2": [image2, 0]},
        },
        "14": {"class_type": "WanImageToVideo", "inputs": {"reference": ["13", 0]}},
    }


def test_feed_order_follows_the_wiring_not_the_entry_names() -> None:
    """`AIVS_REF_2` 接在 `image1` 上时，实际先喂的就是它。"""
    order = feed_order(batch_graph("12", "11"), {"AIVS_REF_1": "11", "AIVS_REF_2": "12"})
    assert order.order == ["AIVS_REF_2", "AIVS_REF_1"], "按接线测：image1 收到的是 12 号那张"
    assert (order.source, order.traced) == ("graph", True)
    assert order.moved(["AIVS_REF_1", "AIVS_REF_2"]), "与入口名的顺序不一致，必须能报出来"


def test_feed_order_agrees_with_the_entry_names_when_the_wiring_agrees() -> None:
    """线是顺着接的：顺序与入口名一致，此时不该报「实测顺序不一样」。"""
    order = feed_order(batch_graph("11", "12"), {"AIVS_REF_1": "11", "AIVS_REF_2": "12"})
    assert order.order == ["AIVS_REF_1", "AIVS_REF_2"]
    assert order.source == "graph"
    assert not order.moved(["AIVS_REF_1", "AIVS_REF_2"])


def test_feed_order_counts_the_frames_in_the_same_series_as_the_refs() -> None:
    """首尾帧与参考图挤在同一串编号里，谁是第一张完全取决于这份图怎么接的。

    `WanImageToVideo` 的 `start_image` 声明在 `reference` 后面，所以这份图先喂的是那张
    合批过的参考图——把首帧当成 `<Picture 1>` 正是老 SKILL 写死的那个假设。
    """
    graph = batch_graph("11", "12")
    graph["1"] = {"class_type": "LoadImage", "inputs": {"image": "first.png"}}
    graph["14"]["inputs"]["start_image"] = ["1", 0]
    order = feed_order(
        graph,
        {"AIVS_FIRST_FRAME": "1", "AIVS_REF_1": "11", "AIVS_REF_2": "12"},
    )
    assert order.order == ["AIVS_REF_1", "AIVS_REF_2", "AIVS_FIRST_FRAME"]
    assert order.source == "graph"


def test_feed_order_ignores_entries_whose_node_is_already_gone() -> None:
    """`detach()` 之后才数：这一次没喂的槽位已经不在图里，不该占一个号。"""
    graph = batch_graph("11", "12")
    graph.pop("12")
    graph["13"]["inputs"].pop("image2")
    order = feed_order(
        graph,
        {"AIVS_REF_1": "11", "AIVS_REF_2": "12", "AIVS_REF_3": "13"},
    )
    assert order.order == ["AIVS_REF_1", "AIVS_REF_3"], "摘掉的那个入口连编号一起消失"


def test_feed_order_says_so_when_nothing_converges() -> None:
    """一排孤立的 LoadImage（谁都没接线）：测不出顺序就如实说这一份是按入口名排的。"""
    graph = {
        "11": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
        "12": {"class_type": "LoadImage", "inputs": {"image": "b.png"}},
    }
    order = feed_order(graph, {"AIVS_REF_1": "11", "AIVS_REF_2": "12"})
    assert order.order == ["AIVS_REF_1", "AIVS_REF_2"], "退回约定顺序，而不是随便排一个"
    assert (order.source, order.traced) == ("title", False)
    assert order.groups == [["AIVS_REF_1"], ["AIVS_REF_2"]]


def test_feed_order_traces_the_group_it_can_and_admits_the_rest() -> None:
    """一半接进了合批节点、一半是孤立的：组内实测、组间按约定，`source` 说清是混的。"""
    graph = batch_graph("12", "11")
    graph["15"] = {"class_type": "LoadImage", "inputs": {"image": "c.png"}}
    order = feed_order(
        graph,
        {"AIVS_REF_1": "11", "AIVS_REF_2": "12", "AIVS_REF_3": "15"},
    )
    assert order.order == ["AIVS_REF_2", "AIVS_REF_1", "AIVS_REF_3"]
    assert order.source == "mixed"
    assert order.groups == [["AIVS_REF_2", "AIVS_REF_1"], ["AIVS_REF_3"]]


# --- ComfyUI 拒收这份图：那个 400 得说清「哪个节点的哪个输入、填的是什么」 ---
#
# 用户手里那条报错以前长这样：`HTTP 400: {"error": {"type": "prompt_outputs_...` 截在 800 字，
# 决定性的那一截（ComfyUI 那边到底有哪些候选）恰好被截掉，第一条建议还是「在流程页重新校验
# 绑定」——走预设的人照着它一步都走不了。下面前三条盯 `comfy/rejection.py` 的翻译，
# 后两条盯用户那句需求原文：「多参数的工作流，图片不足就跳过多余的参数图，可是跳过后目前是
# 400」——**跳过之后那个必填输入自己接回去**（`comfy_base.ComfyTasks._submit_graph`）。

#: 用户那台机器上真装着的 7 个 UNET。ComfyUI 用 `os.path.relpath` 拼候选名，所以 Windows 上
#: 带的是 `\`——这正是「名字看着一样却不在清单里」的常见来源。
INSTALLED_UNETS = [
    "hunyuan\\hunyuan_video_image_to_video_720p_bf16.safetensors",
    "krea2\\krea2TurboOfficialComfy_krea2TurboInt8.safetensors",
    "krea2\\krea2_turbo_fp8_scaled.safetensors",
    "minimax\\int4\\minimaxH3INT4Convrot_fl2vaPrunedInt4.safetensors",
    "minimax\\int8\\minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "minimax\\量化\\minimaxH3INT8INT4_fl2vaINT8Pruned.safetensors",
    "minimax\\量化\\minimaxH3INT8INT4_ref2vaINT8Pruned.safetensors",
]

#: 图里存着、而那台机器上并没有的那个名字（用户贴来的那条 400 里的原值）。
MISSING_UNET = "minimax/minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors"


def rejected_body(
    node_id: str,
    error: dict[str, Any],
    *,
    class_type: str = "UNETLoader",
    kind: str = "prompt_outputs_failed_validation",
) -> str:
    """照 ComfyUI `/prompt` 被拒时的原样拼一份响应体（形状抄自用户贴来的那条）。"""
    return json.dumps(
        {
            "error": {
                "type": kind,
                "message": "Prompt outputs failed validation",
                "details": "",
                "extra_info": {},
            },
            "node_errors": {
                node_id: {
                    "errors": [error],
                    "dependent_outputs": ["9"],
                    "class_type": class_type,
                }
            },
        },
        ensure_ascii=False,
    )


def not_in_list(
    name: str, value: str, candidates: list[str], *, with_extra: bool = True
) -> dict[str, Any]:
    """一条 `value_not_in_list`。

    `details` 照 ComfyUI 那句 f-string 拼（候选清单是 Python repr，反斜杠是双写的）；
    `with_extra=False` 造的是**候选只能从 details 里抠出来**的那种响应——候选超过 20 个时
    ComfyUI 自己就会把 `input_config` 置空，那条回退路径必须有人盯着。
    """
    err: dict[str, Any] = {
        "type": "value_not_in_list",
        "message": "Value not in list",
        "details": f"{name}: '{value}' not in {candidates}",
        "extra_info": {},
    }
    if with_extra:
        err["extra_info"] = {
            "input_name": name,
            "received_value": value,
            "input_config": [candidates, {}],
        }
    return err


def test_rejected_prompt_names_the_node_the_input_and_the_value() -> None:
    """用户贴来的那条 400：节点 236 的 `unet_name` 在 ComfyUI 那边对不上。

    **它与「跳过多余的参考图」无关**——模型名是图里存着的值，本工具从不改写它（只填
    `AIVS_*` 那几个入口）。以前这条错误被截在 800 字，用户只能猜是跳过槽位闯的祸。
    """
    body = rejected_body("236", not_in_list("unet_name", MISSING_UNET, INSTALLED_UNETS))

    err = rejection.to_error(400, body, {"236": {"class_type": "UNETLoader", "inputs": {}}})

    assert err.code == ErrorCode.WORKFLOW_ERROR
    assert "节点 236（UNETLoader）" in err.detail and "unet_name" in err.detail
    assert MISSING_UNET in err.detail, "填的是什么必须原样说出来，用户才认得出是哪一行"
    assert "共 7 个候选，最像的是" in err.detail
    assert "只差路径分隔符" not in err.detail, "这一次真的不是分隔符——替他认定一个会让他改错地方"
    assert any("在 ComfyUI 里把这个节点的模型重选一次" in s for s in err.suggestions)
    assert any("本工具从不改写它" in s for s in err.suggestions), "得说清这一格不是我们填的"
    assert not any("重新校验绑定" in s for s in err.suggestions), "那是绑定那条路的话，预设路走不通"
    assert err.related_ids["raw"] == body, "原文完整留档：「展开原始报错」看的就是它"
    assert err.related_ids["kind"] == "prompt_outputs_failed_validation"
    fault = rejection.faults_of(err)[0]
    assert (fault.node_id, fault.input, fault.kind) == ("236", "unet_name", "value_not_in_list")
    assert len(fault.candidates) == 7, "候选清单一个都不能丢——它就是这条错误里最有用的东西"


def test_a_model_name_that_only_differs_by_a_separator_says_exactly_that() -> None:
    """`/` 与 `\\` 在 ComfyUI 那边是两个字符串，在用户眼里是同一个文件。

    不点出来的话，他会盯着两行长得一模一样的名字找差别。这一条同时走「响应里没有
    `input_config`、候选只能从 details 里抠」那条回退路径。
    """
    installed = "minimax\\int8\\minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    typed = installed.replace("\\", "/")
    body = rejected_body(
        "236", not_in_list("unet_name", typed, [installed, *INSTALLED_UNETS[:2]], with_extra=False)
    )

    err = rejection.to_error(400, body)

    assert f"ComfyUI 上那个叫「{installed}」，只差路径分隔符或大小写" in err.detail
    assert any("只差路径分隔符 / 大小写" in s for s in err.suggestions)
    assert rejection.faults_of(err)[0].candidates[0] == installed, "候选从 details 里也抠得出来"


def test_a_prompt_with_no_outputs_left_says_which_kind_of_rejection_it_is() -> None:
    """摘节点摘到只剩没有输出的一支时 ComfyUI 回的是这一种，得按它自己的名字说。"""
    body = json.dumps(
        {
            "error": {
                "type": "prompt_no_outputs",
                "message": "Prompt has no outputs",
                "details": "",
                "extra_info": {},
            },
            "node_errors": {},
        },
        ensure_ascii=False,
    )

    err = rejection.to_error(400, body)

    assert "prompt_no_outputs" in err.detail and "这份图里没有任何输出节点" in err.detail
    assert any("摘掉了几个节点" in s for s in err.suggestions)
    assert err.related_ids["kind"] == "prompt_no_outputs"
    assert rejection.faults_of(err) == [], "这一种没有点到具体节点，别硬造一条"


#: 合批那份图：两个参考图槽位汇进一个 `ImageBatch`，而它的 `image1` 在 ComfyUI 那边是**必填**的。
#: 「参考图不够就跳过多余的槽位」正是在这种图上变成 400 的——我们把 image1 那一根线切了。
BATCH_GRAPH: dict[str, Any] = {
    **with_ref_slots(2),
    "13": {"class_type": "ImageBatch", "inputs": {"image1": ["12", 0], "image2": ["11", 0]}},
}

#: 与上面只差一处：第二个槽位先过一个缩放节点再汇进合批节点。于是被切掉的那一根原来接的是
#: `ImageScale`，而这一版真在喂的入口是 `LoadImage`——接过去只会换来一条更难懂的错误。
SCALED_BATCH_GRAPH: dict[str, Any] = {
    **with_ref_slots(2),
    "62": {
        "class_type": "ImageScale",
        "inputs": {"image": ["12", 0], "width": 832, "height": 480},
    },
    "13": {"class_type": "ImageBatch", "inputs": {"image1": ["62", 0], "image2": ["11", 0]}},
}


def missing_input_body(node_id: str, field: str, class_type: str = "ImageBatch") -> str:
    """ComfyUI 那句「Required input is missing」的原样形状。"""
    return rejected_body(
        node_id,
        {
            "type": "required_input_missing",
            "message": "Required input is missing",
            "details": field,
            "extra_info": {"input_name": field},
        },
        class_type=class_type,
    )


class BatchRejectingComfy(FakeComfy):
    """第一次提交被「image1 是必填的」拒掉，第二次收下。

    错误一律用 `rejection.to_error` 造：`related_ids` 里那几个键名只写在 `rejection.py` 一处，
    测试跟着它走而不是自己猜一份——猜的那份哪天对不上，`_missing_cuts` 会静默地不再修复。
    """

    def __init__(self, node_id: str = "13", field: str = "image1") -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []
        self._node_id = node_id
        self._field = field

    async def submit(self, graph: dict[str, Any], client_id: str) -> str:
        #: 提交出去的那一刻的样子——接回去这件事改的是同一个 dict，不留快照就看不出改了什么。
        self.calls.append(copy.deepcopy(graph))
        if len(self.calls) == 1:
            raise rejection.to_error(400, missing_input_body(self._node_id, self._field), graph)
        self.submitted = graph
        return "pid-1"


async def test_a_required_input_left_empty_by_skipping_reconnects_and_resubmits(
    tmp_path: Path,
) -> None:
    """用户那句需求原文：图片不足就跳过多余的参数图——**而跳过之后不该以一个 400 收场**。

    幸存的那个 `ImageBatch` 上 `image1` 是必填的，我们刚把它那一根线切了，于是 ComfyUI 直接
    拒收整份图。这一层认出「它点名的正是我们切的那一根」，把那一格接到本次真在喂的参考图上
    再提交一次：同一份素材喂两遍，而不是把图里那张示例图喂进去，更不是整个任务失败。
    """
    write_preset("合批图", BATCH_GRAPH)
    first = tmp_path / "first.png"
    first.write_bytes(b"PNG_HEAD")
    fake = BatchRejectingComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=first,
        refs=make_refs(tmp_path, "林小雨（常服）"),
        extra={"preset": "合批图"},
    )

    task_id = await provider.submit(req, client_id="aivs-test")

    assert task_id == "pid-1", "跳过多余的槽位不该让整个任务失败"
    assert len(fake.calls) == 2, "只重试一次"
    assert "image1" not in fake.calls[0]["13"]["inputs"], "第一次提交里那一格确实是空的"
    assert fake.calls[1]["13"]["inputs"]["image1"] == ["11", 0], "接到本次真在喂的那个参考图上"
    assert fake.calls[1]["13"]["inputs"]["image2"] == ["11", 0]
    assert "12" not in fake.calls[1], "接回去不等于把示例图放回来——那个节点照旧不在图里"
    note = next(n for n in req.notes if "必填" in n)
    assert "节点 13 的 image1" in note and "AIVS_REF_2" in note
    assert "同一份素材喂了两遍" in note, "重复喂一张是降级，绝不静默"


async def test_a_cut_that_cannot_be_reconnected_names_the_exact_input(tmp_path: Path) -> None:
    """接不上只有一种原因：图里没有第二个同类节点（`reconnect` 只认 class_type 相等）。

    这时唯一诚实的回答是「这份图要求这一格必须有素材」，并点名到「节点.输入」——绝不拿一个
    `LoadImage` 去凑 `ImageScale` 那一格，那只会换来一条更难懂的错误。
    """
    write_preset("缩放合批图", SCALED_BATCH_GRAPH)
    first = tmp_path / "first.png"
    first.write_bytes(b"PNG_HEAD")
    fake = BatchRejectingComfy()
    provider = ComfyPresetProvider(client=fake)  # type: ignore[arg-type]
    req = VideoRequest(
        mode="i2v",
        prompt="雨夜推门",
        first_frame=first,
        refs=make_refs(tmp_path, "林小雨（常服）"),
        extra={"preset": "缩放合批图"},
    )

    with pytest.raises(AppError) as caught:
        await provider.submit(req, client_id="aivs-test")

    err = caught.value
    assert len(fake.calls) == 1, "接不上就别再提交一次——那只会换来同一个 400"
    assert "节点 13 的 image1" in err.detail and "ImageScale" in err.detail
    assert "ComfyUI 因此拒收整份图" in err.detail
    assert any("给这个镜头补上对应的素材" in s for s in err.suggestions)
    assert any("改成不依赖它的接法" in s for s in err.suggestions)
    assert err.suggestions[-1].startswith("展开原始报错"), "ComfyUI 自己那几条建议照旧留在后面"
    assert not any("必填" in note for note in req.notes), "没接上就别在账单里说接上了"
