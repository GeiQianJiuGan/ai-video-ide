"""视频优化二次处理（Refine）与版本谱系测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

API = "/api/v1"


def test_refine_kinds(client: TestClient) -> None:
    resp = client.get(f"{API}/refine/kinds")
    assert resp.status_code == 200
    kinds = resp.json()
    assert any(k["kind"] == "upscale" for k in kinds)
    assert any(k["kind"] == "interpolate" for k in kinds)
    assert any(k["kind"] == "recut" for k in kinds)


def test_refine_lineage_tracing(client: TestClient, pid: str) -> None:
    scene = client.post(f"{API}/projects/{pid}/scenes", json={"title": "测试场景"}).json()
    shot = client.post(
        f"{API}/projects/{pid}/scenes/{scene['id']}/shots",
        json={"title": "测试镜头", "duration": 4.0},
    ).json()

    # 1. 创建祖先版本 v1
    from app.services.generation import generation
    import asyncio

    loop = asyncio.get_event_loop()
    v1 = loop.run_until_complete(
        generation.add_version(
            pid,
            shot["id"],
            asset_id=None,
            kind="video",
            source="manual",
            duration=4.0,
        )
    )
    v2 = loop.run_until_complete(
        generation.add_version(
            pid,
            shot["id"],
            asset_id=None,
            kind="video",
            source="upscaled",
            parent_version_id=v1["id"],
            duration=4.0,
        )
    )

    # 3. 从 v2 派生出版本 v3
    v3 = loop.run_until_complete(
        generation.add_version(
            pid,
            shot["id"],
            asset_id=None,
            kind="video",
            source="interpolated",
            parent_version_id=v2["id"],
            duration=4.0,
        )
    )

    # 4. 查询 v3 的谱系 (Lineage)
    lineage_resp = client.get(f"{API}/projects/{pid}/versions/{v3['id']}/lineage")
    assert lineage_resp.status_code == 200, lineage_resp.text
    tree = lineage_resp.json()
    ancestors = tree["ancestors"]
    # 链式祖先顺序：v3 -> v2 -> v1
    assert len(ancestors) == 3
    assert ancestors[0]["id"] == v3["id"]
    assert ancestors[1]["id"] == v2["id"]
    assert ancestors[2]["id"] == v1["id"]

    # 查询 v1 的谱系（验证 children）
    lineage_v1 = client.get(f"{API}/projects/{pid}/versions/{v1['id']}/lineage").json()
    assert len(lineage_v1["children"]) == 1
    assert lineage_v1["children"][0]["id"] == v2["id"]
