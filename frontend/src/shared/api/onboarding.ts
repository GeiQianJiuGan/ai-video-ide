/**
 * 新手引导接口（Step 10）。
 *
 * 只有两件事：**状态**（走到哪一步了，落在 `.runtime/onboarding.json`）与**演示工程**
 * （先账单 `demo/plan`，确认了再 `demo`）。
 *
 * 环境状态这里一个字段都没有——ComfyUI 地址、探测、预设清单全部走已有的
 * `/system/deps`、`/settings`、`/settings/probe`、`/settings/presets`，向导只是把它们
 * 按顺序摆出来，不在这一层重复实现一遍。
 */

import { api } from './client'
import type { Project } from './projects'

export type OnboardingStep = 'welcome' | 'demo' | 'service' | 'bind' | 'tour'

export interface OnboardingState {
  completed: boolean
  skipped: boolean
  step: OnboardingStep
  /** 步骤顺序由后端给，前端不写第二份。 */
  steps: OnboardingStep[]
  /** 状态文件还不存在——首次运行的判断就是它，自动弹窗认的也是它。 */
  first_run: boolean
  /** 已经建过演示工程的话是那个目录，否则空串。 */
  demo_dir: string
  demo_seeded_at: string
  /** 留空时演示工程会落在哪（文档目录下那一份）。 */
  default_demo_dir: string
  /** `demo_dir` 里现在真的有一个工程（认 `project.aivs.json`）。 */
  demo_exists: boolean
  saved_at?: string
}

/** 账单里的一行：会建几个什么。 */
export interface DemoPlanItem {
  kind: string
  label: string
  count: number
}

/** 演示工程账单。**一个字节都不写**，所以可以随便点开看。 */
export interface DemoPlan {
  dir: string
  exists: boolean
  /** `open` = 目录里已经有工程了，只打开不重建。 */
  action: 'open' | 'create'
  items: DemoPlanItem[]
  estimated_bytes: number
  /** 原样显示——其中一条写明「演示工程里没有任何已生成的版本」。 */
  warnings: string[]
}

export interface DemoSummary {
  characters: number
  locations: number
  props: number
  scenes: number
  shots: number
  links: number
}

export interface DemoResult {
  project: Project
  /** false = 目录里本来就有，这次只是打开。第二次点不会重建。 */
  created: boolean
  summary: DemoSummary
}

export interface StatePatch {
  step?: OnboardingStep
  completed?: boolean
  skipped?: boolean
}

export const onboardingApi = {
  get: () => api.get<OnboardingState>('/onboarding'),
  patch: (patch: StatePatch) => api.patch<OnboardingState>('/onboarding', patch),
  /** 账单：目录在哪、会建什么、多大。不写盘。 */
  planDemo: (dir?: string) => api.post<DemoPlan>('/onboarding/demo/plan', { dir: dir || null }),
  /** 落地演示工程；已经有了就只打开。 */
  createDemo: (dir?: string) => api.post<DemoResult>('/onboarding/demo', { dir: dir || null }),
}
