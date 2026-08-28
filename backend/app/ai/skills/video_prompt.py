"""内置 SKILL：四种参考图形态下的镜头 prompt 结构。

**为什么要有这一层。** 以前给模型的只有「写一段画面描述」，于是它写出来的东西与
「这个镜头到底挂了首帧还是末帧」毫无关系——挂了首帧的镜头，prompt 里得有一句
「画面从首帧那一格开始」；挂了首尾帧的，还得有一句「结束时精确落回末帧」。
这些话怎么写不是我们发明的，是模型端推荐的结构（`skill/*.txt`），所以把它们
原样做成四份可读的 SKILL。

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
    guide="""四段，段落名固定英文，内容用中文写也可以：

1. 第一行是**对齐说明**（没有段落名）：写清哪张图对到目标视频的哪一秒，例如
   「Picture 1 (from [Shot 1]) aligns with the 0.00-second mark of the target video;
   Picture 2 (from [Shot 1]) aligns with the N.00-second mark.」N 用这个镜头的时长。
2. `integrated_multimodal_description:` 以 `[Shot n]` 开头，依次写：影像风格 →
   「画面从 Picture 1 建立的构图开始」→ 主体与它必须保持一致的属性 → 环境与光线 →
   运镜（幅度 + 速度）→ 中间过程 → 结尾那句**必须**是「通过可观察的中间状态逐步收敛，
   最终精确落回 Picture 2 建立的构图」。
   **这一段是首尾帧模式的命门**：少了收敛那句话，模型会在末帧附近乱走。
3. `overall_soundscape:`
4. `non_diegetic_music:`

只写画面里看得见的东西：不写心理活动，不写「接上一镜」这类只有人看得懂的话。""",
    example="""How the reference pictures align with the target video — Picture 1 (from Shot 1) \
aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the \
10.00-second mark of the target video.

integrated_multimodal_description:
[Shot 1] Live-action, cinematic, the video begins in the composition established by Picture 1. \
A matte-red portable speaker remains geometrically consistent. Minimal dark studio, soft haze, \
precise reflections and generous negative space. The camera performs a Push In with small \
amplitude at slow speed. A rim light traces the silhouette, then the speaker settles into a clean \
hero frame. The motion develops through observable intermediate states, progressively narrows \
every visual difference, and settles into the exact composition established by Picture 2 at the \
end of the shot.

overall_soundscape:
One soft dial click, restrained room tone and a synchronized low-frequency pulse.

non_diegetic_music:
none""",
)

_I2V = Skill(
    name="i2v",
    title="首帧 → 视频（I2VA）",
    when="只指定了首帧（没有末帧）。最常见的一种。",
    guide="""四段：

1. 第一行是**对齐说明**：「For the target video, at 0.00 seconds into the target video,
   <Picture 1> (from [Shot n]) is fully referenced.」
2. `integrated_multimodal_description:` 以 `[Shot n]` 开头：影像风格 → 「<Picture 1> 里的
   构图、主体外观与空间关系保持一致」→ 主体必须保持的属性 → 环境与光线 → 运镜（幅度 +
   速度）→ 画面如何发展到结尾。
   **不要**写「结束时回到某张图」——这个模式没有末帧，写了会把运动锁死。
3. `overall_soundscape:`
4. `non_diegetic_music:`""",
    example="""For the target video, at 0.00 seconds into the target video, <Picture 1> \
(from [Shot 1]) is fully referenced.

integrated_multimodal_description:
[Shot 1] Live-action, cinematic, the composition, subject appearance and spatial relationships \
in <Picture 1> remain consistent. A matte-red portable speaker remains geometrically consistent. \
Minimal dark studio, soft haze, precise reflections and generous negative space. The camera \
performs a Push In with small amplitude at slow speed. A rim light traces the silhouette, then \
the speaker settles into a clean hero frame.

overall_soundscape:
One soft dial click, restrained room tone and a synchronized low-frequency pulse.

non_diegetic_music:
none""",
)

_L2V = Skill(
    name="l2v",
    title="末帧 → 视频（L2VA）",
    when="只指定了末帧（没有首帧）。常见于「要接到下一幕那张图上」。",
    guide="""四段：

