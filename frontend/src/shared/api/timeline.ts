/**
 * 时间线与导出接口（Step 8）。
 *
 * 这一整组**完全不依赖 AI**：ComfyUI / LLM 全都不在，装配、剪辑、导出照样能走完
 * （硬约束 2）。它只引用 `GenerationVersion`，不生产任何素材。
 *
 * 形状上的三个要点：
 *   1. **多数编辑命令直接回整条时间线**——后端会重排 index_no 与 ripple 位置，
 *      前端拿返回值整体覆盖，不在本地算偏移。
 *   2. **撤销栈在进程里**（`can_undo` / `can_redo` 由后端给），重启应用后会清空。
 *      所以按钮的可用性只信后端这两个字段，不自己记。
 *   3. **导出先给命令再执行**——`GET /export/command` 是预检，把将要跑的 FFmpeg 参数
 *      原样摆出来；`POST /export` 才真的起进程。
 */

import { api } from './client'

export const TRACK_KIND_LABEL: Record<string, string> = {
  video: '视频轨',
  audio: '音频轨',
  subtitle: '字幕轨',
}

export const TRANSITION_KINDS = ['cut', 'dissolve', 'fade_in', 'fade_out'] as const
export type TransitionKind = (typeof TRANSITION_KINDS)[number]

export const TRANSITION_LABEL: Record<string, string> = {
  cut: '硬切',
  dissolve: '叠化',
  fade_in: '淡入',
  fade_out: '淡出',
}

/** 轨道上的一个片段。`start` / `duration` 单位是秒。 */
export interface Clip {
  id: string
  track_id: string
  shot_id: string | null
  version_id: string | null
  asset_id: string | null
  index_no: number
  start: number
  duration: number
  in_point: number
  out_point: number | null
  label: string | null
  /** 0/1（SQLite 没有 bool）。视频片段静音表示「声音已经不从这里出」。 */
  muted: number
  /** 音量倍数，1 是原样。上限 4，但**预览器最高只能到 1**（浏览器的限制）。 */
  volume: number
  /** 这段声音是从哪个视频片段拆出来的（导入的配乐是 null）。 */
  source_clip_id: string | null
  shot_index_no: number | null
  version_no: number | null
  asset_path: string | null
  /** 所属轨道的类型，省得每次回头去 tracks 里找。 */
  track_kind: string | null
  /** 这段画面的声音被拆到了哪个片段上（没拆过是 null）。 */
  detached_audio_clip_id: string | null
  /** 拆声音的那段画面已经不在了：声音照旧能播，但对不上任何画面。 */
  source_missing: boolean
  /** 登记过但文件已经不在磁盘上——导出前必须先处理。 */
  missing_file: boolean
}

export interface Track {
  id: string
  timeline_id: string
  kind: string
  index_no: number
  name: string
  muted: number
  locked: number
  clips: Clip[]
}

export interface Timeline {
  id: string
  name: string
  fps: number
  width: number
  height: number
  tracks: Track[]
  duration_total: number
  /** 撤销栈在进程内，重启后为 false。 */
  can_undo: boolean
  can_redo: boolean
  created_at: string
  updated_at: string
}

/** 自动装配结果。被跳过的镜头逐条列出来——「铺了 8 个」里漏掉的 2 个才是要处理的。 */
export interface AssembleResult {
  placed: string[]
  skipped: { shot_id: string; index_no: number; reason: string }[]
  timeline: Timeline
}

export interface Transition {
  id: string
  timeline_id: string
  from_clip_id: string | null
  to_clip_id: string | null
  kind: string
  duration: number
}

export interface ExportRecord {
  id: string
  timeline_id: string
  path: string
  status: string
  version_ids: string[]
  command: string | null
  error: {
    code: string
    title: string
    detail: string
    suggestions: string[]
  } | null
  duration: number | null
  created_at: string
  finished_at: string | null
}

/** 导出预检：将要执行的命令原样给人看，不藏。 */
export interface ExportPlan {
  path: string
  /** 后端已经拼成一整行（`build_command` 里 `" ".join(args)`），照原样显示，不要再 join。 */
  command: string
  clips: number
  /** 参与混音的音频轨片段数（视频片段自带的声音不算在这里）。 */
  audio_clips: number
  /**
   * 「会被丢掉 / 说不准」的那些事：静音的音频轨、比画面长的声音、会被合掉的空档、
   * ffprobe 不在所以不知道某段画面有没有声音。空数组是常态，有内容就必须显示。
   */
  warnings: string[]
}

