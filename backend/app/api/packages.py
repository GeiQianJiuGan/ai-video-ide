"""导入导出接口：把「一套能跑起来的环境」搬到另一台机器上。

两个粒度、一律**先账单再动手**（与 `adopt` / `ingest` / `sequence` 同一个习惯）：

  · 工程包（`scope="project"`）—— 整个工程搬走：`project.db` 快照 + 素材（成片可选）
    + 一份**环境要求清单**。导入时先 `/packages/inspect` 看清单与本机的比对结果。
  · 场景包（`scope="scene"`）—— 只搬一幕的设定（人物 / 地点 / 道具 / 镜头结构 / 参考图），
    **能导进任意已打开的工程**，id 全部重映射、同名实体默认复用。

`/packages/inspect` 与工程导入挂在顶层（不带 pid）：导入的目标是一个**还不存在的工程**，
此时没有任何 pid 可传。错误码全部复用已有的，**不新增 ErrorCode**。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.packages import packages

router = APIRouter(tags=["packages"])


class ExportPlanBody(BaseModel):
    include_generated: bool = Field(
        default=False,
        description="带上生成的成片（generations/）。默认不带——包会小很多，设定照旧完整",
    )


class ExportBody(ExportPlanBody):
    out_dir: str = Field(description="包落在哪个目录（必须已存在，用目录选择器选）")
    filename: str = Field(default="", description="文件名；留空用账单里的建议名")


class PathBody(BaseModel):
    path: str = Field(description="包文件的绝对路径（.aivspkg）")


class ImportProjectBody(PathBody):
    dir: str = Field(description="新工程落在哪个目录（空目录；已有工程时报 CONFLICT）")


class ImportSceneBody(PathBody):
    reuse_by_name: bool = Field(
        default=True,
        description="同名的人物 / 地点 / 道具复用已有的（默认）。false = 全部新建",
    )


# --- 导出 ---


#: 路径刻意是 `/package`（而不是更顺口的 `/export`）：`POST /projects/{pid}/export` 早就是
#: 时间线的「导出成片」了（`api/timeline.py`），同名会被先注册的那个吃掉，且两者语义完全不同
#: ——一个产出 mp4，一个产出 .aivspkg。


@router.post("/projects/{pid}/package/plan")
async def plan_project(pid: str, body: ExportPlanBody) -> dict[str, Any]:
    """工程包账单：多大、几个文件、哪几条资产的文件已经不在磁盘上、要什么环境。

    **一个字节都不写。** `omitted` 是「带不走的东西」，界面必须原样显示。
    """
    return await packages.plan_project(pid, body.include_generated)


@router.post("/projects/{pid}/package", status_code=201)
async def export_project(pid: str, body: ExportBody) -> dict[str, Any]:
    return await packages.export_project(pid, body.out_dir, body.filename, body.include_generated)


@router.post("/projects/{pid}/scenes/{sid}/package/plan")
async def plan_scene(pid: str, sid: str, body: ExportPlanBody) -> dict[str, Any]:
    """场景包账单。`omitted` 里逐条写明这一幕**带不走什么**（跨幕衔接、队列历史、
    时间线、已补出来的转场镜头…）——跳过不是失败，但必须说出来。"""
    return await packages.plan_scene(pid, sid, body.include_generated)


@router.post("/projects/{pid}/scenes/{sid}/package", status_code=201)
async def export_scene(pid: str, sid: str, body: ExportBody) -> dict[str, Any]:
    return await packages.export_scene(
        pid, sid, body.out_dir, body.filename, body.include_generated
    )


# --- 导入 ---


@router.post("/packages/inspect")
async def inspect(body: PathBody) -> dict[str, Any]:
    """只读清单，不解包：这是什么包、带了什么，以及**它要的环境本机齐不齐**
    （哪份预设缺了、provider 配没配、schema 吃不吃得下）。缺什么只在比对结果里标，不抛。"""
    return await packages.inspect(body.path)


@router.post("/packages/import/project", status_code=201)
async def import_project(body: ImportProjectBody) -> dict[str, Any]:
    """把工程包还原成一个工程并打开它。**导入的副本会拿到一个新的工程 id**
    （同机导入一份副本后两个目录同 id 会在注册表里互相顶掉）。"""
    return await packages.import_project(body.path, body.dir)


@router.post("/projects/{pid}/packages/import/scene/plan")
async def plan_scene_import(pid: str, body: ImportSceneBody) -> dict[str, Any]:
    """导入前账单：每个人物 / 地点 / 道具是复用还是新建、几个文件按 sha1 复用、
    包里明确带不走的是哪些。**一行都不落库。**"""
    return await packages.plan_scene_import(body.path, pid, body.reuse_by_name)


@router.post("/projects/{pid}/packages/import/scene", status_code=201)
async def import_scene(pid: str, body: ImportSceneBody) -> dict[str, Any]:
    return await packages.import_scene(body.path, pid, body.reuse_by_name)
