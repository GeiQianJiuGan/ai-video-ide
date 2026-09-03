"""工程路由：**「这个工程 + 这个能力 → 走哪条路出片」只有这一份口径。**

以前这件事没有口径，于是三条路只有一条真的能跑：`enqueue_shot` 里写死
`generation_mode = "comfy_preset"`、执行时写死 `registry.provider("comfy_preset")`，
`project.generation_mode` 是一列**只写值**。界面上选了「通用 REST API」或「ComfyUI 工作流
绑定」，后端照旧提交给 ComfyUI 预设——选了等于没选，而冻结进版本的参数里还写着用户选的
那条路（破硬约束 3、4）。

这里一次答完四个问题：

  · **走哪条路**（`provider`）以及**这个答案是谁给的**（`source`）。工程那一列为空
    = 跟随设置页，不是「没配置」——绝大多数工程是这一种。
  · **这条路要绑什么**（`binds_workflow` / `preset` / `workflow_id` / `base_url`）。
    界面上那句「这条路不需要工作流绑定」就是 `binds_workflow` 这一个布尔。
  · **绑没绑上**（`ready`）与**缺什么**（`issues`，四要素形状，前端原样显示 suggestions）。
  · **一次能喂几个参考素材**（`capacity()`）：按真正会提交的那条路数，
    不再一律去数 R2V 预设。

三条约定：

  1. **只读、绝不抛**：`resolve()` / `capacity()` 服务的是账单、概览页、编排计划这些只读
     路径，在那里因为「没配地址」就 500，用户连缺什么都看不到。要挡的地方调 `require()`，
     它把 `issues[0]` 抛出去（入队那道门槛）。
  2. **readiness 全部复用现成的判断**，这里一条新规则都不写：预设那条读
     `presets.listing()` 的 `r2v_ready` / `flf_ready`，REST 那条判 `video_base_url` +
     `http_api` 那份合同，绑定那条直接收 `workflows.resolve()` 抛的 `AppError`。
  3. **密钥一次都不经过这里**：地址会进版本参数与界面（排查时第一个要看的东西），
     密钥不会——与「API key 永不回明文」是同一条。

入队时解析一次并冻结进 `job.params_json["route"]`，执行时只读冻结值：**重试不重新解析**，
否则中途改了设置会让「重试」变成「换个后端跑一遍」，而版本上写的还是旧那条（硬约束 3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, settings
from app.core.errors import AppError, ErrorCode
from app.generation.providers import presets, registry
from app.generation.providers.base import RefCapacity
from app.generation.providers.http_api import CONTRACT, missing_base_error
from app.persistence.models import Project
from app.persistence.models_global import GlobalWorkflow
from app.persistence.models_story import Shot
from app.services.base import db_of, fetch_all, load_json
from app.services.workflows import workflows

#: 老库 / 老客户端的别名。项目列与 `api/workflows.py` 历史上叫 `workflow_api`，registry 与
#: 设置页叫 `comfy_workflow`，中间从来没有任何映射——**同一条路两个名字**。归一到 registry
#: 那个名字（`0022_project_route` 已经把库里的老值改过来了），读写两侧各过一次 `normalize()`
#: 收老客户端。
ALIAS: dict[str, str] = {"workflow_api": "comfy_workflow"}

#: 工程那一列为空 = **跟随设置页**，不是「没配置」。绝大多数工程是这一种。
INHERIT = ""

#: 走首尾帧那套参数的能力。与 `services/params.py::resolve_rows` 里那行判断同一份口径：
#: 转场与 FL2VA 都是「两头都给帧」，所以预设也该取 `flf_preset_name` 那一份。
FLF_CAPABILITIES = frozenset({"first_last_frame", "transition", "fl2va"})

#: 能力 → 绑定表里的哪一格。绑定表只有四个能力（`services/workflows.py::CAPABILITIES`），
#: 转场与 FL2VA 在那张表上没有自己的格子，落到首尾帧那一份图上。
WORKFLOW_CAPABILITY: dict[str, str] = {
    "transition": "first_last_frame",
    "fl2va": "first_last_frame",
}

#: 绑定表里参考图槽位那一项（`services/workflows.py::_global_shape()` 给的键）。
REF_SLOTS_KEY = "reference_image_slots"

#: 代码里写的那个默认值。用它分辨 `source` 是 `settings`（用户在设置页或环境变量里改过）
#: 还是 `default`（谁都没选过），思路照 `services/appsettings.py::_source_of`——但**不 import
#: 它**：那边要先 `apply()` 把 `_baseline` 装起来，只读路径不该依赖这个时序。
_DEFAULT_PROVIDER: str = str(Settings.model_fields["video_provider"].default or "")


@dataclass(frozen=True)
class Route:
    """一条解析完的出片路：**解析那一刻的只读快照**，入队时整个冻结进版本参数。"""

    provider: str
    label: str
    #: 这个答案是谁给的：`project`（工程显式选了这条）/ `settings`（跟随设置页，也包括
    #: `AIVS_VIDEO_PROVIDER` 那一层）/ `default`（谁都没选过，用的是代码里那个默认值）。
    source: str
    capability: str
    #: **这条路要不要绑 workflow**：只有 `comfy_workflow` 是 `True`。界面上那句
    #: 「这条路不需要工作流绑定」与四个能力下拉的 `:disabled` 都只看这一个布尔。
    binds_workflow: bool
    #: **这条路要绑的是什么**（`BINDS` 那张表的值）：`preset` / `base_url` / `workflow`，
    #: 未知调用方式是空串。`binds_workflow` 是它的一个特例，留着是因为界面上「要不要绑图」
    #: 那一处只关心那一个布尔；需要三岔的地方（概览页画哪一组控件、二次处理能不能做）
    #: 读这一个字段——**照事实分岔，不照调用方式的名字**（硬约束 1）。
    binds: str = ""
    #: `comfy_preset` 用哪一份图——**已经按继承顺序解析到具体那一份**（`preset_name_of`）。
    preset: str | None = None
    workflow_id: str | None = None
    workflow_name: str | None = None
    #: 只有 `http_api` 有；**永不带密钥**。
    base_url: str | None = None
    ready: bool = True
    #: 缺什么。四要素形状（`AppError.to_dict()`），前端原样显示 suggestions。
    issues: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """摆给界面看的形状（`GET /projects/{pid}/route`、概览页环境栏、能力矩阵）。"""
        return {
            "provider": self.provider,
            "label": self.label,
            "source": self.source,
            "capability": self.capability,
            "binds_workflow": self.binds_workflow,
            "binds": self.binds,
            "preset": self.preset,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "base_url": self.base_url,
            "ready": self.ready,
            "issues": self.issues,
        }

    def frozen(self) -> dict[str, Any]:
        """冻结进 `job.params_json["route"]` 的那一份：**只留事实，不留当时的 readiness**。

        `ready` / `issues` 说的是解析那一刻缺什么，冻进去只会让半年后翻版本参数的人
        把它当成这次任务的失败原因。地址进档（排查时第一个要看的东西），密钥永不进。
        """
        return {
            "provider": self.provider,
            "label": self.label,
            "source": self.source,
            "capability": self.capability,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "preset": self.preset,
            "base_url": self.base_url,
        }


def normalize(name: str | None) -> str:
    """把一个「调用方式」的名字收成 registry 认的那个。空 = 继承（`INHERIT`）。

    收三种输入：`None` / 空串、别名（`workflow_api`）、registry 里的正名。

    **未知值不静默回退到默认那条**——那正是这次要修的 bug 的形状（选了等于没选）。
    库里读出来的坏值由 `resolve()` 收成一条 issue（只读路径不该 500），
    写入侧（`PUT /workflow-bindings`、包导入）则直接把这个 `VALIDATION_ERROR` 抛给用户。
    """
    raw = (name or "").strip()
    if not raw:
        return INHERIT
    fixed = ALIAS.get(raw, raw)
    if fixed not in registry.BUILTIN:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识这个调用方式",
            f"「{raw}」不是可用的调用方式。",
            [
                "可选值："
                + "、".join(f"{n}（{registry.LABELS.get(n, n)}）" for n in registry.names()),
                "留空表示跟随设置页里的视频生成服务",
            ],
            {"available": registry.names(), "value": raw},
        )
    return fixed


def capability_of(shot: Shot, kind: str | None = None) -> str:
    """这个镜头要的是哪种能力。**唯一实现。**

    以前这行判断长在 `enqueue_shot` 里，而参考素材账单那侧默认按 R2V 数槽位——于是首尾帧
    镜头的账单数的是 R2V 那份预设的槽位、真正提交的却是 FLF 那份图，两个数字对不上。
    从这里开始两侧用同一个算法。

    `kind` 是调用方显式指定的（补转场、编排里那条链、二次处理），给了就以它为准。
    """
    if kind:
        return kind
    return "first_last_frame" if shot.prev_shot_id else "image2video"


#: 角色 → 代表能力。`api/projects.py::PUT /preset` 收的是 `r2v` / `flf` 这两个角色名
#: （用户在概览页选的就是这两份预设），而这里一律按能力说话，所以给一张对照表让两侧
#: 共用同一段「预设不可用」的文案。
ROLE_CAPABILITY: dict[str, str] = {"flf": "first_last_frame", "r2v": "image2video"}


def preset_role(capability: str) -> str:
    """预设那条路上只有两个角色：首尾帧那一份图，与其余那一份。"""
    return "flf" if capability in FLF_CAPABILITIES else "r2v"


def preset_ready(name: str | None, capability: str) -> bool:
    """这份预设能不能用于这个能力。

    **判断整个来自 `presets.listing()`**（`r2v_ready` / `flf_ready`），这里一条新规则都不写：
    两个角色要的入口本来就不一样（R2V 只要 `AIVS_PROMPT`，FLF 还要两头的帧），
    再实现一遍必然和设置页那份预设列表说的不一致。
    """
    if not name:
        return False
    key = "flf_ready" if preset_role(capability) == "flf" else "r2v_ready"
    item = next((x for x in presets.listing() if x["name"] == name), None)
    return bool(item and item.get(key))


def preset_error(name: str | None, capability: str) -> AppError:
    """「还没有选生成预设」/「预设不可用」——**这两句话只有这一份**。

    `api/projects.py::PUT /preset`（选的时候）、入队门槛（按下生成的时候）、概览页那份账单
    （还没按的时候）共用它。三处各写一遍的话，用户会在三个地方看到三种说法。

    **「它其实是出图那份图」要单独说**：一份标了 `AIVS_IMAGE` 的图入口标题一个不缺，
    照通用那句话说下去会让用户去改一个本来没问题的标题，而真正的原因是这份图声明了自己
    是出图用的（见 `providers/presets.DECLARATIONS`）。
    """
    role = preset_role(capability)
    which = "首尾帧 / FL2VA" if role == "flf" else "R2V"
    if not name:
        return AppError(
            ErrorCode.MISSING_CAPABILITY,
            "还没有选生成预设",
            f"这个工程（以及设置页）都没有指定{which}要用哪一份预设。",
            [
                "在概览页的「这个工程怎么出片」里选一份预设",
                "或在设置页选一份默认预设——不单独指定的工程会跟着它",
                "预设要先在「预设 Workflow」里导入并校验通过",
            ],
            {"capability": capability, "role": role},
        )
    item = next((x for x in presets.listing() if x["name"] == name), None)
    if item and item.get("declares_image"):
        return AppError(
            ErrorCode.INVALID_WORKFLOW,
            "这是出图那份图，不能用来出画面",
            f"预设 {name} 里标了 {presets.DECLARE_IMAGE}，它声明自己是"
            f"{presets.DECLARATIONS[presets.DECLARE_IMAGE]}，所以不出现在{which}的候选里。",
            [
                f"{which}请另选一份没标 {presets.DECLARE_IMAGE} 的预设",
                "这份图要用在「图片生成 API」那一栏（设置页 → 设为出图默认）",
                f"如果它其实是出画面那份图，把节点上的 {presets.DECLARE_IMAGE} 标题去掉",
            ],
            {"preset": name, "role": role, "capability": capability, "declares_image": True},
        )
    return AppError(
        ErrorCode.INVALID_WORKFLOW,
        "预设不可用",
        f"预设 {name} 不存在或不能用于{which}。",
        [
            "到左侧「预设 Workflow」导入并修复这份图",
            # 两个角色要的东西不一样，说错一句用户就会去改一个本来没问题的标题：
            # R2V 只要一个提示词入口（首尾帧节点可以一个都没有），
            # 补转场要的是严格首尾帧，缺哪一头都接不上。
            (
                "FL2VA 预设必须同时标出 AIVS_FIRST_FRAME、AIVS_LAST_FRAME、AIVS_PROMPT"
                if role == "flf"
                else "R2V 预设至少要标出 AIVS_PROMPT；首尾帧节点没有也行，首帧会当作参考图 1 送进去"
            ),
            "再回项目选择它",
        ],
        {"preset": name, "role": role, "capability": capability},
    )


def preset_name_of(project: Project, capability: str, override: str | None = None) -> str | None:
    """这个工程这个能力最终会提交哪一份预设。

    继承顺序与 `services/params.py::resolve_rows` 那张账单**逐格相同**：
    场景参数覆写 → 角色默认（`flf_preset_name` / `r2v_preset_name`）→ 工程唯一那份
    （`preset_name`）→ 设置页那份（`video_preset`）。最后一级是 `params.py` 上没有的
    （那张表只算工程内的继承），但真正提交时 `comfy_preset` 就是这么退的——
    **账单不能比事实少一级**，不然界面说「没选预设」而生成却成功了。
    """
    role_default = (
        project.flf_preset_name if capability in FLF_CAPABILITIES else project.r2v_preset_name
    )
    for candidate in (override, role_default, project.preset_name, settings.video_preset):
        name = (candidate or "").strip()
        if name:
            return name
    return None


#: **每条路要绑什么。** 这是本文件里唯一一处按名字分岔的地方：硬约束 1 管的是业务层不许认路，
#: 而「哪条路要什么」这件事总得有一个人知道。收在这一张表里之后，其它模块只读 `Route` 上那几个
#: 字段（`binds_workflow` / `preset` / `workflow_id` / `base_url`），一个 `if provider ==` 都不写。
#: 新增适配器时在这里登记它要绑什么，`resolve()` 与 `capacity()` 都不用改。
BINDS: dict[str, str] = {
    "comfy_preset": "preset",
    "http_api": "base_url",
    "comfy_workflow": "workflow",
}


def _as_error(issue: dict[str, Any]) -> AppError:
    """把一条 issue 还原成可以抛的 `AppError`（`require()` 用）。

    `Route.issues` 存的是 `to_dict()` 之后的字典：它要能原样进 JSON 给前端、也要能冻进
    参数里。抛之前在这里还原，四要素一个字都不会变形。
    """
    try:
        code = ErrorCode(str(issue.get("code") or ""))
    except ValueError:  # pragma: no cover - 只在有人手改过 issue 时发生
        code = ErrorCode.MISSING_CAPABILITY
    return AppError(
        code,
        str(issue.get("title") or "这条出片路还不能用"),
        str(issue.get("detail") or ""),
        list(issue.get("suggestions") or []),
        dict(issue.get("related_ids") or {}),
    )


def _pick_provider(project: Project) -> tuple[str, str, list[dict[str, Any]]]:
    """走哪条路、这个答案是谁给的、路上捡到的坏值。

    **工程那一列为空 = 跟随设置页**，不是「没配置」——绝大多数工程是这一种。`source` 三个值：
    `project`（工程显式选了这条）/ `settings`（跟随设置页，含 `AIVS_VIDEO_PROVIDER` 那一层）/
    `default`（谁都没选过，用的是代码里那个默认值）。分辨后两者的办法与
    `services/appsettings.py::_source_of` 同一个思路——和代码默认值比一比，所以在设置页里
    手动选了一遍「ComfyUI 预设」时这里仍然会说 `default`。不精确，但只读路径不该依赖那边
    `apply()` 过没有的 `_baseline`，而这两个值对用户的意义是一样的：**都是设置页说的**。
    """
    issues: list[dict[str, Any]] = []
    raw = (project.generation_mode or "").strip()
    if raw:
        try:
            return normalize(raw), "project", issues
        except AppError as exc:
            #: 库里那一列是老客户端写的、或者被手改过：说出来，然后按「没选」继续往下走——
            #: 只读页面因为一个坏字符串整页 500 的话，用户连缺什么都看不到。
            issues.append(exc.to_dict())
    chosen = str(settings.video_provider or "").strip() or _DEFAULT_PROVIDER
    source = "settings" if chosen != _DEFAULT_PROVIDER else "default"
    try:
        return normalize(chosen), source, issues
    except AppError as exc:
        issues.append(exc.to_dict())
        return chosen, source, issues


async def _resolve(
    pid: str,
    capability: str,
    *,
    project: Project | None = None,
    preset: str | None = None,
    workflow_id: str | None = None,
) -> tuple[Route, GlobalWorkflow | None]:
    """`resolve()` / `require()` / `capacity()` 共用的那一次解析。

    多回一个 `GlobalWorkflow`：`capacity()` 要数那份图上的参考图槽位，重新取一次不只是
    多一次跨库读——中间有人改过绑定的话，两次的答案还会不一样。
    """
    if project is None:
        project = (await fetch_all(db_of(pid), Project))[0]
    provider_name, source, issues = _pick_provider(project)
    binds = BINDS.get(provider_name, "")
    preset_name: str | None = None
    base_url: str | None = None
    row: GlobalWorkflow | None = None
    if binds == "preset":
        preset_name = preset_name_of(project, capability, preset)
        if not preset_ready(preset_name, capability):
            issues.append(preset_error(preset_name, capability).to_dict())
    elif binds == "base_url":
        base_url = (settings.video_base_url or "").strip().rstrip("/") or None
        if base_url is None:
            issues.append(missing_base_error().to_dict())
    elif binds == "workflow":
        try:
            row = await workflows.resolve(
                pid, WORKFLOW_CAPABILITY.get(capability, capability), workflow_id
            )
        except AppError as exc:
            #: 「这个能力还没绑图」/「那份图没校验过」——`workflows.resolve()` 已经把四要素
            #: 连影响那句（`_impact()`）都说全了，这里原样收下，绝不另写一份判断。
            issues.append(exc.to_dict())
    route = Route(
        provider=provider_name,
        label=registry.LABELS.get(provider_name, provider_name),
        source=source,
        capability=capability,
        binds_workflow=binds == "workflow",
        binds=binds,
        preset=preset_name,
        workflow_id=row.id if row is not None else None,
        workflow_name=row.name if row is not None else None,
        base_url=base_url,
        ready=not issues,
        issues=issues,
    )
    return route, row


async def resolve(
    pid: str,
    capability: str,
    *,
    preset: str | None = None,
    workflow_id: str | None = None,
) -> Route:
    """这个工程这个能力走哪条路。**只读、绝不抛。**

    服务的是账单、概览页、能力矩阵、编排计划这些只读路径：在那里因为「没配地址」就 500，
    用户连缺什么都看不到（这正是硬约束 4 要防的）。缺什么在 `issues` 里，四要素齐全。
    """
    route, _ = await _resolve(pid, capability, preset=preset, workflow_id=workflow_id)
    return route


async def for_project(
    pid: str,
    project: Project,
    capability: str,
    *,
    preset: str | None = None,
    workflow_id: str | None = None,
) -> Route:
    """同 `resolve()`，但 `Project` 那一行由调用方给——`enqueue_shot` 已经读过它了。"""
    route, _ = await _resolve(
        pid, capability, project=project, preset=preset, workflow_id=workflow_id
    )
    return route


async def require(
    pid: str,
    capability: str,
    *,
    project: Project | None = None,
    preset: str | None = None,
    workflow_id: str | None = None,
) -> Route:
    """入队那道门槛：不 ready 就把 `issues[0]` 抛出去。

    为什么门槛在入队而不在执行：以前缺地址 / 缺预设 / 没绑图统统要等 pump 跑到那一条才炸，
    用户按十次生成就在队列里躺十条失败，而错误里只剩「ComfyUI 未连接」这种与真正原因差一层
    的话。挡在入队之后，按钮按下去立刻是四要素错误，队列里保持干净。

    **已入队的任务重试时不再走这里**（`_execute` 只读冻结的那一份路由）——否则中途改了设置
    会让「重试」变成「换个后端跑一遍」，而版本参数上写的还是旧那条（硬约束 3）。
    """
    route, _ = await _resolve(
        pid, capability, project=project, preset=preset, workflow_id=workflow_id
    )
    if not route.ready:
        raise _as_error(route.issues[0])
    return route


async def capacity(
    pid: str,
    capability: str,
    *,
    project: Project | None = None,
    preset: str | None = None,
    workflow_id: str | None = None,
) -> RefCapacity:
    """这个工程这个能力一次能喂几个参考素材（三种媒体各一个数）。**绝不抛。**

    **按真正会提交的那条路数。** 以前这里一律去数 R2V 预设：于是首尾帧镜头数的是一份不会被
    提交的图、REST 那条路被硬算成某份预设的槽位（那条路根本没有槽位这回事）、工作流绑定那条
    路数出来的数字和它真正能填的格子毫无关系。账单上那个「超出会丢几个」的确认于是也是假的。

    分岔看的是**事实而不是名字**（硬约束 1）：解析出预设了就数那份预设，解析出图了就数那份图
    上的参考图槽位，两者都没有才回去问适配器自己（REST 那条路答的是「不限」）。
    """
    route, row = await _resolve(
        pid, capability, project=project, preset=preset, workflow_id=workflow_id
    )
    return _capacity(route, row)


def _capacity(route: Route, row: GlobalWorkflow | None) -> RefCapacity:
    """已经解析完之后那一步：数槽位。`capacity()` 与 `summary()` 共用，不重复解析。"""
    if route.preset:
        return presets.capacity_of(route.preset)
    if row is not None:
        slot_names = load_json(row.bindings_json, {}).get(REF_SLOTS_KEY) or []
        count = len(slot_names)
        head = (
            f"工作流「{row.name}」的绑定表里标了 {count} 个参考图槽位，"
            f"一次最多喂 {count} 张参考图。"
            if count
            else f"工作流「{row.name}」的绑定表里一个参考图槽位都没有——"
            "角色图 / 场景图全都喂不进去，人物形象只能靠首帧带。"
        )
        return RefCapacity(
            count,
            row.name,
            head + "这条路只喂图片：参考视频 / 参考音频要改走 ComfyUI 预设或通用 REST API。",
            video=0,
            audio=0,
        )
    #: 没有可数的图（REST 合同那条）：适配器自己回答，`registry.ref_capacity()` 不抛。
    return registry.ref_capacity(route.provider)


def slots(cap: RefCapacity) -> dict[str, Any]:
    """参考素材槽位摆给界面看：三种媒体各一个数。

    `None` = 不限制，`0` 是**有意义的答案**（那份图一个槽都没标），所以两者不能都渲染成
    「—」。判断留给前端，这里只如实转述。
    """
    return {
        "source": cap.source,
        "detail": cap.detail,
        "image": cap.limit,
        "video": cap.video,
        "audio": cap.audio,
    }


def _safe_normalize(name: str | None) -> str:
    """只读路径上的 `normalize()`：坏值原样回，不抛。

    坏值该说的话已经在 `Route.issues` 里了（`_pick_provider` 收的那条）；这里再抛一次
    只会让整个概览页变成一个 500，而用户正是来这儿看「哪里不对」的。
    """
    try:
        return normalize(name)
    except AppError:
        return (name or "").strip()


#: 概览页那一块要同时说清的两条路：普通镜头怎么出、衔接镜头怎么出。
SUMMARY_CAPABILITIES: tuple[str, ...] = ("image2video", "first_last_frame")

CAPABILITY_LABEL: dict[str, str] = {
    "image2video": "普通镜头（图生视频）",
    "first_last_frame": "衔接与转场（首尾帧）",
}


async def summary(pid: str) -> dict[str, Any]:
    """`GET /projects/{pid}/route`：**一个请求画完概览页那一块。**

    照 `GET /projects/{pid}/preset` 一次回 r2v + flf 的作风，两条路一起回——分两个请求
    只会让界面先画一半再跳一下。每条路带自己的 `slots`（参考素材槽位）与 `issues`。

    `mode` 是工程那一列的原样值（空 = 跟随设置页），`options` 的第一项就是「跟随设置页」，
    其余来自 `registry.listing()`：**前端一个调用方式的名字都不写死**。
    """
    project = (await fetch_all(db_of(pid), Project))[0]
    routes: list[dict[str, Any]] = []
    for cap in SUMMARY_CAPABILITIES:
        route, row = await _resolve(pid, cap, project=project)
        item = route.to_dict()
        item["capability_label"] = CAPABILITY_LABEL.get(cap, cap)
        item["slots"] = slots(_capacity(route, row))
        routes.append(item)
    first = routes[0]
    return {
        #: 工程那一列的原样值（已过 `normalize()`，老库里的 `workflow_api` 读出来是
        #: `comfy_workflow`）。空串 = 跟随设置页，前端那个下拉的第一项就选它。
        "mode": _safe_normalize(project.generation_mode),
        "provider": first["provider"],
        "label": first["label"],
        "source": first["source"],
        "binds_workflow": first["binds_workflow"],
        #: 这条路要绑的是什么：`preset` / `base_url` / `workflow`。概览页照它决定画哪一组
        #: 控件（两个预设下拉 / 一行地址说明 / 四个能力下拉），**不写死调用方式的名字**。
        "binds": first["binds"],
        "options": [
            {"name": INHERIT, "label": "跟随设置页", "inherit": True, "binds": ""},
            #: 每条候选带上**它要绑什么**（`BINDS`）：界面上「这四个下拉要改成哪一条才生效」
            #: 只能由这份答案回答。前端自己按名字猜的话，`comfy_workflow` 这个字符串就又
            #: 写死进前端了（硬约束 1）。
            *(
                {**row, "inherit": False, "binds": BINDS.get(row["name"], "")}
                for row in registry.listing()
            ),
        ],
        #: 设置页那条（也就是「留空会走哪条」），让界面把继承来的那条也写出来。
        "settings_provider": _safe_normalize(settings.video_provider or _DEFAULT_PROVIDER),
        "capabilities": routes,
        #: REST 那条路上服务端要实现什么。写死在前端的话，改合同就得改两处。
        "contract": list(CONTRACT),
    }
