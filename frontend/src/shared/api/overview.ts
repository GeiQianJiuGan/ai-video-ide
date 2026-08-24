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

import { api } from './client'

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
    mode: 'comfy_preset'
    preset_name: string | null
    preset_ready: boolean
    ref_slots: number | null
    /**
     * 参考视频 / 参考音频的槽位数，和 `ref_slots`（参考图）**分开给**：
     * 混成一个数会显示「能收 5 个参考素材」而其中 2 个只吃音频，
     * 用户照着塞图必然白跑一趟。
     */
    ref_video_slots: number | null
    ref_audio_slots: number | null
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
