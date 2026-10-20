"""所有 HTTP / WebSocket 的统一边界；不信任 Host 或转发头来决定是否免 Key。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import tempfile
import time
from urllib.parse import urlsplit

from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import compile_path

from app.core.access_policy import permissions
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.services.access import (
    COOKIE,
    LOCAL,
    access,
    denied,
    expected_revision,
    loopback,
    server_mode,
)

PUBLIC = {"/api/v1/health", "/api/v1/auth/status", "/api/v1/auth/session"}


def unauthenticated() -> AppError:
    return AppError(
        ErrorCode.UNAUTHORIZED,
        "需要有效的访问凭证",
        "缺少或无效的 API Key / 登录会话。",
        ["在 Authorization: Bearer 请求头提供 Key，或在网页中登录"],
    )


def principal_for(connection: HTTPConnection):  # noqa: ANN201
    header = connection.headers.get("authorization", "")
    if header:
        scheme, _, value = header.partition(" ")
        principal = access.authenticate(value) if scheme.lower() == "bearer" else None
        if principal is None:
            raise unauthenticated()
        return principal
    cookie = connection.cookies.get(COOKIE, "")
    if cookie:
        principal = access.authenticate(session=cookie)
        if principal is None:
            raise unauthenticated()
        return principal
    if not server_mode() and connection.client and loopback(connection.client.host):
        return LOCAL
    raise unauthenticated()


def check_origin(connection: HTTPConnection) -> None:
    origin = connection.headers.get("origin")
    if not origin:
        return  # 非浏览器客户端没有 Origin；鉴权仍执行。
    parsed = urlsplit(origin)
    allowed = {f"{connection.url.scheme}://{connection.headers.get('host', '')}"}
    allowed.update(settings.allowed_origins)
    if not server_mode():
        allowed |= {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
        if settings.dev_cors:
            allowed |= {"http://localhost:5173", "http://127.0.0.1:5173"}
    # WS 的页面 Origin 使用 http(s)，而连接本身使用 ws(s)。
    allowed |= {v.replace("ws://", "http://").replace("wss://", "https://") for v in tuple(allowed)}
    if origin not in allowed or not parsed.scheme:
        raise denied("浏览器请求来源不在允许范围内。")


class AccessMiddleware:
    def __init__(self, app, router) -> None:  # noqa: ANN001
        self.app = app
        self.router = router
        self.locks: dict[str, asyncio.Lock] = {}
        self.routes = list(self.route_specs(router.routes))

    def route_specs(self, routes, prefix=""):  # noqa: ANN001, ANN201
        # FastAPI 新版保留 include_router 分支，旧版直接展开；兼容两种路由树。
        for route in routes:
            branch = getattr(route, "original_router", None)
            if branch is not None:
                yield from self.route_specs(branch.routes, prefix + route.include_context.prefix)
            elif hasattr(route, "path"):
                path = prefix + route.path
                regex, _, _ = compile_path(path)
                yield path, regex, getattr(route, "methods", None)

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        path = scope["path"]
        method = scope.get("method", "GET")
        route_path = path
        pid = None
        try:
            check_origin(connection)
            # 回环免 Key 只接受回环 Host，防止浏览器 DNS rebinding。
            if not server_mode() and connection.client and loopback(connection.client.host):
                if not loopback(connection.url.hostname or "") and connection.url.hostname not in {
                    "localhost",
                    "127.0.0.1",
                    "::1",
                    "tauri.localhost",
                }:
                    if (
                        not connection.headers.get("authorization")
                        and not settings.require_handshake
                    ):
                        raise denied("本机免 Key 请求必须使用回环地址。")
            if path in PUBLIC and scope["type"] == "http":
                if path == "/api/v1/auth/session":
                    if connection.headers.get("idempotency-key"):
                        raise denied("会话接口不支持 Idempotency-Key。")
                    consumed = 0
                    parts = []
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        consumed += len(message.get("body", b""))
                        if consumed > 4096:
                            raise AppError(
                                ErrorCode.VALIDATION_ERROR,
                                "登录请求过大",
                                "登录请求不能超过 4 KB。",
                                ["只提交访问 Key"],
                                status_code=413,
                            )
                        parts.append(message)
                        if not message.get("more_body"):
                            break

                    async def small_body():  # noqa: ANN202
                        return parts.pop(0) if parts else await receive()

                    await self.app(scope, small_body, send)
                else:
                    await self.app(scope, receive, send)
                return
            principal = principal_for(connection)
            scope.setdefault("state", {})["principal"] = principal
            # 保留桌面壳握手；Bearer 调用无须拥有壳内的一次性 Token。
            if settings.require_handshake and principal.id == "local":
                token = connection.headers.get("x-aivs-token")
                if scope["type"] == "websocket" or (method == "GET" and "/files/" in path):
                    token = token or connection.query_params.get("token")
                if not settings.handshake_token or not secrets.compare_digest(
                    token or "", settings.handshake_token
                ):
                    raise unauthenticated()
            for template, regex, methods in self.routes:
                if (scope["type"] == "websocket") != (methods is None):
                    continue
                match = regex.fullmatch(path)
                if match and (methods is None or method in methods):
                    route_path = template
                    pid = match.groupdict().get("pid")
                    break
            for permission in permissions(route_path, method):
                principal.require(permission, pid)
            if connection.query_params.get("force", "false").strip().lower() not in {
                "false",
                "0",
                "off",
                "no",
                "n",
                "f",
            }:
                principal.require("admin", pid)
            if pid is not None:
                principal.require(
                    "read" if method == "GET" else permissions(route_path, method)[-1], pid
                )
                if not (route_path == "/api/v1/projects/{pid}" and method in {"PATCH", "DELETE"}):
                    await self.ensure_project(pid)
            if scope["type"] == "websocket":
                principal.require("read")
                subscribed_pid = connection.query_params.get("project_id")
                if subscribed_pid:
                    principal.require("read", subscribed_pid)
                    await self.ensure_project(subscribed_pid)
                elif principal.project_ids is not None:
                    raise denied("受限 Key 必须指定 project_id 订阅项目事件。")
                await self.app(scope, receive, send)
                return
            request_id = secrets.token_hex(12)
            scope["state"]["request_id"] = request_id
            mutation = method not in {"GET", "HEAD", "OPTIONS"}
            lock = self.locks.setdefault(pid or "__global__", asyncio.Lock())
            if mutation:
                async with lock:
                    await self.dispatch(
                        scope, receive, send, principal, pid, route_path, request_id
                    )
            else:
                await self.dispatch(scope, receive, send, principal, pid, route_path, request_id)
        except AppError as exc:
            if scope["type"] == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 4401 if exc.status_code == 401 else 4403,
                        "reason": exc.title,
                    }
                )
            else:
                response_headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else {}
                await JSONResponse(
                    {"error": exc.to_dict()}, status_code=exc.status_code, headers=response_headers
                )(scope, receive, send)

    async def ensure_project(self, pid: str) -> None:
        from app.services.projects import projects

        if any(p.id == pid for p in projects.opened):
            return
        rows = access.registered(pid)
        if not rows:
            return  # 由业务层给出统一的不存在错误。
        if rows[0]["archived"]:
            raise AppError(
                ErrorCode.CONFLICT,
                "项目已归档",
                "先恢复项目再修改其内容。",
                ["PATCH 项目并传 archived=false"],
            )
        await projects.open(rows[0]["dir"])

    async def dispatch(self, scope, receive, send, principal, pid, route, request_id) -> None:  # noqa: ANN001
        headers = Headers(scope=scope)
        method = scope["method"]
        mutation = method not in {"GET", "HEAD", "OPTIONS"}
        revision = access.revision(pid) if pid else None
        condition = headers.get("if-match") if mutation else None

        def check_condition() -> None:
            if condition is not None and (not pid or condition != revision):
                spool.close()
                raise AppError(
                    ErrorCode.PRECONDITION_FAILED,
                    "项目已发生变化",
                    "If-Match 与当前项目版本不一致。",
                    ["重新读取资源和 ETag，再合并修改"],
                )

        idem = headers.get("idempotency-key") if mutation else None
        if idem and (len(idem) > 128 or not idem.isascii() or not idem.strip()):
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "重试标识无效",
                "Idempotency-Key 需为 1–128 个 ASCII 字符。",
                ["为一次逻辑操作生成唯一标识"],
            )
        # 会话和密钥接口绝不缓存含凭证的应答。
        if idem and ("/auth/" in route or not route.startswith("/api/")):
            raise denied("此接口不支持 Idempotency-Key。")
        spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        fingerprint = hashlib.sha256(
            (method + scope["path"] + "?" + scope.get("query_string", b"").decode()).encode()
        )
        total = 0
        if mutation:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    spool.close()
                    return
                body = message.get("body", b"")
                total += len(body)
                if total > settings.upload_limit_mb * 1024 * 1024:
                    spool.close()
                    raise AppError(
                        ErrorCode.VALIDATION_ERROR,
                        "上传超过限制",
                        f"请求不能超过 {settings.upload_limit_mb} MB。",
                        ["缩小文件或由管理员调整上传限制"],
                        status_code=413,
                    )
                spool.write(body)
                fingerprint.update(body)
                if not message.get("more_body"):
                    break
            spool.seek(0)
        if idem and headers.get("content-type", "").startswith("multipart/form-data"):
            # 浏览器和 SDK 重试会生成不同 boundary；按字段和文件内容识别同一次上传。
            async def chunks():  # noqa: ANN202
                while data := spool.read(65536):
                    yield data

            canonical = hashlib.sha256(
                (method + scope["path"] + "?" + scope.get("query_string", b"").decode()).encode()
            )
            try:
                form = await MultiPartParser(headers, chunks()).parse()
                try:
                    for name, value in form.multi_items():
                        canonical.update(json.dumps([name], ensure_ascii=False).encode())
                        if isinstance(value, UploadFile):
                            content_hash = hashlib.sha256()
                            while data := await value.read(65536):
                                content_hash.update(data)
                            canonical.update(
                                json.dumps(
                                    [value.filename, value.content_type, content_hash.hexdigest()],
                                    ensure_ascii=False,
                                ).encode()
                            )
                        else:
                            canonical.update(json.dumps([value], ensure_ascii=False).encode())
                finally:
                    await form.close()
            except (MultiPartException, ValueError) as exc:
                spool.close()
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "上传格式无效",
                    str(exc),
                    ["使用 multipart/form-data 上传文件"],
                ) from exc
            fingerprint = canonical
            spool.seek(0)
        cached = None
        if idem:
            with access.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                cached = db.execute(
                    "SELECT * FROM idempotency WHERE key_id=? AND key=?", (principal.id, idem)
                ).fetchone()
                if cached:
                    if cached["fingerprint"] != fingerprint.hexdigest():
                        spool.close()
                        raise AppError(
                            ErrorCode.CONFLICT,
                            "重试标识已被使用",
                            "同一个标识对应的请求内容不同。",
                            ["相同操作保持请求一致；新操作使用新标识"],
                        )
                    if cached["status"] is None:
                        spool.close()
                        raise AppError(
                            ErrorCode.CONFLICT,
                            "操作结果尚未确定",
                            "请求仍在执行，或服务曾在执行期间中断。不会自动重复执行。",
                            ["查询相关资源或任务，确认结果后再决定是否发起新操作"],
                        )
                else:
                    check_condition()
                    db.execute(
                        "INSERT INTO idempotency(key_id,key,fingerprint,created_at) VALUES(?,?,?,?)",
                        (principal.id, idem, fingerprint.hexdigest(), time.time()),
                    )
        if cached:
            spool.close()
            await send(
                {
                    "type": "http.response.start",
                    "status": cached["status"],
                    "headers": [(k.encode(), v.encode()) for k, v in json.loads(cached["headers"])]
                    + [(b"idempotency-replayed", b"true")],
                }
            )
            await send({"type": "http.response.body", "body": bytes(cached["body"])})
            return
        if not idem:
            check_condition()
        sent_body = False

        async def replay():  # noqa: ANN202
            nonlocal sent_body
            if mutation and not sent_body:
                chunk = spool.read(65536)
                more = spool.tell() < total
                if not more:
                    sent_body = True
                return {"type": "http.request", "body": chunk, "more_body": more}
            return await receive()

        messages = []
        status = 500

        async def capture(message):  # noqa: ANN001, ANN202
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                extra = [(b"x-request-id", request_id.encode())]
                tag = access.revision(pid) if mutation and pid else revision
                if tag:
                    extra.append((b"etag", tag.encode()))
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            if idem:
                messages.append(message)
            else:
                await send(message)

        context = expected_revision.set((pid, condition) if pid and condition else None)
        try:
            await self.app(scope, replay if mutation else receive, capture)
            if mutation and pid and 200 <= status < 300:
                from app.events.bus import Channel, bus

                if headers.get("authorization") or not headers.get("origin"):
                    bus.emit(
                        Channel.SYSTEM,
                        "project.external_changed",
                        {"method": method, "path": scope["path"], "request_id": request_id},
                        project_id=pid,
                    )
            if idem:
                start = next(m for m in messages if m["type"] == "http.response.start")
                payload = b"".join(
                    m.get("body", b"") for m in messages if m["type"] == "http.response.body"
                )
                with access.connect() as db:
                    db.execute(
                        "UPDATE idempotency SET status=?,body=?,headers=? WHERE key_id=? AND key=?",
                        (
                            status,
                            payload,
                            json.dumps(
                                [
                                    (k.decode(), v.decode())
                                    for k, v in start["headers"]
                                    if k.lower() != b"set-cookie"
                                ]
                            ),
                            principal.id,
                            idem,
                        ),
                    )
                for message in messages:
                    await send(message)
        finally:
            expected_revision.reset(context)
            spool.close()
            if mutation:
                access.audit(principal.id, pid, method, scope["path"], status, request_id)
