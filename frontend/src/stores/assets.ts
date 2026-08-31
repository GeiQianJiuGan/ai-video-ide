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
  type UndescribedList,
} from '@/shared/api/assets'

export const useAssetsStore = defineStore('assets', () => {
  const assets = ref<Asset[]>([])
  const kind = ref<AssetKind | ''>('')
  const selectedId = ref('')
  const refs = ref<AssetRef[]>([])
  /** 已扫描出的孤儿 id；`null` 表示还没扫过（与「扫过但一个都没有」不是一回事）。 */
  const orphanIds = ref<Set<string> | null>(null)
  /**
   * 缺描述的那一批（`GET /assets/undescribed`）。**跟着列表一起拉，不做成显式动作**：
   * 孤儿扫描要遍历引用所以是按钮，这一条只是问一句「哪些还没写」，而没写描述是
   * 每次进来都该看见的事实——模型引用它们时只看到一个文件名。
   * 临时帧算不算、上限是多少都由后端说（`TRANSIENT_KINDS` / `desc_max`），前端不写第二份。
   */
  const undescribed = ref<UndescribedList | null>(null)
  /** 只看缺描述的那些。与类型筛选叠加，是同一张表的一种读法。 */
  const onlyUndescribed = ref(false)
  const lastDelete = ref<DeleteResult | null>(null)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const selected = computed(() => assets.value.find((a) => a.id === selectedId.value) ?? null)
  const missing = computed(() => assets.value.filter((a) => a.missing))
  const totalBytes = computed(() => assets.value.reduce((n, a) => n + a.size_bytes, 0))
  const undescribedIds = computed(
    () => new Set((undescribed.value?.items ?? []).map((item) => item.id)),
  )
  /** 网格真正渲染的那一批。 */
  const visible = computed(() =>
    onlyUndescribed.value
      ? assets.value.filter((a) => undescribedIds.value.has(a.id))
      : assets.value,
  )
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
    // 缺描述那份清单拉不到不该把整页判成失败（它只是一层标记），所以吞掉这一次的错误。
    undescribed.value = await assetsApi.undescribed(pid).catch(() => null)
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
    undescribed,
    onlyUndescribed,
    lastDelete,
    busy,
    lastError,
    selected,
    missing,
    totalBytes,
    undescribedIds,
    visible,
    orphanBytes,
    load,
    setKind,
    loadRefs,
    scanOrphans,
    remove,
    clearError,
  }
})