1. 第一行是**对齐说明**：「<Picture 1> (from [Shot n]) aligns with the N.00-second mark of
   the target video.」N 用这个镜头的时长。
2. `integrated_multimodal_description:` 以 `[Shot n]` 开头：影像风格 → 「画面从一个能合理
   通向 <Picture 1> 的状态开始」（**不要**描述一张具体的首帧，那是模型自己生成的）→
   主体必须保持的属性 → 环境与光线 → 运镜 → 结尾那句**必须**是「所有运动逐渐失去动量，
   最终落到 <Picture 1> 建立的主体位置、机位、光线与构图」。
3. `overall_soundscape:`
4. `non_diegetic_music:`""",
    example="""How the reference pictures align with the target video — <Picture 1> \
(from [Shot 1]) aligns with the 10.00-second mark of the target video.

integrated_multimodal_description:
[Shot 1] Live-action, cinematic, the scene begins in a plausible state that precedes \
<Picture 1>. A matte-red portable speaker remains geometrically consistent. Minimal dark studio, \
soft haze, precise reflections and generous negative space. The camera performs a Push In with \
small amplitude at slow speed. A rim light traces the silhouette, then the speaker settles into \
a clean hero frame. Every moving element gradually loses momentum and settles into the exact \
subject position, camera angle, lighting and composition established by <Picture 1> at the end \
of the shot.

overall_soundscape:
One soft dial click, restrained room tone and a synchronized low-frequency pulse.

non_diegetic_music:
none""",
)

_REF = Skill(
    name="ref",
    title="参考素材 → 视频（Ref2VA）",
    when="没有首帧也没有末帧，只有参考素材（角色三视图、地点参考图、道具图）；一个都没有时也用这一份。",
    guide="""六段。这一份与前三份形状不同：**它要先把「谁是谁」定义清楚**，因为模型端收不到
参考图的标签（`AIVS_REF_*` 只是文件名），只能靠 `<Subject n>` 这套写法对号。

1. `subject_definitions:` 每个主体一行：「<Subject n> is the visible subject shown in
   <Picture n>: …」后面写它必须保留的形状 / 材质 / 颜色 / 服装 / 标识。
   `<Picture n>` 的编号与喂进去的参考图顺序一致（第 1 张就是 `<Picture 1>`）。
2. `summary:` 一句话：要做一段几秒的视频、有哪些主体、哪张图提供了什么。
3. `retention_analysis:` 每个主体一行：`fully_preserved` / `partially_preserved` + 具体保留什么。
4. `detailed_description:` 先一句整体风格，再以 `[Shot n]` 开头写这一镜：主体带着被参考的
   特征出现 → 环境与光线 → 运镜 → 画面如何发展。
5. `overall_soundscape:`
6. `non_diegetic_music:`

**一张参考图都没有时**：`subject_definitions` 与 `retention_analysis` 写 none，
只留 summary / detailed_description / 两段声音——这时它就是一段纯文本描述。""",
    example="""subject_definitions:
<Subject 1> is the visible subject shown in <Picture 1>: A matte-red portable speaker remains \
geometrically consistent. Preserve the exact product shape, material, color and logo.

summary:
[reference generation] Create a 10-second target video featuring <Subject 1>; <Picture 1> \
provides the subject's visible identity and product attributes.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - preserve the exact product shape, material, \
color and logo throughout the target video.

detailed_description:
The target video uses a cinematic product-film style with coherent lighting and stable visual \
continuity.
[Shot 1] <Subject 1> appears with the referenced characteristics clearly visible. Minimal dark \
studio, soft haze, precise reflections and generous negative space. The camera performs a Push In \
with small amplitude at slow speed. A rim light traces the silhouette, then the speaker settles \
into a clean hero frame.

overall_soundscape:
One soft dial click, restrained room tone and a synchronized low-frequency pulse.

non_diegetic_music:
none""",
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
        f"## 怎么写\n{skill.guide}\n\n{AUDIO_RULE}\n\n"
        f"## 范例（照抄这个结构）\n{skill.example}"
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
