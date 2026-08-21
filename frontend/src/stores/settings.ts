/**
 * 设置 store（Step 1）。
 *
 * 与其它 store 同构（`busy` + `lastError` + 动作后重拉），但有两处专门的取舍：
 *
 *   1. **草稿与已保存分开**：输入框绑在 `draft` 上，只有「保存」时才把**改过的键**
 *      提交给后端。密钥字段的草稿初始为空串，空串意味着「没动」——因为后端根本不回明文，
 *      不这样做会在第一次保存时把用户的密钥清掉。
 *   2. **探测失败不是异常流程**：连不上 LLM / 视频服务是常态（服务没起、地址写错），
 *      所以两块各存一份 `probe` 结果或错误，页面把 suggestions 显示出来，而不是一个红叉。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  settingsApi,
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
  const probes = ref<Record<'llm' | 'video', ProbeState>>({
    llm: emptyProbe(),
    video: emptyProbe(),
  })

  const fields = computed(() => snapshot.value?.fields ?? [])
  const groups = computed(() => snapshot.value?.groups ?? [])
  const providers = computed(() => snapshot.value?.providers ?? [])
  const llm = computed(() => snapshot.value?.llm ?? null)
  const path = computed(() => snapshot.value?.path ?? '')

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

  async function probe(what: 'llm' | 'video'): Promise<boolean> {
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
    fields,
    groups,
    providers,
    llm,
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
    savePreset,
    uploadPreset,
    removePreset,
  }
})
