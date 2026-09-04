"""素材的那一句描述：能存、能改、能清、能列出缺的那些。

这一句是**模型引用这个素材时唯一看得到的说明**（最后由
`generation/providers/base.py::ref_hint` 渲染进 prompt），所以它必须有一条明确的
写路径，而不是只能靠 AI 顺手带上。这里盯四件事：

  · `PATCH /assets/{id}` 改得动，`''` 清得掉（`null` 是「这次不改」）；
  · `path` / `kind` 这些落盘事实改不动——改了就会和磁盘上的文件对不上；
  · 不存在的 id 是四要素 404，不是一个空 200；
  · `undescribed` 不含抽出来的首尾帧（`TRANSIENT_KINDS`）——给临时文件写描述没有意义。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import error_of, upload_png

API = "/api/v1"


def test_patch_sets_and_clears_the_description(client: TestClient, pid: str) -> None:
    aid = upload_png(client, pid, name="alan.png")
    assert client.get(f"{API}/projects/{pid}/assets").json()[0]["description"] is None

    resp = client.patch(
        f"{API}/projects/{pid}/assets/{aid}",
        json={"description": "褪色军绿夹克，短发，左颊一道旧疤"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "褪色军绿夹克，短发，左颊一道旧疤"

    # 刷新一遍：真的落库了，不只是回显
    rows = client.get(f"{API}/projects/{pid}/assets").json()
    assert rows[0]["description"] == "褪色军绿夹克，短发，左颊一道旧疤"

    # 空字符串 = 清掉。`null` 走 exclude_none，是「这次不改」
    assert (
        client.patch(f"{API}/projects/{pid}/assets/{aid}", json={"description": ""}).json()[
            "description"
        ]
        == ""
    )
    keep = client.patch(f"{API}/projects/{pid}/assets/{aid}", json={"description": None})
    assert keep.status_code == 200, keep.text


def test_patch_refuses_to_touch_on_disk_facts(client: TestClient, pid: str) -> None:
    """`path` / `kind` / `sha1` 是落盘事实。改它们只会让登记和磁盘对不上。"""
    aid = upload_png(client, pid, name="fact.png")
    before = client.get(f"{API}/projects/{pid}/assets").json()[0]
    resp = client.patch(
        f"{API}/projects/{pid}/assets/{aid}",
        json={"description": "一张图", "path": "assets/别的地方.png", "kind": "video"},
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["description"] == "一张图"
    assert row["path"] == before["path"]
    assert row["kind"] == before["kind"]


def test_patch_missing_asset_is_a_four_element_404(client: TestClient, pid: str) -> None:
    resp = client.patch(f"{API}/projects/{pid}/assets/ast_nope", json={"description": "x"})
    assert resp.status_code == 404
    assert error_of(resp)["code"] == "NOT_FOUND"


def test_undescribed_lists_only_real_assets_without_a_description(
    client: TestClient, pid: str
) -> None:
    described = upload_png(client, pid, name="has.png")
    bare = upload_png(client, pid, name="bare.png")
    client.patch(f"{API}/projects/{pid}/assets/{described}", json={"description": "有描述"})

    body = client.get(f"{API}/projects/{pid}/assets/undescribed").json()
    ids = [i["id"] for i in body["items"]]
    assert bare in ids
    assert described not in ids
    # 前端照它显示字数提示，所以这个数只能来自后端（`providers/base.py::DESC_MAX`）
    assert body["desc_max"] > 0


def test_undescribed_skips_transient_frames(client: TestClient, pid: str) -> None:
    """抽出来的首尾帧是可再生的临时文件，不进这张清单（照 `TRANSIENT_KINDS`）。"""
    frame = upload_png(client, pid, kind="frame", name="tail.png")
    normal = upload_png(client, pid, name="normal.png")
    ids = [i["id"] for i in client.get(f"{API}/projects/{pid}/assets/undescribed").json()["items"]]
    assert normal in ids
    assert frame not in ids
