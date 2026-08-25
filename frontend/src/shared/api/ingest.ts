/**
 * 长视频导入与切段接口（Ingest）。
 *
 * 对应后端 app/api/ingest.py + services/ingest.py：
 *   - methods: 获取可用的切段算法列表；
 *   - register: 登记本地长视频成片（复制进工程或原地引用）；
 *   - plan: 只读出账单（切成几段、区间、合并记录、告警）；
 *   - run: 确认落库，创建 kind="ingested" 幕与镜头（零文件复制）。
 */

import { api } from './client'
import type { Asset } from './assets'
import type { Scene, StoryboardCard } from './story'

export interface IngestMethod {
  method: string
  label: string
}

export interface IngestRegisterResult extends Asset {
  duration: number | null
  has_audio: boolean | null
  external: boolean
  warnings: string[]
}

export interface IngestSegment {
  index_no: number
  in_point: number
  out_point: number
  duration: number
  reason: string
}

export interface IngestPlanResult {
  asset_id: string
  path: string
  duration: number | null
  size_mb: number
  method: string
  method_label: string
  threshold: number
  min_segment: number
  max_segment?: number | null
  chunk_seconds: number
  cuts: number[]
  merged_away: number[]
  segments: IngestSegment[]
  total: number
  warnings: string[]
}

export interface IngestPlanParams {
  asset_id: string
  method?: string
  threshold?: number
  min_segment?: number
  max_segment?: number
  chunk_seconds?: number
  cuts?: number[]
}

export interface IngestRunParams extends IngestPlanParams {
  title?: string
  prompt?: string
  param_mode?: 'shared' | 'per_shot'
  position?: number
}

export interface IngestRunResult {
  scene: Scene
  shots: StoryboardCard[]
  position: number
  reordered_after: number
}

export const ingestApi = {
  methods: () => api.get<IngestMethod[]>('/ingest/methods'),

  register: (pid: string, path: string, copyIntoProject = true) =>
    api.post<IngestRegisterResult>(`/projects/${pid}/ingest/register`, {
      path,
      copy_into_project: copyIntoProject,
    }),

  plan: (pid: string, params: IngestPlanParams) =>
    api.post<IngestPlanResult>(`/projects/${pid}/ingest/plan`, params),

  run: (pid: string, params: IngestRunParams) =>
    api.post<IngestRunResult>(`/projects/${pid}/ingest/run`, params),
}
