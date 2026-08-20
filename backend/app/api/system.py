"""系统状态：健康检查与外部依赖探测。"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import get_logger

router = APIRouter(tags=["system"])
log = get_logger("api.system")


class Health(BaseModel):
    status: str = "ok"
    app: str
    version: str
    schema_version: int


class DepStatus(BaseModel):
    name: str
    ok: bool
    detail: str
    hint: str = ""


@router.get("/health", response_model=Health)
async def health() -> Health:
    return Health(
        app=settings.app_name,
        version=settings.version,
        schema_version=settings.schema_version,
    )


async def _probe_ffmpeg() -> DepStatus:
    exe = shutil.which(settings.ffmpeg_path)
    if not exe:
        return DepStatus(
            name="ffmpeg",
            ok=False,
            detail=f"未找到可执行文件：{settings.ffmpeg_path}",
            hint=(
                "安装 FFmpeg 并加入 PATH，或在 Settings 中指定绝对路径。"
                "抽帧、代理转码与导出均依赖它。"
            ),
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "-version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        first = out.decode(errors="replace").splitlines()[0] if out else ""
        return DepStatus(name="ffmpeg", ok=proc.returncode == 0, detail=first or exe)
    except (TimeoutError, OSError) as exc:
        return DepStatus(name="ffmpeg", ok=False, detail=f"{type(exc).__name__}: {exc}")


async def _probe_comfy() -> DepStatus:
    url = settings.comfy_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url}/system_stats")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        devices = data.get("devices") or []
        dev = devices[0].get("name", "unknown") if devices else "unknown"
        return DepStatus(name="comfyui", ok=True, detail=f"在线 · {dev}")
    except Exception as exc:
        return DepStatus(
            name="comfyui",
            ok=False,
            detail=f"无法连接 {url}（{type(exc).__name__}）",
            hint="启动 ComfyUI，或在 Settings 中修改地址。未连接时仍可使用 Manual 模式编排项目。",
        )


def _probe_llm() -> DepStatus:
    if settings.llm_provider == "none":
        return DepStatus(
            name="llm",
            ok=True,
            detail="未配置（Manual 模式）",
            hint="LLM 不是必选项。配置后可启用 AI Director 的剧本拆解与 Prompt 润色。",
        )
    ready = bool(settings.llm_base_url or settings.llm_api_key)
    return DepStatus(
        name="llm",
        ok=ready,
        detail=f"{settings.llm_provider} · {settings.llm_model or '未指定模型'}",
        hint="" if ready else "缺少 base_url 或 api_key。",
    )


@router.get("/system/deps", response_model=list[DepStatus])
async def deps() -> list[DepStatus]:
    ffmpeg, comfy = await asyncio.gather(_probe_ffmpeg(), _probe_comfy())
    return [ffmpeg, comfy, _probe_llm()]
