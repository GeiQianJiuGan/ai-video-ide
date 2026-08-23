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

/**
 * 一幕里的人物小节点。名字由后端拼好（`label` = 角色 · 形象），前端不再查两张表。
 */
export interface SceneCastRow {
  id: string
  appearance_id: string
  index_no: number
  appearance_name: string | null
  character_id: string | null
  character_name: string | null
  label: string
  /**
   * 当前角色表那张图，**相对工程目录**的路径（过 `fileUrl(pid, path)` 才是 URL）。
   * 只会是图片；没有角色表、或那一版挂的不是图时是 null，界面显示「无图」。
   */
  thumbnail_path: string | null
}

/** 一幕里的地点小节点。`is_primary` 那条同步着 `scene.location_variant_id`。 */
export interface SceneLocationRow {
  id: string
  location_variant_id: string
  index_no: number
  variant_name: string | null
  label: string
  is_primary: boolean
  /** 变体的参考图，同 `SceneCastRow.thumbnail_path` 的口径。 */
  thumbnail_path: string | null
}

export interface Scene {
  id: string
  index_no: number
  title: string
  summary: string | null
  source_text: string | null
  /** 这一幕的提示词。小节点里**唯一必填**的那个；镜头自己写了 prompt 时以镜头为准。 */
  prompt: string | null
  location_variant_id: string | null
  time_of_day: string | null
  notes: string | null
  shot_count: number
  duration_total: number
  /** 「城南旧宅 · 雨夜」，后端拼好的，前端不再自己查地点名。 */
  location_variant_name: string | null
  /** 小节点：人物与地点都可以是空的，但各自不能超过 `node_limit`。 */
  cast: SceneCastRow[]
  locations: SceneLocationRow[]
  cast_names: string[]
  /** prompt 填了没有。false 就是节点上那个黄标。 */
  prompt_ok: boolean
  /** 人物 / 地点各自的上限，运行期可配（设置页 `scene.node_limit`）。 */
  node_limit: number
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
  /**
   * 卡片上那张图，**相对工程目录**的路径（过 `fileUrl(pid, path)` 才是 URL）。
   * 后端保证它**只会是图片**：优先是从成片里抽出来的真首帧，其次是该镜头生成的图片版本。
   * 以前这里给的是当前版本的资产 id，而当前版本几乎总是 `.mp4`——那就是「分镜里
   * 截取的首帧加载失败」的来源。
   */
  thumbnail_path: string | null
  thumbnail_asset_id: string | null
  /** 能播的那一段（`<video>` 才用它，绝不喂给 `<img>`）。 */
  video_path: string | null
  video_asset_id: string | null
  video_version_id: string | null
  /** 该 Shot 的已生成视频版本，分镜板直接展示并支持采纳。 */
  versions: {
    id: string
    version_no: number
    kind: string
    status: string
    asset_id: string | null
    video_path: string | null
    thumbnail_path: string | null
    duration: number | null
    source: string
    is_current: boolean
    created_at: string
  }[]
  /** 有片子但还没有能当图显示的那一张：调 `extractPosters` 补抽，不是错误。 */
  poster_pending: boolean
  version_count: number
  context_ok: boolean
  context_issues: string[]
}

/**
 * 分镜板上两张卡片之间那条线。镜头之间（`level: 'shot'`）与幕之间（`level: 'scene'`）
 * 是同一种形状，界面上也是同一条线。
 *
 * 四件事只有这一个来源：
 *   - **没配过就是无转场**——`id === null` / `mode === 'cut'`，后端连行都没有；
 *   - `pending` 就是「配了转场但还没出片」，分镜板上那行「转场暂未生成」照它显示，
 *     **不要**在界面里用 `transition_shot_id` 再算一遍（镜头造出来了但任务还在排队时，
 *     它仍然是「暂未生成」）；
 *   - `can_generate` 才是那个「生成」按钮能不能点：转场要**接缝两侧都已经生成过视频**
 *     才补得出来（否则两头都对不上），拦下来的原因在 `blocked`，下一步动作在
 *     `blocked_how`——按钮灰着却不说为什么，和静默失败一样糟；
 *   - `transition_shot_id` 指的那个镜头**照旧在 `shots` 里**（导出顺序、补首帧、
 *     时间线装配都靠它在那儿），前端把它从卡片行里拿出来画在线上而已。
 */
