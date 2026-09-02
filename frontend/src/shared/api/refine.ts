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
import type { RouteBinds, RouteSource } from './projects'

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
  /**
   * 走哪条路。**界面照 `binds` 决定要不要显示「处理预设」那一行**：走 REST 时这个工程
   * 根本没有预设这回事，写「默认视频预设」是在说一个不存在的东西；绑定那条路做不了
   * 二次处理，那句话由 `preset_detail` + `how_to` 说清（硬约束 1、4）。
   */
  route: {
    provider: string
    label: string
    /** `project` 工程显式选了 / `settings` 跟随设置页 / `default` 谁都没选过。 */
    source: RouteSource
    /** `preset` / `base_url` / `workflow`，未知调用方式是空串。 */
    binds: RouteBinds
  }
  items: RefinePlanItem[]
  skipped: RefinePlanSkipped[]
  total: number
  blocked: boolean
  /** 这条路做不了时的出路（四要素里那几条建议）。**必须原样显示**（硬约束 4）。 */
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
