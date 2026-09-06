"""内置 SKILL：四种参考图形态下的镜头 prompt 结构。

**为什么要有这一层。** 以前给模型的只有「写一段画面描述」，于是它写出来的东西与
「这个镜头到底挂了首帧还是末帧」毫无关系——挂了首帧的镜头，prompt 里得有一句
「画面从首帧那一格开始」；挂了首尾帧的，还得有一句「结束时精确落回末帧」。
这些话怎么写不是我们发明的，是模型端推荐的结构（`skill/*.txt`），所以把它们
原样做成四份可读的 SKILL。

**四份的差别只有一件事：那句锚定语怎么写。** 最终提交出去的六段格式由代码在提交那一刻拼
（`generation/providers/base.py::render_video_prompt`），因为 `<Picture n>` 的编号取决于用户
那份 ComfyUI 图实际怎么接的线（`generation/comfy/graph.py::feed_order`）——模型在写 prompt
的时候压根不知道这个数。所以 SKILL 只管「这个形态下 visual_prompt 里要写哪句话」，
**编号与段落结构一个字都不让模型写**（`PICTURE_RULE`）。以前这几份里写着「第 1 张就是
`<Picture 1>`」「Picture 1 是首帧」，那是两个假设叠在一起：既假设首帧排第一，又假设标题序号
就是喂入顺序。两个都不成立时模型会理直气壮地把台词分给错的人。

**渐进披露是它的要点。** 系统提示词里**只放 `catalog()` 那几行**（名字 + 什么时候用），
全文由 `read_skill` 工具按需取一份。四份全塞进系统提示词等于每一轮都多烧几千 token，
也正是老的一次性拆解会超时的那个毛病。

**为什么是 Python 常量而不是随包的 .md**：照 `app/ai/prompts.py` 的先例——冻结成
sidecar 时不需要额外 `--add-data`，少一条「打包后 AI 就不会写 prompt 了」的路。

**配乐那一节刻意保留但固定写「无配乐」**：`non_diegetic_music` 是原结构的一部分，
去掉它就与模型端推荐的形状不再一一对应；而本项目不生成配乐（`prompts` 里那条硬约束）。
所以段落留着、内容固定，真正的兜底仍然是
`prompts.with_shot_audio_policy()`（正向补「声音设计：」、负向补 background music 那几项），
这里不重写一份。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import AppError, ErrorCode

#: 配乐那一节怎么填。四份共用一句，别在每份里各写一遍。
AUDIO_RULE = (
    "overall_soundscape 只写人物对白、同期环境声与叙事必需的动作音效（对白保留说话人与原台词，"
    "没有对白不要编）；non_diegetic_music 固定写 none —— 本项目不生成配乐 / BGM / 配乐轨。"
)

#: 你给的是哪几段。四份共用，别各写一遍。
FIELDS_RULE = """你只提供三段字段，六段格式由系统在提交时拼好：

  - `camera_motion`：景别 + 运镜 + 幅度与速度（如「近景，固定机位，缓慢推近」）；
  - `visual_prompt`：画面里看得见的东西 + 这一份 SKILL 要求的那句锚定语；
  - `audio_dialogue`：对白与同期环境声、必要动作音效。"""

#: 编号那件事怎么说。四份共用——写四遍必然分叉，而分叉的样子恰好是
#: 「模型以为 `<Picture 1>` 是首帧，而这份图上第一张喂进去的是角色三视图」。
PICTURE_RULE = """编号不由你写：**一个 `<Picture n>` / `<Subject n>` 都不要自己编**，
那几段结构性的话（对齐说明 / subject_definitions / summary / retention_analysis）也不要写。

这一次先喂哪一张图，取决于用户那份 ComfyUI 图是怎么接的线——本工具在提交那一刻才顺着接线
数出来，你写 prompt 的时候还不知道这个数。所以 `visual_prompt` 里就用「首帧」「末帧」
「这个角色」这样的说法，系统会照本次图册把它们接到正确的编号上。

自己编一个 `<Picture 1>` 十有八九指错人，**而这种错在队列里一条报错都没有**：
成片能出来，只是两个角色互相串味、台词落到别人头上。"""


@dataclass(frozen=True)
class Skill:
    """一份 SKILL。`name` 就是 `read_skill` 的参数，也是提案里 `skill` 字段的值。"""

    name: str
    title: str
    #: 什么形态下用它。这一行会进系统提示词的清单，所以要一眼看出「该挑哪份」。
    when: str
    #: 怎么写：段落名、锚定语的写法、时间轴对齐那句话。
    guide: str
    #: 范例，原样搬自 `skill/*.txt`。模型照着抄结构比读十条规则准。
    example: str


_FLF = Skill(
    name="flf",
    title="首帧 + 末帧 → 视频（FL2VA）",
    when="这个镜头同时指定了首帧与末帧（两张图都有）。",
    guide="""这个形态的命门是**收敛那句话**。`visual_prompt` 按这个顺序写：

