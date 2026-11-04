"""实际成片衔接的来源冻结、审阅和上游改选保护。"""

import json

import pytest

from app.ai.director.tools import run_read, to_op
from app.services.frames import frames as extractor
from tests.test_frame_authoring import http_video, setup
from tests.test_m4_generation_timeline_overview import add_version
from tests.test_shot_frame_slots import a_shot


def source_video(client, pid):
    sid = a_shot(client, pid)
    version = add_version(client, pid, sid, name="source.png", duration=5)
    client.post(f"/api/v1/projects/{pid}/versions/{version['id']}/current")
    return sid, version


def test_continuity_candidate_review_freeze_and_source_change(client, pid, monkeypatch):
    root, sid, cid, aid = setup(client, pid)
    source, version = source_video(client, pid)
    calls = []

    async def extract(project, asset_id, at):
        calls.append((asset_id, at))
        return {"id": aid}

    monkeypatch.setattr(extractor, "extract", extract)
    body = {"source_version_id": version["id"], "use": "opening"}
    result = client.post(f"{root}/frame-cards/{cid}/continuity", json=body)
    assert result.status_code == 200, result.text
    candidate = result.json()["candidate"]
    assert calls == [(version["asset_id"], "end")]
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert ws["video"] == {"first_card_id": cid}
    assert any(
        c["continuity"] and not c["continuity"]["stale"] for c in ws["cards"][0]["candidates"]
    )
    client.post(f"{root}/frame-cards/{cid}/adopt", json={"candidate_id": candidate["id"]})
    http_video(client, root)
    generated = client.post(
        f"{root}/shots/{sid}/frame-video/generate",
        json={"prompt": "Walk forward.", "first_card_id": cid},
    )
    assert generated.status_code == 201, generated.text
    frozen = json.loads(generated.json()["params_json"])["frame_inputs"]
    assert frozen["cards"][0]["provenance"][0]["source_version_id"] == version["id"]
    newer = add_version(client, pid, source, name="new.png", duration=5)
    client.post(f"{root}/versions/{newer['id']}/current")
    assert client.post(f"{root}/frame-cards/{cid}/continuity", json=body).status_code == 422
    assert (
        client.post(
            f"{root}/shots/{sid}/frame-video/plan", json={"prompt": "Walk", "first_card_id": cid}
        ).status_code
        == 422
    )
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert any(c["continuity"] and c["continuity"]["stale"] for c in ws["cards"][0]["candidates"])


@pytest.mark.asyncio
async def test_ai_continuity_proposal_has_no_extraction_side_effect(client, pid, monkeypatch):
    root, sid, cid, aid = setup(client, pid)
    source, version = source_video(client, pid)
    calls = []

    async def extract(project, asset_id, at):
        calls.append(at)
        return {"id": aid}

    monkeypatch.setattr(extractor, "extract", extract)
    op = await to_op(
        pid,
        "extract_frame_continuity",
        {"card_id": cid, "source_version_id": version["id"], "use": "reference", "at": 3.5},
        1,
    )
    assert not calls
    assert op["warnings"]
    read = await run_read(pid, "get_frame_workspace", {"shot_id": source})
    assert read["current_version_id"] == version["id"]
    response = client.post(f"{root}/director/apply", json={"ops": [op]})
    assert response.status_code == 201, response.text
    assert response.json()["count"] == 1, response.text
    assert calls == [3.5]
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert ws["cards"][0]["refs"][0] == aid
    assert (
        client.post(
            f"{root}/frame-cards/{cid}/continuity",
            json={"source_version_id": version["id"], "at": 5},
        ).status_code
        == 422
    )


