"""ComfyUI 预设：模型端那份图的本地副本 + 入口约定。

这是「本工具不维护模型端的图」这条约束的落点。做法只有一条约定：

    用户在 ComfyUI 里把入口节点的**标题**改成 AIVS_FIRST_FRAME / AIVS_LAST_FRAME /
    AIVS_PROMPT / AIVS_NEGATIVE / AIVS_DURATION / AIVS_SEED / AIVS_REF_1…AIVS_REF_9，
    然后导出 API 格式的 json。

`AIVS_REF_*` 是**参考图**槽位，与首尾帧分开：首尾帧是「画面从哪一格开始 / 结束」，
参考图是「谁出场、在哪儿」。只有首帧时人物形象只能靠那一张图带，很容易在几秒里跑掉——
所以账单里算出来的角色表 / 地点参考图按序号填进这些槽位。图里标了几个就用几个，
一个都没标也能生成（只是丢形象的风险照旧）。

我们只按标题找这几个节点、只往里填值。图里挂了多少 lora、加了什么加速节点、
采样器换成了什么——一概不看、不校验、不改写。模型端想怎么调就怎么调，
本工具不需要跟着更新任何绑定表（这正是旧 Workflow 绑定路径太重的地方）。

预设文件放 `runtime_dir/presets/<名字>.json`：它属于「我这台机器怎么调模型」，
不是工程数据，所以不进 project.db，跟着应用级设置走。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode

#: 入口标题 → 该往节点的哪个输入里填。按顺序取第一个命中的键，
#: 这样 LoadImage / CLIPTextEncode / 各家的原生节点都能覆盖，而不必认识它们的 class_type。
IMAGE_FIELDS = ("image", "filename", "url", "value")

#: 参考图槽位的上限。9 是「一眼能数清」的数目，也刚好覆盖多参考图模型的常见入参
#: （例如 ref_image_0..8）。图里有几个就用几个，不必凑满。
REF_SLOTS = 9
#: 参考图槽位的标题，按序号排好——`ref_slots()` 取的就是这个顺序。
REF_MARKERS: tuple[str, ...] = tuple(f"AIVS_REF_{i}" for i in range(1, REF_SLOTS + 1))

MARKERS: dict[str, tuple[str, ...]] = {
    "AIVS_FIRST_FRAME": IMAGE_FIELDS,
    "AIVS_LAST_FRAME": IMAGE_FIELDS,
    "AIVS_PROMPT": ("text", "prompt", "string", "value"),
    "AIVS_NEGATIVE": ("text", "prompt", "string", "value"),
    "AIVS_DURATION": ("length", "duration", "frames", "seconds", "num_frames", "value"),
    "AIVS_SEED": ("seed", "noise_seed", "value"),
    #: 参考图：角色表 / 地点参考图从这里进去。首帧只能是一张，参考图想喂几张标几个。
    **dict.fromkeys(REF_MARKERS, IMAGE_FIELDS),
}

#: 少了这两个就没法做 R2V；其余入口缺了只是「那一项用图里原来的值」。
#: 参考图槽位一个都没有也照样能生成——只是人物形象只能靠首帧带，容易跑偏。
REQUIRED = ("AIVS_FIRST_FRAME", "AIVS_PROMPT")

HOW_TO = [
    "在 ComfyUI 里右键入口节点 → Title，改成 AIVS_FIRST_FRAME / AIVS_PROMPT 等",
    "想让角色表 / 地点参考图一起喂进去：把接参考图的节点标题改成 AIVS_REF_1、AIVS_REF_2…"
    f"（最多 {REF_SLOTS} 个，有几个标几个）",
    "再用「Save (API Format)」导出，重新上传这份预设",
]

NAME_OK = re.compile(r"^[\w一-鿿][\w一-鿿 .-]{0,63}$")


def presets_dir() -> Path:
    path = settings.runtime_dir / "presets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path_of(name: str) -> Path:
    if not NAME_OK.match(name or ""):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "预设名不合法",
            f"{name!r} 含有不能作为文件名的字符。",
            ["用中英文、数字、空格、点、短横线", "例如 wan-i2v-快速"],
        )
    return presets_dir() / f"{name}.json"


def entry_points(graph: dict[str, Any]) -> dict[str, dict[str, str]]:
    """按标题找入口。返回 {标题: {node_id, field, class_type}}。"""
    found: dict[str, dict[str, str]] = {}
    for node_id, node in graph.items():
        title = str((node.get("_meta") or {}).get("title") or "").strip()
        candidates = MARKERS.get(title)
        if not candidates or title in found:
            continue
        inputs = node.get("inputs") or {}
        field = next((k for k in candidates if k in inputs), None)
        if field is None:
            raise AppError(
                ErrorCode.INVALID_WORKFLOW,
                f"入口节点 {title} 没有可填的输入",
                f"节点 {node_id}（{node.get('class_type')}）的输入是："
                f"{'、'.join(map(str, inputs)) or '（空）'}，"
                f"这里期望其中有 {'、'.join(candidates)} 之一。",
                [
                    "把标题挪到真正接收这个值的节点上（例如 LoadImage / CLIPTextEncode）",
                    *HOW_TO,
                ],
                {"node_id": node_id, "title": title},
            )
        found[title] = {
            "node_id": str(node_id),
            "field": field,
            "class_type": str(node.get("class_type") or ""),
        }
    return found


def ref_slots(points: dict[str, dict[str, str]]) -> list[str]:
    """这份图能收几张参考图——按 AIVS_REF_1、AIVS_REF_2… 的序号排好。

    刻意按声明顺序（`REF_MARKERS`）而不是字典顺序：账单里优先级最高的那张要进 1 号槽，
    「第几张是谁」才对得上（`base.ref_hint` 拼给模型的那句说明也是这个顺序）。
    中间空一号（只标了 1 和 3）也不算错，就是两个槽位——我们不去猜用户为什么跳号。
    """
    return [m for m in REF_MARKERS if m in points]


#: 文件路径 → (mtime_ns, 字节数, 槽位数)。「这份图能收几张」是一句会被反复问的话
#: （上下文账单、编排账单、界面上每一处都要问），一次解析一份几十万字节的图太贵。
#: key 里带上 mtime 与大小：文件一改缓存自然失效，所以这不是「可能过期的快照」，
#: 而是「同一份文件不重复解析」。
_slot_cache: dict[str, tuple[int, int, int]] = {}


def reset_cache() -> None:
    """清掉槽位数缓存。测试与 `registry.reset()` 用——预设目录会整体换掉。"""
    _slot_cache.clear()


def slot_count(name: str) -> int | None:
    """这份预设标了几个 `AIVS_REF_*` 槽位。数不出来时回 `None`（= 别拿它当上限）。

    数不出来有三种：没给名字、文件不在、文件坏了 / 缺必需入口。**一律不抛**——
    问这句话的地方全是只读路径（上下文账单、编排账单、界面），在那里因为预设坏了就
    500，人连「哪里坏了」都看不到；真正提交时 `submit()` 会拿同一份文件把话说清楚。
    """
    if not name:
        return None
    try:
        path = _path_of(name)
        stat = path.stat()
    except (AppError, OSError):
        return None
    key = path.as_posix()
    hit = _slot_cache.get(key)
    if hit is not None and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
        return hit[2]
    try:
        count = len(ref_slots(entry_points(load(name))))
    except AppError:
        return None
    _slot_cache[key] = (stat.st_mtime_ns, stat.st_size, count)
    return count


def inspect(graph: dict[str, Any]) -> dict[str, Any]:
    """预设的体检报告：找到哪些入口、缺哪些、缺了会怎样。"""
    points = entry_points(graph)
    missing = [m for m in REQUIRED if m not in points]
    slots = ref_slots(points)
    return {
        "node_count": len(graph),
        "entry_points": points,
        "found": sorted(points),
        "missing_required": missing,
        "ready": not missing,
        #: 能收几张参考图。0 不影响 ready——只是这份图喂不进角色表，UI 要提醒。
        "ref_slots": len(slots),
        "ref_hint": (
            f"能收 {len(slots)} 张参考图（{'、'.join(slots)}）"
            if slots
            else "没有参考图槽位：角色表 / 地点参考图喂不进去，人物形象只能靠首帧带。"
            f"要支持就在图里加 AIVS_REF_1…AIVS_REF_{REF_SLOTS} 标题"
        ),
        "impact": (
            None if not missing else f"缺少 {'、'.join(missing)}，这份预设无法用于 R2V 生成。"
        ),
    }


def save(name: str, raw: str) -> dict[str, Any]:
    """存一份预设。存之前先体检，绝不把一份填不进去的图悄悄留下。"""
    from app.services.workflows import parse_graph  # 延迟导入：避免生成层反向依赖 service 层

    graph = parse_graph(raw)
    report = inspect(graph)
    if not report["ready"]:
        raise AppError(
            ErrorCode.INVALID_WORKFLOW,
            "这份图里找不到必需的入口",
            str(report["impact"]),
            HOW_TO,
            {"found": report["found"], "missing": report["missing_required"]},
        )
    target = _path_of(name)
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise AppError(
            ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
            "预设写入失败",
            f"{target}: {type(exc).__name__}: {exc}",
            ["确认磁盘可写且空间充足"],
        ) from exc
    return {"name": name, "path": target.as_posix(), **report}


def load(name: str) -> dict[str, Any]:
    from app.services.workflows import parse_graph

    target = _path_of(name)
    if not target.is_file():
        raise AppError(
            ErrorCode.NOT_FOUND,
            "预设不存在",
            f"{target.name} 不在 {presets_dir().as_posix()} 里。",
            ["在设置页上传这份图的 API 格式 json", "或改选一个已有的预设"],
            {"name": name},
        )
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "预设读不出来",
            f"{target}: {type(exc).__name__}: {exc}",
            ["重新上传这份预设"],
        ) from exc
    return parse_graph(raw)


def listing() -> list[dict[str, Any]]:
    """设置页的预设列表。坏文件不隐藏——标成 ready=false 并写清原因。"""
    rows: list[dict[str, Any]] = []
    for path in sorted(presets_dir().glob("*.json")):
        # 坏文件也给全 UI 要用的键：形状不稳会让列表少画一块，而不是显示「这份图坏了」
        item: dict[str, Any] = {
            "name": path.stem,
            "path": path.as_posix(),
            "ready": False,
            "ref_slots": 0,
            "ref_hint": "",
        }
        try:
            item.update(inspect(load(path.stem)))
        except AppError as err:
            item["impact"] = f"{err.title}：{err.detail}"
        rows.append(item)
    return rows


def delete(name: str) -> None:
    _path_of(name).unlink(missing_ok=True)
