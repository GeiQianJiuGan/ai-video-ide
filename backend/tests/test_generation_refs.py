"""多参考图 R2V：账单里的角色表 / 地点参考图必须真的被喂给模型。

以前 `context.resolve()` 算出好几张参考图，`generation` 却只挑一张当首帧、剩下的悄悄丢掉
——「首尾帧容易丢人物形象」就是这么来的。所以这里钉住三件事：

  1. 首帧是账单上标了 `role="first_frame"` 的那一张（规则只在 context 里，不在两个地方各挑一遍）；
  2. 其余采用条目全部变成 `RefImage` 带给适配器，标签是账单上的标签（模型端才知道哪张是谁）；
  3. 显式指定的首帧不会再重复当一次参考图。

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
    """`_images_of` 只用得到 shot_id，不值得为它造一条真 Job。"""

    def __init__(self, shot_id: str) -> None:
        self.id = "job_fake"
        self.shot_id = shot_id


async def png(pid: str, kind: str, name: str) -> str:
    from app.services.assets import assets

    asset = await assets.register_bytes(pid, kind, name, PNG_1PX + name.encode())
    return str(asset["id"])


async def full_shot(tmp_path: Path) -> dict[str, Any]:
    """一个上下文完整的镜头：地点变体 + 有角色表的形象 + prompt。"""
    proj = await projects.create(str(tmp_path / "film"), "多参考图", 1920, 1080, 25.0, "refs")
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


async def test_the_bill_becomes_one_first_frame_and_the_rest_references(tmp_path: Path) -> None:
    made = await full_shot(tmp_path)
    pid, shot_id = made["pid"], made["shot_id"]
    snapshot = await context.snapshot(pid, shot_id)
    assert [i["role"] for i in snapshot["included"]] == ["first_frame", "reference"]

    first, last, refs = await generation._images_of(pid, FakeJob(shot_id), {"context": snapshot})
    assert first is not None and first.name.endswith(".png")
    assert last is None, "没挂末帧时是 i2v"
    # 角色表当首帧（优先级最高），地点参考图作为参考图一起喂进去——以前它在这里被丢掉
    assert [r.kind for r in refs] == ["location_reference"]
    assert "雨夜" in refs[0].label, "标签要照账单原样带走，模型端才知道这张是什么"
    assert refs[0].path.is_file()
    assert first != refs[0].path, "同一张图绝不会既当首帧又当参考图"


async def test_an_explicit_first_frame_is_not_fed_twice(tmp_path: Path) -> None:
    made = await full_shot(tmp_path)
    pid, shot_id = made["pid"], made["shot_id"]
    snapshot = await context.snapshot(pid, shot_id)
    sheet = next(i for i in snapshot["included"] if i["kind"] == "character_sheet")

    first, _, refs = await generation._images_of(
        pid,
        FakeJob(shot_id),
        {"context": snapshot, "first_frame_asset_id": sheet["asset_id"]},
    )
    assert first is not None
    assert [r.kind for r in refs] == ["location_reference"], "显式首帧不该再当一次参考图"


async def test_without_a_bill_there_are_no_reference_images(tmp_path: Path) -> None:
    """`check_context=false` 跳过账单时不该凭空造参考图——只剩显式指定的首帧。"""
    made = await full_shot(tmp_path)
    pid, shot_id = made["pid"], made["shot_id"]
    explicit = await png(pid, "upload", "手挑的首帧.png")

    first, last, refs = await generation._images_of(
        pid, FakeJob(shot_id), {"first_frame_asset_id": explicit}
    )
    assert first is not None and last is None
    assert refs == []
