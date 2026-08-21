/**
 * AI 导演协作栏 store（幕流程图右栏）。
 *
 * 与 stores/flow.ts 同构：pid 由页面传入、`busy` + `lastError`、动作后重拉。
 *
 * 四个刻意的取舍：
 *   1. **待审提案能活过刷新**——它不存在前端内存里，而是从历史里最后一条 `proposal`
 *      记录恢复出来的；后面若已经有 `applied` 记录，说明这一批审完了，不再恢复。
 *      审到一半刷新页面还得从头聊一遍，那一半功夫就白费了。
 *   2. **丢弃就是把 op 改成 'reject'**——照 story 的老规矩。本地直接把它从待审列表里拿掉，
 *      不发请求：没落库的东西不需要「取消落库」。
 *   3. **一条失败不影响其余**——`apply` 回来的 `failed` 留在 `lastApply` 里显示（含
 *      suggestions），成功的那几条正常消失。
 *   4. **LLM 没配置不是错误**——`llm.configured === false` 时页面显示去设置页的引导；
 *      这一栏关掉，流程图手动编排照旧能走完全程。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  directorApi,
  type DirectorApply,
  type DirectorHistory,
  type DirectorOp,
  type DirectorTurn,
} from '@/shared/api/director'

/** 从一条 proposal 记录里把 ops 取出来。坏数据退回空数组，不抛。 */
function opsOf(turn: DirectorTurn): DirectorOp[] {
  const raw = (turn.content as { ops?: unknown }).ops
  return Array.isArray(raw) ? (raw as DirectorOp[]) : []
}

export const useDirectorStore = defineStore('director', () => {
  const history = ref<DirectorHistory | null>(null)
  /** 待审的提案。空数组 = 没有要审的东西。 */
  const pending = ref<DirectorOp[]>([])
  /** 最近一次落库的结果，含失败的每一条与四要素错误。 */
  const lastApply = ref<DirectorApply | null>(null)
  /** true = 这一轮走的是不支持工具调用的退化路径（提案形状一样，只是提示一句）。 */
  const degraded = ref(false)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const turns = computed<DirectorTurn[]>(() => history.value?.turns ?? [])
  /** 只把人与 AI 说的话画成气泡；提案那几条走右边的 Diff 列表。 */
  const messages = computed(() =>
    turns.value.filter((t) => t.role === 'user' || t.role === 'assistant'),
  )
  const llm = computed(() => history.value?.llm ?? null)
  const configured = computed(() => Boolean(history.value?.llm.configured))
  const note = computed(() => history.value?.note ?? '')
  const hasPending = computed(() => pending.value.length > 0)

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

  /** 从历史里恢复待审提案：最后一条 proposal 之后没有 applied，才算还没审完。 */
  function restorePending(): void {
    const rows = turns.value
    const last = [...rows].reverse().find((t) => t.role === 'proposal' || t.role === 'applied')
    pending.value = last && last.role === 'proposal' ? opsOf(last) : []
  }

  async function load(pid: string): Promise<void> {
    await guarded(async () => {
      history.value = await directorApi.history(pid)
      restorePending()
    })
  }

  /**
   * 说一句话。**不落库**——回来的只是提案。
   *
   * 「转了太多轮」那种错误也照旧重拉历史：提案已经落成记录了，用户还能接着审。
   */
  async function send(pid: string, message: string): Promise<boolean> {
    try {
      const out = await guarded(() => directorApi.chat(pid, message))
      degraded.value = out.degraded
      lastApply.value = null
      history.value = await directorApi.history(pid).catch(() => history.value)
      pending.value = out.ops
      return true
    } catch {
      history.value = await directorApi.history(pid).catch(() => history.value)
      restorePending()
      return false
    }
  }

  /** 丢弃一条：本地把 op 改成 'reject' 并移出待审列表，不发请求。 */
  function discard(tempId: string): void {
    pending.value = pending.value.filter((op) => op.temp_id !== tempId)
  }

  function discardAll(): void {
    pending.value = []
  }

  async function apply(pid: string, ops: DirectorOp[]): Promise<DirectorApply | null> {
    if (!ops.length) return null
    try {
      const out = await guarded(() => directorApi.apply(pid, ops))
      lastApply.value = out
      const failed = new Set(out.failed.map((f) => f.temp_id ?? ''))
      const asked = new Set(ops.map((op) => op.temp_id))
      // 成功的走掉、失败的留下继续显示错误；没提交的那几条不动。
      pending.value = pending.value.filter((op) => !asked.has(op.temp_id) || failed.has(op.temp_id))
      history.value = await directorApi.history(pid).catch(() => history.value)
      return out
    } catch {
      return null
    }
  }

  /** 采用一条。 */
  async function accept(pid: string, tempId: string): Promise<DirectorApply | null> {
    const op = pending.value.find((o) => o.temp_id === tempId)
    return op ? apply(pid, [op]) : null
  }

  /** 全部采用。已经被丢弃的不在 `pending` 里，所以这里天然只落用户认可的。 */
  async function acceptAll(pid: string): Promise<DirectorApply | null> {
    return apply(pid, [...pending.value])
  }

  /** 清空协作记录。已经落库的改动不受影响——那是库里的数据，不是聊天记录。 */
  async function clear(pid: string): Promise<void> {
    await guarded(async () => {
      await directorApi.clear(pid)
      history.value = await directorApi.history(pid)
      pending.value = []
      lastApply.value = null
    })
  }

  return {
    history,
    turns,
    messages,
    llm,
    configured,
    note,
    pending,
    hasPending,
    lastApply,
    degraded,
    busy,
    lastError,
    load,
    send,
    discard,
    discardAll,
    accept,
    acceptAll,
    clear,
    clearError,
  }
})
