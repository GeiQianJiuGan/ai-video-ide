"""ComfyUI api graph 的纯函数：解析一份 `workflow_api.json`、把参数值写进它的副本、
把这次用不上的入口从副本里摘掉，以及**把摘的时候切断的那一根必填输入接回去**。

**为什么在这一层**：`app/generation/providers/*` 只能 import `app.core.*` 与 `app.generation.*`
（provider 层绝不 import service 层，`presets.py` / `image.py` 都是这个规矩）。工作流绑定那条路
的适配器（`providers/comfy_workflow.py`）要用 `parse_graph` / `apply_bindings`，`detach()` 还要
再给预设那条路（`providers/comfy_preset.py`）用一份，所以它们不能留在 `services/workflows.py`
里——那边只是重新导出前三个名字，老调用点一行不用改。

这里**只做形状、写值与摘节点**，不认识任何具体模型：绑定表长什么样、哪些槽位是必需的，
仍然是 `services/workflows.py` 与 `persistence/models_gen.py` 的事。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import AppError, ErrorCode

#: 可绑定的输入槽。前四个是素材/文本，后面是采样参数。
SLOTS = (
    "prompt",
    "negative_prompt",
    "reference_image",
    "first_frame",
    "last_frame",
    "source_image",
    "seed",
    "steps",
    "width",
    "height",
    "duration",
)

#: `SLOTS` 里**接文件**的那几个。与标量槽位的区别不是数据类型，而是**这次没给值时该怎么办**
#: （见 `detach()`）：标量保持图里原来的值，媒体要把那个节点摘掉。
#: 预设那条路上同一件事的表是 `providers/presets.py::MEDIA_MARKERS`。
MEDIA_SLOTS = frozenset({"reference_image", "first_frame", "last_frame", "source_image"})


@dataclass(frozen=True)
class Detached:
    """`detach()` 这一次做了什么：摘掉了哪些节点、在留下来的节点上切断了哪些输入。

    以前这里只回「摘掉了谁」，于是**「摘的时候切断了哪一根线」出了这个函数就没人知道**——
    而它恰好是这条路上唯一会咬人的情形：被切的那一格在图里是**必填**的
    （`ImageBatch.image1`），ComfyUI 直接 400，于是「参考图不够就跳过多余的槽位」反过来
    变成了「跳过之后根本提交不出去」。`cuts` 就是为了让 `providers/comfy_base.py` 能在被拒
    之后精确认出「ComfyUI 点名的那个必填输入正是我们切的那一根」，接回去再提交一次。

    `cuts` 里每一项：`node_id` / `field` 是**留下来那个节点**上被切掉的那一格，
    `was_node` / `was_index` / `was_class` / `was_title` 是原来接在那儿的（已被摘掉的）节点。
    **已经跟着摘掉的节点上那些切口不算**（它自己都不在图里了，接回去毫无意义）。
    """

    nodes: list[dict[str, str]] = field(default_factory=list)
    cuts: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        """真摘过东西才为真——调用方到处在问「这次摘过没有」。"""
        return bool(self.nodes)

    def __len__(self) -> int:
        """摘掉的节点个数（老调用点写的是 `len(removed)`，语义一个字没变）。"""
        return len(self.nodes)


def parse_graph(raw: str) -> dict[str, Any]:
    """解析 workflow_api.json。必须是 {节点id: {class_type, inputs}} 形状。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(
            ErrorCode.INVALID_WORKFLOW,
            "不是合法的 JSON",
            f"第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}",
            [
                "确认导出的是 ComfyUI 的「API 格式」而不是界面工作流",
                "用文本编辑器确认文件没有被截断",
            ],
        ) from exc
    if not isinstance(data, dict) or not data:
        raise AppError(
            ErrorCode.INVALID_WORKFLOW,
            "不是 ComfyUI 的 API 格式",
            "顶层应是「节点 id → 节点」的对象，实际拿到的是空对象或数组。",
            ["在 ComfyUI 里用「Save (API Format)」重新导出", "确认没有把界面工作流当成 API 格式"],
        )
    # 界面工作流的顶层是 {id, revision, last_node_id, last_link_id, nodes: [...], links: [...]}。
    # 它和 API 格式差得远，但下面那条「缺少 class_type」的报错会把这几个顶层键当成节点名列出来
    # ——用户看到的是一串不认识的字段，而真正的原因是导出格式选错了。所以先认出这一种。
    if isinstance(data.get("nodes"), list):
        raise AppError(
            ErrorCode.INVALID_WORKFLOW,
            "这是界面工作流，不是 API 格式",
            f"顶层键是 {'、'.join(list(data)[:6])}，{len(data['nodes'])} 个节点在 nodes 数组里。"
            "API 格式的顶层直接是「节点 id → {class_type, inputs}」，没有 nodes / links。",
            [
                "在 ComfyUI 里打开这份工作流 → 菜单「工作流 / Workflow」→「导出 (API)」",
                "旧版前端：设置里打开「Enable Dev mode Options」后会出现「Save (API Format)」按钮",
                "user/default/workflows/ 里存的都是界面格式，不能直接上传",
                "节点标题（AIVS_*）不用重设，导出格式换对就行",
            ],
            {"node_count": len(data["nodes"])},
        )
    bad = [k for k, v in data.items() if not isinstance(v, dict) or "class_type" not in v]
    if bad:
        raise AppError(
            ErrorCode.INVALID_WORKFLOW,
            "节点结构不完整",
            f"以下节点缺少 class_type：{'、'.join(bad[:5])}。",
            ["重新用「Save (API Format)」导出", "确认文件没有被手工改坏"],
            {"nodes": bad[:20]},
        )
    return data


