"""LLM 协议适配层：模型端的差异全部收在这一层。

原来 `client.py` 里那两个函数各自 `if provider == "ollama"`，于是「支持一个新协议」
等于在两处各加一支 if。照硬约束 1（业务层不绑定具体模型）与 `generation/providers/`
的做法，这里把协议做成适配器：`require()` 按设置挑一个，上层（`ai/director/agent.py`、
`services/*`）只认下面这四个动作——列模型 / 探活 / 出 JSON / 走一轮工具调用。

**内部规范形状就是 OpenAI 那一套**（`messages` / `tools` / `tool_calls`）：agent 已经
按它写了，它也是这几家里表达力最全的一个。适配器负责双向翻译，上层永远只看到
`{content, tool_calls: [{id, name, arguments}]}`。

四个协议：

  · `openai_compatible` —— OpenAI / vLLM / LM Studio / DeepSeek / 硅基流动…
    `GET /models` + `POST /chat/completions`，Bearer 认证，原生 tools。
  · `anthropic` —— `POST /messages`：system 是独立字段而不是一条消息，工具结果要拼成
    user 消息里的 `tool_result` 块，且**相邻同角色必须并成一条**（它要求角色交替）。
    没有 JSON 模式，靠提示 + `parse_json_object()` 截花括号兜住。
  · `gemini` —— `POST /models/{model}:generateContent`：角色叫 `user` / `model`，
    工具调用没有 id（我们自己编一个，再靠上一条 assistant 消息把 id 映射回函数名——
    `functionResponse` 认名字不认 id）。密钥走 `x-goog-api-key` 头，**不拼进 URL**，
    免得跟着报错信息与日志一起漏出去。
  · `ollama` —— `GET /api/tags` + `POST /api/chat`。tools 支持随模型而异，统一按
    「不支持」处理，退化成一次性 `complete_json()`（理由见 `ai/director/agent.py`）。

「模型自动获取」就是 `list_models()`：每个适配器知道自家列表长什么样，除了名字还给一个
`label`（Anthropic 的 display_name、Gemini 的 displayName、Ollama 的体积），
设置页直接列出来给人挑，不用手抄模型名。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

log = get_logger("llm.protocols")

#: 一次生成等多久。剧本拆解要一口气写完十几幕的 JSON，本地模型 / 带思考的模型上
#: 两分钟远远不够（超时的现场就是「拆解到一半断掉」），所以放到 5 分钟。
#: 连接超时照旧只有 5 秒——地址写错该立刻知道，不该陪着干等。
TIMEOUT = httpx.Timeout(300.0, connect=5.0)
#: 列模型是「点一下就该有反应」的动作，不能跟生成一样等几分钟。
LIST_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
#: Anthropic 的 /messages 要求必填 max_tokens：给一个够写完一份提案的值。
MAX_TOKENS = 4096
#: 每条 LLM 错误都要带上它——硬约束 2：AI 不可用不等于流程走不下去。
MANUAL_WAY_OUT = "AI 不可用时手动路径仍能走完全程（手动加一幕 / 手动拆解）"
#: provider = none 时的显示名。它不是一个协议，所以不进适配器表。
NONE = "none"
NONE_LABEL = "不使用（手动模式）"


@dataclass(frozen=True, slots=True)
class LlmConfig:
    """一次调用要的四件事。

    刻意不让适配器自己去读 `settings`：设置页要能「先取模型再保存」，
    那一次调用用的是用户刚敲进输入框、还没落盘的地址与密钥。
    """

    provider: str
    base_url: str = ""
    model: str = ""
    api_key: str = ""


def config(
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> LlmConfig:
    """当前设置 + 可选覆盖。覆盖只为「保存之前先试一下」，不会被写回任何地方。"""
    return LlmConfig(
        provider=str(provider if provider is not None else settings.llm_provider).strip(),
        base_url=str(base_url if base_url is not None else settings.llm_base_url).strip(),
        model=str(model if model is not None else settings.llm_model).strip(),
        api_key=str(api_key if api_key is not None else settings.llm_api_key).strip(),
    )


def _client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """唯一的 HTTP 出口，也是测试的接缝：换掉它就能塞 `httpx.MockTransport`。"""
    return httpx.AsyncClient(timeout=timeout)


def parse_json_object(text: str) -> dict[str, Any]:
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


def _snippet(resp: httpx.Response) -> str:
    try:
        return (resp.text or "").strip()[:300] or "（响应体是空的）"
    except (UnicodeDecodeError, httpx.ResponseNotRead):  # pragma: no cover - 极少数二进制响应
        return "（响应体读不出来）"


def _dig(data: Any, *path: Any) -> Any:
    """按路径取值，取不到就说清是在哪一步断的——「响应格式不认识」得能排查。"""
    cur = data
    for step in path:
        try:
            cur = cur[step]
        except (KeyError, IndexError, TypeError) as exc:
            trail = ".".join(str(p) for p in path)
            raise AppError(
                ErrorCode.LLM_INVALID_OUTPUT,
                "LLM 响应格式不认识",
                f"取 {trail} 时断在 {step!r}：{type(exc).__name__}"
                f"（预览：{json.dumps(data, ensure_ascii=False)[:200]}）",
                ["确认设置里的协议与实际服务匹配", MANUAL_WAY_OUT],
            ) from exc
    return cur


def _tool_args(name: Any, raw: Any) -> dict[str, Any]:
    """工具参数在不同端上有时是 dict、有时是一段 JSON 字符串。"""
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise AppError(
            ErrorCode.LLM_INVALID_OUTPUT,
            "工具参数不是合法 JSON",
            f"{name}: {exc.msg}（预览：{str(raw)[:200]}）",
            ["重试一次", "换一个更擅长工具调用的模型", MANUAL_WAY_OUT],
        ) from exc
    return parsed if isinstance(parsed, dict) else {}


class LlmProtocol:
    """一个 LLM 端要能做的四件事。

    子类只写自家的翻译；HTTP、超时与错误归一都在这儿，于是「绝不静默失败」
    这套四要素错误只写一遍，新协议自动继承。
    """

    #: 设置里存的值（`llm.provider`）。
    name = ""
    label = ""
    default_base_url = ""
    #: 列模型的路径，接在 base 后面。
    models_path = ""
    #: 能不能走 function calling。不能的话上层退化成一次性 `complete_json()`。
    supports_tools = False
    #: 没密钥能不能用（本机 Ollama 能，云端不能）。设置页用它决定要不要标必填。
    needs_key = True

    # --- 子类实现 ---

    def headers(self, cfg: LlmConfig) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def parse_models(self, data: Any) -> list[dict[str, str]]:
        raise NotImplementedError

    async def complete_json(self, cfg: LlmConfig, system: str, user: str) -> dict[str, Any]:
        raise NotImplementedError

    async def complete_tools(
        self, cfg: LlmConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        raise AppError(
            ErrorCode.LLM_UNAVAILABLE,
            "这个 LLM 端不支持工具调用",
            f"{self.label} 没有 function calling，走不了「边看边改」的多轮协作。",
            [
                "换成 OpenAI 兼容 / Anthropic / Gemini 协议即可用多轮工具调用",
                "或继续用它——不支持工具时会自动退化成一次性产出提案，形状完全一样",
                MANUAL_WAY_OUT,
            ],
            {"provider": self.name},
        )

    # --- 共用 ---

    def base(self, cfg: LlmConfig) -> str:
        return (cfg.base_url or self.default_base_url).rstrip("/")

    def models_url(self, cfg: LlmConfig) -> str:
        return f"{self.base(cfg)}{self.models_path}"

    def models_params(self) -> dict[str, str]:
        return {}

    async def list_models(self, cfg: LlmConfig) -> list[dict[str, str]]:
        """自动获取模型列表。同名的只留一条，顺序照服务端给的来（那个顺序常常有意义）。"""
        data = await self.request(
            "GET",
            self.models_url(cfg),
            cfg,
            params=self.models_params(),
            http_timeout=LIST_TIMEOUT,
        )
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in self.parse_models(data):
            if row["id"] and row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
        return rows

    async def probe(self, cfg: LlmConfig) -> dict[str, Any]:
        """设置页的「测试连接」：连通性 + 「你填的那个模型在不在」。"""
        items = await self.list_models(cfg)
        ids = {row["id"] for row in items}
        present = (cfg.model in ids) if ids and cfg.model else None
        return {
            "ok": True,
            "provider": self.name,
            "target": self.models_url(cfg),
            "model_count": len(items),
            "model_present": present,
            "detail": f"连通 · {len(items)} 个模型"
            + ("" if present is not False else f"，但其中没有 {cfg.model}——调用时会失败"),
        }

    async def request(
        self,
        method: str,
        url: str,
        cfg: LlmConfig,
        *,
        json_body: Any = None,
        params: dict[str, str] | None = None,
        # 刻意不叫 timeout：那个名字在 async 函数上会被读成「等多久放弃」，
        # 而它是 httpx 自己那套连接 / 读取超时。
        http_timeout: httpx.Timeout | None = None,
    ) -> Any:
        limit = http_timeout or TIMEOUT
        try:
            async with _client(limit) as http:
                resp = await http.request(
                    method,
                    url,
                    headers=self.headers(cfg),
                    json=json_body,
                    params=params or None,
                )
        except httpx.TimeoutException as exc:
            # 超时和「连不上」不是一回事：地址是通的，模型只是写得比 TIMEOUT 还慢
            # （剧本拆解 + 本地模型最容易撞上）。下一步动作完全不同，所以分开报。
            raise AppError(
                ErrorCode.LLM_UNAVAILABLE,
                f"LLM 服务超时（等了 {limit.read or limit.connect or 0:.0f} 秒）",
                f"{url}：{type(exc).__name__}: {exc}",
                [
                    "这个端 / 这个模型这次写得太慢：换一个更快的模型，或把要拆的剧本分段再来一次",
                    "本地模型（Ollama / LM Studio）第一次加载权重很慢，等它加载完再重试通常就过了",
                    MANUAL_WAY_OUT,
                ],
                {"url": url, "provider": self.name, "timeout": limit.read},
            ) from exc
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.LLM_UNAVAILABLE,
                "LLM 服务连不上",
                f"{url}：{type(exc).__name__}: {exc}",
                [
                    "确认地址与端口正确（本机服务通常是 127.0.0.1）",
                    f"{self.label} 的默认地址是 {self.default_base_url}——留空即用它",
                    MANUAL_WAY_OUT,
                ],
                {"url": url, "provider": self.name},
            ) from exc
        if resp.status_code >= 400:
            raise self._http_error(resp, url)
        try:
            return resp.json()
        except ValueError as exc:
            raise AppError(
                ErrorCode.LLM_INVALID_OUTPUT,
                "LLM 服务返回的不是 JSON",
                f"{url}：{_snippet(resp)}",
                [
                    f"确认这个地址是 {self.label} 的接口，而不是网页或反代的错误页",
                    "或在设置页换一个协议后重试",
                ],
                {"url": url, "provider": self.name},
            ) from exc

    def _http_error(self, resp: httpx.Response, url: str) -> AppError:
        """HTTP 4xx/5xx 的下一步动作按状态码分：401 是密钥，404 多半是协议或版本前缀选错了。"""
        code = resp.status_code
        if code in (401, 403):
            tips = [
                "检查 API Key 是否正确、是否与这个协议匹配",
                "有些端要求 Key 带前缀（sk- / AIza…），复制时别漏字符",
            ]
        elif code == 404:
            tips = [
                "检查地址的版本前缀（OpenAI 兼容端通常要以 /v1 结尾）",
                "或确认协议选对了——协议不对时路径也对不上",
                "模型名写错也会是 404，点「自动获取」列一下现有模型",
            ]
        elif code == 429:
            tips = ["超出了服务端的速率或额度限制，稍后重试", "或换一个模型 / 换一个端"]
        else:
            tips = ["把下面的原始报错和服务端日志对着看", "确认模型名在这个端上存在"]
        return AppError(
            ErrorCode.LLM_UNAVAILABLE,
            f"LLM 服务拒绝了这次请求（HTTP {code}）",
            f"{url}：{_snippet(resp)}",
            [*tips, MANUAL_WAY_OUT],
            {"url": url, "status": code, "provider": self.name},
        )


# --- OpenAI 兼容：内部规范形状就是它，所以这一个几乎不用翻译 ---


class OpenAiCompatible(LlmProtocol):
    name = "openai_compatible"
    label = "OpenAI 兼容"
    default_base_url = "https://api.openai.com/v1"
    models_path = "/models"
    supports_tools = True
    needs_key = True

    def headers(self, cfg: LlmConfig) -> dict[str, str]:
        head = {"Content-Type": "application/json"}
        if cfg.api_key:
            head["Authorization"] = f"Bearer {cfg.api_key}"
        return head

    def parse_models(self, data: Any) -> list[dict[str, str]]:
        rows = data.get("data") if isinstance(data, dict) else data
        out: list[dict[str, str]] = []
        for row in rows or []:
            # 有些自建端直接给一个字符串数组，别因为形状不同就报「格式不认识」。
            if isinstance(row, str):
                out.append({"id": row, "label": row})
            elif isinstance(row, dict):
                mid = str(row.get("id") or row.get("name") or "")
                label = str(row.get("display_name") or row.get("name") or mid)
                out.append({"id": mid, "label": label or mid})
        return out

    async def complete_tools(
        self, cfg: LlmConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": cfg.model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        data = await self.request("POST", f"{self.base(cfg)}/chat/completions", cfg, json_body=body)
        message = _dig(data, "choices", 0, "message")
        calls = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            calls.append(
                {
                    "id": str(raw.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "arguments": _tool_args(fn.get("name"), fn.get("arguments")),
                }
            )
        return {"content": message.get("content"), "tool_calls": calls}

    async def complete_json(self, cfg: LlmConfig, system: str, user: str) -> dict[str, Any]:
        data = await self.request(
            "POST",
            f"{self.base(cfg)}/chat/completions",
            cfg,
            json_body={
                "model": cfg.model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        return parse_json_object(str(_dig(data, "choices", 0, "message", "content") or ""))


# --- Anthropic：system 独立、工具结果是 user 消息里的块、角色必须交替 ---


def _anthropic_tool(spec: dict[str, Any]) -> dict[str, Any]:
    fn = spec.get("function") or spec
    return {
        "name": str(fn.get("name") or ""),
        "description": str(fn.get("description") or ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }


def _anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI 形状 → `(system, messages)`。

    两处必须翻译，不然请求直接 400：
      · system 不是一条消息而是顶层字段（可能有多条，拼起来）；
      · 角色必须交替——agent 一轮会追加 1 条 assistant + N 条 tool，
        那 N 条要并成**一条** user 消息里的 N 个 `tool_result` 块。
    """
    system_parts: list[str] = []
    turns: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
            return
        turns.append({"role": role, "content": blocks})

    for msg in messages:
        role = str(msg.get("role") or "")
        raw = msg.get("content")
        text = raw if isinstance(raw, str) else ""
        if role == "system":
            if text:
                system_parts.append(text)
        elif role == "tool":
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(msg.get("tool_call_id") or ""),
                        "content": text,
                    }
                ],
            )
        elif role == "assistant":
            blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text.strip() else []
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": str(fn.get("name") or ""),
                        "input": _tool_args(fn.get("name"), fn.get("arguments")),
                    }
                )
            push("assistant", blocks)
        elif text.strip():
            push("user", [{"type": "text", "text": text}])
    return "\n\n".join(system_parts), turns


