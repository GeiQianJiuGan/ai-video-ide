"""一次能喂几张参考图，以及装不下时那一次确认。

以前这是应用级设置 `video.ref_limit`（默认 8）：账单算到第 8 条就把剩下的划掉。那个数字
与预设里真正标了几个 `AIVS_REF_*` 是两回事，配错一边就白丢用户的角色图 / 场景图，
而且丢得很安静。所以这里钉住四件事：

  1. **上限问适配层**（`registry.ref_capacity()`）：ComfyUI 预设数自己的槽位，
     REST 合同不限张数，没有一份可数的图（没选预设）也不限张数——绝不凭空造一个数字；
  2. **账单不截断**：超出的条目照旧 `included`，只多一个 `over_capacity` 标记，
     `capacity` 块里写清会丢几张、丢哪几张；
  3. **生成前先拦一次** `REF_OVER_CAPACITY`（不是失败，是确认），`allow_ref_drop=True`
     才真入队；单个镜头、整幕、整片编排三条路一视同仁；
  4. 整幕 / 整片是**先扫完再动手**：没确认时一个任务都不该入队，否则用户点了确认
     会把前一半再入队一遍。

不走 TestClient：这是 service 层的异步调用，工程也在同一个事件循环里建，
免得 aiosqlite 的连接跨循环（同 tests/test_generation_refs.py）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.core.errors import AppError
from app.generation.providers import presets, registry
from app.services.cast import cast
from app.services.context import context, ref_capacity
from app.services.generation import generation
from app.services.projects import projects
from app.services.sequence import sequence
from app.services.story import story
from app.services.world import world
from tests.conftest import PNG_1PX

#: 一份最小的可用预设：必需的入口标题齐全，参考图槽位数由 `with_ref_slots` 决定。
BASE_GRAPH: dict[str, Any] = {
    "1": {
        "class_type": "LoadImage",
        "inputs": {"image": "原来的.png"},
        "_meta": {"title": "AIVS_FIRST_FRAME"},
    },
    "3": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "旧提示词"},
        "_meta": {"title": "AIVS_PROMPT"},
    },
}


def with_ref_slots(count: int) -> dict[str, Any]:
    graph = {k: dict(v) for k, v in BASE_GRAPH.items()}
    for i in range(1, count + 1):
        graph[f"1{i}"] = {
            "class_type": "LoadImage",
            "inputs": {"image": f"占位{i}.png"},
            "_meta": {"title": f"AIVS_REF_{i}"},
        }
    return graph


def use_preset(monkeypatch: pytest.MonkeyPatch, name: str, slots: int) -> None:
    """存一份有 `slots` 个参考图槽位的预设，并把它设成默认预设。"""
    presets.save(name, json.dumps(with_ref_slots(slots), ensure_ascii=False))
    monkeypatch.setattr(settings, "video_provider", "comfy_preset")
    monkeypatch.setattr(settings, "video_preset", name)
    registry.reset()


async def png(pid: str, kind: str, name: str) -> str:
    from app.services.assets import assets

    asset = await assets.register_bytes(pid, kind, name, PNG_1PX + name.encode())
    return str(asset["id"])


async def three_ref_shot(tmp_path: Path) -> dict[str, Any]:
    """一个采用了 3 张图的完整镜头：角色表 + 地点参考 + 道具参考（各 1 张）。

    **这 3 张全都是参考图**：首帧只认镜头上显式指定的那一张（`shot.first_frame_asset_id`）
    或上游末帧，这个镜头两样都没有，所以没有任何一条会被提拔成首帧去占掉一个槽位。
    配一个 1 槽位的预设就差 2 张——优先级最低的先被挤掉（道具图、然后是地点图）。
    """
    proj = await projects.create(str(tmp_path / "film"), "参考图上限", 1920, 1080, 25.0, "cap")
    pid = proj.id
    loc = await world.create_location(pid, {"name": "城南旧宅"})
    variant = await world.create_variant(pid, loc["id"], {"name": "雨夜", "time_of_day": "夜"})
    await world.add_variant_reference(
        pid, variant["id"], await png(pid, "location_reference", "loc.png"), "35mm", None
    )
    char = await cast.create_character(pid, {"name": "林昭"})
    appearance = (await cast.list_appearances(pid, char["id"]))[0]
    await cast.add_sheet(pid, appearance["id"], await png(pid, "character_sheet", "sheet.png"))
    prop = await world.create_prop(pid, {"name": "旧怀表"})
    await world.add_prop_reference(pid, prop["id"], await png(pid, "prop_reference", "prop.png"))
    scene = await story.create_scene(
        pid, {"title": "第一场", "location_variant_id": variant["id"], "prompt": "雨夜"}
    )
    shot = await story.create_shot(pid, scene["id"], {"title": "推近", "prompt": "雨夜，林昭推门"})
    await story.set_shot_cast(pid, shot["id"], [appearance["id"]])
    await story.set_shot_props(pid, shot["id"], [{"prop_id": prop["id"], "state": "present"}])
    return {"pid": pid, "scene_id": scene["id"], "shot_id": shot["id"]}


# --- 上限从哪来 ---


def test_capacity_counts_the_slots_in_the_default_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    use_preset(monkeypatch, "三个槽位", 3)
    cap = ref_capacity()
    assert cap.limit == 3, "上限就是预设里标了几个 AIVS_REF_*"
    assert cap.source == "三个槽位", "「这个数字哪来的」必须说得出来"
    assert "3" in cap.detail
    assert cap.dropped(5) == 2 and cap.dropped(2) == 0


def test_capacity_without_a_preset_is_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    """没选预设时不限张数：与其用一个猜的数字丢掉用户的角色图，不如让它在提交时报错。"""
    monkeypatch.setattr(settings, "video_provider", "comfy_preset")
    monkeypatch.setattr(settings, "video_preset", "")
    registry.reset()
    cap = ref_capacity()
    assert cap.limit is None
    assert cap.dropped(99) == 0
    assert "预设" in cap.detail


def test_a_preset_without_ref_slots_reports_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 是一个有意义的答案，不是「查不出来」——这份图一张参考图都收不了。"""
    use_preset(monkeypatch, "没有槽位", 0)
    cap = ref_capacity()
    assert cap.limit == 0
    assert cap.dropped(1) == 1
    assert "AIVS_REF_" in cap.detail and "首帧" in cap.detail


