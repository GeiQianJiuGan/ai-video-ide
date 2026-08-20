"""静态文件读取（Phase 0）。

这个端点是缩略图与视频预览的唯一通道，所以两件事必须钉死：
  1. 越界路径一律拒绝——工程目录之外的文件绝不能被读出来；
  2. 缺文件是结构化 404，不是空响应，否则前端只能看到一张裂图。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.conftest import PNG_1PX, error_of, upload_png


def _asset(client: TestClient, pid: str, asset_id: str) -> dict:
    rows = client.get(f"/api/v1/projects/{pid}/assets").json()
    return next(r for r in rows if r["id"] == asset_id)


def test_serves_an_uploaded_asset_with_its_real_bytes(client: TestClient, pid: str) -> None:
    asset_id = upload_png(client, pid, "character_sheet", "sheet.png")
    rel = _asset(client, pid, asset_id)["path"]

    resp = client.get(f"/api/v1/projects/{pid}/files/{rel}")

    assert resp.status_code == 200, resp.text
    assert resp.content == PNG_1PX + b"sheet.png"
    assert resp.headers["content-type"].startswith("image/png")


def test_range_request_comes_back_as_206(client: TestClient, pid: str) -> None:
    """视频拖进度条依赖 Range；Starlette 的 FileResponse 原生支持，这里守住它不回退。"""
    asset_id = upload_png(client, pid, "upload", "clip.png")
    rel = _asset(client, pid, asset_id)["path"]

    resp = client.get(f"/api/v1/projects/{pid}/files/{rel}", headers={"Range": "bytes=0-7"})

    assert resp.status_code == 206, resp.text
    assert resp.content == (PNG_1PX + b"clip.png")[:8]
    assert resp.headers["content-range"].startswith("bytes 0-7/")


def test_missing_file_says_what_is_gone_and_how_to_recover(client: TestClient, pid: str) -> None:
    resp = client.get(f"/api/v1/projects/{pid}/files/assets/uploads/never-existed.png")

    assert resp.status_code == 404
    err = error_of(resp)
    assert err["code"] == "NOT_FOUND"
    assert "never-existed.png" in err["detail"]


def test_traversal_out_of_the_project_is_refused(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("不该被读到", encoding="utf-8")

    # 用 %2e%2e 而不是字面 ".."：httpx 会在发请求前把字面 ".." 折叠掉，
    # 那样根本到不了端点，测的就不是我们的守卫了。
    resp = client.get(f"/api/v1/projects/{pid}/files/%2e%2e/secret.txt")

    assert resp.status_code == 422, resp.text
    err = error_of(resp)
    assert err["code"] == "VALIDATION_ERROR"
    assert "越界" in err["title"]


def test_absolute_path_cannot_escape_either(client: TestClient, pid: str) -> None:
    """拼接绝对路径是另一条越界方式：root / "C:/x" 在 Windows 上会直接跳到 C:/x。"""
    resp = client.get(f"/api/v1/projects/{pid}/files/{Path.home().as_posix()}/.bashrc")

    assert resp.status_code in (422, 404), resp.text
    assert error_of(resp)["code"] in ("VALIDATION_ERROR", "NOT_FOUND")


def test_directory_is_not_a_file(client: TestClient, pid: str) -> None:
    resp = client.get(f"/api/v1/projects/{pid}/files/assets")

    assert resp.status_code == 422, resp.text
    assert "不是一个文件" in error_of(resp)["title"]


def test_unopened_project_is_a_structured_404(client: TestClient) -> None:
    resp = client.get("/api/v1/projects/prj_nope/files/assets/x.png")

    assert resp.status_code == 404
    error_of(resp)


def test_img_src_may_carry_the_token_in_the_query(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """<img src> 带不了 header，所以文件读取额外接受 ?token=；其它端点仍然只认 header。"""
    asset_id = upload_png(client, pid, "character_sheet", "guard.png")
    rel = _asset(client, pid, asset_id)["path"]
    monkeypatch.setattr(settings, "require_handshake", True)
    monkeypatch.setattr(settings, "handshake_token", "tok-1")
    url = f"/api/v1/projects/{pid}/files/{rel}"

    assert client.get(url).status_code == 401
    assert client.get(f"{url}?token=wrong").status_code == 401
    assert client.get(f"{url}?token=tok-1").status_code == 200
    assert client.get(url, headers={"X-AIVS-Token": "tok-1"}).status_code == 200
    # 放宽只针对文件读取：普通接口不能用 query token 绕过
    assert client.get(f"/api/v1/projects/{pid}/assets?token=tok-1").status_code == 401
