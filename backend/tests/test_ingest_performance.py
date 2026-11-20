"""提速后仍保持区间时间、缓存失效、写入隔离与提示词覆盖语义。"""

import asyncio
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core import ffmpeg as ffmpeg_tool
from app.services.ingest import ingest
from tests.test_ingest import make_real_video
from tests.test_shot_frame_slots import a_shot


def test_scene_prompt_override_clear_is_one_atomic_update_scoped_to_scene(client, pid):
    root = f"/api/v1/projects/{pid}"
    sid = a_shot(client, pid)
    other = a_shot(client, pid)
    shot = client.get(f"{root}/shots/{sid}").json()
    client.patch(f"{root}/shots/{sid}", json={"prompt": "独立", "duration": 7})
    client.patch(f"{root}/shots/{other}", json={"prompt": "保留"})
    response = client.patch(
        f"{root}/scenes/{shot['scene_id']}", json={"prompt": "共用", "clear_shot_prompts": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["prompt"] == "共用"
    after = client.get(f"{root}/shots/{sid}").json()
    assert after["prompt"] == ""
    assert after["duration"] == 7
    assert client.get(f"{root}/shots/{other}").json()["prompt"] == "保留"


@pytest.mark.parametrize("fast", [True, False])
def test_trimmed_scene_scan_keeps_absolute_cut_times_and_reuses_cached_detection(
    client, pid, tmp_path, monkeypatch, fast
):
    located = ffmpeg_tool.locate("ffmpeg")
    if not located.path:
        pytest.skip("FFmpeg unavailable")
    path = tmp_path / "hard-cuts.mp4"
    subprocess.run(
        [
            located.path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=128x72:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=128x72:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=128x72:r=10:d=2",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
            "-map",
            "[out]",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    root = f"/api/v1/projects/{pid}"
    asset = client.post(f"{root}/ingest/register", json={"path": str(path)}).json()
    body = {
        "asset_id": asset["id"],
        "method": "scene",
        "threshold": 0.1,
        "min_segment": 0.1,
        "range_in": 1,
        "range_out": 5,
        "fast_scan": fast,
    }
    original = asyncio.create_subprocess_exec
    scans = []

    async def spawn(*args, **kwargs):
        if "-vf" in args:
            scans.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    for minimum in (0.1, 0.2):
        response = client.post(f"{root}/ingest/plan", json={**body, "min_segment": minimum})
        assert response.status_code == 200, response.text
        assert response.json()["cuts"] == pytest.approx([2, 4], abs=0.11)
    assert len(scans) == 1
    assert "-ss" in scans[0] and "-t" in scans[0] and "-an" in scans[0]
    # 阈值变化须重新分析，不能复用另一种灵敏度的切点。
    response = client.post(f"{root}/ingest/plan", json={**body, "threshold": 0.2})
    assert response.status_code == 200
    assert len(scans) == 2


def test_read_only_video_analysis_does_not_block_shared_prompt_save(client, pid, monkeypatch):
    root = f"/api/v1/projects/{pid}"
    sid = a_shot(client, pid)
    scene = client.get(f"{root}/shots/{sid}").json()["scene_id"]
    started, release = threading.Event(), threading.Event()

    async def slow_analysis(*args, **kwargs):
        started.set()
        await asyncio.to_thread(release.wait, 10)
        return {"segments": []}

    monkeypatch.setattr(ingest, "plan", slow_analysis)
    with ThreadPoolExecutor(max_workers=2) as executor:
        scanning = executor.submit(client.post, f"{root}/ingest/plan", json={"asset_id": "unused"})
        assert started.wait(3)
        try:
            saving = executor.submit(
                client.put,
                f"{root}/scenes/{scene}/frame-prompts",
                json={"image_prompt": "保存不等待视频扫描"},
            )
            assert saving.result(timeout=3).status_code == 200
        finally:
            release.set()
        assert scanning.result(timeout=3).status_code == 200


def test_bulk_import_preserves_independent_prompt_mode(client, pid, tmp_path):
    root = f"/api/v1/projects/{pid}"
    path = make_real_video(tmp_path / "source.mp4", duration=4)
    asset = client.post(f"{root}/ingest/register", json={"path": str(path)}).json()
    result = client.post(
        f"{root}/ingest/run",
        json={
            "asset_id": asset["id"],
            "method": "fixed",
            "chunk_seconds": 2,
            "param_mode": "per_shot",
            "prompt": "预填到各镜头",
        },
    )
    assert result.status_code == 201, result.text
    assert all(s["prompt"] == "预填到各镜头" for s in result.json()["shots"])


def test_file_hash_streams_without_loading_the_whole_video(tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path

    from app.services.assets import sha1_of_file

    path = tmp_path / "large-video.mp4"
    data = b"video bytes" * 200_000
    path.write_bytes(data)

    def disallow_full_read(self):
        raise AssertionError("whole-file reads must not be used for video hashing")

    monkeypatch.setattr(Path, "read_bytes", disallow_full_read)
    assert sha1_of_file(path) == hashlib.sha1(data).hexdigest()
