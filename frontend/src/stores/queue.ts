/**
 * 队列 store（Step 7 的前端）。
 *
 * 队列是唯一「会自己变」的东西，所以这里比别的 store 多一件事：**WS 订阅**。
 * 订阅的归属在**底部控制台**（`app/layout/ConsolePanel.vue`）——它常驻，所以
 * 离开队列页不会把实时通道一起带走；队列页只 `load()`，不 connect / disconnect。
 * 三个刻意的取舍：
 *   1. **WS 只当「该刷新了」的信号**。事件幂等且可丢失（docs/03 §5），
 *      所以收到任何 job/queue 事件都去重拉一次 REST，而不是把 payload 往列表里拼——
 *      拼出来的状态一旦丢一条事件就永远对不上。
 *   2. **重拉有节流**。一个任务跑起来会连着推进度，逐条重拉会把后端问爆；
 *      合并到 400ms 一次。
 *   3. **暂停不是错误状态**。`paused` 只是队列不往下取，已经 running 的照常跑完，
 *      页面要把这句话写出来，不然用户会以为点了暂停就等于取消。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import { generationApi, type Job, type JobBatch, type QueueState } from '@/shared/api/generation'
import { EventClient, type ConnState } from '@/shared/api/ws'

/**
 * 任务框里的一行：要么是一次编排合并成的那一条（`batch` 有值，`members` 是它的成员），
 * 要么是一条零散任务（`job` 有值）。两者互斥，界面照 `batch` 有没有值分支。
 */
export interface QueueRow {
  key: string
  at: string
  batch: JobBatch | null
  job: Job | null
  members: Job[]
}

export interface BreakdownTask {
  id: string
  kind: 'breakdown'
  status: 'running' | 'done' | 'failed'
  title: string
  detail: string
  created_at: string
  finished_at: string | null
  error: string | null
}

