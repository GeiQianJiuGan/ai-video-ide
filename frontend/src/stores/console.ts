/**
 * 底部控制台的开合状态（任务框 / 日志框）。
 *
 * 队列不再是左栏里的一个菜单项：它是**一直在跑的东西**，属于底部控制台，
 * 而不是一个要「走过去看」的页面。所以这个 store 只管三件事——开没开、
 * 在哪个页签、多高——真正的数据仍分别来自 `stores/queue.ts` 与 `stores/system.ts`。
 *
 * 三个刻意的取舍：
 *   1. **状态记在 localStorage**：控制台的高度是布局偏好，刷新页面不该丢
 *      （和 `SplitPane` 记分栏比例是同一个道理）。读写都容错，坏值一律退回默认。
 *   2. **只存偏好，不存数据**。任务与日志都是「会自己变」的，缓存它们只会
 *      让重连后对不上（事件可丢失，硬约束见 docs/03 §5）。
 *   3. **`openWith` 而不是 `open = true`**：状态条上的两个入口（任务标识、事件计数）
 *      分别要落到不同页签，「打开」和「落到哪一页」必须是一次动作。
 */

import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

export type ConsoleTab = 'jobs' | 'logs'

const KEY = 'aivs-console'
/** 太矮就一行都看不见，太高会把主区挤没；两头都夹住。 */
const MIN_H = 120
const MAX_H = 560
const DEFAULT_H = 220

interface Saved {
  open?: boolean
  tab?: ConsoleTab
  height?: number
}

function clampHeight(h: number): number {
  if (!Number.isFinite(h)) return DEFAULT_H
  return Math.min(MAX_H, Math.max(MIN_H, Math.round(h)))
}

/** 读偏好：localStorage 不可用（隐私模式）或内容坏了都不该让应用起不来。 */
function load(): Saved {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Saved
    return typeof parsed === 'object' && parsed !== null ? parsed : {}
  } catch {
    return {}
  }
}

export const useConsoleStore = defineStore('console', () => {
  const saved = load()
  const open = ref(saved.open === true)
  const tab = ref<ConsoleTab>(saved.tab === 'logs' ? 'logs' : 'jobs')
  const height = ref(clampHeight(saved.height ?? DEFAULT_H))

  watch([open, tab, height], () => {
    try {
      window.localStorage.setItem(
        KEY,
        JSON.stringify({ open: open.value, tab: tab.value, height: height.value }),
      )
    } catch {
      // 存不下就算了：控制台照常能用，只是下次打开回到默认高度
    }
  })

  /** 打开并落到指定页签；已经开在这一页了就收起来（同一个入口点两次 = 开 / 关）。 */
  function openWith(next: ConsoleTab): void {
    if (open.value && tab.value === next) {
      open.value = false
      return
    }
    tab.value = next
    open.value = true
  }

  function toggle(): void {
    open.value = !open.value
  }

  function close(): void {
    open.value = false
  }

  function setHeight(px: number): void {
    height.value = clampHeight(px)
  }

  return { open, tab, height, openWith, toggle, close, setHeight, MIN_H, MAX_H }
})
