"""队列在取消、关闭与重开时必须保持可解释的持久状态。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.ids import new_id
from app.persistence.models_gen import Job
from app.services.base import db_of
from app.services.generation import generation
from app.services.projects import projects


async def _project(tmp_path: Path) -> str:
    project = await projects.create(str(tmp_path / "film"), "队列测试", 1920, 1080, 25, "frames")
    return project.id


async def _job(pid: str, status: str = "queued") -> str:
    job_id = new_id("job")
    async with db_of(pid).write() as session:
        session.add(Job(id=job_id, status=status, kind="t2i", params_json="{}"))
    return job_id


async def _status(pid: str, job_id: str) -> str:
    async with db_of(pid).read() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        return row.status


async def _wait_status(pid: str, job_id: str, expected: str) -> None:
    for _ in range(100):
        if await _status(pid, job_id) == expected:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"任务没有进入 {expected} 状态")


@pytest.mark.asyncio
async def test_cancel_waits_for_old_run_before_immediate_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = await _project(tmp_path)
    job_id = await _job(pid)
    stopped = asyncio.Event()
    calls = 0

    async def execute(_pid: str, _job: Job, _params: dict[str, Any]) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return {"id": "version-after-retry"}

    monkeypatch.setattr(generation, "_execute", execute)
    generation.ensure_pump(pid)
    await _wait_status(pid, job_id, "running")
    await generation.cancel(pid, job_id)
    assert stopped.is_set()
    assert await _status(pid, job_id) == "canceled"
    await generation.retry(pid, job_id)
    await _wait_status(pid, job_id, "done")
    assert calls == 2


@pytest.mark.asyncio
async def test_reopen_marks_interrupted_run_failed_and_resumes_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = await _project(tmp_path)
    interrupted_id = await _job(pid, "running")
    queued_id = await _job(pid)

    async def execute(_pid: str, _job: Job, _params: dict[str, Any]) -> dict[str, str]:
        return {"id": "version-after-reopen"}

    monkeypatch.setattr(generation, "_execute", execute)
    await projects.close(pid)
    await projects.open(str(tmp_path / "film"))
    await _wait_status(pid, queued_id, "done")
    assert await _status(pid, interrupted_id) == "failed"
    interrupted = next(
        row for row in await generation.list_jobs(pid) if row["id"] == interrupted_id
    )
    assert interrupted["error"]["suggestions"]


@pytest.mark.asyncio
async def test_late_provider_result_cannot_overwrite_canceled_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = await _project(tmp_path)
    job_id = await _job(pid)

    async def execute(_pid: str, _job: Job, _params: dict[str, Any]) -> dict[str, str]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # 第三方适配器在取消请求到达后才返回成功，也不能改写用户已确认的取消状态。
            return {"id": "late-version"}
        return {"id": "unexpected-version"}

    monkeypatch.setattr(generation, "_execute", execute)
    generation.ensure_pump(pid)
    await _wait_status(pid, job_id, "running")
    await generation.cancel(pid, job_id)
    assert await _status(pid, job_id) == "canceled"
