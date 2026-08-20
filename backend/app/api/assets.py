"""资产总账接口（Step 3）。

上传走 multipart（前端拖拽），登记本机已有文件走 JSON（桌面端选文件）。
删除默认拒绝仍被引用的资产，force=true 才强删，并回报会破坏几处引用。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel, Field

from app.services.assets import assets

router = APIRouter(tags=["assets"])


class RegisterPathBody(BaseModel):
    kind: str = Field(
        description="character_sheet / location_reference / prop_reference / upload …"
    )
    path: str = Field(description="本机文件绝对路径")
    source: str = "manual"
    #: 字段名不用 copy，避免遮蔽 BaseModel.copy
    copy_file: bool = Field(default=True, description="是否复制进工程目录；false 则只登记引用")


class LinkBody(BaseModel):
    asset_id: str
    owner_kind: str
    owner_id: str
    role: str | None = None


@router.get("/projects/{pid}/assets")
async def list_assets(pid: str, kind: str | None = None) -> list[dict[str, Any]]:
    return await assets.list_assets(pid, kind)


@router.get("/projects/{pid}/assets/orphans")
async def orphans(pid: str) -> list[dict[str, Any]]:
    return await assets.orphans(pid)


@router.post("/projects/{pid}/assets/upload", status_code=201)
async def upload_asset(
    pid: str,
    kind: str = Form(default="upload"),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    data = await file.read()
    return await assets.register_bytes(pid, kind, file.filename or "upload.bin", data)


@router.post("/projects/{pid}/assets/register", status_code=201)
async def register_asset(pid: str, body: RegisterPathBody) -> dict[str, Any]:
    return await assets.register_path(pid, body.kind, body.path, body.source, body.copy_file)


@router.get("/projects/{pid}/assets/{asset_id}/refs")
async def refs_of(pid: str, asset_id: str) -> list[dict[str, Any]]:
    return await assets.refs_of(pid, asset_id)


@router.post("/projects/{pid}/assets/link", status_code=201)
async def link_asset(pid: str, body: LinkBody) -> dict[str, Any]:
    await assets.link(pid, body.asset_id, body.owner_kind, body.owner_id, body.role)
    return {"ok": True}


@router.delete("/projects/{pid}/assets/{asset_id}")
async def delete_asset(pid: str, asset_id: str, force: bool = False) -> dict[str, Any]:
    return await assets.delete(pid, asset_id, force)
