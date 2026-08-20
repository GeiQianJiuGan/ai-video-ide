<script setup lang="ts">
/** 工作台骨架：Activity Bar + 主区（RouterView） + 状态条。 */
import { onMounted, onUnmounted } from 'vue'
import { RouterView } from 'vue-router'
import ActivityBar from './ActivityBar.vue'
import StatusBar from './StatusBar.vue'
import { useSystemStore } from '@/stores/system'

const sys = useSystemStore()
let poll: number | undefined

onMounted(() => {
  void sys.refresh()
  sys.connect()
  // 依赖状态可能在应用运行期间变化（用户启动了 ComfyUI），低频复查
  poll = window.setInterval(() => void sys.refresh(), 15_000)
})

onUnmounted(() => {
  if (poll) window.clearInterval(poll)
  sys.disconnect()
})
</script>

<template>
  <div class="flex h-full w-full flex-col overflow-hidden">
    <div class="flex min-h-0 flex-1">
      <ActivityBar />
      <main class="bg-base-0 flex min-h-0 min-w-0 flex-1 flex-col">
        <RouterView />
      </main>
    </div>
    <StatusBar />
  </div>
</template>
