"""LLM 客户端（可选依赖）。

硬约束：LLM 不是必选项。默认 provider = none，此时任何 AI 入口都返回
LLM_UNAVAILABLE 这一条结构化错误，并在建议里指出手动路径同样能走完全程。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

log = get_logger("llm")
TIMEOUT = httpx.Timeout(120.0, connect=5.0)


def status() -> dict[str, Any]:
    """给「LLM 未配置 · 手动模式同样可以走完全部流程」那行灰字用。"""
    configured = settings.llm_provider != "none" and bool(settings.llm_model)
    return {
        "configured": configured,
        "provider": settings.llm_provider,
        "model": settings.llm_model or None,
        "hint": (
            f"已配置 {settings.llm_provider} · {settings.llm_model}"
            if configured
            else "LLM 未配置 · 手动模式同样可以走完全部流程"
        ),
    }


def require_configured() -> None:
    if not status()["configured"]:
        raise AppError(
            ErrorCode.LLM_UNAVAILABLE,
            "LLM 未配置",
            f"当前 provider = {settings.llm_provider}，没有可用的模型。",
            [
                "用「手动添加 Scene」走手动路径——数据结构与 AI 路径完全一致",
                "或设置 AIVS_LLM_PROVIDER / AIVS_LLM_BASE_URL / AIVS_LLM_MODEL 后重试",
            ],
        )


def _parse_json_object(text: str) -> dict[str, Any]:
    """模型爱在 JSON 外面裹一层解释文字，这里只截取最外层花括号。"""
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AppError(
            ErrorCode.LLM_INVALID_OUTPUT,
            "LLM 返回的不是合法 JSON",
            f"{exc.msg}（截断预览：{text[:200]}）",
            ["重试一次", "换一个更擅长结构化输出的模型", "或改用手动拆解"],
        ) from exc
    if not isinstance(data, dict):
        raise AppError(
            ErrorCode.LLM_INVALID_OUTPUT,
            "LLM 返回的结构不对",
            "期望一个 JSON 对象。",
            ["重试一次", "或改用手动拆解"],
        )
    return data


async def complete_json(system: str, user: str) -> dict[str, Any]:
    """要求模型返回 JSON 对象。只支持 OpenAI 兼容协议与 Ollama。"""
    require_configured()
    provider = settings.llm_provider
    base = settings.llm_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            if provider == "ollama":
                resp = await http.post(
                    f"{base or 'http://127.0.0.1:11434'}/api/chat",
                    json={
                        "model": settings.llm_model,
                        "stream": False,
                        "format": "json",
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                    },
                )
                resp.raise_for_status()
                return _parse_json_object(resp.json()["message"]["content"])
            headers = {"Content-Type": "application/json"}
            if settings.llm_api_key:
                headers["Authorization"] = f"Bearer {settings.llm_api_key}"
            resp = await http.post(
                f"{base or 'https://api.openai.com/v1'}/chat/completions",
                headers=headers,
                json={
                    "model": settings.llm_model,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            resp.raise_for_status()
            return _parse_json_object(resp.json()["choices"][0]["message"]["content"])
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.LLM_UNAVAILABLE,
            "LLM 请求失败",
            f"{type(exc).__name__}: {exc}",
            ["确认服务地址与网络可达", "或改用手动拆解——手动路径不依赖 LLM"],
        ) from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise AppError(
            ErrorCode.LLM_INVALID_OUTPUT,
            "LLM 响应格式不认识",
            f"{type(exc).__name__}: {exc}",
            ["确认 provider 设置与实际服务匹配", "或改用手动拆解"],
        ) from exc
