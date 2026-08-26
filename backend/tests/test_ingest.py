"""长视频切段与导入测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

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


def test_ingest_max_segment_and_asset_id_registration(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
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


def test_ingest_trims_head_and_tail(client: TestClient, pid: str, tmp_path: Path) -> None:
    """片头片尾：切段只发生在保留区间里，区间外的时间不进任何镜头。"""
    video_file = make_real_video(tmp_path / "with_bumper.mp4", duration=10)
    asset = client.post(
        f"{API}/projects/{pid}/ingest/register",
        json={"path": str(video_file), "copy_into_project": True},
    ).json()

    plan = client.post(
        f"{API}/projects/{pid}/ingest/plan",
        json={
            "asset_id": asset["id"],
            "method": "fixed",
            "chunk_seconds": 2.0,
            "min_segment": 1.0,
            "range_in": 2.0,
            "range_out": 8.0,
        },
    )
    assert plan.status_code == 200, plan.text
    bill = plan.json()
    assert bill["range_in"] == 2.0
    assert bill["range_out"] == pytest.approx(8.0, abs=0.2)
    assert bill["trimmed_head"] == 2.0
    assert bill["trimmed_tail"] > 0, "片尾被挡掉了多少秒必须写在账单上"
    #: 第一段从片头之后开始，最后一段在片尾之前结束——一帧都不许溢出区间。
    assert bill["segments"][0]["in_point"] == pytest.approx(2.0, abs=0.01)
    assert bill["segments"][-1]["out_point"] <= bill["range_out"] + 0.01
    for seg in bill["segments"]:
        assert seg["in_point"] >= 2.0 - 0.01
    assert any("片头" in w for w in bill["warnings"]), bill["warnings"]


def test_ingest_drops_cuts_inside_the_trimmed_head(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    """手动切点落在片头 / 片尾里时直接扔掉：那一截不属于任何镜头。"""
    video_file = make_real_video(tmp_path / "manual_cuts.mp4", duration=10)
    asset = client.post(
        f"{API}/projects/{pid}/ingest/register",
        json={"path": str(video_file), "copy_into_project": True},
    ).json()

    bill = client.post(
        f"{API}/projects/{pid}/ingest/plan",
        json={
            "asset_id": asset["id"],
            "cuts": [1.0, 4.0, 6.0, 9.5],
            "min_segment": 1.0,
            "range_in": 3.0,
            "range_out": 8.0,
        },
    ).json()
    assert bill["method"] == "manual", "给了切点就不许再跑自动检测"
    starts = [round(seg["in_point"], 2) for seg in bill["segments"]]
    assert starts == [3.0, 4.0, 6.0], starts
    assert bill["segments"][-1]["out_point"] == pytest.approx(8.0, abs=0.2)


def test_ingest_rejects_an_empty_keep_range(client: TestClient, pid: str, tmp_path: Path) -> None:
    """片头片尾把整段吃光时要当场说清楚，而不是切出一堆零长度的段。"""
    from tests.conftest import error_of

    video_file = make_real_video(tmp_path / "over_trimmed.mp4", duration=6)
    asset = client.post(
        f"{API}/projects/{pid}/ingest/register",
        json={"path": str(video_file), "copy_into_project": True},
    ).json()

    resp = client.post(
        f"{API}/projects/{pid}/ingest/plan",
        json={"asset_id": asset["id"], "range_in": 3.0, "range_out": 3.2},
    )
    assert resp.status_code == 422, resp.text
    err = error_of(resp)
    assert err["title"] == "去掉片头片尾之后什么都不剩"
    assert err["suggestions"]

    too_long = client.post(
        f"{API}/projects/{pid}/ingest/plan",
        json={"asset_id": asset["id"], "range_in": 99.0},
    )
    assert too_long.status_code == 422, too_long.text
    assert error_of(too_long)["title"] == "片头切掉的比整段视频还长"


def test_ingest_run_keeps_the_range_on_every_version(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    """落库时区间要一路带到版本上（零文件复制：源文件一帧都没动）。"""
    video_file = make_real_video(tmp_path / "run_range.mp4", duration=10)
    asset = client.post(
        f"{API}/projects/{pid}/ingest/register",
        json={"path": str(video_file), "copy_into_project": True},
    ).json()

    run = client.post(
        f"{API}/projects/{pid}/ingest/run",
        json={
            "asset_id": asset["id"],
            "title": "去掉片头片尾的一幕",
            "method": "fixed",
            "chunk_seconds": 2.0,
            "min_segment": 1.0,
            "range_in": 2.5,
            "range_out": 8.5,
        },
    )
    assert run.status_code == 201, run.text
    result = run.json()
    assert result["shots"], "至少要切出一段"
    assert result["shots"][0]["in_point"] == pytest.approx(2.5, abs=0.01)
    assert result["shots"][-1]["out_point"] <= 8.6
    #: 「为什么第一段不是从 0 秒开始」只有这一句解释，记在幕的备注里。
    assert "片头" in (result["scene"].get("notes") or "")

    first = result["shots"][0]
    version = client.get(f"{API}/projects/{pid}/shots/{first['id']}/versions").json()[0]
    assert version["asset_id"] == asset["id"], "零文件复制：还是同一个源文件"
    assert version["in_point"] == pytest.approx(2.5, abs=0.01)
