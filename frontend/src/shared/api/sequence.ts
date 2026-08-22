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
import type { SceneCastRow, SceneLocationRow } from './story'

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

/**
 * 流程图上的一个节点 = 一幕，本身就是一张小图表：
 * 中间是这一幕能播的那一段（没出片时 `has_video === false`，显示「暂无已生成视频」），
 * 周围挂着小节点——prompt（必填的那个）、人物、地点。
 *
 * 两个字段千万不要混用：`video_path` 是**能播的那一段**（`<video>`），
 * `thumbnail_path` 只会是图片（`<img>`）。把 `.mp4` 喂给 `<img>` 就是之前那个坏图。
 *
 * 幕上**没有**「主视频」这种东西：采用哪一段是每个镜头自己的事
 * （`ShotVideoGroup.adopted_version_id`），节点上播的只是按镜头顺序挑出来的一段。
 */
export interface FlowNode {
  id: string
  index_no: number
  title: string
  summary: string | null
  time_of_day: string | null
  location_variant_id: string | null
  location_variant_name: string | null
  shot_count: number
  transition_count: number
  generated_count: number
  duration_total: number
  cast_names: string[]
  cast_count: number
  /** 这一幕的提示词。小节点里唯一必填的那个。 */
  prompt: string | null
  prompt_ok: boolean
  cast: SceneCastRow[]
  locations: SceneLocationRow[]
  /** 人物 / 地点各自的上限，运行期可配（设置页 `scene.node_limit`）。 */
  node_limit: number
  video_version_id: string | null
  video_asset_id: string | null
  /** 播的这一段属于哪个镜头。采用是镜头级的，所以要知道去哪个镜头改。 */
  video_shot_id: string | null
  video_path: string | null
  video_duration: number | null
  /** 播的这一段就是所属镜头采用了的那一版（否则只是自动挑的一段）。 */
  video_adopted: boolean
  /** 这一幕一共有几段可播的视频，就是「按镜头选一段采用」列表的总长度。 */
  video_count: number
  has_video: boolean
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

/**
 * 某个镜头生成过的一段视频（「按镜头选一段采用」列表里的一行）。
 *
 * `omitted` 里的行形状一样，只是多一条 `reason`——不能当候选也要说清为什么，
 * 列表空着不给理由，用户只会以为功能坏了。
 */
export interface ShotVideoCard {
  id: string
  shot_id: string
  version_no: number
  status: string
  /** generated / manual —— 手工挂进来的版本也要能看出来。 */
  source: string
  duration: number | null
  asset_id: string | null
  asset_path: string | null
  /** 这一版就是该镜头采用了的那一段（= `Shot.current_version_id`）。 */
  is_adopted: boolean
  created_at: string
  reason?: string
}

/**
 * 一幕里的一个镜头，以及它自己的那一批候选。
 *
 * **采用是镜头级的**：一幕下面有很多镜头，每个镜头各自独立生成很多段视频，
 * 「用哪一段」只能一个镜头一个镜头地定。它就是 `adopted_version_id`
 * （后端的 `Shot.current_version_id`），时间线装配认的也是它。
 */
export interface ShotVideoGroup {
  shot_id: string
  index_no: number
  title: string
  /** shot（导演排的戏）/ transition（衔接补出来的那段）。 */
  kind: string
  adopted_version_id: string | null
  items: ShotVideoCard[]
  omitted: ShotVideoCard[]
}

export interface SceneVideos {
  scene_id: string
  title: string
  shots: ShotVideoGroup[]
  /** 这一幕所有镜头的候选总数。 */
  total: number
  /** 已经采用了成片的镜头数。 */
  adopted_count: number
  note: string
}

export const sequenceApi = {
  graph: (pid: string) => api.get<FlowGraph>(`/projects/${pid}/flow`),
  links: (pid: string) => api.get<SceneLink[]>(`/projects/${pid}/links`),
  /** 同一对场景之间只有一条衔接，所以这是 upsert。 */
  setLink: (pid: string, body: LinkBody) => api.put<SceneLink>(`/projects/${pid}/links`, body),
  deleteLink: (pid: string, linkId: string) => api.del<void>(`/projects/${pid}/links/${linkId}`),

  /**
   * 这一幕**按镜头分组**的视频候选；不能当候选的在 `omitted` 里带原因。
   *
   * 这里只列候选。采用走 `versionsApi.setCurrent`（`POST /versions/{id}/current`）——
   * 全工程只有那一个「用哪一段」的入口，刻意不在这一层再开第二个。
   */
  sceneVideos: (pid: string, sid: string) =>
    api.get<SceneVideos>(`/projects/${pid}/scenes/${sid}/videos`),

  /** 只出账单，不入队任何任务。 */
  plan: (pid: string, mode: SequenceMode) =>
    api.post<SequencePlan>(`/projects/${pid}/sequence/plan`, { mode }),
  run: (pid: string, mode: SequenceMode, priority = 100) =>
    api.post<SequenceRun>(`/projects/${pid}/sequence/run`, { mode, priority }),
}
