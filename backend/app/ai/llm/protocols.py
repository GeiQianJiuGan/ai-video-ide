"""LLM 协议适配层：模型端的差异全部收在这一层。

原来 `client.py` 里那两个函数各自 `if provider == "ollama"`，于是「支持一个新协议」
等于在两处各加一支 if。照硬约束 1（业务层不绑定具体模型）与 `generation/providers/`
的做法，这里把协议做成适配器：`require()` 按设置挑一个，上层（`ai/director/agent.py`、
`services/*`）只认下面这几个动作——列模型 / 探活 / 出 JSON / 走一轮工具调用（流式或不流式）。

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

import base64
import json
from collections.abc import AsyncIterator
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


@dataclass(frozen=True, slots=True)
class ImagePart:
    """一张要让模型**真的看见**的图：字节 + 它是什么类型。

    与 `generation/providers/base.py::RefAsset` 不是一回事：那是给视频适配层的**路径**
    （文件要交给 ComfyUI / 云端出图服务去读），这里是要塞进 LLM 请求体的**字节**
    （四种方言都是 base64 内联，没有一家收本机路径）。

    `mime` 由调用方从后缀判断（`services/describe.py`）——图片文件不带 mime 时
    大多数端会直接 400，而这里猜不出比让它照实报错更糟。
    """

    mime: str
    data: bytes

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def data_url(self) -> str:
        """OpenAI 那一族要的 `data:` URL。"""
        return f"data:{self.mime};base64,{self.b64}"


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


def one_chunk(out: dict[str, Any]) -> list[dict[str, Any]]:
    """一次非流式返回 → 流式事件序列（整段文字算一块）。

    **只有这一个地方知道「不流式怎么冒充流式」**：基类的 `stream_tools` 与
    `ai/llm/client.py` 的退化分支共用它，形状不可能分叉。
    """
    text = str(out.get("content") or "")
    events: list[dict[str, Any]] = []
    if text:
        events.append({"type": "delta", "text": text})
    events.append(
        {"type": "final", "content": out.get("content"), "tool_calls": out.get("tool_calls") or []}
    )
    return events


def _sse_payload(chunk: str) -> Any | None:
    """一个 SSE 事件的 data 段 → JSON。

    心跳注释、`[DONE]` 这种非 JSON 的收尾标记回 None——它们不是失败，只是没内容。
    真正读不懂的一段也回 None：一条 delta 丢了不该毁掉整轮，而「一条都没读懂」
    在 `sse()` 收尾时会报出来。
    """
    if not chunk or chunk == "[DONE]":
        return None
    try:
        return json.loads(chunk)
    except json.JSONDecodeError:
        log.info("llm.sse 跳过一段读不懂的 data：%s", chunk[:120])
        return None


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
            raise self._timeout_error(url, limit, exc) from exc
        except httpx.HTTPError as exc:
            raise self._connect_error(url, exc) from exc
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

    async def sse(
        self,
        url: str,
        cfg: LlmConfig,
        json_body: Any,
        *,
        http_timeout: httpx.Timeout | None = None,
    ) -> AsyncIterator[Any]:
        """POST 一个 SSE 流，逐个事件 yield 出 data 段解析后的 JSON。

        与 `request()` 共用同一套错误归一（超时 / 连不上 / HTTP 4xx-5xx），所以流式
        路径上的失败照旧是四要素错误，而不是一句 `httpx.ReadError`。

        `data:` 按 SSE 规范可以有多行，所以攒到空行才算一个事件。**一个都没读懂时要报错**：
        那种「HTTP 200 但什么都没有」最容易被当成「模型没话说」，然后用户对着空气等。
        """
        limit = http_timeout or TIMEOUT
        buf: list[str] = []
        seen = 0
        try:
            async with _client(limit) as http:
                async with http.stream(
                    "POST", url, headers=self.headers(cfg), json=json_body
                ) as resp:
                    if resp.status_code >= 400:
                        # 出错时服务端给的是一整个 JSON 错误体：读完才有 .text 可看。
                        await resp.aread()
                        raise self._http_error(resp, url)
                    async for raw in resp.aiter_lines():
                        line = raw.rstrip("\r")
                        if line.startswith("data:"):
                            buf.append(line[5:].lstrip())
                            continue
                        if line.strip():
                            continue  # event: / id: / 注释——我们要的形状全在 data 里
                        payload = _sse_payload("\n".join(buf).strip())
                        buf.clear()
                        if payload is not None:
                            seen += 1
                            yield payload
        except httpx.TimeoutException as exc:
            raise self._timeout_error(url, limit, exc) from exc
        except httpx.HTTPError as exc:
            raise self._connect_error(url, exc) from exc
        # 末尾没有空行收尾的实现也不少，剩下那一段照样算一个事件。
        tail = _sse_payload("\n".join(buf).strip())
        if tail is not None:
            seen += 1
            yield tail
        if not seen:
            raise AppError(
                ErrorCode.LLM_INVALID_OUTPUT,
                "LLM 流式响应里一个事件都没有",
                f"{url}：连上了、也没报错，但没有任何可读的 data 段。",
                [
                    f"确认这个地址是 {self.label} 的接口，反代或网关可能把 SSE 缓冲掉了",
                    "重试一次；持续如此就在设置页换一个协议 / 换一个端",
                    MANUAL_WAY_OUT,
                ],
                {"url": url, "provider": self.name},
            )

    def _timeout_error(self, url: str, limit: httpx.Timeout, exc: Exception) -> AppError:
        # 超时和「连不上」不是一回事：地址是通的，模型只是写得比 TIMEOUT 还慢
        # （剧本拆解 + 本地模型最容易撞上）。下一步动作完全不同，所以分开报。
        return AppError(
            ErrorCode.LLM_UNAVAILABLE,
            f"LLM 服务超时（等了 {limit.read or limit.connect or 0:.0f} 秒）",
            f"{url}：{type(exc).__name__}: {exc}",
            [
                "这个端 / 这个模型这次写得太慢：换一个更快的模型，或把要拆的剧本分段再来一次",
                "本地模型（Ollama / LM Studio）第一次加载权重很慢，等它加载完再重试通常就过了",
                MANUAL_WAY_OUT,
            ],
            {"url": url, "provider": self.name, "timeout": limit.read},
        )

    def _connect_error(self, url: str, exc: Exception) -> AppError:
        return AppError(
            ErrorCode.LLM_UNAVAILABLE,
            "LLM 服务连不上",
            f"{url}：{type(exc).__name__}: {exc}",
            [
                "确认地址与端口正确（本机服务通常是 127.0.0.1）",
                f"{self.label} 的默认地址是 {self.default_base_url}——留空即用它",
                MANUAL_WAY_OUT,
            ],
            {"url": url, "provider": self.name},
        )

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
        data = await self.request(
            "POST",
            f"{self.base(cfg)}/chat/completions",
            cfg,
            json_body=self._chat_body(cfg, messages, tools),
        )
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

    def _chat_body(
        self, cfg: LlmConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": cfg.model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    async def stream_tools(
        self, cfg: LlmConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        """`stream: true` 下的 `choices[0].delta` 拼回内部规范形状。

        工具调用是**一片片来的**：名字在第一片里，参数是一串 JSON 片段，得按 index 攒齐
        才能解析——中途解析必然撞上「不是合法 JSON」。
        """
        body = {**self._chat_body(cfg, messages, tools), "stream": True}
        texts: list[str] = []
        slots: dict[Any, dict[str, str]] = {}
        order: list[Any] = []
        async for chunk in self.sse(f"{self.base(cfg)}/chat/completions", cfg, body):
            if not isinstance(chunk, dict):
                continue
            for choice in chunk.get("choices") or []:
                delta = (choice or {}).get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    texts.append(piece)
                    yield {"type": "delta", "text": piece}
                for raw in delta.get("tool_calls") or []:
                    key = raw.get("index")
                    if not isinstance(key, int):
                        # 少数端不给 index：带 id 的算新一条，没带的接在上一条后面。
                        key = len(order) if (raw.get("id") or not order) else order[-1]
                    if key not in slots:
                        slots[key] = {"id": "", "name": "", "args": ""}
                        order.append(key)
                    slot = slots[key]
                    if raw.get("id"):
                        slot["id"] = str(raw["id"])
                    fn = raw.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = str(fn["name"])
                    if isinstance(fn.get("arguments"), str):
                        slot["args"] += fn["arguments"]
        yield {
            "type": "final",
            "content": "".join(texts),
            "tool_calls": [
                {
                    "id": slots[k]["id"] or f"call_{i + 1}",
                    "name": slots[k]["name"],
                    "arguments": _tool_args(slots[k]["name"], slots[k]["args"]),
                }
                for i, k in enumerate(order)
                if slots[k]["name"]
            ],
        }

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

    async def describe_image(
        self, cfg: LlmConfig, system: str, user: str, images: list[ImagePart]
    ) -> str:
        # 图走 `image_url` 块里的 data URL：这一族没有「上传文件再引用」的必要，
        # 一张素材图几百 KB，内联最省事也最不容易在别处留一份副本。
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        content += [
            {"type": "image_url", "image_url": {"url": img.data_url}} for img in images
        ]
        data = await self.request(
            "POST",
            f"{self.base(cfg)}/chat/completions",
            cfg,
            json_body={
                "model": cfg.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
            },
        )
        return str(_dig(data, "choices", 0, "message", "content") or "").strip()


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
        data = await self.request(
            "POST",
            f"{self.base(cfg)}/messages",
            cfg,
            json_body=self._messages_body(cfg, messages, tools),
        )
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

    def _messages_body(
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
        return body

    async def stream_tools(
        self, cfg: LlmConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        """按块流：`content_block_start` 开一块（文本或 tool_use），`content_block_delta`
        往里面填（`text_delta` 是正文，`input_json_delta` 是工具参数的 JSON 片段）。

        它的 `error` 事件是**在 HTTP 200 之后**来的（比如中途 overloaded），
        所以这里必须认它——不然就成了「什么都没说就结束了」。
        """
        body = {**self._messages_body(cfg, messages, tools), "stream": True}
        texts: list[str] = []
        blocks: dict[int, dict[str, str]] = {}
        order: list[int] = []
        async for chunk in self.sse(f"{self.base(cfg)}/messages", cfg, body):
            if not isinstance(chunk, dict):
                continue
            kind = str(chunk.get("type") or "")
            if kind == "error":
                info = chunk.get("error") or {}
                raise AppError(
                    ErrorCode.LLM_UNAVAILABLE,
                    "Anthropic 在生成途中报错",
                    f"{info.get('type') or '未给出类型'}：{info.get('message') or chunk}",
                    ["重试一次（overloaded 多半是临时的）", "或换一个模型", MANUAL_WAY_OUT],
                    {"provider": self.name},
                )
            index = chunk.get("index")
            index = index if isinstance(index, int) else 0
            if kind == "content_block_start":
                start = chunk.get("content_block") or {}
                if start.get("type") == "tool_use":
                    blocks[index] = {
                        "id": str(start.get("id") or ""),
                        "name": str(start.get("name") or ""),
                        "args": "",
                    }
                    order.append(index)
                continue
            if kind != "content_block_delta":
                continue
            delta = chunk.get("delta") or {}
            if delta.get("type") == "text_delta":
                piece = str(delta.get("text") or "")
                if piece:
                    texts.append(piece)
                    yield {"type": "delta", "text": piece}
            elif delta.get("type") == "input_json_delta" and index in blocks:
                blocks[index]["args"] += str(delta.get("partial_json") or "")
        yield {
            "type": "final",
            "content": "".join(texts).strip(),
            "tool_calls": [
                {
                    "id": blocks[i]["id"],
                    "name": blocks[i]["name"],
                    # 无参工具那一块一个 delta 都没有，攒出来是空串。
                    "arguments": _tool_args(blocks[i]["name"], blocks[i]["args"] or "{}"),
                }
                for i in order
                if blocks[i]["name"]
            ],
        }

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

    async def describe_image(
        self, cfg: LlmConfig, system: str, user: str, images: list[ImagePart]
    ) -> str:
        # 图**排在文字前面**：Anthropic 自己的建议就是先给图再给问题，
        # 顺序反了它常常只答一句「我看到一张图片」。
        blocks: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": img.mime, "data": img.b64},
            }
            for img in images
        ]
        blocks.append({"type": "text", "text": user})
        data = await self.request(
            "POST",
            f"{self.base(cfg)}/messages",
            cfg,
            json_body={
                "model": cfg.model,
                "max_tokens": MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": blocks}],
            },
        )
        return _anthropic_text(data)


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

    def _stream_url(self, cfg: LlmConfig) -> str:
        # `alt=sse` 不加的话它回的是一个 JSON 数组流，不是 SSE。密钥照旧只在头里。
        return f"{self.base(cfg)}/models/{cfg.model}:streamGenerateContent?alt=sse"

    def _generate_body(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body = _gemini_body(messages)
        if tools:
            body["tools"] = [{"functionDeclarations": [_gemini_tool(t) for t in tools]}]
        return body

    async def complete_tools(
        self, cfg: LlmConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        data = await self.request(
            "POST", self._generate_url(cfg), cfg, json_body=self._generate_body(messages, tools)
        )
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

    async def stream_tools(
        self, cfg: LlmConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        """`streamGenerateContent?alt=sse`：每个事件都是一整个 GenerateContentResponse，
        `parts` 是增量。函数调用不切片，一来就是完整的一个。

        中途被安全过滤挡掉时它给的是**没有 candidates 的一片**——那不是「格式不认识」，
        所以照 `_gemini_parts` 的老规矩说清 blockReason。
        """
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        async for chunk in self.sse(
            self._stream_url(cfg), cfg, self._generate_body(messages, tools)
        ):
            if not isinstance(chunk, dict):
                continue
            for part in _gemini_parts(chunk):
                piece = part.get("text")
                if isinstance(piece, str) and piece:
                    texts.append(piece)
                    yield {"type": "delta", "text": piece}
                fc = part.get("functionCall")
                if isinstance(fc, dict):
                    args = fc.get("args")
                    calls.append(
                        {
                            "id": f"call_{len(calls) + 1}",
                            "name": str(fc.get("name") or ""),
                            "arguments": args if isinstance(args, dict) else {},
                        }
                    )
        yield {
            "type": "final",
            "content": "".join(texts).strip(),
            "tool_calls": [c for c in calls if c["name"]],
        }

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

    async def describe_image(
        self, cfg: LlmConfig, system: str, user: str, images: list[ImagePart]
    ) -> str:
        # 图是 `inline_data`（驼峰的 `inlineData` 也认，这里用下划线那一种——
        # v1beta 两种都收，下划线是文档里的写法）。**密钥照旧只在头里。**
        parts: list[dict[str, Any]] = [
            {"inline_data": {"mime_type": img.mime, "data": img.b64}} for img in images
        ]
        parts.append({"text": user})
        data = await self.request(
            "POST",
            self._generate_url(cfg),
            cfg,
            json_body={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts}],
            },
        )
        return "\n".join(
            str(p.get("text") or "") for p in _gemini_parts(data) if "text" in p
        ).strip()


# --- Ollama：本机端，不要密钥；tools 支持随模型而异，统一按不支持处理 ---


class Ollama(LlmProtocol):
    name = "ollama"
    label = "Ollama（本机）"
    default_base_url = "http://127.0.0.1:11434"
    models_path = "/api/tags"
    supports_tools = False
    #: 本机有视觉模型（llava / qwen-vl / gemma3 之类），但**主模型往往不是**——
    #: 所以设置里有一项 `llm.vision_model` 单独指一个，留空就用主模型。
    #: 主模型不认图时端会回 400，照 `_http_error` 说出来，不静默出一段瞎猜的描述。
    supports_vision = True
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

    async def describe_image(
        self, cfg: LlmConfig, system: str, user: str, images: list[ImagePart]
    ) -> str:
        # Ollama 的图不在 content 里，而是消息上一个 `images` 数组（纯 base64，
        # **不带 `data:` 前缀**——带了它会连前缀一起当图片数据解，直接报错）。
        data = await self.request(
            "POST",
            f"{self.base(cfg)}/api/chat",
            cfg,
            json_body={
                "model": cfg.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user, "images": [img.b64 for img in images]},
                ],
            },
        )
        return str(_dig(data, "message", "content") or "").strip()


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
