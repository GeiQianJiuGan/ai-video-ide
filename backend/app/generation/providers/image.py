"""图片生成适配层：**第三条生成链**（角色四视图 / 地点参考图 / 道具图 / 首尾帧候选）。

在这一轮之前，工程一张图都生不出来：角色四视图、地点参考图、道具图全靠用户自己在别处生成
再手动导入。而这些图不是装饰——`services/context.py::_assign_roles` 把它们当参考素材喂进
`AIVS_REF_*`，只喂一张首帧的镜头在几秒里就把人物形象丢掉了。

**形状照 `app/ai/llm/protocols.py` 那张协议表写，不是照 `providers/http_api.py` 写**：
后者要求**服务端**来适配我们的合同，接不了云端现成的 API，而「兼容市面上所有出图 API」
恰恰是这条链的目标。三条不许绕的规矩，与 LLM 协议表同一条口径：

  1. **协议表是唯一真源**（`BY_NAME`）：默认地址、要不要密钥、请求体形状、返回里图在哪、
     模型列表从哪来，全写在这张表里。`GET /settings` 把它投影成 `image_protocols[]`
     给前端画界面——**加一家 API 只改这一个 dict，前端一行不动**。
  2. **密钥只走请求头**，任何协议都不放进 URL（Gemini 刻意不用 `?key=`）——进了 URL
     就会跟着日志和四要素错误一起漏出去。
  3. **不支持参考图不等于用不了**：`supports_refs=False` 的端把参考图写进 `req.notes`
     降级，照旧出图，不失败（照 `providers/audio.py` 那条「槽位不够只降级」的规矩）。

**云端出图 API 绝大多数是同步的**（一次 POST 就回图），而队列那套「提交 → 轮询 → 取回」
是围绕异步任务写的。所以这里有一层薄壳：`submit()` 真的把图生出来并把字节按 task_id 存在
内存里，`poll()` 立刻回 done，`fetch()` 把它弹出来。这层壳只存在于本文件内部——业务层看到的
仍然只有 `ImageProvider` 那四个方法，于是 `generation._await_task()` 那个轮询循环
（取消检查、进度事件、失败翻译）一行不改就能给图片链用。

`comfy_preset` 那一支是真异步的（ComfyUI 本来就是任务制），它直接继承
`ComfyPresetProvider`，只覆写 `probe()` / `submit()`——照 `providers/audio.py` 那 30 行。
"""

from __future__ import annotations

import base64
import binascii
import copy
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.generation.comfy.client import ComfyClient
from app.generation.providers import presets
from app.generation.providers.base import ImageRequest, TaskState
from app.generation.providers.comfy_preset import ComfyPresetProvider

log = get_logger("provider.image")

#: 一张图等多久。比视频短得多（出图是秒级到一分钟），但云端排队也会拖到几分钟。
TIMEOUT = httpx.Timeout(300.0, connect=5.0)
#: 列模型是「点一下就该有反应」的动作，不能跟出图一样等几分钟。
LIST_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
#: 每条图片错误都要带上它——硬约束 2 的同一个作风：服务不可用不等于流程走不下去。
MANUAL_WAY_OUT = "手动路径不受影响：在角色 / 地点 / 道具页直接上传一张图即可"
#: provider = none 时的显示名。它不是一个协议，所以不进适配器表。
NONE = "none"
NONE_LABEL = "不配置（手动导入图片）"

#: 通用 REST 合同（`http_api` 那一支）。服务端按这两个端点实现就能当出图后端。
IMAGE_CONTRACT = [
    "POST {base}/images/generate → {image: base64} 或 {images:[…]} 或 {output_url}",
    "GET {base}/health → 2xx",
]

#: 常见 MIME → 后缀。落盘文件名靠它，认不出就当 png（各家默认都是 png）。
EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass(frozen=True, slots=True)
class ImageConfig:
    """一次出图要的几件事。

    刻意不让适配器自己去读 `settings`：设置页要能「先取模型 / 先探测再保存」，
    那一次调用用的是用户刚敲进输入框、还没落盘的地址与密钥（照 `LlmConfig` 的理由）。
    """

    protocol: str
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    preset: str = ""
    size: str = "1024x1024"


def config(
    protocol: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    preset: str | None = None,
    size: str | None = None,
) -> ImageConfig:
    """当前设置 + 可选覆盖。覆盖只为「保存之前先试一下」，不会被写回任何地方。"""
    return ImageConfig(
        protocol=str(protocol if protocol is not None else settings.image_provider).strip(),
        base_url=str(base_url if base_url is not None else settings.image_base_url).strip(),
        model=str(model if model is not None else settings.image_model).strip(),
        api_key=str(api_key if api_key is not None else settings.image_api_key).strip(),
        preset=str(preset if preset is not None else settings.image_preset).strip(),
        size=str(size if size is not None else settings.image_size).strip() or "1024x1024",
    )


def _client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """唯一的 HTTP 出口，也是测试的接缝：换掉它就能把整条链关在机器里跑。"""
    return httpx.AsyncClient(timeout=timeout)


