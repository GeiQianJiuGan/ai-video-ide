/**
 * 应用级素材库接口（Phase 3/4）。
 *
 * 字段与后端 app/api/library.py + services/library.py 的返回一一对应，不另起别名。
 *
 * 两条边界写在类型里：
 *   1. `/library/*` 路径上没有 pid——素材库是应用级的，同一时刻只打开一个；
 *   2. 采用是「库 → 工程」，所以它挂在 `/projects/{pid}/adopt` 上，
 *      且必须先 `adoptPlan` 看账单再 `adopt` 动手（文件要进用户的工程目录）。
 */

import { api } from './client'

/** 库能收的素材类型。生成物与代理流属于工程，库里不收。 */
export type LibraryKind =
  'character_sheet' | 'location_reference' | 'prop_reference' | 'audio' | 'upload'

export const LIBRARY_KIND_LABEL: Record<LibraryKind, string> = {
  character_sheet: '角色表',
  location_reference: '场景参考',
  prop_reference: '道具图',
  audio: '音频',
  upload: '其它上传',
}

export interface LibraryTag {
  id: string
  name: string
  color: string | null
  created_at: string
}

export interface LibraryAsset {
  id: string
  kind: string
  /** 相对库目录的路径，交给 libraryFileUrl 变成可显示的 URL。 */
  path: string
  mime: string | null
  width: number | null
  height: number | null
  duration: number | null
  size_bytes: number
  sha1: string | null
  source: string
  title: string | null
  note: string | null
  created_at: string
  /** 库内有多少预设在用它；删之前靠它说清会破坏什么。 */
  ref_count: number
  /** 文件被库外的程序删掉了：登记还在，但显示不出来。 */
  missing: boolean
  tags: LibraryTag[]
}

export interface LibraryAppearance {
  id: string
  character_id: string
  parent_id: string | null
  name: string
  is_default: number
  /** 这个形象自己覆写了哪些可继承字段，其余继续继承父形象。 */
  overrides: string[]
  sheet_count: number
  current_sheet: { id: string; asset_id: string; version_no: number } | null
  [field: string]: unknown
}

export interface LibraryCharacter {
  id: string
  name: string
  alias: string | null
  gender: string | null
  age_range: string | null
  personality: string | null
  background: string | null
  notes: string | null
  created_at: string
  tags: LibraryTag[]
  appearances: LibraryAppearance[]
}

export interface LibraryVariant {
  id: string
  location_id: string
  name: string
  time_of_day: string | null
  weather: string | null
  lighting: string | null
  description: string | null
  reference_count: number
}

export interface LibraryLocation {
  id: string
  name: string
  description: string | null
  notes: string | null
  created_at: string
  tags: LibraryTag[]
  variants: LibraryVariant[]
}

export interface LibraryProp {
  id: string
  name: string
  description: string | null
  notes: string | null
  created_at: string
  tags: LibraryTag[]
  reference_count: number
  current_reference: { id: string; asset_id: string; version_no: number } | null
}

export interface LibraryInfo {
  id: string
  name: string
  dir: string
  schema_version: number
  created_at: string
  counts: { assets: number; characters: number; locations: number; props: number; tags: number }
}

/** GET /library：「没配置」不是错误，前端靠 configured 决定画不画引导。 */
export interface LibraryStatus {
  configured: boolean
  remembered_dir: string | null
  library: LibraryInfo | null
}

export type AdoptKind = 'asset' | 'character' | 'location' | 'prop'

export interface AdoptFile {
  library_asset_id: string
  title: string
  size_bytes: number
  /** 库里的文件不见了：这份采用不了，但工程里已有同内容时仍然算数。 */
  missing: boolean
  already_in_project: boolean
}

/** 采用前的账单。文件要进用户的工程目录，代价必须先说清。 */
export interface AdoptPlan {
  kind: AdoptKind
  library_id: string
  label: string
  name: string
  project_dir: string
  files: AdoptFile[]
  copy_count: number
  reuse_count: number
  missing_count: number
  total_bytes: number
  one_way: string
}

