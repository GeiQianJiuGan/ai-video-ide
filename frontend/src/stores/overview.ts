/**
 * 概览 store：进度总览 + 最近活动 + 连续性检查 + 环境探测。
 *
 * 概览页要回答三件事：现在到哪了、下一步做什么、哪里不对。四个接口分别对应，
 * 一次并发拉齐；其中环境探测会去 ping ComfyUI，探测失败**不是页面失败**——
 * 它只把「哪些路径不受影响」显示出来，所以单独放一个 envError 而不是污染 lastError。
 *
 * 连续性检查刻意做成按需触发（continuity 要遍历全部镜头），不进 load()。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  overviewApi,
  type ActivityItem,
  type ContinuityReport,
  type EnvironmentStatus,
  type OverviewSummary,
} from '@/shared/api/overview'

export const useOverviewStore = defineStore('overview', () => {
  const summary = ref<OverviewSummary | null>(null)
  const activity = ref<ActivityItem[]>([])
  const environment = ref<EnvironmentStatus | null>(null)
  const continuity = ref<ContinuityReport | null>(null)
  const busy = ref(false)
  const checking = ref(false)
  const lastError = ref<ApiError | null>(null)
  /** 环境探测自己的失败，不该让整页显示「加载失败」。 */
  const envError = ref<ApiError | null>(null)

  /** 空工程：不画空图表，改画下一步引导。 */
  const empty = computed(() => (summary.value?.counts.shots ?? 0) === 0)

  function clearError(): void {
    lastError.value = null
  }

  async function load(pid: string): Promise<void> {
    busy.value = true
    try {
      const [s, a] = await Promise.all([overviewApi.summary(pid), overviewApi.activity(pid, 10)])
      summary.value = s
      activity.value = a
      lastError.value = null
    } catch (err) {
      summary.value = null
      activity.value = []
      lastError.value = err instanceof ApiError ? err : null
    } finally {
      busy.value = false
    }
    try {
      environment.value = await overviewApi.environment(pid)
      envError.value = null
    } catch (err) {
      environment.value = null
      envError.value = err instanceof ApiError ? err : null
    }
  }

  async function check(pid: string): Promise<void> {
    checking.value = true
    try {
      continuity.value = await overviewApi.continuity(pid)
      lastError.value = null
    } catch (err) {
      continuity.value = null
      lastError.value = err instanceof ApiError ? err : null
    } finally {
      checking.value = false
    }
  }

  return {
    summary,
    activity,
    environment,
    continuity,
    busy,
    checking,
    lastError,
    envError,
    empty,
    load,
    check,
    clearError,
  }
})
