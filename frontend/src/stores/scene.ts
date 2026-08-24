/**
 * 幕工作台 store（两级场景系统的第二级）。
 *
 * 一幕的小工作坊：这一幕是什么、有哪些镜头、每个镜头首帧从哪来、出过哪些版本。
 * 和 stores/shot.ts（镜头编辑器）的分工是：那一页盯的是**一个镜头的全部真相**，
 * 这一页盯的是**一幕怎么做出来**——所以镜头是列表里的一项，切换要快。
 *
 * 三个刻意的取舍：
 *   1. **首帧是镜头上一个显式槽位，不是「账单里优先级最高的那条」**——
 *      `Shot.first_frame_asset_id` / `last_frame_asset_id` 就是用户按下去的那一下
 *      （`setFrameSlot` / `uploadFrame`，清空传 `''`）。挂角色 / 选地点变体只是
 *      **往账单里加参考素材**（谁出场、在哪儿），一张都不会被提拔成画面第一格；
 *      上游末帧那条仍然是「把 `prev_shot_id` 指过去」，由后端在生成前抽真末帧。
 *      页面上这三件事必须分开写。
 *   2. **改完必须重拉账单**——挂角色、填槽位、改上游，账单立刻不一样了。
 *   3. **入队被拒不是异常**——`CONTEXT_INCOMPLETE` 是设计里的门槛，
 *      错误照常显示（含 suggestions），账单一起刷新，让用户看见缺哪条。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import { assetsApi, type Asset } from '@/shared/api/assets'
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

/**
 * 镜头上那两个显式槽位的字段名。**清空传 `''`**——`null` 会被后端 `exclude_none`
 * 吃掉，等于没改；两个槽位都**只收图片**（模型端接的是 LoadImage），
 * 挑了视频 / 音频后端用 422「首帧只能是图片」拦下来。
 */
export type FrameSlotKey = 'first_frame_asset_id' | 'last_frame_asset_id'

/** 后缀判图。和后端 `assets.kind_of_suffix` 同一口径，用来筛槽位能挑哪几个资产。 */
const IMAGE_SUFFIX = /\.(png|jpe?g|webp|bmp|gif|tiff?)$/i

