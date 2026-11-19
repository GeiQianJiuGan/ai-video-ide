"""预览代理：线上部署时时间线整片播放不卡的关键。

AI 成片是 1080p / 十几到二十几 Mbps，moov 还在文件尾；浏览器隔着公网边下边播一定卡。
代理把它压到 ≤720p、码率封顶、保留音轨、moov 前置，时间线把它的路径带给预览器。
导出永远走原素材，这里不碰。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core import ffmpeg as ffmpeg_tool
from app.core.errors import AppError, ErrorCode
from app.services.assets import assets as asset_service
from app.services.projects import projects
from app.services.timeline import preview_proxy_rel
from app.services.timeline import timeline as timeline_service
from tests.test_timeline_audio import ffmpeg_or_skip, make_clip, make_project, place, video_track


async def test_proxy_is_faststart_keeps_audio_and_shows_up_on_the_timeline(tmp_path: Path) -> None:
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    src = await asset_service.register_path(
        pid, "generated_video", str(await make_clip(binary, tmp_path / "raw" / "第一幕.mp4"))
    )
    clip = await place(pid, src["id"])
    assert clip["proxy_path"] is None, "还没生成代理时不能谎报"

    out = await timeline_service.ensure_proxy(pid, src["id"])
    assert out == {
        "asset_id": src["id"],
        "proxy_path": preview_proxy_rel(src["id"]),
        "reused": False,
    }
    proxy = projects.get(pid).dir / out["proxy_path"]
    data = proxy.read_bytes()
    assert 0 <= data.find(b"moov") < data.find(b"mdat"), "moov 必须前置，浏览器拿到文件头就能起播"
    assert not list(proxy.parent.glob("*.part.mp4")), "临时文件要改名落地，不能留下半截"

    probe = ffmpeg_tool.locate("ffprobe")
    if probe.available:
        proc = await asyncio.create_subprocess_exec(
            probe.path or "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(proxy),
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        assert set(stdout.decode().split()) == {"video", "audio"}, "预览里要能听到声音"

    again = await timeline_service.ensure_proxy(pid, src["id"])
    assert again["reused"] is True
    refreshed = next(c for c in (await video_track(pid))["clips"] if c["id"] == clip["id"])
    assert refreshed["proxy_path"] == out["proxy_path"]
    assert refreshed["asset_path"] == src["path"], "导出仍然认原素材"


async def test_proxy_refuses_non_video_assets(tmp_path: Path) -> None:
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    png = tmp_path / "raw" / "定妆.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        binary,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=32x32",
        "-frames:v",
        "1",
        str(png),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    image = await asset_service.register_path(pid, "generated_image", str(png))
    try:
        await timeline_service.ensure_proxy(pid, image["id"])
    except AppError as err:
        assert err.code == ErrorCode.VALIDATION_ERROR
        assert err.suggestions
    else:
        raise AssertionError("图片不该被转成视频代理")


async def test_files_endpoint_serves_the_proxy_only_when_asked(tmp_path: Path) -> None:
    """`?preview=true` 有代理播代理、没有回原片；不带参数永远是原片（导出 / 下载认它）。"""
    from app.api.files import preview_of

    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    src = await asset_service.register_path(
        pid, "generated_video", str(await make_clip(binary, tmp_path / "raw" / "片.mp4"))
    )
    root = projects.get(pid).dir
    original = (root / src["path"]).resolve()
    assert await preview_of(pid, root, original, src["path"]) is None, "还没生成代理时回原片"

    out = await timeline_service.ensure_proxy(pid, src["id"])
    assert await preview_of(pid, root, original, src["path"]) == root / out["proxy_path"]