影像风格 → 「画面从首帧建立的构图开始」→ 主体与它必须保持一致的属性 → 环境与光线 →
中间过程 → 结尾**必须**是「通过可观察的中间状态逐步收敛，最终精确落回末帧建立的构图」。

少了收尾那句，模型会在末帧附近乱走。首尾帧那两张图对到目标视频的哪一秒由系统写
（它知道这一镜多长、也知道这一次两张帧各排第几），你只要把「从首帧开始 / 落回末帧」
这两句话写进画面描述里。

只写画面里看得见的东西：不写心理活动，不写「接上一镜」这类只有人看得懂的话。""",
    example="""你给的三段：

camera_motion: 近景，固定机位，正面缓慢推近，幅度小、速度慢
visual_prompt: 实拍电影感。画面从首帧建立的构图开始：暗色极简影棚、薄雾、精确的反光与充足的
负空间，一道轮廓光扫过主体边缘。运动通过可观察的中间状态逐步收敛，最终精确落回末帧建立的构图。
audio_dialogue: 一声轻微的旋钮咔嗒，克制的室内底噪与一记同步的低频脉冲。

系统提交时拼成（节选，编号来自这一次的图册）：

How the reference pictures align with the target video — <Picture 1> (from [Shot 1]) aligns with \
the 0.00-second mark of the target video; <Picture 2> (from [Shot 1]) aligns with the 4.00-second \
mark of the target video.

detailed_description:
[Shot 1] 实拍电影感。画面从首帧建立的构图开始：…… Camera Motion: 近景，固定机位，正面缓慢推近

其余几段（subject_definitions / summary / retention_analysis / overall_soundscape /
non_diegetic_music）同样由系统拼好。""",
)

_I2V = Skill(
    name="i2v",
    title="首帧 → 视频（I2VA）",
    when="只指定了首帧（没有末帧）。最常见的一种。",
    guide="""`visual_prompt` 按这个顺序写：

影像风格 → 「首帧里的构图、主体外观与空间关系保持一致」→ 主体必须保持的属性 →
环境与光线 → 画面如何发展到结尾。

**不要**写「结束时回到某张图」——这个模式没有末帧，写了会把运动锁死。
「首帧对到 0.00 秒」那句对齐说明由系统写，你不用写。""",
    example="""你给的三段：

camera_motion: 中景，缓慢推进，幅度小、速度慢
visual_prompt: 实拍电影感。首帧里的构图、主体外观与空间关系保持一致：暗色极简影棚、薄雾、
精确的反光与充足的负空间。轮廓光扫过边缘，随后主体稳稳落进一个干净的主视觉画面。
audio_dialogue: 一声轻微的旋钮咔嗒，克制的室内底噪与一记同步的低频脉冲。

系统提交时拼成（节选，编号来自这一次的图册）：

For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully \
referenced.

detailed_description:
[Shot 1] 实拍电影感。首帧里的构图、主体外观与空间关系保持一致：…… Camera Motion: 中景，缓慢推进

其余几段由系统拼好。挂了角色三视图之类的参考素材时，`subject_definitions` 与
`retention_analysis` 会把「谁是谁、别把两个人混成一个」一并写清楚。""",
)

_L2V = Skill(
    name="l2v",
    title="末帧 → 视频（L2VA）",
    when="只指定了末帧（没有首帧）。常见于「要接到下一幕那张图上」。",
    guide="""`visual_prompt` 按这个顺序写：

影像风格 → 「画面从一个能合理通向末帧的状态开始」（**不要**描述一张具体的首帧，
那是模型自己生成的）→ 主体必须保持的属性 → 环境与光线 → 结尾**必须**是
「所有运动逐渐失去动量，最终落到末帧建立的主体位置、机位、光线与构图」。

「末帧对到第几秒」那句对齐说明由系统写（它知道这一镜多长），你不用写。""",
    example="""你给的三段：

camera_motion: 中景，缓慢推进，幅度小、速度慢
visual_prompt: 实拍电影感。画面从一个能合理通向末帧的状态开始：暗色极简影棚、薄雾、精确的
反光与充足的负空间。所有运动逐渐失去动量，最终落到末帧建立的主体位置、机位、光线与构图。
audio_dialogue: 一声轻微的旋钮咔嗒，克制的室内底噪与一记同步的低频脉冲。

系统提交时拼成（节选，编号来自这一次的图册）：

How the reference pictures align with the target video — <Picture 1> (from [Shot 1]) aligns with \
the 4.00-second mark of the target video.

detailed_description:
[Shot 1] 实拍电影感。画面从一个能合理通向末帧的状态开始：…… Camera Motion: 中景，缓慢推进

其余几段由系统拼好。""",
)

_REF = Skill(
    name="ref",
    title="参考素材 → 视频（Ref2VA）",
    when="没有首帧也没有末帧，只有参考素材（角色三视图、地点参考图、道具图）；一个都没有时也用这一份。",
    guide="""这一份没有帧可锚，所以 `visual_prompt` 里最要紧的是**按名字点人**：
