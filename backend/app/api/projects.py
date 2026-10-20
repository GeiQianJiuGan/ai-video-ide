"""项目容器接口：新建 / 打开 / 最近打开 / 详情 / 关闭。

路径由用户在本机选择，因此请求体里传的是绝对路径字符串。
后端只监听回环地址，工程数据不出本机。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.logging import get_logger
from app.generation.providers import presets
from app.persistence.models import Project, utc_now
from app.services.access import access
from app.services.base import db_of, fetch_all
from app.services.projects import projects

router = APIRouter(tags=["projects"])
log = get_logger("api.projects")


class ProjectCreate(BaseModel):
    dir: str | None = Field(
        default=None, description="管理员可指定工程目录；省略时在受控项目目录创建"
    )
    name: str = Field(min_length=1, max_length=200)
    width: int = Field(default=1920, ge=64, le=8192)
    height: int = Field(default=1080, ge=64, le=8192)
    fps: float = Field(default=25, gt=0, le=240)
    duration_unit: Literal["frames", "seconds"] = "frames"


class ProjectOpen(BaseModel):
    dir: str = Field(description="已有工程目录的绝对路径")


class ProjectOut(BaseModel):
    id: str
    name: str
    dir: str
    width: int
    height: int
    fps: float
    aspect_ratio: str
    duration_unit: str
    schema_version: int
    #: 本次打开时从哪个 schema 升上来；None 表示无需升级
    migrated_from: int | None
    created_at: str
    updated_at: str


class RecentOut(BaseModel):
    id: str
    name: str
    dir: str
    schema_version: int
    opened_at: str
    #: 目录是否还在（被删除或移动后仍然列出，但标为 false）
    exists: bool
    is_open: bool


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectCreate, request: Request) -> ProjectOut:
    from app.core.config import settings
    from app.core.ids import new_id
    from app.services.access import denied

    principal = request.state.principal
    if principal.project_ids is not None:
        raise denied("受限项目 Key 不能创建其他项目。")
    if body.dir:
        principal.require("admin")
    directory = body.dir or str(settings.projects_root / new_id("project"))
    proj = await projects.create(
        directory=directory,
        name=body.name,
        width=body.width,
        height=body.height,
        fps=body.fps,
        duration_unit=body.duration_unit,
    )
    data = proj.to_dict()
    if not principal.allows("admin"):
        data["dir"] = ""
    return ProjectOut(**data)


@router.get("/projects")
async def list_projects(
    request: Request,
    q: str = "",
    archived: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """持久登记的项目，不受最近列表条数与本次进程已打开状态限制。"""
    from pathlib import Path

    principal = request.state.principal
    rows = [
        r
        for r in access.registered()
        if principal.project(r["id"])
        and bool(r["archived"]) == archived
        and q.casefold() in r["name"].casefold()
    ]
    opened = {p.id for p in projects.opened}
    items = []
    for row in rows[offset : offset + limit]:
        item = {
            "id": row["id"],
            "name": row["name"],
            "archived": bool(row["archived"]),
            "exists": (Path(row["dir"]) / "project.aivs.json").is_file(),
            "is_open": row["id"] in opened,
        }
        if principal.allows("admin"):
            item["dir"] = row["dir"]
        items.append(item)
    return {"items": items, "total": len(rows), "limit": limit, "offset": offset}


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = Field(
        default=None, description="true 归档（还需 delete 权限）；false 恢复归档"
    )


@router.patch("/projects/{pid}")
async def patch_project(pid: str, body: ProjectPatch, request: Request) -> dict:
    """修改名称或恢复归档；不移动、递归清除项目目录。"""
    from app.core.errors import not_found
    from app.events.bus import Channel, bus
    from app.services.base import fetch

    if body.archived is True:
        request.state.principal.require("delete", pid)

    rows = access.registered(pid)
    if not rows:
        raise not_found("项目", pid)
    entry = rows[0]
    proj = await projects.open(entry["dir"])
    if body.name is not None:
        await fetch(proj.db, Project, pid, "项目")
        async with proj.db.write() as session:
            row = await session.get(Project, pid)
            row.name = body.name
            row.updated_at = utc_now()
            proj.name = row.name
            proj.updated_at = row.updated_at
        from app.services.projects import _write_manifest

        _write_manifest(proj.dir, projects._manifest_of(proj))
        projects._remember(proj)
    archived = body.archived if body.archived is not None else bool(entry["archived"])
    with access.connect() as db:
        db.execute(
            "UPDATE project_registry SET archived=?,revision=revision+1 WHERE id=?",
            (int(archived), pid),
        )
    if archived:
        await projects.close(pid)
    bus.emit(
        Channel.SYSTEM,
        "project.updated",
        {"id": pid, "name": proj.name, "archived": archived},
        project_id=pid,
    )
    return {"id": pid, "name": proj.name, "archived": archived}


@router.delete("/projects/{pid}", status_code=204)
async def archive_project(pid: str, request: Request) -> None:
    """删除项目入口采用可恢复归档，不删除数据库和素材文件。"""
    await patch_project(pid, ProjectPatch(archived=True), request)


@router.post("/projects/open", response_model=ProjectOut)
async def open_project(body: ProjectOpen) -> ProjectOut:
    proj = await projects.open(body.dir)
    return ProjectOut(**proj.to_dict())


@router.get("/projects/recent", response_model=list[RecentOut])
async def recent_projects() -> list[RecentOut]:
    return [RecentOut(**entry) for entry in projects.recent()]


@router.post("/projects/recent/forget", status_code=204)
async def forget_recent(body: ProjectOpen) -> None:
    """从最近列表移除一条（不动磁盘上的工程本身）。"""
    projects.forget(body.dir)


@router.get("/projects/{pid}", response_model=ProjectOut)
async def get_project(pid: str, request: Request) -> ProjectOut:
    data = projects.get(pid).to_dict()
    if not request.state.principal.allows("admin"):
        data["dir"] = ""
    return ProjectOut(**data)


@router.get("/projects/{pid}/preset")
async def get_project_preset(pid: str) -> dict[str, Any]:
    row = (await fetch_all(db_of(pid), Project))[0]
    listing = presets.listing()
    r2v_name = row.r2v_preset_name or row.preset_name
    flf_name = row.flf_preset_name or row.preset_name

    def item_of(name: str | None) -> dict[str, Any] | None:
        return next((x for x in listing if x["name"] == name), None) if name else None

    r2v_item = item_of(r2v_name)
    flf_item = item_of(flf_name)
    valid_r2v = r2v_name if r2v_item else None
    valid_flf = flf_name if flf_item else None

    return {
        # 兼容旧客户端：name / preset 仍表示普通 Shot 的 R2V 预设。
        "name": valid_r2v,
        "preset": r2v_item,
        "r2v_name": valid_r2v,
        "r2v_preset": r2v_item,
        "flf_name": valid_flf,
        "flf_preset": flf_item,
    }


@router.get("/projects/{pid}/route")
async def get_project_route(pid: str) -> dict[str, Any]:
    """这个工程怎么出片：走哪条路、这条路要绑什么、绑没绑上、缺什么。

    **一个请求画完概览页那一块**，照 `GET /preset` 一次回 r2v + flf 的作风：两条能力
    （普通镜头 / 衔接与转场）各自一份，每份带参考素材槽位与 `issues`（四要素形状，
    前端原样显示 suggestions）。调用方式的候选也在里面（`options`，第一项是「跟随设置页」），
    所以**前端一个调用方式的名字都不写死**。

    只读、绝不抛：缺地址 / 缺预设 / 没绑图都在 `issues` 里——用户正是来这儿看哪里不对的。
    """
    from app.services import route

    return await route.summary(pid)


@router.put("/projects/{pid}/preset")
async def set_project_preset(pid: str, payload: dict[str, str | None]) -> dict[str, Any]:
    from app.services import route

    listing = presets.listing()

    def normalized(key: str) -> str | None:
        return (payload.get(key) or "").strip() or None

    legacy = normalized("name") if "name" in payload else None
    explicit_roles = "r2v_name" in payload or "flf_name" in payload
    requested_r2v = normalized("r2v_name") if "r2v_name" in payload else legacy
    requested_flf = normalized("flf_name") if "flf_name" in payload else legacy

    def settle(name: str | None, role: str, current: str | None) -> str | None:
        """这一次要写进库的预设名——不合格就报错，除非它本来就是库里那一份。

        **「预设不可用」那句话只有 `route.preset_error` 一份**（选的时候、按下生成的时候、
        概览页那份账单共用它）。这里以前抄了一份一模一样的文案，于是「这是出图那份图」
        这类新说法只会出现在其中一处，用户在两个地方看到两种解释。

        库里存着的那份如今不合格时（比如它刚被标上 `AIVS_IMAGE`，成了出图那份图），
        **原样重选等于清空**：不能让用户保存一个已经不能用的选择，但也不该拿一个他自己
        没改的字段挡住整个请求——那样连另一个角色的预设都没法改了。
        """
        if not name:
            return None
        item = next((x for x in listing if x["name"] == name), None)
        if item and item.get("flf_ready" if role == "flf" else "r2v_ready"):
            return name
        if name == current:
            return None
        raise route.preset_error(name, route.ROLE_CAPABILITY[role])

    db = db_of(pid)
    async with db.write() as session:
        row = (await session.execute(select(Project))).scalars().first()
        assert row is not None

        if explicit_roles:
            # 只动 payload 里真的给了的那个角色：另一个保持原样（可能正被别的页面用着）。
            if "r2v_name" in payload:
                row.r2v_preset_name = settle(requested_r2v, "r2v", row.r2v_preset_name)
            if "flf_name" in payload:
                row.flf_preset_name = settle(requested_flf, "flf", row.flf_preset_name)
        else:
            # 老形状（只给一个 `name`）：两个角色都指向它。
            row.r2v_preset_name = settle(requested_r2v, "r2v", row.r2v_preset_name)
            row.flf_preset_name = settle(requested_flf, "flf", row.flf_preset_name)

        # 旧字段继续镜像 R2V，供旧版本应用打开工程时使用。
        row.preset_name = row.r2v_preset_name
        # **改预设不许改路。** 这里以前无条件写 `row.generation_mode = "comfy_preset"`：
        # 在 Workflow 页选好「ComfyUI 工作流绑定」或「通用 REST API」，回概览页改一下预设
        # 就被悄悄改回预设那条路，而界面上那个下拉还显示着用户选的。调用方式只有
        # `PUT /projects/{pid}/workflow-bindings` 那一个写入口（过 `route.normalize()`）。
        row.updated_at = utc_now()
    return await get_project_preset(pid)


@router.post("/projects/{pid}/close", status_code=204)
async def close_project(pid: str) -> None:
    await projects.close(pid)
