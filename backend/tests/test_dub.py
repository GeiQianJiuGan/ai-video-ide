"""音频重构与配音（Dub）接口测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import ffmpeg as ffmpeg_tool

API = "/api/v1"


def make_real_audio(path: Path, duration: int = 3) -> Path:
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
            f"sine=frequency=440:duration={duration}",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return path


def test_dub_plan_and_dialogue_fallback(client: TestClient, pid: str) -> None:
    # 1. 创建一幕并设置幕级台词
    scene = client.post(
        f"{API}/projects/{pid}/scenes",
        json={"title": "雨夜场景", "dialogue": "整幕的通用旁白：天开始下雨了。"},
    ).json()

    # 镜头 1 继承幕级台词
    shot1 = client.post(
        f"{API}/projects/{pid}/scenes/{scene['id']}/shots",
        json={"title": "远景", "duration": 4.0},
    ).json()

    # 镜头 2 自带台词
    shot2 = client.post(
        f"{API}/projects/{pid}/scenes/{scene['id']}/shots",
        json={"title": "特写", "duration": 3.0, "dialogue": "主角说：我们必须离开这里！"},
    ).json()

    # 2. 出配音账单 (Dub Plan)
    plan_resp = client.post(
        f"{API}/projects/{pid}/dub/plan",
        json={"scene_id": scene["id"]},
    )
    assert plan_resp.status_code == 200, plan_resp.text
    bill = plan_resp.json()
    assert bill["total"] == 2
    item1 = next(item for item in bill["items"] if item["shot_id"] == shot1["id"])
    item2 = next(item for item in bill["items"] if item["shot_id"] == shot2["id"])

    assert item1["text"] == "整幕的通用旁白：天开始下雨了。"
    assert item2["text"] == "主角说：我们必须离开这里！"


def test_import_audio_version_and_mute(client: TestClient, pid: str, tmp_path: Path) -> None:
    audio_file = make_real_audio(tmp_path / "voiceover.m4a", duration=3)

    scene = client.post(f"{API}/projects/{pid}/scenes", json={"title": "对话幕"}).json()
    shot = client.post(
        f"{API}/projects/{pid}/scenes/{scene['id']}/shots",
        json={"title": "镜头A", "duration": 4.0},
    ).json()

    # 1. 导入外部音频作为该镜头的音频版本
    imp_resp = client.post(
        f"{API}/projects/{pid}/shots/{shot['id']}/audio/import",
        json={"path": str(audio_file), "adopt": True},
    )
    assert imp_resp.status_code == 201, imp_resp.text
    imported = imp_resp.json()["version"]
    assert imported["kind"] == "audio"
    assert imported["source"] == "imported"

    # 2. 检查镜头的音频版本列表
    ver_resp = client.get(f"{API}/projects/{pid}/shots/{shot['id']}/audio-versions")
    assert ver_resp.status_code == 200
    ver_data = ver_resp.json()
    assert ver_data["current_audio_version_id"] == imported["id"]
    assert len(ver_data["items"]) == 1
    assert ver_data["items"][0]["audio_path"] is not None
    assert ver_data["items"][0]["is_current"] is True

    # 3. 检查分镜板卡片是否显示 has_audio_version
    board_resp = client.get(f"{API}/projects/{pid}/storyboard")
    card = board_resp.json()[0]["shots"][0]
    assert card["current_audio_version_id"] == imported["id"]
    assert card["has_audio_version"] is True

    # 4. 取消采用音频 (Mute)
    mute_resp = client.delete(f"{API}/projects/{pid}/shots/{shot['id']}/audio-current")
    assert mute_resp.status_code == 200
    assert mute_resp.json()["current_audio_version_id"] is None

    # 再次检查分镜板卡片
    board_resp2 = client.get(f"{API}/projects/{pid}/storyboard")
    card2 = board_resp2.json()[0]["shots"][0]
    assert card2["current_audio_version_id"] is None
    assert card2["has_audio_version"] is False
