"""真实调用示例：默认只创建内容，--generate 才调用生成服务。"""

from __future__ import annotations

import argparse
import mimetypes
import os
import time
import uuid
from pathlib import Path

import httpx


def run(
    client: httpx.Client, project_id: str | None, image: Path, generate: bool
) -> dict:
    def request(method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        if method != "GET":
            headers.setdefault("Idempotency-Key", uuid.uuid4().hex)
        response = client.request(method, path, headers=headers, **kwargs)
        if response.is_error:
            raise RuntimeError(
                f"{method} {path}: {response.status_code} {response.text}"
            )
        return response.json() if response.content else None

    if project_id is None:
        project_id = request(
            "POST",
            "projects",
            json={"name": "外部 AI 示例", "duration_unit": "seconds"},
        )["id"]
    project = request("GET", f"projects/{project_id}")
    root = f"projects/{project_id}"
    with image.open("rb") as stream:
        asset = request(
            "POST",
            root + "/assets/upload",
            files={
                "file": (
                    image.name,
                    stream,
                    mimetypes.guess_type(image.name)[0] or "image/png",
                )
            },
        )
    request(
        "PATCH",
        root + f"/assets/{asset['id']}",
        json={"description": "本次镜头的起始画面与主体参考"},
    )
    character = request(
        "POST",
        root + "/characters",
        json={"name": "示例角色", "default_asset_id": asset["id"]},
    )
    appearances = request("GET", root + f"/characters/{character['id']}/appearances")
    location = request("POST", root + "/locations", json={"name": "示例地点"})
    variant = request(
        "POST",
        root + f"/locations/{location['id']}/variants",
        json={"name": "日间", "description": "自然光下的场景"},
    )
    scene = request(
        "POST",
        root + "/scenes",
        json={"title": "外部 AI 创建的幕", "location_variant_id": variant["id"]},
    )
    request(
        "PUT",
        root + f"/scenes/{scene['id']}/cast",
        json={"appearance_ids": [appearances[0]["id"]]},
    )
    duration = 4 if project["duration_unit"] == "seconds" else 4 * project["fps"]
    shot = request(
        "POST",
        root + f"/scenes/{scene['id']}/shots",
        json={"title": "外部 AI 创建的镜头", "duration": duration},
    )
    path = root + f"/shots/{shot['id']}"
    current = client.get(path)
    current.raise_for_status()
    prompt = f"从 @[首帧](frame:first_frame) 开始，@[图片1](asset:{asset['id']}) 中的人物缓慢向前走，镜头平稳跟随，保持人物外貌一致。"
    request(
        "PATCH",
        path,
        headers={"If-Match": current.headers["etag"]},
        json={
            "prompt": prompt,
            "manual_prompt": True,
            "first_frame_asset_id": asset["id"],
        },
    )
    graph = request("GET", path + "/graph")
    result = {
        "project_id": project_id,
        "scene_id": scene["id"],
        "shot_id": shot["id"],
        "asset_id": asset["id"],
    }
    assert graph["shot"]["first_frame_asset_id"] == asset["id"]
    if generate:
        job = request("POST", path + "/generate", json={})
        result["job_id"] = job["id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            job = request("GET", root + f"/jobs/{job['id']}")
            if job["status"] in {"done", "failed", "cancelled"}:
                break
            time.sleep(2)
        else:
            raise TimeoutError(
                "任务仍在执行；请使用返回的 Job ID 继续查询，不要重复生成"
            )
        result["status"] = job["status"]
        if job["status"] == "done":
            versions = request("GET", path + "/versions")
            version = next(v for v in versions if v["id"] == job["version_id"])
            request("POST", root + f"/versions/{version['id']}/current", json={})
            result["version_id"] = version["id"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args()
    headers = {}
    if key := os.environ.get("AIVS_API_KEY"):
        headers["Authorization"] = "Bearer " + key
    base = (
        os.environ.get("AIVS_BASE_URL", "http://127.0.0.1:8765/api/v1").rstrip("/")
        + "/"
    )
    with httpx.Client(base_url=base, headers=headers, timeout=120) as client:
        print(run(client, args.project_id, args.image, args.generate))


if __name__ == "__main__":
    main()