export const useQueueStore = defineStore('queue', () => {
  const state = ref<QueueState | null>(null)
  const conn = ref<ConnState>('closed')
  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  /**
   * 非生成类任务消息。剧本拆解目前是同步 API（没有后端 Job），但它仍然是
   * 一件用户发起、需要等待并且可能失败的工作；把它放在任务框里，用户不会
   * 误以为点击后没有执行。按工程切换时由 reset() 清空。
   */
  const breakdownTasks = ref<BreakdownTask[]>([])

  const jobs = computed(() => state.value?.jobs ?? [])
  const batches = computed(() => state.value?.batches ?? [])
  /**
   * 任务框要显示的行：**一次编排一行，剩下的零散任务各一行**。
   * 合并那一行的成员不再单独出现在顶层（展开才看得到），否则点一下「单线程续接」
   * 会在队列里刷出几十行长得一模一样的东西——那正是要修的毛病。
   *
   * **排序按入队时间倒序，一条完成了不会跳到最底下**：任务框读的是「我刚才让它做的
   * 那些事现在怎么样了」，按状态重排会让刚跑完的那条从眼前消失，用户以为它丢了。
   */
  const rows = computed<QueueRow[]>(() => {
    const merged = new Set<string>()
    for (const b of batches.value) for (const id of b.job_ids) merged.add(id)
    const out: QueueRow[] = [
      ...batches.value.map((batch) => ({
        key: `batch-${batch.id}`,
        at: batch.created_at,
        batch,
        job: null,
        members: jobs.value.filter((j) => j.batch_id === batch.id),
      })),
      ...jobs.value
        .filter((j) => !merged.has(j.id))
        .map((job) => ({
          key: `job-${job.id}`,
          at: job.created_at,
          batch: null,
          job,
          members: [],
        })),
    ]
    return out.sort((a, b) => String(b.at).localeCompare(String(a.at)))
  })
  const paused = computed(() => state.value?.paused ?? false)
  const counts = computed(() => state.value?.counts ?? {})
  const active = computed(() => state.value?.active ?? 0)
  const failed = computed(() => jobs.value.filter((j) => j.status === 'failed'))
  /** 队列空闲：没有在跑也没有在排。已完成的历史不算「有事在做」。 */
  const idle = computed(
    () => !jobs.value.some((j) => ['queued', 'waiting', 'running'].includes(j.status)),
  )

  let client: EventClient | null = null
  let timer: number | null = null
  let currentPid = ''

  function clearError(): void {
    lastError.value = null
  }

  async function load(pid: string): Promise<void> {
    try {
      state.value = await generationApi.queue(pid)
      lastError.value = null
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
      throw err
    }
  }

  function beginBreakdown(): string {
    const id = `breakdown-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
    breakdownTasks.value = [
      ...breakdownTasks.value,
      {
        id,
        kind: 'breakdown' as const,
        status: 'running' as const,
        title: 'AI 剧本拆解',
        detail: '正在根据剧本生成 Scene / Shot 提案…',
        created_at: new Date().toISOString(),
        finished_at: null,
        error: null,
      },
    ].slice(-20)
    return id
  }

  function finishBreakdown(id: string, detail = '拆解完成，提案已准备好审阅。'): void {
    const row = breakdownTasks.value.find((task) => task.id === id)
    if (!row) return
    row.status = 'done'
    row.detail = detail
    row.finished_at = new Date().toISOString()
  }

  function failBreakdown(id: string, error: string): void {
    const row = breakdownTasks.value.find((task) => task.id === id)
    if (!row) return
    row.status = 'failed'
    row.detail = '拆解失败'
    row.error = error
    row.finished_at = new Date().toISOString()
  }

  /** 合并高频事件：400ms 内的多条只重拉一次。 */
  function scheduleReload(pid: string): void {
    if (timer !== null) return
    timer = window.setTimeout(() => {
      timer = null
      void load(pid).catch(() => {})
    }, 400)
  }

  async function guarded(pid: string, run: () => Promise<unknown>): Promise<void> {
    busy.value = true
    try {
      await run()
      lastError.value = null
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
    } finally {
      busy.value = false
      await load(pid).catch(() => {})
    }
  }

  /** 订阅这个工程的 job / queue / version 事件。切工程要重新连。 */
  function connect(pid: string): void {
    if (client && currentPid === pid) return
    disconnect()
    currentPid = pid
    client = new EventClient(pid, ['job', 'queue', 'version'])
    client.onState((s) => (conn.value = s))
    client.on(() => scheduleReload(pid))
    // 断线期间的事件丢了就是丢了，重连后只能靠 REST 对齐
    client.onReconnect(() => void load(pid).catch(() => {}))
    client.connect()
  }

  function disconnect(): void {
    client?.close()
    client = null
    currentPid = ''
    conn.value = 'closed'
    if (timer !== null) {
      window.clearTimeout(timer)
      timer = null
    }
  }

  /**
   * 断开并清空。底部控制台是常驻的，切工程 / 关工程后如果不清掉上一份列表，
   * 用户会在新工程里看到别的工程的任务——那比空列表糟得多。
   */
  function reset(): void {
    disconnect()
    state.value = null
    breakdownTasks.value = []
    lastError.value = null
  }

  const pause = (pid: string) => guarded(pid, () => generationApi.pause(pid))
  const resume = (pid: string) => guarded(pid, () => generationApi.resume(pid))
  const retryFailed = (pid: string) => guarded(pid, () => generationApi.retryFailed(pid))
  const cancelAll = (pid: string) => guarded(pid, () => generationApi.cancelAll(pid))
  const clearFailed = (pid: string) =>
    guarded(pid, async () => {
      breakdownTasks.value = breakdownTasks.value.filter((task) => task.status !== 'failed')
      await generationApi.clearFailed(pid)
      clearError()
    })
  const cancel = (pid: string, jobId: string) =>
    guarded(pid, () => generationApi.cancel(pid, jobId))
  const retry = (pid: string, jobId: string) => guarded(pid, () => generationApi.retry(pid, jobId))
  const deleteJob = (pid: string, jobId: string) =>
    guarded(pid, () => generationApi.deleteJob(pid, jobId))
  const setPriority = (pid: string, jobId: string, priority: number) =>
    guarded(pid, () => generationApi.setPriority(pid, jobId, priority))
  /** 整批重跑：失败 / 已取消的成员重新排上去，已完成的一条都不重做。 */
  const retryBatch = (pid: string, batchId: string) =>
    guarded(pid, () => generationApi.retryBatch(pid, batchId))
  const cancelBatch = (pid: string, batchId: string) =>
    guarded(pid, () => generationApi.cancelBatch(pid, batchId))

  return {
    state,
    conn,
    busy,
    lastError,
    jobs,
    batches,
    rows,
    paused,
    counts,
    active,
    failed,
    breakdownTasks,
    beginBreakdown,
    finishBreakdown,
    failBreakdown,
    idle,
    load,
    connect,
    disconnect,
    reset,
    pause,
    resume,
    retryFailed,
    cancelAll,
    clearFailed,
    cancel,
    retry,
    deleteJob,
    setPriority,
    retryBatch,
    cancelBatch,
    clearError,
  }
})

export type { Job }
