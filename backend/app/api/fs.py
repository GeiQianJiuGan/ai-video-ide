"""本机目录浏览接口（Phase 1）。

只做三件事：列驱动器与常用位置、列一个目录下的子目录、在一个目录下新建文件夹。
不列文件、不读内容——「选一个工程目录」需要的就只有这些。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services import fsbrowse

router = APIRouter(tags=["fs"])


class RootOut(BaseModel):
    name: str
    path: str
    #: drive = 驱动器 / 根；place = 主目录、桌面、文档这类常用位置
    kind: str


class RootsOut(BaseModel):
    roots: list[RootOut]
    home: str
    sep: str


class DirEntryOut(BaseModel):
    name: str
    path: str
    #: 这个目录已经是一个 aivs 工程（可以直接打开）
    is_project: bool
    #: 这个目录已经是一个 aivs 素材库
    is_library: bool
    has_children: bool
    writable: bool


class CrumbOut(BaseModel):
    name: str
    path: str


class DirOut(BaseModel):
    path: str
    #: 盘符没有上一级，这时是 None
    parent: str | None
    name: str
    is_project: bool
    is_library: bool
    writable: bool
    entries: list[DirEntryOut]
    #: 子目录太多时只返回前一批，UI 要提示用户手输路径
    truncated: bool
    crumbs: list[CrumbOut]


class MkdirIn(BaseModel):
    parent: str = Field(description="在哪个目录下新建")
    name: str = Field(min_length=1, max_length=120)


class MkdirOut(BaseModel):
    path: str
    name: str


@router.get("/fs/roots", response_model=RootsOut)
async def fs_roots() -> RootsOut:
    return RootsOut(**fsbrowse.roots())


@router.get("/fs/dirs", response_model=DirOut)
async def fs_dirs(path: str = Query(description="要列出的目录绝对路径")) -> DirOut:
    return DirOut(**fsbrowse.listdir(path))


@router.post("/fs/mkdir", response_model=MkdirOut, status_code=201)
async def fs_mkdir(body: MkdirIn) -> MkdirOut:
    return MkdirOut(**fsbrowse.mkdir(body.parent, body.name))
