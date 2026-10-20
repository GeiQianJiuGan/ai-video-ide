"""构造固定规模的临时工程，测分镜板读取耗时。

示例：python scripts/benchmark_storyboard.py --scenes 30 --shots-per-scene 10
这是开发基准，不设置依赖机器性能的通过阈值。
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.ids import new_id  # noqa: E402
from app.persistence.models_gen import GenerationVersion  # noqa: E402
from app.persistence.models_story import Scene, Shot  # noqa: E402
from app.services.projects import projects  # noqa: E402
from app.services.story import story  # noqa: E402


async def benchmark(scenes: int, shots_per_scene: int, versions_per_shot: int) -> None:
    with tempfile.TemporaryDirectory(prefix="aivs-storyboard-bench-") as temp:
        project = await projects.create(
            str(Path(temp) / "film"), "分镜板基准", 1920, 1080, 25, "frames"
        )
        try:
            shot_ids: list[str] = []
            async with project.db.write() as session:
                for scene_no in range(1, scenes + 1):
                    scene_id = new_id("scene")
                    session.add(Scene(id=scene_id, index_no=scene_no, title=f"第 {scene_no} 幕"))
                    for shot_no in range(1, shots_per_scene + 1):
                        shot_id = new_id("shot")
                        shot_ids.append(shot_id)
                        session.add(
                            Shot(
                                id=shot_id,
                                scene_id=scene_id,
                                index_no=shot_no,
                                title=f"镜头 {scene_no}-{shot_no}",
                            )
                        )
            async with project.db.write() as session:
                for shot_id in shot_ids:
                    for version_no in range(1, versions_per_shot + 1):
                            session.add(
                                GenerationVersion(
                                    id=new_id("generation_version"),
                                    shot_id=shot_id,
                                    version_no=version_no,
                                )
                            )
        except BaseException:
            await projects.close(project.id)
            raise
        try:
            await story.storyboard(project.id)  # 预热 ORM 与文件缓存
            elapsed = []
            for _ in range(5):
                start = time.perf_counter()
                lanes = await story.storyboard(project.id)
                elapsed.append((time.perf_counter() - start) * 1000)
            cards = sum(len(lane["shots"]) for lane in lanes)
            print(
                f"{scenes} 幕 / {cards} 镜头 / {cards * versions_per_shot} 版本："
                f"中位 {statistics.median(elapsed):.1f} ms，"
                f"范围 {min(elapsed):.1f}–{max(elapsed):.1f} ms"
            )
        finally:
            await projects.close(project.id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenes", type=int, default=30)
    parser.add_argument("--shots-per-scene", type=int, default=10)
    parser.add_argument("--versions-per-shot", type=int, default=3)
    args = parser.parse_args()
    if min(args.scenes, args.shots_per_scene, args.versions_per_shot) < 1:
        parser.error("所有数量都必须至少为 1")
    asyncio.run(benchmark(args.scenes, args.shots_per_scene, args.versions_per_shot))


if __name__ == "__main__":
    main()