def test_the_rest_contract_is_unlimited_and_the_binding_route_only_feeds_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """应用级那一问（还没有工程上下文）：两条非预设的路各自答什么。

    `comfy_workflow` 的参考图**答不上来**（几张取决于这个能力绑的那份图，由
    `services/route.py::capacity()` 数绑定行），但参考视频 / 音频**确定是 0**——绑定表里
    根本没有能接它们的槽位。这个 0 不是「不知道」：它会让账单如实说出「你挂的那段对白
    音频这条路喂不进去」，回 `None` 的话用户会以为送出去了。
    """
    monkeypatch.setattr(settings, "video_provider", "http_api")
    monkeypatch.setattr(settings, "video_base_url", "http://127.0.0.1:9/api")
    registry.reset()
    assert ref_capacity().limit is None, "REST 合同把 refs 整组发过去，没有槽位这回事"

    monkeypatch.setattr(settings, "video_provider", "comfy_workflow")
    registry.reset()
    bound = ref_capacity()
    assert bound.limit is None, "几张取决于绑的那份图，凭空造一个数字只会白丢角色图"
    assert bound.source == "工作流绑定"
    assert (bound.video, bound.audio) == (0, 0), "这条路只喂图片，0 是事实不是「不知道」"
    assert "只喂图片" in bound.detail


def test_a_rewritten_preset_is_recounted(monkeypatch: pytest.MonkeyPatch) -> None:
    """槽位数是带缓存的（每个镜头都要问一次），但用户在 ComfyUI 里加了槽位就得立刻算数。"""
    use_preset(monkeypatch, "会变的图", 1)
    assert ref_capacity().limit == 1
    presets.save("会变的图", json.dumps(with_ref_slots(4), ensure_ascii=False))
    assert ref_capacity().limit == 4, "重新上传同名预设后不能还报旧的槽位数"


# --- 账单只报不删 ---


async def test_the_bill_keeps_everything_and_marks_what_will_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await three_ref_shot(tmp_path)
    use_preset(monkeypatch, "只有一个槽位", 1)
    ctx = await context.resolve(made["pid"], made["shot_id"])

    assert ctx["included_count"] == 3, "账单绝不截断：采用了几条就是几条"
    cap = ctx["capacity"]
    assert (cap["limit"], cap["ref_count"], cap["dropped"], cap["over"]) == (1, 3, 2, True)
    assert cap["source"] == "只有一个槽位"
    # 挤掉的是优先级最低的那几条（地点图、道具图），角色表最先保住
    labels = {i["kind"]: i["label"] for i in ctx["items"]}
    assert cap["dropped_labels"] == [labels["location_reference"], labels["prop_reference"]]
    over = [i["kind"] for i in ctx["items"] if i.get("over_capacity")]
    assert over == ["location_reference", "prop_reference"]
    assert all(i["included"] for i in ctx["items"]), "「装不下」和「没采用」是两件事"
    assert [i["role"] for i in ctx["items"]] == ["reference"] * 3, "没指定首帧就一张都不许提拔"
    assert {i["media"] for i in ctx["items"]} == {"image"}
    # 按媒体分开报：图片这一族装不下，视频 / 音频这两族一个都没采用，谈不上装不下
    per_media = cap["media"]
    assert (per_media["image"]["ref_count"], per_media["image"]["dropped"]) == (3, 2)
    assert [per_media[m]["over"] for m in ("video", "audio")] == [False, False]


