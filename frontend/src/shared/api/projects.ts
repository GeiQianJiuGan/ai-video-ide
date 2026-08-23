/**
 * 项目容器接口（M1）。
 *
 * 字段与后端 app/api/projects.py 的 ProjectOut / RecentOut 一一对应，
 * 不在前端另起别名——后端是唯一真源，改字段时两边一起报错比悄悄错更好。
 */

import { api } from './client'

export type DurationUnit = 'frames' | 'seconds'

export interface Project {
  id: string
  name: string
  dir: string
  width: number
  height: number
  fps: number
  aspect_ratio: string
  duration_unit: DurationUnit
  schema_version: number
  /** 打开旧工程时被自动升级的来源版本；没升级就是 null。 */
  migrated_from: number | null
  created_at: string
  updated_at: string
}

export interface ProjectPreset {
  name: string | null
  preset: import('./settings').PresetRow | null
  r2v_name: string | null
  r2v_preset: import('./settings').PresetRow | null
  flf_name: string | null
  flf_preset: import('./settings').PresetRow | null
}

export interface RecentProject {
  id: string
  name: string
  dir: string
  schema_version: number
  opened_at: string
  /** 目录是否还在（被移动或删除时为 false，条目不隐藏，让人能主动忘记它）。 */
  exists: boolean
  is_open: boolean
}

export interface CreateProjectInput {
  dir: string
  name: string
  width: number
  height: number
  fps: number
  duration_unit: DurationUnit
}

export const projectsApi = {
  create: (input: CreateProjectInput) => api.post<Project>('/projects', input),
  open: (dir: string) => api.post<Project>('/projects/open', { dir }),
  recent: () => api.get<RecentProject[]>('/projects/recent'),
  forget: (dir: string) => api.post<void>('/projects/recent/forget', { dir }),
  get: (pid: string) => api.get<Project>(`/projects/${pid}`),
  close: (pid: string) => api.post<void>(`/projects/${pid}/close`),
  preset: (pid: string) => api.get<ProjectPreset>(`/projects/${pid}/preset`),
  setPreset: (pid: string, name: string | null) =>
    api.put<ProjectPreset>(`/projects/${pid}/preset`, { name }),
  setVideoPresets: (pid: string, r2v_name: string | null, flf_name: string | null) =>
    api.put<ProjectPreset>(`/projects/${pid}/preset`, { r2v_name, flf_name }),
}
