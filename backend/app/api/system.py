"""系统状态：健康检查与外部依赖探测。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from app.ai.llm import protocols as llm_protocols
from app.core import ffmpeg as ffmpeg_tool
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
    """探 FFmpeg。内置副本是设计上的默认来源，所以 detail 里要写清用的是哪一份。"""
    found = ffmpeg_tool.locate("ffmpeg")
    if not found.path:
        detail = (
            f"配置指向的 {found.configured_missing} 不存在"
            if found.configured_missing
            else "内置副本与 PATH 里都没有"
        )
        return DepStatus(
            name="ffmpeg",
            ok=False,
            detail=detail,
            hint=(f"{ffmpeg_tool.FETCH_HINT}；抽帧、代理转码与导出都依赖它，其余功能不受影响。"),
        )
    where = ffmpeg_tool.SOURCE_LABEL.get(found.source or "", "")
    try:
        proc = await asyncio.create_subprocess_exec(
            found.path,
            "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        first = out.decode(errors="replace").splitlines()[0] if out else ""
        return DepStatus(
            name="ffmpeg",
            ok=proc.returncode == 0,
            detail=f"{where} · {first or found.path}",
        )
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
    """只看「配没配齐」，不发请求——依赖体检要快，真连一次是设置页的「测试连接」。"""
    if settings.llm_provider in ("", "none"):
        return DepStatus(
            name="llm",
            ok=True,
            detail="未配置（Manual 模式）",
            hint="LLM 不是必选项。配置后可启用 AI Director 的剧本拆解与 Prompt 润色。",
        )
    proto = llm_protocols.get()
    if proto is None:
        return DepStatus(
            name="llm",
            ok=False,
            detail=f"不认识的协议 {settings.llm_provider}",
            hint=f"在设置页重新选择协议，可用的是：{'、'.join(llm_protocols.names())}。",
        )
    # Ollama 这类本机端不要密钥，别把「没填 Key」当成没配好。
    ready = bool(settings.llm_model) and (bool(settings.llm_api_key) or not proto.needs_key)
    return DepStatus(
        name="llm",
        ok=ready,
        detail=f"{proto.label} · {settings.llm_model or '未指定模型'}",
        hint="" if ready else "缺少模型名或 API Key——在设置页点「自动获取」列出可用模型再挑一个。",
    )


@router.get("/system/deps", response_model=list[DepStatus])
async def deps() -> list[DepStatus]:
    ffmpeg, comfy = await asyncio.gather(_probe_ffmpeg(), _probe_comfy())
    return [ffmpeg, comfy, _probe_llm()]
