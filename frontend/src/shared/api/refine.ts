/**
 * 视频二次处理接口（Refine，超分 / 插帧 / 重做）。
 *
 * 对应后端 app/api/refine.py + services/refine.py：
 *   - kinds: 获取二次处理种类及说明；
 *   - plan: 优化账单；
 *   - run: 按账单入队，产出新版本并记录 parent_version_id；
 *   - lineage: 查询版本谱系链（祖先与衍生版本）。
 */

import { api } from './client'
import type { GenerationVersion, Job } from './generation'

export interface RefineKind {
  kind: 'upscale' | 'interpolate' | 'recut'
  label: string
}

export interface RefinePlanItem {
  version_id: string
  version_no: number
  shot_id: string
  shot_index_no: number
  scene_id: string
  kind: string
  duration: number
  asset_id: string | null
}

export interface RefinePlanSkipped {
  target: string
  error: {
    code: string
    title: string
    detail: string
    suggestions: string[]
  }
}

export interface RefinePlanResult {
  kind: string
  kind_label: string
  preset: string | null
  preset_ready: boolean
  preset_detail: string
  items: RefinePlanItem[]
  skipped: RefinePlanSkipped[]
  total: number
  blocked: boolean
  how_to: string[]
}

export interface RefineBody {
  version_ids?: string[]
  shot_ids?: string[]
  scene_id?: string
  kind?: 'upscale' | 'interpolate' | 'recut'
  preset?: string
  seed?: number
  priority?: number
}

export interface VersionLineageResult {
  version_id: string
  ancestors: GenerationVersion[]
  children: GenerationVersion[]
}

export const refineApi = {
  kinds: () => api.get<RefineKind[]>('/refine/kinds'),

  plan: (pid: string, body: RefineBody) =>
    api.post<RefinePlanResult>(`/projects/${pid}/refine/plan`, body),

  run: (pid: string, body: RefineBody) =>
    api.post<Job[]>(`/projects/${pid}/refine/run`, body),

  lineage: (pid: string, versionId: string) =>
    api.get<VersionLineageResult>(`/projects/${pid}/versions/${versionId}/lineage`),
}
