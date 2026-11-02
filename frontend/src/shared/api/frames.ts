import { api } from './client'

export interface FrameCandidate {
  id: string
  asset_id: string
  job_id: string | null
  snapshot: Record<string, unknown>
}
export interface FrameCard {
  id: string
  title: string
  prompt: string
  position: number
  refs: string[]
  adopted_asset_id: string | null
  candidates: FrameCandidate[]
  jobs: { id: string; status: string; error_json: string | null }[]
}
export interface FrameVideo {
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
export interface FramePlan {
  inputs: { cards: { id: string; title: string; asset_id: string; role: string }[] }
  unused_cards: string[]
}
const base = (pid: string) => `/projects/${pid}`
export const framesApi = {
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
    body: Partial<Pick<FrameCard, 'title' | 'prompt' | 'position' | 'refs'>>,
  ) => api.patch(`${base(pid)}/frame-cards/${cid}`, body),
  candidate: (pid: string, cid: string, asset_id: string) =>
    api.post(`${base(pid)}/frame-cards/${cid}/candidates`, { asset_id }),
  adopt: (pid: string, cid: string, candidate_id: string) =>
    api.post(`${base(pid)}/frame-cards/${cid}/adopt`, { candidate_id }),
  generate: (pid: string, cid: string) => api.post(`${base(pid)}/frame-cards/${cid}/generate`),
  saveVideo: (pid: string, sid: string, body: FrameVideo) =>
    api.put(`${base(pid)}/shots/${sid}/frame-video`, body),
  plan: (pid: string, sid: string, body: FrameVideo) =>
    api.post<FramePlan>(`${base(pid)}/shots/${sid}/frame-video/plan`, body),
  video: (pid: string, sid: string, body: FrameVideo) =>
    api.post(`${base(pid)}/shots/${sid}/frame-video/generate`, body),
}