def _anthropic_text(data: Any) -> str:
    blocks = _dig(data, "content")
    return "\n".join(
        str(b.get("text") or "")
        for b in blocks or []
        if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


class Anthropic(LlmProtocol):
    name = "anthropic"
    label = "Anthropic Claude"
    default_base_url = "https://api.anthropic.com/v1"
    models_path = "/models"
    supports_tools = True
    needs_key = True
    #: 这个头是必填的，缺了它 /messages 直接 400。
    version = "2023-06-01"

    def headers(self, cfg: LlmConfig) -> dict[str, str]:
        head = {"Content-Type": "application/json", "anthropic-version": self.version}
        if cfg.api_key:
            head["x-api-key"] = cfg.api_key
        return head

    def models_params(self) -> dict[str, str]:
        return {"limit": "1000"}

    def parse_models(self, data: Any) -> list[dict[str, str]]:
        rows = data.get("data") if isinstance(data, dict) else data
        out: list[dict[str, str]] = []
        for row in rows or []:
            if isinstance(row, dict):
                mid = str(row.get("id") or "")
                out.append({"id": mid, "label": str(row.get("display_name") or mid)})
        return out

    async def complete_tools(
        self, cfg: LlmConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        system, turns = _anthropic_messages(messages)
        body: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": MAX_TOKENS,
            "messages": turns,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [_anthropic_tool(t) for t in tools]
        data = await self.request("POST", f"{self.base(cfg)}/messages", cfg, json_body=body)
        calls = []
        for block in _dig(data, "content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                args = block.get("input")
                calls.append(
                    {
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "arguments": args if isinstance(args, dict) else {},
                    }
                )
        return {"content": _anthropic_text(data), "tool_calls": calls}

    async def complete_json(self, cfg: LlmConfig, system: str, user: str) -> dict[str, Any]:
        # 它没有 JSON 模式：把要求写进 system，剩下交给 parse_json_object 截花括号。
        data = await self.request(
            "POST",
            f"{self.base(cfg)}/messages",
            cfg,
            json_body={
                "model": cfg.model,
                "max_tokens": MAX_TOKENS,
                "system": f"{system}\n\n只输出一个 JSON 对象：不要用 ``` 包裹，不要写解释。",
                "messages": [{"role": "user", "content": user}],
            },
        )
        return parse_json_object(_anthropic_text(data))


# --- Gemini：角色叫 user / model，工具调用没有 id，schema 只认 OpenAPI 子集 ---


def _gemini_schema(node: Any) -> Any:
    """type 要大写，没听过的关键字（`additionalProperties` 之类）会让整条请求 400。"""
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key in ("description", "enum", "required", "nullable", "format"):
        if key in node:
            out[key] = node[key]
    if "type" in node:
        raw = node["type"]
        picked = raw[0] if isinstance(raw, list) and raw else raw
        out["type"] = str(picked).upper()
    if isinstance(node.get("properties"), dict):
        out["properties"] = {k: _gemini_schema(v) for k, v in node["properties"].items()}
    if "items" in node:
        out["items"] = _gemini_schema(node["items"])
    return out


def _gemini_tool(spec: dict[str, Any]) -> dict[str, Any]:
    fn = spec.get("function") or spec
    decl: dict[str, Any] = {
        "name": str(fn.get("name") or ""),
        "description": str(fn.get("description") or ""),
    }
    params = _gemini_schema(fn.get("parameters") or {})
    # 无参工具要**整个省掉** parameters：给一个空 properties 它会嫌 schema 不合法。
    if isinstance(params, dict) and params.get("properties"):
        decl["parameters"] = params
    return decl


def _gemini_body(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """OpenAI 形状 → `{contents, systemInstruction}`。

    `functionResponse` 认函数名不认 id，而 OpenAI 形状里那条 tool 消息只带
    `tool_call_id`——所以边走边记下 id → 名字（上一条 assistant 消息里有）。
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    names: dict[str, str] = {}

    def push(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
            return
        contents.append({"role": role, "parts": parts})

    for msg in messages:
        role = str(msg.get("role") or "")
        raw = msg.get("content")
        text = raw if isinstance(raw, str) else ""
        if role == "system":
            if text:
                system_parts.append(text)
        elif role == "assistant":
            parts: list[dict[str, Any]] = [{"text": text}] if text.strip() else []
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                names[str(call.get("id") or "")] = name
                parts.append(
                    {"functionCall": {"name": name, "args": _tool_args(name, fn.get("arguments"))}}
                )
            push("model", parts)
        elif role == "tool":
            cid = str(msg.get("tool_call_id") or "")
            push(
                "user",
                [{"functionResponse": {"name": names.get(cid, cid), "response": {"result": text}}}],
            )
        elif text.strip():
            push("user", [{"text": text}])
    body: dict[str, Any] = {"contents": contents}
    if system_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    return body


def _gemini_parts(data: Any) -> list[dict[str, Any]]:
    """安全过滤会把整条回答挡掉，此时没有 candidates——那不是「格式不认识」，要说清原因。"""
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not candidates:
        feedback = (data or {}).get("promptFeedback") or {} if isinstance(data, dict) else {}
        raise AppError(
            ErrorCode.LLM_INVALID_OUTPUT,
            "Gemini 没有返回任何内容",
            f"blockReason={feedback.get('blockReason') or '未给出'}"
            f"（预览：{json.dumps(data, ensure_ascii=False)[:200]}）",
            ["换一种说法重试——安全过滤是整条挡掉的", "或换一个模型", MANUAL_WAY_OUT],
        )
    parts = _dig(candidates, 0, "content", "parts")
    return [p for p in parts or [] if isinstance(p, dict)]


class Gemini(LlmProtocol):
    name = "gemini"
    label = "Google Gemini"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"
    models_path = "/models"
    supports_tools = True
    needs_key = True

    def headers(self, cfg: LlmConfig) -> dict[str, str]:
        head = {"Content-Type": "application/json"}
        if cfg.api_key:
            # 刻意走头而不是 ?key=：密钥不该出现在 URL 里，那会跟着报错信息与日志漏出去。
            head["x-goog-api-key"] = cfg.api_key
        return head

    def models_params(self) -> dict[str, str]:
        return {"pageSize": "200"}

    def parse_models(self, data: Any) -> list[dict[str, str]]:
        rows = data.get("models") if isinstance(data, dict) else data
        out: list[dict[str, str]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            methods = row.get("supportedGenerationMethods")
            # 只会做 embedding 之类的模型列出来只会让人挑错。
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
            mid = str(row.get("name") or "").split("models/")[-1]
            out.append({"id": mid, "label": str(row.get("displayName") or mid)})
        return out

    def _generate_url(self, cfg: LlmConfig) -> str:
        return f"{self.base(cfg)}/models/{cfg.model}:generateContent"

    async def complete_tools(
        self, cfg: LlmConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body = _gemini_body(messages)
        if tools:
            body["tools"] = [{"functionDeclarations": [_gemini_tool(t) for t in tools]}]
        data = await self.request("POST", self._generate_url(cfg), cfg, json_body=body)
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        for index, part in enumerate(_gemini_parts(data)):
            if "text" in part:
                texts.append(str(part.get("text") or ""))
            fc = part.get("functionCall")
            if isinstance(fc, dict):
                args = fc.get("args")
                # 它不给 id，我们自己编：agent 只是把它原样回传，我们再翻回函数名。
                calls.append(
                    {
                        "id": f"call_{index + 1}",
                        "name": str(fc.get("name") or ""),
                        "arguments": args if isinstance(args, dict) else {},
                    }
                )
        return {"content": "\n".join(t for t in texts if t).strip(), "tool_calls": calls}

    async def complete_json(self, cfg: LlmConfig, system: str, user: str) -> dict[str, Any]:
        data = await self.request(
            "POST",
            self._generate_url(cfg),
            cfg,
            json_body={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
        )
        text = "\n".join(str(p.get("text") or "") for p in _gemini_parts(data) if "text" in p)
        return parse_json_object(text)


# --- Ollama：本机端，不要密钥；tools 支持随模型而异，统一按不支持处理 ---


class Ollama(LlmProtocol):
    name = "ollama"
    label = "Ollama（本机）"
    default_base_url = "http://127.0.0.1:11434"
    models_path = "/api/tags"
    supports_tools = False
    needs_key = False

    def parse_models(self, data: Any) -> list[dict[str, str]]:
        rows = data.get("models") if isinstance(data, dict) else data
        out: list[dict[str, str]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            mid = str(row.get("name") or row.get("model") or "")
            size = row.get("size")
            # 本机模型是按体积挑的（显存装不下就跑不动），所以标上大小。
            label = (
                f"{mid} · {round(int(size) / (1024**3), 1)} GB"
                if isinstance(size, (int, float)) and size
                else mid
            )
            out.append({"id": mid, "label": label})
        return out

    async def complete_json(self, cfg: LlmConfig, system: str, user: str) -> dict[str, Any]:
        data = await self.request(
            "POST",
            f"{self.base(cfg)}/api/chat",
            cfg,
            json_body={
                "model": cfg.model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        return parse_json_object(str(_dig(data, "message", "content") or ""))


#: 名字 → 适配器。加一个协议只需要在这里多一行——上层与设置页都是按这张表画出来的。
BY_NAME: dict[str, LlmProtocol] = {
    p.name: p for p in (OpenAiCompatible(), Anthropic(), Gemini(), Ollama())
}


def names() -> list[str]:
    """设置页 `llm.provider` 的合法取值。`none` 排第一——它是默认，也是硬约束 2 的那一档。"""
    return [NONE, *BY_NAME]


def labels() -> list[str]:
    return [NONE_LABEL, *(p.label for p in BY_NAME.values())]


def listing() -> list[dict[str, Any]]:
    """设置页要的协议清单：默认地址、支不支持工具、要不要密钥、模型从哪列。"""
    rows: list[dict[str, Any]] = [
        {
            "name": NONE,
            "label": NONE_LABEL,
            "default_base_url": "",
            "supports_tools": False,
            "needs_key": False,
            "models_hint": "",
        }
    ]
    rows.extend(
        {
            "name": p.name,
            "label": p.label,
            "default_base_url": p.default_base_url,
            "supports_tools": p.supports_tools,
            "needs_key": p.needs_key,
            "models_hint": f"GET {p.default_base_url}{p.models_path}",
        }
        for p in BY_NAME.values()
    )
    return rows


def get(name: str | None = None) -> LlmProtocol | None:
    """挑一个适配器；`none` 与不认识的名字都回 None（判断「配没配」用得上）。"""
    return BY_NAME.get(str(name if name is not None else settings.llm_provider).strip())


def require(name: str | None = None) -> LlmProtocol:
    chosen = str(name if name is not None else settings.llm_provider).strip()
    found = BY_NAME.get(chosen)
    if found is not None:
        return found
    if chosen in ("", NONE):
        raise AppError(
            ErrorCode.LLM_UNAVAILABLE,
            "LLM 未配置",
            "当前协议 = none，没有可用的模型。",
            [
                "用「手动添加 Scene」走手动路径——数据结构与 AI 路径完全一致",
                "或在设置页选一个协议（OpenAI 兼容 / Anthropic / Gemini / Ollama）"
                "，填好地址后点「自动获取」挑一个模型",
            ],
            {"provider": chosen},
        )
    raise AppError(
        ErrorCode.VALIDATION_ERROR,
        "不认识的 LLM 协议",
        f"设置里的 llm.provider 是 {chosen!r}。",
        ["在设置页重新选择协议", f"可用的是：{'、'.join(names())}"],
        {"provider": chosen, "available": names()},
    )
