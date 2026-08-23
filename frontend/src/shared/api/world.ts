/**
 * 地点、变体与道具接口（Step 3）。
 *
 * 字段与后端 app/api/world.py + services/world.py 一一对应。
 *
 * 两个形状上的要点：
 *   1. 参考图挂在**变体**上而不是地点上——「城南旧宅 · 雨夜」和「· 白天」各有一套图；
 *   2. 删除能不能做由后端一处决定（仍被 Scene / Shot 引用时拒绝并说清是谁在用），
 *      前端不做二次判断，只负责把拒绝理由与 suggestions 显示出来。
 */

import { api } from './client'

export interface LocationVariant {
  id: string
  location_id: string
  name: string
  time_of_day: string | null
  weather: string | null
  lighting: string | null
  description: string | null
  /** 有多少个 Scene 用着这个变体。删之前的影响范围。 */
  scene_count: number
  created_at: string
  updated_at: string
}

export interface Location {
  id: string
  name: string
  description: string | null
  notes: string | null
  /** 从素材库采用而来时的出处。不是外键，运行期不解析。 */
  origin_library_id: string | null
  variants: LocationVariant[]
  created_at: string
  updated_at: string
}

export interface LocationReference {
  id: string
  variant_id: string
  asset_id: string
  camera: string | null
  note: string | null
  is_current: number
  created_at: string
}

export interface PropReference {
  id: string
  prop_id: string
  asset_id: string
  version_no: number
  note: string | null
  is_current: number
  created_at: string
}

export interface Prop {
  id: string
  name: string
  description: string | null
  notes: string | null
  origin_library_id: string | null
  reference_count: number
  current_reference: PropReference | null
  /** 出现在多少个 Shot 里。 */
  shot_count: number
  created_at: string
  updated_at: string
}

/** 变体被哪些 Scene 用着——「被 N 个 Scene 引用」背后的可点列表。 */
export interface VariantUsage {
  id: string
  title: string
  index_no: number
}

export type LocationPatch = Partial<Pick<Location, 'name' | 'description' | 'notes'>>
export type LocationCreatePatch = LocationPatch & { default_asset_id: string }
export type VariantPatch = Partial<
  Pick<LocationVariant, 'name' | 'time_of_day' | 'weather' | 'lighting' | 'description'>
>
export type PropPatch = Partial<Pick<Prop, 'name' | 'description' | 'notes'>>
export type PropCreatePatch = PropPatch & { default_asset_id: string }

export const VARIANT_TEXT_FIELDS: { key: keyof VariantPatch; label: string; hint: string }[] = [
  { key: 'name', label: '变体名', hint: '雨夜' },
  { key: 'time_of_day', label: '时间', hint: '夜晚' },
  { key: 'weather', label: '天气', hint: '大雨' },
  { key: 'lighting', label: '光线', hint: '仅有窗内暖光，街面反光' },
  { key: 'description', label: '描述', hint: '' },
]

export const worldApi = {
  locations: (pid: string) => api.get<Location[]>(`/projects/${pid}/locations`),
  createLocation: (pid: string, patch: LocationCreatePatch) =>
    api.post<Location>(`/projects/${pid}/locations`, patch),
  updateLocation: (pid: string, lid: string, patch: LocationPatch) =>
    api.patch<Location>(`/projects/${pid}/locations/${lid}`, patch),
  deleteLocation: (pid: string, lid: string) => api.del<void>(`/projects/${pid}/locations/${lid}`),

  createVariant: (pid: string, lid: string, patch: VariantPatch) =>
    api.post<LocationVariant>(`/projects/${pid}/locations/${lid}/variants`, patch),
  updateVariant: (pid: string, vid: string, patch: VariantPatch) =>
    api.patch<LocationVariant>(`/projects/${pid}/variants/${vid}`, patch),
  deleteVariant: (pid: string, vid: string) => api.del<void>(`/projects/${pid}/variants/${vid}`),
  variantUsage: (pid: string, vid: string) =>
    api.get<VariantUsage[]>(`/projects/${pid}/variants/${vid}/usage`),
  variantReferences: (pid: string, vid: string) =>
    api.get<LocationReference[]>(`/projects/${pid}/variants/${vid}/references`),
  addVariantReference: (pid: string, vid: string, assetId: string, camera?: string) =>
    api.post<LocationReference>(`/projects/${pid}/variants/${vid}/references`, {
      asset_id: assetId,
      camera: camera || null,
    }),

  props: (pid: string) => api.get<Prop[]>(`/projects/${pid}/props`),
  createProp: (pid: string, patch: PropCreatePatch) => api.post<Prop>(`/projects/${pid}/props`, patch),
  updateProp: (pid: string, propId: string, patch: PropPatch) =>
    api.patch<Prop>(`/projects/${pid}/props/${propId}`, patch),
  deleteProp: (pid: string, propId: string) => api.del<void>(`/projects/${pid}/props/${propId}`),
  propReferences: (pid: string, propId: string) =>
    api.get<PropReference[]>(`/projects/${pid}/props/${propId}/references`),
  /** 只增版本：新版本自动成为当前版本，旧版本保留。 */
  addPropReference: (pid: string, propId: string, assetId: string, note?: string) =>
    api.post<PropReference>(`/projects/${pid}/props/${propId}/references`, {
      asset_id: assetId,
      note: note || null,
    }),
}
