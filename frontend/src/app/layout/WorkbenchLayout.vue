<script setup lang="ts">
/**
 * 工作台骨架：标题栏 + Activity Bar + 主区 + 右侧 AI 导演停靠栏 + 底部控制台 + 状态条 +
 * 命令面板。
 *
 * 六条边都是常驻的应用外壳，主区只换内容——这样任何时候都能看出
 * 「我在哪个项目、哪个功能里、系统状态如何、还能去哪」。
 *
 * 控制台（任务框 / 日志框）默认收起，但**始终挂着**：队列的 WS 订阅归它，
 * 所以离开队列页不会把实时通道一起带走，状态条上那个任务标识才可能是真的。
 *
 * **AI 导演也是同一个道理**：它以前内嵌在剧本页与幕流程图页上，一换页就卸载，
 * 手上那几条待审提案跟着消失。现在它是右侧停靠栏（`DirectorDock`），跟着人走。
 *
 * **工程包导出弹窗挂在这里**：它以前只长在项目管理页上，而打开工程之后应用级导航整排
 * 消失（两级互斥），于是「打开了工程却导不出这个工程」。挂在常驻外壳上之后，
 * 标题栏、命令面板、项目概览页三个入口指的都是这一个弹窗。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import ActivityBar from './ActivityBar.vue'
import ConsolePanel from './ConsolePanel.vue'
import DirectorDock from './DirectorDock.vue'
import StatusBar from './StatusBar.vue'
import TitleBar from './TitleBar.vue'
import CommandPalette from '@/shared/ui/CommandPalette.vue'
import ExportPackageDialog from '@/features/packages/ExportPackageDialog.vue'
import OnboardingWizard from '@/features/onboarding/OnboardingWizard.vue'
import { useConsoleStore } from '@/stores/console'
import { useOnboardingStore } from '@/stores/onboarding'
import { useProjectStore } from '@/stores/project'
import { useShellStore } from '@/stores/shell'
import { useSystemStore } from '@/stores/system'

const sys = useSystemStore()
const proj = useProjectStore()
const consolePanel = useConsoleStore()
const shell = useShellStore()
const wiz = useOnboardingStore()
const route = useRoute()
const paletteOpen = ref(false)

const pid = computed(() => (route.params.pid as string | undefined) ?? null)

// 刷新页面或直接深链接进来时，路由只带着 pid，工程信息要自己补齐
watch(pid, (id) => void (id ? proj.ensure(id) : null), { immediate: true })

function onKeydown(e: KeyboardEvent): void {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    paletteOpen.value = !paletteOpen.value
  }
  // Ctrl/Cmd + ` 开合底部控制台——和终端里那个手势一样
  if ((e.ctrlKey || e.metaKey) && e.key === '`') {
    e.preventDefault()
    consolePanel.toggle()
  }
  // Ctrl/Cmd + I 开合右侧 AI 导演——照 AI 编辑器里那个手势。没打开工程时它没有对象可谈
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'i' && pid.value) {
    e.preventDefault()
    shell.toggleDirector()
  }
}

let poll: number | undefined

onMounted(() => {
  void sys.refresh()
  sys.connect()
  // 第一次跑这台机器时自动弹新手引导：状态在后端（onboarding.json），
  // 所以「关掉过 / 走完过 / 跳过过」刷新页面也不会重新弹一次
  void wiz.load().then(() => {
    if (wiz.shouldAutoOpen) wiz.open = true
  })
  // 依赖状态可能在应用运行期间变化（用户启动了 ComfyUI），低频复查
  poll = window.setInterval(() => void sys.refresh(), 15_000)
  window.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  if (poll) window.clearInterval(poll)
  sys.disconnect()
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div class="flex h-full w-full flex-col overflow-hidden">
    <TitleBar :project-id="pid" @open-palette="paletteOpen = true" />
    <div class="flex min-h-0 flex-1">
      <ActivityBar />
      <main class="bg-base-0 flex min-h-0 min-w-0 flex-1 flex-col">
        <RouterView />
      </main>
      <DirectorDock :project-id="pid" />
    </div>
    <ConsolePanel :project-id="pid" />
    <StatusBar />
    <CommandPalette v-model:open="paletteOpen" :project-id="pid" />
    <!--
      导出的是「此刻打开着的这个工程」，所以只在工程内挂。弹窗自己在 open 时出账单
      （`ExportPackageDialog`），这里只负责让它在任何一页都叫得出来。
    -->
    <ExportPackageDialog v-if="pid" v-model:open="shell.exportOpen" :pid="pid" />
    <OnboardingWizard />
  </div>
</template>
