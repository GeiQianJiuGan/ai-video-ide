"""导演给出图画面接设定图，制帧视频默认跟随本镜头的采用帧。"""

import json

import pytest

from app.ai.director import agent
from app.ai.director.tools import run_read, to_op
from app.core.errors import AppError
from tests.conftest import upload_png
from tests.test_director_agent import call, use_fake_llm
from tests.test_frame_authoring import http_video, setup
from tests.test_shot_frame_slots import a_shot


def test_director_refs_land_on_image_cards_and_image_jobs(client, pid, monkeypatch):
    root, sid, cid, existing = setup(client, pid)
    character = upload_png(client, pid, name="人物.png")
    location = upload_png(client, pid, name="场景.png")
    for path, name, aid in (("characters", "阿岚", character), ("locations", "客厅", location)):
        response = client.post(f"{root}/{path}", json={"name": name, "default_asset_id": aid})
        assert response.status_code == 201, response.text
    client.patch(
        f"{root}/frame-cards/{cid}", json={"refs": [existing], "prompt": "阿岚站在客厅门边。"}
    )
    before = client.get(f"{root}/shots/{sid}").json()
    use_fake_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call("get_frame_workspace", shot_id=sid),
                    call("list_frame_reference_materials"),
                ],
            },
            {
                "content": "",
                "tool_calls": [
                    call(
                        "update_frame_card",
                        shot_id=sid,
                        card_id=cid,
                        refs=[existing, character, location, character],
                        why="为这张图片补齐人物和场景设定",
                    )
                ],
            },
            {"content": "已提出画面参考图修改。", "tool_calls": []},
        ],
    )
    response = client.post(
        f"{root}/director/chat", json={"message": "给制作图片的画面补人物和场景", "scope": "frames"}
    )
    assert response.status_code == 201, response.text
    ops = response.json()["ops"]
    assert len(ops) == 1 and ops[0]["target"] == "frame_card"
    assert ops[0]["after"]["refs"] == [existing, character, location]
    assert any("阿岚" in label for label in ops[0]["after"]["reference_labels"])
    assert client.get(f"{root}/shots/{sid}/frames").json()["cards"][0]["refs"] == [existing]
    applied = client.post(f"{root}/director/apply", json={"ops": ops}).json()
    assert applied["count"] == 1 and applied["failed"] == []
    workspace = client.get(f"{root}/shots/{sid}/frames").json()
    assert workspace["cards"][0]["refs"] == [existing, character, location]
    assert workspace["cards"][0]["adopted_asset_id"] == existing
    assert client.get(f"{root}/shots/{sid}").json() == before
    client.patch(
        "/api/v1/settings",
        json={
            "image.provider": "openai_images",
            "image.base_url": "http://127.0.0.1:9",
            "image.model": "gpt-image-1",
            "image.api_key": "fake-test-key",
        },
    )
    generated = client.post(f"{root}/frame-cards/{cid}/generate")
    assert generated.status_code == 201, generated.text
    assert json.loads(generated.json()["params_json"])["ref_asset_ids"] == [
        existing,
        character,
        location,
    ]


@pytest.mark.asyncio
async def test_frame_tools_expose_real_materials_and_reject_wrong_shots_and_bad_refs(client, pid):
    root, sid, cid, aid = setup(client, pid)
    other = a_shot(client, pid)
    workspace = await run_read(pid, "get_frame_workspace", {"shot_id": sid})
    assert workspace["cards"][0]["id"] == cid
    snapshot = json.loads(await agent._snapshot(pid, include_frames=True))
    assert any(
        w["shot_id"] == sid and w["cards"][0]["id"] == cid for w in snapshot["frame_workspaces"]
    )
    for args in (
        {"shot_id": other, "card_id": cid, "refs": [aid]},
        {"shot_id": sid, "card_id": cid, "refs": ["ast_missing"]},
    ):
        with pytest.raises(AppError):
            await to_op(pid, "update_frame_card", args, 1)
    # 落库入口也复查归属，不能伪造或重放到另一个镜头。
    response = client.post(
        f"{root}/director/apply",
        json={
            "ops": [
                {
                    "op": "update_frame_card",
                    "shot_id": other,
                    "card_id": cid,
                    "after": {"refs": [aid]},
                }
            ]
        },
    )
    assert response.json()["count"] == 0 and len(response.json()["failed"]) == 1
    assert client.get(f"{root}/shots/{sid}/frames").json()["cards"][0]["refs"] == []


def test_video_defaults_follow_adopted_local_frames_and_keep_explicit_choices(client, pid):
    root, sid, first, first_asset = setup(client, pid)
    client.patch(f"{root}/frame-cards/{first}", json={"role": "first"})
    tail = client.post(f"{root}/shots/{sid}/frames", json={"title": "收尾", "role": "last"}).json()[
        "id"
    ]
    tail_asset = upload_png(client, pid, name="尾帧.png")
    candidate = client.post(
        f"{root}/frame-cards/{tail}/candidates", json={"asset_id": tail_asset}
    ).json()
    client.post(f"{root}/frame-cards/{tail}/adopt", json={"candidate_id": candidate["id"]})
    ws = client.get(f"{root}/shots/{sid}/frames").json()
    assert ws["video"] == {"first_card_id": first, "last_card_id": tail}
    http_video(client, root)
    response = client.post(
        f"{root}/shots/{sid}/frame-video/generate", json={"prompt": "少女从门边走进客厅。"}
    )
    assert response.status_code == 201, response.text
    frozen = json.loads(response.json()["params_json"])
    assert frozen["first_frame_asset_id"] == first_asset
    assert frozen["last_frame_asset_id"] == tail_asset
    assert frozen["context"]["included"] == []
    # 手选画面与「仅使用首帧」保留；之后换采用图，下一次请求跟随新图。
    client.put(
        f"{root}/shots/{sid}/frame-video",
        json={"prompt": "少女回头。", "first_card_id": tail, "last_card_id": ""},
    )
    replacement = upload_png(client, pid, name="新尾帧.png")
    cv = client.post(f"{root}/frame-cards/{tail}/candidates", json={"asset_id": replacement}).json()
    client.post(f"{root}/frame-cards/{tail}/adopt", json={"candidate_id": cv["id"]})
    saved = client.get(f"{root}/shots/{sid}/frames").json()["video"]
    assert saved["first_card_id"] == tail and saved["last_card_id"] == ""
    plan = client.post(f"{root}/shots/{sid}/frame-video/plan", json={}).json()
    assert plan["inputs"]["first_frame_asset_id"] == replacement
    assert "last_frame_asset_id" not in plan["inputs"]


def test_unadopted_candidates_never_become_default_video_inputs(client, pid):
    root, _, _, aid = setup(client, pid)
    sid = a_shot(client, pid)
    card = client.post(f"{root}/shots/{sid}/frames", json={"title": "开场", "role": "first"}).json()
    client.post(f"{root}/frame-cards/{card['id']}/candidates", json={"asset_id": aid})
    workspace = client.get(f"{root}/shots/{sid}/frames").json()
    assert workspace["video"] == {}
    assert workspace["cards"][0]["adopted_asset_id"] is None
    response = client.post(
        f"{root}/shots/{sid}/frame-video/plan", json={"prompt": "少女走进客厅。"}
    )
    assert response.status_code == 422