async def test_the_snapshot_freezes_the_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事后要说得清「为什么少喂了一张」，所以当时的上限也冻结进版本。"""
    made = await three_ref_shot(tmp_path)
    use_preset(monkeypatch, "只有一个槽位", 1)
    snap = await context.snapshot(made["pid"], made["shot_id"])
    assert snap["capacity"]["dropped"] == 2
    assert [i["kind"] for i in snap["included"] if i["over_capacity"]] == [
        "location_reference",
        "prop_reference",
    ]
    assert {i["media"] for i in snap["included"]} == {"image"}, "冻结的条目也要带媒体类型"


async def test_enough_slots_means_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await three_ref_shot(tmp_path)
    use_preset(monkeypatch, "三个槽位刚好", 3)
    cap = (await context.resolve(made["pid"], made["shot_id"]))["capacity"]
    assert (cap["limit"], cap["ref_count"], cap["over"]) == (3, 3, False)
    assert cap["dropped_labels"] == []


# --- 生成前那一次确认 ---


async def test_generating_one_shot_asks_before_dropping_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await three_ref_shot(tmp_path)
    pid, shot_id = made["pid"], made["shot_id"]
    use_preset(monkeypatch, "只有一个槽位", 1)
    await generation.pause(pid)  # 别让 pump 真去连 ComfyUI

    with pytest.raises(AppError) as caught:
        await generation.enqueue_shot(pid, shot_id)
    err = caught.value
    assert err.code == "REF_OVER_CAPACITY"
    assert "会丢 2 个" in err.title
    assert "只能喂 1张" in err.detail
    assert err.related_ids["confirm"] == "allow_ref_drop", "前端照它知道带哪个参数重来"
    assert err.related_ids["shot_ids"] == [shot_id]
    assert any("AIVS_REF" in s for s in err.suggestions), "得给一条不丢图的出路"
    assert await generation.list_jobs(pid) == [], "没确认之前一个任务都不该入队"

    job = await generation.enqueue_shot(pid, shot_id, allow_ref_drop=True)
    assert job["shot_id"] == shot_id
    assert len(await generation.list_jobs(pid)) == 1


async def test_generating_a_whole_scene_scans_before_enqueueing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整幕不能「一半入队、一半等确认」：那样点一下确认就会重复生成前一半。"""
    made = await three_ref_shot(tmp_path)
    pid, scene_id = made["pid"], made["scene_id"]
    char = (await cast.list_characters(pid))[0]
    appearance = (await cast.list_appearances(pid, char["id"]))[0]
    other = await story.create_shot(pid, scene_id, {"title": "跟拍", "prompt": "跟着走"})
    await story.set_shot_cast(pid, other["id"], [appearance["id"]])
    await story.set_shot_props(
        pid, other["id"], [{"prop_id": (await world.list_props(pid))[0]["id"], "state": "present"}]
    )
    use_preset(monkeypatch, "只有一个槽位", 1)
    await generation.pause(pid)

    with pytest.raises(AppError) as caught:
        await generation.enqueue_scene(pid, scene_id)
    assert caught.value.code == "REF_OVER_CAPACITY"
    assert await generation.list_jobs(pid) == []

    result = await generation.enqueue_scene(pid, scene_id, allow_ref_drop=True)
    assert len(result["queued"]) == 2 and result["skipped"] == []
    assert len(await generation.list_jobs(pid)) == 2


async def test_the_sequence_bill_lists_the_shots_that_will_lose_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await three_ref_shot(tmp_path)
    pid = made["pid"]
    use_preset(monkeypatch, "只有一个槽位", 1)
    await generation.pause(pid)

    bill = await sequence.plan(pid)
    assert [d["shot_id"] for d in bill["ref_drops"]] == [made["shot_id"]]
    assert bill["ref_drops"][0]["dropped"] == 2
    assert [b["media"] for b in bill["ref_drops"][0]["media"]] == ["image"], "按媒体分族报"
    assert bill["ref_drops"][0]["scene_index_no"] == 1
    assert any("参考图" in n for n in bill["notes"]), "账单上要看得见，不能只在报错里说"
    assert bill["total_jobs"] == 1, "装不下不是 blocker：确认一下照样能生成"
    assert bill["blockers"] == []

    with pytest.raises(AppError) as caught:
        await sequence.run(pid)
    assert caught.value.code == "REF_OVER_CAPACITY"
    assert await generation.list_jobs(pid) == []

    done = await sequence.run(pid, allow_ref_drop=True)
    assert len(done["queued"]) == 1 and done["skipped"] == []
    assert len(await generation.list_jobs(pid)) == 1
