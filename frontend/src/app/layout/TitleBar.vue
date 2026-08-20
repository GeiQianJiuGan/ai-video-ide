<script setup lang="ts">
/**
 * 顶部标题栏：应用身份 + 当前位置面包屑 + 命令面板入口。
 *
 * 这是「应用感」的锚点：任何时候都能看出自己在哪个项目、哪个功能里。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { ChevronRight, Command, Film } from '@lucide/vue'
import { featureByRoute } from '@/app/features'

const props = defineProps<{ projectId: string | null }>()
const emit = defineEmits<{ openPalette: [] }>()

const route = useRoute()
const feature = computed(() => featureByRoute(route.name as string | undefined))

const place = computed(() => {
  if (feature.value) return feature.value.title
  if (route.name === 'settings') return '设置'
  if (route.name === 'projects') return '项目管理'
  return '工作台'
})

const projectLabel = computed(() => (props.projectId ? props.projectId : '未打开项目'))
</script>

<template>
  <header
    class="bg-base-2 border-line-1 flex h-row shrink-0 items-center gap-2 border-b px-2 text-xs"
  >
    <span class="text-accent flex shrink-0 items-center gap-1.5 font-medium">
      <Film :size="13" :stroke-width="1.8" />AI Video Studio
    </span>

    <span class="text-fg-4">|</span>

    <nav class="flex min-w-0 items-center gap-1">
      <span class="text-fg-3 truncate" :class="!projectId && 'text-fg-4'">{{ projectLabel }}</span>
      <ChevronRight :size="12" class="text-fg-4 shrink-0" />
      <span class="text-fg-1 truncate">{{ place }}</span>
    </nav>

    <button
      type="button"
      class="text-fg-3 hover:text-fg-1 border-line-1 hover:border-line-2 ml-auto flex h-5 shrink-0 items-center gap-1.5 rounded-sm border px-2 text-2xs"
      title="打开命令面板，搜索并跳转到任意功能"
      @click="emit('openPalette')"
    >
      <Command :size="10" />搜索功能
      <kbd class="text-fg-4 font-mono">Ctrl K</kbd>
    </button>
  </header>
</template>
