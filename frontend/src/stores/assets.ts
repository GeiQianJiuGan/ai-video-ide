/**
 * 资产总账状态（Step 3 的前端）。
 *
 * 这一页回答三个问题：有哪些文件、谁在用它、哪些没人要。所以状态就是三块：
 * `assets`（全量，含 ref_count 与 missing）、`refs`（选中那一个的反查结果）、
 * `orphanIds`（扫描出来的孤儿集合）。
 *
 * 两个刻意的取舍：
 *   1. **孤儿扫描是显式动作**，结果记成一个 id 集合叠在列表上，而不是另开一个列表。
 *      「哪些能删」和「有哪些文件」是同一张表的两种读法，分成两页会让人对不上。
 *   2. **删除失败不是异常**。仍被引用时后端回 CONFLICT 并列出是谁在用——那正是
 *      用户要看的东西，照常显示（含 suggestions），不吞不改写。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  assetsApi,
  type Asset,
  type AssetKind,
  type AssetRef,
  type DeleteResult,
} from '@/shared/api/assets'

export const useAssetsStore = defineStore('assets', () => {
  const assets = ref<Asset[]>([])
  const kind = ref<AssetKind | ''>('')
  const selectedId = ref('')
  const refs = ref<AssetRef[]>([])
  /** 已扫描出的孤儿 id；`null` 表示还没扫过（与「扫过但一个都没有」不是一回事）。 */
  const orphanIds = ref<Set<string> | null>(null)
  const lastDelete = ref<DeleteResult | null>(null)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const selected = computed(() => assets.value.find((a) => a.id === selectedId.value) ?? null)
  const missing = computed(() => assets.value.filter((a) => a.missing))
  const totalBytes = computed(() => assets.value.reduce((n, a) => n + a.size_bytes, 0))
  const orphanBytes = computed(() =>
    orphanIds.value === null
      ? 0
      : assets.value
          .filter((a) => orphanIds.value?.has(a.id))
          .reduce((n, a) => n + a.size_bytes, 0),
  )

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

  async function load(pid: string): Promise<void> {
    await guarded(async () => {
      assets.value = await assetsApi.list(pid, kind.value || undefined)
    })
    if (selectedId.value) await loadRefs(pid, selectedId.value).catch(() => {})
  }

  async function setKind(pid: string, next: AssetKind | ''): Promise<void> {
    kind.value = next
    await load(pid)
  }

  async function loadRefs(pid: string, assetId: string): Promise<void> {
    selectedId.value = assetId
    refs.value = await assetsApi.refs(pid, assetId).catch(() => [])
  }

  async function scanOrphans(pid: string): Promise<void> {
    await guarded(async () => {
      const list = await assetsApi.orphans(pid)
      orphanIds.value = new Set(list.map((a) => a.id))
    })
  }

  /** 删除。仍被引用时 force=false 会拿到 CONFLICT——那正是要给人看的答案。 */
  async function remove(pid: string, assetId: string, force = false): Promise<boolean> {
    lastDelete.value = null
    try {
      lastDelete.value = await guarded(() => assetsApi.remove(pid, assetId, force))
    } catch {
      return false
    }
    if (selectedId.value === assetId) {
      selectedId.value = ''
      refs.value = []
    }
    orphanIds.value?.delete(assetId)
    await load(pid).catch(() => {})
    return true
  }

  return {
    assets,
    kind,
    selectedId,
    refs,
    orphanIds,
    lastDelete,
    busy,
    lastError,
    selected,
    missing,
    totalBytes,
    orphanBytes,
    load,
    setKind,
    loadRefs,
    scanOrphans,
    remove,
    clearError,
  }
})