export interface StoryboardConnector {
  /** 后端还没有这条记录时是 null（= 无转场）。 */
  id: string | null
  level: 'shot' | 'scene'
  /** cut 无转场 / transition 补一段转场。 */
  mode: string
  duration: number | null
  prompt: string | null
  transition_shot_id: string | null
  /** 那段转场已经有成片了。 */
  generated: boolean
  /** 配了转场却还没出片——「转场暂未生成」那行字的唯一依据。 */
  pending: boolean
  /** 现在还不能生成的原因（谁还没出片）；能生成时是 null。 */
  blocked: string | null
  /** 被拦下来时的下一步动作，文案在后端写一遍。 */
  blocked_how: string | null
  /** = `pending && !blocked`。「生成」按钮的可点状态只看它。 */
  can_generate: boolean
  from_shot_id?: string
  to_shot_id?: string
  from_scene_id?: string
  to_scene_id?: string
  from_index_no?: number
  to_index_no?: number
  to_title?: string
}

export interface StoryboardLane {
  id: string
  index_no: number
  title: string
  location_variant_id: string | null
  shots: StoryboardCard[]
  /** 本幕内相邻两个正片镜头之间那条线，按顺序，比卡片数少一条。 */
  links: StoryboardConnector[]
  /** 本幕到下一幕那条线；最后一幕是 null。 */
  next_link: StoryboardConnector | null
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
  appearance_ids?: string[]
  prompt?: string | null
  negative_prompt?: string | null
}

export interface ProposedScene {
  op: string
  temp_id: string
  title: string
  summary: string | null
  time_of_day: string | null
  source_text?: string | null
  location?: string | null
  location_variant?: string | null
  prompt?: string | null
  negative_prompt?: string | null
  characters?: string[]
  appearance_ids?: string[]
  location_variant_id?: string | null
  location_variant_ids?: string[]
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

/**
 * 补首帧的结果。
 *
 * 抽帧是写操作，所以它不在 `GET /storyboard` 里顺手做——读一次分镜板绝不会
 * 起 FFmpeg 进程。单条失败不打断其余，每条都带完整四要素。
 */
export interface PosterResult {
  requested: number
  extracted: { shot_id: string; asset_id: string; path: string; reused: boolean }[]
  failed: {
    shot_id: string
    title: string
    error: { code: string; title: string; detail: string; suggestions: string[] }
  }[]
}

export type StoryPatch = Partial<Pick<Story, 'title' | 'raw_text' | 'mode'>>
export type ScenePatch = Partial<
  Pick<
    Scene,
    'title' | 'summary' | 'source_text' | 'prompt' | 'location_variant_id' | 'time_of_day' | 'notes'
  >
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
  /** 单幕（含小节点）。流程图节点里已经带了同样的字段，这条是详情页用的。 */
  scene: (pid: string, sid: string) => api.get<Scene>(`/projects/${pid}/scenes/${sid}`),
  createScene: (pid: string, patch: ScenePatch) =>
    api.post<Scene>(`/projects/${pid}/scenes`, patch),
  /** 后端按 `exclude_none` 收 patch：要**清空**某个字段得传 `''`，传 `null` 等于没改。 */
  updateScene: (pid: string, sid: string, patch: ScenePatch) =>
    api.patch<Scene>(`/projects/${pid}/scenes/${sid}`, patch),
  deleteScene: (pid: string, sid: string) => api.del<void>(`/projects/${pid}/scenes/${sid}`),
  reorderScenes: (pid: string, order: string[]) =>
    api.put<Scene[]>(`/projects/${pid}/scenes/order`, { order }),
  /** 这一幕的人物小节点。可以是空数组；超过 `node_limit` 由后端拒绝并说明改哪里。 */
  setSceneCast: (pid: string, sid: string, appearanceIds: string[]) =>
    api.put<Scene>(`/projects/${pid}/scenes/${sid}/cast`, { appearance_ids: appearanceIds }),
  /** 这一幕的地点小节点。**第一条同时是主地点**，所以顺序有意义。 */
  setSceneLocations: (pid: string, sid: string, variantIds: string[]) =>
    api.put<Scene>(`/projects/${pid}/scenes/${sid}/locations`, {
      location_variant_ids: variantIds,
    }),

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
  /** 给 `poster_pending` 的卡片补抽首帧。不传 shotIds 就是「全部补上」。 */
  extractPosters: (pid: string, shotIds?: string[]) =>
    api.post<PosterResult>(`/projects/${pid}/storyboard/posters`, {
      shot_ids: shotIds ?? null,
    }),

  /** 只出提案，不写库。LLM 未配置时抛 `LLM_UNAVAILABLE`，建议里带手动路径。 */
  propose: (pid: string, text?: string) =>
    api.post<BreakdownProposal>(`/projects/${pid}/breakdown/propose`, { text: text ?? null }),
  apply: (pid: string, scenes: ProposedScene[]) =>
    api.post<{ scenes_created: number; shots_created: number }>(
      `/projects/${pid}/breakdown/apply`,
      { scenes },
    ),
}
