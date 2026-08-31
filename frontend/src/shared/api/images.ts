/**
 * 图片素材生成（第三条生成链）。
 *
 * 字段与后端 app/api/images.py + services/images.py 一一对应。
 *
 * 三个形状上的要点：
 *   1. **文案不在组件里抄第二份**——SKILL 的标题 / 适用场景 / 固定写死的那几句
 *      全部来自 `skills()`，前端只渲染。加一份 SKILL 时界面一行不用改。
 *   2. **先账单再动手**——`plan()` 是只读的（用哪个协议、照哪份 SKILL、拼出来的
 *      正 / 负向 prompt 全文、图会落到哪里、缺什么），`generate()` 才入队。
 *   3. **用户那段话只填「长什么样」**——四视图、纯背景、无文字那些结构由 SKILL 补齐，
 *      所以输入框的占位符要照 `ImageSkill.lead` 写，别让用户自己再写一遍。
 */

import { api } from './client'

/** 出图对象。素材图三种 + 镜头首 / 末帧候选两种。 */
export const IMAGE_TARGETS = [
  'appearance',
  'location_variant',
  'prop',
  'shot_first_frame',
  'shot_last_frame',
] as const
export type ImageTarget = (typeof IMAGE_TARGETS)[number]

export const IMAGE_TARGET_LABEL: Record<ImageTarget, string> = {
  appearance: '角色形象',
  location_variant: '地点变体',
  prop: '道具',
  shot_first_frame: '镜头首帧候选',
  shot_last_frame: '镜头末帧候选',
}

/**
 * 一份内置出图 SKILL。`fixed` 是**系统固定补齐**的那几句（四视图、纯白背景、无文字……），
 * `lead` 是给用户那个输入框的引导语——两句都在后端写一遍，界面原样显示。
 */
export interface ImageSkill {
  name: string
  title: string
  when: string
  fixed: string
  lead: string
  note: string
}

export interface ImageSkills {
  items: ImageSkill[]
  /** 「你只写长什么样」那条规矩的全文。弹窗里显示一次，别在组件里另写一句。 */
  rule: string
}

/** 账单里那几张参考图（只认显式传进来的，服务端不替用户猜）。 */
export interface ImagePlanRef {
  asset_id: string
  file: string
  media: string
}

/** 现在用的是哪个图片服务。`configured === false` 时按钮要 disabled 并写明原因。 */
export interface ImageProviderInfo {
  name: string
  label: string
  configured: boolean
  supports_refs: boolean
  preset: string
  model: string
  size: string
}

/**
 * 出这张图之前的那份账单。**只读，一行库都不改。**
 *
 * `missing[]` 是四要素错误的列表（没配服务、没指预设），`can_generate === false`
 * 时「生成」不能点，但账单里的 `prompt` 照样拼好了——用户能先看清系统会补哪几句。
 */
export interface ImagePlan {
  target_kind: string
  target_id: string
  /** 「角色 · 阿岚 · 默认形象 四视图」。队列面板与账单共用这一句，前端不拼第二遍。 */
  target_label: string
  skill: { name: string; title: string; when: string }
  /** 用户那段话本身（只写「长什么样」的那一段）。 */
  user_text: string
  /** 拼好之后的正向 prompt 全文：结构是系统补的，不是用户写的。 */
  prompt: string
  negative_prompt: string
  refs: ImagePlanRef[]
  /** 「图会落到哪里」那句话。前端原样显示。 */
  lands: string
  asset_kind: string
  provider: ImageProviderInfo
  /** 降级说明（比如这个端收不了参考图）。跳过不是失败，但必须说出来。 */
  warnings: string[]
  missing: {
    code: string
    title: string
    detail: string
    suggestions: string[]
  }[]
  can_generate: boolean
}

/**
 * 入队回来的那一行任务。**素材图不属于任何镜头**，所以 `shot_id` 是 null——
 * 它和视频任务在同一张 job 表、同一个 pump 里，底部控制台照 `target_label` 认它。
 */
export interface ImageJob {
  id: string
  shot_id: string | null
  target_kind: string | null
  target_id: string | null
  /** t2i（没带参考图）/ i2i（带了）。 */
  kind: string
  status: string
  priority: number
  created_at: string
  target_label: string | null
  /** 入队时冻结的那份账单：SKILL 之后改了，已入队的这一张也不该变样。 */
  plan: ImagePlan
}

export interface ImageBody {
  target_kind: ImageTarget | string
  target_id: string
  /** 只填「长什么样」。 */
  prompt?: string
  /** 留空就按 target_kind 自动选（角色→四视图，地点→简单场景图，道具→单件白底）。 */
  skill?: string | null
  ref_asset_ids?: string[]
}

export const imagesApi = {
  /** 内置的三份出图 SKILL。界面上那个下拉的文案只有这一份。 */
  skills: (pid: string) => api.get<ImageSkills>(`/projects/${pid}/images/skills`),
  /** 只出账单，不入队、不改库。 */
  plan: (pid: string, body: ImageBody) =>
    api.post<ImagePlan>(`/projects/${pid}/images/plan`, {
      prompt: '',
      skill: null,
      ref_asset_ids: [],
      ...body,
    }),
  generate: (pid: string, body: ImageBody & { priority?: number }) =>
    api.post<ImageJob>(`/projects/${pid}/images/generate`, {
      prompt: '',
      skill: null,
      ref_asset_ids: [],
      priority: 100,
      ...body,
    }),
}
