"""Frame authoring must not alter legacy shot inputs or adopted video."""

import json
import sqlite3

import pytest

from app.persistence.models_gen import Job
from app.persistence.models_story import Shot
from app.services.base import db_of, fetch, load_json
from app.services.generation import generation
from app.services.images import images
from tests.conftest import PNG_1PX, upload_png
from tests.test_shot_frame_slots import a_shot


def setup(client, pid):
    sid = a_shot(client, pid)
    root = f"/api/v1/projects/{pid}"
    assert client.post(f"{root}/queue/pause").status_code == 200
    aid = upload_png(client, pid, "upload", "画面.png")
    c = client.post(f"{root}/shots/{sid}/frames", json={"title": "开场"})
    assert c.status_code == 201, c.text
    cid = c.json()["id"]
    v = client.post(f"{root}/frame-cards/{cid}/candidates", json={"asset_id": aid})
    assert v.status_code == 201, v.text
    assert (
        client.post(
            f"{root}/frame-cards/{cid}/adopt", json={"candidate_id": v.json()["id"]}
        ).status_code
        == 200
    )
    return root, sid, cid, aid


def http_video(client, root):
    assert (
        client.patch(
            "/api/v1/settings",
            json={"video.provider": "http_api", "video.base_url": "http://127.0.0.1:9"},
        ).status_code
        == 200
    )
    assert (
        client.put(f"{root}/workflow-bindings", json={"generation_mode": "http_api"}).status_code
        == 200
    )


def test_drafts_candidates_and_legacy_inputs_are_independent(client, pid):
    root, sid, cid, aid = setup(client, pid)
    before = client.get(f"{root}/shots/{sid}").json()
    assert (
        client.patch(
            f"{root}/frame-cards/{cid}",
            json={
                "title": "门口",
                "prompt": "少女站在门口",
                "refs": [aid],
                "story": "少女在门边发现桌上的信封，尚未走近桌边。",
                "role": "first",
            },
        ).status_code
        == 200
    )
    draft = {
        "prompt": "The girl turns around.",
        "duration": 5,
        "first_card_id": cid,
        "last_card_id": "",
    }
    assert client.put(f"{root}/shots/{sid}/frame-video", json=draft).status_code == 200
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert ws["video"] == {**draft, "preset": ""}
    assert ws["cards"][0]["adopted_asset_id"] == aid
    assert ws["cards"][0]["refs"] == [aid]
    assert ws["cards"][0]["story"] == "少女在门边发现桌上的信封，尚未走近桌边。"
    assert ws["cards"][0]["role"] == "first"
    after = client.get(f"{root}/shots/{sid}").json()
    for key in (
        "prompt",
        "intent_json",
        "first_frame_asset_id",
        "last_frame_asset_id",
        "prev_shot_id",
        "current_version_id",
    ):
        assert before.get(key) == after.get(key)
    other = a_shot(client, pid)
    assert client.get(f"{root}/shots/{other}/frames").json() == {"cards": [], "video": {}}