export interface AdoptResult {
  kind: AdoptKind
  library_id: string
  label: string
  target_id: string
  name: string
  copied: number
  reused: number
  asset_ids: string[]
  one_way: string
  appearance_ids?: string[]
  variant_ids?: string[]
}

export const libraryApi = {
  status: () => api.get<LibraryStatus>('/library'),
  configure: (dir: string) => api.post<LibraryStatus>('/library/configure', { dir }),
  close: () => api.post<LibraryStatus>('/library/close'),

  assets: (params: { kind?: string; tag?: string } = {}) => {
    const q = new URLSearchParams()
    if (params.kind) q.set('kind', params.kind)
    if (params.tag) q.set('tag', params.tag)
    const suffix = q.toString()
    return api.get<LibraryAsset[]>(`/library/assets${suffix ? `?${suffix}` : ''}`)
  },
  upload: (file: File, kind: LibraryKind, title?: string) =>
    api.upload<LibraryAsset>('/library/assets/upload', file, { kind, title: title ?? '' }),
  patchAsset: (aid: string, patch: { title?: string; note?: string }) =>
    api.patch<LibraryAsset>(`/library/assets/${aid}`, patch),
  deleteAsset: (aid: string, force = false) =>
    api.del<{ id: string; file_removed: boolean }>(
      `/library/assets/${aid}${force ? '?force=true' : ''}`,
    ),

  tags: () => api.get<LibraryTag[]>('/library/tags'),
  createTag: (name: string) => api.post<LibraryTag>('/library/tags', { name }),
  deleteTag: (tid: string) => api.del<void>(`/library/tags/${tid}`),
  attachTag: (tid: string, ownerKind: string, ownerId: string) =>
    api.post<unknown>(`/library/tags/${tid}/attach`, {
      owner_kind: ownerKind,
      owner_id: ownerId,
    }),
  detachTag: (tid: string, ownerKind: string, ownerId: string) =>
    api.post<void>(`/library/tags/${tid}/detach`, { owner_kind: ownerKind, owner_id: ownerId }),

  characters: () => api.get<LibraryCharacter[]>('/library/characters'),
  createCharacter: (patch: { name: string }) =>
    api.post<LibraryCharacter>('/library/characters', patch),
  deleteCharacter: (cid: string) => api.del<void>(`/library/characters/${cid}`),
  addSheet: (aid: string, assetId: string) =>
    api.post<unknown>(`/library/appearances/${aid}/sheets`, { asset_id: assetId }),

  locations: () => api.get<LibraryLocation[]>('/library/locations'),
  createLocation: (patch: { name: string }) =>
    api.post<LibraryLocation>('/library/locations', patch),
  deleteLocation: (lid: string) => api.del<void>(`/library/locations/${lid}`),
  createVariant: (lid: string, patch: { name: string }) =>
    api.post<LibraryVariant>(`/library/locations/${lid}/variants`, patch),
  addVariantReference: (vid: string, assetId: string) =>
    api.post<unknown>(`/library/variants/${vid}/references`, { asset_id: assetId }),

  props: () => api.get<LibraryProp[]>('/library/props'),
  createProp: (patch: { name: string }) => api.post<LibraryProp>('/library/props', patch),
  deleteProp: (pid: string) => api.del<void>(`/library/props/${pid}`),
  addPropReference: (propId: string, assetId: string) =>
    api.post<unknown>(`/library/props/${propId}/references`, { asset_id: assetId }),

  adoptPlan: (pid: string, kind: AdoptKind, libraryId: string) =>
    api.post<AdoptPlan>(`/projects/${pid}/adopt/plan`, { kind, library_id: libraryId }),
  adopt: (pid: string, kind: AdoptKind, libraryId: string) =>
    api.post<AdoptResult>(`/projects/${pid}/adopt`, { kind, library_id: libraryId }),
}

/** 字节数 → 人看的大小。账单里「约 X MB」用它。 */
export function humanBytes(bytes: number): string {
  if (bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`
}
