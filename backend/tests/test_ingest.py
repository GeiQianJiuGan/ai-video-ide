"""长视频切段与导入测试。"""

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


def test_ingest_methods_endpoint(client: TestClient) -> None:
    resp = client.get(f"{API}/ingest/methods")
    assert resp.status_code == 200
    methods = resp.json()
    assert any(m["method"] == "auto" for m in methods)
    assert any(m["method"] == "scene" for m in methods)
    assert any(m["method"] == "silence" for m in methods)
    assert any(m["method"] == "fixed" for m in methods)


def test_ingest_register_plan_and_run(client: TestClient, pid: str, tmp_path: Path) -> None:
    video_file = make_real_video(tmp_path / "long_video.mp4", duration=6)

    # 1. 注册视频 (Register)
    reg_resp = client.post(
        f"{API}/projects/{pid}/ingest/register",
        json={"path": str(video_file), "copy_into_project": True},
    )
    assert reg_resp.status_code == 201, reg_resp.text
    asset = reg_resp.json()
    assert asset["id"]
    assert asset["duration"] is not None

    # 2. 生成账单 (Plan)
    plan_resp = client.post(
        f"{API}/projects/{pid}/ingest/plan",
        json={
            "asset_id": asset["id"],
            "method": "fixed",
            "chunk_seconds": 2.0,
            "min_segment": 1.0,
        },
    )
    assert plan_resp.status_code == 200, plan_resp.text
    bill = plan_resp.json()
    assert bill["total"] >= 2
    assert len(bill["segments"]) >= 2
    assert bill["segments"][0]["in_point"] == 0.0

    # 3. 运行导入 (Run)
    run_resp = client.post(
        f"{API}/projects/{pid}/ingest/run",
        json={
            "asset_id": asset["id"],
            "title": "成片切段第一幕",
            "method": "fixed",
            "chunk_seconds": 2.0,
            "min_segment": 1.0,
            "param_mode": "shared",
        },
    )
    assert run_resp.status_code == 201, run_resp.text
    result = run_resp.json()
    scene = result["scene"]
    assert scene["kind"] == "ingested"
    assert scene["title"] == "成片切段第一幕"
    assert len(result["shots"]) >= 2

    # 4. 验证分镜板中卡片
    board_resp = client.get(f"{API}/projects/{pid}/storyboard")
    assert board_resp.status_code == 200
    lanes = board_resp.json()
    ingested_lane = next(lane for lane in lanes if lane["id"] == scene["id"])
    first_shot = ingested_lane["shots"][0]

    # 5. 一键删除长视频切出的整幕与镜头
    del_resp = client.delete(f"{API}/projects/{pid}/scenes/{scene['id']}")
    assert del_resp.status_code == 204

    # 验证整幕及其所有镜头已从分镜板清理
    board_after = client.get(f"{API}/projects/{pid}/storyboard").json()
    assert all(lane["id"] != scene["id"] for lane in board_after)
    # 确认镜头已不存在
    assert client.get(f"{API}/projects/{pid}/shots/{first_shot['id']}").status_code == 404


def test_ingest_max_segment_and_asset_id_registration(client: TestClient, pid: str, tmp_path: Path) -> None:
    video_file = make_real_video(tmp_path / "long_10s.mp4", duration=10)

    # 1. 注册视频
    reg = client.post(
        f"{API}/projects/{pid}/ingest/register",
        json={"path": str(video_file), "copy_into_project": True},
    ).json()

    # 2. 用 asset id 再次 register 能够直接复用
    reg_by_id = client.post(
        f"{API}/projects/{pid}/ingest/register",
        json={"path": reg["id"], "copy_into_project": True},
    ).json()
    assert reg_by_id["id"] == reg["id"]

    # 3. 设定 max_segment = 3.0，测试 10s 视频被切为多段不超过 3s 的切片
    plan = client.post(
        f"{API}/projects/{pid}/ingest/plan",
        json={
            "asset_id": reg["id"],
            "method": "fixed",
            "chunk_seconds": 10.0,
            "max_segment": 3.0,
        },
    ).json()

    assert plan["total"] >= 4
    for seg in plan["segments"]:
        assert seg["duration"] <= 3.01
