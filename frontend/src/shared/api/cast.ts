/**
 * 角色与形象接口（Step 2）。
 *
 * 字段与后端 app/api/cast.py + services/cast.py 一一对应，不在前端另起别名。
 *
 * 这里最要紧的类型是 `ResolvedField`：派生形象的每个可继承字段，值究竟是自己填的
 * 还是继承来的，后端已经在 `resolve_fields()` 里算好了。前端**不重算继承**——
 * 只按 source 决定怎么画（正常色 / 浅色 + 来源名 / 空占位）。
 */

import { api } from './client'

/** 可继承字段。与后端 persistence/models_cast.py::INHERITABLE 同序同名。 */
export const INHERITABLE = [
  'face',
  'hair',
  'body',
  'traits',
  'costume',
  'state',
  'age',
  'notes',
] as const

export type InheritableField = (typeof INHERITABLE)[number]

export const FIELD_LABEL: Record<InheritableField, string> = {
  face: '脸型',
  hair: '发型',
  body: '体型',
  traits: '特征',
  costume: '服装',
  state: '状态',
  age: '年龄',
  notes: '备注',
}

export interface ResolvedField {
  value: string | null
  /** own = 自己填的；inherited = 来自某个祖先形象；empty = 整条链上都没人填。 */
  source: 'own' | 'inherited' | 'empty'
  from_id: string | null
  from_name: string | null
  /** 派生形象上「本来会继承、但被我改了」。根形象永远是 false。 */
  overridden: boolean
}

export interface SheetVersion {
  id: string
  appearance_id: string
  version_no: number
  /** 可以为 null：占位版本（先记一次生成意图，还没有图）。 */
  asset_id: string | null
  source: string
  is_current: number
  created_at: string
}

export interface Appearance {
  id: string
  character_id: string
  parent_id: string | null
  name: string
  is_default: number
  face: string | null
  hair: string | null
  body: string | null
  traits: string | null
  costume: string | null
  state: string | null
  age: string | null
  notes: string | null
  /** 自己覆写了哪些可继承字段。 */
  overrides: string[]
  fields: Record<InheritableField, ResolvedField>
  created_at: string
  updated_at: string
}

/** 列表接口比详情多两个统计字段。 */
export interface AppearanceRow extends Appearance {
  sheet_count: number
  current_sheet: SheetVersion | null
}

export interface Character {
  id: string
  name: string
  alias: string | null
  gender: string | null
  age_range: string | null
  personality: string | null
  background: string | null
  voice_desc: string | null
  notes: string | null
  /** 从素材库采用而来时的出处。**不是外键**，运行期不解析——库删了照样能打开。 */
  origin_library_id: string | null
  appearance_count: number
  created_at: string
  updated_at: string
}

export type CharacterPatch = Partial<
  Pick<
    Character,
    | 'name'
    | 'alias'
    | 'gender'
    | 'age_range'
    | 'personality'
    | 'background'
    | 'voice_desc'
    | 'notes'
  >
>

export type AppearancePatch = Partial<Record<InheritableField, string | null>> & {
  name?: string
  parent_id?: string | null
  default?: boolean
}

export const CHARACTER_TEXT_FIELDS: { key: keyof CharacterPatch; label: string }[] = [
  { key: 'name', label: '名字' },
  { key: 'alias', label: '别名' },
  { key: 'gender', label: '性别' },
  { key: 'age_range', label: '年龄段' },
  { key: 'personality', label: '性格' },
  { key: 'background', label: '背景' },
  { key: 'voice_desc', label: '声音' },
  { key: 'notes', label: '备注' },
]

export const castApi = {
  characters: (pid: string) => api.get<Character[]>(`/projects/${pid}/characters`),
  createCharacter: (pid: string, patch: CharacterPatch) =>
    api.post<Character>(`/projects/${pid}/characters`, patch),
  updateCharacter: (pid: string, cid: string, patch: CharacterPatch) =>
    api.patch<Character>(`/projects/${pid}/characters/${cid}`, patch),
  deleteCharacter: (pid: string, cid: string) =>
    api.del<void>(`/projects/${pid}/characters/${cid}`),

  appearances: (pid: string, cid: string) =>
    api.get<AppearanceRow[]>(`/projects/${pid}/characters/${cid}/appearances`),
  /** parent_id 留空即根形象；带上就是派生，未填的字段自动继承。 */
  createAppearance: (pid: string, cid: string, patch: AppearancePatch) =>
    api.post<Appearance>(`/projects/${pid}/characters/${cid}/appearances`, patch),
  appearance: (pid: string, aid: string) =>
    api.get<Appearance>(`/projects/${pid}/appearances/${aid}`),
  updateAppearance: (pid: string, aid: string, patch: AppearancePatch) =>
    api.patch<Appearance>(`/projects/${pid}/appearances/${aid}`, patch),
  /** 把某个字段还原成「继承」：清覆写标记 + 清本地值，值重新由父形象决定。 */
  revertField: (pid: string, aid: string, field: InheritableField) =>
    api.post<Appearance>(`/projects/${pid}/appearances/${aid}/revert/${field}`),
  setDefaultAppearance: (pid: string, aid: string) =>
    api.post<Appearance>(`/projects/${pid}/appearances/${aid}/default`),
  deleteAppearance: (pid: string, aid: string) =>
    api.del<void>(`/projects/${pid}/appearances/${aid}`),

  sheets: (pid: string, aid: string) =>
    api.get<SheetVersion[]>(`/projects/${pid}/appearances/${aid}/sheets`),
  /** 只增版本：新版本自动成为当前版本，旧版本保留（硬约束 3）。 */
  addSheet: (pid: string, aid: string, assetId: string, source = 'manual') =>
    api.post<SheetVersion>(`/projects/${pid}/appearances/${aid}/sheets`, {
      asset_id: assetId,
      source,
    }),
}
