"""LLM 客户端（可选依赖）。

硬约束：LLM 不是必选项。默认 provider = none，此时任何 AI 入口都返回
LLM_UNAVAILABLE 这一条结构化错误，并在建议里指出手动路径同样能走完全程。

**协议差异不在这里**——它们全在 `protocols.py`（openai_compatible / anthropic /
gemini / ollama）。这一层只做三件事：判断「配没配」、按设置挑一个协议、把调用转过去。
所以支持一个新协议不需要碰这个文件，上层（`ai/director/agent.py`、`services/*`）
更不需要知道对面是谁。
"""

from __future__ import annotations

from typing import Any

from app.ai.llm import protocols
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

log = get_logger("llm")

#: 兼容旧引用：截花括号解析 JSON 的实现搬到了 protocols（适配器都要用它）。
_parse_json_object = protocols.parse_json_object


def status() -> dict[str, Any]:
    """给「LLM 未配置 · 手动模式同样可以走完全部流程」那行灰字用。"""
    proto = protocols.get()
    configured = proto is not None and bool(settings.llm_model)
    return {
        "configured": configured,
        "provider": settings.llm_provider,
        "label": proto.label if proto else protocols.NONE_LABEL,
        "model": settings.llm_model or None,
        #: 不支持工具的端会退化成一次性产出提案，界面上要说清这件事。
        "supports_tools": bool(proto and proto.supports_tools),
        "hint": (
            f"已配置 {proto.label} · {settings.llm_model}"
            if configured and proto
            else "LLM 未配置 · 手动模式同样可以走完全部流程"
        ),
    }


def require_configured() -> None:
    """三种「没配好」分开说：没选协议 / 协议名不认识 / 选了协议但没挑模型。"""
    proto = protocols.require()  # none 与不认识的名字在这里各报各的
    if not settings.llm_model:
        raise AppError(
            ErrorCode.LLM_UNAVAILABLE,
            "还没有选模型",
            f"协议是 {proto.label}，但没有模型名——没有模型名视为未配置。",
            [
                "在设置页的「模型」旁点「自动获取」，从列表里挑一个",
                "或直接填模型名（自建端的模型不一定列得出来）",
                "手动路径不依赖 LLM，随时可以走完全程",
            ],
            {"provider": proto.name},
        )


def supports_tools() -> bool:
    """这个端能不能走 function calling。

    不能的话上层退化成一次性 `complete_json()` 产出 ops 数组——两条路产出的提案
    形状完全一样，用户审阅时看不出区别（见 `ai/director/agent.py` 的注释）。
    Ollama 的 `/api/chat` 对 tools 的支持随模型而异，统一算作不支持。
    """
    proto = protocols.get()
    return bool(proto and proto.supports_tools)


async def complete_tools(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> dict[str, Any]:
    """function calling **一轮**。循环由调用方控制（见 ai/director/agent.py）。

    入参与返回都是 OpenAI 形状（`{content, tool_calls: [{id, name, arguments}]}`），
    翻译成对面认的样子是适配器的事。
    """
    require_configured()
    proto = protocols.require()
    return await proto.complete_tools(protocols.config(), messages, tools)


async def complete_json(system: str, user: str) -> dict[str, Any]:
    """要求模型返回一个 JSON 对象。没有 JSON 模式的端靠提示 + 截花括号兜住。"""
    require_configured()
    proto = protocols.require()
    return await proto.complete_json(protocols.config(), system, user)


async def list_models(
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """设置页的「自动获取模型」。

    允许传入**还没保存**的协议 / 地址 / 密钥：不然用户得先存一份可能是错的配置
    才能看到模型列表。这些覆盖只用于这一次请求，不写回 settings.json。
    """
    cfg = protocols.config(provider=provider, base_url=base_url, api_key=api_key)
    proto = protocols.require(cfg.provider)
    items = await proto.list_models(cfg)
    ids = {row["id"] for row in items}
    log.info("llm.models", provider=cfg.provider, count=len(items))
    return {
        "provider": cfg.provider,
        "label": proto.label,
        "target": proto.models_url(cfg),
        "count": len(items),
        "items": items,
        "current": cfg.model or None,
        "current_present": (cfg.model in ids) if cfg.model and ids else None,
    }


async def probe() -> dict[str, Any]:
    """设置页的「测试连接」：连通性 + 「你填的那个模型在不在」。"""
    require_configured()
    proto = protocols.require()
    return await proto.probe(protocols.config())
