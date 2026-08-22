"""分镜板上那张图：卡片只吃图片，抽帧是显式的一次写操作。

这个文件盯的是一个真实的 bug：卡片以前拿的是「当前版本的资产 id」，而当前版本
几乎总是一段 `.mp4`，前端把它塞进 `<img>` 就永远是坏图——「分镜里截取的首帧
加载失败」说的就是它。所以四件事：

  1. 只有视频版本时，`thumbnail_path` 必须是空的、`video_path` 才是那段片子，
     并且卡片自己举手 `poster_pending=true`；
  2. 同一镜头有图片版本时，那张图直接当封面，不需要抽帧；
  3. `GET /storyboard` **绝不起 FFmpeg**——补图是 `POST /storyboard/posters`
     这条显式动作，抽完卡片上就有真首帧了，再点一次是幂等复用；
  4. 某一段视频抽不出来只是它自己的事（逐条四要素理由，不打断其余）；
     FFmpeg 整个缺失才是全局失败，那种情况立刻报出来，不给人看 20 条一样的错。
  5. **版本轨是同一个 bug 的第二个现场**：`GET /shots/{id}/versions` 也得把
     `video_path` / `thumbnail_path` 分开给，抽出来的首帧同时就是版本封面。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import ffmpeg as ffmpeg_tool
from tests.conftest import error_of, upload_png

API = "/api/v1"


def upload_bytes(client: TestClient, pid: str, name: str, blob: bytes, kind: str) -> str:
    resp = client.post(
        f"{API}/projects/{pid}/assets/upload",
        data={"kind": kind},
        files={"file": (name, blob, "video/mp4")},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def fake_mp4(client: TestClient, pid: str, name: str = "clip.mp4") -> str:
    """后缀对、内容假：够 `kind_of_suffix` 认出「能播的那一段」，但 FFmpeg 抽不出帧。"""
    return upload_bytes(client, pid, name, b"FAKEMP4" + name.encode(), "generated_video")


def real_mp4(client: TestClient, pid: str, tmp_path: Path, name: str = "real.mp4") -> str:
    """用 lavfi 造一段真视频再上传——抽帧的正向用例只能靠真文件。"""
    located = ffmpeg_tool.locate("ffmpeg")
    if not located.path:
        pytest.skip("这台机器上没有 FFmpeg（跑一次 scripts/fetch_ffmpeg.py）")
    out = tmp_path / name
    subprocess.run(  # noqa: S603 - 路径来自 ffmpeg.locate()，不是用户输入
        [
            located.path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return upload_bytes(client, pid, name, out.read_bytes(), "generated_video")


def board(client: TestClient, pid: str) -> list[dict[str, Any]]:
    resp = client.get(f"{API}/projects/{pid}/storyboard")
    assert resp.status_code == 200, resp.text
    return [dict(lane) for lane in resp.json()]


def one_card(client: TestClient, pid: str) -> dict[str, Any]:
    lanes = board(client, pid)
    assert len(lanes) == 1 and len(lanes[0]["shots"]) == 1, "这些用例只造一场一镜"
    return dict(lanes[0]["shots"][0])


def make_shot(client: TestClient, pid: str) -> str:
    scene = client.post(f"{API}/projects/{pid}/scenes", json={"title": "雨夜追车"})
    assert scene.status_code == 201, scene.text
    shot = client.post(
        f"{API}/projects/{pid}/scenes/{scene.json()['id']}/shots", json={"title": "车灯划过水面"}
    )
    assert shot.status_code == 201, shot.text
    return str(shot.json()["id"])


def add_version(client: TestClient, pid: str, shot: str, **body: Any) -> dict[str, Any]:
    resp = client.post(f"{API}/projects/{pid}/shots/{shot}/versions", json=body)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def test_card_never_hands_a_video_to_an_img(client: TestClient, pid: str) -> None:
    shot = make_shot(client, pid)
    made = add_version(client, pid, shot, asset_id=fake_mp4(client, pid), duration=4.0)

    card = one_card(client, pid)
    assert card["thumbnail_path"] is None, "缩略图只认图片，mp4 绝不能进 <img>"
    assert card["thumbnail_asset_id"] is None
    assert str(card["video_path"]).endswith(".mp4")
    assert card["video_version_id"] == made["id"]
    assert card["poster_pending"] is True, "有片子没有图：卡片要自己举手，等人点「补首帧」"


def test_an_image_version_is_the_cover_without_any_ffmpeg(client: TestClient, pid: str) -> None:
    shot = make_shot(client, pid)
    add_version(client, pid, shot, asset_id=fake_mp4(client, pid), duration=4.0)
    add_version(
        client,
        pid,
        shot,
        asset_id=upload_png(client, pid, "generated_image", "draft.png"),
        kind="image",
    )

    card = one_card(client, pid)
    assert str(card["thumbnail_path"]).endswith(".png")
    assert str(card["video_path"]).endswith(".mp4"), "封面是图，能播的仍然是那段视频"
    assert card["poster_pending"] is False, "已经有图了就没什么要补的"


def test_no_pending_card_means_nothing_to_do(client: TestClient, pid: str) -> None:
    make_shot(client, pid)
    resp = client.post(f"{API}/projects/{pid}/storyboard/posters", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"requested": 0, "extracted": [], "failed": []}


def test_extract_posters_puts_a_real_first_frame_on_the_card(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    shot = make_shot(client, pid)
    add_version(client, pid, shot, asset_id=real_mp4(client, pid, tmp_path), duration=1.0)
    assert one_card(client, pid)["poster_pending"] is True

    done = client.post(f"{API}/projects/{pid}/storyboard/posters", json={"shot_ids": [shot]})
    assert done.status_code == 200, done.text
    out = done.json()
    assert out["requested"] == 1 and out["failed"] == []
    assert out["extracted"][0]["shot_id"] == shot
    assert out["extracted"][0]["reused"] is False

    card = one_card(client, pid)
    assert str(card["thumbnail_path"]).endswith(".png"), "抽完卡片上就该有真首帧"
    assert card["poster_pending"] is False
    assert str(card["video_path"]).endswith(".mp4"), "补图不会动那段视频"

    # 再点一次：没有可补的了（上一张已经认出来是这段视频的首帧）
    again = client.post(f"{API}/projects/{pid}/storyboard/posters", json={})
    assert again.json() == {"requested": 0, "extracted": [], "failed": []}


def test_one_broken_clip_does_not_take_the_others_down(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    good_shot = make_shot(client, pid)
    add_version(client, pid, good_shot, asset_id=real_mp4(client, pid, tmp_path), duration=1.0)
    lanes = board(client, pid)
    scene_id = lanes[0]["id"]
    bad = client.post(
        f"{API}/projects/{pid}/scenes/{scene_id}/shots", json={"title": "坏掉的那一段"}
    ).json()["id"]
    add_version(client, pid, bad, asset_id=fake_mp4(client, pid, "broken.mp4"), duration=4.0)

    out = client.post(f"{API}/projects/{pid}/storyboard/posters", json={})
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["requested"] == 2
    assert [row["shot_id"] for row in body["extracted"]] == [good_shot], "好的那条照样抽出来"
    assert len(body["failed"]) == 1
    failure = body["failed"][0]
    assert failure["shot_id"] == bad and failure["title"] == "坏掉的那一段"
    assert failure["error"]["code"] == "FFMPEG_ERROR"
    assert failure["error"]["suggestions"], "绝不静默失败：逐条都要给出下一步"


def test_without_ffmpeg_it_fails_once_not_per_card(
    client: TestClient, pid: str, no_ffmpeg: None
) -> None:
    shot = make_shot(client, pid)
    add_version(client, pid, shot, asset_id=fake_mp4(client, pid), duration=4.0)

    resp = client.post(f"{API}/projects/{pid}/storyboard/posters", json={})
    assert resp.status_code == 400, resp.text
    err = error_of(resp)
    assert err["code"] == "FFMPEG_MISSING", "FFmpeg 缺失是全局问题，不该变成 N 条一样的失败"


def versions(client: TestClient, pid: str, shot: str) -> list[dict[str, Any]]:
    resp = client.get(f"{API}/projects/{pid}/shots/{shot}/versions")
    assert resp.status_code == 200, resp.text
    return [dict(row) for row in resp.json()]


def test_version_track_splits_video_and_thumbnail_too(client: TestClient, pid: str) -> None:
    """版本轨和卡片同一条规矩：`.mp4` 只能进 `video_path`。

    这里是同一个 bug 的第二个现场——版本轨以前拿 `asset_id` 直接塞进 `<img>`，
    而版本的资产几乎总是一段视频，于是每一格都是坏图标。
    """
    shot = make_shot(client, pid)
    add_version(client, pid, shot, asset_id=fake_mp4(client, pid), duration=4.0)
    add_version(
        client,
        pid,
        shot,
        asset_id=upload_png(client, pid, "generated_image", "draft.png"),
        kind="image",
    )

    rows = versions(client, pid, shot)
    assert [r["version_no"] for r in rows] == [2, 1], "新的在前"
    image, video = rows
    assert str(image["thumbnail_path"]).endswith(".png") and image["video_path"] is None
    assert str(video["video_path"]).endswith(".mp4")
    assert video["thumbnail_path"] is None, "还没抽帧：只给能播的那一段，别硬凑一张图"


def test_extracted_poster_shows_up_on_the_version_too(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    shot = make_shot(client, pid)
    add_version(client, pid, shot, asset_id=real_mp4(client, pid, tmp_path), duration=1.0)
    assert versions(client, pid, shot)[0]["thumbnail_path"] is None

    done = client.post(f"{API}/projects/{pid}/storyboard/posters", json={"shot_ids": [shot]})
    assert done.status_code == 200, done.text

    row = versions(client, pid, shot)[0]
    assert str(row["thumbnail_path"]).endswith("_start.png"), "抽出来的首帧同时就是版本封面"
    assert str(row["video_path"]).endswith(".mp4"), "补图不会动那段视频"
