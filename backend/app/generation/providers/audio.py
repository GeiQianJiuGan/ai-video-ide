"""音源适配层：**声音是一条独立的链，不是视频的一个参数。**

为什么单独一条：AI 出的视频那条音轨往往很差，而在这之前想换掉它只能把整段画面重跑一次
——几分钟的显存与时间，只为采一段声音。声音独立之后，它是同一个镜头上多出来的一版
（`GenerationVersion.kind="audio"` + `Shot.current_audio_version_id`），画面一个字节都不重跑，
时间线装配时落到专门的配音轨上并把画面那一段静音（`services/timeline.py`）。

两个适配器，与视频那边一一对应，形状完全一样：
  · `ComfyAudioProvider`  —— 音源那份图（另一份预设，标 `AIVS_AUDIO_*` / `AIVS_VOICE_REF`）；
  · `HttpAudioProvider`   —— 通用 REST 合同，`mode="audio"`，body 里多 text / voice_ref。

三条边界：
  · **地址、密钥、预设全是另一套**（`settings.audio_*`），不共用视频那份：音源图往往跑在
    另一台机器 / 另一个端口上，共用一份地址会让「视频能连、音频连不上」说不清；
  · **`audio_provider="none"` 是默认**（硬约束 2 的同一个作风）：音频入口一律回
    `MISSING_CAPABILITY` 并写清手动导入音频那条路完全不受影响；
  · **降级要说出来**：图里没有 `AIVS_VOICE_REF` / `AIVS_SOURCE_VIDEO` 时只写一条 note
    并照旧生成（那份图由模型端维护），notes 跟着版本一起冻结。
"""

from __future__ import annotations

import copy
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.generation.comfy.client import ComfyClient
from app.generation.providers import presets
from app.generation.providers.base import AudioRequest
from app.generation.providers.comfy_preset import ComfyPresetProvider
from app.generation.providers.http_api import CONTRACT, HttpApiProvider, _encode, _expect_json

log = get_logger("provider.audio")

#: 音源图的入口 → 从 `AudioRequest` 的哪一项取值。**只有这一张表**，
#: 别在别处再写一遍「台词填哪儿」。
FIELDS = ("AIVS_AUDIO_TEXT", "AIVS_AUDIO_PROMPT", "AIVS_AUDIO_DURATION", "AIVS_AUDIO_SEED")


class AudioComfyClient(ComfyClient):
    """指向音源那台 ComfyUI。地址是**运行期读的属性**而不是构造时定死的值：
    配置页改了 `audio.base_url` 之后这个进程内单例要跟着变（与 `ComfyClient` 同一个理由）。
    留空时退回视频那台——同一台机器上跑两份图是最常见的摆法。
    """

    @property
    def base_url(self) -> str:
        return (settings.audio_base_url or settings.comfy_base_url).rstrip("/")