def _snippet(resp: httpx.Response) -> str:
    try:
        return (resp.text or "").strip()[:300] or "（响应体是空的）"
    except (UnicodeDecodeError, httpx.ResponseNotRead):  # pragma: no cover - 二进制响应
        return "（响应体读不出来）"


def _new_task_id() -> str:
    """同步端那层壳的任务 id。**刻意不用 `new_id()`**：它不是一个实体，不进库、不进 URL，
    只在本进程内活到 `fetch()` 把结果弹出来为止。"""
    return f"img-{uuid.uuid4().hex[:16]}"


def _decode(raw: str, where: str, protocol: str) -> bytes:
    """把 base64 解成字节。**认不出来要说清是哪一处给的**，别抛一个裸的 binascii 错误。

    有些端会带上 `data:image/png;base64,` 前缀，这里顺手削掉——为此让整张图落不下来
    太不值得。
    """
    text = str(raw or "").strip()
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        data = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise AppError(
            ErrorCode.WORKFLOW_ERROR,
            "图片服务返回的图片解不开",
            f"{where} 给的不是合法 base64：{type(exc).__name__}: {exc}（预览：{text[:80]}）",
            ["确认设置里的协议与实际服务匹配", MANUAL_WAY_OUT],
            {"protocol": protocol},
        ) from exc
    if not data:
        raise AppError(
            ErrorCode.WORKFLOW_ERROR,
            "图片服务返回了空图片",
            f"{where} 给的 base64 解出来是 0 字节。",
            ["重试一次", "或换一个模型 / 换一个端", MANUAL_WAY_OUT],
            {"protocol": protocol},
        )
    return data


def _b64(path_bytes: bytes) -> str:
    return base64.b64encode(path_bytes).decode("ascii")


def _read_ref(ref: Any) -> bytes:
    """读一个参考图的字节。文件不在就报 `MISSING_ASSET`——照 `http_api._encode` 的口径。"""
    path = ref.path
    if not path.is_file():
        raise AppError(
            ErrorCode.MISSING_ASSET,
            "参考图不在磁盘上",
            f"{path} 找不到。",
            ["确认该资产文件还在工程目录里", "或去掉这张参考图再生成"],
            {"path": path.as_posix()},
        )
    return path.read_bytes()


def _first_image(data: Any) -> tuple[str, str] | None:
    """从一份 JSON 响应里把第一张图挖出来，回 `("b64" | "url", 值)`。

    **各家的键位置全不一样**，而它们又都在往彼此的形状上靠（`data[].b64_json`、
    `images[]`、`image`、`output_url`…）。所以这里按已知的几种位置依次找，
    找不到的交给调用方报「响应格式不认识」——挖图这件事只写这一份。
    """
    if not isinstance(data, dict):
        return None
    rows = data.get("data") or data.get("images") or data.get("output") or []
    if isinstance(rows, str):
        rows = [rows]
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, str) and row:
                return ("url", row) if row.startswith(("http://", "https://")) else ("b64", row)
            if isinstance(row, dict):
                for key in ("b64_json", "b64", "image", "data", "base64"):
                    if row.get(key):
                        return ("b64", str(row[key]))
                for key in ("url", "output_url", "image_url"):
                    if row.get(key):
                        return ("url", str(row[key]))
    for key in ("image", "b64_json", "base64", "image_base64"):
        if isinstance(data.get(key), str) and data[key]:
            return ("b64", str(data[key]))
    for key in ("output_url", "url", "image_url"):
        if isinstance(data.get(key), str) and data[key]:
            return ("url", str(data[key]))
    return None


