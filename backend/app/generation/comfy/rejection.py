"""把 ComfyUI `/prompt` 回的那一个 400 翻译成四要素错误。

**为什么值得单独一个文件**：这条路上的失败几乎全部长成同一个形状——ComfyUI 用一份
`node_errors` 精确说了「哪个节点的哪个输入、填的是什么、它那边有哪些候选」，而我们以前
把整段 JSON 截到 800 字就扔给用户看（`client.py` 里那行 `resp.text[:800]`）。于是最要紧的
那一截（候选列表）恰好被截在中间，建议里第一句还是「在流程页重新校验绑定」——那是绑定
那条路的话，走预设的人照着它一步都走不了。**硬约束 4 要的是说清，不是多说。**

这里只做翻译，一个字节都不出网：`parse()` 认 ComfyUI 的形状，`to_error()` 拼成 `AppError`，
原始响应体照旧完整地放进 `related_ids["raw"]`（界面上「展开原始报错」看的是它）。

两件刻意做的事：

  · **模型文件名要点出「最像的那几个」**。ComfyUI 的候选名是拿 `os.path.relpath` 拼的，
    Windows 上子目录分隔符是 `\\`；而图里存的那个值常常是从别处抄来的 `/`。这两个在
    ComfyUI 那边是不同的字符串，在用户眼里是同一个文件——不点出来，他会盯着两行长得
    几乎一样的名字找差别。
  · **点名到「这个值不是本工具填的」**。`unet_name` / `ckpt_name` / `lora_name` 这类值我们
    从来不改写（`providers/presets.py::MARKERS` 里根本没有它们，硬约束 1 的方向：
    本工具不维护模型端的图），所以建议必须把人送去 ComfyUI 里重选一次模型，
    而不是让他在本工具的设置页里翻。
"""

from __future__ import annotations

import ast
import difflib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import Any

from app.core.errors import AppError, ErrorCode

#: 原始响应体进 `related_ids["raw"]` 的上限。**这一份不是给人读的**（给人读的是 detail），
#: 而是「展开原始报错」里的存档，所以留得比 detail 长得多。
RAW_MAX = 4000
#: 最多点名几个「最像的候选」。列满一屏等于没点名。
NEAR_MAX = 3
#: 「像不像」的门槛（`difflib` 的 ratio，只比文件名那一段）。
NEAR_RATIO = 0.6
#: 最多逐个说几个出错的节点。一个模型没装常常连着报十几个节点，
#: 前几个说清比十几个各说半句有用；完整清单在原始报错里。
FAULT_MAX = 3

#: `required_input_missing` 那个 type 的字面量。摘节点那条路要按它认「ComfyUI 点名的这个
#: 必填输入是不是我们切掉的那一根」（`providers/comfy_base.py::_missing_cuts`），
#: 所以写成常量而不是散在两处的字符串。
MISSING_INPUT = "required_input_missing"
#: 值不在候选里。模型文件名对不上就是这一种。
VALUE_NOT_IN_LIST = "value_not_in_list"
#: 整份图连一个输出节点都没有。**摘节点之后有可能变成这一种**（唯一的保存节点挂在
#: 这一版用不上的那一支上），所以它要单独说一句。
NO_OUTPUTS = "prompt_no_outputs"
#: `related_ids` 里那一份结构化故障的键名。
FAULTS_KEY = "faults"

#: ComfyUI 的错误 type → 一句人话。**认不出来的照原样显示**：那是 ComfyUI 的词，
#: 我们编一句「未知错误」只会把它盖掉。
KIND_LABEL: dict[str, str] = {
    VALUE_NOT_IN_LIST: "填的值不在 ComfyUI 的候选里",
    MISSING_INPUT: "少了必填输入",
    "bad_linked_input": "连线接错了",
    "return_type_mismatch": "上游节点的输出类型接不进这个输入",
    "exception_during_validation": "ComfyUI 校验这个节点时自己抛了异常",
    "exception_during_inner_validation": "ComfyUI 校验这个节点的子图时自己抛了异常",
    "value_smaller_than_min": "值小于这个输入允许的最小值",
    "value_bigger_than_max": "值大于这个输入允许的最大值",
    "invalid_input_type": "值的类型不对",
    "custom_validation_failed": "这个节点自己的校验没通过",
    "prompt_no_outputs": "这份图里没有任何输出节点",
    "prompt_outputs_failed_validation": "输出节点全都没通过校验",
    "invalid_prompt": "ComfyUI 认为这份图本身不合法",
}

