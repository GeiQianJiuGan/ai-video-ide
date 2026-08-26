"""镜头拆分（Split Shot）功能测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import ffmpeg as ffmpeg_tool

API = "/api/v1"


def make_real_video(path: Path, duration: int = 4) -> Path:
    located = ffmpeg_tool.locate("ffmpeg")
    if not located.path:
        pytest.skip("测试环境缺少 FFmpeg")
    subprocess.run(
        [
            located.path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size=64x64:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return path


def test_shot_split_with_version_ranges(client: TestClient, pid: str, tmp_path: Path) -> None:
    # 1. 导入一段视频切段
    video_file = make_real_video(tmp_path / "split_src.mp4", duration=6)
    reg_resp = client.post(
        f"{API}/projects/{pid}/ingest/register",
        json={"path": str(video_file), "copy_into_project": True},
    )
    asset_id = reg_resp.json()["id"]

    run_resp = client.post(
        f"{API}/projects/{pid}/ingest/run",
        json={
            "asset_id": asset_id,
            "method": "manual",
            "cuts": [6.0],  # 整段作为一个镜头 (0~6s)
        },
    )
    assert run_resp.status_code == 201
    shot_id = run_resp.json()["shots"][0]["id"]

    # 验证初始镜头时长为 6.0
    shot_before = client.get(f"{API}/projects/{pid}/shots/{shot_id}").json()
    assert shot_before["duration"] == 6.0

    # 2. 在 2.5 秒处拆分镜头
    split_resp = client.post(
        f"{API}/projects/{pid}/shots/{shot_id}/split",
        json={"at_seconds": 2.5},
    )
    assert split_resp.status_code == 200, split_resp.text
    body = split_resp.json()
    assert body["first_duration"] == 2.5
    assert body["second_duration"] == 3.5
    new_shot_id = body["new_shot_id"]

    # 3. 验证两个镜头的信息与区间
    shot1 = client.get(f"{API}/projects/{pid}/shots/{shot_id}").json()
    shot2 = client.get(f"{API}/projects/{pid}/shots/{new_shot_id}").json()

    assert shot1["duration"] == 2.5
    assert shot2["duration"] == 3.5
    assert shot1["index_no"] < shot2["index_no"]

    # 拆分**只增不改**（硬约束 3）：原来那一版还在，两半各是一个新版本。
    origin_id = run_resp.json()["shots"][0]["version_id"]
    v1 = next(v for v in shot1["versions"] if v["id"] == body["first_version_id"])
    v2 = next(v for v in shot2["versions"] if v["id"] == body["second_version_id"])
    origin = next(v for v in shot1["versions"] if v["id"] == origin_id)
    assert (origin["in_point"], origin["out_point"]) == (0.0, 6.0)
    assert v1["in_point"] == 0.0
    assert v1["out_point"] == 2.5
    assert v2["in_point"] == 2.5
    assert v2["out_point"] == 6.0
    assert v1["parent_version_id"] == origin_id
    assert v2["parent_version_id"] == origin_id
    # 两个镜头各自采用的都是拆出来的那一半
    assert shot1["current_version_id"] == v1["id"]
    assert shot2["current_version_id"] == v2["id"]


def test_shot_split_invalid_offset(client: TestClient, pid: str) -> None:
    # 创建一个场景与镜头
    scene = client.post(f"{API}/projects/{pid}/scenes", json={"title": "测试场景"}).json()
    shot = client.post(
        f"{API}/projects/{pid}/scenes/{scene['id']}/shots",
        json={"title": "镜头1", "duration": 4.0},
    ).json()

    # 负数 / 超限拆分点应抛出 422 校验错误
    resp_neg = client.post(
        f"{API}/projects/{pid}/shots/{shot['id']}/split", json={"at_seconds": 0.05}
    )
    assert resp_neg.status_code == 422

    resp_over = client.post(
        f"{API}/projects/{pid}/shots/{shot['id']}/split", json={"at_seconds": 4.0}
    )
    assert resp_over.status_code == 422
