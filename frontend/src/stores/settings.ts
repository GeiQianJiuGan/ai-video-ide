/**
 * 设置 store（Step 1）。
 *
 * 与其它 store 同构（`busy` + `lastError` + 动作后重拉），但有三处专门的取舍：
 *
 *   1. **草稿与已保存分开**：输入框绑在 `draft` 上，只有「保存」时才把**改过的键**
 *      提交给后端。密钥字段的草稿初始为空串，空串意味着「没动」——因为后端根本不回明文，
 *      不这样做会在第一次保存时把用户的密钥清掉。
 *   2. **探测失败不是异常流程**：连不上 LLM / 视频服务是常态（服务没起、地址写错），
 *      所以两块各存一份 `probe` 结果或错误，页面把 suggestions 显示出来，而不是一个红叉。
 *   3. **自动获取用的是草稿里那份配置**：先取模型再保存，比「先存一份可能是错的配置」顺。
 *      这些覆盖只随那一次请求走，后端不落盘（见 `settingsApi.models`）。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  settingsApi,
  type ImageProtocolRow,
  type LlmProtocolRow,
  type ModelListing,
  type PresetListing,
  type ProbeResult,
  type SettingField,
  type SettingsPatch,
  type SettingsSnapshot,
} from '@/shared/api/settings'

/** 一块（LLM / 视频）的探测状态。 */
export interface ProbeState {
  busy: boolean
  result: ProbeResult | null
  error: ApiError | null
}

/** 「自动获取」的状态。`key` 记的是给哪一项取的，页面只在那一项下面展开列表。 */
export interface FetchState {
  key: string
  busy: boolean
  listing: ModelListing | null
  error: ApiError | null
}

type DraftValue = string | number | boolean

function emptyProbe(): ProbeState {
  return { busy: false, result: null, error: null }
}

/** 字段 → 草稿初值。secret 一律空串：空串表示「没动过」。 */
function draftOf(field: SettingField): DraftValue {
  if (field.kind === 'secret') return ''
  if (field.kind === 'bool') return Boolean(field.value)
  return field.value === null || field.value === undefined ? '' : (field.value as DraftValue)
}