def apply_bindings(
    graph: dict[str, Any], bindings: dict[str, str], values: dict[str, Any]
) -> dict[str, Any]:
    """把参数值写进 api graph 的副本。Adapter 的核心，也是唯一知道节点细节的地方。"""
    out = json.loads(json.dumps(graph))
    for slot, target in bindings.items():
        if slot not in values or values[slot] is None:
            continue
        node_id, field = target.split(".", 1)
        node = out.get(node_id)
        if isinstance(node, dict):
            node.setdefault("inputs", {})[field] = values[slot]
    return out


def _linked(value: Any) -> str | None:
    """这个输入是不是「连到另一个节点的输出」？是就回那个节点 id。

    api 格式里一条连线就写成 `[节点id, 输出序号]`。这是「摘节点」**唯一**需要认识的图结构
    ——class_type、lora、加速节点、采样器一个都不用认（与硬约束 1 同一条精神：
    本工具不维护模型端的图）。
    """
    if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
        return str(value[0])
    return None


def detach(
    graph: dict[str, Any],
    node_ids: Iterable[str],
    *,
    keep: Iterable[str] = (),
) -> Detached:
    """把这些节点从提交的那份图里摘掉，连带只为它们服务的中间节点。**就地改**，回摘掉了谁。

    **为什么必须摘而不是「留着不填」**：标了 `AIVS_*` 标题却这一次没有值的媒体入口，图里那一格
    存的是用户在 ComfyUI 里存图时挂着的示例文件。不填就等于把那张不相干的图/那段不相干的音频
    真送进模型——用户看到的是「画面莫名其妙往那张示例图上收敛」，而队列里一条错误都没有。
    于是「多标几个入口」在事实上变成了一种风险，用户不敢在图里多摆节点，这个便利就废了。
    摘掉之后语义才对得上：**标了标题 = 「这一格由本工具填」，本工具这次没填 = 「这一格这次不用」。**

    标量入口（seed / steps / 宽高 / 时长）**不走这里**：那一格留着的是图里原本的采样参数，
    是用户有意存进去的默认值，保持原值正是想要的行为。两类的分界表是 `MEDIA_SLOTS`
    与 `providers/presets.py::MEDIA_MARKERS`。

    摘的办法只用局部信息，不问 ComfyUI 的 `/object_info`，也不认识任何 class_type：

      1. 把图里所有指向它的连线（`inputs` 里那个 `[node_id, k]`）连键一起删掉；
      2. 删掉这个节点本身——ComfyUI 是从输出节点往回走的，剩下的孤立节点既不会被校验
         也不会被执行，但留着会让「提交了什么」这份存档看不出这次到底跑了哪张图；
      3. **往下游传一层**：切完之后一个连线型输入都不剩的下游节点，说明它是专为这条链服务的
         中间件（LoadImage → ImageScale → 主节点 里的 ImageScale），跟着摘；仍然连着别的东西的
         是汇合点（WanImageToVideo 丢了 `end_image` 还连着 positive / negative / vae /
         start_image），到此为止——**只摘只为它服务的，不动共用的**。

    `keep` 是这一次真填了值的那些入口节点：它们永远不会被连带摘掉（第 3 步在它们那儿停），
    最坏情况下也只是少一个输入键，而不是把这次真正要跑的那条链摘断。

    回的 `Detached.nodes` 每一项是 `{node_id, class_type, title}`——调用方把它写进 `req.notes`，
    一路冻进版本参数 `ref_notes` 并显示在界面上：**摘掉一个节点是降级，绝不静默**（硬约束 4）。
    `Detached.cuts` 是第 1 步切掉的那些线里「下游还留在图里」的那些（理由见 `Detached`）。
    """
    protected = {str(n) for n in keep}
    pending = [str(n) for n in node_ids if str(n) not in protected]
    removed: list[dict[str, str]] = []
    cuts: list[dict[str, Any]] = []
    while pending:
        node_id = pending.pop(0)
        node = graph.pop(node_id, None)
        if not isinstance(node, dict):
            continue
        meta = node.get("_meta")
        gone = {
            "node_id": node_id,
            "class_type": str(node.get("class_type") or ""),
            "title": str((meta or {}).get("title") or "") if isinstance(meta, dict) else "",
        }
        removed.append(gone)
        for other_id, other in graph.items():
            inputs = other.get("inputs")
            if not isinstance(inputs, dict):
                continue
            cut = [key for key, value in inputs.items() if _linked(value) == node_id]
            if not cut:
                continue
            for name in cut:
                #: 先把「原来接的是哪个节点的第几个输出」记下来再删——删完就再也拿不回来了，
                #: 而接回去（`reconnect`）要的正是这个输出序号。
                link = inputs.get(name)
                cuts.append(
                    {
                        "node_id": other_id,
                        "field": name,
                        "was_node": node_id,
                        "was_index": int(link[1]) if isinstance(link, list) else 0,
                        "was_class": gone["class_type"],
                        "was_title": gone["title"],
                    }
                )
                inputs.pop(name, None)
            if other_id in protected:
                continue
            if not any(_linked(value) is not None for value in inputs.values()):
                pending.append(other_id)
    #: 下游自己也被摘掉了的那些切口留着没用（那个节点已经不在图里）。
    return Detached(removed, [cut for cut in cuts if cut["node_id"] in graph])


