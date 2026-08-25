"""剧本 / Scene / Shot / 分镜板接口（Step 5）。

AI 拆解只返回**提案**（/breakdown/propose），提案不落库；
人确认后再 /breakdown/apply。手动新建 Scene / Shot 走同一套结构，不依赖 LLM。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import params
from app.services.story import story

router = APIRouter(tags=["story"])


class StoryBody(BaseModel):
    title: str | None = None
    raw_text: str | None = None
    mode: str | None = Field(default=None, description="manual / ai_assisted")


class SceneBody(BaseModel):
    title: str | None = None
    summary: str | None = None
    source_text: str | None = None
    prompt: str | None = Field(
        default=None, description="这一幕的提示词——小节点里唯一必填的那个；镜头级 prompt 优先"
    )
    location_variant_id: str | None = None
    time_of_day: str | None = None
    notes: str | None = None
    dialogue: str | None = Field(
        default=None, description="这一幕的台词兜底（音源那条链读它）；镜头级 dialogue 优先"
    )
    kind: str | None = Field(
        default=None, description="storyboard 剧本拆出来的 / ingested 从成片切出来的"
    )
    param_mode: str | None = Field(
        default=None,
        description="shared 镜头留空继承幕级参数 / per_shot 新建镜头时预填（只影响创建那一刻）",
    )
    params: dict[str, Any] | None = Field(
        default=None,
        description="幕级共用参数（prompt/negative/duration/seed/steps/preset/refs），整份替换",
    )


class ShotBody(BaseModel):
    title: str | None = None
    description: str | None = None
    duration: float | None = None
    camera: str | None = None
    movement: str | None = None
    status: str | None = None
    prompt: str | None = None
    negative_prompt: str | None = None
    dialogue: str | None = Field(
        default=None, description="这个镜头说的话（音源那条链用）；视频那条链不读它"
    )
    seed: int | None = None
    steps: int | None = None
    workflow_id: str | None = None
    prev_shot_id: str | None = Field(default=None, description="上游镜头；用于首尾帧连续性")
    #: 首 / 末帧槽位。**「哪一张是首帧」是用户按下去的那一下**，不再由账单里优先级最高
    #: 的那张顶替（那条老规矩会把角色三视图当成画面第一格）。必须是图片资产。
    #: PATCH 里的 `null` 会被 `exclude_none` 吃掉，所以**清空槽位传空串 `""`**。
    first_frame_asset_id: str | None = Field(
        default=None, description="首帧图片资产 id；传空串表示清空这个槽位"
    )
    last_frame_asset_id: str | None = Field(
        default=None, description="末帧图片资产 id；传空串表示清空这个槽位"
    )


class OrderBody(BaseModel):
    order: list[str]


class MoveBody(BaseModel):
    scene_id: str
    position: int | None = Field(default=None, description="目标 Scene 内 0-based 落点；None=末尾")


class CastBody(BaseModel):
    appearance_ids: list[str]


class LocationsBody(BaseModel):
    location_variant_ids: list[str] = Field(
        description="这一幕出现的地点变体；第一条同时是主地点（Scene.location_variant_id）"
    )


class PostersBody(BaseModel):
    shot_ids: list[str] | None = Field(
        default=None, description="只补这几个镜头；留空表示补全部「有片子但没有图」的卡片"
    )


class SplitShotBody(BaseModel):
    at_seconds: float = Field(description="在镜头内的第几秒进行拆分（如 2.5）")


class PropsBody(BaseModel):
    items: list[dict[str, Any]] = Field(
        description='[{"prop_id": "prp_…", "state": "present|discarded"}]'
    )


class ProposeBody(BaseModel):
    text: str | None = Field(default=None, description="留空则用已保存的剧本原文")


class ApplyBody(BaseModel):
    scenes: list[dict[str, Any]] = Field(description="提案对象里的 scenes，可带 op=reject 剔除")


@router.get("/projects/{pid}/story")
async def get_story(pid: str) -> dict[str, Any]:
    return await story.get_story(pid)


@router.patch("/projects/{pid}/story")
async def save_story(pid: str, body: StoryBody) -> dict[str, Any]:
    return await story.save_story(pid, body.model_dump(exclude_none=True))


@router.get("/projects/{pid}/scenes")
async def list_scenes(pid: str) -> list[dict[str, Any]]:
    return await story.list_scenes(pid)


@router.post("/projects/{pid}/scenes", status_code=201)
async def create_scene(pid: str, body: SceneBody) -> dict[str, Any]:
    return await story.create_scene(pid, body.model_dump(exclude_none=True))


@router.get("/projects/{pid}/scene-node-options")
async def scene_node_options(pid: str) -> dict[str, Any]:
    """挑小节点用的两张清单（形象 / 地点变体），各带缩略图与当前上限。"""
    return await story.node_options(pid)


@router.get("/projects/{pid}/scenes/{sid}")
async def get_scene(pid: str, sid: str) -> dict[str, Any]:
    """一幕的全部小节点（prompt / 人物 / 地点）与当前上限。"""
    return await story.get_scene(pid, sid)


@router.patch("/projects/{pid}/scenes/{sid}")
async def update_scene(pid: str, sid: str, body: SceneBody) -> dict[str, Any]:
    return await story.update_scene(pid, sid, body.model_dump(exclude_none=True))


@router.put("/projects/{pid}/scenes/{sid}/cast")
async def set_scene_cast(pid: str, sid: str, body: CastBody) -> dict[str, Any]:
    """这一幕的人物小节点。可以是空的，但不能超过 scene.node_limit。"""
    return await story.set_scene_cast(pid, sid, body.appearance_ids)


@router.put("/projects/{pid}/scenes/{sid}/locations")
async def set_scene_locations(pid: str, sid: str, body: LocationsBody) -> dict[str, Any]:
    """这一幕的地点小节点。第一条会同步成主地点变体，同样受 scene.node_limit 限制。"""
    return await story.set_scene_locations(pid, sid, body.location_variant_ids)


@router.delete("/projects/{pid}/scenes/{sid}", status_code=204)
async def delete_scene(pid: str, sid: str) -> None:
    await story.delete_scene(pid, sid)


@router.put("/projects/{pid}/scenes/order")
async def reorder_scenes(pid: str, body: OrderBody) -> list[dict[str, Any]]:
    return await story.reorder_scenes(pid, body.order)


@router.post("/projects/{pid}/scenes/{sid}/shots", status_code=201)
async def create_shot(pid: str, sid: str, body: ShotBody) -> dict[str, Any]:
    return await story.create_shot(pid, sid, body.model_dump(exclude_none=True))


@router.put("/projects/{pid}/scenes/{sid}/shots/order")
async def reorder_shots(pid: str, sid: str, body: OrderBody) -> list[dict[str, Any]]:
    await story.reorder_shots(pid, sid, body.order)
    return await story.storyboard(pid)


@router.get("/projects/{pid}/shots/{shot_id}")
async def get_shot(pid: str, shot_id: str) -> dict[str, Any]:
    return await story.get_shot(pid, shot_id)


@router.get("/projects/{pid}/shots/{shot_id}/params")
async def resolve_params(pid: str, shot_id: str, capability: str = "image2video") -> dict[str, Any]:
    """参数账单：每一项的值 + 它来自哪一级（镜头 / 幕 / 工程 / 默认）。

    界面照它标出「这一项继承自幕」——看不见的继承等于猜，改了幕以后不知道哪些镜头会变。
    """
    return await params.resolve(pid, shot_id, capability=capability)


@router.patch("/projects/{pid}/shots/{shot_id}")
async def update_shot(pid: str, shot_id: str, body: ShotBody) -> dict[str, Any]:
    """更新镜头。首帧槽位有 `prev_shot_id` 时的规则：

    **有上游镜头时首帧强制从上游末帧来，不允许显式指定。** 用户想用自己的首帧就得先
    断开上游（清空 `prev_shot_id`），否则报 `VALIDATION_ERROR`。这是为了防止两处配置
    打架：上游末帧是 tail_frame 衔接的全部意义，显式首帧会把它顶掉。

    **转场镜头首尾帧都不能手动设置**——首帧来自上游镜头末帧，末帧来自下游镜头首帧，
    都是自动确定的，手动改只会让衔接断开。
    """
    from app.core.errors import AppError, ErrorCode
    from app.persistence.models_story import Shot
    from app.services.base import db_of, fetch

    shot = await fetch(db_of(pid), Shot, shot_id, "镜头")

    if shot.kind == "transition" and body.prev_shot_id is not None:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "转场镜头的上下游已固定",
            "转场镜头自动连接负责转场的前后两个镜头，不能手动修改上游依赖。",
            ["回到负责这段转场的前后两个镜头修改衔接", "双击转场镜头只编辑转场本身"],
        )

    # 校验：转场镜头不许改首尾帧
    if shot.kind == "transition":
        if body.first_frame_asset_id is not None or body.last_frame_asset_id is not None:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "转场镜头首尾帧不能手动设置",
                "转场镜头的首帧来自上游镜头末帧，末帧来自下游镜头首帧，都是自动确定的。"
                "手动改会让衔接断开——转场就是为了把两个镜头无缝连起来。",
                ["保持转场镜头的自动首尾帧", "如果要自定义首尾帧，请用普通镜头而非转场"],
            )

    # 校验：有上游时不许改首帧
    if body.first_frame_asset_id is not None:
        if shot.prev_shot_id and body.first_frame_asset_id != "":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "有上游镜头时不能指定首帧",
                f"这个镜头的首帧来自上游镜头（tail_frame 衔接）。"
                "要用自己的首帧就先断开上游：清空「上游镜头」那一栏。",
                ["在镜头编辑器里清空「上游镜头」", "保持当前的上游衔接，不指定首帧"],
            )

    return await story.update_shot(pid, shot_id, body.model_dump(exclude_none=True))


@router.delete("/projects/{pid}/shots/{shot_id}", status_code=204)
async def delete_shot(pid: str, shot_id: str) -> None:
    await story.delete_shot(pid, shot_id)


@router.post("/projects/{pid}/shots/{shot_id}/split")
async def split_shot(pid: str, shot_id: str, body: SplitShotBody) -> dict[str, Any]:
    """将镜头在指定秒数处拆分为两个镜头（长视频切段加工 / 分镜精修）。"""
    return await story.split_shot(pid, shot_id, body.at_seconds)


@router.post("/projects/{pid}/shots/{shot_id}/move")
async def move_shot(pid: str, shot_id: str, body: MoveBody) -> list[dict[str, Any]]:
    return await story.move_shot(pid, shot_id, body.scene_id, body.position)


@router.put("/projects/{pid}/shots/{shot_id}/cast")
async def set_shot_cast(pid: str, shot_id: str, body: CastBody) -> dict[str, Any]:
    return await story.set_shot_cast(pid, shot_id, body.appearance_ids)


@router.put("/projects/{pid}/shots/{shot_id}/props")
async def set_shot_props(pid: str, shot_id: str, body: PropsBody) -> dict[str, Any]:
    return await story.set_shot_props(pid, shot_id, body.items)


@router.get("/projects/{pid}/storyboard")
async def storyboard(pid: str) -> list[dict[str, Any]]:
    return await story.storyboard(pid)


@router.post("/projects/{pid}/storyboard/posters")
async def extract_posters(pid: str, body: PostersBody | None = None) -> dict[str, Any]:
    """给「有片子但没有图」的卡片补抽首帧。读分镜板不起 FFmpeg，补图是这一条显式动作。"""
    return await story.extract_posters(pid, body.shot_ids if body else None)


@router.post("/projects/{pid}/breakdown/propose")
async def propose_breakdown(pid: str, body: ProposeBody) -> dict[str, Any]:
    """只返回提案，不写库。LLM 未配置时返回 LLM_UNAVAILABLE，并提示可手动添加。"""
    return await story.propose_breakdown(pid, body.text)


@router.post("/projects/{pid}/breakdown/apply")
async def apply_breakdown(pid: str, body: ApplyBody) -> dict[str, Any]:
    return await story.apply_breakdown(pid, body.scenes)
