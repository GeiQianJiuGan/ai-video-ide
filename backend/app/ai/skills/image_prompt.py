"""内置 SKILL：三种素材参考图的提示词结构（角色四视图 / 场景参考图 / 道具图）。

**为什么要有这一层。** 这些图不是随便一张好看的图：角色形象要能当参考素材一路喂进
`AIVS_REF_*`，所以它必须是**四视图、纯背景、同一套服装**——一张有环境、有构图、有前景遮挡
的角色照喂进去，模型只会把那些环境一起学过去。地点参考图必须**没有人**，否则那个人会在
每一个用到这张图的镜头里冒出来。道具图必须单件、白底、完整可见。

这些话每次都要写，而且**写错了用户看不出来**（图还是出得来，只是几秒之后人物就跑偏了）。
所以它们由系统按 SKILL 固定补齐，用户那段话只填「长什么样」——这正是这一轮要的东西。

**与 `video_prompt.py` 的关系**：那边的 `Skill` 是「教 AI 怎么写一段 prompt」，这边是
「系统自己往 prompt 里补的那句话」，两件事，所以形状不共用（`ImageSkill` 多一个 `fixed`
字段，那就是真正拼进 prompt 的文本）。**渐进披露照旧**：系统提示词里只有 `image_catalog()`
那三行，全文由同一个 `read_skill` 工具按需取。

**拼装只有一处口径**（`render_image_prompt`）：AI 提案那条路与界面上那个「生成参考图」
按钮共用它。两处各拼一次的话，AI 出的图和手点出的图会不是一套规格，而这件事同样看不出来。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import AppError, ErrorCode

#: 三份共用的负向提示词。**别在每份里各写一遍**：这几项（文字、水印、边框、拼图）
#: 是参考图的通病，出现哪一样都会让这张图当参考素材时把它带进画面。
IMAGE_NEGATIVE = (
    "text, watermark, signature, logo, frame, border, collage, "
    "extra limbs, deformed hands, blurry, lowres, jpeg artifacts"
)

#: 给 AI 看的那条规矩（进 `image_render()` 的全文，也进 `prompts.DIRECTOR_IMAGE_CONTRACT`）。
IMAGE_RULE = (
    "你只写「长什么样」：外形、年龄气质、服装配色、材质、时间与天气这类事实。"
    "四视图、纯背景、无文字、无人物那些话由系统按 SKILL 固定补齐，"
    "**你不要自己写**——重复写只会互相打架（例如你写了「电影感光影」，"
    "而参考图恰恰要的是平光无投影）。"
)


@dataclass(frozen=True)
class ImageSkill:
    """一份出图 SKILL。

    `fixed` 是**真的拼进 prompt 的那段话**（结构由它决定），`lead` 是用户那段话前面
    加的引子（「这个角色长这样：」），`negative` 是这一份额外要挡的东西。
    """

    name: str
    title: str
    #: 什么时候用它。这一行会进系统提示词的清单。
    when: str
    fixed: str
    lead: str
    #: 额外负向（接在 `IMAGE_NEGATIVE` 后面）。
    negative: str = ""
    #: 给人 / 给 AI 的补充说明，只进全文，不进 prompt。
    note: str = ""


_CHAR_SHEET = ImageSkill(
    name="char_sheet",
    title="角色四视图（角色形象参考）",
    when="要一张角色形象参考图（角色 / 形象素材）。喂给模型当人物形象参考的就是这一张。",
    fixed=(
        "同一角色的角色设定四视图：正面、四分之三侧面、正侧面、背面，"
        "横向等高并排排列，全身完整可见，同一套服装、同一套配色、同一个发型，"
        "纯白背景，平光、无投影、无环境，无文字标注、无箭头、无分格线。"
    ),
    lead="这个角色长这样",
    negative="multiple characters, different outfits, cropped body, dramatic shadows",
    note=(
        "四视图是为了让人物形象在几秒的视频里不跑偏：只喂一张正面照时，"
        "模型一转身就得自己编背面。等高并排是为了每一视图都够大。"
    ),
)

_SCENE_SIMPLE = ImageSkill(
    name="scene_simple",
    title="场景参考图（简单）",
    when="要一张地点 / 场景参考图（地点变体素材），也用于给镜头出一张首尾帧候选。",
    fixed=(
        "一张干净的场景参考图：**画面里没有人物**、没有动物，"
        "自然光，广角平视，构图居中并留出空间，景深自然，"
        "无文字、无水印、无前景遮挡。"
    ),
    lead="这个地点长这样",
    negative="person, people, crowd, silhouette of a person, foreground clutter",
    note=(
        "刻意不写「电影感」「戏剧光影」那类词：这张图是要被复用到很多镜头里的地点参考，"
        "带上强烈的光影与构图之后，每个用到它的镜头都会被那一套光锁死。"
        "**无人物**是硬要求——图里那个人会在每一个引用这张图的镜头里冒出来。"
    ),
)

_PROP_REF = ImageSkill(
    name="prop_ref",
    title="道具参考图",
    when="要一张道具参考图（道具素材）。",
    fixed=(
        "单件道具的产品级参考图：只有这一件物体，纯白背景，四分之三视角，"
        "整体完整可见、不出画，柔和均匀的光，无手、无人、无支架，"
        "无文字、无水印。"
    ),
    lead="这件道具长这样",
    negative="hands, person, multiple objects, cropped object, busy background",
    note="纯白底 + 完整可见是为了它当参考素材时能被干净地取用，不把背景一起带进镜头。",
)

#: 三份内置出图 SKILL。名字就是 `read_skill` 的参数，也是提案里 `skill` 字段的值。
IMAGE_SKILLS: dict[str, ImageSkill] = {s.name: s for s in (_CHAR_SHEET, _SCENE_SIMPLE, _PROP_REF)}

IMAGE_NAMES = tuple(IMAGE_SKILLS)

#: 素材类型 → 默认用哪一份。**只有这一张表**：AI 提案、界面按钮、服务层兜底共用它。
#: 镜头首 / 末帧走场景那一份——它要的正是「一张干净的画面」，而不是四视图。
BY_TARGET: dict[str, str] = {
    "appearance": "char_sheet",
    "location_variant": "scene_simple",
    "prop": "prop_ref",
    "shot_first_frame": "scene_simple",
    "shot_last_frame": "scene_simple",
}


def image_catalog() -> str:
    """给系统提示词用的清单。**只有这三行进提示词**，全文靠 `read_skill` 按需取。"""
    return "\n".join(f"- {s.name}（{s.title}）：{s.when}" for s in IMAGE_SKILLS.values())


def image_get(name: str) -> ImageSkill:
    """按名字取一份。不认识的名字报四要素错误，不去猜它想要哪一份。"""
    skill = IMAGE_SKILLS.get(str(name or "").strip().lower())
    if skill is None:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有这份出图 SKILL",
            f"skill = {name or '（空）'}。",
            [
                f"可用的是：{'、'.join(IMAGE_NAMES)}",
                "角色用 char_sheet、地点用 scene_simple、道具用 prop_ref",
            ],
            {"skill": name},
        )
    return skill


def image_render(name: str) -> str:
    """一份出图 SKILL 的全文（`read_skill` 取的就是这个）。"""
    skill = image_get(name)
    lines = [
        f"# SKILL {skill.name} · {skill.title}",
        "",
        f"什么时候用：{skill.when}",
        "",
        "## 系统固定补的那段（你不要重复写）",
        skill.fixed,
        "",
        f"## 你要写的那段\n{skill.lead}：<在这里写外形、气质、服装配色、材质、时间与天气>",
        "",
        f"## 规矩\n{IMAGE_RULE}",
        "",
        f"## 负向（系统自动加）\n{IMAGE_NEGATIVE}"
        + (f"\n这一份还会加：{skill.negative}" if skill.negative else ""),
    ]
    if skill.note:
        lines += ["", f"## 为什么这么定\n{skill.note}"]
    return "\n".join(lines)


def image_pick(target_kind: str) -> str:
    """这类素材默认用哪一份 SKILL。**判定只有这一处口径**。"""
    found = BY_TARGET.get(str(target_kind or "").strip())
    if found is None:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识这种素材类型",
            f"target_kind = {target_kind or '（空）'}。",
            [f"可用的是：{'、'.join(BY_TARGET)}"],
            {"target_kind": target_kind},
        )
    return found


def render_image_prompt(name: str, user_text: str) -> tuple[str, str]:
    """拼出最终的正 / 负向提示词。**唯一那处拼装口径**（AI 路径与手动按钮共用）。

    结构由 SKILL 决定（`fixed`），用户那段话只填「长什么样」。空着也照旧拼得出来
    ——那就是一张只有结构没有细节的图，不值得为此报错挡住整条路。
    """
    skill = image_get(name)
    text = str(user_text or "").strip()
    positive = f"{skill.fixed}\n{skill.lead}：{text}" if text else skill.fixed
    negative = f"{IMAGE_NEGATIVE}, {skill.negative}" if skill.negative else IMAGE_NEGATIVE
    return positive, negative


def image_listing() -> list[dict[str, str]]:
    """给界面那个下拉用的清单。**文案只有这一份**，前端不要再抄一遍。"""
    return [
        {
            "name": s.name,
            "title": s.title,
            "when": s.when,
            "fixed": s.fixed,
            "lead": s.lead,
            "note": s.note,
        }
        for s in IMAGE_SKILLS.values()
    ]
