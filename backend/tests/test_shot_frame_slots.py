"""镜头上那两个显式槽位：首帧 / 末帧。

以前 `Shot` 上根本没有「哪一张是首帧」这个字段，首帧只能靠账单里优先级最高的那一条顶上，
于是角色三视图被标成首帧、也真的被喂进 `AIVS_FIRST_FRAME`。这里钉住四件事：

  1. **「哪一张是首帧」是用户按下去的那一下**——写进 `shot.first_frame_asset_id`，
     账单照它出一条 `kind="first_frame"` 的条目，别的条目一律是参考素材；
  2. 槽位**只能是图片**：模型端那两个入口接的是 LoadImage，视频 / 音频请当参考素材用；
  3. **清空槽位传空串**（PATCH 里的 `null` 会被 `exclude_none` 吃掉）；
  4. `GET /shots/{id}` 顺手把两张图的相对路径带回来，前端不必再按 id 拉一遍资产。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import PNG_1PX, error_of, upload_png


def a_shot(client: TestClient, pid: str) -> str:
    scene = client.post(
        f"/api/v1/projects/{pid}/scenes", json={"title": "第一场", "prompt": "雨夜"}
    ).json()
    shot = client.post(
        f"/api/v1/projects/{pid}/scenes/{scene['id']}/shots",
        json={"title": "推近", "prompt": "雨夜，林昭推门"},
    )
    assert shot.status_code == 201, shot.text
    return str(shot.json()["id"])


def upload(client: TestClient, pid: str, name: str, mime: str) -> str:
    resp = client.post(
        f"/api/v1/projects/{pid}/assets/upload",
        data={"kind": "upload"},
        files={"file": (name, PNG_1PX + name.encode(), mime)},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def patch_shot(client: TestClient, pid: str, shot_id: str, body: dict[str, Any]) -> Any:
    return client.patch(f"/api/v1/projects/{pid}/shots/{shot_id}", json=body)


def test_the_slots_are_written_read_back_and_cleared(client: TestClient, pid: str) -> None:
    shot_id = a_shot(client, pid)
    first = upload_png(client, pid, "upload", "第一格.png")
    last = upload_png(client, pid, "upload", "最后一格.png")

    resp = patch_shot(
        client, pid, shot_id, {"first_frame_asset_id": first, "last_frame_asset_id": last}
    )
    assert resp.status_code == 200, resp.text

    got = client.get(f"/api/v1/projects/{pid}/shots/{shot_id}").json()
    assert (got["first_frame_asset_id"], got["last_frame_asset_id"]) == (first, last)
    # 路径一起给：前端画槽位缩略图时不必按 id 再拉一遍资产
    assert got["first_frame_path"].startswith("assets/uploads/")
    assert got["last_frame_path"].startswith("assets/uploads/")

    # 清空传空串：PATCH 里的 null 会被 exclude_none 吃掉，永远到不了 service
    assert patch_shot(client, pid, shot_id, {"first_frame_asset_id": ""}).status_code == 200
    got = client.get(f"/api/v1/projects/{pid}/shots/{shot_id}").json()
    assert (got["first_frame_asset_id"], got["first_frame_path"]) == (None, None)
    assert got["last_frame_asset_id"] == last, "只清了首帧，末帧不该跟着丢"


def test_a_video_cannot_be_a_frame_slot(client: TestClient, pid: str) -> None:
    """首尾帧决定画面的第一格 / 最后一格，那个入口接的是 LoadImage。"""
    shot_id = a_shot(client, pid)
    clip = upload(client, pid, "动作参考.mp4", "video/mp4")

    resp = patch_shot(client, pid, shot_id, {"first_frame_asset_id": clip})
    assert resp.status_code == 422
    err = error_of(resp)
    assert err["title"] == "首帧只能是图片"
    assert "video" in err["detail"]
    assert any("参考素材" in s for s in err["suggestions"]), "得指出视频该往哪儿放"

    resp = patch_shot(client, pid, shot_id, {"last_frame_asset_id": clip})
    assert resp.status_code == 422 and error_of(resp)["title"] == "末帧只能是图片"
    got = client.get(f"/api/v1/projects/{pid}/shots/{shot_id}").json()
    assert (got["first_frame_asset_id"], got["last_frame_asset_id"]) == (None, None)


def test_an_unknown_asset_in_a_slot_is_a_structured_404(client: TestClient, pid: str) -> None:
    shot_id = a_shot(client, pid)
    resp = patch_shot(client, pid, shot_id, {"first_frame_asset_id": "ast_nope"})
    assert resp.status_code == 404
    assert "首帧资产" in error_of(resp)["title"]


def test_a_new_shot_can_be_created_with_a_first_frame(client: TestClient, pid: str) -> None:
    scene = client.post(
        f"/api/v1/projects/{pid}/scenes", json={"title": "第一场", "prompt": "雨夜"}
    ).json()
    first = upload_png(client, pid, "upload", "第一格.png")
    resp = client.post(
        f"/api/v1/projects/{pid}/scenes/{scene['id']}/shots",
        json={"title": "推近", "prompt": "推门", "first_frame_asset_id": first},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["first_frame_asset_id"] == first

    bad = client.post(
        f"/api/v1/projects/{pid}/scenes/{scene['id']}/shots",
        json={"title": "跟拍", "prompt": "跟着走", "first_frame_asset_id": "ast_nope"},
    )
    assert bad.status_code == 404, "新建时也走同一道校验"


def test_the_bill_shows_the_slot_as_the_first_frame(client: TestClient, pid: str) -> None:
    """账单上那条「首帧 · xxx」就是槽位里的那张图，角色表照旧是参考素材。"""
    shot_id = a_shot(client, pid)
    first = upload_png(client, pid, "upload", "第一格.png")
    patch_shot(client, pid, shot_id, {"first_frame_asset_id": first})

    ctx = client.get(f"/api/v1/projects/{pid}/shots/{shot_id}/context").json()
    item = next(i for i in ctx["items"] if i["kind"] == "first_frame")
    assert item["role"] == "first_frame"
    assert item["label"].startswith("首帧 · ")
    assert item["media"] == "image"
    assert item["included"] is True
    assert item["reason"] == "镜头上指定的首帧"
    # 首帧不占参考素材的槽位：它走 AIVS_FIRST_FRAME，不进 AIVS_REF_*
    assert ctx["capacity"]["ref_count"] == 0


def test_a_slot_whose_asset_was_deleted_is_reported_not_hidden(
    client: TestClient, pid: str
) -> None:
    """资产删了槽位就指着一个不存在的 id——账单要说出来，不能当没这回事。"""
    shot_id = a_shot(client, pid)
    first = upload_png(client, pid, "upload", "第一格.png")
    patch_shot(client, pid, shot_id, {"first_frame_asset_id": first})
    assert client.delete(f"/api/v1/projects/{pid}/assets/{first}").status_code in (200, 204)

    got = client.get(f"/api/v1/projects/{pid}/shots/{shot_id}").json()
    assert got["first_frame_asset_id"] == first
    assert got["first_frame_path"] is None, "文件没了就别给一个假路径"
    ctx = client.get(f"/api/v1/projects/{pid}/shots/{shot_id}/context").json()
    item = next(i for i in ctx["items"] if i["kind"] == "first_frame")
    assert "找不到" in item["label"]
    assert item["missing_file"] is True
