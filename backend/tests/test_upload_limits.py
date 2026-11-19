"""大文件上传的限制、错误响应及有界内存回归。"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from app.core.access_middleware import AccessMiddleware
from app.core.config import Settings, settings
from app.core.errors import AppError
from app.services.assets import sniff_size, stage_upload
from tests.conftest import PNG_1PX, error_of


def test_default_limit_supports_long_videos():
    assert Settings.model_fields["upload_limit_mb"].default == 10240


def test_upload_limit_and_deduplication(client, pid, monkeypatch, project_dir):
    monkeypatch.setattr(settings, "upload_limit_mb", 1)
    assert client.get("/api/v1/integration/capabilities").json()["upload_limit_mb"] == 1
    path = f"/api/v1/projects/{pid}/assets/upload"
    data = b"video" * 20000
    first = client.post(path, files={"file": ("source.mp4", data)})
    second = client.post(path, files={"file": ("source.mp4", data)})
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert (project_dir / first.json()["path"]).read_bytes() == data
    assert first.json()["sha1"] == hashlib.sha1(data).hexdigest()
    rejected = client.post(path, files={"file": ("too-large.mp4", b"x" * 1024 * 1024)})
    assert rejected.status_code == 413  # 表单开销也计入总量。
    assert error_of(rejected)["related_ids"]["upload_limit_mb"] == 1
    assert len(client.get(f"/api/v1/projects/{pid}/assets").json()) == 1
    assert not list(project_dir.rglob("*.upload"))


async def test_chunked_oversize_body_is_drained_before_error(monkeypatch):
    monkeypatch.setattr(settings, "upload_limit_mb", 1)
    bodies = [b"x" * (600 * 1024), b"y" * (600 * 1024), b"tail"]
    received = 0

    async def receive():
        nonlocal received
        body = bodies[received]
        received += 1
        return {"type": "http.request", "body": body, "more_body": received < len(bodies)}

    async def never_called(*args):
        pytest.fail("超限请求不应进入业务接口")

    middleware = AccessMiddleware(never_called, type("Router", (), {"routes": []})())
    scope = {"type": "http", "method": "POST", "path": "/upload", "headers": []}
    with pytest.raises(AppError) as error:
        await middleware.dispatch(scope, receive, never_called, None, None, "/upload", "test")
    assert received == len(bodies)
    assert error.value.status_code == 413


def test_upload_copy_and_image_probe_never_read_whole_file(tmp_path, monkeypatch):
    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    data = PNG_1PX + b"x" * (3 * 1024 * 1024)
    staged, digest = stage_upload(tmp_path, BoundedStream(data), "source.png")
    assert staged.read_bytes() == data
    assert digest == hashlib.sha1(data).hexdigest()

    def forbidden_read(*args):
        pytest.fail("读取图片头不能把整个视频读入内存")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    assert sniff_size(staged) == (1, 1)


def test_empty_and_interrupted_uploads_remove_staged_files(tmp_path):
    with pytest.raises(AppError, match="文件是空的"):
        stage_upload(tmp_path, io.BytesIO(), "empty.mp4")

    class BrokenStream(io.BytesIO):
        def read(self, size=-1):
            raise OSError(28, "disk full")

    with pytest.raises(AppError) as error:
        stage_upload(tmp_path, BrokenStream(b"video"), "source.mp4")
    assert error.value.code == "DISK_FULL"
    assert not list(tmp_path.glob("*.upload"))
