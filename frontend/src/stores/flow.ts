/**
 * 幕流程图 store（两级场景系统的第一级）。
 *
 * 与 stores/story.ts 同构：pid 由页面传入、`busy` + `lastError`、动作后重拉。
 *
 * 三个刻意的取舍：
 *   1. **账单只活在内存里**——`plan` 不持久化，刷新就没了。它是「按下去之前先看一眼」，
 *      不是数据；执行完把它换成 `run` 返回的那一份，界面上「说好的」与「做了的」能对上。
 *   2. **改一条衔接就重拉整张图**——补出来的转场镜头会改变节点上的计数，
 *      本地补算迟早和库里不一致。
 *   3. **入队被拒不是异常**——`run` 返回的 `skipped` 里每条都带结构化原因，
 *      页面照常显示（含 suggestions），不当成失败弹红叉。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  sequenceApi,
  type FlowGraph,
  type FlowNode,
  type LinkMode,
  type SceneLink,
  type SequenceMode,
  type SequencePlan,
  type SequenceRun,
} from '@/shared/api/sequence'
import { storyApi, type ScenePatch } from '@/shared/api/story'

export const useFlowStore = defineStore('flow', () => {
  const graph = ref<FlowGraph | null>(null)
  const selectedSceneId = ref('')
  /** 编排模式：各幕并发 / 单线程续接。 */
  const mode = ref<SequenceMode>('parallel')
  /** 账单。null 表示还没算过或已经执行完清掉。 */
  const plan = ref<SequencePlan | null>(null)
  /** 最近一次执行的结果，含被跳过的每一条与原因。 */
  const lastRun = ref<SequenceRun | null>(null)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const nodes = computed<FlowNode[]>(() => graph.value?.nodes ?? [])
  const links = computed<SceneLink[]>(() => graph.value?.links ?? [])
  const selectedScene = computed(
    () => nodes.value.find((n) => n.id === selectedSceneId.value) ?? null,
  )
  const shotTotal = computed(() => nodes.value.reduce((n, s) => n + s.shot_count, 0))
  const generatedTotal = computed(() => nodes.value.reduce((n, s) => n + s.generated_count, 0))

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
      graph.value = await sequenceApi.graph(pid)
      if (!graph.value.nodes.some((n) => n.id === selectedSceneId.value)) {
        selectedSceneId.value = graph.value.nodes[0]?.id ?? ''
      }
    })
  }

  /** 相邻两幕之间那一条；没配过就是 `cut`（后端也这么默认）。 */
  function linkBetween(fromId: string, toId: string): SceneLink | null {
    return links.value.find((l) => l.from_scene_id === fromId && l.to_scene_id === toId) ?? null
  }

  async function addScene(pid: string, patch: ScenePatch): Promise<void> {
    await guarded(async () => {
      const made = await storyApi.createScene(pid, patch)
      graph.value = await sequenceApi.graph(pid)
      selectedSceneId.value = made.id
    })
  }

  async function saveScene(pid: string, sid: string, patch: ScenePatch): Promise<void> {
    await guarded(async () => {
      await storyApi.updateScene(pid, sid, patch)
      graph.value = await sequenceApi.graph(pid)
    })
  }

  async function removeScene(pid: string, sid: string): Promise<void> {
    await guarded(async () => {
      await storyApi.deleteScene(pid, sid)
      graph.value = await sequenceApi.graph(pid)
    })
  }

  /** 改衔接方式。账单会因此不一样，所以顺手把它作废，逼用户重新看一眼。 */
  async function setLink(
    pid: string,
    fromId: string,
    toId: string,
    linkMode: LinkMode,
    duration?: number,
  ): Promise<void> {
    await guarded(async () => {
      await sequenceApi.setLink(pid, {
        from_scene_id: fromId,
        to_scene_id: toId,
        mode: linkMode,
        duration: duration ?? null,
      })
      graph.value = await sequenceApi.graph(pid)
      plan.value = null
    })
  }

  async function makePlan(pid: string): Promise<SequencePlan | null> {
    try {
      plan.value = await guarded(() => sequenceApi.plan(pid, mode.value))
      return plan.value
    } catch {
      plan.value = null
      return null
    }
  }

  /** 真入队。执行完把账单换成后端回的那一份，并重拉图（转场镜头改变了计数）。 */
  async function run(pid: string): Promise<SequenceRun | null> {
    try {
      const out = await guarded(() => sequenceApi.run(pid, mode.value))
      lastRun.value = out
      plan.value = out.plan
      graph.value = await sequenceApi.graph(pid).catch(() => graph.value)
      return out
    } catch {
      return null
    }
  }

  function discardPlan(): void {
    plan.value = null
  }

  return {
    graph,
    nodes,
    links,
    selectedSceneId,
    selectedScene,
    mode,
    plan,
    lastRun,
    busy,
    lastError,
    shotTotal,
    generatedTotal,
    load,
    linkBetween,
    addScene,
    saveScene,
    removeScene,
    setLink,
    makePlan,
    run,
    discardPlan,
    clearError,
  }
})