def reconnect(graph: dict[str, Any], cut: dict[str, Any], candidates: Iterable[str]) -> str | None:
    """把 `detach()` 切掉的那一根接到这一次真在用的同类节点上。**就地改**，接上了回那个节点 id。

    **只在一种情形下用**：ComfyUI 说被切掉的那一格是必填的（`ImageBatch.image1`），于是
    「参考图不够就跳过多余的槽位」反而让任务提交不出去。这时把那一格接到本次真填了素材的
    那个入口上——同一份素材喂两遍，而不是把图里那张示例图喂进去，更不是整个任务失败。

    **类型判断只有一条：`class_type` 字符串完全相等。** 摘节点那一套刻意不认识任何 class_type
    （也不打 `/object_info`），这里同样不认识：被摘的是 `LoadImage`，就只接到另一个 `LoadImage`
    上。名字不一样的一律不接（回 `None`，由调用方如实报错）——把 `VAEEncode.pixels` 接到一个
    VAE 上只会换来一条更难懂的错误。
    """
    field_name = str(cut.get("field") or "")
    holder = str(cut.get("node_id") or "")
    want = str(cut.get("was_class") or "")
    target = graph.get(holder)
    if not field_name or not want or not isinstance(target, dict):
        return None
    index = cut.get("was_index")
    for candidate in candidates:
        node_id = str(candidate)
        node = graph.get(node_id)
        if node_id == holder or not isinstance(node, dict):
            continue
        if str(node.get("class_type") or "") != want:
            continue
        target.setdefault("inputs", {})[field_name] = [
            node_id,
            index if isinstance(index, int) else 0,
        ]
        return node_id
    return None


