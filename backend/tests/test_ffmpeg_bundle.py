"""内置 FFmpeg 的定位规则。

盯三件事：内置副本排在 PATH 之前、显式配置指错了不会被静默忽略、
找不到时的错误里必须写着「怎么拿到」（`绝不静默失败` 的四要素）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import ffmpeg as ffmpeg_tool
from app.core.config import settings
from app.core.errors import AppError

SUFFIX = ffmpeg_tool.EXE_SUFFIX


def _fake_exe(dirpath: Path, name: str = "ffmpeg") -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / f"{name}{SUFFIX}"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)
    return path


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把内置目录指到 tmp_path，避免读到真机上 bin/ 里那份。"""
    d = tmp_path / "bundle"
    d.mkdir()
    monkeypatch.setattr(settings, "bundle_dir", d)
    monkeypatch.setattr(ffmpeg_tool, "bundle_dirs", lambda: [d])
    return d


def test_bundled_wins_over_path(bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """机器上装了系统 FFmpeg 也要用内置那份：版本可控，参数组合才对得上。"""
    exe = _fake_exe(bundle)
    monkeypatch.setattr(ffmpeg_tool.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    found = ffmpeg_tool.locate("ffmpeg")
    assert found.source == "bundled"
    assert Path(found.path or "") == exe.resolve()


def test_falls_back_to_path(bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffmpeg_tool.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    found = ffmpeg_tool.locate("ffmpeg")
    assert (found.source, found.path) == ("path", "/usr/bin/ffmpeg")


def test_explicit_config_wins(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_exe(bundle)
    mine = _fake_exe(tmp_path / "mine")
    monkeypatch.setattr(settings, "ffmpeg_path", str(mine))
    found = ffmpeg_tool.locate("ffmpeg")
    assert found.source == "configured"
    assert Path(found.path or "") == mine.resolve()


def test_explicit_config_missing_is_not_silently_ignored(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """指了一个不存在的路径 → 报配置错，而不是悄悄换成内置那份。"""
    _fake_exe(bundle)
    monkeypatch.setattr(settings, "ffmpeg_path", str(tmp_path / "nope" / "ffmpeg"))
    found = ffmpeg_tool.locate("ffmpeg")
    assert found.path is None
    assert found.configured_missing

    with pytest.raises(AppError) as excinfo:
        ffmpeg_tool.require("ffmpeg")
    err = excinfo.value
    assert err.code.value == "FFMPEG_MISSING"
    assert "nope" in err.detail
    assert err.suggestions


def test_missing_everywhere_tells_how_to_get_it(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ffmpeg_tool.shutil, "which", lambda _n: None)
    with pytest.raises(AppError) as excinfo:
        ffmpeg_tool.require("ffmpeg")
    err = excinfo.value
    assert err.code.value == "FFMPEG_MISSING"
    assert err.title and err.detail and err.suggestions
    # 第一条建议必须是「拿到内置副本」——那是设计上的默认路径。
    assert "fetch_ffmpeg.py" in err.suggestions[0]
    assert err.related_ids.get("searched")


def test_bundle_dirs_includes_repo_bin() -> None:
    """开发期的下载目标 <repo>/bin 必须在查找列表里，否则脚本下完了没人用。"""
    from app.core.config import REPO_ROOT

    assert REPO_ROOT / "bin" in ffmpeg_tool.bundle_dirs()


def _make_clip(exe: str, dest: Path, seconds: float) -> bytes:
    """用 FFmpeg 自己造一段真视频当素材。

    别的导出测试只查 `GET /export/command` 的参数计划，不起进程；这里要的恰恰是「真的跑起来」，
    所以素材不能是 1×1 PNG——那不是视频，concat filter 拿它没有帧可拼。
    """
    subprocess.run(
        [
            exe,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate=10:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest.read_bytes()


def test_export_actually_runs_with_the_bundled_ffmpeg(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    """内置副本必须真的能导出成片——「找得到」和「能用」是两件事。

    没跑过 scripts/fetch_ffmpeg.py 的机器上跳过：那是缺内置副本，不是这条链路坏了。
    """
    found = ffmpeg_tool.locate("ffmpeg")
    if found.source != "bundled" or not found.path:
        pytest.skip("这台机器上还没有内置副本：先跑 scripts/fetch_ffmpeg.py")
    exe = found.path

    scene = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": "第一场"}).json()
    for i in (1, 2):
        blob = _make_clip(exe, tmp_path / f"src{i}.mp4", 1.0)
        up = client.post(
            f"/api/v1/projects/{pid}/assets/upload",
            data={"kind": "generated_video"},
            files={"file": (f"clip{i}.mp4", blob, "video/mp4")},
        )
        assert up.status_code == 201, up.text
        shot = client.post(
            f"/api/v1/projects/{pid}/scenes/{scene['id']}/shots", json={"title": f"S{i}"}
        ).json()
        ver = client.post(
            f"/api/v1/projects/{pid}/shots/{shot['id']}/versions",
            json={"asset_id": up.json()["id"], "kind": "video", "duration": 1.0},
        )
        assert ver.status_code == 201, ver.text

    assemble = client.post(f"/api/v1/projects/{pid}/timeline/assemble", json={"replace": True})
    assert assemble.status_code == 200, assemble.text

    out = tmp_path / "成片.mp4"
    resp = client.post(f"/api/v1/projects/{pid}/export", json={"path": str(out)})
    assert resp.status_code == 201, resp.text
    record = resp.json()
    assert record["path"] == str(out)
    assert record["duration"] == pytest.approx(2.0)
    assert out.is_file() and out.stat().st_size > 0
    # 成片本身也要登记成资产，否则资产库里看不到它。
    assert record["asset_id"]

    # 导出记录必须留痕，且命令里写着用的是哪一份 FFmpeg。
    exports = client.get(f"/api/v1/projects/{pid}/exports").json()
    assert [e["id"] for e in exports] == [record["id"]]
    assert exports[0]["status"] == "done"
    assert exports[0]["error"] is None
    assert exports[0]["command"].startswith(exe)
