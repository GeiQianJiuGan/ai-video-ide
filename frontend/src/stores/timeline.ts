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
  type DetachResult,
  type ExportPlan,
  type ExportRecord,
  type Timeline,
  type Track,
  type Transition,
  type TrimBody,
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
  /** 最近一次「拆出声音」的结果，界面要据此说明新开了轨 / 复用了文件。 */
  const lastDetach = ref<DetachResult | null>(null)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const clips = computed<Clip[]>(() => timeline.value?.tracks.flatMap((t) => t.clips) ?? [])
  /** 文件已经不在磁盘上的片段——导出前必须先处理，否则 FFmpeg 会在半路失败。 */
  const missing = computed(() => clips.value.filter((c) => c.missing_file))
  const canUndo = computed(() => timeline.value?.can_undo ?? false)
  const canRedo = computed(() => timeline.value?.can_redo ?? false)
  /**
   * 画面来自**第一条视频轨**——和导出（`timeline.build_command`）同一条规矩。
   * 预览器要是自己另挑一条，看到的就不是将要导出的东西。
   */
  const videoTrack = computed<Track | null>(
    () => timeline.value?.tracks.find((t) => t.kind === 'video') ?? null,
  )
  const audioTracks = computed<Track[]>(
    () => timeline.value?.tracks.filter((t) => t.kind === 'audio') ?? [],
  )

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

  /**
   * 裁切。**拖左边缘时 `in_point` 与 `start` 在同一个 body 里**：一次请求、一格撤销——
   * 分两次发的话「撤销」只能退回一半，边缘会停在一个用户从来没见过的位置。
   */
  async function trim(pid: string, clipId: string, body: TrimBody): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.trim(pid, clipId, body)
    })
  }

  async function split(pid: string, clipId: string, at: number): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.split(pid, clipId, at)
    })
  }

  async function isolateAudioSelection(
    pid: string,
    clipId: string,
    body: { in_point: number; out_point: number },
  ): Promise<{ selectedClipId: string; segments: number }> {
    const out = await guarded(() => timelineApi.isolateAudioSelection(pid, clipId, body))
    timeline.value = out.timeline
    return { selectedClipId: out.selected_clip_id, segments: out.segments }
  }

  async function remove(pid: string, clipId: string, ripple = true): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.remove(pid, clipId, ripple)
    })
    // 删片段会连带删掉挂在它上面的转场
    transitions.value = await timelineApi.transitions(pid).catch(() => transitions.value)
  }

  /** 清空这一段的内容（位置与长度不动）。删除是另一件事，见 `remove`。 */
  async function clear(pid: string, clipId: string): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.clear(pid, clipId)
    })
  }

  async function replaceVersion(pid: string, clipId: string, versionId: string): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.replaceVersion(pid, clipId, versionId)
    })
  }

  // --- 声音 ---

  async function setMix(
    pid: string,
    clipId: string,
    body: { muted?: boolean; volume?: number },
  ): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.setMix(pid, clipId, body)
    })
  }

  /**
   * 把画面的声音拆成独立音频片段。返回值留一份给界面说话：
   * 新开了一条轨、或者复用了上次拆好的文件，都是用户该知道的事。
   */
  async function detachAudio(pid: string, clipId: string): Promise<DetachResult> {
    const out = await guarded(() => timelineApi.detachAudio(pid, clipId))
    timeline.value = out.timeline
    lastDetach.value = out
    return out
  }

  // --- 轨道 ---

  async function addTrack(pid: string, kind = 'audio', name?: string | null): Promise<Track> {
    const out = await guarded(() => timelineApi.addTrack(pid, kind, name))
    timeline.value = out.timeline
    return out.track
  }

  async function patchTrack(
    pid: string,
    trackId: string,
    body: { name?: string; muted?: boolean; locked?: boolean },
  ): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.patchTrack(pid, trackId, body)
    })
  }

  /** 轨道上还有片段时后端先回 CONFLICT + `confirm: "force"`，界面确认后再带 `force` 重放。 */
  async function removeTrack(pid: string, trackId: string, force = false): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.removeTrack(pid, trackId, force)
    })
  }

  async function addClip(
    pid: string,
    trackId: string,
    body: { asset_id: string; start?: number; duration?: number | null; label?: string | null },
  ): Promise<string> {
    const out = await guarded(() => timelineApi.addClip(pid, trackId, body))
    timeline.value = out.timeline
    return out.clip_id
  }

  async function addBlankClip(
    pid: string,
    trackId: string,
    body: { duration: number; label?: string | null },
  ): Promise<string> {
    const out = await guarded(() => timelineApi.addBlankClip(pid, trackId, body))
    timeline.value = out.timeline
    return out.clip_id
  }

  async function resizeBlankClip(pid: string, clipId: string, duration: number): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.resizeBlankClip(pid, clipId, duration)
    })
  }

  async function moveToAudioTrack(pid: string, clipId: string, trackId: string): Promise<void> {
    await guarded(async () => {
      timeline.value = await timelineApi.moveToAudioTrack(pid, clipId, trackId)
    })
  }

  async function moveToNewAudioTrack(pid: string, clipId: string): Promise<string | null> {
    const out = await guarded(() => timelineApi.moveToNewAudioTrack(pid, clipId))
    timeline.value = out.timeline
    return out.track_id
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

  async function openExportFolder(pid: string): Promise<string | null> {
    try {
      return (await guarded(() => timelineApi.openExportFolder(pid))).path
    } catch {
      return null
    }
  }

  return {
    timeline,
    transitions,
    exports,
    skipped,
    placedCount,
    plan,
    lastDetach,
    busy,
    lastError,
    clips,
    missing,
    canUndo,
    canRedo,
    videoTrack,
    audioTracks,
    load,
    assemble,
    undo,
    redo,
    move,
    trim,
    split,
    isolateAudioSelection,
    remove,
    clear,
    replaceVersion,
    setMix,
    detachAudio,
    addTrack,
    patchTrack,
    removeTrack,
    addClip,
    addBlankClip,
    resizeBlankClip,
    moveToAudioTrack,
    moveToNewAudioTrack,
    addTransition,
    removeTransition,
    loadPlan,
    runExport,
    openExportFolder,
    clearError,
    clearAssembleNote,
  }
})
