/**
 * 新手引导 store：向导的开合 + 状态文件的读写 + 演示工程。
 *
 * 两件事刻意留在这里而不是组件里：
 *
 *   1. **步骤记在后端**（`PATCH /onboarding`），所以中途关掉再打开接着走；
 *      组件只管画当前那一步。
 *   2. **失败不吞**——每个动作把 ApiError 留在 `lastError` 里，向导原样显示 suggestions。
 *
 * `open` 与状态文件无关：向导关掉不等于走完了，所以它只是本地开合。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  onboardingApi,
  type DemoPlan,
  type DemoResult,
  type OnboardingState,
  type OnboardingStep,
} from '@/shared/api/onboarding'

export const STEP_LABEL: Record<OnboardingStep, string> = {
  welcome: '这是什么',
  demo: '演示工程',
  service: '连上生成服务',
  bind: '绑定预设或 API',
  tour: '功能巡览',
}

export const useOnboardingStore = defineStore('onboarding', () => {
  const state = ref<OnboardingState | null>(null)
  const plan = ref<DemoPlan | null>(null)
  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)
  /** 向导这一层是不是展开着。与 `completed` 无关——关掉不等于走完。 */
  const open = ref(false)

  const steps = computed<OnboardingStep[]>(() => state.value?.steps ?? ['welcome'])
  const step = computed<OnboardingStep>(() => state.value?.step ?? 'welcome')
  const stepIndex = computed(() => Math.max(0, steps.value.indexOf(step.value)))
  const isLast = computed(() => stepIndex.value >= steps.value.length - 1)
  /** 首次运行且没走完也没跳过——自动弹窗认的就是这一句。 */
  const shouldAutoOpen = computed(
    () => !!state.value?.first_run && !state.value.completed && !state.value.skipped,
  )

  function keep(next: OnboardingState): OnboardingState {
    state.value = next
    lastError.value = null
    return next
  }

  function fail(err: unknown): never {
    lastError.value = err instanceof ApiError ? err : null
    throw err
  }

  /** 读一次状态。失败不弹窗、不拦路——引导不该挡住应用启动。 */
  async function load(): Promise<void> {
    try {
      keep(await onboardingApi.get())
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
    }
  }

  async function setStep(next: OnboardingStep): Promise<void> {
    // 先把界面切过去，落库只是记住进度——网络慢不该卡住「下一步」。
    if (state.value) state.value = { ...state.value, step: next }
    try {
      keep(await onboardingApi.patch({ step: next }))
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
    }
  }

  async function next(): Promise<void> {
    if (isLast.value) return complete()
    const target = steps.value[stepIndex.value + 1]
    if (target) await setStep(target)
  }

  async function prev(): Promise<void> {
    const target = steps.value[stepIndex.value - 1]
    if (target) await setStep(target)
  }

  async function complete(): Promise<void> {
    open.value = false
    try {
      keep(await onboardingApi.patch({ completed: true }))
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
    }
  }

  async function skip(): Promise<void> {
    open.value = false
    try {
      keep(await onboardingApi.patch({ skipped: true }))
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
    }
  }

  /**
   * 从设置页 / 命令面板 / 起始页重开。
   *
   * 默认回到第一步，并把 `completed` / `skipped` 清掉——不然下一次 `state()` 读回来还是
   * 「已走完」，进度条上那一行会和眼前的向导对不上。起始页那个「打开演示项目」传
   * `'demo'` 直接落在演示工程那一步：从空的最近列表点过来的人要的是那一步，不是开场白。
   */
  async function reopen(at: OnboardingStep = 'welcome'): Promise<void> {
    open.value = true
    try {
      keep(await onboardingApi.patch({ step: at, completed: false, skipped: false }))
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
    }
  }

  /** 账单。**不写盘**，所以可以随便点。 */
  async function loadPlan(dir?: string): Promise<DemoPlan> {
    busy.value = true
    try {
      plan.value = await onboardingApi.planDemo(dir)
      lastError.value = null
      return plan.value
    } catch (err) {
      return fail(err)
    } finally {
      busy.value = false
    }
  }

  async function createDemo(dir?: string): Promise<DemoResult> {
    busy.value = true
    try {
      const result = await onboardingApi.createDemo(dir)
      lastError.value = null
      await load()
      return result
    } catch (err) {
      return fail(err)
    } finally {
      busy.value = false
    }
  }

  function clearError(): void {
    lastError.value = null
  }

  return {
    state,
    plan,
    busy,
    lastError,
    open,
    steps,
    step,
    stepIndex,
    isLast,
    shouldAutoOpen,
    load,
    setStep,
    next,
    prev,
    complete,
    skip,
    reopen,
    loadPlan,
    createDemo,
    clearError,
  }
})
