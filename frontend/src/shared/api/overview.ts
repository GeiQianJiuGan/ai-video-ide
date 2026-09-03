/**
 * 项目概览与连续性检查接口（Step 9）。
 *
 * 字段与后端 app/api/overview.py + services/overview.py 一一对应。
 *
 * 两条口径必须由后端决定，前端不重造：
 *   1. 镜头状态的中文名（`shot_status[].label`）——后端 STATUS_LABEL 是唯一真源；
 *   2. 连续性问题的文案与建议——检查只报事实与坐标，不自动改数据，
 *      所以 title / detail / suggestions 原样显示，判断权留给导演。
 */

import { api, type ErrorPayload } from './client'
import type { RouteSource } from './projects'

export interface OverviewCounts {
  scenes: number
  shots: number
  characters: number
  appearances: number
  character_sheets: number
  locations: number
  props: number
  versions: number
  timeline_clips: number
  exports: number
}

export interface ShotStatusBucket {
  status: string
  /** 后端给的中文名，前端直接用。 */
  label: string
  count: number
}

/** 「继续上次工作」指向最近改动过的镜头，而不是一个笼统的入口。 */
export interface ResumePointer {
  shot_id: string
  index_no: number
  title: string | null
  status: string
  status_label: string
  scene_id: string | null
  scene_title: string | null
  updated_at: string
}

export interface OverviewSummary {
  project: Record<string, unknown>
  counts: OverviewCounts
  shot_status: ShotStatusBucket[]
  progress: { generated: number; total: number; percent: number }
  duration_total: number
  queue: { active: number; failed: number }
  resume: ResumePointer | null
  last_export: { id: string; path: string; status: string; created_at: string } | null
}

export interface ActivityItem {
  at: string | null
  /** version / job_failed / job_canceled / export_* */
  kind: string
  text: string
  shot_id: string | null
}

export interface ContinuityIssue {
  kind: string
  severity: 'error' | 'warning' | 'info'
  shot_id: string | null
  shot_index_no?: number
  scene_id?: string
  title: string
  detail: string
  suggestions: string[]
}

export interface ContinuityReport {
  issues: ContinuityIssue[]
  counts: { error: number; warning: number; info: number }
  clean: boolean
}

export interface CapabilityRow {
  capability: string
  ready: boolean
  workflow_count: number
  ready_count: number
  default_workflow_id: string | null
  default_workflow_name: string | null
  required_slots: string[]
  /** 这条能力缺失会导致什么做不出来；ready 时为 null。 */
  impact: string | null
}

export interface EnvironmentStatus {
  comfy: { online: boolean; base_url: string; detail: string }
  ffmpeg: {
    available: boolean
    path: string
    /** 用的是哪一份：`bundled` 内置 / `path` 系统 PATH / `configured` 配置指定。 */
    source: string
    detail: string
    impact: string | null
    /** 缺失时怎么拿到内置副本；可用时是空串。 */
    hint: string
  }
  gpu: {
    available: boolean
    name?: string | null
    vram_total_mb?: number
    vram_free_mb?: number
    detail: string
  }
  capabilities: { capabilities: CapabilityRow[]; comfy: unknown } | null
  generation: {
    /**
     * 最终走哪条路（`comfy_preset` / `http_api` / `comfy_workflow`）。**不写成字面量联合**：
     * 名字来自后端的适配器注册表，加一条路时前端不该跟着改型（硬约束 1）。
     */
    mode: string
    /** 这条路的中文名，由后端给（`registry.LABELS`），前端不写第二份。 */
    mode_label: string
    /** 这个答案是谁给的：工程显式选了 / 跟随设置页 / 谁都没选过。 */
    mode_source: RouteSource
    /** 界面上那句「这条路不需要工作流绑定」只看这一个布尔。 */
    binds_workflow: boolean
    /** 绑定那条路上普通镜头绑的是哪份图；其余两条路是 null。 */
    workflow_name: string | null
    /** 这条路的服务在不在（「测试连接」那一下）。**它不回答「绑没绑上」**，那半句在 `issues` 里。 */
    service: { ok: boolean; target: string; detail: string; error: ErrorPayload | null }
    /** 两条能力（普通镜头 / 衔接与转场）各自 ready 才算这个工程能出片。 */
    ready: boolean
    /** 缺什么。四要素形状，suggestions 原样显示（硬约束 4）。 */
    issues: ErrorPayload[]
    /** 预设那条路才有值：走 REST / 工作流绑定时是 null，**不是「未绑定预设」**。 */
    preset_name: string | null
    /** 说的是**这条路**能不能出片，不是「有没有选预设」。 */
    preset_ready: boolean
    ref_slots: number | null
    /**
     * 参考视频 / 参考音频的槽位数，和 `ref_slots`（参考图）**分开给**：
     * 混成一个数会显示「能收 5 个参考素材」而其中 2 个只吃音频，
     * 用户照着塞图必然白跑一趟。
     */
    ref_video_slots: number | null
    ref_audio_slots: number | null
    /** 这几个数是哪份图 / 哪条合同给的。 */
    ref_detail: string | null
    r2v_name: string | null
    r2v_ready: boolean
    r2v_ref_slots: number | null
    r2v_ref_video_slots: number | null
    r2v_ref_audio_slots: number | null
    flf_name: string | null
    flf_ready: boolean
    detail: string
  } | null
}

export const CAPABILITY_LABEL: Record<string, string> = {
  text2image: '文生图',
  image2video: '图生视频',
  first_last_frame: '首尾帧',
  upscale: '超分',
}

/** counts 里哪些值值得摆在概览上，以及点进去落到哪个功能页。 */
export const COUNT_CARDS: { key: keyof OverviewCounts; label: string; route: string | null }[] = [
  { key: 'scenes', label: 'Scene', route: 'story' },
  { key: 'shots', label: 'Shot', route: 'storyboard' },
  { key: 'characters', label: '角色', route: 'characters' },
  { key: 'locations', label: '地点', route: 'locations' },
  { key: 'props', label: '道具', route: 'props' },
  { key: 'versions', label: '生成版本', route: 'queue' },
  { key: 'timeline_clips', label: '时间线片段', route: 'timeline' },
  { key: 'exports', label: '导出', route: 'timeline' },
]

export const overviewApi = {
  summary: (pid: string) => api.get<OverviewSummary>(`/projects/${pid}/overview`),
  activity: (pid: string, limit = 10) =>
    api.get<ActivityItem[]>(`/projects/${pid}/overview/activity?limit=${limit}`),
  continuity: (pid: string) => api.get<ContinuityReport>(`/projects/${pid}/overview/continuity`),
  environment: (pid: string) => api.get<EnvironmentStatus>(`/projects/${pid}/overview/environment`),
}
