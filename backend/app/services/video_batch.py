"""批量视频生成范围：源视频导入不算新视频已完成。"""

from app.persistence.models_gen import VIDEO_KINDS, GenerationVersion, Job
from app.services.base import db_of, fetch_all


async def video_batch_state(pid: str) -> tuple[set[str], dict[str, str]]:
    versions = await fetch_all(db_of(pid), GenerationVersion)
    done = {
        v.shot_id
        for v in versions
        if v.kind == "video" and v.status == "done" and v.asset_id and v.source != "imported"
    }
    jobs = await fetch_all(db_of(pid), Job)
    active = {
        j.shot_id: j.id
        for j in jobs
        if j.shot_id and j.kind in VIDEO_KINDS and j.status in {"queued", "waiting", "running"}
    }
    return done, active
