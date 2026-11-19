"""长视频制帧：真实区间抽帧、动态继承及失败时的完整性。"""

import pytest

from app.core.errors import AppError, ErrorCode
from app.persistence.models_gen import Job
from app.services.base import db_of, fetch, load_json
from app.services.frame_authoring import frames
from app.services.frames import frames as extractor
from tests.test_frame_authoring import http_video, setup
from tests.test_ingest import make_real_video
from tests.test_providers import with_ref_slots, write_preset


@pytest.mark.parametrize("count", [1, 3])
def test_long_video_extracts_each_trimmed_segment_into_image_references(
    client, pid, tmp_path, count
):
    root = f"/api/v1/projects/{pid}"
    source = make_real_video(tmp_path / "source.mp4", duration=6)
    registered = client.post(f"{root}/ingest/register", json={"path": str(source)})
    assert registered.status_code == 201, registered.text
    response = client.post(
        f"{root}/ingest/run",
        json={
            "asset_id": registered.json()["id"],
            "production_mode": "frames",
            "method": "fixed",
            "chunk_seconds": 2,
            "min_segment": 0.5,
            "range_in": 1,
            "range_out": 5,
            "frames_per_shot": count,
            "image_prompt": "保持构图，转换为水墨画。",
            "video_prompt": "Slow camera movement.",
        },
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["created"] == 2
    for shot in result["shots"]:
        workspace = client.get(f"{root}/shots/{shot['id']}/frames").json()
        assert workspace["video"]["shared_prompt"] == "Slow camera movement."
        assert len(workspace["cards"]) == count
        assert [c["role"] for c in workspace["cards"]] == (
            ["first"] if count == 1 else ["first", "key", "last"]
        )
        for card in workspace["cards"]:
            assert card["prompt"] == ""
            assert card["effective_prompt"] == "保持构图，转换为水墨画。"
            assert card["adopted_asset_id"] is None
            assert not card["candidates"]
            assert len(card["refs"]) == 1
            asset = client.get(f"{root}/assets/{card['refs'][0]}").json()
            meta = load_json(asset["meta_json"], {})
            assert shot["in_point"] <= meta["at"] < shot["out_point"]
            assert meta["from_asset_id"] == registered.json()["id"]
        # 未制作并采用图片之前，不把源片参考帧直接当成视频输入。
        assert "first_card_id" not in workspace["video"]


@pytest.mark.asyncio
async def test_shared_prompts_override_clear_and_freeze_at_submission(client, pid):
    root, sid, cid, aid = setup(client, pid)
    shot = client.get(f"{root}/shots/{sid}").json()
    endpoint = f"{root}/scenes/{shot['scene_id']}/frame-prompts"
    body = {"image_prompt": "共用水墨画风", "video_prompt": "Camera moves slowly."}
    assert client.put(endpoint, json=body).status_code == 200
    image_plan = client.post(
        f"{root}/images/plan",
        json={
            "target_kind": "frame_card",
            "target_id": cid,
        },
    )
    assert image_plan.status_code == 200, image_plan.text
    assert "共用水墨画风" in image_plan.json()["prompt"]
    write_preset("shared-frame-image", with_ref_slots(1))
    client.patch(
        "/api/v1/settings",
        json={
            "image.provider": "comfy_preset",
            "image.preset": "shared-frame-image",
        },
    )
    client.patch(f"{root}/frame-cards/{cid}", json={"refs": [aid]})
    image_job_resp = client.post(f"{root}/frame-cards/{cid}/generate")
    assert image_job_resp.status_code == 201, image_job_resp.text
    image_job = await fetch(db_of(pid), Job, image_job_resp.json()["id"], "图片任务")
    assert load_json(image_job.params_json, {})["image_prompt"] == body["image_prompt"]
    assert load_json(image_job.params_json, {})["ref_asset_ids"] == [aid]
    http_video(client, root)
    draft = {"prompt": "", "first_card_id": cid, "duration": 5}
    saved = client.put(f"{root}/shots/{sid}/frame-video", json=draft)
    assert saved.status_code == 200
    job_resp = client.post(f"{root}/shots/{sid}/frame-video/generate", json=draft)
    assert job_resp.status_code == 201, job_resp.text
    job = await fetch(db_of(pid), Job, job_resp.json()["id"], "任务")
    assert load_json(job.params_json, {})["prompt"] == body["video_prompt"]

    # 服务调用也不能把解析出的共用值保存为独立覆盖。
    await frames.save_video(pid, sid, {**draft, "shared_prompt": body["video_prompt"]})
    assert client.put(endpoint, json={"image_prompt": "", "video_prompt": ""}).status_code == 200
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert "shared_prompt" not in ws["video"]
    assert ws["cards"][0]["effective_prompt"] == ""
    missing = client.post(f"{root}/shots/{sid}/frame-video/generate", json=draft)
    assert missing.status_code == 422
    image_job = await fetch(db_of(pid), Job, image_job_resp.json()["id"], "图片任务")
    assert load_json(image_job.params_json, {})["image_prompt"] == body["image_prompt"]

    client.patch(f"{root}/frame-cards/{cid}", json={"prompt": "独立图片提示词"})
    client.put(f"{root}/shots/{sid}/frame-video", json={**draft, "prompt": "Independent motion."})
    updated = {"image_prompt": "新的共用画风", "video_prompt": "New shared motion."}
    assert client.put(endpoint, json=updated).status_code == 200
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert ws["cards"][0]["effective_prompt"] == "独立图片提示词"
    assert ws["video"]["prompt"] == "Independent motion."
    assert client.put(endpoint, json={**updated, "clear_overrides": True}).status_code == 200
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert ws["cards"][0]["prompt"] == ""
    assert ws["cards"][0]["effective_prompt"] == "新的共用画风"
    assert ws["cards"][0]["adopted_asset_id"] == aid
    assert ws["video"]["prompt"] == ""
    assert ws["video"]["first_card_id"] == cid
    assert ws["video"]["shared_prompt"] == updated["video_prompt"]
    job = await fetch(db_of(pid), Job, job_resp.json()["id"], "任务")
    assert load_json(job.params_json, {})["prompt"] == body["video_prompt"]


def test_failed_extraction_does_not_create_partial_scenes(client, pid, tmp_path, monkeypatch):
    root = f"/api/v1/projects/{pid}"
    source = make_real_video(tmp_path / "source.mp4", duration=4)
    registered = client.post(f"{root}/ingest/register", json={"path": str(source)}).json()
    before = client.get(f"{root}/scenes").json()

    async def fail(*args, **kwargs):
        raise AppError(ErrorCode.FFMPEG_ERROR, "抽帧失败", "测试失败", [])

    monkeypatch.setattr(extractor, "extract", fail)
    response = client.post(
        f"{root}/ingest/run",
        json={
            "asset_id": registered["id"],
            "production_mode": "frames",
            "method": "fixed",
            "chunk_seconds": 2,
        },
    )
    assert response.status_code == 400, response.text
    assert client.get(f"{root}/scenes").json() == before


@pytest.mark.parametrize("count", [0, 9])
def test_invalid_frame_count_is_rejected(client, pid, count):
    response = client.post(
        f"/api/v1/projects/{pid}/ingest/run",
        json={
            "asset_id": "missing",
            "production_mode": "frames",
            "frames_per_shot": count,
        },
    )
    assert response.status_code == 422
