"""已切好的片段可以手动选帧，并且始终受本段区间约束。"""

from tests.conftest import upload_png
from tests.test_ingest import make_real_video


def imported_shots(client, pid, tmp_path):
    root = f"/api/v1/projects/{pid}"
    clip = make_real_video(tmp_path / "source.mp4", duration=6)
    asset = client.post(f"{root}/ingest/register", json={"path": str(clip)}).json()
    result = client.post(
        f"{root}/ingest/run",
        json={
            "asset_id": asset["id"],
            "method": "fixed",
            "chunk_seconds": 2,
            "range_in": 1,
            "range_out": 5,
        },
    )
    assert result.status_code == 201, result.text
    return root, result.json()["shots"]


def test_select_multiple_frames_from_existing_segment_and_replace_reference(client, pid, tmp_path):
    root, shots = imported_shots(client, pid, tmp_path)
    sid = shots[1]["id"]
    before = client.get(f"{root}/shots/{sid}").json()
    assert client.get(f"{root}/shots/{sid}/frames").json()["cards"] == []
    source_response = client.get(f"{root}/shots/{sid}/frame-source")
    assert source_response.status_code == 200, source_response.text
    source = source_response.json()
    assert (source["in_point"], source["out_point"]) == (3, 5)
    select_url = f"{root}/shots/{sid}/source-frames"
    first = client.post(
        select_url, json={"source_version_id": source["source_version_id"], "at": 3.5}
    )
    assert first.status_code == 201, first.text
    cid = first.json()["card_id"]
    second = client.post(
        select_url, json={"source_version_id": source["source_version_id"], "at": 4}
    )
    assert second.status_code == 201, second.text
    reference = upload_png(client, pid, name="character-reference.png")
    client.patch(
        f"{root}/frame-cards/{cid}",
        json={"refs": [first.json()["asset_id"], reference], "prompt": "独立画风"},
    )
    candidate = client.post(
        f"{root}/frame-cards/{cid}/candidates", json={"asset_id": reference}
    ).json()
    client.post(f"{root}/frame-cards/{cid}/adopt", json={"candidate_id": candidate["id"]})
    replaced = client.post(
        select_url,
        json={
            "source_version_id": source["source_version_id"],
            "at": 4.5,
            "card_id": cid,
        },
    )
    assert replaced.status_code == 201, replaced.text
    assert replaced.json()["card_id"] == cid
    workspace = client.get(f"{root}/shots/{sid}/frames").json()
    assert len(workspace["cards"]) == 2
    card = next(c for c in workspace["cards"] if c["id"] == cid)
    assert card["refs"] == [replaced.json()["asset_id"], reference]
    assert card["prompt"] == "独立画风"
    assert card["adopted_asset_id"] == reference
    assert len(card["candidates"]) == 1
    after = client.get(f"{root}/shots/{sid}").json()
    assert after["current_version_id"] == before["current_version_id"]
    assert after["duration"] == before["duration"]


def test_source_frame_selection_rejects_other_segment_times_versions_and_cards(
    client, pid, tmp_path
):
    root, shots = imported_shots(client, pid, tmp_path)
    left, right = (s["id"] for s in shots)
    source = client.get(f"{root}/shots/{right}/frame-source").json()
    url = f"{root}/shots/{right}/source-frames"
    for at in (0, 2.99, 5, 6):
        result = client.post(url, json={"source_version_id": source["source_version_id"], "at": at})
        assert result.status_code == 422, result.text
    foreign = client.get(f"{root}/shots/{left}/frame-source").json()
    result = client.post(url, json={"source_version_id": foreign["source_version_id"], "at": 3.5})
    assert result.status_code == 422, result.text
    card = client.post(f"{root}/shots/{left}/frames", json={"title": "其他镜头"}).json()
    result = client.post(
        url,
        json={"source_version_id": source["source_version_id"], "at": 3.5, "card_id": card["id"]},
    )
    assert result.status_code == 422, result.text
    assert client.get(f"{root}/shots/{right}/frames").json()["cards"] == []


def test_source_picker_uses_latest_segment_after_splitting(client, pid, tmp_path):
    root, shots = imported_shots(client, pid, tmp_path)
    sid = shots[0]["id"]
    result = client.post(f"{root}/shots/{sid}/split", json={"at_seconds": 1})
    assert result.status_code == 200, result.text
    source = client.get(f"{root}/shots/{sid}/frame-source").json()
    assert (source["in_point"], source["out_point"]) == (1, 2)
    invalid = client.post(
        f"{root}/shots/{sid}/source-frames",
        json={"source_version_id": source["source_version_id"], "at": 2.5},
    )
    assert invalid.status_code == 422
