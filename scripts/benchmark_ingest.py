"""隔离测量共用提示词与切段落库；不含 FFmpeg，不使用现有工程。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import settings  # noqa: E402
from app.core.ids import new_id  # noqa: E402
from app.persistence.models_frames import FrameCard, FrameWorkspace  # noqa: E402
from app.persistence.models_story import Scene, Shot  # noqa: E402
from app.persistence.models_world import Asset  # noqa: E402
from app.services.frame_authoring import frames  # noqa: E402
from app.services.generation import generation  # noqa: E402
from app.services.ingest import ingest  # noqa: E402
from app.services.projects import projects  # noqa: E402
from sqlalchemy import event  # noqa: E402


async def benchmark(count: int, output: Path | None) -> None:
    with tempfile.TemporaryDirectory(prefix="aivs-ingest-bench-") as temp:
        settings.runtime_dir = Path(temp) / "runtime"
        settings.runtime_dir.mkdir()
        project = await projects.create(
            str(Path(temp) / "film"), "切段性能基准", 640, 360, 25, "frames"
        )
        original_plan = ingest.plan
        generation._paused.add(project.id)
        try:
            scene_id, asset_id = new_id("scene"), new_id("asset")
            async with project.db.write() as session:
                session.add(Scene(id=scene_id, title="已有幕", index_no=1))
                session.add(
                    Asset(id=asset_id, kind="upload", path="source.mp4", source="benchmark")
                )
                await session.flush()
                for i in range(count):
                    sid = new_id("shot")
                    session.add(Shot(id=sid, scene_id=scene_id, index_no=i + 1))
                    await session.flush()
                    session.add(
                        FrameWorkspace(shot_id=sid, video_json='{"prompt":"old","preset":"keep"}')
                    )
                    session.add_all(
                        [
                            FrameCard(
                                id=new_id("frame"),
                                shot_id=sid,
                                position=j,
                                title=f"画面{j}",
                                prompt="old",
                            )
                            for j in range(4)
                        ]
                    )
            statements = 0

            def query(*args):
                nonlocal statements
                statements += 1

            event.listen(project.db.engine.sync_engine, "before_cursor_execute", query)
            results = {"shots": count, "frame_cards": count * 4}

            async def measure(name, action):
                nonlocal statements
                statements = 0
                start = time.perf_counter()
                await action()
                results[name] = {
                    "milliseconds": round((time.perf_counter() - start) * 1000, 2),
                    "sql_statements": statements,
                }

            await measure(
                "shared_save",
                lambda: frames.save_shared_prompts(
                    project.id, scene_id, {"image_prompt": "new", "video_prompt": "new"}
                ),
            )
            await measure(
                "shared_clear",
                lambda: frames.save_shared_prompts(
                    project.id,
                    scene_id,
                    {"image_prompt": "new", "video_prompt": "new", "clear_overrides": True},
                ),
            )

            async def plan(*args, **kwargs):
                return {
                    "path": "source.mp4",
                    "method": "fixed",
                    "method_label": "固定长度",
                    "trimmed_head": 0,
                    "trimmed_tail": 0,
                    "total": count,
                    "segments": [
                        {
                            "index_no": i + 1,
                            "in_point": i * 2,
                            "out_point": (i + 1) * 2,
                            "duration": 2,
                        }
                        for i in range(count)
                    ],
                }

            ingest.plan = plan
            await measure("ingest_database", lambda: ingest.run(project.id, asset_id))
            event.remove(project.db.engine.sync_engine, "before_cursor_execute", query)
            if output:
                await asyncio.to_thread(
                    output.write_text, json.dumps(results, indent=2), encoding="utf-8"
                )
            print(json.dumps(results))
        finally:
            ingest.plan = original_plan
            await generation.stop_pump(project.id)
            generation._paused.discard(project.id)
            await projects.close(project.id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shots", type=int, default=200)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    asyncio.run(benchmark(args.shots, args.output))
