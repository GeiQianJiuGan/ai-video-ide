"""M0 地基验收：健康检查、依赖探测、错误契约、ID、事件总线、数据库、WS。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.errors import AppError, ErrorCode, not_found
from app.core.ids import kind_of, new_id
from app.events.bus import Channel, Event, bus
from app.persistence.db import Database


def test_health(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == settings.version
    assert body["schema_version"] == settings.schema_version


def test_deps_probe_never_raises(client: TestClient) -> None:
    """外部依赖缺失时必须返回结构化状态，而不是抛 500。"""
    resp = client.get("/api/v1/system/deps")
    assert resp.status_code == 200
    names = {d["name"] for d in resp.json()}
    assert names == {"ffmpeg", "comfyui", "llm"}
    for dep in resp.json():
        assert isinstance(dep["ok"], bool)
        assert dep["detail"], f"{dep['name']} 缺少 detail，违反「绝不静默失败」"
        if not dep["ok"]:
            assert dep["hint"], f"{dep['name']} 失败但没给修复建议"


def test_openapi_available(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200


# --- 错误契约 ---


def test_app_error_shape() -> None:
    err = AppError(
        ErrorCode.MISSING_CAPABILITY,
        "当前 Workflow 不支持 Character Reference",
        "Wan T2V 未声明 character_reference 能力。",
        ["切换到 Wan I2V", "补充 image 槽位绑定"],
        {"shot_id": "sht_1"},
    )
    data = err.to_dict()
    assert data["code"] == "MISSING_CAPABILITY"
    assert data["suggestions"] and data["related_ids"]["shot_id"] == "sht_1"
    assert err.status_code == 400


def test_not_found_helper_maps_to_404() -> None:
    err = not_found("Shot", "sht_x")
    assert err.status_code == 404
    assert err.suggestions


# --- ID ---


def test_new_id_prefix_roundtrip() -> None:
    ident = new_id("shot")
    assert ident.startswith("sht_")
    assert kind_of(ident) == "shot"


def test_new_id_rejects_unregistered_kind() -> None:
    with pytest.raises(ValueError, match="未登记"):
        new_id("banana")


def test_new_id_is_sortable_by_time() -> None:
    ids = [new_id("job") for _ in range(20)]
    assert ids == sorted(ids)


# --- 事件总线 ---


async def test_bus_delivers_to_subscriber() -> None:
    received = []

    async def listen() -> None:
        async for ev in bus.subscribe(project_id="prj_1"):
            received.append(ev)
            break

    task = asyncio.create_task(listen())
    await asyncio.sleep(0.01)
    bus.emit(Channel.JOB, "job.progress", {"progress": 0.5}, project_id="prj_1")
    await asyncio.wait_for(task, timeout=1)
    assert received[0].event == "job.progress"
    assert received[0].payload["progress"] == 0.5


async def test_bus_filters_by_project_and_channel() -> None:
    received = []

    async def listen() -> None:
        async for ev in bus.subscribe(project_id="prj_1", channels={Channel.QUEUE}):
            received.append(ev)

    task = asyncio.create_task(listen())
    await asyncio.sleep(0.01)
    bus.emit(Channel.JOB, "job.started", {}, project_id="prj_1")  # 频道不匹配
    bus.emit(Channel.QUEUE, "queue.state", {}, project_id="prj_2")  # 项目不匹配
    bus.emit(Channel.QUEUE, "queue.state", {"running": 1}, project_id="prj_1")
    await asyncio.sleep(0.05)
    task.cancel()
    assert [e.event for e in received] == ["queue.state"]
    assert received[0].payload["running"] == 1


async def test_bus_publish_is_non_blocking_when_no_subscriber() -> None:
    bus.emit(Channel.SYSTEM, "system.gpu", {"vram": 1})  # 不得抛异常


async def test_bus_publish_from_other_thread_wakes_subscriber() -> None:
    """FFmpeg 进度解析等跑在 executor 线程，跨线程投递必须能唤醒订阅者。"""
    import threading

    async def listen() -> Event:
        async for ev in bus.subscribe():
            return ev
        raise AssertionError("subscribe 意外结束")

    task = asyncio.create_task(listen())
    await asyncio.sleep(0.01)
    threading.Thread(
        target=lambda: bus.emit(Channel.SYSTEM, "system.gpu", {"vram": 42}), daemon=True
    ).start()
    ev = await asyncio.wait_for(task, timeout=2)
    assert ev.payload["vram"] == 42


def test_subscriber_is_hashable_so_bus_can_track_it() -> None:
    """_Subscriber 必须 eq=False，否则 dataclass 会置 __hash__=None 导致无法入 set。"""
    from app.events.bus import _Subscriber

    assert _Subscriber.__hash__ is not None


# --- 数据库 ---


async def test_db_wal_and_ping(db: Database) -> None:
    assert await db.ping() is True
    async with db.read() as s:
        from sqlalchemy import text

        mode = (await s.execute(text("PRAGMA journal_mode"))).scalar_one()
        fk = (await s.execute(text("PRAGMA foreign_keys"))).scalar_one()
    assert mode.lower() == "wal"
    assert fk == 1
    await db.close()


async def test_db_write_rolls_back_on_error(db: Database) -> None:
    from sqlalchemy import text

    async with db.write() as s:
        await s.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
    with pytest.raises(RuntimeError):
        async with db.write() as s:
            await s.execute(text("INSERT INTO t (v) VALUES ('a')"))
            raise RuntimeError("boom")
    async with db.read() as s:
        count = (await s.execute(text("SELECT COUNT(*) FROM t"))).scalar_one()
    assert count == 0
    await db.close()


# --- WebSocket ---


def test_ws_connect_and_receive_event(client: TestClient) -> None:
    with client.websocket_connect("/api/v1/ws?project_id=prj_1&channels=job") as socket:
        hello = socket.receive_json()
        assert hello["event"] == "system.connected"
        bus.emit(Channel.JOB, "job.completed", {"job_id": "job_1"}, project_id="prj_1")
        msg = socket.receive_json()
        assert msg["channel"] == "job"
        assert msg["payload"]["job_id"] == "job_1"
