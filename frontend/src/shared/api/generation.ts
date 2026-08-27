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
  first_frame: '首帧槽位',
  last_frame: '末帧槽位',
  character_sheet: '角色表',
  location_reference: '地点参考',
  prev_frame: '上游末帧',
  prop_reference: '道具参考',
  manual: '手动添加',
}

/**
 * 采用的条目在生成时**充当什么**。规则只在后端（`services/context.py::_assign_roles`），
 * 前端只负责把它标出来——没被采用的是空串。
 *
 * **首尾帧和参考素材是两件事**：首 / 末帧决定「画面从哪一格开始 / 结束」（走
 * `AIVS_FIRST_FRAME` / `AIVS_LAST_FRAME`，不占参考槽位），参考素材决定「谁出场、在哪儿、
 * 什么动作、什么声音」（走 `AIVS_REF_*`）。首帧**只认镜头上那个显式槽位**或上游镜头的
 * 真末帧，角色表 / 地点图一张都不会被提拔成首帧。
 */
export const CONTEXT_ROLE_LABEL: Record<string, string> = {
  first_frame: '首帧',
  last_frame: '末帧',
  reference: '参考素材',
}

/**
 * 参考素材的媒体族。`other` 是「认不出后缀」，后端不会采用它（`reason` 里写清了）。
 * 每一族进各自的槽位（`AIVS_REF_*` / `AIVS_REF_VIDEO_*` / `AIVS_REF_AUDIO_*`），
 * 所以「装不下」也是各族分开算的。
 */
export const CONTEXT_MEDIA_LABEL: Record<string, string> = {
  image: '参考图',
  video: '参考视频',
  audio: '参考音频',
  other: '未知类型',
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
  /**
   * `first_frame` / `last_frame` / `reference`，没被采用时是空串。
   * 旧版本冻结的账单里可能没有这个字段。
   */
  role?: string
  /**
   * `image` / `video` / `audio` / `other`——只看后缀（后端 `assets.kind_of_suffix`）。
   * 界面照它决定用 `<img>` 还是 `<video>` / `<audio>`，也照它分组标「装不下」。
   * 旧账单里没有这个字段，按 `image` 读。
   */
  media?: string
  /** 手动添加的，或被手动移除的——两种都算人工干预过。 */
  manual: boolean
  /**
   * 采用了、但模型端那份图收不下它（提交时会按槽位顺序被挤掉）。
   * 「装不下」和「没采用」是两件事，界面上必须分开标。
   */
  over_capacity?: boolean
  asset_path: string | null
  /** 登记过但文件已经不在磁盘上。 */
  missing_file: boolean
}

/** 某一族参考素材的槽位账。三族各算一遍，混着数会把「音频装不下」算没了。 */
export interface ContextCapacityMedia {
  /** 参考图 / 参考视频 / 参考音频。 */
  label: string
  /** null = 不限张数；0 是有意义的答案（这一族槽位一个都没标）。 */
  limit: number | null
  ref_count: number
  dropped: number
  dropped_labels: string[]
  over: boolean
}

/**
 * 这一次模型端能收几个参考素材，以及会不会有素材喂不进去。
 *
 * **不是应用级设置**：ComfyUI 预设数自己标了几个 `AIVS_REF_*`，通用 REST 合同不限张数，
 * 没选预设时也不限（`limit === null`）。`limit === 0` 是有意义的答案——那份图一张参考图
 * 都收不了，人物形象只能靠首帧带。
 *
 * 顶层那几个字段说的是**参考图**（历史口径）；视频 / 音频看 `media` 子块，
 * `over` 是「任意一族装不下」。
 */
export interface ContextCapacity {
  /** null = 不限张数。 */
  limit: number | null
  /** 这个数字哪来的（预设名 / 「REST 合同」）。 */
  source: string
  /** 为什么是这个上限，直接显示给用户。 */
  detail: string
  /** 账单里算作「参考图」的条数（首 / 末帧那两张不占槽位）。 */
  ref_count: number
  /** 会喂不进去几张。 */
  dropped: number
  /** 会被挤掉的是哪几张（账单末尾、优先级最低的那几条）。 */
  dropped_labels: string[]
  /** true 时生成前会先要一次确认（`REF_OVER_CAPACITY`）。 */
  over: boolean
  /** 三族各自的账，键是 `image` / `video` / `audio`。旧账单里可能没有。 */
  media?: Record<string, ContextCapacityMedia>
}

