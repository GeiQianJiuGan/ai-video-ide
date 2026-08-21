/**
 * Workflow store（Step 4 的前端状态）。
 *
 * 与其它 store 同构：pid 由页面传入、`busy` + `lastError`、动作后重拉。
 *
 * 一个刻意的取舍：**校验失败不算异常流程**。后端在绑定不全或缺节点时抛结构化错误，
 * 但那是「你还得配」而不是「系统坏了」，所以这里把它记进 `lastError` 之外还单独
 * 留一份 `lastValidation`——列表里那条工作流的状态已经被后端改成 invalid，
 * 页面要能同时显示「哪几个槽位没绑」。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  workflowsApi,
  type CapabilityMatrix,
  type ImportWorkflowBody,
  type ValidationResult,
  type Workflow,
} from '@/shared/api/workflows'

export const useWorkflowStore = defineStore('workflows', () => {
  const list = ref<Workflow[]>([])
  const matrix = ref<CapabilityMatrix | null>(null)
  const selectedId = ref('')
  /** 选中那条的完整体（带 api_json），列表里的没有原始图。 */
  const detail = ref<Workflow | null>(null)
  /** 最近一次校验结果；失败时后端抛错，这里存的是错误里的 problems / missing_nodes。 */
  const lastValidation = ref<ValidationResult | null>(null)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const comfy = computed(() => matrix.value?.comfy ?? null)
  const capabilities = computed(() => matrix.value?.capabilities ?? [])
  const readyCount = computed(() => capabilities.value.filter((c) => c.ready).length)

  function fail(err: unknown): never {
    lastError.value = err instanceof ApiError ? err : null
    throw err
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
      return fail(err)
    } finally {
      busy.value = false
    }
  }

  async function loadDetail(pid: string): Promise<void> {
    detail.value = selectedId.value ? await workflowsApi.get(pid, selectedId.value) : null
  }

  async function load(pid: string): Promise<void> {
    await guarded(async () => {
      const [rows, mtx] = await Promise.all([workflowsApi.list(pid), workflowsApi.matrix(pid)])
      list.value = rows
      matrix.value = mtx
      if (!rows.some((r) => r.id === selectedId.value)) selectedId.value = rows[0]?.id ?? ''
      await loadDetail(pid)
    })
  }

  async function select(pid: string, wid: string): Promise<void> {
    selectedId.value = wid
    lastValidation.value = null
    await guarded(() => loadDetail(pid))
  }

  async function importWorkflow(pid: string, body: ImportWorkflowBody): Promise<Workflow> {
    return guarded(async () => {
      const row = await workflowsApi.import(pid, body)
      selectedId.value = row.id
      const [rows, mtx] = await Promise.all([workflowsApi.list(pid), workflowsApi.matrix(pid)])
      list.value = rows
      matrix.value = mtx
      await loadDetail(pid)
      return row
    })
  }

  async function bind(pid: string, wid: string, bindings: Record<string, string>): Promise<void> {
    await guarded(async () => {
      detail.value = await workflowsApi.bind(pid, wid, bindings)
      list.value = await workflowsApi.list(pid)
    })
  }

  /**
   * 校验。失败时后端已经把 status 改成 invalid 并写了 validation_json，
   * 所以无论成功失败都要重拉列表——否则页面上的状态点会停在校验前，
   * 而「哪几个槽位没绑」也正是从重拉回来的 `detail.validation` 里读的
   * （错误体里的 problems 只够写一行标题，不够画整块结果）。
   */
  async function validate(pid: string, wid: string, probe: boolean): Promise<boolean> {
    busy.value = true
    lastValidation.value = null
    try {
      lastValidation.value = await workflowsApi.validate(pid, wid, probe)
      lastError.value = null
      return true
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
      return false
    } finally {
      busy.value = false
      // 状态与 validation 都写在库里了，重拉一次才对得上
      await Promise.all([
        workflowsApi.list(pid).then((rows) => (list.value = rows)),
        workflowsApi.matrix(pid).then((mtx) => (matrix.value = mtx)),
        loadDetail(pid),
      ]).catch(() => {})
    }
  }

  async function setDefault(pid: string, wid: string): Promise<void> {
    await guarded(async () => {
      detail.value = await workflowsApi.setDefault(pid, wid)
      const [rows, mtx] = await Promise.all([workflowsApi.list(pid), workflowsApi.matrix(pid)])
      list.value = rows
      matrix.value = mtx
    })
  }

  async function update(
    pid: string,
    wid: string,
    patch: { name?: string; notes?: string; status?: string },
  ): Promise<void> {
    await guarded(async () => {
      detail.value = await workflowsApi.update(pid, wid, patch)
      const [rows, mtx] = await Promise.all([workflowsApi.list(pid), workflowsApi.matrix(pid)])
      list.value = rows
      matrix.value = mtx
    })
  }

  async function remove(pid: string, wid: string): Promise<void> {
    await guarded(async () => {
      await workflowsApi.remove(pid, wid)
      if (selectedId.value === wid) {
        selectedId.value = ''
        detail.value = null
        lastValidation.value = null
      }
      const [rows, mtx] = await Promise.all([workflowsApi.list(pid), workflowsApi.matrix(pid)])
      list.value = rows
      matrix.value = mtx
      if (!selectedId.value && rows.length) {
        selectedId.value = rows[0]?.id ?? ''
        await loadDetail(pid)
      }
    })
  }

  return {
    list,
    matrix,
    capabilities,
    comfy,
    readyCount,
    selectedId,
    detail,
    lastValidation,
    busy,
    lastError,
    load,
    select,
    importWorkflow,
    bind,
    validate,
    setDefault,
    update,
    remove,
    clearError,
  }
})
