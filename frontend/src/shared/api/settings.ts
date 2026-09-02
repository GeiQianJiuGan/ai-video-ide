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
export type SettingKind = 'str' | 'int' | 'float' | 'bool' | 'secret' | 'enum' | 'text'

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
  /** 与 `choices` 一一对应的人话标签。空数组表示直接显示 `choices` 里的值。 */
  choice_labels: string[]
  /**
   * 非空表示这一项的取值**可以自动获取**（值就是 `POST /settings/models` 的 `what`）。
   * 页面照它画那个「自动获取」按钮——不在前端硬编码「模型这一项特殊」。
   */
  fetch: string
  /** 这项配错了会导致什么做不出来。后端给的文案，前端不重写一遍。 */
  impact: string
  /**
   * `kind === 'text'` 用：留空时实际生效的那段内置文本（系统提示词的内置默认）。
   * 「恢复内置默认」= 把输入框填回这一段并提交空串——内置文案只有后端一份，
   * 前端绝不抄第二份。
   */
  builtin: string
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

/** `llm.status()` 的形状，与协作栏里那份一致。 */
export interface LlmStatus {
  provider: string
  configured: boolean
  /** 协议的人话名（`不使用（手动模式）` / `Anthropic Claude` …）。 */
  label: string
  model?: string | null
  /** false 表示这个端不支持 function calling，AI 协作会退化成一次性产出提案。 */
  supports_tools: boolean
  /**
   * 这个端能不能**真的看图**。false 时「AI 补全描述」按钮保持 `disabled`，
   * tooltip 里写原因并指向手填那条路——不画假界面。
   */
  supports_vision: boolean
  /** 看图用哪个模型（null = 用主模型）。本机端主模型常常不认图，所以单独一项。 */
  vision_model?: string | null
  hint: string
}

/**
 * 一个 LLM 协议的能力说明。**协议表是后端的唯一真源**：默认地址、要不要密钥、
 * 支不支持工具都从这里读，前端不抄一份「Anthropic 的地址长这样」。
 */
export interface LlmProtocolRow {
  name: string
  label: string
  default_base_url: string
  supports_tools: boolean
  /** 能不能看图。素材描述那条链靠它决定「AI 补全」是不是可用（后端协议表投影过来）。 */
  supports_vision: boolean
  needs_key: boolean
  /** 模型列表从哪来，例如 `GET https://api.openai.com/v1/models`。 */
  models_hint: string
}

/**
 * 一个图片协议的能力说明。与 `LlmProtocolRow` 同一个思路——**协议表是后端的唯一真源**，
 * 加一家出图 API 只改后端那一个 dict，这里与设置页一行都不用动。
 *
 * `wants_preset` 为真的（本机 ComfyUI）认的是模型端保存的那份 T2I 图，所以要另填
 * `image.preset`；`supports_refs` 为假的端收不了参考图，带了也只会降级并说出来。
 */
export interface ImageProtocolRow {
  name: string
  label: string
  default_base_url: string
  needs_key: boolean
  supports_refs: boolean
  wants_preset: boolean
  /** 模型列表从哪来，例如 `GET https://api.openai.com/v1/models`。 */
  models_hint: string
}

/** 一种调用方式。`legacy` 已经恒为 `false`（三条路一视同仁），键留着只为不给一个恒假的布尔改型。 */ export interface ProviderRow {
  name: string
  label: string
  legacy: boolean
}

export interface SettingsSnapshot {
  path: string
  groups: SettingGroup[]
  fields: SettingField[]
  llm: LlmStatus
  llm_protocols: LlmProtocolRow[]
  image_protocols: ImageProtocolRow[]
  providers: ProviderRow[]
}

/** 一个可自动获取的取值。`label` 是给人看的（display_name / 体积），`id` 才是要存的。 */
export interface ModelOption {
  id: string
  label: string
}

/** 「自动获取」的结果。`current_present === false` 是一条真警告：连得上但模型不在。 */
export interface ModelListing {
  provider: string
  label: string
  target: string
  count: number
  items: ModelOption[]
  current: string | null
  current_present: boolean | null
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
  r2v_ready?: boolean
  flf_ready?: boolean
  capabilities?: Array<'r2v' | 'flf' | 'ref_video' | 'ref_audio'>
  impact: string | null
  found?: string[]
  missing_required?: string[]
  node_count?: number
  /** 有没有首帧入口。没有不影响 `ready`，但首帧只能当参考图 1 送进去。 */
  first_frame_ok?: boolean
  /**
   * 这份图能收几张参考图（`AIVS_REF_1…` 标了几个）。
   * **0 不影响 `ready`**——只是角色表 / 地点参考图喂不进去，人物形象只能靠首帧带，
   * 所以页面要把 `ref_hint` 那句话显示出来，而不是只画一个绿点。
   */
  ref_slots: number
  /**
   * 参考视频 / 参考音频的槽位数（`AIVS_REF_VIDEO_*` / `AIVS_REF_AUDIO_*`）。
   * **和 `ref_slots` 分开数**：混成一个总数会显示「能收 5 个参考素材」而其中 2 个
   * 只吃音频，用户照着塞图只会得到一个空跑的任务。**0 是常态**，绝大多数图只收图片，
   * 所以这两个数只在大于 0 时值得画出来。
   */
  ref_video_slots?: number
  ref_audio_slots?: number
  /** 三族各自的槽位数，键是 `image` / `video` / `audio`。 */
  ref_slots_by_media?: Record<string, number>
  ref_hint: string
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
  probe: (what: 'llm' | 'video' | 'image') => api.post<ProbeResult>('/settings/probe', { what }),
  /**
   * 自动获取某一项的候选取值（LLM 模型 / 出图模型）。
   *
   * 协议 / 地址 / 密钥可以带上**还没保存**的那份：让用户先看到模型列表再决定存什么，
   * 而不是先存一份可能是错的配置。后端不会把它们写进 settings.json。
   * `api_key` 留空表示「沿用已保存的那把」——密钥不回明文，所以没敲就别提交。
   */
  models: (
    what: 'llm' | 'image',
    over: { provider?: string; base_url?: string; api_key?: string } = {},
  ) => api.post<ModelListing>('/settings/models', { what, ...over }),
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
