/**
 * 角色 store：角色列表 + 当前角色的形象树 + Character Sheet 版本。
 *
 * 与 stores/project.ts 同构（busy / lastError / clearError），三点额外约定：
 *   1. pid 由页面传进来，store 不存副本——工程是应用级状态，存两份必然会有一份是旧的；
 *   2. 继承值不在这里算：`Appearance.fields` 是后端算好的账单，直接透传给 UI；
 *   3. Sheet 版本只增不改（硬约束 3），所以 addSheet 之后重拉列表而不是本地改字段。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  castApi,
  type Appearance,
  type AppearancePatch,
  type AppearanceRow,
  type Character,
  type CharacterPatch,
  type InheritableField,
  type SheetVersion,
} from '@/shared/api/cast'

export const useCastStore = defineStore('cast', () => {
  const characters = ref<Character[]>([])
  const selectedId = ref('')
  const appearances = ref<AppearanceRow[]>([])
  const selectedAppearanceId = ref('')
  /** 当前形象的全部 Sheet 版本（列表里只带 current_sheet，看历史要单独拉）。 */
  const sheets = ref<SheetVersion[]>([])
  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const selected = computed(() => characters.value.find((c) => c.id === selectedId.value) ?? null)
  const selectedAppearance = computed(
    () => appearances.value.find((a) => a.id === selectedAppearanceId.value) ?? null,
  )

  function fail(err: unknown): never {
    lastError.value = err instanceof ApiError ? err : null
    throw err
  }

  function clearError(): void {
    lastError.value = null
  }

  /** 出错时也要把动作跑完（busy 复位），但错误必须留下来给 UI 显示。 */
  async function guarded<T>(run: () => Promise<T>): Promise<T> {
    busy.value = true
    try {
      const out = await run()
      lastError.value = null
      return out
    } catch (err) {
      return fail(err)
    } finally {
      busy.value = false
    }
  }

  async function loadAppearances(pid: string, cid: string): Promise<void> {
    appearances.value = await castApi.appearances(pid, cid)
    const still = appearances.value.some((a) => a.id === selectedAppearanceId.value)
    if (!still) {
      const preferred = appearances.value.find((a) => a.is_default) ?? appearances.value[0]
      selectedAppearanceId.value = preferred?.id ?? ''
    }
    await loadSheets(pid)
  }

  async function loadSheets(pid: string): Promise<void> {
    if (!selectedAppearanceId.value) {
      sheets.value = []
      return
    }
    sheets.value = await castApi.sheets(pid, selectedAppearanceId.value)
  }

  /** 进页面 / 采用完成后的全量对齐。选中项尽量保留，没了才换。 */
  async function load(pid: string): Promise<void> {
    await guarded(async () => {
      characters.value = await castApi.characters(pid)
      if (!characters.value.some((c) => c.id === selectedId.value)) {
        selectedId.value = characters.value[0]?.id ?? ''
        selectedAppearanceId.value = ''
      }
      if (selectedId.value) await loadAppearances(pid, selectedId.value)
      else {
        appearances.value = []
        sheets.value = []
      }
    })
  }

  async function select(pid: string, cid: string): Promise<void> {
    if (selectedId.value === cid) return
    selectedId.value = cid
    selectedAppearanceId.value = ''
    await guarded(() => loadAppearances(pid, cid))
  }

  async function selectAppearance(pid: string, aid: string): Promise<void> {
    selectedAppearanceId.value = aid
    await guarded(() => loadSheets(pid))
  }

  async function create(pid: string, name: string, defaultAssetId: string): Promise<Character> {
    return guarded(async () => {
      // 后端建角色时会顺手给一个「默认形象」——没有形象的角色在镜头里无法被引用
      const row = await castApi.createCharacter(pid, { name, default_asset_id: defaultAssetId })
      characters.value = await castApi.characters(pid)
      selectedId.value = row.id
      selectedAppearanceId.value = ''
      await loadAppearances(pid, row.id)
      return row
    })
  }

  async function update(pid: string, cid: string, patch: CharacterPatch): Promise<void> {
    await guarded(async () => {
      await castApi.updateCharacter(pid, cid, patch)
      characters.value = await castApi.characters(pid)
    })
  }

  async function remove(pid: string, cid: string): Promise<void> {
    await guarded(async () => {
      await castApi.deleteCharacter(pid, cid)
      if (selectedId.value === cid) {
        selectedId.value = ''
        selectedAppearanceId.value = ''
      }
      characters.value = await castApi.characters(pid)
      if (!selectedId.value) selectedId.value = characters.value[0]?.id ?? ''
      if (selectedId.value) await loadAppearances(pid, selectedId.value)
      else {
        appearances.value = []
        sheets.value = []
      }
    })
  }

  /** parent_id 留空即建根形象，带上就是派生（未填字段自动继承父形象）。 */
  async function addAppearance(
    pid: string,
    name: string,
    parentId: string | null,
  ): Promise<Appearance> {
    return guarded(async () => {
      const row = await castApi.createAppearance(pid, selectedId.value, {
        name,
        parent_id: parentId,
      })
      selectedAppearanceId.value = row.id
      characters.value = await castApi.characters(pid)
      await loadAppearances(pid, selectedId.value)
      return row
    })
  }

  async function updateAppearance(pid: string, aid: string, patch: AppearancePatch): Promise<void> {
    await guarded(async () => {
      await castApi.updateAppearance(pid, aid, patch)
      await loadAppearances(pid, selectedId.value)
    })
  }

  /** 把字段还原成继承：值重新由父形象决定。根形象调用会被后端拒绝并说明原因。 */
  async function revertField(pid: string, aid: string, field: InheritableField): Promise<void> {
    await guarded(async () => {
      await castApi.revertField(pid, aid, field)
      await loadAppearances(pid, selectedId.value)
    })
  }

  async function setDefaultAppearance(pid: string, aid: string): Promise<void> {
    await guarded(async () => {
      await castApi.setDefaultAppearance(pid, aid)
      await loadAppearances(pid, selectedId.value)
    })
  }

  async function removeAppearance(pid: string, aid: string): Promise<void> {
    await guarded(async () => {
      await castApi.deleteAppearance(pid, aid)
      if (selectedAppearanceId.value === aid) selectedAppearanceId.value = ''
      characters.value = await castApi.characters(pid)
      await loadAppearances(pid, selectedId.value)
    })
  }

  /** 新版本自动成为当前版本，旧版本保留在 sheets 里可回看。 */
  async function addSheet(pid: string, aid: string, assetId: string): Promise<void> {
    await guarded(async () => {
      await castApi.addSheet(pid, aid, assetId)
      await loadAppearances(pid, selectedId.value)
    })
  }

  return {
    characters,
    selectedId,
    selected,
    appearances,
    selectedAppearanceId,
    selectedAppearance,
    sheets,
    busy,
    lastError,
    load,
    select,
    selectAppearance,
    create,
    update,
    remove,
    addAppearance,
    updateAppearance,
    revertField,
    setDefaultAppearance,
    removeAppearance,
    addSheet,
    clearError,
  }
})
