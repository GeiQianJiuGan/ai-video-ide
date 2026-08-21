<script setup lang="ts">
/**
 * 功能页标题条：图标 + 标题 + 里程碑 + 能力要求徽标 + 一句话作用。
 *
 * 从 FeatureView 里抽出来，好让「已接后端的实页面」和「还是外壳的页面」
 * 顶部长得一模一样——注册表（app/features.ts）仍是这些文案的唯一真源。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppBadge from './AppBadge.vue'
import { REQUIREMENT_LABEL, featureByRoute, type Requirement } from '@/app/features'
import { useSystemStore } from '@/stores/system'

const props = defineProps<{
  /** 留空时取当前路由名。 */
  route?: string
}>()

const currentRoute = useRoute()
const sys = useSystemStore()

const feature = computed(() =>
  featureByRoute(props.route ?? (currentRoute.name as string | undefined)),
)

function satisfied(req: Requirement): boolean {
  if (req === 'backend') return sys.health !== null
  return sys.deps.find((d) => d.name === req)?.ok ?? false
}
</script>

<template>
  <header v-if="feature" class="border-line-1 bg-base-1 shrink-0 border-b px-3 py-2">
    <div class="flex items-center gap-2">
      <component :is="feature.icon" :size="14" :stroke-width="1.6" class="text-accent" />
      <h1 class="text-fg-1 text-sm font-medium">{{ feature.title }}</h1>
      <AppBadge tone="accent">{{ feature.milestone }}</AppBadge>
      <AppBadge v-for="r in feature.requires" :key="r" :tone="satisfied(r) ? 'ok' : 'warn'">
        {{ REQUIREMENT_LABEL[r] }}
      </AppBadge>
      <div class="ml-auto flex items-center gap-1"><slot name="actions" /></div>
    </div>
    <p class="text-fg-3 mt-1 text-xs">{{ feature.purpose }}</p>
  </header>
</template>