#: `details` 里那句 `unet_name: 'xxx' not in [...]` 的形状。`extra_info` 缺项时靠它抠出
#: 「哪个输入、填的什么」——各版本 ComfyUI 的 `extra_info` 不完全一样，而这两个词最要紧。
_DETAIL_RE = re.compile(r"^(?P<name>[^:]+):\s*'(?P<value>.*)'\s+not in\s", re.S)
#: 候选列表在 `details` 里那一段的引子。候选超过 20 个时 ComfyUI 会把 `input_config` 置空、
#: 并把这一段换成 `(list of length 231)`——那时一个候选都拿不到，只能如实说拿不到。
_NOT_IN = " not in "


@dataclass(frozen=True)
class Fault:
    """一个节点上的一处校验失败。除 `title` 之外全部照抄 ComfyUI 的说法。

    `title` 是**我们这份图里**那个节点的标题（`_meta.title`）：它往往就是 `AIVS_REF_1`
    这种入口名，比 id 和 class_type 都更能让用户认出「这是本工具填的那一格」。
    """

    node_id: str
    kind: str
    input: str = ""
    value: str = ""
    message: str = ""
    details: str = ""
    class_type: str = ""
    title: str = ""
    candidates: tuple[str, ...] = ()

    @property
    def where(self) -> str:
        """「节点 236（UNETLoader · AIVS_REF_1）」——id、节点类型、图里的标题三样都写。

        只给 id 的话，用户得在 ComfyUI 里把图打开一个个点；只给类型的话，同一种节点
        一份图里常常有七八个。
        """
        marks = " · ".join(part for part in (self.class_type, self.title) if part)
        return f"节点 {self.node_id}（{marks}）" if marks else f"节点 {self.node_id}"

    def as_dict(self) -> dict[str, Any]:
        """进 `related_ids[FAULTS_KEY]` 的那一份（JSON 里没有元组）。"""
        row = {f.name: getattr(self, f.name) for f in fields(self)}
        row["candidates"] = list(self.candidates)
        return row


def from_dict(row: dict[str, Any]) -> Fault:
    """`as_dict()` 的反向。多出来的键直接丢——这份东西会跟着任务参数存下来，
    以后加了字段的版本读回来时不该炸。"""
    names = {f.name for f in fields(Fault)}
    data: dict[str, Any] = {key: value for key, value in row.items() if key in names}
    data["node_id"] = str(data.get("node_id") or "")
    data["kind"] = str(data.get("kind") or "")
    raw = data.get("candidates") or ()
    data["candidates"] = tuple(str(x) for x in raw) if isinstance(raw, (list, tuple)) else ()
    return Fault(**data)


def faults_of(exc: AppError) -> list[Fault]:
    """把 `to_error()` 放进 `related_ids` 的那份结构读回来。

    摘节点那条路要问「ComfyUI 点名的那个必填输入，是不是我们切掉的那一根」
    （`providers/comfy_base.py::_missing_cuts`），而它手里只有一个 `AppError`。
    键名写在这一处，两边就不会各写一份字符串。
    """
    rows = (exc.related_ids or {}).get(FAULTS_KEY)
    if not isinstance(rows, list):
        return []
    return [from_dict(row) for row in rows if isinstance(row, dict)]


def _norm(name: str) -> str:
    """比模型名时统一掉的两件事：路径分隔符与大小写。

    ComfyUI 的候选名是 `os.path.relpath` 拼出来的，Windows 上分隔符是 `\\`；图里那个值
    常常是从别处抄来的 `/`。严格比对不认，用户眼里却是同一个文件。
    """
    return name.replace("\\", "/").strip().lower()


