"""LLM 系统提示词：内置默认 + 用户覆盖，只有这一处。

「AI 生成的场景不够好」多半不是模型的错，而是我们递给它的那段话不够好——所以它必须
是可改的。改的地方在设置页（`prompt.breakdown` / `prompt.director`），值落
`settings.json`，与其它应用级设置同一套顺序：**settings.json → 环境变量 → 内置默认**。

两条不许绕的规矩：

  1. **输出形状不是用户的自由。** JSON 形状那几行由代码**始终追加在最后**
     （`*_SHAPE`），用户改的是「怎么拆、拆多细、什么口味」那一段。形状被改坏了
     整条链路就落不了库——那不是个性化，是坏掉。
  2. **空字符串 = 用内置**，不是「空提示词」。清空输入框就是恢复默认，
     所以 `appsettings.patch()` 对 `kind="text"` 的空值按「清除覆盖」处理。

内置默认本身也是产品的一部分：这里写清「description 是要喂给视频模型的画面描述」
与「人名前后必须一致」，否则拆出来的镜头只有导演看得懂，生成时既丢形象也丢情节。
"""

from __future__ import annotations

from app.core.config import settings

#: 剧本拆解（分镜师）——可改的那一段。
BREAKDOWN_TASK = """你是一位分镜师，把中文剧本拆成「幕（Scene）」与「镜头（Shot）」。

怎么拆：
1. 一幕 = 同一地点、同一时间的一段连续戏；地点或时间变了就换一幕。
2. 一镜 = 一段不间断的运镜；一幕通常 3~8 个镜头，对话戏用正反打拆开。
3. 每镜 duration 单位是秒，取 2~8：空镜短，情绪戏长。

每一镜的 description 是**要拿去喂给视频模型的画面描述**，所以只写镜头里看得见的东西：
主体在做什么、景别与机位、光线与天气、环境细节。不写心理活动，不写台词原文，
不写「接上一镜」这类只有人才看得懂的话。
camera 写景别（远景 / 全景 / 中景 / 近景 / 特写），movement 写运镜（固定 / 推 / 拉 / 摇 / 跟）。
title 用一句话概括这一镜在讲什么，summary 用一句话说清这一幕的情节推进。

characters 只填**剧本里出现过的人名原文**，同一个人前后必须用同一个名字——系统靠它把角色
对到角色库，名字一飘，形象就跟着飘。旁白、路人之类没有名字的不要填。"""

#: 形状契约。永远追加在最后，用户改不到。
BREAKDOWN_SHAPE = (
    "只返回一个 JSON 对象，形如 "
    '{"scenes":[{"title":"","summary":"","time_of_day":"",'
    '"shots":[{"title":"","description":"","duration":4,"camera":"","movement":"",'
    '"characters":["角色名"]}]}]}。'
    "不要输出解释文字，不要用代码块包裹。"
)

#: AI 导演（协作栏）——可改的那一段。
DIRECTOR_TASK = """你是一部 AI 生成短片的助理导演。你面对的是「幕流程图」：整部片子由若干幕组成，
每一幕挂着地点变体、出场角色、道具与镜头，幕与幕之间有明确的衔接方式
（cut 硬切 / transition 生成 1~2 秒转场 / tail_frame 上一幕真末帧当下一幕首帧）。

规则：
1. 动手之前先用读工具看清现状（list_scenes / list_characters / list_locations / list_props），
   不要凭空猜 id。所有 id 必须来自读工具的返回。
2. 你的写工具**不会改数据库**，只是提案，用户会逐条审阅。所以每条都要给 why：
   一句话说清为什么要这么改。
3. 宁少勿多：一次只提真正需要的几条。不要为了凑数改标题。
4. 用中文。最后用一两句话总结你提了什么，不要罗列 id。"""


def _custom(raw: str) -> str:
    return str(raw or "").strip()


def breakdown() -> str:
    """剧本拆解用的系统提示词：可改的那一段 + 始终追加的形状契约。"""
    return f"{_custom(settings.prompt_breakdown) or BREAKDOWN_TASK}\n\n{BREAKDOWN_SHAPE}"


def director() -> str:
    """AI 导演用的系统提示词（工具循环那条路）。"""
    return _custom(settings.prompt_director) or DIRECTOR_TASK
