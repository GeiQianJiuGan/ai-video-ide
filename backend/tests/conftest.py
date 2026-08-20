"""pytest 公共 fixture。

工程是应用级状态（recent.json + 已打开的库 + 进程内队列），每个测试都必须拿到
干净的运行目录，否则测试之间会互相看见对方的工程。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import create_app
from app.persistence.db import Database
from app.services.generation import generation
from app.services.library import library as library_service
from app.services.projects import projects


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "project.db")
    await database.create_all()
    return database


@pytest.fixture(autouse=True)
async def clean_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setattr(settings, "runtime_dir", tmp_path / "runtime")
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    await projects.close_all()
    # 素材库同样是应用级状态：进程里只有一个，不关掉会被下一个测试看见
    await library_service.shutdown()
    yield
    for pid in list(generation._pumps):  # 别把调度任务泄漏到下一个测试
        await generation.stop_pump(pid)
    generation._paused.clear()
    generation._cancelled.clear()
    await projects.close_all()
    await library_service.shutdown()


@pytest.fixture
def project(client: TestClient, tmp_path: Path) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/projects",
        json={
            "dir": str(tmp_path / "film"),
            "name": "测试片",
            "width": 1920,
            "height": 1080,
            "fps": 25,
            "duration_unit": "frames",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def pid(project: dict[str, Any]) -> str:
    return str(project["id"])


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "film"


@pytest.fixture
def library_dir(tmp_path: Path) -> Path:
    return tmp_path / "素材库"


@pytest.fixture
def library(client: TestClient, library_dir: Path) -> dict[str, Any]:
    """配置一个空目录当素材库。素材库是应用级的，不属于任何工程。"""
    resp = client.post("/api/v1/library/configure", json={"dir": str(library_dir)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    return dict(body["library"])


def error_of(resp: Any) -> dict[str, Any]:
    """所有失败都必须是同一种形状：code/title/detail/suggestions 一个都不能少。"""
    body = resp.json()
    assert "error" in body, f"不是结构化错误：{body}"
    err = body["error"]
    assert err["title"], "错误缺少 title"
    assert err["detail"], f"错误缺少 detail：{err}"
    assert err["suggestions"], "错误没有给出任何修复建议，违反「绝不静默失败」"
    return dict(err)


#: 1×1 透明 PNG，用来当上传/参考图/成片素材的替身。
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

#: 一份最小可用的 ComfyUI API 图：文本节点 + 两个图片输入 + 采样器。
#: LoadImage 不在 BUILTIN_HINT 里，因此会被当成需要探测的自定义节点。
GRAPH: dict[str, Any] = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 20, "model": ["4", 0]}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
    "10": {"class_type": "LoadImage", "inputs": {"image": "first.png"}},
    "11": {"class_type": "LoadImage", "inputs": {"image": "last.png"}},
}
#: 各能力的完整绑定表，绑定齐了才可能校验通过。
BINDINGS: dict[str, dict[str, str]] = {
    "text2image": {"prompt": "6.text", "seed": "3.seed"},
    "image2video": {"prompt": "6.text", "reference_image": "10.image"},
    "first_last_frame": {"first_frame": "10.image", "last_frame": "11.image"},
    "upscale": {"source_image": "10.image"},
}


def import_workflow(
    client: TestClient,
    pid: str,
    capability: str = "image2video",
    *,
    name: str | None = None,
    bindings: dict[str, str] | None = None,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/projects/{pid}/workflows",
        json={
            "name": name or f"{capability} 默认流程",
            "capability": capability,
            "api_json": json.dumps(graph if graph is not None else GRAPH),
            "bindings": BINDINGS[capability] if bindings is None else bindings,
        },
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def ready_workflow(client: TestClient, pid: str, capability: str = "image2video") -> dict[str, Any]:
    """导入并校验一条工作流。probe=false：本地绑定校验不需要 ComfyUI 在线。"""
    row = import_workflow(client, pid, capability)
    resp = client.post(f"/api/v1/projects/{pid}/workflows/{row['id']}/validate?probe=false")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    return dict(client.get(f"/api/v1/projects/{pid}/workflows/{row['id']}").json())


def upload_png(client: TestClient, pid: str, kind: str = "upload", name: str = "ref.png") -> str:
    """上传一张 1×1 PNG，返回 asset_id。

    尾部缀上文件名，让不同用途的图内容不同——否则 sha1 去重会把它们合成一个资产，
    测试就看不出「角色表」和「地点参考」分别落在哪个目录。
    """
    resp = client.post(
        f"/api/v1/projects/{pid}/assets/upload",
        data={"kind": kind},
        files={"file": (name, PNG_1PX + name.encode(), "image/png")},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def lib_png(
    client: TestClient, kind: str = "upload", name: str = "lib.png", title: str | None = None
) -> str:
    """往素材库上传一张 1×1 PNG，返回库内 asset_id。内容缀上文件名以避开 sha1 去重。"""
    data: dict[str, str] = {"kind": kind}
    if title is not None:
        data["title"] = title
    resp = client.post(
        "/api/v1/library/assets/upload",
        data=data,
        files={"file": (name, PNG_1PX + name.encode(), "image/png")},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])
