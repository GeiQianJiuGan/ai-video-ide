"""参数解析：**「这一项到底从哪儿来的」只有这一份口径。**

三级继承 **工程 → 幕 → 镜头**，取值规则一句话：**镜头上那一项为空就往上找**。
`NULL` 是「继承」，不是 0 也不是空串——所以「长视频维护同一份 prompt 和素材」不需要
第二套代码路径，只是把幕上那一份填好、镜头上留空而已（`Scene.param_mode="shared"`）。
反过来要每个镜头各自独立，就在创建时把幕级值预填到镜头上（`per_shot`，剧本拆分镜的老行为）。
**`param_mode` 只影响创建那一刻**，解析永远只有下面这一条路。

为什么要单独一个模块：这套回退以前散在四处各写一遍——
`generation.enqueue_shot`（冻结进版本的那个 prompt）、`context.resolve`（账单里的 problems）、
`story.storyboard`（卡片上的黄色感叹号）、`story.scene_nodes`（`prompt_ok`）。
四处只要有一处漏了一级，界面就会出现「账单说齐了，冻结进版本里的 prompt 却是空的」。

**解析结果是一张账单**（`{value, level, inherited}`），沿用项目里 `source` 那个作风：
用户看得见这个值是自己写的、继承自幕、还是工程默认——看不见的继承等于猜。
"""

from __future__ import annotations

from typing import Any

from app.persistence.models import Project
from app.persistence.models_story import Scene, Shot
from app.services import route
from app.services.base import db_of, fetch, fetch_all, load_json

#: **一幕要算「信息齐了」得有什么**，按它是怎么来的分。照 `models_gen.REQUIRED_SLOTS`
#: 那张表的样子写成一处查表，而不是散在几个 `if scene.kind == ...` 里。
#:
#:   storyboard 剧本拆出来的：prompt / 地点 / 出场角色——画面全靠这三样生成；
#:   ingested   从成片切出来的：**什么都不必填**。画面已经有了，再要求写 prompt、选地点、
#:              挑角色只是三道毫无意义的门槛（长视频处理这条路上压根没有剧本）。
SCENE_REQUIRED: dict[str, tuple[str, ...]] = {
    "storyboard": ("prompt", "location", "cast"),
    "ingested": (),
}

#: 幕级参数里认得的键。多写的键原样留着（前端可能先行一步），但不参与解析。
SCENE_PARAM_KEYS = ("prompt", "negative", "duration", "seed", "steps", "preset", "refs")

_MISSING = (None, "", [], {})


def requires(scene: Scene, field: str) -> bool:
    """这一幕按它的来源要不要这一项（`prompt` / `location` / `cast`）。"""
    return field in SCENE_REQUIRED.get(scene.kind, SCENE_REQUIRED["storyboard"])


def scene_params(scene: Scene) -> dict[str, Any]:
    """幕级共用参数。坏 JSON 退回空字典并保持可用（`load_json` 的老规矩）。"""
    data = load_json(scene.params_json, {})
    return data if isinstance(data, dict) else {}


def _pick(*candidates: tuple[Any, str]) -> tuple[Any, str]:
    for value, level in candidates:
        if value not in _MISSING:
            return value, level
    return None, "default"


def _cell(value: Any, level: str) -> dict[str, Any]:
    return {"value": value, "level": level, "inherited": level not in ("shot", "default")}


def prompt_of(shot: Shot, scene: Scene) -> str:
    """喂给模型的那句 prompt。**镜头 prompt → 幕 prompt → 镜头画面描述。**

    `generation.enqueue_shot` 冻结进版本的、`context.resolve` 检查的、分镜卡片上标黄的
    都必须是这一个函数——三处对不上就会出现「账单说齐了，生成的时候却是空的」。
    """
    value, _ = _pick(
        (shot.prompt, "shot"),
        (scene_params(scene).get("prompt"), "scene"),
        (scene.prompt, "scene"),
        (shot.description, "shot"),
    )
    return str(value or "").strip()


def prompt_required(scene: Scene) -> bool:
    return requires(scene, "prompt")


def prompt_missing(shot: Shot, scene: Scene) -> bool:
    """缺 prompt 算不算问题。**导入幕不算**——画面已经有了，不靠 prompt 生成。"""
    return prompt_required(scene) and not prompt_of(shot, scene)


def scene_issues(scene: Scene) -> list[str]:
    """这一幕按它的来源还缺什么。空列表 = 齐了。"""
    missing = []
    if requires(scene, "prompt") and not (scene.prompt or "").strip():
        missing.append("这一幕还没写 prompt")
    return missing


