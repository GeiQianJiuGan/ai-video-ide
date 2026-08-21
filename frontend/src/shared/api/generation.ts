/**
 * 上下文账单 · 生成版本 · 队列接口（Step 6 / Step 7）。
 *
 * 三件事凑在一个模块里，因为它们围着同一个问题：**这条片段是怎么来的**。
 *
 * 形状上的三个要点：
 *   1. **上下文是一张账单**——`items` 里连「没被采用的」也在，各自带 `reason`。
 *      前端不做筛选、不做重排：后端已经按 priority 排好并算过上限。
 *   2. **版本只增不改**——没有任何 PUT/PATCH 能改写一个已有版本，
 *      只能 `POST /versions/{id}/current` 换当前版本。
 *   3. **入队会被拒**——上下文不完整时抛 `CONTEXT_INCOMPLETE`，
 *      `check_context: false` 才是「我确认无误」的显式跳过。
 */

import { api } from './client'

/** 参考来源的种类，priority 由后端给，这里只用于图标与分组文案。 */
export const CONTEXT_KIND_LABEL: Record<string, string> = {
  character_sheet: '角色表',
  location_reference: '地点参考',
  prev_frame: '上游末帧',
  prop_reference: '道具参考',
  manual: '手动添加',
}

/** 账单里的一条。`included` 为 false 时 `reason` 就是「为什么没用它」。 */
export interface ContextItem {
  key: string
  kind: string
  label: string
  priority: number
  asset_id: string | null
  source_id: string | null
  reason: string
  included: boolean
  /** 手动添加的，或被手动移除的——两种都算人工干预过。 */
  manual: boolean
  asset_path: string | null
  /** 登记过但文件已经不在磁盘上。 */
  missing_file: boolean
}

export interface ContextBill {
  shot_id: string
  items: ContextItem[]
  included_count: number
  limit: number
  at_limit: boolean
  /** false 时 `problems` 就是入队会被拒的理由。 */
  complete: boolean
  problems: string[]
  overrides: unknown[]
  resolved_at: string
}

/** 一个生成版本。`params` / `context` 是当次冻结的取值，不随后续改动变化。 */
export interface GenerationVersion {
  id: string
  shot_id: string
  version_no: number
  kind: string
  status: string
  asset_id: string | null
  workflow_id: string | null
  duration: number | null
  /** generated / manual */
  source: string
  is_current: boolean
  params: Record<string, unknown>
  context: unknown
  error: unknown
  created_at: string
}

export const JOB_STATUS = [
  'queued',
  'waiting',
  'running',
  'done',
  'failed',
  'canceled',
  'paused',
] as const
export type JobStatus = (typeof JOB_STATUS)[number]

export const JOB_STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  waiting: '等上游',
  running: '正在跑',
  done: '完成',
  failed: '失败',
  canceled: '已取消',
  paused: '已暂停',
}

export interface Job {
  id: string
  shot_id: string
  kind: string
  status: string
  priority: number
  progress: number
  depends_on: string | null
  /** 「等待上游 Shot 14 完成（需要末帧）」——等待要能解释，不能只是不动。 */
  wait_reason: string | null
  attempt: number
  workflow_id: string | null
  version_id: string | null
  shot_index_no: number | null
  shot_title: string | null
  params: Record<string, unknown>
  /** 失败现场：结构化错误四要素，直接丢给 ErrorPanel 显示。 */
  error: {
    code: string
    title: string
    detail: string
    suggestions: string[]
    related_ids?: Record<string, string>
  } | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface QueueState {
  paused: boolean
  worker_limit: number
  counts: Record<string, number>
  active: number
  jobs: Job[]
}

export interface EnqueueSceneResult {
  queued: string[]
  /** 被跳过的镜头连结构化理由一起返回——整场生成不能悄悄漏掉几个。 */
  skipped: { shot_id: string; index_no: number; error: Job['error'] }[]
  total: number
}

export const contextApi = {
  bill: (pid: string, shotId: string) =>
    api.get<ContextBill>(`/projects/${pid}/shots/${shotId}/context`),
  /** 移除一条（`key`）/ 手动加一张图（`asset_id`）/ `reset` 恢复自动。 */
  override: (
    pid: string,
    shotId: string,
    body: { action: 'remove' | 'add' | 'reset'; key?: string; asset_id?: string; label?: string },
  ) => api.post<ContextBill>(`/projects/${pid}/shots/${shotId}/context/override`, body),
}

export const generationApi = {
  versions: (pid: string, shotId: string) =>
    api.get<GenerationVersion[]>(`/projects/${pid}/shots/${shotId}/versions`),
  /** 手动导入成片也走版本系统——不生成也能把工程做完。 */
  addVersion: (
    pid: string,
    shotId: string,
    body: { asset_id: string; kind?: string; duration?: number | null },
  ) => api.post<GenerationVersion>(`/projects/${pid}/shots/${shotId}/versions`, body),
  setCurrent: (pid: string, versionId: string) =>
    api.post<GenerationVersion>(`/projects/${pid}/versions/${versionId}/current`),

  enqueueShot: (
    pid: string,
    shotId: string,
    body: {
      kind?: string | null
      priority?: number
      workflow_id?: string | null
      check_context?: boolean
    } = {},
  ) => api.post<Job>(`/projects/${pid}/shots/${shotId}/generate`, body),
  enqueueScene: (pid: string, sceneId: string, priority = 100) =>
    api.post<EnqueueSceneResult>(`/projects/${pid}/scenes/${sceneId}/generate`, { priority }),

  queue: (pid: string) => api.get<QueueState>(`/projects/${pid}/queue`),
  jobs: (pid: string, status?: string) =>
    api.get<Job[]>(`/projects/${pid}/jobs${status ? `?status=${status}` : ''}`),
  pause: (pid: string) => api.post<QueueState>(`/projects/${pid}/queue/pause`),
  resume: (pid: string) => api.post<QueueState>(`/projects/${pid}/queue/resume`),
  retryFailed: (pid: string) => api.post<QueueState>(`/projects/${pid}/queue/retry-failed`),
  cancel: (pid: string, jobId: string) => api.post<Job>(`/projects/${pid}/jobs/${jobId}/cancel`),
  retry: (pid: string, jobId: string) => api.post<Job>(`/projects/${pid}/jobs/${jobId}/retry`),
  setPriority: (pid: string, jobId: string, priority: number) =>
    api.put<Job>(`/projects/${pid}/jobs/${jobId}/priority`, { priority }),
}
