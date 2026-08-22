"""应用级设置：可写配置与「测试连接」（Step 1）。

极薄：Pydantic body + 转调 `services/appsettings.py` 与 `generation/providers/presets.py`。
两条边界写在这里，因为它们是对外契约的一部分：

  · **API Key 永不回明文**——`GET /settings` 只回 `masked` + `has_value`；
  · 预设是「我这台机器怎么调模型」，所以挂在应用级而不是某个工程下。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, UploadFile
from pydantic import BaseModel, Field

from app.core.errors import AppError, ErrorCode
from app.generation.providers import presets, registry
from app.services.appsettings import app_settings

router = APIRouter(tags=["settings"])


class ProbeIn(BaseModel):
    what: str = Field("llm", description="llm | video")


class ModelsIn(BaseModel):
    """自动获取取值。协议 / 地址 / 密钥可以带上「还没保存」的那份，先试再存。"""

    what: str = Field("llm", description="llm")
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None


@router.get("/settings")
async def read() -> dict[str, Any]:
    return {**app_settings.snapshot(), "providers": registry.listing()}


@router.patch("/settings")
async def patch(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """按点号键提交覆盖。值为 `null`（密钥与提示词也可以是空串）表示清除覆盖，回到内置默认。"""
    return {**await app_settings.patch(payload), "providers": registry.listing()}


@router.post("/settings/probe")
async def probe(payload: ProbeIn) -> dict[str, Any]:
    return await app_settings.probe(payload.what)


@router.post("/settings/models")
async def models(payload: ModelsIn) -> dict[str, Any]:
    """列出这个端有哪些模型，省得手抄模型名。

    密钥的空串按「没填」处理——前端只在用户真的敲了新密钥时才提交它，
    否则这里要沿用已保存的那把（照 `GET /settings` 永不回明文的规矩）。
    """
    return await app_settings.models(
        payload.what,
        provider=payload.provider or None,
        base_url=payload.base_url,
        api_key=payload.api_key or None,
    )


# --- 生成预设（模型端那份图的本地副本） ---


@router.get("/settings/presets")
async def list_presets() -> dict[str, Any]:
    return {
        "dir": presets.presets_dir().as_posix(),
        "items": presets.listing(),
        "how_to": presets.HOW_TO,
    }


@router.post("/settings/presets")
async def upload_preset(name: str = Body(...), graph: str = Body(...)) -> dict[str, Any]:
    """粘贴 API 格式的 json 文本。存之前先体检，绝不留下一份填不进去的图。"""
    return presets.save(name, graph)


@router.post("/settings/presets/upload")
async def upload_preset_file(file: UploadFile, name: str | None = None) -> dict[str, Any]:
    raw = (await file.read()).decode("utf-8", errors="replace")
    stem = (name or (file.filename or "preset")).rsplit("/", 1)[-1]
    if stem.lower().endswith(".json"):
        stem = stem[:-5]
    return presets.save(stem, raw)


@router.delete("/settings/presets/{name}")
async def delete_preset(name: str) -> dict[str, str]:
    rows = {row["name"] for row in presets.listing()}
    if name not in rows:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "预设不存在",
            f"预设目录里没有 {name}。",
            ["刷新列表后重试"],
            {"name": name},
        )
    presets.delete(name)
    return {"deleted": name}