class ImageProtocol:
    """一个出图端要能做的事。子类只写自家的方言（`_generate`），HTTP、超时、错误归一、
    「同步端 → 任务形状」那层壳都在这儿——于是四要素错误只写一遍，新协议自动继承。
    """

    #: 设置里存的值（`image.provider`）。
    name = ""
    label = ""
    default_base_url = ""
    #: 列模型的路径，接在 base 后面。留空 = 这个端列不了模型（设置页不画那个按钮）。
    models_path = ""
    #: 探活路径（没有模型列表的端用它）。
    health_path = "/health"
    #: 没密钥能不能用。设置页用它决定要不要标必填。
    needs_key = True
    #: 收不收参考图（图生图 / 风格参考）。收不了的端只降级，不失败。
    supports_refs = True
    #: 要不要在设置页里指一份本机预设（只有 ComfyUI 那一支要）。
    wants_preset = False

    def __init__(self) -> None:
        #: task_id → (文件名, 字节)。同步端那层壳的全部状态，`fetch()` 弹出即清。
        self._results: dict[str, tuple[str, bytes]] = {}

    # --- 子类实现 ---

    async def _generate(self, cfg: ImageConfig, req: ImageRequest) -> tuple[str, bytes]:
        """真正出图的那一下。回 (文件名, 字节)。"""
        raise NotImplementedError

    def parse_models(self, data: Any) -> list[dict[str, str]]:
        return []

    def headers(self, cfg: ImageConfig) -> dict[str, str]:
        """**密钥只在这里进请求头**，任何协议都不许把它拼进 URL。"""
        return {"Content-Type": "application/json"}

    # --- 共用 ---

    def base(self, cfg: ImageConfig) -> str:
        target = (cfg.base_url or self.default_base_url).rstrip("/")
        if not target:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                "还没有配置图片服务地址",
                f"{self.label} 需要一个服务地址，设置里是空的，且这个协议没有默认地址。",
                [
                    "在设置页的「图片生成 API」里填写地址",
                    *(f"服务端需要实现：{line}" for line in IMAGE_CONTRACT),
                    MANUAL_WAY_OUT,
                ],
                {"protocol": self.name},
            )
        return target

    def model(self, cfg: ImageConfig) -> str:
        return cfg.model

    def require_model(self, cfg: ImageConfig) -> str:
        name = self.model(cfg)
        if not name:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                "还没有选图片模型",
                f"{self.label} 必须指定模型名，设置里是空的。",
                [
                    "在设置页的「图片生成 API」里点「自动获取」挑一个模型",
                    MANUAL_WAY_OUT,
                ],
                {"protocol": self.name},
            )
        return name

    async def list_models(self, cfg: ImageConfig) -> list[dict[str, str]]:
        """自动获取模型列表。列不了的端回空列表——那不是错误，设置页照旧让人手填模型名。"""
        if not self.models_path:
            return []
        data = await self.request(
            "GET", f"{self.base(cfg)}{self.models_path}", cfg, http_timeout=LIST_TIMEOUT
        )
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in self.parse_models(data):
            if row["id"] and row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
        return rows

    async def probe_with(self, cfg: ImageConfig) -> dict[str, Any]:
        """设置页的「测试连接」：连通性 +「你填的那个模型在不在」。"""
        if self.models_path:
            items = await self.list_models(cfg)
            ids = {row["id"] for row in items}
            present = (cfg.model in ids) if ids and cfg.model else None
            return {
                "ok": True,
                "provider": self.name,
                "target": f"{self.base(cfg)}{self.models_path}",
                "model_count": len(items),
                "model_present": present,
                "detail": f"图片服务已连接 · {len(items)} 个模型"
                + ("" if present is not False else f"，但其中没有 {cfg.model}——出图时会失败"),
            }
        target = f"{self.base(cfg)}{self.health_path}"
        await self.request("GET", target, cfg, http_timeout=LIST_TIMEOUT, want_json=False)
        return {
            "ok": True,
            "provider": self.name,
            "target": target,
            "model_count": 0,
            "model_present": None,
            "detail": f"图片服务已连接（{self.base(cfg)}）",
        }

    async def probe(self) -> dict[str, Any]:
        return await self.probe_with(config())

    def provider(self) -> Any:
        """给 `registry.image_provider()` 用的那个实例。

        HTTP 那几支自己就是 provider（四个方法都在这个类上），所以回 `self`；
        ComfyUI 那一支要回一个 `ComfyPresetProvider` 的子类——它的轮询与取回是
        ComfyUI 的 history / view，与这层同步壳完全不同（见 `ComfyImages.provider`）。
        """
        return self

    # --- 「同步端 → 任务形状」那层壳 ---

    async def submit(self, req: ImageRequest, *, client_id: str) -> str:
        """出图并把结果存起来，回一个本进程内的 task_id。

        `client_id` 用不上（同步端没有 WS 频道这回事），但签名必须与 `VideoProvider`
        一致——那是 `generation._await_task()` 能复用的前提。
        """
        cfg = config()
        if req.refs and not self.supports_refs:
            names = "、".join(r.label or r.path.name for r in req.refs)
            req.notes.append(
                f"{self.label} 这条路收不了参考图，账单里 {len(req.refs)} 张没有喂进去：{names}。"
                "出来的图只由提示词决定。"
            )
            log.info("provider.image_refs_unsupported", protocol=self.name, refs=len(req.refs))
        filename, data = await self._generate(cfg, req)
        task_id = _new_task_id()
        self._results[task_id] = (filename, data)
        log.info(
            "provider.image_submitted",
            protocol=self.name,
            task_id=task_id,
            bytes=len(data),
            refs=len(req.refs),
        )
        return task_id

    async def poll(self, task_id: str) -> TaskState:
        """同步端在 `submit()` 里就出完了，所以这里只有两种答案。"""
        if task_id in self._results:
            return TaskState("done", 1.0, "已出图")
        return TaskState(
            "failed",
            1.0,
            f"任务 {task_id} 的结果不在这个进程里了（后端可能重启过）。",
            raw={"task_id": task_id},
        )

    async def fetch(self, task_id: str) -> tuple[str, bytes]:
        found = self._results.pop(task_id, None)
        if found is None:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "图片结果已经取走或丢失了",
                f"task_id={task_id} 不在本进程的结果表里（后端重启会清空它）。",
                ["重新生成一次", MANUAL_WAY_OUT],
                {"task_id": task_id, "protocol": self.name},
            )
        return found

    # --- HTTP ---

    async def request(
        self,
        method: str,
        url: str,
        cfg: ImageConfig,
        *,
        json_body: Any = None,
        files: Any = None,
        form: dict[str, Any] | None = None,
        # 刻意不叫 timeout：那个名字在 async 函数上会被读成「等多久放弃」，
        # 而它是 httpx 自己那套连接 / 读取超时（照 `llm/protocols.py` 的口径）。
        http_timeout: httpx.Timeout | None = None,
        want_json: bool = True,
    ) -> Any:
        limit = http_timeout or TIMEOUT
        head = dict(self.headers(cfg))
        if files is not None:
            # multipart 的 Content-Type 由 httpx 带 boundary 生成，手写会让服务端解不开
            head.pop("Content-Type", None)
        try:
            async with _client(limit) as http:
                resp = await http.request(
                    method, url, headers=head, json=json_body, data=form, files=files
                )
        except httpx.TimeoutException as exc:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                f"图片服务超时（等了 {limit.read or limit.connect or 0:.0f} 秒）",
                f"{url}：{type(exc).__name__}: {exc}",
                [
                    "这个端这次太慢：稍后重试，或换一个更快的模型",
                    "本机出图（ComfyUI）第一次加载权重很慢，加载完再试通常就过了",
                    MANUAL_WAY_OUT,
                ],
                {"url": url, "protocol": self.name},
            ) from exc
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.COMFY_OFFLINE,
                "图片服务连不上",
                f"{url}：{type(exc).__name__}: {exc}",
                [
                    "确认地址与端口正确（本机服务通常是 127.0.0.1）",
                    f"{self.label} 的默认地址是 {self.default_base_url or '（没有默认地址）'}",
                    MANUAL_WAY_OUT,
                ],
                {"url": url, "protocol": self.name},
            ) from exc
        if resp.status_code >= 400:
            raise self._http_error(resp, url)
        if not want_json:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "图片服务返回的不是 JSON",
                f"{url}：{_snippet(resp)}",
                [
                    f"确认这个地址是 {self.label} 的接口，而不是网页或反代的错误页",
                    "或在设置页换一个协议后重试",
                ],
                {"url": url, "protocol": self.name},
            ) from exc

    def _http_error(self, resp: httpx.Response, url: str) -> AppError:
        """4xx/5xx 的下一步动作按状态码分：401 是密钥，404 多半是协议或版本前缀选错了。"""
        code = resp.status_code
        if code in (401, 403):
            tips = [
                "检查 API Key 是否正确、是否与这个协议匹配",
                "有些端要求 Key 带前缀（sk- / AIza…），复制时别漏字符",
            ]
        elif code == 404:
            tips = [
                "检查地址的版本前缀（OpenAI 兼容端通常要以根地址填到域名即可，路径由协议拼）",
                "或确认协议选对了——协议不对时路径也对不上",
                "模型名写错也会是 404，点「自动获取」列一下现有模型",
            ]
        elif code == 429:
            tips = ["超出了服务端的速率或额度限制，稍后重试", "或换一个模型 / 换一个端"]
        elif code == 400:
            tips = [
                "把下面的原始报错读一遍：出图端最常拒绝的是画幅（size）与模型名",
                "有些端只接受固定几档画幅，在设置页把「图片尺寸」改成它支持的那一档",
            ]
        else:
            tips = ["把下面的原始报错和服务端日志对着看", "确认模型名在这个端上存在"]
        return AppError(
            ErrorCode.WORKFLOW_ERROR,
            f"图片服务拒绝了这次请求（HTTP {code}）",
            f"{url}：{_snippet(resp)}",
            [*tips, MANUAL_WAY_OUT],
            {"url": url, "status": code, "protocol": self.name},
        )

    async def _download(self, cfg: ImageConfig, url: str) -> bytes:
        """有些端只回一个图片地址。**必须自己下回来**：素材要落进工程，不能只存在服务端
        （那个地址往往几小时后就失效）。"""
        try:
            async with _client(TIMEOUT) as http:
                resp = await http.get(url, headers=self.headers(cfg))
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "图片下载失败",
                f"{url}：{type(exc).__name__}: {exc}",
                ["重新生成一次（很多端的图片地址有有效期）", MANUAL_WAY_OUT],
                {"url": url, "protocol": self.name},
            ) from exc
        return resp.content

    async def _take(self, cfg: ImageConfig, data: Any, where: str) -> tuple[str, bytes]:
        """把响应里的那张图变成 (文件名, 字节)。base64 与地址两种形态都收。"""
        found = _first_image(data)
        if found is None:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "图片服务的响应里找不到图",
                f"{where} 返回的 JSON 里没有 data[].b64_json / images[] / image / output_url："
                f"{str(data)[:400]}",
                ["确认设置里的协议与实际服务匹配", "展开原始响应对着服务端文档看", MANUAL_WAY_OUT],
                {"protocol": self.name},
            )
        how, value = found
        if how == "url":
            target = value if value.startswith(("http://", "https://")) else None
            if target is None:
                target = f"{self.base(cfg)}/{value.lstrip('/')}"
            return _filename_of(target), await self._download(cfg, target)
        return f"{_new_task_id()}.png", _decode(value, where, self.name)


