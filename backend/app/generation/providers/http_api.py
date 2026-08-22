"""通用 REST 适配器：模型端按这份固定合同实现即可。

存在的意义是「不是 ComfyUI 也能接」：自研推理服务、云端 API、别人的封装层，
只要满足下面三个端点就能当视频生成后端，本工具不需要认识它内部长什么样。

    POST {base}/submit
      body {mode, prompt, negative, duration, seed, extra, first_frame, last_frame, refs}
      —— 两个 frame 是 base64（图在我们这边，不能指望对方能读我们的磁盘）
      —— refs 是**首尾帧之外的参考图**，按优先级排好的数组，每项
         {data: base64, name, label, kind}：label 是「它是谁」（角色表 / 地点参考），
         模型端用不上可以忽略，但顺序必须当成语义——第 1 张最重要
      resp {task_id}

    GET {base}/tasks/{task_id}
      resp {status: queued|running|done|failed, progress: 0~1, output_url, error}

    GET {output_url}      —— 相对路径按 base 拼；产物字节流

    GET {base}/health     —— 「测试连接」用，非 2xx 就算不通

字段缺失、状态字不认识、非 2xx，一律归一成带建议的 AppError——绝不静默当成「还在跑」。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.generation.providers import base
from app.generation.providers.base import STATUSES, TaskState, VideoRequest

log = get_logger("provider.http_api")

CONTRACT = [
    "POST {base}/submit → {task_id}",
    "GET {base}/tasks/{task_id} → {status, progress, output_url, error}",
    "GET {base}/health → 2xx",
]


class HttpApiProvider:
    name = "http_api"

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self._explicit_base = (base_url or "").rstrip("/")
        self._explicit_key = api_key

    @property
    def base_url(self) -> str:
        return self._explicit_base or settings.video_base_url.rstrip("/")

    @property
    def _key(self) -> str:
        return self._explicit_key if self._explicit_key is not None else settings.video_api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    def _require_base(self) -> str:
        base = self.base_url
        if not base:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                "还没有配置视频生成服务地址",
                "http_api 方式需要一个实现了本工具合同的服务地址。",
                [
                    "在设置页的「视频生成 API」里填写地址",
                    "或把调用方式改回 comfy_preset（默认，直接连 ComfyUI）",
                    *(f"服务端需要实现：{line}" for line in CONTRACT),
                ],
            )
        return base

    def _offline(self, exc: Exception) -> AppError:
        return AppError(
            ErrorCode.COMFY_OFFLINE,
            "视频生成服务未连接",
            f"{self.base_url} 无法访问：{type(exc).__name__}: {exc}",
            [
                "确认该服务正在运行",
                "确认设置页里的地址正确",
                "只做手动整理与时间线编辑时可以忽略",
            ],
        )

    def _timeout(self, connect: float = 5.0) -> httpx.Timeout:
        return httpx.Timeout(float(settings.video_timeout), connect=connect)

    # --- 探测 ---

    def ref_capacity(self) -> base.RefCapacity:
        """**不限张数。** 这条合同由我们定，`refs` 是整组带过去的，没有槽位这回事。

        真限制在服务端（它能吃几张），可那件事我们既问不到也不该猜——猜低了白丢用户的图。
        """
        return base.RefCapacity(
            None,
            "REST 合同",
            "通用 REST 合同把参考图整组发过去，不存在槽位不够：账单算出几张就发几张。",
        )

    async def probe(self) -> dict[str, Any]:
        base = self._require_base()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as http:
                resp = await http.get(f"{base}/health", headers=self._headers())
        except httpx.HTTPError as exc:
            raise self._offline(exc) from exc
        if resp.status_code >= 400:
            raise AppError(
                ErrorCode.COMFY_OFFLINE,
                "视频生成服务返回了错误",
                f"GET {base}/health → HTTP {resp.status_code}: {resp.text[:400]}",
                [
                    "确认这个地址实现了本工具的合同",
                    *(f"需要：{line}" for line in CONTRACT),
                ],
            )
        return {
            "ok": True,
            "target": base,
            "detail": f"视频生成服务已连接（{base}）",
            "raw": _json_or_text(resp),
        }

    # --- 生成 ---

    async def submit(self, req: VideoRequest, *, client_id: str) -> str:
        base = self._require_base()
        body: dict[str, Any] = {
            "mode": req.mode,
            "prompt": req.prompt,
            "negative": req.negative,
            "duration": req.duration,
            "seed": req.seed,
            "client_id": client_id,
            "extra": req.extra,
        }
        for key, path in (("first_frame", req.first_frame), ("last_frame", req.last_frame)):
            if path is None:
                continue
            body[key] = _encode(path)
            body[f"{key}_name"] = path.name
        # 参考图整组带过去：这条合同由我们定，所以它天生支持多张，不存在槽位不够的问题。
        if req.refs:
            body["refs"] = [
                {
                    "data": _encode(ref.path),
                    "name": ref.path.name,
                    "label": ref.label,
                    "kind": ref.kind,
                }
                for ref in req.refs
            ]
        try:
            async with httpx.AsyncClient(timeout=self._timeout()) as http:
                resp = await http.post(f"{base}/submit", json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise self._offline(exc) from exc
        data = _expect_json(resp, f"POST {base}/submit")
        task_id = str(data.get("task_id") or data.get("id") or "")
        if not task_id:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "视频生成服务没有返回任务 id",
                f"POST {base}/submit 的响应里没有 task_id：{str(data)[:400]}",
                [f"服务端需要按合同返回：{CONTRACT[0]}"],
            )
        log.info(
            "provider.submitted",
            provider=self.name,
            task_id=task_id,
            mode=req.mode,
            refs=len(req.refs),
        )
        return task_id

    async def poll(self, task_id: str) -> TaskState:
        base = self._require_base()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as http:
                resp = await http.get(f"{base}/tasks/{task_id}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise self._offline(exc) from exc
        data = _expect_json(resp, f"GET {base}/tasks/{task_id}")
        status = str(data.get("status") or "").strip().lower()
        if status not in STATUSES:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "视频生成服务返回了不认识的状态",
                f"status={data.get('status')!r}，本工具只认 {'、'.join(STATUSES)}。",
                [f"服务端需要按合同返回：{CONTRACT[1]}"],
                {"task_id": task_id},
            )
        detail = str(data.get("error") or data.get("detail") or "")
        if status == "failed" and not detail:
            detail = "服务端报告任务失败，但没有给出原因。"
        return TaskState(
            status,
            float(data.get("progress") or (1.0 if status in ("done", "failed") else 0.0)),
            detail or _DEFAULT_DETAIL[status],
            raw=data,
        )

    async def fetch(self, task_id: str) -> tuple[str, bytes]:
        base = self._require_base()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as http:
                info = _expect_json(
                    await http.get(f"{base}/tasks/{task_id}", headers=self._headers()),
                    f"GET {base}/tasks/{task_id}",
                )
                url = str(info.get("output_url") or "")
                if not url:
                    raise AppError(
                        ErrorCode.WORKFLOW_ERROR,
                        "视频生成服务没有给出产物地址",
                        f"任务 {task_id} 已完成，但响应里没有 output_url：{str(info)[:400]}",
                        [f"服务端需要按合同返回：{CONTRACT[1]}"],
                        {"task_id": task_id},
                    )
                target = (
                    url
                    if url.startswith(("http://", "https://"))
                    else urljoin(base + "/", url.lstrip("/"))
                )
                resp = await http.get(target, headers=self._headers(), timeout=self._timeout())
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise self._offline(exc) from exc
        return _filename_of(url, task_id), resp.content


_DEFAULT_DETAIL = {
    "queued": "已提交，排队中",
    "running": "服务端正在跑",
    "done": "已出片",
    "failed": "任务失败",
}


def _encode(path: Path) -> str:
    if not path.is_file():
        raise AppError(
            ErrorCode.MISSING_ASSET,
            "参考图不在磁盘上",
            f"{path} 找不到。",
            ["确认该资产文件还在工程目录里", "或重新挑一张参考图"],
            {"path": path.as_posix()},
        )
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _expect_json(resp: httpx.Response, where: str) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise AppError(
            ErrorCode.WORKFLOW_ERROR,
            "视频生成服务拒绝了请求",
            f"{where} → HTTP {resp.status_code}: {resp.text[:600]}",
            [
                "展开原始报错查看服务端给的信息",
                "确认设置页里的地址与密钥正确",
            ],
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise AppError(
            ErrorCode.WORKFLOW_ERROR,
            "视频生成服务的响应不是 JSON",
            f"{where} → {resp.text[:400]}",
            [f"服务端需要按合同返回 JSON：{CONTRACT[1]}"],
        ) from exc
    if not isinstance(data, dict):
        raise AppError(
            ErrorCode.WORKFLOW_ERROR,
            "视频生成服务的响应形状不对",
            f"{where} 返回了 {type(data).__name__}，这里需要一个 JSON 对象。",
            [f"服务端需要按合同返回：{CONTRACT[1]}"],
        )
    return data


def _json_or_text(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:200]


def _filename_of(url: str, task_id: str) -> str:
    tail = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return tail if tail and "." in tail else f"{task_id}.mp4"
