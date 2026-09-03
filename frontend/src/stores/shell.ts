/**
 * 应用外壳自己的开合状态：右侧「AI 导演」停靠栏 + 全局的工程包导出弹窗。
 *
 * 这两件事凑在一个 store 里不是偷懒——它们是同一类东西：**不属于任何一页，
 * 却必须在任何一页都打得开**。
 *
 *   1. **AI 导演不再是剧本页上的一栏**。它要能长期停在右侧，跟着人从剧本走到流程图、
 *      走到分镜板、走到资产库都不消失（`scope` 由当前路由派生，见
 *      `app/layout/DirectorDock.vue`）。开合与宽度因此只能活在外壳里：挂在页面上的状态
 *      一换路由就跟着组件一起没了。
 *   2. **导出工程包在工程里必须找得到**。它以前只长在项目管理页上，而打开工程之后应用级
 *      导航整排消失（两级互斥，见 `app/features.ts::APP_NAV`），于是「我打开了工程，
 *      却导不出这个工程」——那颗按钮只在跳转前的一瞬间点得到。弹窗改挂在常驻外壳上
 *      （`WorkbenchLayout`），入口在标题栏、命令面板与项目概览页。
 *
 * 与 `stores/console.ts` 同一套做法：**只存偏好、不存数据**，读写 localStorage 全程容错
 * （隐私模式下存不下也不该让应用起不来）。导出弹窗**刻意不记**：它是一次性动作，
 * 下次进来自动弹一个要选目录的对话框是惊吓，不是贴心。
 *
 * `appliedTick` 是提案落库后的一声招呼。AI 导演现在停在外壳上，它不知道此刻开着哪一页，
 * 而落库之后幕数、镜头数、衔接、角色都可能变了——所以由关心这件事的页面自己
 * `watch(() => shell.appliedTick)` 去重拉。**只传一个计数，不传内容**：谁该重拉什么，
 * 只有那一页自己知道。
 */

import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

const KEY = 'aivs-shell'
/** 太窄提案卡的 before → after 就折成面条，太宽会把主区挤没；两头都夹住。 */
const MIN_W = 280
const MAX_W = 720
const DEFAULT_W = 360

interface Saved {
  directorOpen?: boolean
  directorWidth?: number
}

function clampWidth(w: number): number {
  if (!Number.isFinite(w)) return DEFAULT_W
  return Math.min(MAX_W, Math.max(MIN_W, Math.round(w)))
}

/** 读偏好：localStorage 不可用或内容坏了，都不该让应用起不来。 */
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

export const useShellStore = defineStore('shell', () => {
  const saved = load()
  /** 右侧 AI 导演停靠栏开着没有。默认关——第一次进来主区不该被挤掉三分之一。 */
  const directorOpen = ref(saved.directorOpen === true)
  const directorWidth = ref(clampWidth(saved.directorWidth ?? DEFAULT_W))
  /** 工程包导出弹窗。不记偏好（见文件开头）。 */
  const exportOpen = ref(false)
  /** 提案落库了几次。页面靠它决定要不要重拉，内容不经过这里。 */
  const appliedTick = ref(0)

  watch([directorOpen, directorWidth], () => {
    try {
      window.localStorage.setItem(
        KEY,
        JSON.stringify({ directorOpen: directorOpen.value, directorWidth: directorWidth.value }),
      )
    } catch {
      // 存不下就算了：这一轮照常能用，只是下次打开回到默认宽度
    }
  })

  function toggleDirector(): void {
    directorOpen.value = !directorOpen.value
  }

  /** 确保停靠栏开着（页面里那颗「问问 AI 导演」按钮走这条，不做开 / 关切换）。 */
  function showDirector(): void {
    directorOpen.value = true
  }

  function closeDirector(): void {
    directorOpen.value = false
  }

  function setDirectorWidth(px: number): void {
    directorWidth.value = clampWidth(px)
  }

  function openExport(): void {
    exportOpen.value = true
  }

  function bumpApplied(): void {
    appliedTick.value += 1
  }

  return {
    directorOpen,
    directorWidth,
    exportOpen,
    appliedTick,
    toggleDirector,
    showDirector,
    closeDirector,
    setDirectorWidth,
    openExport,
    bumpApplied,
    MIN_W,
    MAX_W,
  }
})
