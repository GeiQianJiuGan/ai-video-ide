"""Step 2 验收：真末帧抽取。

单线程续接的全部前提就是这一步——上一段的**真末帧**当下一段的首帧。
所以这里测五件事：

  1. 抽出来的是一张真 PNG，并且登记成 `Asset(kind="frame")`（有登记才能进 context 账单）；
  2. 同一个 (asset, at) 再问一次直接复用，不重复起 FFmpeg 进程；
  3. 没有 FFmpeg 时报 `FFMPEG_MISSING`，并且建议里要给出**另一条路**——改成转场衔接；
  4. 帧是**临时资源不是工程资产**：落 `cache/frames/`，不进资产总账、不算孤儿；
  5. **源成片一删，从它抽的帧跟着删**（登记与磁盘文件都要没），并且删了哪些要说出来。

这个文件不走 TestClient：抽帧是 service 层的异步调用，工程也在同一个事件循环里建，
免得 aiosqlite 的连接跨循环。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core import ffmpeg as ffmpeg_tool
from app.core.errors import AppError
from app.services.assets import assets as asset_service
from app.services.frames import frames
from app.services.projects import projects


def ffmpeg_or_skip() -> str:
    located = ffmpeg_tool.locate("ffmpeg")
    if not located.path:
        pytest.skip("这台机器上没有 FFmpeg（跑一次 scripts/fetch_ffmpeg.py）")
    return located.path


async def make_project(tmp_path: Path) -> str:
    proj = await projects.create(str(tmp_path / "film"), "抽帧测试片", 320, 240, 25.0, "frames")
    return proj.id


async def make_clip(binary: str, target: Path, seconds: float = 1.0) -> Path:
    """造一段真视频当上游成片——用 lavfi，不依赖任何素材文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        binary,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={seconds}:size=64x64:rate=10",
        "-pix_fmt",
        "yuv420p",
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    assert target.is_file(), "造样片失败，后面的断言没有意义"  # noqa: ASYNC240 - 测试里的本地文件检查
    return target


async def test_extract_tail_frame_registers_a_frame_asset(tmp_path: Path) -> None:
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    clip = await make_clip(binary, tmp_path / "raw" / "上一幕.mp4")
    source = await asset_service.register_path(pid, "generated_video", str(clip))

    frame = await frames.extract(pid, source["id"], "end")
    assert frame["kind"] == "frame"
    assert frame["reused"] is False
    assert frame["path"].startswith("cache/frames/"), (
        "帧是临时资源，落 cache/ 而不是 assets/（见 KIND_DIR['frame']）"
    )
    on_disk = projects.get(pid).dir / frame["path"]
    assert on_disk.is_file() and on_disk.stat().st_size > 0
    assert on_disk.read_bytes()[:8] == bytes.fromhex("89504e470d0a1a0a"), "抽出来的必须是 PNG"
    meta = json.loads(frame["meta_json"])
    assert meta["from_asset_id"] == source["id"], "context 靠这条出处认出「上游末帧」"
    assert meta["at"] == "end"

    listed = await asset_service.list_assets(pid, kind="frame")
    assert [a["id"] for a in listed] == [frame["id"]], "抽出来的帧必须能被列出来"


async def test_frames_are_temporary_and_stay_off_the_asset_ledger(tmp_path: Path) -> None:
    """帧不算工程资产：资产总账与孤儿扫描都不该出现它。

    它没有任何 `AssetRef`，如果算进去，孤儿列表会被一堆「我没导入过的图」刷满，
    而用户其实无从判断哪一张能删。要看它们得显式传 kind="frame"。
    """
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    clip = await make_clip(binary, tmp_path / "raw" / "上一幕.mp4")
    source = await asset_service.register_path(pid, "generated_video", str(clip))
    frame = await frames.extract(pid, source["id"], "end")

    ledger = {a["id"] for a in await asset_service.list_assets(pid)}
    assert source["id"] in ledger, "成片本身当然还在总账上"
    assert frame["id"] not in ledger, "临时帧不进资产总账"
    assert frame["id"] not in {a["id"] for a in await asset_service.orphans(pid)}, (
        "临时帧没有引用，但它不是孤儿——它跟着源片走"
    )