def _leaf(name: str) -> str:
    """只留文件名那一段——「同名但在另一个子目录里」是第二常见的一种对不上。"""
    return _norm(name).rsplit("/", 1)[-1]


def near_misses(value: str, candidates: Sequence[str]) -> tuple[str, list[str]]:
    """ComfyUI 的候选里最像「用户本来要的那个」的几个，外加一句「为什么像」。

    三档，**顺序就是可信度**：只差分隔符 / 大小写 → 同名但在别的子目录 → 只是长得像。
    最后一档必须说成「最像的是」而不能说「你要的是」：用户那个文件也可能根本没装，
    替他认定一个反而会让他去改一个本来没写错的名字。
    """
    if not value or not candidates:
        return "", []
    same = [c for c in candidates if _norm(c) == _norm(value)]
    if same:
        return "same", same[:NEAR_MAX]
    leaf = [c for c in candidates if _leaf(c) == _leaf(value)]
    if leaf:
        return "leaf", leaf[:NEAR_MAX]
    ranked = sorted(
        ((difflib.SequenceMatcher(None, _leaf(value), _leaf(c)).ratio(), c) for c in candidates),
        key=lambda pair: (-pair[0], pair[1]),
    )
    like = [c for ratio, c in ranked if ratio >= NEAR_RATIO][:NEAR_MAX]
    return ("like", like) if like else ("", [])


def _candidates(extra: dict[str, Any], details: str) -> tuple[str, ...]:
    """ComfyUI 那边的候选列表。两个来源都得认，一个都拿不到时如实回空。

      · `extra_info["input_config"]`：那个输入的定义，JSON 化之后是 `[[候选...], {选项}]`；
      · 候选超过 20 个时 ComfyUI 把 `input_config` 置成 null、`details` 里换成
        `(list of length 231)`——这一种确实一个候选都拿不到。

    `details` 里那句 `x: 'v' not in ['a', 'b']` 是 Python 的 repr，`ast.literal_eval` 读得
    回来（只读字面量，读不回来就当没有——绝不 `eval`）。
    """
    config = extra.get("input_config")
    if isinstance(config, (list, tuple)) and config:
        head = config[0]
        if isinstance(head, (list, tuple)):
            return tuple(str(x) for x in head)
        if all(isinstance(x, str) for x in config):
            return tuple(str(x) for x in config)
    at = details.find(_NOT_IN)
    tail = details[at + len(_NOT_IN) :].strip() if at >= 0 else ""
    if not tail.startswith("["):
        return ()
    try:
        parsed = ast.literal_eval(tail)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return ()
    return tuple(str(x) for x in parsed) if isinstance(parsed, (list, tuple)) else ()


def _from_details(kind: str, details: str) -> tuple[str, str]:
    """`extra_info` 缺项时，从 `details` 里抠出「哪个输入、填的什么」。"""
    text = details.strip()
    if not text:
        return "", ""
    if kind == MISSING_INPUT:
        #: 这一种的 `details` 就是那个输入名本身（老版本会带一句前缀）。
        return (text.split(":", 1)[-1].strip() if ":" in text else text), ""
    hit = _DETAIL_RE.match(text)
    if hit:
        return hit["name"].strip(), hit["value"]
    head, sep, rest = text.partition(":")
    return (head.strip(), rest.strip()) if sep else ("", "")


@dataclass(frozen=True)
class Rejection:
    """ComfyUI 拒绝这一次提交的完整说法（`parse()` 的产物，没有任何我们编的内容）。"""

    status: int
    kind: str = ""
    message: str = ""
    details: str = ""
    faults: tuple[Fault, ...] = ()
    body: str = ""
    parsed: bool = False

    @property
    def headline(self) -> str:
        """detail 的第一句。**认得出 type 就说它**，认不出只报 HTTP 码——不编原因。"""
        label = KIND_LABEL.get(self.kind, "")
        if self.kind and label:
            return f"ComfyUI 校验没通过（HTTP {self.status} · {self.kind}）：{label}。"
        if self.kind:
            return f"ComfyUI 校验没通过（HTTP {self.status} · {self.kind}）。"
        return f"ComfyUI 拒绝了这份图（HTTP {self.status}）。"


