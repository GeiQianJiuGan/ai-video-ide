<script setup lang="ts">
/**
 * 功能页统一骨架。
 *
 * 刻意做成「应用形态」而不是占位说明页：真实的标题栏、工具栏、三栏工作区都在，
 * 每个区域用空状态说明将出现什么。功能未启用时显示能力锁，
 * 但绝不伪造数据——看起来能用其实是假的，比明确说「还没做」更糟。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { Lock } from '@lucide/vue'
import AppPanel from './AppPanel.vue'
import AppButton from './AppButton.vue'
import AppBadge from './AppBadge.vue'
import EmptyState from './EmptyState.vue'
import { REQUIREMENT_LABEL, featureByRoute, type Requirement } from '@/app/features'
import { useSystemStore } from '@/stores/system'

const route = useRoute()
const sys = useSystemStore()

const feature = computed(() => featureByRoute(route.name as string | undefined))

function satisfied(req: Requirement): boolean {
  if (req === 'backend') return sys.health !== null
  return sys.deps.find((d) => d.name === req)?.ok ?? false
}
</script>

<template>
  <div v-if="feature" class="flex min-h-0 flex-1 flex-col">
    <header class="border-line-1 bg-base-1 shrink-0 border-b px-3 py-2">
      <div class="flex items-center gap-2">
        <component :is="feature.icon" :size="14" :stroke-width="1.6" class="text-accent" />
        <h1 class="text-fg-1 text-sm font-medium">{{ feature.title }}</h1>
        <AppBadge tone="accent">{{ feature.milestone }}</AppBadge>
        <AppBadge v-for="r in feature.requires" :key="r" :tone="satisfied(r) ? 'ok' : 'warn'">
          {{ REQUIREMENT_LABEL[r] }}
        </AppBadge>
      </div>
      <p class="text-fg-3 mt-1 text-xs">{{ feature.purpose }}</p>
    </header>

    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1 border-b px-2">
      <AppButton
        v-for="a in feature.actions"
        :key="a.label"
        size="sm"
        :variant="a.primary ? 'primary' : 'ghost'"
        :disabled="!feature.ready"
        :title="a.hint"
      >
        {{ a.label }}
      </AppButton>
      <span v-if="!feature.ready" class="text-fg-4 ml-auto flex items-center gap-1 text-2xs">
        <Lock :size="10" />{{ feature.milestone }} 启用后可操作
      </span>
    </div>

    <div class="flex min-h-0 flex-1 gap-2 p-2">
      <AppPanel v-if="feature.panels.left" :title="feature.panels.left.title" class="w-56 shrink-0">
        <EmptyState title="尚无内容" :body="feature.panels.left.body" />
      </AppPanel>

      <div class="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
        <AppPanel :title="feature.panels.main.title" class="min-h-0 flex-1">
          <div class="flex min-h-0 flex-1 flex-col">
            <EmptyState
              :title="feature.panels.main.title + ' · 将在这里工作'"
              :body="feature.panels.main.body"
            />
            <div class="border-line-1 mx-6 mb-6 border-t pt-3">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">完成这一步之后</p>
              <ul class="text-fg-2 mt-1.5 space-y-1 text-xs">
                <li v-for="o in feature.outcome" :key="o" class="flex gap-2">
                  <span class="text-accent">→</span><span>{{ o }}</span>
                </li>
              </ul>
            </div>
          </div>
        </AppPanel>

        <AppPanel
          v-if="feature.panels.bottom"
          :title="feature.panels.bottom.title"
          class="h-32 shrink-0"
        >
          <EmptyState title="尚无内容" :body="feature.panels.bottom.body" />
        </AppPanel>
      </div>

      <AppPanel
        v-if="feature.panels.right"
        :title="feature.panels.right.title"
        class="w-72 shrink-0"
      >
        <EmptyState title="尚无选中项" :body="feature.panels.right.body" />
      </AppPanel>
    </div>
  </div>

  <div v-else class="text-fg-3 flex min-h-0 flex-1 items-center justify-center text-xs">
    未登记的功能页：{{ String(route.name) }}
  </div>
</template>
