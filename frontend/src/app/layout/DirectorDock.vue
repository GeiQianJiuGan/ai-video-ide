<script setup lang="ts">
/**
 * 右侧「AI 导演」停靠栏——**全局的一栏，不属于任何一页**。
 *
 * 以前 `DirectorPanel` 只挂在剧本页与幕流程图页上：一离开那两页它就卸载，正在写的那段话、
 * 手上那几条待审提案跟着组件一起消失。可导演这件事本身是跨页的——在分镜板上看着卡片
 * 说「这两镜之间补一段转场」，在资产库里看着缺图说「给阿岚补一张四视图」，
 * 都不该先跑回剧本页。所以它搬到常驻外壳里（`WorkbenchLayout`），照
 * `ConsolePanel` 的老规矩：**开合与宽度记在 store 里**（`stores/shell.ts`），换页不动。
 *
 * 三个刻意的取舍：
 *   1. **`scope` 由当前路由派生，不让用户选**。它只影响后端拼系统提示词里那一句
 *      「用户现在在哪一页」（`SCOPE_HINT`，不落库、不分会话），所以「在哪儿就是哪个 scope」
 *      是唯一说得通的口径；多一个下拉只会让人以为换了会话。剧本页 → `script`，其余 → `flow`。
 *   2. **一个实例，一份会话**。两页各挂一个的做法在停靠栏时代会同时活着两个组件、
 *      共用一个 store，同一段话画两遍。所以那两页的内嵌栏已经撤掉，改成打开这一栏。
 *   3. **落库后不由这一栏去重拉页面**：它不知道此刻开着哪一页。改成
 *      `shell.bumpApplied()`，关心的页面自己 watch（见 `stores/shell.ts`）。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import DirectorPanel from '@/features/director/DirectorPanel.vue'
import type { DirectorScope } from '@/shared/api/director'
import { useShellStore } from '@/stores/shell'

defineProps<{ projectId: string | null }>()

const route = useRoute()
const shell = useShellStore()

/** 剧本页是「讲剧情、拆幕镜」，其余页面都更像「改结构」。 */
const scope = computed<DirectorScope>(() => (route.name === 'story' ? 'script' : 'flow'))

/** chip 只把话填进输入框，所以按当前这一页给最顺手的那几句。 */
const QUICK: Record<DirectorScope, string[]> = {
  script: [
    '我讲一段剧情，你拆成幕和镜头：',
    '按现在这几幕往下接一幕',
    '给第 1 幕补齐镜头 prompt',
    '按首尾帧规范重写这一幕的 prompt',
  ],
  flow: [
    '看一遍现在几幕，缺角色或地点就提出来',
    '把第 2 幕到第 3 幕的衔接改成末帧续接',
    '剧本里出现了但角色库里没有的人，建出来并出一张四视图',
    '给还没有描述的素材补上「这张图长什么样」',
  ],
}

const PLACEHOLDER: Record<DirectorScope, string> = {
  script: '讲讲这段戏要什么。Enter 发送，Shift+Enter 换行',
  flow: '要它做什么？Enter 发送，Shift+Enter 换行',
}

/** 宽度是拖出来的。指针事件在这里收尾，`pointercapture` 让指针滑出窗口也不丢。 */
function startResize(e: PointerEvent): void {
  e.preventDefault()
  const startX = e.clientX
  const startW = shell.directorWidth
  // 往左拖 = 变宽，所以是 startX - clientX
  const move = (m: PointerEvent): void => shell.setDirectorWidth(startW + (startX - m.clientX))
  const up = (): void => {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
}
</script>

<template>
  <section
    v-if="projectId && shell.directorOpen"
    class="relative flex shrink-0 flex-col"
    :style="{ width: `${shell.directorWidth}px` }"
  >
    <!-- 左边缘那条拖拽把手。1px 太难瞄，所以热区 4px、只在悬停时才显色 -->
    <div
      class="hover:bg-accent/40 absolute inset-y-0 -left-0.5 z-10 w-1 cursor-col-resize"
      title="拖动改变这一栏的宽度"
      @pointerdown="startResize"
    />
    <DirectorPanel
      :key="projectId"
      class="min-h-0 flex-1"
      :pid="projectId"
      :scope="scope"
      title="AI 导演"
      :placeholder="PLACEHOLDER[scope]"
      :quick-actions="QUICK[scope]"
      closable
      @close="shell.closeDirector()"
      @applied="shell.bumpApplied()"
    />
  </section>
</template>