export const useSettingsStore = defineStore('settings', () => {
  const snapshot = ref<SettingsSnapshot | null>(null)
  const presets = ref<PresetListing | null>(null)
  const draft = ref<Record<string, DraftValue>>({})

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)
  const savedAt = ref('')
  const probes = ref<Record<'llm' | 'video' | 'image', ProbeState>>({
    llm: emptyProbe(),
    video: emptyProbe(),
    image: emptyProbe(),
  })
  const fetched = ref<FetchState>({ key: '', busy: false, listing: null, error: null })

  const fields = computed(() => snapshot.value?.fields ?? [])
  const groups = computed(() => snapshot.value?.groups ?? [])
  const providers = computed(() => snapshot.value?.providers ?? [])
  const llm = computed(() => snapshot.value?.llm ?? null)
  const llmProtocols = computed(() => snapshot.value?.llm_protocols ?? [])
  const imageProtocols = computed(() => snapshot.value?.image_protocols ?? [])
  const path = computed(() => snapshot.value?.path ?? '')

  /** 草稿里选中的那个协议的能力说明（默认地址 / 要不要密钥 / 支不支持工具）。 */
  const draftProtocol = computed<LlmProtocolRow | null>(() => {
    const name = String(draft.value['llm.provider'] ?? '')
    return llmProtocols.value.find((p) => p.name === name) ?? null
  })

  /**
   * 出图那一族的同一件事。协议表是后端的唯一真源，所以这里只是「按名字查一行」——
   * 加一家出图 API 时这一段与设置页都不用改。
   */
  const draftImageProtocol = computed<ImageProtocolRow | null>(() => {
    const name = String(draft.value['image.provider'] ?? '')
    return imageProtocols.value.find((p) => p.name === name) ?? null
  })

  const byKey = computed<Record<string, SettingField>>(() =>
    Object.fromEntries(fields.value.map((f) => [f.key, f])),
  )

  function fieldsOf(group: string): SettingField[] {
    return fields.value.filter((f) => f.group === group)
  }

  /** 这一项的草稿与已保存值是否不同——决定「保存」提交哪些键、UI 上标不标「未保存」。 */
  function isDirty(key: string): boolean {
    const field = byKey.value[key]
    if (!field) return false
    const value = draft.value[key]
    if (field.kind === 'secret') return String(value ?? '') !== ''
    if (field.kind === 'bool') return Boolean(value) !== Boolean(field.value)
    return String(value ?? '') !== String(field.value ?? '')
  }

  const dirtyKeys = computed(() => Object.keys(draft.value).filter(isDirty))
  const dirty = computed(() => dirtyKeys.value.length > 0)

  function resetDraft(): void {
    draft.value = Object.fromEntries(fields.value.map((f) => [f.key, draftOf(f)]))
  }

  function absorb(next: SettingsSnapshot): void {
    snapshot.value = next
    resetDraft()
  }

  function clearError(): void {
    lastError.value = null
  }

  async function guarded<T>(run: () => Promise<T>): Promise<T> {
    busy.value = true
    try {
      const out = await run()
      lastError.value = null
      return out
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
      throw err
    } finally {
      busy.value = false
    }
  }

  async function load(): Promise<void> {
    await guarded(async () => {
      absorb(await settingsApi.get())
      await loadPresets()
    })
  }

  /** 预设列表拿不到不该拖垮整页：它只影响「默认预设」那一栏。 */
  async function loadPresets(): Promise<void> {
    try {
      presets.value = await settingsApi.presets()
    } catch {
      presets.value = null
    }
  }

  /** 保存改过的键。secret 的空串不提交（= 没动），要清除请显式调 `clear()`。 */
  async function save(): Promise<void> {
    const patch: SettingsPatch = {}
    for (const key of dirtyKeys.value) patch[key] = draft.value[key] ?? ''
    if (!Object.keys(patch).length) return
    await guarded(async () => {
      absorb(await settingsApi.patch(patch))
      savedAt.value = new Date().toISOString()
      await loadPresets()
    })
  }

  /** 立刻写一项（下拉框这种改完就该生效的）。 */
  async function setOne(key: string, value: DraftValue | null): Promise<void> {
    await guarded(async () => {
      absorb(await settingsApi.patch({ [key]: value }))
      savedAt.value = new Date().toISOString()
      await loadPresets()
    })
  }

  /** 清除这项覆盖，回到环境变量 / 默认。 */
  async function clear(key: string): Promise<void> {
    await setOne(key, null)
  }

  /**
   * 自动获取某一项的候选取值（`field.fetch` 非空的那些：LLM 模型 / 出图模型）。
   *
   * 用的是**草稿里**的协议与地址：用户常常是「改完地址就想看看有哪些模型」。
   * 密钥只在真敲了新的时候才带上——没敲就让后端沿用已保存的那把（它不回明文）。
   *
   * `field.fetch` 同时是设置里的键前缀（`llm` / `image`），所以这里不写
   * 「哪一族要读哪几个键」的第二份表——后端加一族只多一个 `fetch` 值。
   */
  async function fetchOptions(field: SettingField): Promise<boolean> {
    if (!field.fetch) return false
    fetched.value = { key: field.key, busy: true, listing: null, error: null }
    const family = field.fetch
    const typedKey = String(draft.value[`${family}.api_key`] ?? '')
    try {
      const listing = await settingsApi.models(family as 'llm' | 'image', {
        provider: String(draft.value[`${family}.provider`] ?? ''),
        base_url: String(draft.value[`${family}.base_url`] ?? ''),
        ...(typedKey ? { api_key: typedKey } : {}),
      })
      fetched.value = { key: field.key, busy: false, listing, error: null }
      return true
    } catch (err) {
      fetched.value = {
        key: field.key,
        busy: false,
        listing: null,
        error: err instanceof ApiError ? err : null,
      }
      return false
    }
  }

  /** 从列表里挑一个：只改草稿，让「未保存」标记照常出现——存不存是用户的事。 */
  function pickOption(key: string, id: string): void {
    draft.value[key] = id
  }

  function clearFetched(): void {
    fetched.value = { key: '', busy: false, listing: null, error: null }
  }

  async function probe(what: 'llm' | 'video' | 'image'): Promise<boolean> {
    probes.value[what] = { busy: true, result: null, error: null }
    try {
      probes.value[what] = { busy: false, result: await settingsApi.probe(what), error: null }
      return true
    } catch (err) {
      probes.value[what] = {
        busy: false,
        result: null,
        error: err instanceof ApiError ? err : null,
      }
      return false
    }
  }

  async function savePreset(name: string, graph: string): Promise<void> {
    await guarded(async () => {
      await settingsApi.savePreset(name, graph)
      await loadPresets()
    })
  }

  async function uploadPreset(file: File, name?: string): Promise<void> {
    await guarded(async () => {
      await settingsApi.uploadPreset(file, name)
      await loadPresets()
    })
  }

  async function removePreset(name: string): Promise<void> {
    await guarded(async () => {
      await settingsApi.removePreset(name)
      await loadPresets()
      if (byKey.value['video.preset']?.value === name) await clear('video.preset')
      // 出图那份预设指的是同一个目录里的图，删掉之后同样不能再留着一个悬空的名字。
      if (byKey.value['image.preset']?.value === name) await clear('image.preset')
    })
  }

  return {
    snapshot,
    presets,
    draft,
    busy,
    lastError,
    savedAt,
    probes,
    fetched,
    fields,
    groups,
    providers,
    llm,
    llmProtocols,
    imageProtocols,
    draftProtocol,
    draftImageProtocol,
    path,
    byKey,
    dirty,
    dirtyKeys,
    fieldsOf,
    isDirty,
    resetDraft,
    clearError,
    load,
    loadPresets,
    save,
    setOne,
    clear,
    probe,
    fetchOptions,
    pickOption,
    clearFetched,
    savePreset,
    uploadPreset,
    removePreset,
  }
})
