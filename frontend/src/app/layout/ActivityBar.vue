<script setup lang="ts">
/**
 * 左侧 Activity Bar：全局导航。
 *
 * 分两段，段的存在与否由「有没有打开工程」决定：
 *   上段（scope: app）—— 项目管理 / 素材库，永远在，因为它们不依赖工程；
 *   下段（scope: project）—— 按创作流程分组，**只有打开工程时才出现**。
 *
 * 没打开工程时不再画一排灰锁：那些功能此刻根本没有对象可操作，
 * 画出来只是噪音。上段的「项目」就是从工程里返回项目管理的入口。
 * 每项 hover 出现全名与作用，未启用的功能带锁标记。
 */
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { Lock, Settings } from '@lucide/vue'
import {
  APP_NAV_FEATURES,
  GROUPS,
  NAV_LABEL,
  PROJECT_NAV_FEATURES,
  type Feature,
} from '@/app/features'

const route = useRoute()
const pid = computed(() => (route.params.pid as string | undefined) ?? null)

/** 项目内分组：没打开工程时整段为空，模板里连标题都不画。 */
const grouped = computed<{ title: string; items: Feature[] }[]>(() => {
  if (!pid.value) return []
  return GROUPS.map((g) => ({
    title: g.title,
    items: PROJECT_NAV_FEATURES.filter((f) => f.group === g.id),
  })).filter((g) => g.items.length > 0)
})

function tip(f: Feature): string {
  const back = f.id === 'projects' && pid.value ? '（返回项目管理）' : ''
  return `${f.title}${back} · ${f.purpose}${f.ready ? '' : `（${f.milestone} 启用）`}`
}
</script>

<template>
  <nav
    class="bg-base-1 border-line-1 flex w-rail shrink-0 flex-col items-center gap-0.5 overflow-y-auto border-r py-1.5"
  >
    <RouterLink
      v-for="f in APP_NAV_FEATURES"
      :key="f.id"
      :to="{ name: f.route }"
      class="text-fg-3 hover:text-fg-1 hover:bg-base-2 relative flex w-10 flex-col items-center gap-0.5 rounded-sm py-1 transition-colors"
      active-class="text-accent bg-base-2"
      :title="tip(f)"
    >
      <component :is="f.icon" :size="16" :stroke-width="1.6" />
      <span class="w-full truncate text-center text-[9px] leading-none">
        {{ NAV_LABEL[f.id] ?? f.title }}
      </span>
      <Lock v-if="!f.ready" :size="8" class="text-fg-4 absolute top-0.5 right-1" />
    </RouterLink>

    <template v-for="group in grouped" :key="group.title">
      <div class="mt-1.5 flex w-full flex-col items-center">
        <span class="text-fg-4 text-[8px] leading-none tracking-wider">{{ group.title }}</span>
        <div class="bg-line-1 mt-1 h-px w-6" />
      </div>

      <RouterLink
        v-for="f in group.items"
        :key="f.id"
        :to="{ name: f.route, params: { pid } }"
        class="text-fg-3 hover:text-fg-1 hover:bg-base-2 relative flex w-10 flex-col items-center gap-0.5 rounded-sm py-1 transition-colors"
        active-class="text-accent bg-base-2"
        :title="tip(f)"
      >
        <component :is="f.icon" :size="16" :stroke-width="1.6" />
        <span class="w-full truncate text-center text-[9px] leading-none">
          {{ NAV_LABEL[f.id] ?? f.title }}
        </span>
        <Lock v-if="!f.ready" :size="8" class="text-fg-4 absolute top-0.5 right-1" />
      </RouterLink>
    </template>

    <RouterLink
      to="/settings"
      class="text-fg-3 hover:text-fg-1 hover:bg-base-2 mt-auto flex w-10 shrink-0 flex-col items-center gap-0.5 rounded-sm py-1"
      active-class="text-accent bg-base-2"
      title="设置 · 外部依赖状态与实时事件"
    >
      <Settings :size="16" :stroke-width="1.6" />
      <span class="text-[9px] leading-none">设置</span>
    </RouterLink>
  </nav>
</template>
