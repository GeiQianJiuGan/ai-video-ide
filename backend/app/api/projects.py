"""项目容器接口：新建 / 打开 / 最近打开 / 详情 / 关闭。

路径由用户在本机选择，因此请求体里传的是绝对路径字符串。
后端只监听回环地址，工程数据不出本机。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.logging import get_logger
from app.generation.providers import presets
from app.persistence.models import Project, utc_now
from app.services.base import db_of, fetch_all
from app.services.projects import projects

router = APIRouter(tags=["projects"])
log = get_logger("api.projects")


class ProjectCreate(BaseModel):
    dir: str = Field(description="工程目录绝对路径；不存在会被创建")
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
async def create_project(body: ProjectCreate) -> ProjectOut:
    proj = await projects.create(
        directory=body.dir,
        name=body.name,
        width=body.width,
        height=body.height,
        fps=body.fps,
        duration_unit=body.duration_unit,
    )
    return ProjectOut(**proj.to_dict())


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
async def get_project(pid: str) -> ProjectOut:
    return ProjectOut(**projects.get(pid).to_dict())


@router.get("/projects/{pid}/preset")
async def get_project_preset(pid: str) -> dict[str, Any]:
    row = (await fetch_all(db_of(pid), Project))[0]
    listing = presets.listing()
    r2v_name = row.r2v_preset_name or row.preset_name
    flf_name = row.flf_preset_name or row.preset_name

    def item_of(name: str | None) -> dict[str, Any] | None:
        return next((x for x in listing if x["name"] == name), None) if name else None

    return {
        # 兼容旧客户端：name / preset 仍表示普通 Shot 的 R2V 预设。
        "name": r2v_name,
        "preset": item_of(r2v_name),
        "r2v_name": r2v_name,
        "r2v_preset": item_of(r2v_name),
        "flf_name": flf_name,
        "flf_preset": item_of(flf_name),
    }


@router.put("/projects/{pid}/preset")
async def set_project_preset(pid: str, payload: dict[str, str | None]) -> dict[str, Any]:
    listing = presets.listing()

    def normalized(key: str) -> str | None:
        return (payload.get(key) or "").strip() or None

    legacy = normalized("name") if "name" in payload else None
    explicit_roles = "r2v_name" in payload or "flf_name" in payload
    requested_r2v = normalized("r2v_name") if "r2v_name" in payload else legacy
    requested_flf = normalized("flf_name") if "flf_name" in payload else legacy

    def validate(name: str | None, role: str) -> None:
        if not name:
            return
        item = next((x for x in listing if x["name"] == name), None)
        ready_key = "flf_ready" if role == "flf" else "r2v_ready"
        if item and item.get(ready_key):
            return
        from app.core.errors import AppError, ErrorCode

        raise AppError(
            ErrorCode.INVALID_WORKFLOW,
            "预设不可用",
            (f"预设 {name} 不存在或不能用于{'首尾帧 / FL2VA' if role == 'flf' else 'R2V'}。"),
            [
                "到左侧「预设 Workflow」导入并修复这份图",
                # 两个角色要的东西不一样，说错一句用户就会去改一个本来没问题的标题：
                # R2V 只要一个提示词入口（首尾帧节点可以一个都没有），
                # 补转场要的是严格首尾帧，缺哪一头都接不上。
                (
                    "FL2VA 预设必须同时标出 AIVS_FIRST_FRAME、AIVS_LAST_FRAME、AIVS_PROMPT"
                    if role == "flf"
                    else "R2V 预设至少要标出 AIVS_PROMPT；首尾帧节点没有也行，"
                    "首帧会当作参考图 1 送进去"
                ),
                "再回项目选择它",
            ],
            {"preset": name, "role": role},
        )

    validate(requested_r2v, "r2v")
    validate(requested_flf, "flf")
    db = db_of(pid)
    async with db.write() as session:
        row = (await session.execute(select(Project))).scalars().first()
        assert row is not None
        if explicit_roles:
            if "r2v_name" in payload:
                row.r2v_preset_name = requested_r2v
            if "flf_name" in payload:
                row.flf_preset_name = requested_flf
        else:
            row.r2v_preset_name = requested_r2v
            row.flf_preset_name = requested_flf
        # 旧字段继续镜像 R2V，供旧版本应用打开工程时使用。
        row.preset_name = row.r2v_preset_name
        row.generation_mode = "comfy_preset"
        row.updated_at = utc_now()
    return await get_project_preset(pid)


@router.post("/projects/{pid}/close", status_code=204)
async def close_project(pid: str) -> None:
    await projects.close(pid)
