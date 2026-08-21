/**
 * 剧本 / Scene / Shot / 分镜板接口（Step 5）。
 *
 * 字段与后端 app/api/story.py + services/story.py 一一对应。
 *
 * 三个形状上的要点：
 *   1. **AI 只出提案**——`breakdown/propose` 不写库，返回的每条带 `op`，
 *      前端逐条审阅后把整个 `scenes` 数组回传给 `breakdown/apply` 才落库；
 *      把某条标成 `op: 'reject'` 就是「不要它」。
 *   2. **index_no 就是时间顺序**——跨 Scene 移动镜头后由后端统一重排，
 *      前端不自己算序号，移动完拿后端返回的分镜板覆盖本地。
 *   3. Scene 引用的是**地点变体**而不是地点（`location_variant_id`）。
 */

import { api } from './client'

/** LLM 自述状态。`configured === false` 时 AI 入口要说清手动路径同样能走完。 */
export interface LlmStatus {
  configured: boolean
  provider: string
  model: string | null
  hint: string
}

/** 剧本原文。一个工程一份，没有就由后端建一份空的。 */
export interface Story {
  id: string
  title: string
  raw_text: string
  /** manual / ai_assisted / ai_auto —— 这份结构是怎么来的。 */
  mode: string
  llm: LlmStatus
  created_at: string
  updated_at: string
}

export interface Scene {
  id: string
  index_no: number
  title: string
  summary: string | null
  source_text: string | null
  location_variant_id: string | null
  time_of_day: string | null
  notes: string | null
  shot_count: number
  duration_total: number
  /** 「城南旧宅 · 雨夜」，后端拼好的，前端不再自己查地点名。 */
  location_variant_name: string | null
  created_at: string
  updated_at: string
}

/** 镜头创作进度。与任务状态（job.status）是两件事，不要混在一个色系里。 */
export const SHOT_STATUS = ['draft', 'ready', 'generated', 'review', 'locked'] as const
export type ShotStatus = (typeof SHOT_STATUS)[number]

export const SHOT_STATUS_LABEL: Record<ShotStatus, string> = {
  draft: '草稿',
  ready: '可生成',
  generated: '已生成',
  review: '待审',
  locked: '已定稿',
}

export interface ShotCastRow {
  id: string
  shot_id: string
  appearance_id: string
  note: string | null
  appearance_name: string | null
  character_id: string | null
  character_name: string | null
}

export interface ShotPropRow {
  id: string
  shot_id: string
  prop_id: string
  /** present / discarded —— 连续性检查靠它判断「伞什么时候还在」。 */
  state: string
  prop_name: string | null
}

/**
 * 镜头里带出来的生成版本（`GET /shots/{id}` 内嵌的简版）。
 *
 * 这里是 ORM 列的原样投影，`*_json` 还是字符串——镜头编辑器要看展开后的参数与
 * 上下文账单时走 `generationApi.versions`，那条接口会把它们解析好。
 */
export interface ShotVersionRow {
  id: string
  shot_id: string
  version_no: number
  kind: string
  status: string
  asset_id: string | null
  workflow_id: string | null
  params_json: string | null
  context_json: string | null
  error_json: string | null
  duration: number | null
  /** generated / manual —— 手工挂进来的版本也要能看出来。 */
  source: string
  created_at: string
}

export interface Shot {
  id: string
  scene_id: string
  index_no: number
  title: string
  /** shot（导演排的戏）/ transition（两幕之间那段转场，由衔接生成）。 */
  kind: string
  description: string | null
  duration: number
  camera: string | null
  movement: string | null
  status: string
  prompt: string | null
  negative_prompt: string | null
  seed: number | null
  steps: number | null
  workflow_id: string | null
  prev_shot_id: string | null
  current_version_id: string | null
  scene_title: string
  scene_index_no: number
  context_overrides: unknown[]
  cast: ShotCastRow[]
  props: ShotPropRow[]
  version_count: number
  versions: ShotVersionRow[]
  created_at: string
  updated_at: string
}

/** 分镜板卡片。`context_issues` 就是卡片上黄色感叹号的文案来源。 */
export interface StoryboardCard {
  id: string
  index_no: number
  title: string
  /** shot / transition —— 转场是系统按衔接补出来的，卡片上要标出来。 */
  kind: string
  duration: number
  status: string
  camera: string | null
  cast_names: string[]
  thumbnail_asset_id: string | null
  version_count: number
  context_ok: boolean
  context_issues: string[]
}

