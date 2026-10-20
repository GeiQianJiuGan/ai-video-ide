"""外部 AI 的发现、访问凭证、查询与关系快照接口。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.core.access_middleware import principal_for
from app.core.access_policy import permissions
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.persistence.models_cast import Appearance, Character
from app.persistence.models_gen import Job
from app.persistence.models_story import Scene, Shot
from app.persistence.models_world import Asset, Location, LocationVariant, Prop
from app.services.access import COOKIE, SCOPES, access, server_mode
from app.services.base import as_dict, db_of, fetch
from app.services.context import context
from app.services.generation import generation
from app.services.projects import projects
from app.services.story import story

router = APIRouter(tags=["integration"])


class SessionBody(BaseModel):
    key: str = Field(min_length=1, max_length=256)


class KeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    project_ids: list[str] | None = None


class KeyOut(BaseModel):
    id: str
    name: str
    scopes: list[str]
    project_ids: list[str] | None
    key: str


class Page(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


@router.get("/auth/status")
async def auth_status(request: Request) -> dict:
    """登录页探测；不返回密钥、项目、配置或服务器路径。"""
    try:
        principal_for(request)
        authenticated = True
    except AppError:
        authenticated = False
    return {"required": server_mode(), "authenticated": authenticated}


@router.post("/auth/session", status_code=204)
async def login(body: SessionBody, response: Response) -> None:
    principal = access.authenticate(body.key)
    if principal is None:
        from app.core.access_middleware import unauthenticated

        raise unauthenticated()
    response.set_cookie(
        COOKIE,
        access.session(principal),
        max_age=settings.session_seconds,
        httponly=True,
        secure=server_mode() and settings.session_secure_cookie,
        samesite="strict",
        path="/",
    )


@router.delete("/auth/session", status_code=204)
async def logout(request: Request, response: Response) -> None:
    access.logout(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/")


@router.get("/auth/me")
async def identity(request: Request) -> dict:
    return asdict(request.state.principal)


@router.get("/auth/keys")
async def list_keys() -> list[dict]:
    return access.keys()


@router.post("/auth/keys", response_model=KeyOut, status_code=201)
async def issue_key(body: KeyBody) -> dict:
    """完整 Key 仅在创建响应中返回一次。轮换时创建替代 Key，再撤销旧 Key。"""
    return access.issue(body.name, body.scopes, body.project_ids)


@router.delete("/auth/keys/{key_id}", status_code=204)
async def revoke_key(key_id: str) -> None:
    access.revoke(key_id)


@router.get("/integration/capabilities")
async def integration_capabilities(request: Request) -> dict:
    return {
        "api_version": "v1",
        "auth": "bearer",
        "server_mode": server_mode(),
        "identity": asdict(request.state.principal),
        "scopes": sorted(SCOPES),
        "openapi": "/api/v1/integration/openapi.json",
        "operations": "/api/v1/integration/operations",
        "features": [
            "project_registry",
            "resource_search",
            "shot_graph",
            "idempotency",
            "project_etag",
            "scoped_events",
        ],
        "upload_limit_mb": settings.upload_limit_mb,
    }


@router.get("/integration/operations")
async def operations(request: Request) -> list[dict]:
    """返回当前凭证可调用的方法与所需权限；参数 schema 见 OpenAPI。"""
    principal = request.state.principal
    result = []
    for path, item in request.app.openapi()["paths"].items():
        for method, operation in item.items():
            if method.upper() not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
                continue
            scopes = permissions(path, method.upper())
            if all(principal.allows(s) for s in scopes):
                result.append(
                    {
                        "method": method.upper(),
                        "path": path,
                        "operation_id": operation["operationId"],
                        "summary": operation.get("summary"),
                        "scopes": scopes,
                    }
                )
    return result


@router.get("/integration/openapi.json")
async def integration_schema(request: Request) -> dict:
    from copy import deepcopy

    schema = deepcopy(request.app.openapi())
    principal = request.state.principal
    for path in list(schema["paths"]):
        item = schema["paths"][path]
        for method in list(item):
            if not all(principal.allows(s) for s in permissions(path, method.upper())):
                del item[method]
        if not item:
            del schema["paths"][path]
    return schema


@router.get("/integration/audit", response_model=Page)
async def audit_log(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    project_id: str | None = None,
) -> dict:
    with access.connect() as db:
        where = " WHERE project_id=?" if project_id else ""
        values = (project_id,) if project_id else ()
        total = db.execute("SELECT COUNT(*) FROM audit" + where, values).fetchone()[0]
        rows = db.execute(
            "SELECT * FROM audit" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


RESOURCES = {
    "characters": Character,
    "appearances": Appearance,
    "locations": Location,
    "variants": LocationVariant,
    "props": Prop,
    "assets": Asset,
    "scenes": Scene,
    "shots": Shot,
    "jobs": Job,
}
ResourceKind = Literal[
    "characters",
    "appearances",
    "locations",
    "variants",
    "props",
    "assets",
    "scenes",
    "shots",
    "jobs",
]


@router.get("/projects/{pid}/resources", response_model=Page)
async def resource_search(
    pid: str,
    resource: ResourceKind,
    q: str = "",
    status: str | None = None,
    kind: str | None = None,
    scene_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """分页读取项目资源原始字段；继承结果使用对应资源的详情接口。"""
    model = RESOURCES[resource]
    stmt = select(model)
    if q:
        fields = [
            getattr(model, f)
            for f in ("name", "title", "description", "summary", "path")
            if hasattr(model, f)
        ]
        if fields:
            # contains(autoescape) 将百分号和下划线当普通搜索文字。
            stmt = stmt.where(or_(*(field.contains(q, autoescape=True) for field in fields)))
    for field, value in (("status", status), ("kind", kind), ("scene_id", scene_id)):
        if value is not None:
            if not hasattr(model, field):
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "筛选字段不适用",
                    f"{resource} 不支持 {field} 筛选。",
                    ["删除该筛选字段后重试"],
                )
            stmt = stmt.where(getattr(model, field) == value)
    async with db_of(pid).read() as session:
        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (
            (await session.execute(stmt.order_by(model.id).limit(limit).offset(offset)))
            .scalars()
            .all()
        )
    return {"items": [as_dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/projects/{pid}/shots/{shot_id}/graph")
async def shot_graph(pid: str, shot_id: str) -> dict:
    shot = await story.get_shot(pid, shot_id)
    versions = await generation.list_versions(pid, shot_id)
    return {
        "shot": shot,
        "context": await context.resolve(pid, shot_id),
        "versions": versions,
        "current_version": next(
            (v for v in versions if v["id"] == shot.get("current_version_id")), None
        ),
    }


@router.get("/projects/{pid}/assets/{asset_id}/content")
async def asset_content(pid: str, asset_id: str) -> FileResponse:
    """仅按已登记素材读取媒体，禁止工程数据库与任意服务器路径。"""
    from app.api.files import resolve_within, serve
    from app.services.assets import kind_of_suffix

    asset = await fetch(db_of(pid), Asset, asset_id, "素材")
    root = projects.get(pid).dir
    target = resolve_within(root, asset.path)
    if kind_of_suffix(target.suffix) == "other" or target.suffix.lower() in {".db", ".json"}:
        from app.services.access import denied

        raise denied("开放下载接口只允许已登记的图片、视频和音频。")
    return serve(root, asset.path, "工程素材")


@router.get("/library/assets/{asset_id}/content")
async def library_asset_content(asset_id: str) -> FileResponse:
    from app.api.files import resolve_within, serve
    from app.persistence.models_library import LibAsset
    from app.services.access import denied
    from app.services.assets import kind_of_suffix
    from app.services.library import library

    lib = await library.current()
    asset = await fetch(lib.db, LibAsset, asset_id, "素材")
    target = resolve_within(lib.dir, asset.path)
    if kind_of_suffix(target.suffix) == "other":
        raise denied("开放下载接口只允许已登记的媒体。")
    return serve(lib.dir, asset.path, "公共素材库")


# 补齐现有列表没有提供的单对象读取入口；都查询当前 pid 对应的数据库。
def detail_endpoint(model):  # noqa: ANN001, ANN201
    async def read_detail(pid: str, ident: str) -> dict[str, Any]:
        return as_dict(await fetch(db_of(pid), model, ident, "资源"))

    return read_detail


for resource, model in RESOURCES.items():
    if resource not in {"shots", "scenes", "appearances"}:
        router.add_api_route(
            f"/projects/{{pid}}/{resource}/{{ident}}",
            detail_endpoint(model),
            methods=["GET"],
            name=f"get_{resource}_detail",
            summary=f"读取 {resource} 单条资源",
        )
