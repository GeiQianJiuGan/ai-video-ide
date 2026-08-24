"""参考素材 R2V：账单里的角色表 / 地点参考图 / 参考视频 / 参考音频必须真的被喂给模型。

以前 `context.resolve()` 算出好几张参考图，`generation` 却只挑一张当首帧、剩下的悄悄丢掉
——「首尾帧容易丢人物形象」就是这么来的。补上 `refs` 之后又留了一个更坏的毛病：
账单里优先级最高的那一条会被**自动提拔**成首帧，于是角色三视图成了画面第一格。
所以这里钉住四件事：

  1. **首帧只认显式指定**——镜头上的 `first_frame_asset_id`、入队参数里的那一个，
     或上游镜头的真末帧。都没有时首帧就是 `None`，一条参考素材都不许被提拔；
  2. 采用的条目全部变成 `RefAsset` 带给适配器，标签是账单上的标签
     （模型端才知道哪个是谁），并各自带上 `media`；
  3. 显式指定的首帧 / 末帧不会再重复当一次参考素材；
  4. 规则只在 `context._assign_roles` 一处，`generation` 照账单读，不再自己挑一遍。

不走 TestClient：这是 service 层的异步调用，工程也在同一个事件循环里建，
免得 aiosqlite 的连接跨循环（同 tests/test_frames.py）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.cast import cast
from app.services.context import context
from app.services.generation import generation
from app.services.projects import projects
from app.services.story import story
from app.services.world import world
from tests.conftest import PNG_1PX


class FakeJob:
    """`_images_of` 只用得到 shot_id 与 kind，不值得为它造一条真 Job。"""

    def __init__(self, shot_id: str, kind: str = "image2video") -> None:
        self.id = "job_fake"
        self.shot_id = shot_id
        self.kind = kind


async def png(pid: str, kind: str, name: str) -> str:
    from app.services.assets import assets

    asset = await assets.register_bytes(pid, kind, name, PNG_1PX + name.encode())
    return str(asset["id"])


async def media_asset(pid: str, kind: str, name: str) -> str:
    """一个非图片素材。媒体只看后缀（`assets.kind_of_suffix`），内容无所谓。"""
    from app.services.assets import assets

    asset = await assets.register_bytes(pid, kind, name, b"NOT-REALLY-" + name.encode())
    return str(asset["id"])


async def file_of(pid: str, asset_id: str) -> str:
    """资产落盘名是内容哈希（`assets.register_bytes`），所以断言要跟它对，不是原文件名。"""
    return Path((await context.asset_of(pid, asset_id))["path"]).name


async def full_shot(tmp_path: Path) -> dict[str, Any]:
    """一个上下文完整的镜头：地点变体 + 有角色表的形象 + prompt。**没有首帧**。"""
    proj = await projects.create(str(tmp_path / "film"), "多参考素材", 1920, 1080, 25.0, "refs")
    pid = proj.id
    loc = await world.create_location(pid, {"name": "城南旧宅"})
    variant = await world.create_variant(pid, loc["id"], {"name": "雨夜", "time_of_day": "夜"})
    await world.add_variant_reference(
        pid, variant["id"], await png(pid, "location_reference", "loc.png"), "35mm", None
    )
    char = await cast.create_character(pid, {"name": "林昭"})
    appearance = (await cast.list_appearances(pid, char["id"]))[0]
    await cast.add_sheet(pid, appearance["id"], await png(pid, "character_sheet", "sheet.png"))
    scene = await story.create_scene(
        pid, {"title": "第一场", "location_variant_id": variant["id"], "prompt": "雨夜"}
    )
    shot = await story.create_shot(pid, scene["id"], {"title": "推近", "prompt": "雨夜，林昭推门"})
    await story.set_shot_cast(pid, shot["id"], [appearance["id"]])
    return {"pid": pid, "shot_id": shot["id"], "dir": proj.dir}


async def test_nothing_is_promoted_to_first_frame(tmp_path: Path) -> None:
    """没指定首帧时，账单里那两张全是参考素材——三视图当画面第一格就是这个 bug。"""
    made = await full_shot(tmp_path)
    pid, shot_id = made["pid"], made["shot_id"]
    snapshot = await context.snapshot(pid, shot_id)
    assert [i["role"] for i in snapshot["included"]] == ["reference", "reference"]

    first, last, refs = await generation._images_of(pid, FakeJob(shot_id), {"context": snapshot})
    assert (first, last) == (None, None), "首帧只认显式指定，绝不从参考素材里提拔"
    assert [r.kind for r in refs] == ["character_sheet", "location_reference"]
    assert [r.media for r in refs] == ["image", "image"]
    assert "雨夜" in refs[1].label, "标签要照账单原样带走，模型端才知道这个是什么"
    assert all(r.path.is_file() for r in refs)


async def test_the_shot_slot_is_the_first_frame_and_is_not_fed_twice(tmp_path: Path) -> None:
    """「哪一张是首帧」是用户按下去的那一下：镜头上的槽位。"""
    made = await full_shot(tmp_path)
    pid, shot_id = made["pid"], made["shot_id"]
    picked = await png(pid, "upload", "手挑的首帧.png")
    await story.update_shot(pid, shot_id, {"first_frame_asset_id": picked})

    snapshot = await context.snapshot(pid, shot_id)
    roles = {i["kind"]: i["role"] for i in snapshot["included"]}
    assert roles["first_frame"] == "first_frame"
    assert roles["character_sheet"] == "reference", "槽位填了，角色表照旧是参考素材"

    first, _, refs = await generation._images_of(pid, FakeJob(shot_id), {"context": snapshot})
    assert first is not None and first.name == await file_of(pid, picked)
    assert [r.kind for r in refs] == ["character_sheet", "location_reference"]
    assert first not in [r.path for r in refs], "同一张图绝不会既当首帧又当参考素材"


async def test_the_enqueue_parameter_still_wins(tmp_path: Path) -> None:
    """入队时显式传的那一张优先于镜头槽位——转场就是这么指定末帧的。"""
    made = await full_shot(tmp_path)
    pid, shot_id = made["pid"], made["shot_id"]
    slot = await png(pid, "upload", "槽位里的.png")
    explicit = await png(pid, "upload", "入队时传的.png")
    await story.update_shot(pid, shot_id, {"first_frame_asset_id": slot})
    snapshot = await context.snapshot(pid, shot_id)

    first, last, refs = await generation._images_of(
        pid,
        FakeJob(shot_id, "first_last_frame"),
        {"context": snapshot, "first_frame_asset_id": explicit, "last_frame_asset_id": slot},
    )
    assert first is not None and first.name == await file_of(pid, explicit)
    assert last is not None and last.name == await file_of(pid, slot)
    assert [r.kind for r in refs] == ["character_sheet", "location_reference"]


async def test_clearing_the_slot_takes_the_first_frame_away(tmp_path: Path) -> None:
    """空串清空槽位（PATCH 里的 null 会被 exclude_none 吃掉），首帧跟着回到「没有」。"""
    made = await full_shot(tmp_path)
    pid, shot_id = made["pid"], made["shot_id"]
    picked = await png(pid, "upload", "手挑的首帧.png")
    await story.update_shot(pid, shot_id, {"first_frame_asset_id": picked})
    await story.update_shot(pid, shot_id, {"first_frame_asset_id": ""})

    shot = await story.get_shot(pid, shot_id)
    assert (shot["first_frame_asset_id"], shot["first_frame_path"]) == (None, None)
    snapshot = await context.snapshot(pid, shot_id)
    assert all(i["kind"] != "first_frame" for i in snapshot["included"])
    first, _, _ = await generation._images_of(pid, FakeJob(shot_id), {"context": snapshot})
    assert first is None


async def test_video_and_audio_refs_reach_the_provider(tmp_path: Path) -> None:
    """R2V 的参考素材不只有图：手动加进账单的视频 / 音频要带着 media 一起送出去。"""
    made = await full_shot(tmp_path)
    pid, shot_id = made["pid"], made["shot_id"]
    clip = await media_asset(pid, "upload", "动作参考.mp4")
    voice = await media_asset(pid, "upload", "对白.wav")
    await context.override(pid, shot_id, "add", {"asset_id": clip, "label": "推门的动作"})
    await context.override(pid, shot_id, "add", {"asset_id": voice, "label": "林昭的台词"})

    snapshot = await context.snapshot(pid, shot_id)
    medias = {i["kind"]: i["media"] for i in snapshot["included"]}
    assert medias["character_sheet"] == "image"
    assert sorted(i["media"] for i in snapshot["included"] if i["kind"] == "manual") == [
        "audio",
        "video",
    ]

    _, _, refs = await generation._images_of(pid, FakeJob(shot_id), {"context": snapshot})
    by_media = {r.media: r for r in refs}
    assert by_media["video"].path.name == await file_of(pid, clip)
    assert by_media["audio"].path.name == await file_of(pid, voice)
    assert by_media["video"].media_label == "参考视频"


async def test_an_unknown_suffix_is_excluded_with_a_reason(tmp_path: Path) -> None:
    """认不出后缀的文件不许悄悄喂出去——账单上写清为什么没采用。"""
    made = await full_shot(tmp_path)
    pid, shot_id = made["pid"], made["shot_id"]
    weird = await media_asset(pid, "upload", "说明.txt")
    await context.override(pid, shot_id, "add", {"asset_id": weird, "label": "一段说明"})

    bill = await context.resolve(pid, shot_id)
    item = next(i for i in bill["items"] if i["kind"] == "manual")
    assert item["included"] is False
    assert "图片 / 视频 / 音频" in item["reason"]

    _, _, refs = await generation._images_of(
        pid, FakeJob(shot_id), {"context": await context.snapshot(pid, shot_id)}
    )
    assert all(r.path.suffix != ".txt" for r in refs)


async def test_without_a_bill_there_are_no_reference_materials(tmp_path: Path) -> None:
    """`check_context=false` 跳过账单时不该凭空造参考素材——只剩显式指定的首帧。"""
    made = await full_shot(tmp_path)
    pid, shot_id = made["pid"], made["shot_id"]
    explicit = await png(pid, "upload", "手挑的首帧.png")

    first, last, refs = await generation._images_of(
        pid, FakeJob(shot_id), {"first_frame_asset_id": explicit}
    )
    assert first is not None and last is None
    assert refs == []