#: 「image1 / image2 / …」这种带序号的输入名。见 `_inflows()`——只当**保险**用。
_NUMBERED = re.compile(r"^(.*?)(\d+)$")


@dataclass(frozen=True)
class FeedOrder:
    """这份图**实际**按什么顺序把素材喂给模型（`feed_order()` 的答案）。

    `order` 是入口名（`AIVS_REF_2` / `first_frame` / `__ref_0` 这类，由调用方定义）按真实
    喂入顺序排好的一串；`groups` 是它是怎么排出来的——一组 = 在图里汇合到同一个节点、
    因此顺序是**实测**的那几个入口，组与组之间没有连线证据，只能按调用方给的约定顺序排。

    `source` 三个取值，**必须一路传到界面上**（硬约束 4：说清这个数字是怎么来的）：

      · `graph`  = 全部入口汇合到同一处，整个顺序都是实测的；
      · `mixed`  = 有实测的组，也有孤立的入口，跨组顺序按约定；
      · `title`  = 一个汇合点都没有，整串顺序就是调用方给的约定顺序（等于没测出来）。
    """

    order: list[str] = field(default_factory=list)
    source: str = "title"
    groups: list[list[str]] = field(default_factory=list)

    @property
    def traced(self) -> bool:
        """真从接线里测出过东西（哪怕只有一组）。"""
        return self.source != "title"

    def moved(self, given: Sequence[str]) -> bool:
        """实测顺序与约定顺序不一致——**这正是要报给用户的那件事**。"""
        return list(self.order) != [key for key in given if key in set(self.order)]


def _inflows(inputs: dict[str, Any]) -> list[str]:
    """这个节点的连线型输入，**按它们真正的声明顺序**。

    api 格式里 `inputs` 是一个 JSON 对象，而 ComfyUI 导出时的键顺序就是节点上那几个输入
    从上到下的顺序（`json.loads` 保序，`apply_bindings` 的深拷贝也保序）——`ImageBatch` 的
    `image1` 排在 `image2` 前面、`WanImageToVideo` 的 `start_image` 排在 `end_image` 前面，
    这就是「第几张图」的事实来源。

    带序号的输入名（`image1` / `image2` / …，或 `ref_images.ref_image_0` / …）**额外按数字排一遍**当保险：
    有些工具会把 JSON 的键按字典序重排一次，那样 `image10` 会跑到 `image2` 前面。
    按公共前缀对编号槽位独立按自然数大小重排，保证即使同节点包含 prompt、clip 等未带数字的输入，
    编号槽位（如 image_0..7）也能精准自然排序。前缀不一致或压根没有序号时照旧用键顺序，绝不自己猜。
    """
    fields = [name for name, value in inputs.items() if _linked(value) is not None]
    if len(fields) < 2:
        return fields

    prefix_groups: dict[str, list[tuple[int, str]]] = {}
    for name in fields:
        m = _NUMBERED.match(name)
        if m:
            prefix_groups.setdefault(m.group(1), []).append((int(m.group(2)), name))

    sorted_replacements: dict[str, list[str]] = {}
    for prefix, group in prefix_groups.items():
        if len(group) >= 2:
            sorted_replacements[prefix] = [name for _, name in sorted(group, key=lambda x: x[0])]

    if not sorted_replacements:
        return fields

    out: list[str] = []
    prefix_iters = {prefix: iter(names) for prefix, names in sorted_replacements.items()}
    for name in fields:
        m = _NUMBERED.match(name)
        if m and m.group(1) in prefix_iters:
            out.append(next(prefix_iters[m.group(1)]))
        else:
            out.append(name)
    return out


