/**
 * 应用级设置接口（Step 1）。
 *
 * 这一层是配置页的唯一数据来源。两个形状上的要点，页面必须照着做：
 *
 *   1. **每个字段带 `source`**（`file` / `env` / `default`）——「你看到的这个值是从哪来的」
 *      在排查时是唯一有用的信息，所以要显示出来，而不是只画一个输入框。
 *   2. **密钥永不回明文**：secret 字段的 `value` 恒为 `null`，只有 `masked` 与 `has_value`。
 *      因此输入框**只在用户真的敲了东西时才提交**；提交空串表示清除。
 *
 * 提交 `null` 表示清除这项覆盖，回到环境变量或代码默认——不是「把它设成空」。
 */

import { api } from './client'

export type SettingSource = 'file' | 'env' | 'default'
export type SettingKind = 'str' | 'int' | 'float' | 'bool' | 'secret' | 'enum'

export const SOURCE_LABEL: Record<SettingSource, string> = {
  file: '来自配置文件',
  env: '来自环境变量',
  default: '默认值',
}

export interface SettingField {
  key: string
  group: string
  label: string
  kind: SettingKind
  choices: string[]
  /** 这项配错了会导致什么做不出来。后端给的文案，前端不重写一遍。 */
  impact: string
  source: SettingSource
  /** secret 字段恒为 null。 */
  value: string | number | boolean | null
  masked: string | null
  has_value: boolean | null
}

export interface SettingGroup {
  id: string
  title: string
}

/** `llm.status()` 的形状，与状态栏里那份一致。 */
export interface LlmStatus {
  provider: string
  configured: boolean
  detail: string
  model?: string
}

/** 一种调用方式。`legacy` 的是旧的 Workflow 绑定路径，只作兼容保留。 */
export interface ProviderRow {
  name: string
  label: string
  legacy: boolean
}

export interface SettingsSnapshot {
  path: string
  groups: SettingGroup[]
  fields: SettingField[]
  llm: LlmStatus
  providers: ProviderRow[]
}

/** 「测试连接」成功时的形状。失败是 ApiError，页面照常显示 suggestions。 */
export interface ProbeResult {
  ok: boolean
  target: string
  detail: string
  [extra: string]: unknown
}

/** 一份 ComfyUI 预设的体检报告。`ready=false` 的不隐藏，写清 `impact`。 */
export interface PresetRow {
  name: string
  path: string
  ready: boolean
  impact: string | null
  found?: string[]
  missing_required?: string[]
  node_count?: number
}

export interface PresetListing {
  dir: string
  items: PresetRow[]
  /** 「怎么把图改成本工具认的样子」——后端给的步骤，直接显示。 */
  how_to: string[]
}

export type SettingsPatch = Record<string, string | number | boolean | null>

export const settingsApi = {
  get: () => api.get<SettingsSnapshot>('/settings'),
  patch: (patch: SettingsPatch) => api.patch<SettingsSnapshot>('/settings', patch),
  probe: (what: 'llm' | 'video') => api.post<ProbeResult>('/settings/probe', { what }),
  presets: () => api.get<PresetListing>('/settings/presets'),
  savePreset: (name: string, graph: string) =>
    api.post<PresetRow>('/settings/presets', { name, graph }),
  /** `name` 走查询串：后端那个参数是查询参数，不是 form 字段。留空则用文件名。 */
  uploadPreset: (file: File, name?: string) =>
    api.upload<PresetRow>(
      name
        ? `/settings/presets/upload?name=${encodeURIComponent(name)}`
        : '/settings/presets/upload',
      file,
    ),
  removePreset: (name: string) => api.del<void>(`/settings/presets/${encodeURIComponent(name)}`),
}