def test_schema_26_frame_cards_upgrade_with_candidates_and_video_preserved(
    client, pid, project_dir
):
    from app.persistence import migrate

    root, sid, cid, aid = setup(client, pid)
    draft = {"prompt": "The girl turns around.", "first_card_id": cid, "duration": 5}
    expected_video = client.put(f"{root}/shots/{sid}/frame-video", json=draft).json()
    assert client.post(f"{root}/close").status_code == 204
    db_path = project_dir / "project.db"
    # 在隔离测试工程中还原旧版结构，验证有实际帧图数据的升级与升级前备份。
    migrate.command.downgrade(migrate._config(db_path), "0026_creation_run")
    migrate.upgrade_to_head(db_path)
    backups = list((project_dir / "backups" / "schema").glob("project-0026_creation_run-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert "story" not in {c[1] for c in connection.execute("PRAGMA table_info(frame_card)")}
        assert connection.execute("SELECT adopted_asset_id FROM frame_card").fetchone()[0] == aid
    response = client.post("/api/v1/projects/open", json={"dir": str(project_dir)})
    assert response.status_code == 200, response.text
    workspace = client.get(f"{root}/shots/{sid}/frames").json()
    assert workspace["video"] == expected_video
    card = workspace["cards"][0]
    assert card["id"] == cid and card["adopted_asset_id"] == aid
    assert card["candidates"][0]["asset_id"] == aid
    assert card["story"] == "" and card["role"] == "key"


@pytest.mark.asyncio
async def test_video_freezes_only_selected_frames_and_never_auto_adopts(client, pid, monkeypatch):
    root, sid, cid, aid = setup(client, pid)
    http_video(client, root)
    draft = {
        "prompt": "The girl turns around.",
        "duration": 5,
        "first_card_id": cid,
        "last_card_id": "",
    }
    plan = client.post(f"{root}/shots/{sid}/frame-video/plan", json=draft)
    assert plan.status_code == 200, plan.text
    response = client.post(f"{root}/shots/{sid}/frame-video/generate", json=draft)
    assert response.status_code == 201, response.text
    job = await fetch(db_of(pid), Job, response.json()["id"], "任务")
    params = load_json(job.params_json, {})
    assert params["creation_pipeline"] == "frames"
    assert params["director_intent"] == {}
    assert params["source_version_id"] is None
    assert params["prompt"] == draft["prompt"]
    assert params["duration"] == 5
    assert params["context"]["included"] == []
    # Later changing the adopted frame cannot change a queued video's inputs.
    other = upload_png(client, pid, "upload", "后来.png")
    cv = client.post(f"{root}/frame-cards/{cid}/candidates", json={"asset_id": other}).json()
    client.post(f"{root}/frame-cards/{cid}/adopt", json={"candidate_id": cv["id"]})
    first, last, refs = await generation._images_of(pid, job, params)
    assert first and first.name.endswith(".png") and last is None and refs == []
    assert params["first_frame_asset_id"] == aid
    from app.generation.providers import registry
    from app.generation.providers.base import TaskState

    class Provider:
        async def submit(self, req, *, client_id):
            self.req = req
            return "fake-task"

        async def poll(self, task_id):
            return TaskState("done", 1.0)

        async def fetch(self, task_id):
            return "test.mp4", b"test-video"

    provider = Provider()
    monkeypatch.setattr(registry, "provider", lambda name: provider)
    await generation._run_provider(pid, job, params)
    assert provider.req.refs == []
    assert provider.req.intent == {}
    assert provider.req.source_video is None
    assert provider.req.first_frame == first
    assert provider.req.prompt == draft["prompt"]
    old = await generation.add_version(pid, sid, asset_id=None)
    await generation.add_version(pid, sid, asset_id=None, params=params)
    shot = await fetch(db_of(pid), Shot, sid, "镜头")
    assert shot.current_version_id == old["id"]


def test_cross_shot_frames_and_missing_inputs_are_rejected(client, pid):
    root, sid, cid, _ = setup(client, pid)
    other = a_shot(client, pid)
    body = {"first_card_id": cid, "prompt": "Move forward.", "duration": 4}
    response = client.post(f"{root}/shots/{other}/frame-video/generate", json=body)
    assert response.status_code == 422
    assert (
        client.post(f"{root}/shots/{other}/frame-video/generate", json={"prompt": "Move"}).status_code
        == 422
    )
    assert (
        client.post(
            f"{root}/shots/{sid}/frame-video/generate", json={**body, "duration": -1}
        ).status_code
        == 422
    )


@pytest.mark.asyncio
async def test_generated_image_appends_candidate_without_selecting_or_changing_shot(client, pid):
    root, sid, cid, aid = setup(client, pid)
    job = Job(
        id="job_test",
        target_kind="frame_card",
        target_id=cid,
        params_json=json.dumps({"image_prompt": "少女站在门口", "ref_asset_ids": [aid]}),
    )
    landed = await images.land(pid, job, "generated.png", PNG_1PX + b"generated")
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert len(ws["cards"][0]["candidates"]) == 2
    assert ws["cards"][0]["adopted_asset_id"] == aid
    assert ws["cards"][0]["candidates"][-1]["snapshot"]["ref_asset_ids"] == [aid]
    assert landed["asset_id"] != aid
    shot = client.get(f"{root}/shots/{sid}").json()
    assert shot["first_frame_asset_id"] is None and shot["last_frame_asset_id"] is None


def test_frame_skill_and_unsupported_reference_service(client, pid, monkeypatch):
    root, _, cid, aid = setup(client, pid)
    client.patch(
        "/api/v1/settings",
        json={"image.provider": "http_api", "image.base_url": "http://127.0.0.1:9"},
    )
    body = {"target_kind": "frame_card", "target_id": cid, "prompt": "少女站在门口"}
    response = client.post(f"{root}/images/plan", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["skill"]["name"] == "shot_frame"
    assert "画面里没有人物" not in response.json()["prompt"]
    assert "person, people" not in response.json()["negative_prompt"]
    from app.generation.providers import image

    monkeypatch.setattr(image.get("http_api"), "supports_refs", False)
    queued = client.post(f"{root}/images/generate", json={**body, "ref_asset_ids": [aid]})
    assert queued.status_code == 201, queued.text
    params = load_json(queued.json()["params_json"], {})
    assert params["ref_asset_ids"] == []
    assert params["dropped_ref_asset_ids"] == [aid]
    assert queued.json()["plan"]["warnings"]


def test_frame_preset_override_and_strict_frame_inputs(client, pid):
    from tests.test_providers import GRAPH, write_preset

    root, sid, cid, _ = setup(client, pid)
    write_preset("frames-only", GRAPH)
    no_frame = {k: v for k, v in GRAPH.items() if k not in ("1", "2")}
    write_preset("legacy-ref", no_frame)
    assert (
        client.put(
            f"{root}/workflow-bindings", json={"generation_mode": "comfy_preset"}
        ).status_code
        == 200
    )
    client.put(f"{root}/preset", json={"preset_name": "legacy-ref"})
    body = {
        "first_card_id": cid,
        "last_card_id": cid,
        "prompt": "Stay still.",
        "duration": 4,
        "preset": "frames-only",
    }
    before = client.get(f"{root}/route").json()
    response = client.post(f"{root}/shots/{sid}/frame-video/generate", json=body)
    assert response.status_code == 201, response.text
    assert load_json(response.json()["params_json"], {})["preset"] == "frames-only"
    assert client.get(f"{root}/route").json() == before
    body.update(last_card_id="", preset="legacy-ref")
    assert client.post(f"{root}/shots/{sid}/frame-video/generate", json=body).status_code == 422


def test_remove_card_clears_only_its_frame_draft_binding(client, pid):
    root, sid, cid, aid = setup(client, pid)
    client.put(f"{root}/shots/{sid}/frame-video", json={"first_card_id": cid, "prompt": "Move"})
    response = client.delete(f"{root}/frame-cards/{cid}")
    assert response.status_code == 200, response.text
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert ws["cards"] == [] and ws["video"]["first_card_id"] == ""
    assert any(a["id"] == aid for a in client.get(f"{root}/assets").json())