def _filename_of(url: str) -> str:
    """从地址里取一个像样的文件名。取不到就编一个 png——落盘要有后缀，
    `assets.register_bytes` 靠它判类型。"""
    tail = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return tail if tail and "." in tail else f"{_new_task_id()}.png"


def _mime_of(name: str) -> str:
    """按后缀猜 MIME。multipart 与 Gemini 的 `inline_data` 都要它，认不出当 png。"""
    lower = str(name or "").lower()
    for mime, ext in EXT_BY_MIME.items():
        if lower.endswith(ext):
            return mime
    return "image/png"


# --- OpenAI 兼容：`/v1/images/generations`，有参考图时改走 `/v1/images/edits` ---


class OpenAiImages(ImageProtocol):
    """OpenAI 及一众照它形状实现的端（Azure 转发层、硅基流动、各家聚合网关…）。

    **`response_format` 刻意不发**：新模型（gpt-image-1）会因为它直接 400，而各家默认
    回的就是 `b64_json`；回地址的端由 `_take()` 自己下回来。少发一个字段能多接一批端。
    """

    name = "openai_images"
    label = "OpenAI 兼容出图（/v1/images）"
    default_base_url = "https://api.openai.com"
    models_path = "/v1/models"
    needs_key = True
    supports_refs = True

    def headers(self, cfg: ImageConfig) -> dict[str, str]:
        head = {"Content-Type": "application/json"}
        if cfg.api_key:
            head["Authorization"] = f"Bearer {cfg.api_key}"
        return head

    def parse_models(self, data: Any) -> list[dict[str, str]]:
        rows = data.get("data") if isinstance(data, dict) else None
        out: list[dict[str, str]] = []
        for row in rows or []:
            if isinstance(row, dict) and row.get("id"):
                out.append({"id": str(row["id"]), "label": str(row.get("owned_by") or "")})
        return out

    async def _generate(self, cfg: ImageConfig, req: ImageRequest) -> tuple[str, bytes]:
        base, model = self.base(cfg), self.require_model(cfg)
        if req.negative:
            # `/v1/images` 这套没有负向字段。**不能静默丢掉**：negative 里写着「不要文字、
            # 不要水印」，丢了就等于放开了那些。并进正向并留一条 note。
            req.notes.append("这个端没有负向提示词字段，已把负向内容并进正向（写成「避免：…」）。")
        prompt = req.prompt if not req.negative else f"{req.prompt}\n避免出现：{req.negative}"
        if req.refs:
            url = f"{base}/v1/images/edits"
            files = [
                ("image[]", (ref.path.name, _read_ref(ref), _mime_of(ref.path.name)))
                for ref in req.refs
            ]
            data = await self.request(
                "POST",
                url,
                cfg,
                form={"model": model, "prompt": prompt, "size": req.size, "n": "1"},
                files=files,
            )
        else:
            url = f"{base}/v1/images/generations"
            body: dict[str, Any] = {"model": model, "prompt": prompt, "size": req.size, "n": 1}
            body.update(req.extra.get("body") or {})
            data = await self.request("POST", url, cfg, json_body=body)
        return await self._take(cfg, data, f"POST {url}")