@pytest.mark.asyncio
async def test_real_video_frame_is_available_as_reviewable_candidate(client, pid, tmp_path):
    from app.services.assets import assets
    from tests.test_frames import ffmpeg_or_skip, make_clip

    root, sid, cid, original = setup(client, pid)
    source = a_shot(client, pid)
    clip = await make_clip(ffmpeg_or_skip(), tmp_path / "continuity.mp4")
    media = await assets.register_path(pid, "generated_video", str(clip))
    version = client.post(
        f"{root}/shots/{source}/versions",
        json={"asset_id": media["id"], "kind": "video", "duration": 1},
    ).json()
    client.post(f"{root}/versions/{version['id']}/current")
    result = client.post(
        f"{root}/frame-cards/{cid}/continuity",
        json={"source_version_id": version["id"], "use": "opening"},
    )
    assert result.status_code == 200, result.text
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert ws["cards"][0]["adopted_asset_id"] == original
    pictures = client.get(f"{root}/assets", params={"kind": "frame"}).json()
    assert any(p["id"] == result.json()["asset_id"] and not p["missing"] for p in pictures)


def test_public_script_page_receives_explicit_frame_mode(client, pid, monkeypatch):
    from app.ai import prompts
    from tests.test_director_agent import use_fake_llm

    use_fake_llm(monkeypatch, [{"content": "已按制帧方式准备。", "tool_calls": []}])
    response = client.post(
        f"/api/v1/projects/{pid}/director/chat",
        json={"message": "检查当前流程", "scope": "script", "production_mode": "frames"},
    )
    assert response.status_code == 201, response.text
    assert "当前制作方式明确为制帧" in prompts.director("script", "frames")
    assert "当前制作方式明确为直出" in prompts.director("script", "direct")


@pytest.mark.asyncio
async def test_ai_video_tools_keep_frame_inputs_separate_and_protect_new_edits(client, pid):
    root, sid, cid, aid = setup(client, pid)
    http_video(client, root)
    before = client.get(f"{root}/shots/{sid}").json()
    op = await to_op(
        pid,
        "update_frame_video",
        {"shot_id": sid, "prompt": "Walk forward from the opening.", "first_card_id": cid},
        1,
    )
    result = client.post(f"{root}/director/apply", json={"ops": [op]})
    assert result.json()["count"] == 1, result.text
    generate = await to_op(pid, "generate_frame_video", {"shot_id": sid}, 2)
    result = client.post(f"{root}/director/apply", json={"ops": [generate]})
    assert result.json()["count"] == 1, result.text
    params = json.loads(result.json()["applied"][0]["params_json"])
    assert params["creation_pipeline"] == "frames"
    assert params["frame_inputs"]["first_frame_asset_id"] == aid
    after = client.get(f"{root}/shots/{sid}").json()
    assert after["prompt"] == before["prompt"]
    stale = await to_op(pid, "update_frame_video", {"shot_id": sid, "prompt": "Old proposal"}, 3)
    client.put(
        f"{root}/shots/{sid}/frame-video", json={"prompt": "New manual edit", "first_card_id": cid}
    )
    rejected = client.post(f"{root}/director/apply", json={"ops": [stale]}).json()
    assert rejected["count"] == 0 and rejected["failed"]
    assert client.get(f"{root}/shots/{sid}/frames").json()["video"]["prompt"] == "New manual edit"


def test_adopting_continuity_binds_opening_only_after_review(client, pid, monkeypatch):
    root, sid, cid, aid = setup(client, pid)
    source, version = source_video(client, pid)
    other = client.post(f"{root}/shots/{sid}/frames", json={"title": "实际衔接开场"}).json()["id"]
    client.put(
        f"{root}/shots/{sid}/frame-video",
        json={"first_card_id": cid, "prompt": "Preserve this action.", "duration": 7},
    )

    async def extract(project, asset_id, at):
        return {"id": aid}

    monkeypatch.setattr(extractor, "extract", extract)
    result = client.post(
        f"{root}/frame-cards/{other}/continuity",
        json={"source_version_id": version["id"], "use": "opening"},
    )
    assert result.status_code == 200, result.text
    assert client.get(f"{root}/shots/{sid}/frames").json()["video"]["first_card_id"] == cid
    adopted = client.post(
        f"{root}/frame-cards/{other}/adopt", json={"candidate_id": result.json()["candidate"]["id"]}
    )
    assert adopted.status_code == 200, adopted.text
    video = client.get(f"{root}/shots/{sid}/frames").json()["video"]
    assert video["first_card_id"] == other
    assert video["prompt"] == "Preserve this action." and video["duration"] == 7