export interface StoryboardLane {
  id: string
  index_no: number
  title: string
  location_variant_id: string | null
  shots: StoryboardCard[]
}

/** AI 提案里的镜头。`op` 由前端改：`reject` 表示不要它。 */
export interface ProposedShot {
  op: string
  temp_id: string
  title: string
  description: string | null
  duration: number
  camera: string | null
  movement: string | null
  characters: string[]
}

export interface ProposedScene {
  op: string
  temp_id: string
  title: string
  summary: string | null
  time_of_day: string | null
  shots: ProposedShot[]
}

/** 文本里的人名对到已有角色的结果；`none` 表示库里没这个人。 */
export interface CharacterMapping {
  name: string
  match_id: string | null
  match_name: string | null
  confidence: 'exact' | 'fuzzy' | 'none'
}

export interface BreakdownProposal {
  scenes: ProposedScene[]
  scene_count: number
  shot_count: number
  character_mapping: CharacterMapping[]
  note: string
}

export type StoryPatch = Partial<Pick<Story, 'title' | 'raw_text' | 'mode'>>
export type ScenePatch = Partial<
  Pick<Scene, 'title' | 'summary' | 'source_text' | 'location_variant_id' | 'time_of_day' | 'notes'>
>
export type ShotPatch = Partial<
  Pick<
    Shot,
    | 'title'
    | 'description'
    | 'duration'
    | 'camera'
    | 'movement'
    | 'status'
    | 'prompt'
    | 'negative_prompt'
    | 'seed'
    | 'steps'
    | 'workflow_id'
    | 'prev_shot_id'
  >
>

export const storyApi = {
  story: (pid: string) => api.get<Story>(`/projects/${pid}/story`),
  saveStory: (pid: string, patch: StoryPatch) => api.patch<Story>(`/projects/${pid}/story`, patch),

  scenes: (pid: string) => api.get<Scene[]>(`/projects/${pid}/scenes`),
  createScene: (pid: string, patch: ScenePatch) =>
    api.post<Scene>(`/projects/${pid}/scenes`, patch),
  updateScene: (pid: string, sid: string, patch: ScenePatch) =>
    api.patch<Scene>(`/projects/${pid}/scenes/${sid}`, patch),
  deleteScene: (pid: string, sid: string) => api.del<void>(`/projects/${pid}/scenes/${sid}`),
  reorderScenes: (pid: string, order: string[]) =>
    api.put<Scene[]>(`/projects/${pid}/scenes/order`, { order }),

  createShot: (pid: string, sid: string, patch: ShotPatch) =>
    api.post<Shot>(`/projects/${pid}/scenes/${sid}/shots`, patch),
  reorderShots: (pid: string, sid: string, order: string[]) =>
    api.put<StoryboardLane[]>(`/projects/${pid}/scenes/${sid}/shots/order`, { order }),
  shot: (pid: string, shotId: string) => api.get<Shot>(`/projects/${pid}/shots/${shotId}`),
  updateShot: (pid: string, shotId: string, patch: ShotPatch) =>
    api.patch<Shot>(`/projects/${pid}/shots/${shotId}`, patch),
  deleteShot: (pid: string, shotId: string) => api.del<void>(`/projects/${pid}/shots/${shotId}`),
  /** position 是目标 Scene 内 0-based 落点，省略即末尾。返回重排后的整块分镜板。 */
  moveShot: (pid: string, shotId: string, sceneId: string, position?: number) =>
    api.post<StoryboardLane[]>(`/projects/${pid}/shots/${shotId}/move`, {
      scene_id: sceneId,
      position: position ?? null,
    }),
  setShotCast: (pid: string, shotId: string, appearanceIds: string[]) =>
    api.put<Shot>(`/projects/${pid}/shots/${shotId}/cast`, { appearance_ids: appearanceIds }),
  setShotProps: (pid: string, shotId: string, items: { prop_id: string; state: string }[]) =>
    api.put<Shot>(`/projects/${pid}/shots/${shotId}/props`, { items }),

  storyboard: (pid: string) => api.get<StoryboardLane[]>(`/projects/${pid}/storyboard`),

  /** 只出提案，不写库。LLM 未配置时抛 `LLM_UNAVAILABLE`，建议里带手动路径。 */
  propose: (pid: string, text?: string) =>
    api.post<BreakdownProposal>(`/projects/${pid}/breakdown/propose`, { text: text ?? null }),
  apply: (pid: string, scenes: ProposedScene[]) =>
    api.post<{ scenes_created: number; shots_created: number }>(
      `/projects/${pid}/breakdown/apply`,
      { scenes },
    ),
}
