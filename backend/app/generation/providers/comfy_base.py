"""两个 ComfyUI 适配器（预设 / 工作流绑定）共用的那一半：上传、提交、轮询、取回产物。

分出来的理由很实在：这些动作与「图是谁维护的」无关——都是「把本地文件塞进 ComfyUI 的
input 目录」「读 history 判断跑完没有」「把最后一个产物下载回来」。各写一份的话，
「跑完了但没有任何产物」这句四要素错误就会有两份，而它恰好是最需要口径一致的一句
（用户看到它时要去检查图的末端有没有保存节点）。

**提交也在这里**（`_submit_graph`），因为「摘掉这一版用不上的媒体入口」之后那件会咬人的事
两条路一模一样：被切掉的那一格在图里是**必填**的（`ImageBatch.image1`），ComfyUI 直接 400，
于是「参考图不够就跳过多余的槽位」反过来成了「跳过之后根本提交不出去」。这一层认出那种
拒绝、把那一根接到本次真在喂的同类入口上、再提交一次，并把这件事写进 `notes`。

**「把正向提示词换成六段图册格式」那一步的取舍也在这里**（`_retold`）：两条 ComfyUI 路
收到的都只是「一串图片 + 一段文字」，所以「第 3 张是张秀才」这件事只能写进提示词，
而什么时候该写、什么时候原样发送（关掉了参考素材说明 / 这一镜只有首尾帧要点名）
两边必须是同一份答案。各写一遍必然分叉，而分叉的样子恰好是最难查的那一类：
一条路上编号对得上、另一条路上两个角色互相串味，界面上却都写着「已生成」。

真正分岔的只有各自的 `submit()`：预设那条按 `AIVS_*` 标题填，绑定那条按绑定表填。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.generation import renderers
from app.generation.comfy import rejection
from app.generation.comfy.client import ComfyClient, comfy, outputs_of
from app.generation.comfy.graph import Detached, reconnect
from app.generation.providers import base
from app.generation.providers.base import TaskState, VideoRequest

log = get_logger("provider.comfy")


class ComfyTasks:
    """ComfyUI 那半条链。子类必须自己实现 `submit()` 与 `ref_capacity()`。"""

    #: 子类覆盖，用于日志与报错文案。
    name = "comfy"

    def __init__(self, client: ComfyClient | None = None) -> None:
        self._client = client or comfy
        #: task_id → 提交时用的那份图（预设名 / 工作流名），只为报错时能说清「哪一份图没出片」。
        self._used: dict[str, str] = {}

    async def _upload(self, path: Path) -> str:
        if not path.is_file():  # noqa: ASYNC240 - 本地文件检查，开销可忽略
            raise AppError(
                ErrorCode.MISSING_ASSET,
                "参考素材不在磁盘上",
                f"{path} 找不到。",
                ["确认该资产文件还在工程目录里", "或重新挑一个参考素材"],
                {"path": path.as_posix()},
            )
        # 参考素材是本地文件，读它不值得再包一层线程（大段视频也就是一次同步读）
        return await self._client.upload_input(path.name, path.read_bytes(), subfolder="")  # noqa: ASYNC240

    def _retold(
        self,
        req: VideoRequest,
        book: base.PictureBook,
        source: str,
        write: Callable[[str], bool],
    ) -> None:
        """把正向 prompt 换成六段图册格式，并记下这一次到底喂了哪段话、哪几张图。

        ComfyUI 那类图收不到结构化字段，只收得到一串图片加一段文字，所以「第 3 张是张秀才」
        这件事只能写进 prompt（渲染分岔只有 `renderers.render_prompt` 一处，它按 `req.skill`
        选渲染器——默认那份 `minimax-h3` 主体仍在 `base.render_video_prompt`）。老路是在四段
        格式末尾附一句「参考图1=宋焘」，模型读到的仍是一段散文，`<Picture n>` 与画面里的人没有
        任何显式绑定——「张秀才长成了宋焘」就是那个形状。

        **两条 ComfyUI 路共用这一份**（预设按标题填、绑定按绑定表填，但收到提示词的都只是
        图里某个节点的某个字段），所以「什么时候不重排」这三条判断只有一处口径：

          · `video.ref_labels` 关掉时**原样发送**：那是用户明确说过「别动我的提示词」。此时
            图册照旧记进版本参数（界面要能说清那几张图是按什么顺序喂进去的），只是模型收不到
            编号，这是一次降级，所以要说出来（硬约束 4）；
          · **一张要点名的素材都没有时也原样发送**：只有首 / 末帧的镜头（转场、单线程续接
            全是这一种）在模型端本来就用两个专门的入口表达「画面的头和尾」，prompt 里没有
            任何 `<Picture n>` 需要对号。为它们凭空多出一段 `subject_definitions: none`
            只是噪声——这次改造要修的是「两个角色画成同一个人」，不是把所有老镜头的提示词
            都换一种写法；
          · **写不进去时不假装重排过**：`write` 回 False（那份图上根本没有正向提示词入口）
            时图册照记，`sent_prompt` 保持原样。

        `write` 由各条路自己给：它知道自己那份图的正向 prompt 落在哪个节点的哪个字段
        （预设是 `AIVS_PROMPT` 那个入口，绑定是绑定表里 `prompt` 那一行），
        写进去了回 True。这一层不认识任何入口约定。
        """
        req.book = book
        req.sent_prompt = req.prompt
        if not book or not book.subjects:
            return
        if not settings.video_ref_labels:
            req.notes.append(
                "设置里关掉了「参考素材说明」，这一版按原样发送提示词——"
                f"模型收不到「第几张图是谁」（这一次的图册：{book.listing}）。"
            )
            return
        sent = renderers.render_prompt(req.skill, req, book)
        if not write(sent):
            log.info("provider.prompt_entry_missing", source=source)
            return
        req.sent_prompt = sent
        req.notes += book.notes
        req.notes.append(f"提示词已按参考生成格式重排，这一次的图册：{book.listing}。")
        log.info(
            "provider.prompt_retold",
            source=source,
            pictures=len(book.items),
            subjects=len(book.subjects),
            order=book.order_source,
        )

    async def _submit_graph(
        self,
        graph: dict[str, Any],
        *,
        client_id: str,
        source: str,
        detached: Detached,
        refill: Sequence[str],
        notes: list[str],
    ) -> str:
        """提交这份图。**摘节点切断的那一根必填输入自己接回去，再提交一次。**

        用户那句需求原文是「多参数的工作流，图片不足就跳过多余的参数图，可是跳过后目前是
        400」。跳过（`comfy/graph.py::detach`）本身是对的——图里那一格存的是存图时挂着的示例
        文件，留着等于把一张不相干的图真喂进模型。会 400 的是**幸存的那个合批节点**：
        `ImageBatch(image1, image2)` 的 `image1` 是必填的，我们把它那一根线切了，ComfyUI 于是
        回 `required_input_missing`。

        三条取舍：

          · **只在 ComfyUI 点名到我们切的那一格时才动手**（`_missing_cuts` 两头都比：
            节点 id + 输入名）。别的失败一律原样往上抛——模型没装、地址不对那类错误加两句
            「我摘过节点」只会把真正的原因埋掉。
          · **接到本次真在喂的同类入口上**，也就是把同一份素材喂两遍。剩下的选择只有
            「把示例图喂进去」（等于没摘）和「整个任务失败」（用户什么都拿不到），
            重复喂一张这个镜头本来就要用的素材是三者里唯一诚实又能出片的。
          · **只重试一次**，且接回去这件事必须写进 `notes` → 冻结成版本参数 `ref_notes`
            （硬约束 4：降级绝不静默）。接不上（图里没有第二个同类节点）就报
            `cut_submit_error`，它点名到 `节点.输入` 并给两条真能走通的出路。

        `notes` 收的是 `VideoRequest.notes` 那个列表本身——传 `req` 进来的话，出图 / 配音
        那两条路（它们没有 `VideoRequest`）就用不上这段了。
        """
        try:
            return await self._client.submit(graph, client_id=client_id)
        except AppError as exc:
            cuts = _missing_cuts(exc, detached.cuts)
            if not cuts:
                raise detached_submit_error(exc, source, detached) from exc
            fixed: list[dict[str, Any]] = []
            for cut in cuts:
                target = reconnect(graph, cut, refill)
                if target is None:
                    continue
                fixed.append(cut)
                origin = str(cut.get("was_title") or cut.get("was_class") or "?")
                notes.append(
                    f"{source} 里节点 {cut['node_id']} 的 {cut['field']} 是必填的，"
                    f"而这一版没有素材可填（那一格原来接的是 {origin}）"
                    f"——已把它接到本次真在喂的那个入口（节点 {target}），"
                    "也就是同一份素材喂了两遍；图里挂着的示例文件仍然一个都没有送进 ComfyUI。"
                )
            if not fixed:
                raise cut_submit_error(exc, source, cuts) from exc
            log.info(
                "provider.cuts_reconnected",
                source=source,
                fixed=len(fixed),
                asked=len(cuts),
            )
            try:
                return await self._client.submit(graph, client_id=client_id)
            except AppError as again:
                raise cut_submit_error(again, source, cuts) from again

    async def poll(self, task_id: str) -> TaskState:
        history = await self._client.history(task_id)
        if not history:
            return TaskState("running", 0.0, "ComfyUI 正在跑")
        status = ((history.get("status") or {}) if isinstance(history, dict) else {}) or {}
        if str(status.get("status_str") or "") == "error":
            return TaskState("failed", 1.0, _error_detail(status), raw=history)
        if not outputs_of(history):
            if str(status.get("status_str") or "").lower() not in {
                "success",
                "completed",
                "complete",
            }:
                return TaskState("running", 0.0, "ComfyUI 正在跑", raw=history)
            return TaskState(
                "failed",
                1.0,
                "跑完了但没有任何产物——图的末端可能没有保存节点。",
                raw=history,
            )
        return TaskState("done", 1.0, "已出片", raw=history)

    async def fetch(self, task_id: str) -> tuple[str, bytes]:
        history = await self._client.history(task_id)
        files = outputs_of(history)
        if not files:
            raise AppError(
                ErrorCode.WORKFLOW_ERROR,
                "ComfyUI 没有产出任何文件",
                f"prompt_id={task_id}，用的是 {self._used.get(task_id, '?')}。",
                [
                    "确认图的末端有 SaveImage / VHS 之类的保存节点",
                    "在 ComfyUI 界面里手动跑一次同一份图确认能出片",
                ],
                {"raw": str(history)[:2000]},
            )
        chosen = files[-1]
        data = await self._client.download(chosen["filename"], chosen["subfolder"], chosen["type"])
        return chosen["filename"], data


def _error_detail(status: dict[str, Any]) -> str:
    """把 ComfyUI 的 messages 里那条 execution_error 摘出来，别让人去翻原始 JSON。"""
    for entry in status.get("messages") or []:
        if isinstance(entry, list) and len(entry) == 2 and entry[0] == "execution_error":
            info = entry[1] if isinstance(entry[1], dict) else {}
            return (
                f"{info.get('node_type') or '节点'} 执行失败："
                f"{info.get('exception_message') or '（ComfyUI 没有给原因）'}"
            )
    return "ComfyUI 报告任务失败，但没有给出原因。"


def detached_submit_error(exc: AppError, source: str, removed: Detached) -> AppError:
    """提交被 ComfyUI 拒绝、而这一次又摘过节点时，多给一条指向那件事的建议。**两条路共用。**

    摘掉一个这次用不上的媒体入口（`comfy/graph.py::detach`）有一种会咬人的情形：图里那一格是
    **必填**的（例如 `ImageBatch.image1`）。这时 ComfyUI 回的是「Required input is missing」，
    而那个输入正是我们刚切掉的——不点出来的话，用户只会看着一份自己明明存好的图发愣。
    **那一种现在会先自己接回去重试**（`ComfyTasks._submit_graph`），接不上才落到
    `cut_submit_error`；走到这里的是「拒绝的原因与我们切的那一格对不上」的其余情形。

    **只有真摘过、且真是「ComfyUI 拒绝了这份图」时才加**：离线或超时那类失败与摘节点无关，
    多这两句只会把真正的原因埋掉（硬约束 4 要的是说清，不是多说）。
    """
    if not removed or exc.code != ErrorCode.WORKFLOW_ERROR:
        return exc
    nodes = removed.nodes
    which = "、".join(f"{r['title'] or r['class_type']}#{r['node_id']}" for r in nodes[:6])
    return AppError(
        exc.code,
        exc.title,
        exc.detail,
        [
            *exc.suggestions,
            f"这一次从{source}里摘掉了 {len(nodes)} 个这一版用不上的节点（{which}）："
            "它们是登记为入口、但这个镜头没有值的媒体格子，以及只为它们服务的中间节点",
            "如果上面的报错说某个输入缺失，说明图里那一格是必填的——把这个入口从图里删掉，"
            "或者改成不依赖它的接法（例如末帧那一支单独存一份图）",
        ],
        exc.related_ids,
    )


def _missing_cuts(exc: AppError, cuts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """ComfyUI 点名的那几个「必填输入缺失」里，哪些正是摘节点时被我们切掉的那一根。

    **必须两头都对上**（节点 id + 输入名）：只按输入名认的话，图里另一个也叫 `image1` 的
    节点缺输入时，我们会去改一个这次根本没碰过的地方，把一条本来清楚的错误变成一次乱接线。
    """
    if not cuts or exc.code != ErrorCode.WORKFLOW_ERROR:
        return []
    hit = {
        (fault.node_id, fault.input)
        for fault in rejection.faults_of(exc)
        if fault.kind == rejection.MISSING_INPUT and fault.input
    }
    if not hit:
        return []
    return [cut for cut in cuts if (str(cut["node_id"]), str(cut["field"])) in hit]


def cut_submit_error(exc: AppError, source: str, cuts: Sequence[dict[str, Any]]) -> AppError:
    """切断的那一根是必填的、又接不回去时那条错误。**点名到「节点.输入」。**

    接不回去只有一种原因：这份图里没有第二个同类节点可以接（`reconnect` 只认 class_type
    相等，绝不猜类型）。这时唯一诚实的回答是「这份图要求这一格必须有素材」，并给两条真能
    走通的出路——把「参考图不够」丢成一个 400 就完事，正是硬约束 4 说的静默失败。
    """
    which = "、".join(f"节点 {c['node_id']} 的 {c['field']}" for c in cuts[:4])
    origin = "、".join(
        sorted({str(c.get("was_title") or c.get("was_class") or "?") for c in cuts})[:4]
    )
    return AppError(
        exc.code,
        exc.title,
        f"{exc.detail} 这一次从{source}里摘掉了这一版用不上的媒体入口（{origin}），"
        f"于是 {which} 空了出来——而图里那一格是必填的，ComfyUI 因此拒收整份图。",
        [
            f"给这个镜头补上对应的素材（{origin} 那一格要的那种），这一格就不会再空着",
            f"或在 ComfyUI 里把 {which} 改成不依赖它的接法"
            "（例如把合批节点换成只收一路的接法，或给需要它的那一支单独存一份图）",
            *exc.suggestions,
        ],
        exc.related_ids,
    )