# --- Google Gemini：`:generateContent`，参考图走 `inline_data` ---


class GeminiImages(ImageProtocol):
    """Gemini 那套（`gemini-2.5-flash-image` 一类）。

    **密钥走 `x-goog-api-key`，刻意不用 `?key=`**：进了 URL 就会跟着日志与四要素错误
    一起漏出去（与 `llm/protocols.py::Gemini` 同一条规矩）。
    """

    name = "gemini"
    label = "Google Gemini 出图"
    default_base_url = "https://generativelanguage.googleapis.com"
    models_path = "/v1beta/models"
    needs_key = True
    supports_refs = True

    def headers(self, cfg: ImageConfig) -> dict[str, str]:
        head = {"Content-Type": "application/json"}
        if cfg.api_key:
            head["x-goog-api-key"] = cfg.api_key
        return head

    def parse_models(self, data: Any) -> list[dict[str, str]]:
        rows = data.get("models") if isinstance(data, dict) else None
        out: list[dict[str, str]] = []
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            # 这个端回的是 "models/gemini-…"，设置里存的是后面那一段
            ident = str(row["name"]).split("/")[-1]
            out.append({"id": ident, "label": str(row.get("displayName") or "")})
        return out

    async def _generate(self, cfg: ImageConfig, req: ImageRequest) -> tuple[str, bytes]:
        base, model = self.base(cfg), self.require_model(cfg)
        url = f"{base}/v1beta/models/{model}:generateContent"
        text = req.prompt
        if req.negative:
            req.notes.append("这个端没有负向提示词字段，已把负向内容并进正向（写成「避免：…」）。")
            text = f"{text}\n避免出现：{req.negative}"
        width, height = req.size_wh()
        parts: list[dict[str, Any]] = [{"text": f"{text}\n图片尺寸约 {width}x{height} 像素。"}]
        for ref in req.refs:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": _mime_of(ref.path.name),
                        "data": _b64(_read_ref(ref)),
                    }
                }
            )
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            # 不写这一句的话这个端只回文字（「我给你描述一下这张图…」），一张图都拿不到
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        body.update(req.extra.get("body") or {})
        data = await self.request("POST", url, cfg, json_body=body)
        found = self._inline_image(data)
        if found is None:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "这个端没有回图片，只回了文字",
                f"POST {url} 的 candidates 里找不到 inlineData。（预览：{str(data)[:300]}）",
                [
                    "确认模型名是会出图的那一族（例如 gemini-2.5-flash-image）",
                    "纯文本模型（gemini-2.5-flash）不出图，换一个",
                    MANUAL_WAY_OUT,
                ],
                {"protocol": self.name, "model": model},
            )
        mime, raw = found
        data_bytes = _decode(raw, f"POST {url}", self.name)
        return f"{_new_task_id()}{EXT_BY_MIME.get(mime, '.png')}", data_bytes

    def _inline_image(self, data: Any) -> tuple[str, str] | None:
        """从 candidates 里挖出第一张 `inline_data`。回 (mime, base64)。"""
        if not isinstance(data, dict):
            return None
        for cand in data.get("candidates") or []:
            parts = ((cand or {}).get("content") or {}).get("parts") or []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                blob = part.get("inlineData") or part.get("inline_data")
                if isinstance(blob, dict) and blob.get("data"):
                    mime = str(blob.get("mimeType") or blob.get("mime_type") or "image/png")
                    return mime, str(blob["data"])
        return None