async def test_deleting_the_clip_takes_its_frames_with_it(tmp_path: Path) -> None:
    """成片删了，从它抽的首尾帧也得删——登记、磁盘文件都要没，并且要说出来。"""
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    clip = await make_clip(binary, tmp_path / "raw" / "上一幕.mp4")
    source = await asset_service.register_path(pid, "generated_video", str(clip))
    tail = await frames.extract(pid, source["id"], "end")
    head = await frames.extract(pid, source["id"], "start")
    root = projects.get(pid).dir
    assert (root / tail["path"]).is_file() and (root / head["path"]).is_file()

    out = await asset_service.delete(pid, source["id"])
    assert out["file_removed"] is True
    assert {r["id"] for r in out["derived_removed"]} == {tail["id"], head["id"]}, (
        "连带删除绝不静默：删了哪几张要出现在返回值里"
    )
    assert not (root / tail["path"]).exists() and not (root / head["path"]).exists()
    left = await asset_service.list_assets(pid, kind="frame")
    assert left == [], "登记也要跟着走，不然界面上会指着一个不存在的文件"


async def test_deleting_a_frame_alone_does_not_touch_the_clip(tmp_path: Path) -> None:
    """反过来不成立：单独删掉一张帧，源成片必须原样留着（需要时能重抽）。"""
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    clip = await make_clip(binary, tmp_path / "raw" / "上一幕.mp4")
    source = await asset_service.register_path(pid, "generated_video", str(clip))
    tail = await frames.extract(pid, source["id"], "end")

    out = await asset_service.delete(pid, tail["id"])
    assert out["derived_removed"] == [], "帧本身没有派生物"
    assert (projects.get(pid).dir / source["path"]).is_file(), "删一张帧不该动到成片"
    again = await frames.extract(pid, source["id"], "end")
    assert again["reused"] is False, "删掉之后能重新抽出来——它本来就是可再生的"


async def test_extracting_the_same_frame_twice_reuses_it(tmp_path: Path) -> None:
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    clip = await make_clip(binary, tmp_path / "raw" / "上一幕.mp4")
    source = await asset_service.register_path(pid, "generated_video", str(clip))

    first = await frames.extract(pid, source["id"], "end")
    again = await frames.extract(pid, source["id"], "end")
    assert again["id"] == first["id"]
    assert again["reused"] is True, "一次编排里同一个末帧会被问很多次，不能每次都起进程"

    head = await frames.extract(pid, source["id"], "start")
    assert head["id"] != first["id"], "首帧与末帧是两个不同的资产"


async def test_extracting_from_a_non_video_says_so(tmp_path: Path) -> None:
    ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    png = tmp_path / "raw" / "角色表.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"not-really-a-png")
    source = await asset_service.register_path(pid, "character_sheet", str(png))

    with pytest.raises(AppError) as caught:
        await frames.extract(pid, source["id"], "end")
    err = caught.value
    assert err.code == "VALIDATION_ERROR"
    assert err.suggestions, "绝不静默失败：必须给出下一步"


async def test_missing_source_file_offers_the_transition_way_out(tmp_path: Path) -> None:
    pid = await make_project(tmp_path)
    stray = tmp_path / "raw" / "已删除.mp4"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"fake-mp4-bytes")
    source = await asset_service.register_path(pid, "generated_video", str(stray))
    (projects.get(pid).dir / source["path"]).unlink()

    with pytest.raises(AppError) as caught:
        await frames.extract(pid, source["id"], "end")
    err = caught.value
    assert err.code == "MISSING_ASSET"
    assert any("转场" in s for s in err.suggestions), "抽不出末帧时必须指出另一条路"


async def test_without_ffmpeg_the_error_names_the_alternative(
    tmp_path: Path, no_ffmpeg: None
) -> None:
    pid = await make_project(tmp_path)
    clip = tmp_path / "raw" / "上一幕.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"fake-mp4-bytes")
    source = await asset_service.register_path(pid, "generated_video", str(clip))

    with pytest.raises(AppError) as caught:
        await frames.extract(pid, source["id"], "end")
    err = caught.value
    assert err.code == "FFMPEG_MISSING"
    assert err.suggestions
