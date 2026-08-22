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

from app.ai import prompts
from app.ai.llm import client as llm
from app.ai.llm import protocols as llm_protocols
from app.core.config import Settings, settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.persistence.models import utc_now

log = get_logger("appsettings")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """一个可配置字段：对外的点号键 → `Settings` 上的属性。"""

    key: str
    attr: str
    group: str
    label: str
    kind: str = "str"  # str | int | float | bool | secret | enum | text
    choices: tuple[str, ...] = ()
    #: 与 choices 一一对应的人话标签。空表示直接显示 choices 里的值。
    choice_labels: tuple[str, ...] = ()
    #: 这个字段配错了会导致什么做不出来。UI 直接显示，不在前端重写一遍。
    impact: str = ""
    #: 非空表示这个字段的取值**可以自动获取**（值就是 `POST /settings/models` 的 what）。
    #: 设置页照它画那个「自动获取」按钮，不在前端硬编码「模型这一项特殊」。
    fetch: str = ""
    #: `kind="text"` 用：留空时实际生效的那段内置文本。设置页把它当占位与「恢复内置默认」
    #: 的来源——内置提示词只有 `app/ai/prompts.py` 一份，前端绝不抄第二份。
    builtin: str = ""


#: 分组只影响配置页怎么摆，不影响存储结构。
GROUPS: tuple[tuple[str, str], ...] = (
    ("llm", "LLM（AI 协作）"),
    ("prompt", "AI 提示词"),
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
        # 协议表是唯一真源（app/ai/llm/protocols.py）：加一个协议不用改这里。
        tuple(llm_protocols.names()),
        tuple(llm_protocols.labels()),
        "none 时 AI 协作栏不可用；手动编排不受影响。",
    ),
    FieldSpec("llm.base_url", "llm_base_url", "llm", "地址", impact="留空则用协议的默认地址。"),
    FieldSpec(
        "llm.model",
        "llm_model",
        "llm",
        "模型",
        impact="没有模型名时视为未配置。",
        fetch="llm",
    ),
    FieldSpec("llm.api_key", "llm_api_key", "llm", "API Key", "secret"),
    FieldSpec(
        "prompt.breakdown",
        "prompt_breakdown",
        "prompt",
        "剧本拆解（分镜师）",
        "text",
        impact=(
            "决定 AI 把剧本拆成什么样的幕与镜头。留空用内置默认；"
            "JSON 输出形状由系统始终追加，改不坏。"
        ),
        builtin=prompts.BREAKDOWN_TASK,
    ),
    FieldSpec(
        "prompt.director",
        "prompt_director",
        "prompt",
        "AI 导演（协作栏）",
        "text",
        impact=(
            "流程图右侧那个协作栏的角色与规则。留空用内置默认；"
            "「写工具只出提案、不落库」这条边界在代码里，提示词改不动它。"
        ),
        builtin=prompts.DIRECTOR_TASK,
    ),
    FieldSpec(
        "video.provider",
        "video_provider",
        "video",
        "调用方式",
        "enum",
        ("comfy_preset", "http_api", "comfy_workflow"),
        ("ComfyUI 预设（默认）", "通用 REST API", "ComfyUI 工作流绑定（兼容）"),
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
    # 这里刻意**没有**「参考图上限」这一项：能收几张是模型端那份图的事实
    # （预设里数 AIVS_REF_* 槽位），由适配层的 ref_capacity() 回答。配一个数字只会和
    # 真实槽位打架，还得用户自己去对；超出槽位时改成生成前警告 + 确认。
    FieldSpec(
        "video.ref_labels",
        "video_ref_labels",
        "video",
        "在 prompt 里写明参考图是谁",
        "bool",
        impact=(
            "把「参考图1=林小雨（常服）」拼到 prompt 末尾。ComfyUI 那类图收不到标签，"
            "只能靠这句话让模型知道哪张是主角；不想让它动 prompt 就关掉。"
        ),
    ),
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
                    "choice_labels": list(spec.choice_labels),
                    "fetch": spec.fetch,
                    "impact": spec.impact,
                    #: 留空时实际生效的那段内置文本（只有 kind="text" 有）。
                    "builtin": spec.builtin,
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
            #: 每个协议的默认地址 / 要不要密钥 / 支不支持工具——设置页照它给提示，
            #: 不在前端抄一份「Anthropic 的地址长这样」。
            "llm_protocols": llm_protocols.listing(),
        }

    async def patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        """写覆盖。值为 `null` 表示**清除覆盖**，回到环境变量或默认。

        密钥与长文本（提示词）额外把 `""` 也当成清除：那两种字段的输入框在界面上就是
        「清空 = 恢复默认」，`""` 落盘只会变成一把空密钥或一段空提示词。
        """
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
            if raw is None or (spec.kind in ("secret", "text") and str(raw).strip() == ""):
                overrides.pop(key, None)
                continue
            overrides[key] = _coerce(spec, raw)
        self._write(overrides)
        self.apply()
        log.info("appsettings.patched", keys=sorted(patch))
        return self.snapshot()

    # --- 探测与自动获取 ---

    async def probe(self, what: str) -> dict[str, Any]:
        """配置页的「测试连接」。失败一律是四要素错误，不是一个红叉。"""
        if what == "llm":
            return await llm.probe()
        if what == "video":
            from app.generation.providers import registry

            return await registry.provider().probe()
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识的探测对象",
            f"what={what}",
            ["用 llm 或 video"],
        )

    async def models(
        self,
        what: str,
        provider: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """自动获取某个字段的候选取值（当前只有 LLM 模型）。

        协议 / 地址 / 密钥可以是**还没保存**的那份：让用户先看到模型列表再决定存什么，
        而不是先存一份可能是错的配置。这些覆盖不落盘。
        """
        if what != "llm":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这一项没有可自动获取的取值",
                f"what={what}",
                ["当前只有 llm（模型列表）支持自动获取"],
                {"what": what},
            )
        return await llm.list_models(provider=provider, base_url=base_url, api_key=api_key)


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


app_settings = AppSettingsService()
