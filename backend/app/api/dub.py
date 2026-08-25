"""音源接口：给镜头配一条声音，**画面一个字节都不重跑**。

三类端点，刻意分开：
  · `dub/plan` → `dub/run`  —— 生成（先账单再动手，一次可能给整幕十几个镜头配音）；
  · `dub/import`            —— 手动导入外面做好的音频，**不需要任何服务**（硬约束 2 的落点）；
  · `audio-versions` / `mute` —— 看有哪几条音轨、取消采用。

「采用哪一条音轨」不在这里：那件事走全工程唯一的采用入口
`POST /projects/{pid}/versions/{version_id}/current`——它认得出这一版是音频，
落到 `Shot.current_audio_version_id` 上（见 `generation.set_current_version`）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.dub import dub

router = APIRouter(tags=["dub"])


class DubBody(BaseModel):
    shot_ids: list[str] | None = Field(default=None, description="给这几个镜头配音")
    scene_id: str | None = Field(default=None, description="整幕一起配")
    text: str | None = Field(
        default=None, description="统一台词；不传就按镜头的 dialogue → 幕的 dialogue"
    )
    prompt: str | None = Field(
        default=None, description="声音描述（音色 / 情绪 / 环境音）。没有台词的镜头靠它"
    )
    negative: str | None = None
    voice_ref_asset_id: str | None = Field(
        default=None, description="音色参考音频的资产 id（几秒干净人声即可）"
    )
    with_video: bool = Field(
        default=False,
        description="把镜头采用的那段画面也送过去（口型驱动那类模型要它）。"
        "图里没有 AIVS_SOURCE_VIDEO 入口时只写一条 note，不失败。",
    )
    preset: str | None = Field(default=None, description="临时换一份音源图")
    seed: int | None = None
    priority: int = 100


class ImportBody(BaseModel):
    path: str = Field(description="本机上那个音频文件的绝对路径")
    adopt: bool = Field(default=True, description="导入后立刻采用成这个镜头的音轨")


@router.post("/projects/{pid}/dub/plan")
async def plan(pid: str, body: DubBody) -> dict[str, Any]:
    """只出账单：给哪几个镜头配、说什么、多长、哪几个跳过为什么。**一个任务都不入队。**"""
    return await dub.plan(
        pid,
        shot_ids=body.shot_ids,
        scene_id=body.scene_id,
        text=body.text,
        prompt=body.prompt,
        voice_ref_asset_id=body.voice_ref_asset_id,
        with_video=body.with_video,
        preset=body.preset,
    )


@router.post("/projects/{pid}/dub/run", status_code=201)
async def run(pid: str, body: DubBody) -> dict[str, Any]:
    """按账单入队。每条产出一个 `kind="audio"` 的版本并自动成为这个镜头采用的音轨。"""
    return await dub.run(
        pid,
        shot_ids=body.shot_ids,
        scene_id=body.scene_id,
        text=body.text,
        prompt=body.prompt,
        negative=body.negative,
        voice_ref_asset_id=body.voice_ref_asset_id,
        with_video=body.with_video,
        preset=body.preset,
        seed=body.seed,
        priority=body.priority,
    )


@router.post("/projects/{pid}/shots/{shot_id}/audio/import", status_code=201)
async def import_audio(pid: str, shot_id: str, body: ImportBody) -> dict[str, Any]:
    """把外面做好的一段音频导入成这个镜头的音频版本。**这条路不需要任何服务。**"""
    return await dub.import_audio(pid, shot_id, body.path, adopt=body.adopt)


@router.get("/projects/{pid}/shots/{shot_id}/audio-versions")
async def audio_versions(pid: str, shot_id: str) -> dict[str, Any]:
    """这个镜头上所有音频版本 + 当前采用的是哪一条。画面那些版本不在这张表里。"""
    return await dub.list_audio_versions(pid, shot_id)


@router.delete("/projects/{pid}/shots/{shot_id}/audio-current")
async def mute(pid: str, shot_id: str) -> dict[str, Any]:
    """取消采用音轨（回到「用画面自带的声音」）。版本一条都不删，随时再采用回去。"""
    return await dub.mute(pid, shot_id)
