"""制帧素材按实体分类；容量按当前接口/工作流解析，顺序与舍弃清单冻结。"""

import json

import pytest

from app.generation.providers.image import GeminiImages, ImageConfig, OpenAiImages
from tests.conftest import upload_png
from tests.test_frame_authoring import setup
from tests.test_providers import with_ref_slots, write_preset


def test_material_picker_uses_current_entity_references_not_all_assets(client, pid):
    root, _, _, old = setup(client, pid)
    character_image = upload_png(client, pid, name="角色设定.png")
    location_image = upload_png(client, pid, name="场景设定.png")
    prop_image = upload_png(client, pid, name="道具设定.png")
    for path, name, aid in (
        ("characters", "阿岚", character_image),
        ("locations", "客厅", location_image),
        ("props", "来信", prop_image),
    ):
        response = client.post(f"{root}/{path}", json={"name": name, "default_asset_id": aid})
        assert response.status_code == 201, response.text
    response = client.get(f"{root}/frame-reference-materials")
    assert response.status_code == 200, response.text
    groups = response.json()["groups"]
    assert set(groups) == {"character", "location", "prop"}
    assert [row["asset_id"] for row in groups["character"]] == [character_image]
    assert [row["asset_id"] for row in groups["location"]] == [location_image]
    assert [row["asset_id"] for row in groups["prop"]] == [prop_image]
    assert all(row["available"] for rows in groups.values() for row in rows)
    assert old not in [row["asset_id"] for rows in groups.values() for row in rows]


@pytest.mark.parametrize("slots", [0, 1, 2])
def test_workflow_capacity_keeps_first_references_and_freezes_dropped_tail(client, pid, slots):
    root, _, cid, first = setup(client, pid)
    second = upload_png(client, pid, name="第二参考.png")
    third = upload_png(client, pid, name="第三参考.png")
    write_preset("frame-image-capacity", with_ref_slots(slots))
    client.patch(
        "/api/v1/settings",
        json={"image.provider": "comfy_preset", "image.preset": "frame-image-capacity"},
    )
    requested = [third, first, second]
    body = {
        "target_kind": "frame_card",
        "target_id": cid,
        "prompt": "少女站在客厅里，望着桌上的信封。",
        "ref_asset_ids": [*requested, third],
    }
    plan = client.post(f"{root}/images/plan", json=body)
    assert plan.status_code == 200, plan.text
    data = plan.json()
    assert data["can_generate"], data["missing"]
    assert data["provider"]["max_refs"] == slots
    assert [r["asset_id"] for r in data["refs"]] == requested[:slots]
    assert [r["asset_id"] for r in data["dropped_refs"]] == requested[slots:]
    generated = client.post(f"{root}/images/generate", json=body)
    assert generated.status_code == 201, generated.text
    frozen = json.loads(generated.json()["params_json"])
    assert frozen["ref_asset_ids"] == requested[:slots]
    assert frozen["requested_ref_asset_ids"] == requested
    assert frozen["dropped_ref_asset_ids"] == requested[slots:]
    assert frozen["reference_limit"] == slots
    # 改图不改变已经入队的清单。
    write_preset("frame-image-capacity", with_ref_slots(3))
    assert (
        json.loads(client.get(f"{root}/jobs/{generated.json()['id']}").json()["params_json"])[
            "ref_asset_ids"
        ]
        == requested[:slots]
    )


def test_image_api_capacity_is_used_for_frame_plans(client, pid):
    root, _, cid, first = setup(client, pid)
    second = upload_png(client, pid, name="第二参考.png")
    client.patch(
        "/api/v1/settings",
        json={
            "image.provider": "openai_images",
            "image.base_url": "http://127.0.0.1:9",
            "image.model": "dall-e-2",
            "image.api_key": "fake-test-key",
        },
    )
    response = client.post(
        f"{root}/images/plan",
        json={
            "target_kind": "frame_card",
            "target_id": cid,
            "prompt": "A girl stands by the table.",
            "ref_asset_ids": [second, first],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["provider"]["max_refs"] == 1
    assert [r["asset_id"] for r in response.json()["refs"]] == [second]
    assert [r["asset_id"] for r in response.json()["dropped_refs"]] == [first]


def test_known_api_limits_do_not_invent_a_limit_for_unknown_models():
    assert OpenAiImages().reference_limit(ImageConfig("openai_images", model="gpt-image-1")) == 16
    assert (
        OpenAiImages().reference_limit(ImageConfig("openai_images", model="custom-relay-model"))
        is None
    )
    assert (
        GeminiImages().reference_limit(ImageConfig("gemini", model="gemini-3-pro-image-preview"))
        == 14
    )
    assert (
        GeminiImages().reference_limit(ImageConfig("gemini", model="gemini-2.5-flash-image"))
        is None
    )