# --- 通用 REST 合同：服务端按 `IMAGE_CONTRACT` 实现就能接 ---


class HttpApiImages(ImageProtocol):
    """自建 / 自己包一层的端。形状是本工具定的（`IMAGE_CONTRACT`），base64 收发。

    `needs_key = False`：这类端大多在内网、没有鉴权。填了密钥就发
    `Authorization: Bearer`，没填就不发——不因为「没配密钥」把一条能用的路挡掉。
    """

    name = "http_api"
    label = "通用 REST API（本工具合同）"
    default_base_url = ""
    models_path = ""
    health_path = "/health"
    needs_key = False
    supports_refs = True

    def headers(self, cfg: ImageConfig) -> dict[str, str]:
        head = {"Content-Type": "application/json"}
        if cfg.api_key:
            head["Authorization"] = f"Bearer {cfg.api_key}"
        return head

    async def _generate(self, cfg: ImageConfig, req: ImageRequest) -> tuple[str, bytes]:
        base = self.base(cfg)
        url = f"{base}/images/generate"
        width, height = req.size_wh()
        body: dict[str, Any] = {
            "prompt": req.prompt,
            "negative_prompt": req.negative,
            "size": req.size,
            "width": width,
            "height": height,
            "model": self.model(cfg),
            "seed": req.seed,
            #: 参考图连标签一起给：这个合同是我们定的，所以「第几张是谁」不必靠序号硬记。
            "refs": [
                {
                    "name": ref.path.name,
                    "label": ref.label,
                    "kind": ref.kind,
                    "data": _b64(_read_ref(ref)),
                }
                for ref in req.refs
            ],
            **(req.extra.get("body") or {}),
        }
        data = await self.request("POST", url, cfg, json_body=body)
        return await self._take(cfg, data, f"POST {url}")


# --- 本机 ComfyUI：另一份 T2I 图，走节点标题约定 ---


class ImageComfyClient(ComfyClient):
    """指向出图那台 ComfyUI。地址是**运行期读的属性**：配置页改了 `image.base_url`
    之后这个进程内单例要跟着变（与 `AudioComfyClient` 同一个理由）。
    留空时退回视频那台——同一台机器上跑两份图是最常见的摆法。
    """

    @property
    def base_url(self) -> str:
        return (settings.image_base_url or settings.comfy_base_url).rstrip("/")


