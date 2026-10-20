"""外部 AI 接口的真实请求验收：鉴权、隔离、重试、并发和文档。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.main import create_app
from app.services.access import access
from tests.conftest import PNG_1PX, error_of


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + key}


def issue(scopes=None, projects=None):  # noqa: ANN001, ANN201
    return access.issue("测试 AI", scopes or ["read", "write", "delete"], projects)


def create_shot(client, pid):  # noqa: ANN001, ANN201
    scene = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": "第一幕"}).json()
    shot = client.post(
        f"/api/v1/projects/{pid}/scenes/{scene['id']}/shots", json={"title": "镜头"}
    ).json()
    return scene, shot


def test_bootstrap_persists_and_never_discloses_key() -> None:
    with TestClient(create_app(), client=("127.0.0.1", 1), base_url="http://127.0.0.1") as client:
        raw = (settings.runtime_dir / "bootstrap-api-key.txt").read_text().strip()
        assert len(raw) > 40
        assert raw not in client.get("/api/v1/auth/keys").text
        first = client.get("/api/v1/auth/me", headers=bearer(raw)).json()
    with TestClient(create_app(), client=("127.0.0.1", 1), base_url="http://127.0.0.1") as client:
        assert (settings.runtime_dir / "bootstrap-api-key.txt").read_text().strip() == raw
        assert client.get("/api/v1/auth/me", headers=bearer(raw)).json() == first


def test_first_cli_start_creates_runtime_and_ipv6_endpoint(tmp_path, monkeypatch):
    from app.main import _write_endpoint

    monkeypatch.setattr(settings, "runtime_dir", tmp_path / "fresh" / "runtime")
    monkeypatch.setattr(settings, "host", "::1")
    _write_endpoint(8765)
    endpoint = json.loads((settings.runtime_dir / "endpoint.json").read_text())
    assert endpoint["base_url"] == "http://[::1]:8765/api/v1"
    assert "api_key" not in endpoint


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/projects",
        "/api/v1/settings",
        "/api/v1/fs/roots",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/api/v1/library",
    ],
)
def test_server_protects_old_and_new_routes(client, monkeypatch, path):  # noqa: ANN001, ANN201
    monkeypatch.setattr(settings, "access_mode", "server")
    response = client.get(path, headers={"Host": "127.0.0.1", "X-Forwarded-For": "127.0.0.1"})
    assert response.status_code == 401
    error_of(response)
    assert client.get("/api/v1/health").status_code == 200


def test_direct_external_address_and_invalid_key_never_exempt(client, monkeypatch):  # noqa: ANN001, ANN201
    assert client.get("/api/v1/projects").status_code == 200
    assert client.get("/api/v1/projects", headers=bearer("invalid")).status_code == 401
    assert client.get("/api/v1/projects", headers={"Host": "evil.test"}).status_code == 403
    monkeypatch.setattr(settings, "host", "0.0.0.0")
    assert client.get("/api/v1/projects").status_code == 401
    monkeypatch.setattr(settings, "host", "127.0.0.1")
    # 外网来源即使伪造 Host / X-Forwarded-For 也不能免鉴权。
    client._transport.client = ("203.0.113.9", 50000)
    assert (
        client.get("/api/v1/projects", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401
    )


def test_scopes_project_isolation_files_and_schema(client, pid, tmp_path):  # noqa: ANN001, ANN201
    other = client.post(
        "/api/v1/projects", json={"name": "其他项目", "dir": str(tmp_path / "other")}
    ).json()["id"]
    key = issue(["read"], [pid])
    headers = bearer(key["key"])
    listing = client.get("/api/v1/projects", headers=headers).json()
    assert [p["id"] for p in listing["items"]] == [pid]
    assert "dir" not in listing["items"][0]
    assert client.get(f"/api/v1/projects/{pid}", headers=headers).json()["dir"] == ""
    for url in (
        f"/api/v1/projects/{other}",
        f"/api/v1/projects/{other}/shots/xxx",
        f"/api/v1/projects/{other}/files/project.db",
    ):
        assert client.get(url, headers=headers).status_code == 403
    assert (
        client.post(
            f"/api/v1/projects/{pid}/characters", headers=headers, json={"name": "角色"}
        ).status_code
        == 403
    )
    assert client.get("/api/v1/settings", headers=headers).status_code == 403
    assert client.get("/api/v1/library/assets", headers=headers).status_code == 403
    ops = client.get("/api/v1/integration/operations", headers=headers).json()
    assert all(op["method"] == "GET" for op in ops if "/projects" in op["path"])
    schema = client.get("/api/v1/integration/openapi.json", headers=headers).json()
    assert "/api/v1/settings" not in schema["paths"]
    assert "post" not in schema["paths"]["/api/v1/projects"]


def test_crud_upload_frames_mentions_graph_search_and_delete(client, pid):  # noqa: ANN001, ANN201
    _, shot = create_shot(client, pid)
    headers = bearer(issue(projects=[pid])["key"])
    root = f"/api/v1/projects/{pid}"
    created = client.post(root + "/characters", headers=headers, json={"name": "女主角"})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    assert client.get(root + f"/characters/{cid}", headers=headers).json()["name"] == "女主角"
    assert (
        client.patch(
            root + f"/characters/{cid}", headers=headers, json={"name": "主角"}
        ).status_code
        == 200
    )
    page = client.get(
        root + "/resources?resource=characters&q=主角&limit=1", headers=headers
    ).json()
    assert page["total"] == 1 and len(page["items"]) == 1
    uploaded = client.post(
        root + "/assets/upload",
        headers=headers,
        files={"file": ("frame.png", PNG_1PX, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    aid = uploaded.json()["id"]
    assert client.get(root + f"/assets/{aid}/content", headers=headers).content == PNG_1PX
    assert (
        client.get(
            root + f"/assets/{aid}/content", headers={**headers, "Range": "bytes=0-7"}
        ).status_code
        == 206
    )
    assert client.get(root + "/files/project.db", headers=headers).status_code == 403
    assert (
        client.post(
            root + "/assets/register",
            headers=headers,
            json={"kind": "upload", "path": "project.db"},
        ).status_code
        == 403
    )
    prompt = f"@[图片1](asset:{aid})，人物向前行走"
    patch = client.patch(
        root + f"/shots/{shot['id']}",
        headers=headers,
        json={"prompt": prompt, "manual_prompt": True, "first_frame_asset_id": aid},
    )
    assert patch.status_code == 200, patch.text
    graph = client.get(root + f"/shots/{shot['id']}/graph", headers=headers)
    assert graph.status_code == 200, graph.text
    assert graph.json()["shot"]["first_frame_asset_id"] == aid
    assert graph.json()["shot"]["prompt"] == prompt
    assert client.delete(root + f"/characters/{cid}", headers=headers).status_code == 204
    assert client.get(root + f"/characters/{cid}", headers=headers).status_code == 404


def test_idempotency_etag_and_changed_request_conflict(client, pid):  # noqa: ANN001, ANN201
    _, shot = create_shot(client, pid)
    path = f"/api/v1/projects/{pid}/shots/{shot['id']}"
    old = client.get(path).headers["etag"]
    headers = {"If-Match": old, "Idempotency-Key": "change-1"}
    first = client.patch(path, json={"prompt": "新画面"}, headers=headers)
    assert first.status_code == 200
    assert first.headers["etag"] != old
    replay = client.patch(path, json={"prompt": "新画面"}, headers=headers)
    assert replay.status_code == 200 and replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == first.json()
    changed = client.patch(path, json={"prompt": "其他画面"}, headers=headers)
    assert changed.status_code == 409
    stale = client.patch(path, json={"prompt": "过期修改"}, headers={"If-Match": old})
    assert stale.status_code == 412
    assert client.get(path).json()["prompt"] == "新画面"
    audit = client.get("/api/v1/integration/audit").json()["items"]
    assert any(r["route"] == path and r["status"] == 200 for r in audit)


def test_idempotency_create_and_generate_no_duplicate(client, pid, monkeypatch):  # noqa: ANN001, ANN201
    _, shot = create_shot(client, pid)
    root = f"/api/v1/projects/{pid}"
    headers = {"Idempotency-Key": "create-scene"}
    one = client.post(root + "/scenes", json={"title": "不会重复的幕"}, headers=headers)
    two = client.post(root + "/scenes", json={"title": "不会重复的幕"}, headers=headers)
    assert one.json()["id"] == two.json()["id"]
    calls = []

    async def enqueue(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(args)
        return {"id": "job_once", "status": "pending"}

    from app.services.generation import generation

    monkeypatch.setattr(generation, "enqueue_shot", enqueue)
    path = root + f"/shots/{shot['id']}/generate"
    write_key = bearer(issue(["read", "write"], [pid])["key"])
    assert client.post(path, json={}, headers=write_key).status_code == 403
    gen_key = {**bearer(issue(["generate"], [pid])["key"]), "Idempotency-Key": "generate-once"}
    assert client.post(path, json={}, headers=gen_key).status_code == 201
    assert client.post(path, json={}, headers=gen_key).status_code == 201
    assert len(calls) == 1


def test_project_registry_reopen_archive_restore_and_safe_creation(
    client, pid, monkeypatch, tmp_path
):  # noqa: ANN001, ANN201
    assert client.post(f"/api/v1/projects/{pid}/close").status_code == 204
    assert client.get(f"/api/v1/projects/{pid}").status_code == 200
    assert client.patch(f"/api/v1/projects/{pid}", json={"name": "已重命名"}).status_code == 200
    assert client.delete(f"/api/v1/projects/{pid}").status_code == 204
    archived = client.get("/api/v1/projects?archived=true").json()["items"]
    assert archived[0]["id"] == pid
    assert (Path(archived[0]["dir"]) / "project.db").exists()
    assert client.get(f"/api/v1/projects/{pid}/scenes").status_code == 409
    assert client.patch(f"/api/v1/projects/{pid}", json={"archived": False}).status_code == 200
    assert client.get(f"/api/v1/projects/{pid}").json()["name"] == "已重命名"
    monkeypatch.setattr(settings, "projects_root", tmp_path / "managed")
    key = bearer(issue(["read", "write"])["key"])
    assert (
        client.post(
            "/api/v1/projects", json={"name": "越界", "dir": str(tmp_path / "outside")}, headers=key
        ).status_code
        == 403
    )
    created = client.post("/api/v1/projects", json={"name": "受控创建"}, headers=key)
    assert created.status_code == 201, created.text
    assert created.json()["dir"] == ""
    assert len(list((tmp_path / "managed").glob("*/project.db"))) == 1


def test_session_key_revocation_origin_and_no_query_key(client, monkeypatch):  # noqa: ANN001, ANN201
    from httpx import URL

    monkeypatch.setattr(settings, "access_mode", "server")
    client.base_url = URL("https://studio.test")
    key = issue(["read"])
    admin = (settings.runtime_dir / "bootstrap-api-key.txt").read_text().strip()
    assert (
        client.post(
            "/api/v1/auth/session",
            json={"key": key["key"]},
            headers={"Origin": "https://evil.test"},
        ).status_code
        == 403
    )
    login = client.post(
        "/api/v1/auth/session", json={"key": key["key"]}, headers={"Origin": "https://studio.test"}
    )
    assert login.status_code == 204
    assert "HttpOnly" in login.headers["set-cookie"] and "Secure" in login.headers["set-cookie"]
    assert client.get("/api/v1/auth/me").json()["id"] == key["id"]
    assert client.delete(f"/api/v1/auth/keys/{key['id']}", headers=bearer(admin)).status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401
    client.cookies.clear()
    assert client.get("/api/v1/projects?token=" + admin).status_code == 401
    assert client.get("/api/v1/projects", headers=bearer(key["key"])).status_code == 401
    assert (
        client.delete(
            "/api/v1/auth/keys/" + access.keys()[0]["id"], headers=bearer(admin)
        ).status_code
        == 403
    )


def test_websocket_requires_key_project_scope_and_filters_events(client, pid, monkeypatch):  # noqa: ANN001, ANN201
    from app.events.bus import Channel, bus

    key = issue(["read"], [pid])
    monkeypatch.setattr(settings, "access_mode", "server")
    with pytest.raises(WebSocketDisconnect) as missing:
        with client.websocket_connect("/api/v1/ws"):
            pass
    assert missing.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as unscoped:
        with client.websocket_connect("/api/v1/ws", headers=bearer(key["key"])):
            pass
    assert unscoped.value.code == 4403
    with client.websocket_connect(
        f"/api/v1/ws?project_id={pid}", headers=bearer(key["key"])
    ) as socket:
        assert socket.receive_json()["event"] == "system.connected"
        bus.emit(Channel.SYSTEM, "private.global", {"secret": "hidden"})
        bus.emit(Channel.SHOT, "shot.updated", {"id": "shot_ok"}, project_id=pid)
        event = socket.receive_json()
        assert event["event"] == "shot.updated" and event["project_id"] == pid


def test_openapi_has_permissions_security_and_stable_unique_operations(client):  # noqa: ANN001, ANN201
    schema = client.get("/openapi.json").json()
    operation_ids = []
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            operation_ids.append(operation["operationId"])
            assert "x-required-scopes" in operation
            if path == "/api/v1/projects/{pid}/shots/{shot_id}" and method == "patch":
                assert operation["x-required-scopes"] == ["write"]
                assert {p["name"] for p in operation["parameters"]} >= {
                    "If-Match",
                    "Idempotency-Key",
                }
    assert len(operation_ids) == len(set(operation_ids))
    assert (
        json.loads(json.dumps(schema))["components"]["securitySchemes"]["ApiKeyBearer"]["scheme"]
        == "bearer"
    )


def test_multipart_retry_ignores_boundary_and_preserves_file_identity(client, pid):
    path = f"/api/v1/projects/{pid}/assets/upload"
    headers = {"Idempotency-Key": "upload-once"}
    first = client.post(path, headers=headers, files={"file": ("same.png", PNG_1PX, "image/png")})
    second = client.post(path, headers=headers, files={"file": ("same.png", PNG_1PX, "image/png")})
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.headers["idempotency-replayed"] == "true"
    changed = client.post(
        path, headers=headers, files={"file": ("other.png", PNG_1PX, "image/png")}
    )
    assert changed.status_code == 409


def test_documented_python_workflow_runs_against_real_routes(client, pid, tmp_path):
    import importlib.util

    source = Path(__file__).resolve().parents[2] / "docs/examples/ai_workflow.py"
    spec = importlib.util.spec_from_file_location("ai_workflow", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    image = tmp_path / "reference.png"
    image.write_bytes(PNG_1PX)

    class WorkflowClient:
        def request(self, method, path, **kwargs):
            return client.request(method, "/api/v1/" + path, **kwargs)

        def get(self, path):
            return self.request("GET", path)

    result = module.run(WorkflowClient(), pid, image, False)
    assert result["project_id"] == pid
    graph = client.get(f"/api/v1/projects/{pid}/shots/{result['shot_id']}/graph").json()
    assert graph["shot"]["first_frame_asset_id"] == result["asset_id"]
    assert result["asset_id"] in graph["shot"]["prompt"]


def test_generate_key_cannot_use_internal_director_to_bypass_write(client, pid):
    key = bearer(issue(["generate"], [pid])["key"])
    response = client.post(f"/api/v1/projects/{pid}/director/apply", headers=key, json={"ops": []})
    assert response.status_code == 403


def test_archive_patch_and_boolean_force_cannot_bypass_permissions(client, pid):
    write_key = bearer(issue(["read", "write"], [pid])["key"])
    root = f"/api/v1/projects/{pid}"
    assert client.patch(root, headers=write_key, json={"archived": True}).status_code == 403
    delete_key = bearer(issue(["read", "delete"], [pid])["key"])
    for value in ("true", "1", "on", "yes", "y", "t"):
        assert (
            client.delete(root + f"/assets/unused?force={value}", headers=delete_key).status_code
            == 403
        )


def test_login_limit_and_unknown_routes_keep_error_contract(client):
    oversized = client.post("/api/v1/auth/session", json={"key": "x" * 5000})
    assert oversized.status_code == 413
    error_of(oversized)
    assert (
        client.post(
            "/api/v1/auth/session",
            json={"key": "invalid"},
            headers={"Idempotency-Key": "not-for-login"},
        ).status_code
        == 403
    )
    unknown = client.get("/api/v1/not-an-endpoint")
    assert unknown.status_code == 404
    error_of(unknown)


def test_library_download_and_force_still_require_their_own_scope(client, library):
    uploaded = client.post(
        "/api/v1/library/assets/upload", files={"file": ("library.png", PNG_1PX, "image/png")}
    )
    assert uploaded.status_code == 201, uploaded.text
    aid = uploaded.json()["id"]
    key = bearer(issue(["read", "delete", "library"])["key"])
    assert client.get(f"/api/v1/library/assets/{aid}/content", headers=key).content == PNG_1PX
    assert client.delete(f"/api/v1/library/assets/{aid}?force=on", headers=key).status_code == 403