def resolve_rows(
    shot: Shot, scene: Scene, project: Project, *, capability: str = "image2video"
) -> dict[str, Any]:
    """一张账单：每一项的值 + 它来自哪一级。**只读，不碰库**，所以谁都能调。

    `level` 是 `shot` / `scene` / `project` / `app` / `default`：`app` 只出现在预设那一格
    （工程没绑预设时跟随设置页的那一份，见 `route.app_preset_of`），它读的是应用级设置
    而不是库，所以「不碰库」照旧成立。
    """
    sp = scene_params(scene)
    flf = capability in ("first_last_frame", "transition", "fl2va")
    preset_default = (project.flf_preset_name if flf else project.r2v_preset_name) or None
    fields = {
        "prompt": _cell(
            *_pick(
                (shot.prompt, "shot"),
                (sp.get("prompt"), "scene"),
                (scene.prompt, "scene"),
                (shot.description, "shot"),
            )
        ),
        "negative": _cell(
            *_pick(
                (shot.negative_prompt, "shot"),
                (sp.get("negative"), "scene"),
                (project.negative_prompt, "project"),
            )
        ),
        "seed": _cell(*_pick((shot.seed, "shot"), (sp.get("seed"), "scene"))),
        "steps": _cell(*_pick((shot.steps, "shot"), (sp.get("steps"), "scene"))),
        "preset": _cell(
            *_pick(
                (sp.get("preset"), "scene"),
                (preset_default, "project"),
                (project.preset_name, "project"),
                #: **账单不能比事实少一级。** 工程那三列为空 = 跟随设置页
                #: （`route.app_preset_of`：按角色那一项 → 共用那一项），新建工程刻意不再把
                #: 当时的应用级默认物化进库，所以这一级现在是绝大多数工程真正用的那一份。
                #: 少了它，界面会说「没选预设」而按下生成却成功——那正是硬约束 4 要防的。
                (route.app_preset_of(capability), "app"),
            )
        ),
        #: 幕级追加的参考素材（`Asset.id` 列表）。镜头自己的出场表照旧由
        #: `context.resolve` 解析，这里只是「这一幕所有镜头都要带上的那几张」。
        "refs": _cell(*_pick((sp.get("refs"), "scene"))),
        #: `Shot.duration` 是非空列（默认 4.0），所以它永远来自镜头；幕级那个值只在
        #: **新建镜头时预填**。故意不把它做成可空——为了这点收益重建 `shot` 这张
        #: 中心表不值得，切段出来的镜头时长本来就由区间决定。
        "duration": _cell(shot.duration, "shot"),
    }
    scene_default = sp.get("duration")
    if scene_default not in _MISSING:
        fields["duration"]["scene_default"] = scene_default
    return {
        "shot_id": shot.id,
        "scene_id": scene.id,
        "scene_kind": scene.kind,
        "param_mode": scene.param_mode,
        "capability": capability,
        "fields": fields,
        #: 按这一幕的来源还缺什么（`SCENE_REQUIRED`）。
        "missing": ["既没有 prompt 也没有画面描述"] if prompt_missing(shot, scene) else [],
    }


async def resolve(pid: str, shot_id: str, *, capability: str = "image2video") -> dict[str, Any]:
    db = db_of(pid)
    shot = await fetch(db, Shot, shot_id, "镜头")
    scene = await fetch(db, Scene, shot.scene_id, "场景")
    project = (await fetch_all(db, Project))[0]
    return resolve_rows(shot, scene, project, capability=capability)


def prefill(scene: Scene) -> dict[str, Any]:
    """新建镜头时要写实到镜头上的幕级参数。

    `shared` 返回空字典（留空 = 解析时回退到幕，改一处三十段跟着变）；
    `per_shot` 把幕上那几项抄下来，于是每个镜头从此各自独立。
    **只在创建那一刻用**，生成路径不看 `param_mode`。
    """
    if scene.param_mode != "per_shot":
        return {}
    sp = scene_params(scene)
    out: dict[str, Any] = {}
    if sp.get("prompt") or scene.prompt:
        out["prompt"] = sp.get("prompt") or scene.prompt
    for key, column in (("negative", "negative_prompt"), ("seed", "seed"), ("steps", "steps")):
        if sp.get(key) not in _MISSING:
            out[column] = sp[key]
    if sp.get("duration") not in _MISSING:
        out["duration"] = sp["duration"]
    return out
