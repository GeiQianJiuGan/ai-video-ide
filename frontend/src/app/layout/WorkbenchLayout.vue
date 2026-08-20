<script setup lang="ts">
/**
 * 工作台骨架：标题栏 + Activity Bar + 主区 + 状态条 + 命令面板。
 *
 * 四条边都是常驻的应用外壳，主区只换内容——这样任何时候都能看出
 * 「我在哪个项目、哪个功能里、系统状态如何、还能去哪」。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import ActivityBar from './ActivityBar.vue'
import StatusBar from './StatusBar.vue'
import TitleBar from './TitleBar.vue'
import CommandPalette from '@/shared/ui/CommandPalette.vue'
import { useSystemStore } from '@/stores/system'

const sys = useSystemStore()
const route = useRoute()
const paletteOpen = ref(false)

const pid = computed(() => (route.params.pid as string | undefined) ?? null)

function onKeydown(e: KeyboardEvent): void {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    paletteOpen.value = !paletteOpen.value
  }
}

let poll: number | undefined

onMounted(() => {
  void sys.refresh()
  sys.connect()
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
    </div>
    <StatusBar />
    <CommandPalette v-model:open="paletteOpen" :project-id="pid" />
  </div>
</template>
