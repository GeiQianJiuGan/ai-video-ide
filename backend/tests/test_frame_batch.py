"""全项目批量制图与首份结果自动采用。"""

import json

from app.core.errors import AppError, ErrorCode
from app.persistence.models_gen import Job
from app.services.images import images
from tests.conftest import PNG_1PX
from tests.test_shot_frame_slots import a_shot


async def test_first_generated_candidate_adopted_and_later_result_preserves_choice(client, pid):
    root = f"/api/v1/projects/{pid}"
    sid = a_shot(client, pid)
    cid = client.post(f"{root}/shots/{sid}/frames", json={"title": "首帧"}).json()["id"]
    first = await images.land(
        pid,
        Job(
            id="job_first",
            target_kind="frame_card",
            target_id=cid,
            params_json=json.dumps({"image_prompt": "3D CG"}),
        ),
        "first.png",
        PNG_1PX,
    )
    second = await images.land(
        pid,
        Job(id="job_second", target_kind="frame_card", target_id=cid, params_json="{}"),
        "second.png",
        PNG_1PX + b"second",
    )
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert ws["cards"][0]["adopted_asset_id"] == first["asset_id"]
    assert ws["video"]["first_card_id"] == cid
    assert len(ws["cards"][0]["candidates"]) == 2
    assert (
        client.post(
            f"{root}/frame-cards/{cid}/adopt", json={"candidate_id": second["id"]}
        ).status_code
        == 200
    )
    await images.land(
        pid,
        Job(id="job_third", target_kind="frame_card", target_id=cid, params_json="{}"),
        "third.png",
        PNG_1PX + b"third",
    )
    assert (
        client.get(f"{root}/shots/{sid}/frames").json()["cards"][0]["adopted_asset_id"]
        == second["asset_id"]
    )
    shot = client.get(f"{root}/shots/{sid}").json()
    assert shot["first_frame_asset_id"] is None


def test_generate_all_across_shots_inherits_prompts_and_skips_running(client, pid):
    root = f"/api/v1/projects/{pid}"
    client.post(f"{root}/queue/pause")
    client.patch(
        "/api/v1/settings",
        json={"image.provider": "http_api", "image.base_url": "http://127.0.0.1:9"},
    )
    ids = []
    for index in range(2):
        sid = a_shot(client, pid)
        scene = client.get(f"{root}/shots/{sid}").json()["scene_id"]
        client.put(f"{root}/scenes/{scene}/frame-prompts", json={"image_prompt": "转换为3D CG"})
        ids.append(
            client.post(f"{root}/shots/{sid}/frames", json={"title": f"画面{index}"}).json()["id"]
        )
    response = client.post(f"{root}/frames/generate-all")
    assert response.status_code == 200, response.text
    assert {item["card_id"] for item in response.json()["queued"]} == set(ids)
    for item in response.json()["queued"]:
        job = client.get(f"{root}/jobs/{item['job_id']}").json()
        assert json.loads(job["params_json"])["image_prompt"] == "转换为3D CG"
        assert json.loads(job["params_json"])["prompt"] == "转换为3D CG"
        assert job["batch_id"] == response.json()["batch_id"]
    queue = client.get(f"{root}/queue").json()
    batch = next(b for b in queue["batches"] if b["id"] == response.json()["batch_id"])
    assert batch["total"] == 2
    assert batch["kind"] == "frame_images"
    again = client.post(f"{root}/frames/generate-all").json()
    assert not again["queued"] and len(again["skipped"]) == 2


def test_batch_reports_missing_prompts_and_continues_after_failure(client, pid, monkeypatch):
    root = f"/api/v1/projects/{pid}"
    sid = a_shot(client, pid)
    ids = []
    for title, prompt in [("空白", ""), ("失败", "失败"), ("成功", "成功")]:
        ids.append(
            client.post(
                f"{root}/shots/{sid}/frames", json={"title": title, "prompt": prompt}
            ).json()["id"]
        )

    async def enqueue(pid, kind, cid, **kwargs):
        if cid == ids[1]:
            raise AppError(ErrorCode.MISSING_CAPABILITY, "未配置", "配置服务", ["打开设置"])
        return {"id": "job_ok", "plan": {"warnings": []}}

    monkeypatch.setattr(images, "enqueue", enqueue)
    result = client.post(f"{root}/frames/generate-all").json()
    assert result["skipped"][0]["card_id"] == ids[0]
    assert result["failed"][0]["card_id"] == ids[1]
    assert result["queued"][0]["card_id"] == ids[2]


