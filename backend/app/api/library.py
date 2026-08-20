"""应用级素材库接口（Phase 3）。

`/library/*` 与工程无关：素材库是应用级的，同一时刻只打开一个。所以这些路径上
**没有 pid**——这也是它和 `/projects/{pid}/assets` 的分界线。

未配置素材库时一律返回结构化的 NOT_FOUND（见 services/library.py::current），
只有 `GET /library` 例外：它用 `configured: false` 告诉前端该画引导，而不是报错。

采用（`POST /projects/{pid}/adopt`）落在这里而不是 assets.py，因为它是「库 → 工程」
的那一步；plan 先给账单，adopt 才动手。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel, Field

from app.services.adopt import ADOPT_KINDS, adopt
from app.services.library import library

router = APIRouter(tags=["library"])


class ConfigureBody(BaseModel):
    dir: str = Field(description="素材库目录绝对路径；空目录会被初始化成新素材库")


class RegisterBody(BaseModel):
    kind: str = Field(default="upload", description="character_sheet / location_reference / …")
    path: str = Field(description="本机文件绝对路径；库永远复制一份进库目录")
    title: str | None = None


class AssetPatch(BaseModel):
    title: str | None = None
    note: str | None = None


class TagBody(BaseModel):
    name: str | None = None
    color: str | None = None


class TagLinkBody(BaseModel):
    owner_kind: str = Field(description="asset / character / location / prop")
    owner_id: str


class CharacterBody(BaseModel):
    name: str | None = None
    alias: str | None = None
    gender: str | None = None
    age_range: str | None = None
    personality: str | None = None
    background: str | None = None
    voice_desc: str | None = None
    notes: str | None = None


class AppearanceBody(BaseModel):
    name: str | None = None
    face: str | None = None
    hair: str | None = None
    body: str | None = None
    traits: str | None = None
    age: str | None = None
    costume: str | None = None
    state: str | None = None
    notes: str | None = None
    parent_id: str | None = Field(default=None, description="从哪个形象预设派生")
    default: bool = False


class SheetBody(BaseModel):
    asset_id: str


class LocationBody(BaseModel):
    name: str | None = None
    description: str | None = None
    notes: str | None = None


class VariantBody(BaseModel):
    name: str | None = None
    time_of_day: str | None = None
    weather: str | None = None
    lighting: str | None = None
    description: str | None = None


class ReferenceBody(BaseModel):
    asset_id: str
    camera: str | None = None
    note: str | None = None


class PropBody(BaseModel):
    name: str | None = None
    description: str | None = None
    notes: str | None = None


class AdoptBody(BaseModel):
    kind: str = Field(description=" / ".join(ADOPT_KINDS))
    library_id: str = Field(description="库里那条记录的 id")


# --- 库本身 ---


@router.get("/library")
async def library_status() -> dict[str, Any]:
    """「有没有配置素材库」本身不是错误，前端靠 configured 决定画不画引导。"""
    return await library.status()


@router.post("/library/configure")
async def configure(body: ConfigureBody) -> dict[str, Any]:
    await library.configure(body.dir)
    return await library.status()


@router.post("/library/close")
async def close_library() -> dict[str, Any]:
    return await library.close()


# --- 库内素材 ---


@router.get("/library/assets")
async def list_assets(kind: str | None = None, tag: str | None = None) -> list[dict[str, Any]]:
    return await library.list_assets(kind, tag)


@router.post("/library/assets/upload", status_code=201)
async def upload_asset(
    kind: str = Form(default="upload"),
    title: str | None = Form(default=None),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    data = await file.read()
    return await library.upload(kind, file.filename or "upload.bin", data, title)


@router.post("/library/assets/register", status_code=201)
async def register_asset(body: RegisterBody) -> dict[str, Any]:
    return await library.register(body.kind, body.path, body.title)


@router.patch("/library/assets/{aid}")
async def update_asset(aid: str, body: AssetPatch) -> dict[str, Any]:
    return await library.update_asset(aid, body.model_dump(exclude_none=True))


@router.get("/library/assets/{aid}/refs")
async def asset_refs(aid: str) -> list[dict[str, Any]]:
    return await library.refs_of(aid)


@router.delete("/library/assets/{aid}")
async def delete_asset(aid: str, force: bool = False) -> dict[str, Any]:
    return await library.delete_asset(aid, force)


# --- 标签（库会越攒越大，这是它比工程多出来的一层） ---


@router.get("/library/tags")
async def list_tags() -> list[dict[str, Any]]:
    return await library.list_tags()


@router.post("/library/tags", status_code=201)
async def create_tag(body: TagBody) -> dict[str, Any]:
    return await library.create_tag(body.model_dump(exclude_none=True))


@router.delete("/library/tags/{tid}", status_code=204)
async def delete_tag(tid: str) -> None:
    await library.delete_tag(tid)


@router.post("/library/tags/{tid}/attach")
async def attach_tag(tid: str, body: TagLinkBody) -> dict[str, Any]:
    return await library.attach_tag(tid, body.owner_kind, body.owner_id)


@router.post("/library/tags/{tid}/detach", status_code=204)
async def detach_tag(tid: str, body: TagLinkBody) -> None:
    await library.detach_tag(tid, body.owner_id)


# --- 角色预设 ---


@router.get("/library/characters")
async def list_characters() -> list[dict[str, Any]]:
    return await library.list_characters()


@router.post("/library/characters", status_code=201)
async def create_character(body: CharacterBody) -> dict[str, Any]:
    return await library.create_character(body.model_dump(exclude_none=True))


@router.patch("/library/characters/{cid}")
async def update_character(cid: str, body: CharacterBody) -> dict[str, Any]:
    return await library.update_character(cid, body.model_dump(exclude_none=True))


@router.delete("/library/characters/{cid}", status_code=204)
async def delete_character(cid: str) -> None:
    await library.delete_character(cid)


@router.post("/library/characters/{cid}/appearances", status_code=201)
async def create_appearance(cid: str, body: AppearanceBody) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True, exclude={"parent_id", "default"})
    return await library.create_appearance(
        cid, patch, parent_id=body.parent_id, default=body.default
    )


@router.patch("/library/appearances/{aid}")
async def update_appearance(aid: str, body: AppearanceBody) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True, exclude={"parent_id", "default"})
    return await library.update_appearance(aid, patch)


@router.delete("/library/appearances/{aid}", status_code=204)
async def delete_appearance(aid: str) -> None:
    await library.delete_appearance(aid)


@router.post("/library/appearances/{aid}/sheets", status_code=201)
async def add_sheet(aid: str, body: SheetBody) -> dict[str, Any]:
    return await library.add_sheet(aid, body.asset_id)


# --- 地点预设 ---


@router.get("/library/locations")
async def list_locations() -> list[dict[str, Any]]:
    return await library.list_locations()


@router.post("/library/locations", status_code=201)
async def create_location(body: LocationBody) -> dict[str, Any]:
    return await library.create_location(body.model_dump(exclude_none=True))


@router.patch("/library/locations/{lid}")
async def update_location(lid: str, body: LocationBody) -> dict[str, Any]:
    return await library.update_location(lid, body.model_dump(exclude_none=True))


@router.delete("/library/locations/{lid}", status_code=204)
async def delete_location(lid: str) -> None:
    await library.delete_location(lid)


@router.post("/library/locations/{lid}/variants", status_code=201)
async def create_variant(lid: str, body: VariantBody) -> dict[str, Any]:
    return await library.create_variant(lid, body.model_dump(exclude_none=True))


@router.patch("/library/variants/{vid}")
async def update_variant(vid: str, body: VariantBody) -> dict[str, Any]:
    return await library.update_variant(vid, body.model_dump(exclude_none=True))


@router.delete("/library/variants/{vid}", status_code=204)
async def delete_variant(vid: str) -> None:
    await library.delete_variant(vid)


@router.get("/library/variants/{vid}/references")
async def variant_references(vid: str) -> list[dict[str, Any]]:
    return await library.variant_references(vid)


@router.post("/library/variants/{vid}/references", status_code=201)
async def add_variant_reference(vid: str, body: ReferenceBody) -> dict[str, Any]:
    return await library.add_variant_reference(vid, body.asset_id, body.camera, body.note)


# --- 道具预设 ---


@router.get("/library/props")
async def list_props() -> list[dict[str, Any]]:
    return await library.list_props()


@router.post("/library/props", status_code=201)
async def create_prop(body: PropBody) -> dict[str, Any]:
    return await library.create_prop(body.model_dump(exclude_none=True))


@router.patch("/library/props/{prop_id}")
async def update_prop(prop_id: str, body: PropBody) -> dict[str, Any]:
    return await library.update_prop(prop_id, body.model_dump(exclude_none=True))


@router.delete("/library/props/{prop_id}", status_code=204)
async def delete_prop(prop_id: str) -> None:
    await library.delete_prop(prop_id)


@router.post("/library/props/{prop_id}/references", status_code=201)
async def add_prop_reference(prop_id: str, body: ReferenceBody) -> dict[str, Any]:
    return await library.add_prop_reference(prop_id, body.asset_id, body.note)


# --- 采用：库 → 工程，单向复制 ---


@router.post("/projects/{pid}/adopt/plan")
async def adopt_plan(pid: str, body: AdoptBody) -> dict[str, Any]:
    """先出账单：复制几个文件、多大、哪些工程里已经有了。UI 必须先给用户看这个。"""
    return await adopt.plan(pid, body.kind, body.library_id)


@router.post("/projects/{pid}/adopt", status_code=201)
async def adopt_into_project(pid: str, body: AdoptBody) -> dict[str, Any]:
    return await adopt.adopt(pid, body.kind, body.library_id)
