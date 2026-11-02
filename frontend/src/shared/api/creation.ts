import { api } from './client'
import type { DirectorAttachment } from './director'
import type { ProductionMode } from '@/app/productionNavigation'

export type CreationGoal = 'screenplay' | 'storyboard'

export interface CreationTask {
  id: string
  status: 'running' | 'applying' | 'paused' | 'failed' | 'review' | 'completed' | 'discarded'
  state: {
    goal: CreationGoal
    mode: ProductionMode
    message: string
    error?: string
    draft: { screenplay_md: string } | null
    outlines: { title: string }[] | null
    scenes: {
      title: string
      shots: { title: string; description: string; frames: { title: string; prompt: string }[] }[]
    }[]
    applied: unknown[]
  }
}
const path = (pid: string) => `/projects/${pid}/director/tasks`
export const creationApi = {
  latest: (pid: string) => api.get<CreationTask | null>(`${path(pid)}/latest`),
  start: (
    pid: string,
    message: string,
    goal: CreationGoal,
    mode: ProductionMode,
    attachments: DirectorAttachment[],
  ) => api.post<CreationTask>(path(pid), { message, goal, mode, attachments }),
  control: (pid: string, rid: string, action: 'pause' | 'resume' | 'apply' | 'discard') =>
    api.post<CreationTask>(`${path(pid)}/${rid}/${action}`),
}