def feed_order(graph: dict[str, Any], entries: Mapping[str, str]) -> FeedOrder:
    """顺着接线数出「这份图实际先喂哪一张、后喂哪一张」。**图册编号的唯一事实来源。**

    **为什么不能按标题序号数**：`AIVS_REF_1` / `AIVS_REF_2` 只说明「这一格由本工具填」，
    没有任何东西保证 1 号那个 `LoadImage` 真的接在合批节点的 `image1` 上——在 ComfyUI 里把
    两根线互换一下就反了，而这不是用户操作失误，图是他自己维护的（硬约束 1：本工具不维护
    模型端的图）。于是喂给模型的那句「`<Picture 1>` 是阿岚」会指错人，画面里两个角色互相
    串味，而队列里一条错误都没有——正是最难查的那一类。首尾帧更明显：它们和参考图挤在
    同一串编号里，谁是 `<Picture 1>` 完全取决于那份图怎么接的。

    **数法只用局部信息**（与 `detach()` 同一条精神：不认识任何 class_type、不打
    `/object_info`、不 import 服务层）：

      1. 每个节点收到的那一串 = 按 `_inflows()` 的顺序把它每个上游收到的串接起来，
         自己是入口的话把自己也算上（去重，先到的算数）。`ImageBatch(image1=REF_2,
         image2=REF_1)` 收到的就是 `[REF_2, REF_1]`——**实测，与标题序号无关**；
      2. 图里所有节点各算一串，**收得最全的那一串**就是这几个入口的总顺序
         （最下游那个节点天然收得最全）；
      3. 没能汇合进同一串的入口各自成一组，组间顺序退回调用方给的约定顺序，
         并在 `source` 里说清这一次只测到了一部分。

    `entries` 是「入口名 → 节点 id」，**顺序即调用方的约定顺序**（预设那条路是标题序号，
    绑定那条路是绑定表的行号）。只认还在图里的那些——`detach()` 之后调用它正是为了让
    这一次真没喂的槽位不占编号，所以图里已经没有的节点直接忽略。

    这里**只回顺序，不回一句人话**：措辞要用「参考图2」这种给人看的名字，那是
    `providers/base.py::PictureBook` 的事，两处各写一遍必然分叉。
    """
    live = {key: str(node_id) for key, node_id in entries.items() if str(node_id) in graph}
    given = list(live)
    if len(given) < 2:
        #: 0 个或 1 个入口时不存在「谁在前」这个问题，也没有可分叉的余地。
        return FeedOrder(list(given), "graph", [[key] for key in given])

    owner: dict[str, list[str]] = {}
    for key, node_id in live.items():
        #: 一个节点上挂两个入口名是常态（绑定那条路的「参考图（单槽）」与 `__ref_0`
        #: 常常指着同一个 LoadImage），所以是一对多。
        owner.setdefault(node_id, []).append(key)

    cache: dict[str, tuple[str, ...]] = {}
    walking: set[str] = set()

    def upstream(node_id: str) -> tuple[str, ...]:
        got = cache.get(node_id)
        if got is not None:
            return got
        if node_id in walking:
            #: 正常的 api graph 是 DAG；真有环就到此为止，不进死循环（宁可少测出一段顺序）。
            return ()
        walking.add(node_id)
        seen: list[str] = []
        node = graph.get(node_id)
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if isinstance(inputs, dict):
            for name in _inflows(inputs):
                parent = _linked(inputs.get(name))
                if parent is None:
                    continue
                for key in upstream(parent):
                    if key not in seen:
                        seen.append(key)
        for key in owner.get(node_id, ()):
            if key not in seen:
                seen.append(key)
        walking.discard(node_id)
        cache[node_id] = tuple(seen)
        return cache[node_id]

    rank = {key: index for index, key in enumerate(given)}
    chains = sorted(
        {upstream(node_id) for node_id in list(graph)},
        key=lambda got: (-len(got), min((rank[key] for key in got), default=0)),
    )
    groups: list[list[str]] = []
    placed: set[str] = set()
    for chain in chains:
        #: 收得最全的先占位。与已占位的那组交叉（同一个入口喂给两个不同的汇合点）时整串跳过
        #: ——把同一个入口排进两组会让编号出现重复，那比少测出一段顺序糟得多。
        if len(chain) < 2 or any(key in placed for key in chain):
            continue
        groups.append(list(chain))
        placed.update(chain)
    groups += [[key] for key in given if key not in placed]
    groups.sort(key=lambda group: min(rank[key] for key in group))
    if len(groups) == 1:
        source = "graph"
    elif all(len(group) == 1 for group in groups):
        source = "title"
    else:
        source = "mixed"
    return FeedOrder([key for group in groups for key in group], source, groups)
