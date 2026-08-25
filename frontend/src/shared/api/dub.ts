/**
 * 音频重构与配音接口（Dub）。
 *
 * 对应后端 app/api/dub.py + services/dub.py：
 *   - plan: 配音账单（单镜头 / 批量镜头 / 整幕）；
 *   - run: 按账单入队生成，产出 kind="audio" 的版本并自动成为当前配音；
 *   - importAudio: 导入外部音频作为镜头版本；
 *   - audioVersions: 镜头的所有音频版本及采用状态；
 *   - mute: 取消采用独立音频（恢复原画面自带声音）。
 */

import { api } from './client'
import type { Asset } from './assets'
import type { GenerationVersion, Job } from './generation'

export interface DubPlanItem {
  shot_id: string
  shot_index_no: number
  scene_id: string
  text: string
  prompt: string
  duration: number
  source_version_id: string | null
  video_missing: boolean
  replaces_version_id: string | null
}

export interface DubPlanSkipped {
  target: string
  error: {
    code: string
    title: string
    detail: string
    suggestions: string[]
  }
}

export interface DubPlanResult {
  provider: string
  provider_label: string
  configured: boolean
  preset: string | null
  preset_ready: boolean
  preset_detail: string
  voice_ref_asset_id: string | null
  with_video: boolean
  items: DubPlanItem[]
  skipped: DubPlanSkipped[]
  total: number
  blocked: boolean
  how_to: string[]
}

export interface DubBody {
  shot_ids?: string[]
  scene_id?: string
  text?: string
  prompt?: string
  negative?: string
  voice_ref_asset_id?: string
  with_video?: boolean
  preset?: string
  seed?: number
  priority?: number
}

export interface AudioVersionItem extends GenerationVersion {
  audio_path: string | null
}

export interface AudioVersionsResult {
  shot_id: string
  current_audio_version_id: string | null
  items: AudioVersionItem[]
}

export interface ImportAudioResult {
  asset: Asset
  version: GenerationVersion
  adopted: boolean
}

export const dubApi = {
  plan: (pid: string, body: DubBody) =>
    api.post<DubPlanResult>(`/projects/${pid}/dub/plan`, body),

  run: (pid: string, body: DubBody) =>
    api.post<Job[]>(`/projects/${pid}/dub/run`, body),

  importAudio: (pid: string, shotId: string, path: string, adopt = true) =>
    api.post<ImportAudioResult>(`/projects/${pid}/shots/${shotId}/audio/import`, {
      path,
      adopt,
    }),

  audioVersions: (pid: string, shotId: string) =>
    api.get<AudioVersionsResult>(`/projects/${pid}/shots/${shotId}/audio-versions`),

  mute: (pid: string, shotId: string) =>
    api.del<{ shot_id: string; current_audio_version_id: null }>(
      `/projects/${pid}/shots/${shotId}/audio-current`,
    ),
}