class ComfyImageProvider(ComfyPresetProvider):
    """ComfyUI 出图（T2I / 图生图）。

    刻意继承视频那个适配器：上传输入、轮询 history、取回产物三件事与出画面完全一样，
    抄一遍只会在「ComfyUI 报错怎么翻译」上分叉。不一样的只有两件——填哪些入口（`submit`）、
    连哪台机器（`ImageComfyClient`）。

    **它不走上面那层同步壳**：ComfyUI 本来就是排队的，`poll` / `fetch` 继承下来即可。
    """

    name = "comfy_preset"

    def __init__(self) -> None:
        super().__init__(client=ImageComfyClient())

    async def probe(self) -> dict[str, Any]:
        ping = await self._client.ping()
        if not ping["online"]:
            raise AppError(
                ErrorCode.COMFY_OFFLINE,
                "图片服务未连接",
                ping["detail"],
                [
                    "启动那台 ComfyUI 后重试",
                    f"确认设置里的图片地址正确（当前 {self._client.base_url}）",
                    MANUAL_WAY_OUT,
                ],
            )
        name = str(settings.image_preset or "")
        if not name:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                "还没有选图片预设",
                "出图是另一份图（一份 T2I 的图，标了 AIVS_PROMPT，"
                "可选 AIVS_WIDTH / AIVS_HEIGHT）。",
                ["在设置页的「图片生成 API」里选一份预设", *presets.HOW_TO],
            )
        report = next((r for r in presets.listing() if r["name"] == name), None)
        if report is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "选中的图片预设不存在",
                f"设置里的图片预设是 {name}，但预设目录里没有它。",
                ["在设置页重新上传这份预设", "或改选一个已有的预设"],
            )
        if not report.get("prompt_ok"):
            # 出图那份图必需的入口只有 AIVS_PROMPT 一个：从入口标题分不出「这是 T2I 还是
            # R2V」，所以这里判的是 `prompt_ok` 而不是 `r2v_ready`——后者会因为这份图
            # 声明了 AIVS_IMAGE 而为 false（那是它退出视频候选的方式），照它判会自相矛盾。
            raise AppError(
                ErrorCode.INVALID_WORKFLOW,
                "这份图里没有提示词入口",
                f"预设 {name} 里没有 AIVS_PROMPT，本工具没法告诉它要画什么。",
                presets.HOW_TO,
                {"preset": name, "found": report.get("found")},
            )
        slots = report.get("ref_slots", 0)
        declared = bool(report.get("declares_image"))
        detail = f"图片服务已连接 · 预设 {name} 就绪（{slots} 个参考图槽位）"
        if not declared:
            # **没声明照旧能用**（老工程一份都不用重新标），但要说出代价：这份图同时还
            # 躺在 R2V / 首尾帧的候选里，在那边选中它只会白跑一趟。
            detail += (
                f"。这份图没标 {presets.DECLARE_IMAGE}，所以它同时还出现在视频预设的候选里"
                "——给它加上这个标题就只归「出图」那一栏"
            )
        return {
            "ok": True,
            "target": self._client.base_url,
            "preset": name,
            "preset_ready": True,
            "ref_slots": slots,
            "declares_image": declared,
            "detail": detail,
        }

    async def submit(self, req: ImageRequest, *, client_id: str) -> str:  # type: ignore[override]
        name = str(req.extra.get("preset") or settings.image_preset or "")
        if not name:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                "还没有选图片预设",
                "生成图片需要一份 T2I 的图（API 格式），它和出画面那份图不是同一份。",
                ["在设置页的「图片生成 API」里上传并选中一份预设", MANUAL_WAY_OUT],
            )
        graph = copy.deepcopy(presets.load(name))
        points = presets.entry_points(graph)
        if "AIVS_PROMPT" not in points:
            raise AppError(
                ErrorCode.INVALID_WORKFLOW,
                "这份预设里没有提示词入口",
                f"预设 {name} 里没有 AIVS_PROMPT，本工具没法告诉它要画什么。",
                presets.HOW_TO,
                {"preset": name, "found": sorted(points)},
            )
        width, height = req.size_wh()
        values: dict[str, Any] = {
            "AIVS_PROMPT": req.prompt,
            "AIVS_NEGATIVE": req.negative,
            "AIVS_SEED": req.seed,
            "AIVS_WIDTH": width,
            "AIVS_HEIGHT": height,
        }
        # 声明是**可选**的：没标照旧提交（老工程一份都不用重新标），但这次用的是哪份图、
        # 它还混在视频候选里这件事得留档——冻结进版本参数，界面上看得见。
        if presets.DECLARE_IMAGE not in presets.declarations(graph):
            req.notes.append(
                f"预设 {name} 没标 {presets.DECLARE_IMAGE}，本工具只能按设置里指名的那一份"
                "当出图预设用；它同时还出现在视频预设的候选里。"
            )
        # 画幅是**可选**入口：图里没标就用图里原来的尺寸，只说一句，不失败。
        if "AIVS_WIDTH" not in points and "AIVS_HEIGHT" not in points:
            req.notes.append(
                f"预设 {name} 没有 AIVS_WIDTH / AIVS_HEIGHT 入口，{req.size} 这个尺寸没送出去"
                "——出来的是图里原本的画幅。"
            )
        values.update(await self._image_refs(name, points, req))
        for marker, spot in points.items():
            value = values.get(marker)
            if value is None or value == "":
                continue  # 没给的项保持图里原来的值，不要用空串把它冲掉
            graph[spot["node_id"]]["inputs"][spot["field"]] = value
        prompt_id = await self._client.submit(graph, client_id=client_id)
        self._used[prompt_id] = name
        log.info(
            "provider.image_submitted",
            protocol=self.name,
            preset=name,
            prompt_id=prompt_id,
            refs=len(req.refs),
        )
        return prompt_id

    async def _image_refs(
        self, name: str, points: dict[str, dict[str, str]], req: ImageRequest
    ) -> dict[str, Any]:
        """参考图按顺序填进 `AIVS_REF_1…9`。**槽位不够只降级、不失败**——这条规矩与
        出画面那边完全一致（`comfy_preset._refs`），少喂了哪几张写进 `req.notes` 留档。
        """
        if not req.refs:
            return {}
        slots = presets.ref_slots(points, "image")
        if not slots:
            names = "、".join(r.label or r.path.name for r in req.refs)
            req.notes.append(
                f"预设 {name} 一个 AIVS_REF_* 槽位都没有，{len(req.refs)} 张参考图"
                f"（{names}）没有喂进去——出来的图只由提示词决定。"
            )
            return {}
        values: dict[str, Any] = {}
        for slot, ref in zip(slots, req.refs, strict=False):
            values[slot] = await self._upload(ref.path)
        dropped = req.refs[len(slots) :]
        if dropped:
            names = "、".join(r.label or r.path.name for r in dropped)
            req.notes.append(
                f"预设 {name} 只有 {len(slots)} 个参考图槽位，"
                f"后 {len(dropped)} 张没有喂进去：{names}。"
            )
        return values