def _fault(
    node_id: str, entry: dict[str, Any], err: dict[str, Any], graph: dict[str, Any]
) -> Fault:
    """一条 `node_errors[id]["errors"][n]` → 一个 `Fault`。标题从我们这份图里取。"""
    kind = str(err.get("type") or "")
    details = str(err.get("details") or "")
    raw_extra = err.get("extra_info")
    extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
    name = str(extra.get("input_name") or "")
    received = extra.get("received_value")
    value = "" if received is None else str(received)
    if not name or not value:
        by_text = _from_details(kind, details)
        name = name or by_text[0]
        value = value or by_text[1]
    node = graph.get(node_id)
    node = node if isinstance(node, dict) else {}
    meta = node.get("_meta")
    meta = meta if isinstance(meta, dict) else {}
    return Fault(
        node_id=node_id,
        kind=kind,
        input=name,
        value=value,
        message=str(err.get("message") or ""),
        details=details,
        class_type=str(entry.get("class_type") or node.get("class_type") or ""),
        title=str(meta.get("title") or ""),
        candidates=_candidates(extra, details),
    )


def parse(status: int, body: str, graph: dict[str, Any] | None = None) -> Rejection:
    """认 ComfyUI 的形状。**读不出来不是异常**（`parsed=False`，`to_error` 照旧给原文）。

    `graph` 是我们**这一次真提交出去的那一份**（摘过节点之后的副本），只用来查节点标题；
    不传也能用，只是错误里少了 `AIVS_*` 那个名字。
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        return Rejection(status, body=body)
    raw_top = data.get("error")
    top: dict[str, Any] = raw_top if isinstance(raw_top, dict) else {}
    faults: list[Fault] = []
    node_errors = data.get("node_errors")
    if isinstance(node_errors, dict):
        for node_id, entry in node_errors.items():
            if not isinstance(entry, dict):
                continue
            rows = entry.get("errors")
            for err in rows if isinstance(rows, list) else []:
                if isinstance(err, dict):
                    faults.append(_fault(str(node_id), entry, err, graph or {}))
    return Rejection(
        status,
        kind=str(top.get("type") or ""),
        message=str(top.get("message") or ""),
        details=str(top.get("details") or ""),
        faults=tuple(faults),
        body=body,
        parsed=True,
    )


def _tail(fault: Fault) -> str:
    """`value_not_in_list` 那句话的后半截：为什么像、有几个候选。

    **一个候选都没拿到时要说出来**（ComfyUI 在候选超过 20 个时确实不给清单）——
    含糊成「没有这个模型」的话，用户会以为我们看过它那边的目录了。
    """
    why, near = near_misses(fault.value, fault.candidates)
    total = len(fault.candidates)
    if near and why == "same":
        return f"——ComfyUI 上那个叫「{near[0]}」，只差路径分隔符或大小写"
    if near and why == "leaf":
        return f"——同名的那个在「{near[0]}」，子目录不一样"
    if near:
        listed = "、".join(f"「{n}」" for n in near)
        return f"——它那边共 {total} 个候选，最像的是 {listed}"
    if total:
        return f"——它那边共 {total} 个候选，没有一个接近的（这个模型很可能还没装）"
    return "——候选太多，ComfyUI 这次没把清单给出来"


def _say(fault: Fault) -> str:
    """一个 `Fault` → 一句话。**哪个节点、哪个输入、填的什么**，三样缺一样都不算说清。"""
    if fault.kind == VALUE_NOT_IN_LIST:
        return (
            f"{fault.where}的 {fault.input} 填的是「{fault.value}」，"
            f"ComfyUI 上没有这个{_tail(fault)}。"
        )
    if fault.kind == MISSING_INPUT:
        return f"{fault.where}少了必填输入 {fault.input}。"
    label = KIND_LABEL.get(fault.kind) or fault.kind or "校验失败"
    where = fault.where + (f"的 {fault.input}" if fault.input else "")
    extra = fault.details or fault.message
    return f"{where}：{label}" + (f"（{extra}）。" if extra else "。")


def _suggest(rej: Rejection) -> list[str]:
    """建议**必须把人送到能改这件事的那一处**。这条路上那一处几乎总是 ComfyUI，不是本工具。"""
    if not rej.parsed:
        return [
            "确认这个地址真的是 ComfyUI（它这次回的不是 ComfyUI 的错误格式）",
            "在 ComfyUI 那侧的控制台里看这次提交留下的日志",
            "展开原始报错查看它回的原文",
        ]
    out: list[str] = []
    model = False
    for fault in rej.faults[:FAULT_MAX]:
        if fault.kind == VALUE_NOT_IN_LIST:
            model = True
            why, near = near_misses(fault.value, fault.candidates)
            if near:
                listed = "、".join(f"「{n}」" for n in near)
                lead = (
                    "只差路径分隔符 / 大小写，ComfyUI 上那个叫"
                    if why == "same"
                    else "ComfyUI 上最像的是"
                )
                out.append(
                    f"节点 {fault.node_id} 的 {fault.input}：{lead} {listed}"
                    "——在 ComfyUI 里把这个节点的模型重选一次，名字就会写成它认得的那一个"
                )
            else:
                out.append(
                    f"节点 {fault.node_id} 的 {fault.input}：在 ComfyUI 里把这个节点的模型"
                    "重选一次（它那边的候选里没有现在这个名字）"
                )
        elif fault.kind == MISSING_INPUT:
            out.append(
                f"节点 {fault.node_id} 的 {fault.input} 是必填的：照这个名字去图里找那个节点，"
                "看它本来该由谁接上"
            )
    if model:
        out.append(
            "模型 / LoRA / 采样器这类值是图里存着的，本工具从不改写它（只填 AIVS_* 那几个入口）"
            "——在 ComfyUI 里改好之后用「Save (API Format)」重新导出这份图再上传"
        )
        out.append("确认那个文件真的在 ComfyUI 的 models/ 对应子目录里（刚放进去的要刷新一次）")
    if rej.kind == NO_OUTPUTS:
        out.append(
            "这份图里没有任何输出节点（SaveImage / VHS_VideoCombine 之类）——本次任务的说明里"
            "如果写了「摘掉了几个节点」，那就是唯一的输出挂在这一版用不上的那一支上"
        )
    out.append("展开原始报错可以看到 ComfyUI 侧的完整信息（候选清单也在里面）")
    return out


def to_error(status: int, body: str, graph: dict[str, Any] | None = None) -> AppError:
    """ComfyUI 拒绝提交时那个 `WORKFLOW_ERROR`。**全后端只有这一处拼这句话。**

    标题照旧是「ComfyUI 拒绝了本次任务」（界面上、日志里都按它认这一类失败）；
    变的是 detail 与 suggestions，以及多出来的 `related_ids`：

      · `raw`：原始响应体（截到 `RAW_MAX`），「展开原始报错」看的是它；
      · `kind`：ComfyUI 的顶层 type，日志按它分类；
      · `faults`：结构化的每一处失败。摘节点那条路要拿它判「必填的那一根是不是我们切的」
        （`providers/comfy_base.py`），所以**不能只留一段拼好的文字**。
    """
    rej = parse(status, body, graph)
    if not rej.parsed:
        #: 认不出来的响应体照旧给原文——编一句解释比截断更糟。
        detail = f"HTTP {status}: {body[:800]}"
    else:
        lines = [rej.headline, *[_say(fault) for fault in rej.faults[:FAULT_MAX]]]
        rest = len(rej.faults) - FAULT_MAX
        if rest > 0:
            lines.append(f"另外还有 {rest} 处也没通过校验（完整清单在原始报错里）。")
        if not rej.faults and rej.details:
            lines.append(rej.details[:400])
        detail = " ".join(line for line in lines if line)
    return AppError(
        ErrorCode.WORKFLOW_ERROR,
        "ComfyUI 拒绝了本次任务",
        detail,
        _suggest(rej),
        {
            "raw": body[:RAW_MAX],
            "kind": rej.kind,
            FAULTS_KEY: [fault.as_dict() for fault in rej.faults],
        },
    )
