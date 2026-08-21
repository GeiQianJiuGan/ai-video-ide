"""应用级设置（可写配置）。

配置一直只能靠 `AIVS_` 环境变量或 `.env`——那对「在界面里换一个 LLM 地址」不够用。
这里加一层**可写覆盖**：`runtime_dir/settings.json`，与 `library.json` / `recent.json`
同级，属于「这台机器怎么用这个应用」而不是某个工程的数据。

生效顺序 **settings.json → 环境变量（含 .env） → 代码默认**。

实现刻意不引入 `effective()` 间接层：文件里的覆盖在启动时**写进 `settings` 单例**，
于是全部既有读取点（`settings.llm_provider`、`settings.comfy_base_url`…）一行不用改，
测试里的 `monkeypatch.setattr(settings, ...)` 也照旧有效。

每个字段都回一个 `source`（`file` / `env` / `default`），照 `core/ffmpeg.py::Located.source`
的做法——「你看到的这个值是从哪来的」在排查时是唯一有用的信息。
密钥永不回明文：只回 `masked` 与 `has_value`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.ai.llm import client as llm
from app.core.config import Settings, settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.persistence.models import utc_now

log = get_logger("appsettings")

PROBE_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """一个可配置字段：对外的点号键 → `Settings` 上的属性。"""

    key: str
    attr: str
    group: str
    label: str
    kind: str = "str"  # str | int | float | bool | secret | enum
    choices: tuple[str, ...] = ()
    #: 这个字段配错了会导致什么做不出来。UI 直接显示，不在前端重写一遍。
    impact: str = ""


#: 分组只影响配置页怎么摆，不影响存储结构。
GROUPS: tuple[tuple[str, str], ...] = (
    ("llm", "LLM（AI 协作）"),
    ("video", "视频生成 API"),
    ("comfy", "ComfyUI"),
    ("scene", "幕（流程图节点）"),
    ("runtime", "运行"),
)

FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "llm.provider",
        "llm_provider",
        "llm",
        "协议",
        "enum",
        ("none", "openai_compatible", "ollama"),
        "none 时 AI 协作栏不可用；手动编排不受影响。",
    ),
    FieldSpec("llm.base_url", "llm_base_url", "llm", "地址", impact="留空则用协议的默认地址。"),
    FieldSpec("llm.model", "llm_model", "llm", "模型", impact="没有模型名时视为未配置。"),
    FieldSpec("llm.api_key", "llm_api_key", "llm", "API Key", "secret"),
    FieldSpec(
        "video.provider",
        "video_provider",
        "video",
        "调用方式",
        "enum",
        ("comfy_preset", "http_api", "comfy_workflow"),
        "comfy_preset 走模型端保存的图；http_api 走通用合同；comfy_workflow 是旧的绑定路径。",
    ),
    FieldSpec(
        "video.base_url",
        "video_base_url",
        "video",
        "服务地址",
        impact="http_api 时必填；comfy_preset 留空则用下面的 ComfyUI 地址。",
    ),
    FieldSpec("video.api_key", "video_api_key", "video", "API Key", "secret"),
    FieldSpec(
        "video.preset",
        "video_preset",
        "video",
        "默认预设",
        impact="comfy_preset 时指哪一份图；缺它无法生成。",
    ),
    FieldSpec("video.timeout", "video_timeout", "video", "单次超时（秒）", "int"),
    FieldSpec("comfy.base_url", "comfy_base_url", "comfy", "地址"),
    FieldSpec(
        "scene.node_limit",
        "scene_node_limit",
        "scene",
        "一幕里人物 / 地点的上限",
        "int",
        impact="人物与地点各自不能超过它（prompt 不受限，它是必填的那一个）。",
    ),
    FieldSpec(
        "runtime.worker_limit",
        "worker_limit",
        "runtime",
        "并发生成数",
        "int",
        impact="调大会同时挤占显存，OOM 时先把它调回 1。",
    ),
    FieldSpec(
        "runtime.ffmpeg_path",
        "ffmpeg_path",
        "runtime",
        "FFmpeg 路径",
        impact="裸名字 ffmpeg 表示用内置那份；写成路径就是指名要用它。",
    ),
)

BY_KEY = {f.key: f for f in FIELDS}


def mask(value: str) -> str:
    """密钥只回尾巴，让人能认出「填的是哪一把」，但拿不到它。"""
    text = str(value or "")
    if not text:
        return ""
    return f"{'*' * max(4, len(text) - 4)}{text[-4:]}" if len(text) > 4 else "*" * len(text)


class AppSettingsService:
    """settings.json 的读写 + 探测。进程内单例，与其它 service 同构。"""

    def __init__(self) -> None:
        #: 应用文件覆盖**之前**的值（= 环境变量或代码默认）。清除某个覆盖时要回到它。
        self._baseline: dict[str, Any] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return settings.runtime_dir / "settings.json"

    # --- 存储 ---

    def _read(self) -> dict[str, Any]:
        path = self.path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 坏文件不该让应用起不来：退回环境变量，并在日志里说清楚。
            log.warning("appsettings.unreadable", path=str(path), error=str(exc))
            return {}
        raw = data.get("overrides") if isinstance(data, dict) else None
        return {k: v for k, v in raw.items() if k in BY_KEY} if isinstance(raw, dict) else {}

    def _write(self, overrides: dict[str, Any]) -> None:
        target = self.path
        tmp = target.with_suffix(".json.tmp")
        payload = {"kind": "aivs-settings", "saved_at": utc_now(), "overrides": overrides}
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
        except OSError as exc:
            raise AppError(
                ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
                "设置写入失败",
                f"{target}: {type(exc).__name__}: {exc}",
                ["确认磁盘可写且空间充足", "或改用 AIVS_ 环境变量配置"],
            ) from exc

    # --- 生效 ---

    def apply(self) -> dict[str, Any]:
        """把文件里的覆盖写进 settings 单例。启动时与每次 PATCH 后都要调。"""
        overrides = self._read()
        if not self._loaded:
            self._baseline = {f.attr: getattr(settings, f.attr) for f in FIELDS}
            self._loaded = True
        for spec in FIELDS:
            raw = overrides.get(spec.key)
            value = self._baseline[spec.attr] if raw is None else _coerce(spec, raw)
            setattr(settings, spec.attr, value)
        return overrides

    def _source_of(self, key: str, overrides: dict[str, Any]) -> str:
        if key in overrides:
            return "file"
        spec = BY_KEY[key]
        default = Settings.model_fields[spec.attr].default
        # 环境变量与 .env 都会体现在「启动值 ≠ 代码默认」上，两者对用户是同一件事。
        return "env" if self._baseline.get(spec.attr) != default else "default"

    # --- 对外形状 ---

    def snapshot(self) -> dict[str, Any]:
        overrides = self.apply()
        fields = []
        for spec in FIELDS:
            value = getattr(settings, spec.attr)
            secret = spec.kind == "secret"
            fields.append(
                {
                    "key": spec.key,
                    "group": spec.group,
                    "label": spec.label,
                    "kind": spec.kind,
                    "choices": list(spec.choices),
                    "impact": spec.impact,
                    "source": self._source_of(spec.key, overrides),
                    "value": None if secret else value,
                    "masked": mask(str(value)) if secret else None,
                    "has_value": bool(value) if secret else None,
                }
            )
        return {
            "path": self.path.as_posix(),
            "groups": [{"id": gid, "title": title} for gid, title in GROUPS],
            "fields": fields,
            "llm": llm.status(),
        }

    async def patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        """写覆盖。值为 `null` 表示**清除覆盖**，回到环境变量或默认。"""
        unknown = [k for k in patch if k not in BY_KEY]
        if unknown:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "有不认识的设置项",
                "、".join(unknown),
                ["只提交 GET /settings 里出现过的 key"],
                {"unknown": unknown},
            )
        overrides = self._read()
        for key, raw in patch.items():
            spec = BY_KEY[key]
            if raw is None or (spec.kind == "secret" and raw == ""):
                overrides.pop(key, None)
                continue
            overrides[key] = _coerce(spec, raw)
        self._write(overrides)
        self.apply()
        log.info("appsettings.patched", keys=sorted(patch))
        return self.snapshot()

    # --- 探测 ---

    async def probe(self, what: str) -> dict[str, Any]:
        """配置页的「测试连接」。失败一律是四要素错误，不是一个红叉。"""
        if what == "llm":
            return await self._probe_llm()
        if what == "video":
            from app.generation.providers import registry

            return await registry.provider().probe()
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识的探测对象",
            f"what={what}",
            ["用 llm 或 video"],
        )

    async def _probe_llm(self) -> dict[str, Any]:
        llm.require_configured()
        base = settings.llm_base_url.rstrip("/")
        if settings.llm_provider == "ollama":
            url = f"{base or 'http://127.0.0.1:11434'}/api/tags"
            headers: dict[str, str] = {}
        else:
            url = f"{base or 'https://api.openai.com/v1'}/models"
            headers = (
                {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
            )
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as http:
                resp = await http.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.LLM_UNAVAILABLE,
                "LLM 服务连不上",
                f"{url}：{type(exc).__name__}: {exc}",
                [
                    "确认地址与端口正确（本机服务通常是 127.0.0.1）",
                    "确认 API Key 与协议匹配",
                    "AI 协作不可用时手动编排仍能走完全程",
                ],
                {"url": url},
            ) from exc
        except ValueError as exc:
            raise AppError(
                ErrorCode.LLM_INVALID_OUTPUT,
                "LLM 服务返回的不是 JSON",
                f"{url}：{type(exc).__name__}: {exc}",
                ["确认这个地址是 OpenAI 兼容 / Ollama 接口，而不是网页"],
                {"url": url},
            ) from exc
        names = _model_names(data)
        found = settings.llm_model in names if names else None
        return {
            "ok": True,
            "target": url,
            "model_count": len(names),
            "model_present": found,
            "detail": (
                f"连通 · {len(names)} 个模型"
                + ("" if found is not False else f"，但其中没有 {settings.llm_model}——调用时会失败")
            ),
        }


def _coerce(spec: FieldSpec, raw: Any) -> Any:
    """按字段类型收紧输入。写错了当场说清楚，绝不悄悄存一个坏值。"""
    try:
        if spec.kind == "int":
            value: Any = int(raw)
            if value <= 0:
                raise ValueError("必须为正整数")
        elif spec.kind == "float":
            value = float(raw)
        elif spec.kind == "bool":
            value = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes")
        else:
            value = str(raw).strip()
            if spec.kind == "enum" and value not in spec.choices:
                raise ValueError(f"只能是 {'、'.join(spec.choices)}")
    except (TypeError, ValueError) as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"{spec.label} 的取值不合法",
            f"{spec.key} = {raw!r}：{exc}",
            ["按提示改正后重试", "或提交 null 清除这项覆盖，回到环境变量的值"],
            {"key": spec.key},
        ) from exc
    return value


def _model_names(data: Any) -> list[str]:
    """OpenAI 兼容与 Ollama 的模型列表长得不一样，这里只取名字。"""
    if isinstance(data, dict):
        rows = data.get("data") or data.get("models") or []
        if isinstance(rows, list):
            return [
                str(r.get("id") or r.get("name") or "")
                for r in rows
                if isinstance(r, dict) and (r.get("id") or r.get("name"))
            ]
    return []


app_settings = AppSettingsService()
