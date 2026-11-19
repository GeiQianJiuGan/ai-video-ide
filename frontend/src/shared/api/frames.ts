import { api } from './client'
import type { ImageJob } from './images'

export type FrameRole = 'first' | 'key' | 'last'
export const FRAME_ROLE_LABEL: Record<FrameRole, string> = {
  first: '计划开场',
  key: '关键状态（叙事参考）',
  last: '预期收尾',
}

export interface FrameCandidate {
  id: string
  asset_id: string
  job_id: string | null
  snapshot: Record<string, unknown>
  continuity?: {
    source_version_id: string
    source_shot_id: string
    at: string | number
    use: string
    stale: boolean
    requires_trim: boolean
  }
}
export interface FrameCard {
  id: string
  title: string
  prompt: string
  effective_prompt?: string
  shared_prompt?: string
  story: string
  role: FrameRole
  position: number
  refs: string[]
  adopted_asset_id: string | null
  candidates: FrameCandidate[]
  jobs: { id: string; status: string; error_json: string | null }[]
}
export interface FrameVideo {
  shared_prompt?: string
  preset?: string
  prompt: string
  duration: number
  first_card_id: string
  last_card_id: string
}
export interface FrameWorkspace {
  cards: FrameCard[]
  video: Partial<FrameVideo>
}
export interface FrameSourceVideo {
  source_version_id: string
  asset_id: string
  path: string
  in_point: number
  out_point: number
  duration: number
}
export interface SharedFramePrompts {
  image_prompt: string
  video_prompt: string
  clear_overrides: boolean
}
export interface FrameNarrativePlan {
  frames: { title: string; role: FrameRole; story: string; prompt: string }[]
  video_prompt: string
  duration: number
  shot_updated_at: string
  frame_card_ids: string[]
}
export interface FramePlan {
  prompt_preview: string
  inputs: { cards: { id: string; title: string; asset_id: string; role: string }[] }
  unused_cards: string[]
}
export interface FrameReferenceMaterial {
  kind: 'character' | 'location' | 'prop'
  id: string
  label: string
  asset_id: string | null
  available: boolean
}
export interface FrameReferenceMaterials {
  groups: Record<FrameReferenceMaterial['kind'], FrameReferenceMaterial[]>
  provider: { configured: boolean; label: string; max_refs: number | null }
}
export function frameVideoDraft(video: Partial<FrameVideo>, duration: number): FrameVideo {
  return { prompt: '', duration, first_card_id: '', last_card_id: '', preset: '', ...video }
}
const base = (pid: string) => `/projects/${pid}`
export const framesApi = {
  generateAllVideos: (pid: string, scope: 'all' | 'unfinished') =>
    api.post<{
      queued: string[]
      batch_id: string
      skipped: { shot_id: string; index_no: number; error: { title: string; detail: string } }[]
    }>(`${base(pid)}/frame-video/generate-all`, { scope }),
  generateAll: (pid: string, scope: 'all' | 'unfinished' = 'unfinished') =>
    api.post<{
      queued: { card_id: string; title: string; job_id: string; warnings: string[] }[]
      skipped: { card_id: string; title: string; reason: string }[]
      failed: {
        card_id: string
        title: string
        error: { title: string; detail: string; suggestions: string[] }
      }[]
    }>(`${base(pid)}/frames/generate-all`, { scope }),
  sourceVideo: (pid: string, sid: string) =>
    api.get<FrameSourceVideo>(`${base(pid)}/shots/${sid}/frame-source`),
  selectSourceFrame: (
    pid: string,
    sid: string,
    body: { source_version_id: string; at: number; card_id: string },
  ) =>
    api.post<{ card_id: string; asset_id: string; path: string; at: number }>(
      `${base(pid)}/shots/${sid}/source-frames`,
      body,
    ),
  sharedPrompts: (pid: string, sceneId: string) =>
    api.get<{ image_prompt: string; video_prompt: string }>(
      `${base(pid)}/scenes/${sceneId}/frame-prompts`,
    ),
  saveSharedPrompts: (
    pid: string,
    sceneId: string,
    body: { image_prompt: string; video_prompt: string; clear_overrides: boolean },
  ) => api.put(`${base(pid)}/scenes/${sceneId}/frame-prompts`, body),
  continuity: (
    pid: string,
    cid: string,
    body: { source_version_id: string; at: 'end' | number; use: 'opening' | 'reference' },
  ) =>
    api.post<{ asset_id: string; message: string }>(
      `${base(pid)}/frame-cards/${cid}/continuity`,
      body,
    ),
  referenceMaterials: (pid: string) =>
    api.get<FrameReferenceMaterials>(`${base(pid)}/frame-reference-materials`),
  proposePlan: (pid: string, sid: string) =>
    api.post<FrameNarrativePlan>(`${base(pid)}/shots/${sid}/frame-plan/propose`, {}),
  applyPlan: (pid: string, sid: string, body: FrameNarrativePlan) =>
    api.post<FrameWorkspace>(`${base(pid)}/shots/${sid}/frame-plan/apply`, body),
  remove: (pid: string, cid: string) => api.del(`${base(pid)}/frame-cards/${cid}`),
  capabilities: (pid: string) =>
    api.get<{
      provider: string
      presets: { name: string; first_frame_ok: boolean; flf_ready: boolean }[]
    }>(`${base(pid)}/frame-capabilities`),
  get: (pid: string, sid: string) => api.get<FrameWorkspace>(`${base(pid)}/shots/${sid}/frames`),
  create: (pid: string, sid: string, title: string) =>
    api.post(`${base(pid)}/shots/${sid}/frames`, { title }),
  update: (
    pid: string,
    cid: string,
    body: Partial<Pick<FrameCard, 'title' | 'prompt' | 'story' | 'role' | 'position' | 'refs'>>,
  ) => api.patch(`${base(pid)}/frame-cards/${cid}`, body),
  candidate: (pid: string, cid: string, asset_id: string) =>
    api.post(`${base(pid)}/frame-cards/${cid}/candidates`, { asset_id }),
  adopt: (pid: string, cid: string, candidate_id: string) =>
    api.post(`${base(pid)}/frame-cards/${cid}/adopt`, { candidate_id }),
  generate: (pid: string, cid: string) =>
    api.post<ImageJob>(`${base(pid)}/frame-cards/${cid}/generate`),
  saveVideo: (pid: string, sid: string, body: FrameVideo) =>
    api.put(`${base(pid)}/shots/${sid}/frame-video`, body),
  optimize: (pid: string, sid: string, body: FrameVideo) =>
    api.post<{ prompt: string }>(`${base(pid)}/shots/${sid}/frame-video/optimize`, body),
  plan: (pid: string, sid: string, body: FrameVideo) =>
    api.post<FramePlan>(`${base(pid)}/shots/${sid}/frame-video/plan`, body),
  video: (pid: string, sid: string, body: FrameVideo) =>
    api.post<{ id: string; kind: string }>(`${base(pid)}/shots/${sid}/frame-video/generate`, body),
}