async def test_image_scope_all_regenerates_completed_without_duplicate_active(client, pid):
    from tests.test_frame_authoring import setup

    root, sid, cid, aid = setup(client, pid)
    client.patch(f"{root}/frame-cards/{cid}", json={"prompt": "重新制图"})
    client.patch(
        "/api/v1/settings",
        json={"image.provider": "http_api", "image.base_url": "http://127.0.0.1:9"},
    )
    assert not client.post(f"{root}/frames/generate-all", json={"scope": "unfinished"}).json()[
        "queued"
    ]
    result = client.post(f"{root}/frames/generate-all", json={"scope": "all"}).json()
    assert len(result["queued"]) == 1
    assert not client.post(f"{root}/frames/generate-all", json={"scope": "all"}).json()["queued"]
    assert client.get(f"{root}/shots/{sid}/frames").json()["cards"][0]["adopted_asset_id"] == aid


async def test_video_scope_ignores_imported_source_and_groups_jobs(client, pid):
    from app.persistence.models_gen import GenerationVersion
    from app.services.base import db_of
    from tests.test_frame_authoring import http_video, setup

    root, first, _, aid = setup(client, pid)
    _, second, _, _ = setup(client, pid)
    http_video(client, root)
    for sid in (first, second):
        client.put(f"{root}/shots/{sid}/frame-video", json={"prompt": "连续动作", "duration": 4})
    async with db_of(pid).write() as session:
        session.add(
            GenerationVersion(id="ver_done", shot_id=first, asset_id=aid, source="generated")
        )
        session.add(
            GenerationVersion(id="ver_source", shot_id=second, asset_id=aid, source="imported")
        )
    result = client.post(f"{root}/frame-video/generate-all", json={"scope": "unfinished"})
    assert result.status_code == 200, result.text
    body = result.json()
    assert len(body["queued"]) == 1, body
    job = client.get(f"{root}/jobs/{body['queued'][0]}").json()
    assert job["shot_id"] == second and job["batch_id"] == body["batch_id"]
    again = client.post(f"{root}/frame-video/generate-all", json={"scope": "all"}).json()
    assert len(again["queued"]) == 1, again
    assert client.get(f"{root}/jobs/{again['queued'][0]}").json()["shot_id"] == first
    assert client.post(f"{root}/frames/generate-all", json={"scope": "bad"}).status_code == 422


async def test_sequential_scope_skips_completed_and_waits_for_active_job(client, pid, monkeypatch):
    from app.persistence.models_gen import GenerationVersion
    from app.services.base import db_of
    from app.services.generation import generation
    from app.services.sequence import sequence

    root = f"/api/v1/projects/{pid}"
    first, second, third = [a_shot(client, pid) for _ in range(3)]
    async with db_of(pid).write() as session:
        session.add(
            GenerationVersion(
                id="done_video", shot_id=first, asset_id="asset_done", source="generated"
            )
        )
        session.add(Job(id="job_active", shot_id=second, kind="image2video", status="queued"))
    calls = []

    async def plan(*args):
        return {"ref_drops": []}

    async def enqueue(pid, sid, **kwargs):
        calls.append((sid, kwargs))
        return {"id": "job_" + sid}

    monkeypatch.setattr(sequence, "plan", plan)
    monkeypatch.setattr(generation, "enqueue_shot", enqueue)
    response = client.post(
        f"{root}/sequence/run", json={"mode": "sequential", "scope": "unfinished"}
    )
    assert response.status_code == 201, response.text
    assert [c[0] for c in calls] == [third]
    assert calls[0][1]["wait_for_job_id"] == "job_active"
    calls.clear()
    response = client.post(f"{root}/sequence/run", json={"mode": "sequential", "scope": "all"})
    assert response.status_code == 201, response.text
    assert [c[0] for c in calls] == [first, third]