/** 拆声音的结果。新开了轨道 / 复用了已有文件都要说出来。 */
export interface DetachResult {
  audio_clip_id: string
  track_id: string
  track_name: string
  /** 为了放下它新开了一条音频轨（原来的在这个时间段都占着）。 */
  created_track: boolean
  /** 之前已经拆过同一段素材，直接复用那份音频（没重跑 FFmpeg）。 */
  reused_file: boolean
  asset_id: string
  timeline: Timeline
}

export interface TrackResult {
  track: Track
  timeline: Timeline
}

export interface AddClipResult {
  clip_id: string
  timeline: Timeline
}

/** 裁切请求。**拖左边缘时 `in_point` 与 `start` 必须一起给**：一次请求、一格撤销。 */
export interface TrimBody {
  in_point?: number | null
  out_point?: number | null
  start?: number | null
  ripple?: boolean
}

export const timelineApi = {
  get: (pid: string) => api.get<Timeline>(`/projects/${pid}/timeline`),
  /** `replace=true` 清空视频轨后重铺；false 追加。 */
  assemble: (pid: string, replace = true) =>
    api.post<AssembleResult>(`/projects/${pid}/timeline/assemble`, { replace }),
  undo: (pid: string) => api.post<Timeline>(`/projects/${pid}/timeline/undo`),
  redo: (pid: string) => api.post<Timeline>(`/projects/${pid}/timeline/redo`),

  move: (pid: string, clipId: string, start: number) =>
    api.post<Timeline>(`/projects/${pid}/clips/${clipId}/move`, { start }),
  trim: (pid: string, clipId: string, body: TrimBody) =>
    api.post<Timeline>(`/projects/${pid}/clips/${clipId}/trim`, body),
  split: (pid: string, clipId: string, at: number) =>
    api.post<Timeline>(`/projects/${pid}/clips/${clipId}/split`, { at }),
  remove: (pid: string, clipId: string, ripple = true) =>
    api.del<Timeline>(`/projects/${pid}/clips/${clipId}?ripple=${ripple}`),
  /** 只换这一个片段的版本，整条线不重排。 */
  replaceVersion: (pid: string, clipId: string, versionId: string) =>
    api.post<Timeline>(`/projects/${pid}/clips/${clipId}/version`, { version_id: versionId }),
  /** 静音 / 音量。视频片段与音频片段同一个入口。 */
  setMix: (pid: string, clipId: string, body: { muted?: boolean; volume?: number }) =>
    api.post<Timeline>(`/projects/${pid}/clips/${clipId}/mix`, body),
  /** 把这段画面的声音拆成音频轨上的独立片段（源片段随之静音）。 */
  detachAudio: (pid: string, clipId: string) =>
    api.post<DetachResult>(`/projects/${pid}/clips/${clipId}/detach-audio`),

  addTrack: (pid: string, kind: string, name?: string | null) =>
    api.post<TrackResult>(`/projects/${pid}/tracks`, { kind, name: name ?? null }),
  patchTrack: (
    pid: string,
    trackId: string,
    body: { name?: string; muted?: boolean; locked?: boolean },
  ) => api.patch<Timeline>(`/projects/${pid}/tracks/${trackId}`, body),
  /** 轨道上还有片段时后端先回 CONFLICT + `confirm: "force"`；确认后 `force=true` 重放。 */
  removeTrack: (pid: string, trackId: string, force = false) =>
    api.del<Timeline>(`/projects/${pid}/tracks/${trackId}?force=${force}`),
  /** 把一个已登记的资产放到轨道上（导入的配乐 / 音效走这里）。 */
  addClip: (
    pid: string,
    trackId: string,
    body: { asset_id: string; start?: number; duration?: number | null; label?: string | null },
  ) => api.post<AddClipResult>(`/projects/${pid}/tracks/${trackId}/clips`, body),

  transitions: (pid: string) => api.get<Transition[]>(`/projects/${pid}/transitions`),
  addTransition: (
    pid: string,
    body: { from_clip_id: string; to_clip_id: string; kind?: string; duration?: number },
  ) => api.post<Transition>(`/projects/${pid}/transitions`, body),
  removeTransition: (pid: string, tid: string) =>
    api.del<void>(`/projects/${pid}/transitions/${tid}`),

  exports: (pid: string) => api.get<ExportRecord[]>(`/projects/${pid}/exports`),
  exportCommand: (pid: string) => api.get<ExportPlan>(`/projects/${pid}/export/command`),
  /** `path` 留空则写进工程 `generations/exports/`。 */
  export: (pid: string, path?: string | null) =>
    api.post<ExportRecord>(`/projects/${pid}/export`, { path: path ?? null }),
}
