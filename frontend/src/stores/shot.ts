/**
 * 镜头编辑器状态（Step 6 / Step 7 的前端）。
 *
 * 这一页要同时回答三个问题，所以 store 里就是三块并列的状态：
 *   - 这个镜头是什么（`shot`，含出场角色与道具）；
 *   - 到底喂了什么给模型（`bill`，上下文账单，含被省略项与理由）；
 *   - 出过哪些结果（`versions`，只增不改）。
 *
 * 两个刻意的取舍：
 *   1. **改完必须重拉账单**。挂一个角色、换一次地点变体，账单立刻不一样了——
 *      让页面上的「缺什么」和数据库对不上，比不显示更糟。
 *   2. **入队被拒不是异常**。`CONTEXT_INCOMPLETE` 是设计里的门槛，
 *      错误照常显示（含 suggestions），但账单要一起刷新，让用户看见到底缺哪条。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import { storyApi, type Shot, type ShotPatch } from '@/shared/api/story'
import {
  contextApi,
  generationApi,
  type ContextBill,
  type GenerationVersion,
  type Job,
} from '@/shared/api/generation'

export const useShotStore = defineStore('shot', () => {
  const shot = ref<Shot | null>(null)
  const bill = ref<ContextBill | null>(null)
  const versions = ref<GenerationVersion[]>([])
  /** 最近一次入队产生的任务；页面用它给一句「已入队，去队列页看」。 */
  const lastJob = ref<Job | null>(null)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const included = computed(() => bill.value?.items.filter((i) => i.included) ?? [])
  const omitted = computed(() => bill.value?.items.filter((i) => !i.included) ?? [])
  const currentVersion = computed(() => versions.value.find((v) => v.is_current) ?? null)

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
  async function refresh(pid: string, shotId: string): Promise<void> {
    const [b, v] = await Promise.all([
      contextApi.bill(pid, shotId).catch(() => null),
      generationApi.versions(pid, shotId).catch(() => []),
    ])
    bill.value = b
    versions.value = v
  }

  async function load(pid: string, shotId: string): Promise<void> {
    if (!shotId) {
      shot.value = null
      bill.value = null
      versions.value = []
      return
    }
    await guarded(async () => {
      shot.value = await storyApi.shot(pid, shotId)
      await refresh(pid, shotId)
    })
  }

  async function save(pid: string, patch: ShotPatch): Promise<void> {
    const id = shot.value?.id
    if (!id) return
    await guarded(async () => {
      shot.value = await storyApi.updateShot(pid, id, patch)
      // prompt / 上游镜头都会改变账单的结论
      bill.value = await contextApi.bill(pid, id).catch(() => bill.value)
    })
  }

  async function setCast(pid: string, appearanceIds: string[]): Promise<void> {
    const id = shot.value?.id
    if (!id) return
    await guarded(async () => {
      shot.value = await storyApi.setShotCast(pid, id, appearanceIds)
      await refresh(pid, id)
    })
  }

  async function setProps(pid: string, items: { prop_id: string; state: string }[]): Promise<void> {
    const id = shot.value?.id
    if (!id) return
    await guarded(async () => {
      shot.value = await storyApi.setShotProps(pid, id, items)
      await refresh(pid, id)
    })
  }

  /** 人工干预账单：移除一条 / 加一张图 / 恢复自动。 */
  async function override(
    pid: string,
    body: { action: 'remove' | 'add' | 'reset'; key?: string; asset_id?: string; label?: string },
  ): Promise<void> {
    const id = shot.value?.id
    if (!id) return
    await guarded(async () => {
      bill.value = await contextApi.override(pid, id, body)
      shot.value = await storyApi.shot(pid, id)
    })
  }

  async function addVersion(pid: string, assetId: string, kind = 'video'): Promise<void> {
    const id = shot.value?.id
    if (!id) return
    await guarded(async () => {
      await generationApi.addVersion(pid, id, { asset_id: assetId, kind })
      shot.value = await storyApi.shot(pid, id)
      versions.value = await generationApi.versions(pid, id)
    })
  }

  async function setCurrent(pid: string, versionId: string): Promise<void> {
    const id = shot.value?.id
    if (!id) return
    await guarded(async () => {
      await generationApi.setCurrent(pid, versionId)
      shot.value = await storyApi.shot(pid, id)
      versions.value = await generationApi.versions(pid, id)
    })
  }

  /**
   * 入队生成。`checkContext=false` 是「我确认无误」的显式跳过，不是默认值。
   * 被拒时把账单重拉一次——错误里写着缺什么，页面上也得能一条条看到。
   *
   * `allowRefDrop` 是另一种显式确认：参考图比模型端那份图能收的多时后端先回
   * `REF_OVER_CAPACITY`（还没入队），用户看清会丢哪几张后带这个标志重来一次。
   */
  async function enqueue(
    pid: string,
    opts: { workflowId?: string | null; checkContext?: boolean; allowRefDrop?: boolean } = {},
  ): Promise<Job | null> {
    const id = shot.value?.id
    if (!id) return null
    busy.value = true
    try {
      lastJob.value = await generationApi.enqueueShot(pid, id, {
        workflow_id: opts.workflowId ?? null,
        check_context: opts.checkContext ?? true,
        allow_ref_drop: opts.allowRefDrop ?? false,
      })
      lastError.value = null
      return lastJob.value
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
      return null
    } finally {
      busy.value = false
      await refresh(pid, id)
      shot.value = await storyApi.shot(pid, id).catch(() => shot.value)
    }
  }

  return {
    shot,
    bill,
    versions,
    lastJob,
    included,
    omitted,
    currentVersion,
    busy,
    lastError,
    load,
    save,
    setCast,
    setProps,
    override,
    addVersion,
    setCurrent,
    enqueue,
    clearError,
  }
})