class ComfyAudioProvider(ComfyPresetProvider):
    """ComfyUI 音源图。

    刻意继承视频那个适配器：上传输入、轮询 history、取回产物这三件事与出画面完全一样，
    抄一遍只会在「ComfyUI 报错怎么翻译」上分叉。不一样的只有两件——填哪些入口（`submit`）、
    连哪台机器（`AudioComfyClient`）。
    """

    name = "comfy_preset"

    def __init__(self) -> None:
        super().__init__(client=AudioComfyClient())

    async def probe(self) -> dict[str, Any]:
        ping = await self._client.ping()
        if not ping["online"]:
            raise AppError(
                ErrorCode.COMFY_OFFLINE,
                "音源服务未连接",
                ping["detail"],
                [
                    "启动那台 ComfyUI 后重试",
                    f"确认设置里的音源地址正确（当前 {self._client.base_url}）",
                    "手动导入音频到配音轨那条路不受影响",
                ],
            )
        name = str(settings.audio_preset or "")
        if not name:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                "还没有选音源预设",
                "音源是另一份图（另存一份标了 AIVS_AUDIO_TEXT / AIVS_AUDIO_PROMPT 的预设）。",
                ["在设置页的「音源生成」里选一份预设", *presets.HOW_TO],
            )
        report = next((r for r in presets.listing() if r["name"] == name), None)
        if report is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "选中的音源预设不存在",
                f"设置里的音源预设是 {name}，但预设目录里没有它。",
                ["在设置页重新上传这份预设", "或改选一个已有的预设"],
            )
        if not report.get("audio_ready"):
            raise AppError(
                ErrorCode.INVALID_WORKFLOW,
                "这份图不是音源图",
                f"预设 {name} 里既没有 AIVS_AUDIO_TEXT 也没有 AIVS_AUDIO_PROMPT，"
                "本工具没法告诉它「说什么 / 什么声音」。",
                [
                    "在 ComfyUI 里把台词那个文本框标成 AIVS_AUDIO_TEXT",
                    "只出环境音的图把描述框标成 AIVS_AUDIO_PROMPT 即可",
                    *presets.HOW_TO,
                ],
                {"preset": name, "found": report.get("found")},
            )
        return {
            "ok": True,
            "target": self._client.base_url,
            "preset": name,
            "preset_ready": True,
            "detail": f"音源服务已连接 · 预设 {name} 就绪",
        }

    async def submit(self, req: AudioRequest, *, client_id: str) -> str:  # type: ignore[override]
        name = str(req.extra.get("preset") or settings.audio_preset or "")
        if not name:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                "还没有选音源预设",
                "生成声音需要一份音源图（API 格式），它和出画面那份图不是同一份。",
                [
                    "在设置页的「音源生成」里上传并选中一份预设",
                    "或直接把外部做好的音频导入成这个镜头的音频版本（不需要任何服务）",
                ],
            )
        graph = copy.deepcopy(presets.load(name))
        points = presets.entry_points(graph)
        if not any(m in points for m in presets.AUDIO_REQUIRED_ANY):
            raise AppError(
                ErrorCode.INVALID_WORKFLOW,
                "这份预设不是音源图",
                f"预设 {name} 里既没有 AIVS_AUDIO_TEXT 也没有 AIVS_AUDIO_PROMPT。",
                [
                    "把台词那个文本框标成 AIVS_AUDIO_TEXT（只出环境音的图标 AIVS_AUDIO_PROMPT）",
                    *presets.HOW_TO,
                ],
                {"preset": name, "found": sorted(points)},
            )
        values: dict[str, Any] = {
            "AIVS_AUDIO_TEXT": req.text,
            "AIVS_AUDIO_PROMPT": req.prompt,
            "AIVS_AUDIO_DURATION": req.duration,
            "AIVS_AUDIO_SEED": req.seed,
        }
        # 台词没有专门的入口时退到描述框：只收一个文本框的图很常见，
        # 为此拒绝生成等于把这类图整个挡在外面。降级照旧写进 notes。
        if req.text and "AIVS_AUDIO_TEXT" not in points and "AIVS_AUDIO_PROMPT" in points:
            joined = f"{req.prompt}\n{req.text}".strip() if req.prompt else req.text
            values["AIVS_AUDIO_PROMPT"] = joined
            req.notes.append(
                f"预设 {name} 没有 AIVS_AUDIO_TEXT 入口，台词已并进 AIVS_AUDIO_PROMPT 一起送。"
            )
        # 音色参考与画面都是**可选**入口：图里没有就只写一条 note，不失败。
        for marker, path, what in (
            ("AIVS_VOICE_REF", req.voice_ref, "音色参考"),
            ("AIVS_SOURCE_VIDEO", req.source_video, "这个镜头的画面"),
        ):
            if path is None:
                continue
            if marker not in points:
                req.notes.append(
                    f"预设 {name} 里没有 {marker} 入口，{what}（{path.name}）没有送出去"
                    "——这一版不会参考它。"
                )
                continue
            values[marker] = await self._upload(path)
        # 这条链**刻意不摘节点**（出画面那两条会，见 `comfy_preset._detach_idle`）：音源图上
        # `AIVS_VOICE_REF` 挂着的那段示例音频**就是这份图的默认音色**，而不是一个碰巧留在格子里
        # 的不相干文件——没指定音色时用它是对的，摘掉反而会让「只能靠参考音频跑」的克隆图直接报
        # 「Required input is missing」。`AIVS_SOURCE_VIDEO`（口型驱动）同理。所以这里照旧
        # 「没给的项保持图里原来的值」，一个字都不要跟着视频那条路改。
        for marker, spot in points.items():
            value = values.get(marker)
            if value is None or value == "":
                continue
            graph[spot["node_id"]]["inputs"][spot["field"]] = value
        prompt_id = await self._client.submit(graph, client_id=client_id)
        self._used[prompt_id] = name
        log.info("provider.audio_submitted", preset=name, prompt_id=prompt_id)
        return prompt_id


class HttpAudioProvider(HttpApiProvider):
    """通用 REST 合同的音源版本：同样三个端点，`mode="audio"`。

        POST {base}/submit  body {mode: "audio", text, prompt, negative, duration, seed,
                                  voice_ref, voice_ref_name, source_video, source_video_name,
                                  extra}
        GET  {base}/tasks/{task_id} / GET {output_url} / GET {base}/health —— 与视频一模一样

    地址与密钥读 `settings.audio_*`：音源服务和视频服务不是同一个东西。
    """

    name = "http_api"

    @property
    def base_url(self) -> str:
        return (settings.audio_base_url or "").rstrip("/")

    @property
    def _key(self) -> str:
        return settings.audio_api_key

    def _require_base(self) -> str:
        base = self.base_url
        if not base:
            raise AppError(
                ErrorCode.MISSING_CAPABILITY,
                "还没有配置音源服务地址",
                "http_api 方式需要一个实现了本工具合同的音源服务地址。",
                [
                    "在设置页的「音源生成」里填写地址",
                    "或把音源调用方式改成 comfy_preset（另存一份音源图）",
                    "或直接手动导入做好的音频，这条路不需要任何服务",
                    *(f"服务端需要实现：{line}" for line in CONTRACT),
                ],
            )
        return base

    async def submit(self, req: AudioRequest, *, client_id: str) -> str:  # type: ignore[override]
        base = self._require_base()
        body: dict[str, Any] = {
            "mode": "audio",
            "text": req.text,
            "prompt": req.prompt,
            "negative": req.negative,
            "duration": req.duration,
            "seed": req.seed,
            "client_id": client_id,
            "extra": req.extra,
        }
        for key, path in (("voice_ref", req.voice_ref), ("source_video", req.source_video)):
            if path is None:
                continue
            body[key] = _encode(path)
            body[f"{key}_name"] = path.name
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
                "音源服务没有返回任务 id",
                f"POST {base}/submit 的响应里没有 task_id：{str(data)[:400]}",
                [f"服务端需要按合同返回：{CONTRACT[0]}"],
            )
        log.info("provider.audio_submitted", provider=self.name, task_id=task_id)
        return task_id


#: 名字 → 构造函数。与视频那张表刻意同名同形：设置页两处画的是同一套下拉。
BUILTIN: dict[str, Any] = {
    "comfy_preset": ComfyAudioProvider,
    "http_api": HttpAudioProvider,
}

LABELS = {
    "none": "不配置（手动导入音频）",
    "comfy_preset": "ComfyUI 音源预设",
    "http_api": "通用 REST API",
}