class ComfyImages(ImageProtocol):
    """协议表里的 ComfyUI 那一项。

    它只是**表里的一行**（设置页据此画界面、探测走它），真正干活的是
    `ComfyImageProvider`——那一支的轮询与取回是 ComfyUI 的 history / view，
    与上面那层「同步端 → 任务形状」的壳完全不同，所以 `provider()` 回的不是 `self`。
    """

    name = "comfy_preset"
    label = "本机 ComfyUI 预设（T2I 图）"
    default_base_url = "http://127.0.0.1:8188"
    models_path = ""
    needs_key = False
    supports_refs = True
    wants_preset = True

    def provider(self) -> Any:
        return ComfyImageProvider()

    async def probe_with(self, cfg: ImageConfig) -> dict[str, Any]:
        """**刻意不带覆盖探测**：ComfyUI 那一支的地址与预设是运行期从 `settings` 读的
        （`ImageComfyClient.base_url`），在这里塞一份还没保存的配置只会两处不一致。
        """
        report = await ComfyImageProvider().probe()
        return {"provider": self.name, "model_count": 0, "model_present": None, **report}


#: 协议名 → 适配器实例。**这张表是唯一真源**：默认地址、要不要密钥、请求体形状、
#: 返回里图在哪、模型列表从哪来全在各自的类里，`GET /settings` 把它投影成
#: `image_protocols[]` 给前端画界面——加一家 API 只改这一个 dict，前端一行不动。
BY_NAME: dict[str, ImageProtocol] = {
    p.name: p for p in (ComfyImages(), OpenAiImages(), GeminiImages(), HttpApiImages())
}


def names() -> list[str]:
    """设置页那个下拉的取值。`none` 在最前面，因为它是默认。"""
    return [NONE, *BY_NAME]


def labels() -> list[str]:
    """与 `names()` **一一对应**的人话标签（设置页的 `choice_labels` 直接用它）。
    形状刻意与 `ai/llm/protocols.labels()` 一致：两处设置页字段照同一套写法拼。"""
    return [NONE_LABEL, *(p.label for p in BY_NAME.values())]


def listing() -> list[dict[str, Any]]:
    """投影给前端的协议表。多一家 API 时这里一行不用改。"""
    rows: list[dict[str, Any]] = [
        {
            "name": NONE,
            "label": NONE_LABEL,
            "default_base_url": "",
            "needs_key": False,
            "supports_refs": False,
            "wants_preset": False,
            "models_hint": "",
        }
    ]
    rows += [
        {
            "name": p.name,
            "label": p.label,
            "default_base_url": p.default_base_url,
            "needs_key": p.needs_key,
            "supports_refs": p.supports_refs,
            "wants_preset": p.wants_preset,
            "models_hint": p.models_path or "",
        }
        for p in BY_NAME.values()
    ]
    return rows


def get(name: str) -> ImageProtocol | None:
    return BY_NAME.get(str(name or "").strip())


def require(name: str) -> ImageProtocol:
    """按名字取一个协议。取不到要说清有哪几个可选，别只说「不支持」。"""
    found = get(name)
    if found is None:
        options = "、".join(f"{n}（{lab}）" for n, lab in zip(names(), labels(), strict=True))
        raise AppError(
            ErrorCode.MISSING_CAPABILITY,
            "没有配置图片生成服务" if not name or name == NONE else "不认识这个图片调用方式",
            f"image.provider = {name or '（空）'}。可选：{options}。",
            ["在设置页的「图片生成 API」里选一种方式并填好地址", MANUAL_WAY_OUT],
            {"provider": name},
        )
    return found


async def list_models(
    protocol: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """设置页那个「自动获取模型」。形状与 `ai/llm/client.list_models()` 一致。

    协议 / 地址 / 密钥可以是**还没保存**的那份（不然得先存一份可能是错的配置才能看列表），
    **一律不落盘**。列不出来的端（ComfyUI、通用合同）回空 `items`——那不是错误，
    设置页照旧让人手填。
    """
    cfg = config(protocol=protocol, base_url=base_url, api_key=api_key, model=model)
    proto = require(cfg.protocol)
    items = await proto.list_models(cfg)
    ids = {row["id"] for row in items}
    log.info("image.models", protocol=cfg.protocol, count=len(items))
    return {
        "provider": cfg.protocol,
        "label": proto.label,
        "target": f"{proto.base(cfg)}{proto.models_path}" if proto.models_path else "",
        "count": len(items),
        "items": items,
        "current": cfg.model or None,
        "current_present": (cfg.model in ids) if cfg.model and ids else None,
    }
