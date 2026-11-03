"""Frame authoring endpoints, independent of legacy shot editing."""

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.ai.frame_plan import FramePlanDraft
from app.domain.frames import FrameCardPatch as CardPatch
from app.persistence.models_frames import FrameCard, FrameWorkspace
from app.persistence.models_gen import Job
from app.services.base import db_of, dump_json, fetch, fetch_all, load_json
from app.services.frame_authoring import frames, invalid
from app.services.images import images

router = APIRouter(tags=["frames"])


@router.get("/projects/{pid}/frame-reference-materials")
async def reference_materials(pid: str) -> dict[str, Any]:
    return await frames.reference_materials(pid)


class CardCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = ""
    story: str = ""
    role: Literal["first", "key", "last"] = "key"


class AssetBody(BaseModel):
    asset_id: str


class AdoptBody(BaseModel):
    candidate_id: str


class VideoBody(BaseModel):
    prompt: str = ""
    duration: float = Field(default=4, gt=0, le=60)
    preset: str = ""
    first_card_id: str = ""
    last_card_id: str = ""


class PlanApply(FramePlanDraft):
    shot_updated_at: str
    frame_card_ids: list[str] = Field(default_factory=list)


@router.post("/projects/{pid}/shots/{sid}/frame-plan/propose")
async def propose_frame_plan(pid: str, sid: str) -> dict[str, Any]:
    return await frames.propose_plan(pid, sid)


@router.post("/projects/{pid}/shots/{sid}/frame-plan/apply")
async def apply_frame_plan(pid: str, sid: str, body: PlanApply) -> dict[str, Any]:
    return await frames.apply_plan(pid, sid, body.model_dump())


@router.get("/projects/{pid}/shots/{sid}/frames")
async def workspace(pid: str, sid: str) -> dict[str, Any]:
    return await frames.get(pid, sid)


@router.post("/projects/{pid}/shots/{sid}/frames", status_code=201)
async def create(pid: str, sid: str, body: CardCreate) -> dict[str, Any]:
    return await frames.create(pid, sid, body.model_dump())


@router.patch("/projects/{pid}/frame-cards/{cid}")
async def update(pid: str, cid: str, body: CardPatch) -> dict[str, Any]:
    return await frames.update(pid, cid, body.model_dump(exclude_none=True))


@router.post("/projects/{pid}/frame-cards/{cid}/candidates", status_code=201)
async def candidate(pid: str, cid: str, body: AssetBody) -> dict[str, Any]:
    return await frames.add_candidate(pid, cid, body.asset_id)


@router.delete("/projects/{pid}/frame-cards/{cid}")
async def remove(pid: str, cid: str) -> dict[str, str]:
    card = await fetch(db_of(pid), FrameCard, cid, "画面")
    jobs = await fetch_all(db_of(pid), Job, where=Job.target_id == cid)
    if any(j.status in ("queued", "waiting", "running", "paused") for j in jobs):
        raise invalid("请先取消该画面的未完成图片任务，再移除画面。")
    async with db_of(pid).write() as session:
        row = await session.get(FrameCard, cid)
        await session.delete(row)
        ws = await session.get(FrameWorkspace, card.shot_id)
        if ws:
            data = load_json(ws.video_json, {})
            for key in ("first_card_id", "last_card_id"):
                if data.get(key) == cid:
                    data[key] = ""
            ws.video_json = dump_json(data)
    return {"id": cid}


@router.post("/projects/{pid}/frame-cards/{cid}/adopt")
async def adopt(pid: str, cid: str, body: AdoptBody) -> dict[str, Any]:
    return await frames.adopt(pid, cid, body.candidate_id)


@router.post("/projects/{pid}/frame-cards/{cid}/generate", status_code=201)
async def generate(pid: str, cid: str) -> dict[str, Any]:
    card = await fetch(db_of(pid), FrameCard, cid, "画面")
    return await images.enqueue(
        pid,
        "frame_card",
        cid,
        prompt=card.prompt,
        skill="shot_frame",
        ref_asset_ids=load_json(card.refs_json, []),
    )


@router.put("/projects/{pid}/shots/{sid}/frame-video")
async def save_video(pid: str, sid: str, body: VideoBody) -> dict[str, Any]:
    return await frames.save_video(pid, sid, body.model_dump())


@router.post("/projects/{pid}/shots/{sid}/frame-video/plan")
async def plan(pid: str, sid: str, body: VideoBody) -> dict[str, Any]:
    return await frames.video(pid, sid, body.model_dump(exclude_unset=True))


@router.post("/projects/{pid}/shots/{sid}/frame-video/generate", status_code=201)
async def video(pid: str, sid: str, body: VideoBody) -> dict[str, Any]:
    return await frames.video(pid, sid, body.model_dump(exclude_unset=True), enqueue=True)


@router.get("/projects/{pid}/frame-capabilities")
async def capabilities(pid: str) -> dict[str, Any]:
    from app.generation.providers import presets
    from app.services import route

    chosen = await route.resolve(pid, "image2video")
    return {
        "provider": chosen.provider,
        "presets": [
            {
                "name": p["name"],
                "first_frame_ok": p.get("first_frame_ok", False),
                "flf_ready": p.get("flf_ready", False),
            }
            for p in presets.listing()
        ]
        if chosen.binds == "preset"
        else [],
    }
