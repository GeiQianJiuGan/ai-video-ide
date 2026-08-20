<script setup lang="ts">
/** 左侧 Activity Bar：全局导航。48px 宽，图标 + 极小字号文字。 */
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  Boxes,
  Clapperboard,
  FolderOpen,
  Gauge,
  Images,
  LayoutGrid,
  ListVideo,
  MapPinned,
  Settings,
  Users,
  Workflow,
  ScrollText,
} from '@lucide/vue'

const route = useRoute()
const pid = computed(() => (route.params.pid as string | undefined) ?? null)

const GLOBAL = [{ to: '/', icon: FolderOpen, label: '项目' }]

const PROJECT = [
  { name: 'dashboard', icon: Gauge, label: '概览' },
  { name: 'characters', icon: Users, label: '角色' },
  { name: 'locations', icon: MapPinned, label: '场景' },
  { name: 'props', icon: Boxes, label: '道具' },
  { name: 'story', icon: ScrollText, label: '剧本' },
  { name: 'storyboard', icon: LayoutGrid, label: '分镜' },
  { name: 'timeline', icon: Clapperboard, label: '时间线' },
  { name: 'assets', icon: Images, label: '资产' },
  { name: 'workflows', icon: Workflow, label: 'Workflow' },
  { name: 'queue', icon: ListVideo, label: '队列' },
] as const
</script>

<template>
  <nav
    class="bg-base-1 border-line-1 flex w-rail shrink-0 flex-col items-center gap-0.5 border-r py-1.5"
  >
    <RouterLink
      v-for="item in GLOBAL"
      :key="item.to"
      :to="item.to"
      class="text-fg-3 hover:text-fg-1 hover:bg-base-2 flex w-10 flex-col items-center gap-0.5 rounded-sm py-1"
      :class="route.path === '/' && 'text-accent bg-base-2'"
    >
      <component :is="item.icon" :size="16" :stroke-width="1.6" />
      <span class="text-[9px] leading-none">{{ item.label }}</span>
    </RouterLink>

    <div class="bg-line-1 my-1 h-px w-6" />

    <template v-if="pid">
      <RouterLink
        v-for="item in PROJECT"
        :key="item.name"
        :to="{ name: item.name, params: { pid } }"
        class="text-fg-3 hover:text-fg-1 hover:bg-base-2 flex w-10 flex-col items-center gap-0.5 rounded-sm py-1 transition-colors"
        active-class="text-accent bg-base-2"
      >
        <component :is="item.icon" :size="16" :stroke-width="1.6" />
        <span class="w-full truncate text-center text-[9px] leading-none">{{ item.label }}</span>
      </RouterLink>
    </template>
    <p v-else class="text-fg-4 px-1 text-center text-[9px] leading-tight">打开项目<br />后可用</p>

    <RouterLink
      to="/settings"
      class="text-fg-3 hover:text-fg-1 hover:bg-base-2 mt-auto flex w-10 flex-col items-center gap-0.5 rounded-sm py-1"
      active-class="text-accent bg-base-2"
    >
      <Settings :size="16" :stroke-width="1.6" />
      <span class="text-[9px] leading-none">设置</span>
    </RouterLink>
  </nav>
</template>
