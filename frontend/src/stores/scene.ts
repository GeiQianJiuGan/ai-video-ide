/**
 * 幕工作台 store（两级场景系统的第二级）。
 *
 * 一幕的小工作坊：这一幕是什么、有哪些镜头、每个镜头首帧从哪来、出过哪些版本。
 * 和 stores/shot.ts（镜头编辑器）的分工是：那一页盯的是**一个镜头的全部真相**，
 * 这一页盯的是**一幕怎么做出来**——所以镜头是列表里的一项，切换要快。
 *
 * 三个刻意的取舍：
 *   1. **首帧来源是四条路，不是一个字段**——角色表 / 地点参考 / 上传 / 上游末帧。
 *      前三条都是「往上下文里加一张图」（override add），最后一条是
 *      「把 prev_shot_id 指过去」，由后端在生成前抽真末帧。页面上要写清区别。
 *   2. **改完必须重拉账单**——挂角色、换首帧、改上游，账单立刻不一样了。
 *   3. **入队被拒不是异常**——`CONTEXT_INCOMPLETE` 是设计里的门槛，
 *      错误照常显示（含 suggestions），账单一起刷新，让用户看见缺哪条。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import { assetsApi } from '@/shared/api/assets'
import { loadNodeOptions, type CastOption, type VariantOption } from '@/shared/api/pickers'
import {
  contextApi,
  generationApi,
  type ContextBill,
  type GenerationVersion,
  type Job,
} from '@/shared/api/generation'
import {
  storyApi,
  type Scene,
  type ScenePatch,
  type Shot,
  type ShotPatch,
} from '@/shared/api/story'

export type { CastOption, VariantOption }

export const useSceneStore = defineStore('scene', () => {
  const scene = ref<Scene | null>(null)
  const shots = ref<Shot[]>([])
  const selectedShotId = ref('')
  const bill = ref<ContextBill | null>(null)
  const versions = ref<GenerationVersion[]>([])
  const castOptions = ref<CastOption[]>([])
  const variants = ref<VariantOption[]>([])
  /** 最近一次入队产生的任务；页面用它给一句「已入队，去队列页看」。 */
  const lastJob = ref<Job | null>(null)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const shot = computed(() => shots.value.find((s) => s.id === selectedShotId.value) ?? null)
  const included = computed(() => bill.value?.items.filter((i) => i.included) ?? [])
  const omitted = computed(() => bill.value?.items.filter((i) => !i.included) ?? [])
  const currentVersion = computed(() => versions.value.find((v) => v.is_current) ?? null)
  /** 这一幕里导演排的戏；转场是系统补的，列表里要分开看。 */
  const realShots = computed(() => shots.value.filter((s) => s.kind !== 'transition'))
  const transitionShots = computed(() => shots.value.filter((s) => s.kind === 'transition'))
  /** 上游可选项：本幕里排在它前面的镜头。跨幕的续接由流程图上的衔接负责。 */
  const prevCandidates = computed(() =>
    realShots.value.filter(
      (s) => s.id !== selectedShotId.value && s.index_no < (shot.value?.index_no ?? 0),
    ),
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

  /** 账单与版本各自独立成败：账单拉不到也不该把版本列表清空。 */
  async function refresh(pid: string): Promise<void> {
    const id = selectedShotId.value
    if (!id) {
      bill.value = null
      versions.value = []
      return
    }
    const [b, v] = await Promise.all([
      contextApi.bill(pid, id).catch(() => null),
      generationApi.versions(pid, id).catch(() => []),
    ])
    bill.value = b
    versions.value = v
  }

  /** 这一幕的镜头。整幕的 Shot 都要完整对象（首帧槽位要看 prev_shot_id 与 cast）。 */
  async function loadShots(pid: string, sid: string): Promise<void> {
    const lanes = await storyApi.storyboard(pid)
    const lane = lanes.find((l) => l.id === sid)
    const rows = await Promise.all(
      (lane?.shots ?? []).map((card) => storyApi.shot(pid, card.id).catch(() => null)),
    )
    shots.value = rows.filter((s): s is Shot => s !== null)
    if (!shots.value.some((s) => s.id === selectedShotId.value)) {
      selectedShotId.value = realShots.value[0]?.id ?? shots.value[0]?.id ?? ''
    }
  }

  /** 挑首帧要用的两张清单：形象、地点变体（各自带缩略图，一个接口给全）。 */
  async function loadPickers(pid: string): Promise<void> {
    const options = await loadNodeOptions(pid)
    castOptions.value = options.cast
    variants.value = options.locations
  }

  async function load(pid: string, sid: string): Promise<void> {
    if (!sid) {
      scene.value = null
      shots.value = []
      return
    }
    await guarded(async () => {
      const scenes = await storyApi.scenes(pid)
      scene.value = scenes.find((s) => s.id === sid) ?? null
      if (scene.value === null) {
        shots.value = []
        selectedShotId.value = ''
        return
      }
      await loadShots(pid, sid)
      await Promise.all([refresh(pid), loadPickers(pid)])
    })
  }

  async function select(pid: string, shotId: string): Promise<void> {
    selectedShotId.value = shotId
    await guarded(() => refresh(pid))
  }

  async function saveScene(pid: string, patch: ScenePatch): Promise<void> {
    const sid = scene.value?.id
    if (!sid) return
    await guarded(async () => {
      scene.value = await storyApi.updateScene(pid, sid, patch)
      // 换地点变体会改变每个镜头的账单结论
      await refresh(pid)
    })
  }

  async function addShot(pid: string, title: string): Promise<void> {
    const sid = scene.value?.id
    if (!sid) return
    await guarded(async () => {
      const made = await storyApi.createShot(pid, sid, { title })
      selectedShotId.value = made.id
      await loadShots(pid, sid)
      await refresh(pid)
    })
  }

  async function saveShot(pid: string, patch: ShotPatch): Promise<void> {
    const id = selectedShotId.value
    const sid = scene.value?.id
    if (!id || !sid) return
    await guarded(async () => {
      await storyApi.updateShot(pid, id, patch)
      await loadShots(pid, sid)
      await refresh(pid)
    })
  }

  async function removeShot(pid: string, shotId: string): Promise<void> {
    const sid = scene.value?.id
    if (!sid) return
    await guarded(async () => {
      await storyApi.deleteShot(pid, shotId)
      if (selectedShotId.value === shotId) selectedShotId.value = ''
      await loadShots(pid, sid)
      await refresh(pid)
    })
  }

  async function setCast(pid: string, appearanceIds: string[]): Promise<void> {
    const id = selectedShotId.value
    const sid = scene.value?.id
    if (!id || !sid) return
    await guarded(async () => {
      await storyApi.setShotCast(pid, id, appearanceIds)
      await loadShots(pid, sid)
      await refresh(pid)
    })
  }

  /** 人工干预账单：移除一条 / 加一张图 / 恢复自动。 */
  async function override(
    pid: string,
    body: { action: 'remove' | 'add' | 'reset'; key?: string; asset_id?: string; label?: string },
  ): Promise<void> {
    const id = selectedShotId.value
    const sid = scene.value?.id
    if (!id || !sid) return
    await guarded(async () => {
      bill.value = await contextApi.override(pid, id, body)
      await loadShots(pid, sid)
    })
  }

  /** 上传一张图直接当首帧：先登记成资产，再往账单里加一条人工项。 */
  async function uploadFirstFrame(pid: string, file: File): Promise<void> {
    const id = selectedShotId.value
    if (!id) return
    await guarded(async () => {
      const asset = await assetsApi.upload(pid, file, 'upload')
      bill.value = await contextApi.override(pid, id, {
        action: 'add',
        asset_id: asset.id,
        label: '首帧（上传）',
      })
    })
  }

  async function addVersion(pid: string, file: File): Promise<void> {
    const id = selectedShotId.value
    const sid = scene.value?.id
    if (!id || !sid) return
    await guarded(async () => {
      const asset = await assetsApi.upload(pid, file, 'generated_video')
      await generationApi.addVersion(pid, id, { asset_id: asset.id, kind: 'video' })
      await loadShots(pid, sid)
      versions.value = await generationApi.versions(pid, id)
    })
  }

  async function setCurrent(pid: string, versionId: string): Promise<void> {
    const id = selectedShotId.value
    const sid = scene.value?.id
    if (!id || !sid) return
    await guarded(async () => {
      await generationApi.setCurrent(pid, versionId)
      await loadShots(pid, sid)
      versions.value = await generationApi.versions(pid, id)
    })
  }

  /**
   * 入队生成。`checkContext=false` 是「我确认无误」的显式跳过，不是默认值。
   * 被拒时把账单重拉一次——错误里写着缺什么，页面上也得能一条条看到。
   */
  async function enqueue(pid: string, checkContext = true): Promise<Job | null> {
    const id = selectedShotId.value
    if (!id) return null
    busy.value = true
    try {
      lastJob.value = await generationApi.enqueueShot(pid, id, { check_context: checkContext })
      lastError.value = null
      return lastJob.value
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
      return null
    } finally {
      busy.value = false
      await refresh(pid)
      if (scene.value) await loadShots(pid, scene.value.id).catch(() => undefined)
    }
  }

  /** 整幕入队。返回后端的账单式结果（入队了几条、跳过了哪几条与原因）。 */
  async function enqueueScene(
    pid: string,
  ): Promise<{ queued: string[]; skipped: unknown[] } | null> {
    const sid = scene.value?.id
    if (!sid) return null
    try {
      const out = await guarded(() => generationApi.enqueueScene(pid, sid))
      await loadShots(pid, sid)
      await refresh(pid)
      return out
    } catch {
      return null
    }
  }

  return {
    scene,
    shots,
    realShots,
    transitionShots,
    selectedShotId,
    shot,
    bill,
    included,
    omitted,
    versions,
    currentVersion,
    castOptions,
    variants,
    prevCandidates,
    lastJob,
    busy,
    lastError,
    load,
    select,
    saveScene,
    addShot,
    saveShot,
    removeShot,
    setCast,
    override,
    uploadFirstFrame,
    addVersion,
    setCurrent,
    enqueue,
    enqueueScene,
    clearError,
  }
})