export const useSceneStore = defineStore('scene', () => {
  const scene = ref<Scene | null>(null)
  const shots = ref<Shot[]>([])
  const selectedShotId = ref('')
  const bill = ref<ContextBill | null>(null)
  const versions = ref<GenerationVersion[]>([])
  const castOptions = ref<CastOption[]>([])
  const variants = ref<VariantOption[]>([])
  /** 工程里的资产，只为两个槽位的下拉与「已有素材」挑选用（缺图的临时帧不在里面）。 */
  const assets = ref<Asset[]>([])
  /** 最近一次入队产生的任务；页面用它给一句「已入队，去队列页看」。 */
  const lastJob = ref<Job | null>(null)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const shot = computed(() => shots.value.find((s) => s.id === selectedShotId.value) ?? null)
  const included = computed(() => bill.value?.items.filter((i) => i.included) ?? [])
  const omitted = computed(() => bill.value?.items.filter((i) => !i.included) ?? [])
  const currentVersion = computed(() => versions.value.find((v) => v.is_current) ?? null)
  /**
   * 能填进首 / 末帧槽位的资产：**只有图片**，且文件还在磁盘上。
   * 视频 / 音频不是「不能用」，是该当参考素材加进上下文（`uploadRef`）。
   */
  const imageAssets = computed(() =>
    assets.value.filter((a) => !a.missing && IMAGE_SUFFIX.test(a.path)),
  )
  /** 这一幕里导演排的戏；转场是系统补的，列表里要分开看。 */
  const realShots = computed(() => shots.value.filter((s) => s.kind !== 'transition'))
  const transitionShots = computed(() => shots.value.filter((s) => s.kind === 'transition'))
  /** 上游可选项：本幕里排在它前面的镜头。跨幕的续接由流程图上的衔接负责。 */
  const prevCandidates = computed(() =>
    realShots.value.filter(
      (s) => {
        if (s.id === selectedShotId.value || s.index_no >= (shot.value?.index_no ?? 0)) return false
        const byId = new Map(shots.value.map((row) => [row.id, row]))
        const seen = new Set<string>()
        let node: string | null = s.id
        while (node) {
          if (node === selectedShotId.value || seen.has(node)) return false
          seen.add(node)
          node = byId.get(node)?.prev_shot_id ?? null
        }
        return true
      },
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

  /**
   * 挑参考素材要用的清单：形象、地点变体（各自带缩略图，一个接口给全），
   * 外加工程资产总账——首 / 末帧槽位那两个下拉从它里面挑。
   */
  async function loadPickers(pid: string): Promise<void> {
    const [options, rows] = await Promise.all([
      loadNodeOptions(pid),
      assetsApi.list(pid).catch(() => [] as Asset[]),
    ])
    castOptions.value = options.cast
    variants.value = options.locations
    assets.value = rows
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

  /**
   * 填 / 清一个首末帧槽位。`assetId` 传 `''` 就是清空。
   *
   * 这里写的是**镜头上那个字段**，不是往账单里加一条人工项——账单里 `kind=first_frame`
   * 的那条是这个字段的投影。以前上传首帧走的是 override add，于是「哪一张是首帧」
   * 由优先级决定，角色三视图会被当成画面第一格喂进 `AIVS_FIRST_FRAME`。
   */
  async function setFrameSlot(pid: string, key: FrameSlotKey, assetId: string): Promise<void> {
    await saveShot(pid, { [key]: assetId })
  }

  /** 上传一张图并直接填进那个槽位（先登记成资产，再写字段）。 */
  async function uploadFrame(pid: string, key: FrameSlotKey, file: File): Promise<void> {
    const id = selectedShotId.value
    const sid = scene.value?.id
    if (!id || !sid) return
    await guarded(async () => {
      const asset = await assetsApi.upload(pid, file, 'upload')
      await storyApi.updateShot(pid, id, { [key]: asset.id })
      await loadShots(pid, sid)
      await refresh(pid)
    })
  }

  /**
   * 上传一个**参考素材**并挂进上下文（人工项，优先级最高）。
   * 图 / 视频 / 音频都收：参考素材回答的是「谁出场、在哪儿、什么动作、什么声音」，
   * 不只是「长什么样」。认不出后缀的后端会列出来但不采用，并写清理由。
   */
  async function uploadRef(pid: string, file: File): Promise<void> {
    const id = selectedShotId.value
    const sid = scene.value?.id
    if (!id || !sid) return
    await guarded(async () => {
      const asset = await assetsApi.upload(pid, file, 'upload')
      bill.value = await contextApi.override(pid, id, {
        action: 'add',
        asset_id: asset.id,
        label: file.name,
      })
      await loadShots(pid, sid)
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
   *
   * `allowRefDrop=true` 是**另一种**显式确认：参考图比模型端那份图能收的多时后端先回
   * `REF_OVER_CAPACITY`（一个任务都没入队），用户看清会丢哪几张之后带这个标志重来一次。
   */
  async function enqueue(
    pid: string,
    checkContext = true,
    allowRefDrop = false,
  ): Promise<Job | null> {
    const id = selectedShotId.value
    if (!id) return null
    busy.value = true
    try {
      lastJob.value = await generationApi.enqueueShot(pid, id, {
        check_context: checkContext,
        allow_ref_drop: allowRefDrop,
      })
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
    allowRefDrop = false,
  ): Promise<{ queued: string[]; skipped: unknown[] } | null> {
    const sid = scene.value?.id
    if (!sid) return null
    try {
      const out = await guarded(() => generationApi.enqueueScene(pid, sid, 100, allowRefDrop))
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
    assets,
    imageAssets,
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
    setFrameSlot,
    uploadFrame,
    uploadRef,
    addVersion,
    setCurrent,
    enqueue,
    enqueueScene,
    clearError,
  }
})
