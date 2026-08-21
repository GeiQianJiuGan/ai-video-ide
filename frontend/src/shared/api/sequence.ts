/**
 * 幕流程图与编排接口（两级场景系统的第一级）。
 *
 * 字段与后端 app/api/sequence.py + services/sequence.py 一一对应。
 *
 * 三个形状上的要点：
 *   1. **衔接是一等公民**——`SceneLink` 决定两幕之间怎么接，同一对场景只有一条，
 *      所以是 `PUT`（upsert）而不是 `POST`；每种模式后端都给一句 `hint`，前端直接显示。
 *   2. **先账单再动手**——`plan()` 是只读的，把「要生成几条、要补几段转场、缺什么」
 *      全列出来；`run()` 才入队，返回里带上那份账单，界面上「说好的」和「做了的」能对上。
 *   3. **转场是一个正常镜头**——`kind === 'transition'`，属于上一幕且排在最后，
 *      所以时间线自动装配天然把它放在两幕之间。界面上要能和导演排的戏区分开。
 */

import { api } from './client'

export const LINK_MODES = ['cut', 'transition', 'tail_frame'] as const
export type LinkMode = (typeof LINK_MODES)[number]

export const LINK_MODE_LABEL: Record<LinkMode, string> = {
  cut: '硬切',
  transition: '转场',
  tail_frame: '续接末帧',
}

export const SEQUENCE_MODES = ['parallel', 'sequential'] as const
export type SequenceMode = (typeof SEQUENCE_MODES)[number]

export const SEQUENCE_MODE_LABEL: Record<SequenceMode, string> = {
  parallel: '各幕并发',
  sequential: '单线程续接',
}

export interface SceneLink {
  id: string
  from_scene_id: string
  to_scene_id: string
  mode: string
  /** transition 生成出来的那个镜头；还没生成时是 null。 */
  shot_id: string | null
  duration: number
  prompt: string | null
  from_index_no: number | null
  from_title: string | null
  to_index_no: number | null
  to_title: string | null
  /** 这种衔接方式的一句话解释，文案在后端写一遍，前端不复制。 */
  hint: string
  created_at: string
  updated_at: string
}

/** 流程图上的一个节点 = 一幕。缩略图取这一幕第一个有成片的镜头。 */
export interface FlowNode {
  id: string
  index_no: number
  title: string
  summary: string | null
  time_of_day: string | null
  location_variant_id: string | null
  shot_count: number
  transition_count: number
  generated_count: number
  duration_total: number
  cast_names: string[]
  cast_count: number
  thumbnail_asset_id: string | null
  thumbnail_path: string | null
  /** 这一幕的上下文问题去重后的清单，就是节点上黄色感叹号的文案。 */
  issues: string[]
}

export interface FlowGraph {
  nodes: FlowNode[]
  links: SceneLink[]
  modes: { name: string; hint: string }[]
  note: string
}

/** 账单里的一幕。`ready_count` 是「真会被入队的条数」，不是镜头总数。 */
export interface PlanScene {
  scene_id: string
  index_no: number
  title: string
  shot_count: number
  ready_count: number
  already_generated: number
  missing: string[]
}

/** 账单里的一条衔接。`configured` 是图上配的，`effective` 是这次真会用的。 */
export interface PlanLink {
  from_scene_id: string
  to_scene_id: string
  from_index_no: number
  to_index_no: number
  configured: string
  effective: string
  hint: string
  will_create_transition: boolean
  duration: number | null
  blocked?: string
}

export interface PlanBlocker {
  scene_id: string
  index_no: number
  shot_id?: string
  why: string
  how: string
}

export interface SequencePlan {
  mode: string
  scenes: PlanScene[]
  links: PlanLink[]
  transitions_to_create: number
  ignored_transitions: number
  total_jobs: number
  blockers: PlanBlocker[]
  notes: string[]
}

/** 一段被补出来的转场。`reused: true` = 已经有成片，这次没重做。 */
export interface TransitionResult {
  link: PlanLink
  shot_id: string
  job_id: string | null
  reused: boolean
  note?: string
}

/** 跳过的每一条都带结构化原因——绝不静默少做一件事。 */
export interface SequenceSkip {
  shot_id?: string
  index_no?: number
  link?: PlanLink
  error: {
    code: string
    title: string
    detail: string
    suggestions: string[]
  }
}

export interface SequenceRun {
  mode: string
  queued: string[]
  transitions: TransitionResult[]
  skipped: SequenceSkip[]
  /** sequential 才有：串出来的那条链，按播放顺序。 */
  chain?: string[]
  plan: SequencePlan
}

export interface LinkBody {
  from_scene_id: string
  to_scene_id: string
  mode: string
  duration?: number | null
  prompt?: string | null
}

export const sequenceApi = {
  graph: (pid: string) => api.get<FlowGraph>(`/projects/${pid}/flow`),
  links: (pid: string) => api.get<SceneLink[]>(`/projects/${pid}/links`),
  /** 同一对场景之间只有一条衔接，所以这是 upsert。 */
  setLink: (pid: string, body: LinkBody) => api.put<SceneLink>(`/projects/${pid}/links`, body),
  deleteLink: (pid: string, linkId: string) => api.del<void>(`/projects/${pid}/links/${linkId}`),

  /** 只出账单，不入队任何任务。 */
  plan: (pid: string, mode: SequenceMode) =>
    api.post<SequencePlan>(`/projects/${pid}/sequence/plan`, { mode }),
  run: (pid: string, mode: SequenceMode, priority = 100) =>
    api.post<SequenceRun>(`/projects/${pid}/sequence/run`, { mode, priority }),
}
