/**
 * 时间线状态（Step 8 的前端）。
 *
 * 三个刻意的取舍：
 *   1. **每条编辑命令的返回值就是新的整条时间线**，直接整体覆盖。后端已经重排过
 *      index_no 与 ripple 之后的位置，前端再算一遍只会算出第二套真相。
 *   2. **撤销可用性只看后端**（`can_undo` / `can_redo`）。撤销栈在进程里，重启即清空——
 *      前端自己记一份必然会在重启后骗人。
 *   3. **导出分两步**：先 `plan()` 把将要执行的 FFmpeg 命令摆出来，再 `runExport()`。
 *      这里不把两步合成一个「导出」动作，因为看不见命令的导出出问题时无从下手。
 *
 * 装配的 `skipped` 单独存一份：它是这一页最重要的产出之一（「哪几个镜头没铺上、为什么」），
 * 不能只作为一次性返回值被丢掉。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  timelineApi,
  type AssembleResult,
  type Clip,
  type ExportPlan,
  type ExportRecord,
  type Timeline,
  type Transition,
} from '@/shared/api/timeline'

export const useTimelineStore = defineStore('timeline', () => {
  const timeline = ref<Timeline | null>(null)
  const transitions = ref<Transition[]>([])
  const exports = ref<ExportRecord[]>([])
  /** 最近一次装配被跳过的镜头，带结构化理由。 */
  const skipped = ref<AssembleResult['skipped']>([])
  const placedCount = ref<number | null>(null)
  /** 导出预检结果；`null` 表示还没预检过。 */
  const plan = ref<ExportPlan | null>(null)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const clips = computed<Clip[]>(() => timeline.value?.tracks.flatMap((t) => t.clips) ?? [])
  /** 文件已经不在磁盘上的片段——导出前必须先处理，否则 FFmpeg 会在半路失败。 */
  const missing = computed(() => clips.value.filter((c) => c.missing_file))
  const canUndo = computed(() => timeline.value?.can_undo ?? false)
  const canRedo = computed(() => timeline.value?.can_redo ?? false)

  function clearError(): void {
    lastError.value = null
  }

  function clearAssembleNote(): void {
    skipped.value = []
    placedCount.value = null
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

  /** 转场与导出历史各自独立成败：其中一个拉不到不该把时间线清空。 */
  async function load(pid: string): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.get(pid)
    })
    const [tr, ex] = await Promise.all([
      timelineApi.transitions(pid).catch(() => [] as Transition[]),
      timelineApi.exports(pid).catch(() => [] as ExportRecord[]),
    ])
    transitions.value = tr
    exports.value = ex
  }

  async function assemble(pid: string, replace = true): Promise<void> {
    await guarded(async () => {
      const out = await timelineApi.assemble(pid, replace)
      timeline.value = out.timeline
      skipped.value = out.skipped
      placedCount.value = out.placed.length
    })
  }

  /** 撤销栈为空时后端回 CONFLICT——照常显示，不吞。 */
  async function undo(pid: string): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.undo(pid)
    })
  }

  async function redo(pid: string): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.redo(pid)
    })
  }

  async function move(pid: string, clipId: string, start: number): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.move(pid, clipId, start)
    })
  }

  async function trim(
    pid: string,
    clipId: string,
    body: { in_point?: number | null; out_point?: number | null; ripple?: boolean },
  ): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.trim(pid, clipId, body)
    })
  }

  async function split(pid: string, clipId: string, at: number): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.split(pid, clipId, at)
    })
  }

  async function remove(pid: string, clipId: string, ripple = true): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.remove(pid, clipId, ripple)
    })
    // 删片段会连带删掉挂在它上面的转场
    transitions.value = await timelineApi.transitions(pid).catch(() => transitions.value)
  }

  async function replaceVersion(pid: string, clipId: string, versionId: string): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.replaceVersion(pid, clipId, versionId)
    })
  }

  async function addTransition(
    pid: string,
    body: { from_clip_id: string; to_clip_id: string; kind?: string; duration?: number },
  ): Promise<void> {
    await guarded(async () => {
      await timelineApi.addTransition(pid, body)
      transitions.value = await timelineApi.transitions(pid)
    })
  }

  async function removeTransition(pid: string, tid: string): Promise<void> {
    await guarded(async () => {
      await timelineApi.removeTransition(pid, tid)
      transitions.value = await timelineApi.transitions(pid)
    })
  }

  /** 预检：只拿命令，不起进程。 */
  async function loadPlan(pid: string): Promise<void> {
    await guarded(async () => {
      plan.value = await timelineApi.exportCommand(pid)
    })
  }

  async function runExport(pid: string, path?: string | null): Promise<ExportRecord | null> {
    let record: ExportRecord | null = null
    try {
      record = await guarded(() => timelineApi.export(pid, path))
    } catch {
      record = null
    }
    // 成功与失败都会留一条记录，历史必须刷新
    exports.value = await timelineApi.exports(pid).catch(() => exports.value)
    return record
  }

  return {
    timeline,
    transitions,
    exports,
    skipped,
    placedCount,
    plan,
    busy,
    lastError,
    clips,
    missing,
    canUndo,
    canRedo,
    load,
    assemble,
    undo,
    redo,
    move,
    trim,
    split,
    remove,
    replaceVersion,
    addTransition,
    removeTransition,
    loadPlan,
    runExport,
    clearError,
    clearAssembleNote,
  }
})