export interface ContextBill {
  shot_id: string
  items: ContextItem[]
  included_count: number
  /** 以前这里是应用级上限 `limit` / `at_limit`，现在换成这一整块。 */
  capacity: ContextCapacity
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
  /** generated / manual / imported / upscaled ... */
  source: string
  parent_version_id?: string | null
  in_point?: number | null
  out_point?: number | null
  is_current: boolean
  params: Record<string, unknown>
  context: unknown
  error: unknown
  created_at: string
  /**
   * 能播的那一段（后端保证是视频）。**和 `thumbnail_path` 绝不混用**：
   * 把 `.mp4` 塞进 `<img>` 只会得到一个坏图标——版本轨上那个坏图就是这么来的。
   */
  video_path?: string | null
  /** 能当图显示的那一张（版本本身是图片，或这段视频抽出来的首帧）。 */
  thumbnail_path?: string | null
  /** 针对 kind="audio" 的音频版本，相对工程目录的音频路径。 */
  audio_path?: string | null
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
  /**
   * 这条任务属于哪一次编排（「单线程续接」/「并发生成」/「整幕配音」…）。
   * **空是常态**——单个镜头的生成不属于任何一批，任务框里照旧一行一条。
   */
  batch_id: string | null
  batch_label: string | null
  batch_kind: string | null
  /** 在这一批里排第几（1 起）。合并那条任务靠它说「执行到第 3/12 步」。 */
  batch_seq: number | null
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

/**
 * 一次编排在任务框里合并成的那一条。**后端纯算出来的**，没有 batch 表——
 * 总数、走到第几步、失败在哪一条全部由成员任务推出来，前端不再自己拼一遍。
 */
export interface JobBatch {
  id: string
  /** 入队那一刻定死的名字（「单线程续接 · 12 个镜头」）。 */
  label: string
  /** sequential / parallel / scene / transition / dub / refine，只用于文案与图标。 */
  kind: string
  total: number
  counts: Record<string, number>
  /** 这一批的聚合状态：running / queued / failed / canceled / done。 */
  status: string
  /** 已经了结的条数（含失败与取消——它们不会再动了）。 */
  settled: number
  /** 正在做第几步（1 起）；跑完之后停在 total 上，不回到 0。 */
  step: number
  running_job_id: string | null
  running_label: string | null
  error: Job['error']
  failed_count: number
  /** 有失败 / 已取消的成员才能整批重跑（已完成的一条都不重做）。 */
  retryable: boolean
  job_ids: string[]
  created_at: string
  finished_at: string | null
}

export interface QueueState {
  paused: boolean
  worker_limit: number
  counts: Record<string, number>
  active: number
  jobs: Job[]
  /** 合并视图：一次编排一条。空数组表示这个工程里只有零散的单条任务。 */
  batches: JobBatch[]
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
      /**
       * 「参考图装不下也继续」。默认 false：后端先回 `REF_OVER_CAPACITY` 说明会丢几张，
       * 用户确认后带上 true 再调一次同一个入口（`related_ids.confirm` 就是这个参数名）。
       */
      allow_ref_drop?: boolean
    } = {},
  ) => api.post<Job>(`/projects/${pid}/shots/${shotId}/generate`, body),
  enqueueScene: (pid: string, sceneId: string, priority = 100, allowRefDrop = false) =>
    api.post<EnqueueSceneResult>(`/projects/${pid}/scenes/${sceneId}/generate`, {
      priority,
      allow_ref_drop: allowRefDrop,
    }),

  queue: (pid: string) => api.get<QueueState>(`/projects/${pid}/queue`),
  jobs: (pid: string, status?: string) =>
    api.get<Job[]>(`/projects/${pid}/jobs${status ? `?status=${status}` : ''}`),
  pause: (pid: string) => api.post<QueueState>(`/projects/${pid}/queue/pause`),
  resume: (pid: string) => api.post<QueueState>(`/projects/${pid}/queue/resume`),
  retryFailed: (pid: string) => api.post<QueueState>(`/projects/${pid}/queue/retry-failed`),
  cancelAll: (pid: string) =>
    api.post<{ cancelled: string[]; count: number }>(`/projects/${pid}/queue/cancel-all`),
  clearFailed: (pid: string) =>
    api.post<{ cleared: number }>(`/projects/${pid}/queue/clear-failed`),
  cancel: (pid: string, jobId: string) => api.post<Job>(`/projects/${pid}/jobs/${jobId}/cancel`),
  retry: (pid: string, jobId: string) => api.post<Job>(`/projects/${pid}/jobs/${jobId}/retry`),
  /** 整批重跑：单线程一条失败会连带停掉后面全部，重跑必须是一次动作。 */
  retryBatch: (pid: string, batchId: string) =>
    api.post<{ batch_id: string; retried: string[]; count: number }>(
      `/projects/${pid}/queue/batches/${batchId}/retry`,
    ),
  /** 整批取消：这一批里还没了结的成员一起停。 */
  cancelBatch: (pid: string, batchId: string) =>
    api.post<{ batch_id: string; cancelled: string[]; count: number }>(
      `/projects/${pid}/queue/batches/${batchId}/cancel`,
    ),
  deleteJob: (pid: string, jobId: string) =>
    api.del<{ deleted: string }>(`/projects/${pid}/jobs/${jobId}`),
  setPriority: (pid: string, jobId: string, priority: number) =>
    api.put<Job>(`/projects/${pid}/jobs/${jobId}/priority`, { priority }),
}
