"""一次编排在队列里合并成一条：`job.batch_*` 四列 + `queue.batches[]` + 整批重跑。

盯的是「用户点的那一下」有没有在队列里保持成一件事：

  1. **一次编排 = 一条**：单线程续接入队几十条任务，`batches[]` 里只有一条，成员认得回来；
  2. **进度是第 N/M 步**，不是百分比——ComfyUI 不回显进度，`step` / `settled` / `total`
     必须是从成员数出来的真数字；
  3. **失败也不清 batch_id**：单线程一条失败会连带停掉后面全部，整批重跑就是靠它把成员
     找回来的（已完成的一条都不重做）；
  4. **等上游的成员重跑后回到 waiting 而不是 queued**：链条的先后就是这一批的意义，
     全塞成 queued 会让它们一拥而上，下游拿不到上游这次的真末帧；
  5. **单个镜头的生成不属于任何一批**：四列是空的，`batches[]` 里没有它。

所有用例先 `POST /queue/pause`，pump 就不会真去连 ComfyUI。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import error_of
from tests.test_sequence import two_scenes

API = "/api/v1"


@pytest.fixture(autouse=True)
def _preset(video_preset: str) -> None:
    """这个文件里每一条都要真入队，所以默认那条路必须有一份能用的图。

    入队门槛在 `services/route.py::require()`（缺预设 = 四要素错误，不进队列），
    而这里测的是批次身份与整批重跑，不是那道门槛。
    """


def test_sequential_run_merges_into_one_batch(client: TestClient, pid: str) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    _, _, sa, sb = two_scenes(client, pid)
    out = client.post(f"{API}/projects/{pid}/sequence/run", json={"mode": "sequential"}).json()
    assert out["batch_id"], "一次编排必须有批次身份，不然队列里认不回来"

    state = client.get(f"{API}/projects/{pid}/queue").json()
    assert len(state["batches"]) == 1, "两条任务是一次编排，队列里只该合并成一条"
    batch = state["batches"][0]
    assert batch["id"] == out["batch_id"]
    assert batch["kind"] == "sequential"
    assert "单线程续接" in batch["label"]
    assert batch["total"] == 2
    # 队列暂停着，一条都没跑：第 0/2 步，而不是一个编出来的百分比
    assert (batch["settled"], batch["step"], batch["status"]) == (0, 0, "queued")
    assert set(batch["job_ids"]) == {j["id"] for j in state["jobs"]}
    assert batch["retryable"] is False, "没有失败也没有取消，没什么可重跑的"

    head = next(j for j in state["jobs"] if j["shot_id"] == sa)
    tail = next(j for j in state["jobs"] if j["shot_id"] == sb)
    assert (head["batch_seq"], tail["batch_seq"]) == (1, 2), "第几步要能说得出来"
    assert head["batch_id"] == tail["batch_id"] == batch["id"]


def test_retry_batch_restores_the_chain(client: TestClient, pid: str) -> None:
    """整批重跑：失败与取消的回队列，等上游的仍然是 waiting，已完成的一条都不重做。"""
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    _, _, sa, sb = two_scenes(client, pid)
    client.post(f"{API}/projects/{pid}/sequence/run", json={"mode": "sequential"})
    state = client.get(f"{API}/projects/{pid}/queue").json()
    batch_id = state["batches"][0]["id"]
    head = next(j for j in state["jobs"] if j["shot_id"] == sa)
    tail = next(j for j in state["jobs"] if j["shot_id"] == sb)

    # 造出「链头失败 → 下游被连带取消」这个现场
    assert client.post(f"{API}/projects/{pid}/jobs/{head['id']}/cancel").status_code == 200
    assert client.post(f"{API}/projects/{pid}/jobs/{tail['id']}/cancel").status_code == 200
    batch = client.get(f"{API}/projects/{pid}/queue").json()["batches"][0]
    assert batch["status"] == "canceled"
    assert batch["settled"] == 2
    assert batch["retryable"] is True

    resp = client.post(f"{API}/projects/{pid}/queue/batches/{batch_id}/retry")
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 2

    jobs = client.get(f"{API}/projects/{pid}/jobs").json()
    again_head = next(j for j in jobs if j["id"] == head["id"])
    again_tail = next(j for j in jobs if j["id"] == tail["id"])
    assert again_head["status"] == "queued", "链头不等任何人"
    assert again_tail["status"] == "waiting", "等上游末帧的成员不能一拥而上"
    assert again_tail["wait_reason"], "等待必须能解释，不能只是不动"
    assert again_head["error"] is None and again_head["finished_at"] is None


def test_retry_batch_refuses_when_nothing_to_redo(client: TestClient, pid: str) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    two_scenes(client, pid)
    client.post(f"{API}/projects/{pid}/sequence/run", json={"mode": "sequential"})
    batch_id = client.get(f"{API}/projects/{pid}/queue").json()["batches"][0]["id"]
    resp = client.post(f"{API}/projects/{pid}/queue/batches/{batch_id}/retry")
    assert resp.status_code == 409, resp.text
    err = error_of(resp)
    assert err["code"] == "CONFLICT"
    assert "版本永不覆盖" in err["detail"]

    missing = client.post(f"{API}/projects/{pid}/queue/batches/jbt_nope/retry")
    assert missing.status_code == 404
    error_of(missing)


def test_cancel_batch_stops_the_unsettled_members(client: TestClient, pid: str) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    two_scenes(client, pid)
    client.post(f"{API}/projects/{pid}/sequence/run", json={"mode": "sequential"})
    batch_id = client.get(f"{API}/projects/{pid}/queue").json()["batches"][0]["id"]
    resp = client.post(f"{API}/projects/{pid}/queue/batches/{batch_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 2
    state = client.get(f"{API}/projects/{pid}/queue").json()
    assert {j["status"] for j in state["jobs"]} == {"canceled"}
    assert state["batches"][0]["status"] == "canceled"


def test_single_shot_job_belongs_to_no_batch(client: TestClient, pid: str) -> None:
    """空值是常态：单个镜头的生成不属于任何编排，队列里照旧一行一条。"""
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    _, _, sa, _ = two_scenes(client, pid)
    resp = client.post(f"{API}/projects/{pid}/shots/{sa}/generate", json={})
    assert resp.status_code == 201, resp.text
    state = client.get(f"{API}/projects/{pid}/queue").json()
    assert state["batches"] == []
    job = state["jobs"][0]
    assert (job["batch_id"], job["batch_label"], job["batch_kind"], job["batch_seq"]) == (
        None,
        None,
        None,
        None,
    )
