"""ComfyUI HTTP 客户端。

只做四件事：探活、列出已安装节点、提交 prompt、查历史。
ComfyUI 不在线不是崩溃，是一个带建议的 COMFY_OFFLINE——手动模式与时间线都不依赖它。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

log = get_logger("comfy")
TIMEOUT = httpx.Timeout(10.0, connect=3.0)


def _offline(exc: Exception) -> AppError:
    return AppError(
        ErrorCode.COMFY_OFFLINE,
        "ComfyUI 未连接",
        f"{settings.comfy_base_url} 无法访问：{type(exc).__name__}: {exc}",
        [
            "启动 ComfyUI 后重试",
            f"确认地址正确（当前 {settings.comfy_base_url}，可用 AIVS_COMFY_BASE_URL 覆盖）",
            "只做手动整理与时间线编辑时可以忽略此错误",
        ],
    )


class ComfyClient:
    def __init__(self, base_url: str | None = None) -> None:
        #: 不在构造时把地址定死：配置页改了 comfy_base_url 之后，这个进程内单例要跟着变。
        self._explicit = (base_url or "").rstrip("/")

    @property
    def base_url(self) -> str:
        return self._explicit or settings.comfy_base_url.rstrip("/")

    @property
    def _base(self) -> str:
        return self.base_url

    async def _get(self, path: str) -> Any:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as http:
                resp = await http.get(f"{self._base}{path}")
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise _offline(exc) from exc

    async def ping(self) -> dict[str, Any]:
        """探活。返回 {online, base_url, detail}，不抛异常——这是给状态条用的。"""
        try:
            await self._get("/system_stats")
        except AppError as err:
            return {"online": False, "base_url": self._base, "detail": err.detail}
        return {"online": True, "base_url": self._base, "detail": "已连接"}

    async def system_stats(self) -> dict[str, Any]:
        """GPU / 显存等运行时信息，概览页的环境卡片用它。"""
        data = await self._get("/system_stats")
        return data if isinstance(data, dict) else {}

    async def installed_nodes(self) -> set[str]:
        """已安装节点的 class_type 集合，用于「缺少自定义节点」探测。"""
        data = await self._get("/object_info")
        return set(data) if isinstance(data, dict) else set()

    async def submit(self, api_graph: dict[str, Any], client_id: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as http:
                resp = await http.post(
                    f"{self._base}/prompt", json={"prompt": api_graph, "client_id": client_id}
                )
                if resp.status_code >= 400:
                    raise AppError(
                        ErrorCode.WORKFLOW_ERROR,
                        "ComfyUI 拒绝了本次任务",
                        f"HTTP {resp.status_code}: {resp.text[:800]}",
                        [
                            "在流程页重新校验绑定",
                            "确认工作流所需的模型文件已就位",
                            "展开原始报错查看 ComfyUI 侧的详细信息",
                        ],
                    )
                return str(resp.json().get("prompt_id", ""))
        except httpx.HTTPError as exc:
            raise _offline(exc) from exc

    async def upload_input(self, filename: str, data: bytes, subfolder: str = "") -> str:
        """把首/末帧与参考素材传进 ComfyUI 的 input 目录，返回图里该填的文件名。

        R2V 的入口是文件，而文件在我们这边；ComfyUI 只认它自己 input 目录下的名字，
        所以每次生成前先上传。同名会被 ComfyUI 自动改名，因此**必须用它回的名字**。

        **参考视频 / 参考音频走的是同一个 `/upload/image`**：ComfyUI 自己的前端上传
        视频与音频用的也是这个端点（它只是把文件放进 input 目录，不看是什么媒体），
        另外那两个端点（`/upload/mask`）干的是别的事。所以这里刻意不叫 `upload_image`——
        名字里带 image 会让人以为得另找一个端点传 `.wav`。
        """
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=3.0)) as http:
                resp = await http.post(
                    f"{self._base}/upload/image",
                    files={"image": (filename, data, "application/octet-stream")},
                    data={"overwrite": "false", "subfolder": subfolder},
                )
                if resp.status_code >= 400:
                    raise AppError(
                        ErrorCode.COMFY_LOST,
                        "参考素材上传失败",
                        f"HTTP {resp.status_code}: {resp.text[:500]}",
                        [
                            "确认 ComfyUI 版本支持 /upload/image",
                            "确认 ComfyUI 侧 input 目录可写",
                            "参考视频 / 音频走的是同一个上传端点，确认它没有被反向代理挡掉",
                        ],
                    )
                body = resp.json()
        except httpx.HTTPError as exc:
            raise _offline(exc) from exc
        except ValueError as exc:
            raise AppError(
                ErrorCode.COMFY_LOST,
                "参考素材上传的响应不认识",
                f"{type(exc).__name__}: {exc}",
                ["确认这个地址真的是 ComfyUI"],
            ) from exc
        name = str(body.get("name") or filename)
        sub = str(body.get("subfolder") or "")
        return f"{sub}/{name}" if sub else name

    async def history(self, prompt_id: str) -> dict[str, Any]:
        data = await self._get(f"/history/{prompt_id}")
        return data.get(prompt_id, {}) if isinstance(data, dict) else {}

    async def download(self, filename: str, subfolder: str = "", kind: str = "output") -> bytes:
        """把产物取回本机工程目录——素材必须留在工程里，不能只存在 ComfyUI 的输出目录。"""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=3.0)) as http:
                resp = await http.get(
                    f"{self._base}/view",
                    params={"filename": filename, "subfolder": subfolder, "type": kind},
                )
                resp.raise_for_status()
                return resp.content
        except httpx.HTTPError as exc:
            raise _offline(exc) from exc


def outputs_of(history: dict[str, Any]) -> list[dict[str, Any]]:
    """从 history 里挑出图片 / 视频产物，统一成 {filename, subfolder, type}。"""
    found: list[dict[str, Any]] = []
    for node in (history.get("outputs") or {}).values():
        if not isinstance(node, dict):
            continue
        for key in ("images", "gifs", "videos", "files"):
            for item in node.get(key) or []:
                if isinstance(item, dict) and item.get("filename"):
                    found.append(
                        {
                            "filename": str(item["filename"]),
                            "subfolder": str(item.get("subfolder") or ""),
                            "type": str(item.get("type") or "output"),
                        }
                    )
    return found


comfy = ComfyClient()
