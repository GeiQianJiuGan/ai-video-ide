/**
 * 剧本 / Scene / Shot store（Step 5 的前端状态）。
 *
 * 与 stores/world.ts 同构：pid 由页面传入、`busy` + `lastError`、动作后重拉。
 *
 * 两个刻意的取舍：
 *   1. **提案只活在内存里**——`proposal` 不落库也不持久化，刷新页面就没了。
 *      它就该这样：没被人审阅通过的东西不算数据。
 *   2. **序号由后端说**——移动 / 排序后一律用后端返回的分镜板覆盖本地，
 *      前端不自己算 index_no，否则拖两下就和库里不一致。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  storyApi,
  type BreakdownProposal,
  type Scene,
  type ScenePatch,
  type Shot,
  type ShotPatch,
  type Story,
  type StoryPatch,
  type StoryboardLane,
} from '@/shared/api/story'

export const useStoryStore = defineStore('story', () => {
  const story = ref<Story | null>(null)
  const scenes = ref<Scene[]>([])
  const lanes = ref<StoryboardLane[]>([])
  const selectedSceneId = ref('')
  const selectedShotId = ref('')
  const shot = ref<Shot | null>(null)
  /** AI 拆解提案；null 表示还没提过或已落库 / 已丢弃。 */
  const proposal = ref<BreakdownProposal | null>(null)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const selectedScene = computed(
    () => scenes.value.find((s) => s.id === selectedSceneId.value) ?? null,
  )
  const llm = computed(() => story.value?.llm ?? null)
  const shotCount = computed(() => scenes.value.reduce((n, s) => n + s.shot_count, 0))

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

  async function loadShot(pid: string): Promise<void> {
    if (!selectedShotId.value) {
      shot.value = null
      return
    }
    shot.value = await storyApi.shot(pid, selectedShotId.value)
  }

  /** 剧本页要的三件事：原文、场景列表、当前选中场景。分镜板另有 `loadBoard`。 */
  async function load(pid: string): Promise<void> {
    await guarded(async () => {
      const [row, list] = await Promise.all([storyApi.story(pid), storyApi.scenes(pid)])
      story.value = row
      scenes.value = list
      if (!list.some((s) => s.id === selectedSceneId.value)) {
        selectedSceneId.value = list[0]?.id ?? ''
      }
    })
  }

  async function loadBoard(pid: string): Promise<void> {
    await guarded(async () => {
      const [board, list] = await Promise.all([storyApi.storyboard(pid), storyApi.scenes(pid)])
      lanes.value = board
      scenes.value = list
      if (
        selectedShotId.value &&
        !board.some((l) => l.shots.some((c) => c.id === selectedShotId.value))
      ) {
        selectedShotId.value = ''
      }
      await loadShot(pid)
    })
  }

  async function selectScene(sid: string): Promise<void> {
    selectedSceneId.value = sid
  }

  async function selectShot(pid: string, shotId: string): Promise<void> {
    selectedShotId.value = shotId
    await guarded(() => loadShot(pid))
  }

  // --- 剧本原文 ---

  async function saveStory(pid: string, patch: StoryPatch): Promise<void> {
    await guarded(async () => {
      story.value = await storyApi.saveStory(pid, patch)
    })
  }

  // --- Scene ---

  async function createScene(pid: string, patch: ScenePatch): Promise<Scene> {
    return guarded(async () => {
      const row = await storyApi.createScene(pid, patch)
      selectedSceneId.value = row.id
      scenes.value = await storyApi.scenes(pid)
      return row
    })
  }

  async function updateScene(pid: string, sid: string, patch: ScenePatch): Promise<void> {
    await guarded(async () => {
      await storyApi.updateScene(pid, sid, patch)
      scenes.value = await storyApi.scenes(pid)
    })
  }

  async function removeScene(pid: string, sid: string): Promise<void> {
    await guarded(async () => {
      await storyApi.deleteScene(pid, sid)
      if (selectedSceneId.value === sid) selectedSceneId.value = ''
      scenes.value = await storyApi.scenes(pid)
      if (!selectedSceneId.value) selectedSceneId.value = scenes.value[0]?.id ?? ''
    })
  }

  /** 上移 / 下移一个场景。整条顺序一起提交，后端按数组下标重排。 */
  async function moveScene(pid: string, sid: string, delta: number): Promise<void> {
    const order = scenes.value.map((s) => s.id)
    const at = order.indexOf(sid)
    const to = at + delta
    if (at < 0 || to < 0 || to >= order.length) return
    order.splice(to, 0, ...order.splice(at, 1))
    await guarded(async () => {
      scenes.value = await storyApi.reorderScenes(pid, order)
    })
  }

  // --- Shot ---

  async function createShot(pid: string, sid: string, patch: ShotPatch): Promise<Shot> {
    return guarded(async () => {
      const row = await storyApi.createShot(pid, sid, patch)
      scenes.value = await storyApi.scenes(pid)
      return row
    })
  }

  async function updateShot(pid: string, shotId: string, patch: ShotPatch): Promise<void> {
    await guarded(async () => {
      const row = await storyApi.updateShot(pid, shotId, patch)
      if (shot.value?.id === row.id) shot.value = row
      lanes.value = await storyApi.storyboard(pid)
    })
  }

  async function removeShot(pid: string, shotId: string): Promise<void> {
    await guarded(async () => {
      await storyApi.deleteShot(pid, shotId)
      if (selectedShotId.value === shotId) {
        selectedShotId.value = ''
        shot.value = null
      }
      lanes.value = await storyApi.storyboard(pid)
      scenes.value = await storyApi.scenes(pid)
    })
  }

  /** 跨 Scene 移动或场景内换位。后端返回重排后的整块分镜板，直接覆盖。 */
  async function moveShot(
    pid: string,
    shotId: string,
    sceneId: string,
    position?: number,
  ): Promise<void> {
    await guarded(async () => {
      lanes.value = await storyApi.moveShot(pid, shotId, sceneId, position)
      scenes.value = await storyApi.scenes(pid)
      await loadShot(pid)
    })
  }

  async function setCast(pid: string, shotId: string, appearanceIds: string[]): Promise<void> {
    await guarded(async () => {
      shot.value = await storyApi.setShotCast(pid, shotId, appearanceIds)
      lanes.value = await storyApi.storyboard(pid)
    })
  }

  async function setProps(
    pid: string,
    shotId: string,
    items: { prop_id: string; state: string }[],
  ): Promise<void> {
    await guarded(async () => {
      shot.value = await storyApi.setShotProps(pid, shotId, items)
    })
  }

  // --- AI 拆解（可选） ---

  async function propose(pid: string, text?: string): Promise<void> {
    await guarded(async () => {
      proposal.value = await storyApi.propose(pid, text)
    })
  }

  function discardProposal(): void {
    proposal.value = null
  }

  /** 落库：把（可能被改过 op 的）提案整体回传，后端只写 op !== 'reject' 的条目。 */
  async function applyProposal(
    pid: string,
  ): Promise<{ scenes_created: number; shots_created: number }> {
    const current = proposal.value
    if (!current) throw new Error('没有待落库的提案')
    return guarded(async () => {
      const out = await storyApi.apply(pid, current.scenes)
      proposal.value = null
      const [row, list] = await Promise.all([storyApi.story(pid), storyApi.scenes(pid)])
      story.value = row
      scenes.value = list
      return out
    })
  }

  return {
    story,
    scenes,
    lanes,
    selectedSceneId,
    selectedScene,
    selectedShotId,
    shot,
    proposal,
    llm,
    shotCount,
    busy,
    lastError,
    load,
    loadBoard,
    selectScene,
    selectShot,
    saveStory,
    createScene,
    updateScene,
    removeScene,
    moveScene,
    createShot,
    updateShot,
    removeShot,
    moveShot,
    setCast,
    setProps,
    propose,
    discardProposal,
    applyProposal,
    clearError,
  }
})