「宋焘与张秀才并排坐在几案后」这样写，系统会把这两个名字接到各自的 `<Subject n>` 上
（名字来自素材本身，所以人名必须与素材库里逐字一致）。

`visual_prompt` 按这个顺序写：整体影像风格 → 主体带着被参考的特征出现 → 环境与光线 →
画面如何发展。

**这一份最容易写坏的一句是「保持两人服饰与面容一致」**：它本意是「别在镜头里忽然换装」，
模型只看字面，读出来就是「让这两个人长得一样」——那正是「张秀才长成了宋焘」的来源。
所以**不要**写这类跨人物的「保持一致」，谁保住谁的样子由系统在 `retention_analysis` 里
逐个写清（还会补一句「他们是不同的人，绝不能把一个人的脸 / 发型 / 服装搬到另一个人身上」）。

**一个参考素材都没有时**照样用这一份：系统会把 `subject_definitions` 与
`retention_analysis` 写成 none，这时它就是一段纯文本描述。""",
    example="""你给的三段：

camera_motion: 近景，固定机位，正面缓慢推近
visual_prompt: 实拍电影感，光线连贯、画面稳定。宋焘与张秀才并排坐在殿檐下的几案后，
两人各自带着自己被参考的形象特征；廊下阴影深长，青砖地面泛着湿光。
audio_dialogue: 宋焘低声道：「此题何解？」远处更漏滴答，纸页在风里轻响。

系统提交时拼成（编号与人名来自这一次的图册）：

subject_definitions:
<Subject 1> is the visible subject shown in <Picture 1>: 宋焘。青灰长衫，方脸，短须……
<Subject 2> is the visible subject shown in <Picture 2>: 张秀才。月白长衫，清瘦，二十上下……

summary:
[reference generation] Create a 4.00-second target video featuring <Subject 1> and <Subject 2>. \
<Picture 1> and <Picture 2> provide the visible identity of these subjects.

retention_analysis:
<Subject 1>（宋焘）(appears in [Shot 1]): fully_preserved - keep the exact face, hair, build and \
costume shown in <Picture 1> unchanged; do not blend it with any other subject.
<Subject 2>（张秀才）(appears in [Shot 1]): fully_preserved - keep the exact face, hair, build \
and costume shown in <Picture 2> unchanged; do not blend it with any other subject.
<Subject 1> and <Subject 2> are different people: never copy the face, hair or costume of one \
onto another, and never merge two of them into one person, even if the shot description asks for \
a consistent look.

detailed_description:
[Shot 1] 实拍电影感，光线连贯、画面稳定。宋焘与张秀才并排坐在…… Camera Motion: 近景，固定机位，\
正面缓慢推近

overall_soundscape / non_diegetic_music 同样由系统拼好。""",
)

#: 四份内置 SKILL。名字就是 `read_skill` 的参数，也是提案里 `skill` 字段的值。
SKILLS: dict[str, Skill] = {s.name: s for s in (_FLF, _I2V, _L2V, _REF)}

NAMES = tuple(SKILLS)


def catalog() -> str:
    """给系统提示词用的清单。**只有这几行进提示词**，全文靠 `read_skill` 按需取。"""
    return "\n".join(f"- {s.name}（{s.title}）：{s.when}" for s in SKILLS.values())


def render(name: str) -> str:
    """一份 SKILL 的全文。不认识的名字报四要素错误，不去猜它想读哪份。"""
    skill = SKILLS.get(str(name or "").strip().lower())
    if skill is None:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有这份 SKILL",
            f"name = {name or '（空）'}。",
            [f"可用的是：{'、'.join(NAMES)}", "拿不准就先看镜头挂了首帧还是末帧"],
        )
    return (
        f"# SKILL {skill.name} · {skill.title}\n\n"
        f"什么时候用：{skill.when}\n\n"
        f"## 你写哪几段\n{FIELDS_RULE}\n\n"
        f"## 怎么写\n{skill.guide}\n\n{AUDIO_RULE}\n\n"
        f"## 编号不由你写\n{PICTURE_RULE}\n\n"
        f"## 范例\n{skill.example}"
    )


def pick(has_first: bool, has_last: bool, has_refs: bool = False) -> str:
    """按镜头挂了什么挑一份。**判定只有这一处口径**，后端兜底与界面提示共用它。

    首 / 末帧说的是显式槽位（`Shot.first_frame_asset_id` / `last_frame_asset_id`），
    也就是「用户按下去的那一下」——参考素材永远不会被提拔成首帧
    （见 `services/context.py::_assign_roles`）。
    """
    if has_first and has_last:
        return "flf"
    if has_first:
        return "i2v"
    if has_last:
        return "l2v"
    return "ref"  # 有参考素材、或者一张都没有：都是这一份（后者退化成纯文本描述）
