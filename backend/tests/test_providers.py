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
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
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
    assert submitted["6"]["inputs"]["text"] == "雨夜推门"
    assert fake.uploaded == ["head.png", "sheet.png", "extra.png"], "音频连上传都不该发生"
    assert any("对白" in note and "只能喂图片" in note for note in req.notes)
    assert any("城南旧宅 雨夜" in note and "1 个" in note for note in req.notes)


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
    assert "参考图1=林小雨（常服）" in text and "参考图2" not in text, "描述只说真喂进去的那几张"


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
    assert submitted["6"]["inputs"]["text"] == "雨夜推门"
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
