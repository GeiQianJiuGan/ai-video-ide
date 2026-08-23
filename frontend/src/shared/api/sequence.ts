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

/**
 * **镜头之间**那条线只有两种。刻意没有 `tail_frame`：镜头级的「续接末帧」早就有
 * 表达方式了（`Shot.prev_shot_id`），再给它一个同义词只会让两处配置打架。
 */
export const SHOT_LINK_MODES = ['cut', 'transition'] as const
export type ShotLinkMode = (typeof SHOT_LINK_MODES)[number]

export const SHOT_LINK_MODE_LABEL: Record<ShotLinkMode, string> = {
  cut: '无转场',
  transition: '转场',
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

/**
 * 两个镜头之间那条线。**没有行就是「无转场」**，所以分镜板上画线不需要先建记录。
 *
 * `shot_id` 是这条线补出来的那个转场镜头；它还没出片时分镜板上要写「转场暂未生成」，
 * 判断依据在 `StoryboardConnector.pending`，不要在界面里各算一遍。
 */
export interface ShotLink {
  id: string
  from_shot_id: string
  to_shot_id: string
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

export interface ShotLinkBody {
  from_shot_id: string
  to_shot_id: string
  mode: string
  duration?: number | null
  prompt?: string | null
}

/**
 * 「一键生成转场」账单里的一条。两级共用一种形状，`level` 区分：
 * `shot` 是镜头之间那条线，`scene` 是幕与幕之间那条。
 *
 * `first_frame` / `last_frame` 说的是这段转场两头的图从哪来
 * （`real_frame` 真帧 / `extract` 生成前会抽一张 / `none` 取不到），界面照它解释
 * 「接缝准不准」。**取不到就不生成**：转场是把上一镜真末帧接到下一镜真首帧，
 * 退回设定图接不上，所以这里没有 `reference_image` 这条路了。
 *
 * `blocked` 是这一条这次做不了的原因（最常见的就是接缝两侧还没都出片），
 * 有它就说明 `will_generate === false`。
 */
export interface TransitionPlanItem {
  level: 'shot' | 'scene'
  link_id: string
  where: string
  from_shot_id?: string
  to_shot_id?: string
  from_scene_id?: string
  to_scene_id?: string
  from_index_no?: number
  to_index_no?: number
  from_title?: string
  to_title?: string
  duration: number | null
  prompt: string | null
  /** 已经补出来的那个转场镜头。 */
  shot_id: string | null
  generated: boolean
  first_frame?: string
  last_frame?: string
  will_generate: boolean
  note?: string
  blocked?: string
}

export interface TransitionPlan {
  items: TransitionPlanItem[]
  /** 这次真会生成的条数（已出片的不算）。 */
  total: number
  /** 已经有成片、这次跳过的条数。 */
  reused: number
  blocked: { link_id: string; why: string; how: string }[]
  notes: string[]
}

/** 一键生成转场里被补出来的一段。`reused: true` = 已经有成片，这次没重做。 */
export interface TransitionMade {
  level?: string
  link_id?: string
  shot_id: string
  job_id: string | null
  reused: boolean
  note?: string
}

export interface TransitionRun {
  transitions: TransitionMade[]
  queued: string[]
  /** 跳过的每一条都带原因——绝不静默少做一件事。 */
  skipped: {
    link_id: string
    where: string
    reason?: string
    error?: { code: string; title: string; detail: string; suggestions: string[] }
  }[]
  plan: TransitionPlan
}

export const sequenceApi = {
  graph: (pid: string) => api.get<FlowGraph>(`/projects/${pid}/flow`),
  links: (pid: string) => api.get<SceneLink[]>(`/projects/${pid}/links`),
  /** 同一对场景之间只有一条衔接，所以这是 upsert。 */
  setLink: (pid: string, body: LinkBody) => api.put<SceneLink>(`/projects/${pid}/links`, body),
  deleteLink: (pid: string, linkId: string) => api.del<void>(`/projects/${pid}/links/${linkId}`),

  shotLinks: (pid: string) => api.get<ShotLink[]>(`/projects/${pid}/shot-links`),
  /** 同一对镜头之间只有一条衔接，所以这也是 upsert。 */
  setShotLink: (pid: string, body: ShotLinkBody) =>
    api.put<ShotLink>(`/projects/${pid}/shot-links`, body),
  deleteShotLink: (pid: string, linkId: string) =>
    api.del<void>(`/projects/${pid}/shot-links/${linkId}`),

  /**
   * 一键生成转场的账单：两级一起列，只读，不抽帧也不入队。
   *
   * **转场要接缝两侧都已经生成过视频才补得出来**（少一头就没有真帧可接），
   * 等成片的那几条 `will_generate === false` 并在 `blocked` 里写明是谁还没生成；
   * 分镜板上那个「生成」能不能点看的是 `StoryboardConnector.can_generate`。
   */
  transitionPlan: (pid: string) =>
    api.post<TransitionPlan>(`/projects/${pid}/sequence/transitions/plan`, {}),
  /**
   * 按账单补转场。`only` 给的是衔接 id（镜头级 / 幕级都认 id），
   * 分镜板上单条转场的「生成」按钮走的就是它；不传就是全部。
   *
   * 接缝两侧没都出片的那几条**一条都不会做**：不传 `only` 时它们进 `plan.blocked`
   * （跳过不是失败），显式指名一条却还没出片时报 `MISSING_INPUT`「转场要等前后都出片」。
   */
  transitionRun: (
    pid: string,
    opts: { only?: string[]; priority?: number; allowRefDrop?: boolean } = {},
  ) =>
    api.post<TransitionRun>(`/projects/${pid}/sequence/transitions/run`, {
      priority: opts.priority ?? 100,
      allow_ref_drop: opts.allowRefDrop ?? false,
      ...(opts.only ? { only: opts.only } : {}),
    }),

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
