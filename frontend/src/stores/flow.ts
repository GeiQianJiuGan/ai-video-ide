/**
 * 幕流程图 store（两级场景系统的第一级）。
 *
 * 与 stores/story.ts 同构：pid 由页面传入、`busy` + `lastError`、动作后重拉。
 *
 * 四个刻意的取舍：
 *   1. **账单只活在内存里**——`plan` 不持久化，刷新就没了。它是「按下去之前先看一眼」，
 *      不是数据；执行完把它换成 `run` 返回的那一份，界面上「说好的」与「做了的」能对上。
 *   2. **改一条衔接就重拉整张图**——补出来的转场镜头会改变节点上的计数，
 *      本地补算迟早和库里不一致。改小节点（prompt / 人物 / 地点）也一样重拉：
 *      它们会改变每个镜头的上下文账单，于是节点上的 `issues` 跟着变。
 *   3. **入队被拒不是异常**——`run` 返回的 `skipped` 里每条都带结构化原因，
 *      页面照常显示（含 suggestions），不当成失败弹红叉。
 *   4. **上限不在前端判断**——`node.node_limit` 只用来显示 `N/上限` 与提前禁用按钮，
 *      真正的守卫在后端（超了会带「上限可改：设置页…」的四要素错误）。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import { loadNodeOptions, type CastOption, type VariantOption } from '@/shared/api/pickers'
import {
  sequenceApi,
  type FlowGraph,
  type FlowNode,
  type LinkMode,
  type SceneLink,
  type SceneVideos,
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
  /** 选中那一幕生成过的视频。切幕时先清空，绝不把上一幕的列表留在屏幕上。 */
  const videos = ref<SceneVideos | null>(null)
  /** 小节点要挑的两张清单：形象、地点变体。 */
  const castOptions = ref<CastOption[]>([])
  const variantOptions = ref<VariantOption[]>([])

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
    // 选项清单与视频列表各自独立成败：它们拉不到不该把整张图判成失败
    const options = await loadNodeOptions(pid)
    castOptions.value = options.cast
    variantOptions.value = options.locations
    await loadVideos(pid, selectedSceneId.value)
  }

  /** 选中一幕：顺手把它生成过的视频拉出来（就是「选一段采用」那个列表）。 */
  async function select(pid: string, sid: string): Promise<void> {
    selectedSceneId.value = sid
    await loadVideos(pid, sid)
  }

  async function loadVideos(pid: string, sid: string): Promise<void> {
    if (!pid || !sid) {
      videos.value = null
      return
    }
    videos.value = await sequenceApi.sceneVideos(pid, sid).catch(() => null)
  }

  /**
   * 采用某一段为这一幕的主视频（`versionId = null` 取消采用）。
   * 后端顺手把它设成所属镜头的当前版本，所以整张图要重拉——别的幕的计数也可能跟着变。
   */
  async function adoptMainVideo(pid: string, sid: string, versionId: string | null): Promise<void> {
    await guarded(async () => {
      await sequenceApi.adoptMainVideo(pid, sid, versionId)
      graph.value = await sequenceApi.graph(pid)
      await loadVideos(pid, sid)
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
    await loadVideos(pid, selectedSceneId.value)
  }

  async function saveScene(pid: string, sid: string, patch: ScenePatch): Promise<void> {
    await guarded(async () => {
      await storyApi.updateScene(pid, sid, patch)
      graph.value = await sequenceApi.graph(pid)
    })
  }

  /** 这一幕的人物小节点。空数组是合法的；超上限由后端拒绝（错误里写着改哪里）。 */
  async function setSceneCast(pid: string, sid: string, appearanceIds: string[]): Promise<void> {
    await guarded(async () => {
      await storyApi.setSceneCast(pid, sid, appearanceIds)
      graph.value = await sequenceApi.graph(pid)
    })
  }

  /** 这一幕的地点小节点。**第一条是主地点**，所以传进来的顺序有意义。 */
  async function setSceneLocations(pid: string, sid: string, variantIds: string[]): Promise<void> {
    await guarded(async () => {
      await storyApi.setSceneLocations(pid, sid, variantIds)
      graph.value = await sequenceApi.graph(pid)
    })
  }

  async function removeScene(pid: string, sid: string): Promise<void> {
    await guarded(async () => {
      await storyApi.deleteScene(pid, sid)
      graph.value = await sequenceApi.graph(pid)
      if (!graph.value.nodes.some((n) => n.id === selectedSceneId.value)) {
        selectedSceneId.value = graph.value.nodes[0]?.id ?? ''
      }
    })
    await loadVideos(pid, selectedSceneId.value)
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
    videos,
    castOptions,
    variantOptions,
    mode,
    plan,
    lastRun,
    busy,
    lastError,
    shotTotal,
    generatedTotal,
    load,
    select,
    loadVideos,
    adoptMainVideo,
    linkBetween,
    addScene,
    saveScene,
    setSceneCast,
    setSceneLocations,
    removeScene,
    setLink,
    makePlan,
    run,
    discardPlan,
    clearError,
  }
})
