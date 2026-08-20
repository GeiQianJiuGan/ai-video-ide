<script setup lang="ts">
/**
 * 左侧 Activity Bar：全局导航。
 *
 * 与旧版的区别：按创作流程分组（素材 / 叙事 / 生成 / 成片），
 * 每项 hover 出现全名与作用说明，未启用的功能带锁标记——
 * 功能是否存在、能不能用，站在导航栏就能看清。
 */
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { House, Lock, Settings, FolderOpen } from '@lucide/vue'
import { GROUPS, NAV_FEATURES, NAV_LABEL, type Feature } from '@/app/features'

const route = useRoute()
const pid = computed(() => (route.params.pid as string | undefined) ?? null)

const grouped = computed<{ title: string; items: Feature[] }[]>(() =>
  GROUPS.map((g) => ({
    title: g.title,
    items: NAV_FEATURES.filter((f) => f.group === g.id),
  })).filter((g) => g.items.length > 0),
)

function tip(f: Feature): string {
  return `${f.title} · ${f.purpose}${f.ready ? '' : `（${f.milestone} 启用）`}`
}
</script>

<template>
  <nav
    class="bg-base-1 border-line-1 flex w-rail shrink-0 flex-col items-center gap-0.5 overflow-y-auto border-r py-1.5"
  >
    <RouterLink
      to="/"
      class="text-fg-3 hover:text-fg-1 hover:bg-base-2 flex w-10 flex-col items-center gap-0.5 rounded-sm py-1"
      :class="route.path === '/' && 'text-accent bg-base-2'"
      title="工作台首页 · 核心链路总览与全部功能入口"
    >
      <House :size="16" :stroke-width="1.6" />
      <span class="text-[9px] leading-none">首页</span>
    </RouterLink>

    <RouterLink
      to="/projects"
      class="text-fg-3 hover:text-fg-1 hover:bg-base-2 flex w-10 flex-col items-center gap-0.5 rounded-sm py-1"
      active-class="text-accent bg-base-2"
      title="项目管理 · 新建或打开工程目录"
    >
      <FolderOpen :size="16" :stroke-width="1.6" />
      <span class="text-[9px] leading-none">项目</span>
    </RouterLink>

    <template v-for="group in grouped" :key="group.title">
      <div class="mt-1.5 flex w-full flex-col items-center">
        <span class="text-fg-4 text-[8px] leading-none tracking-wider">{{ group.title }}</span>
        <div class="bg-line-1 mt-1 h-px w-6" />
      </div>

      <component
        :is="pid ? RouterLink : 'div'"
        v-for="f in group.items"
        :key="f.id"
        :to="pid ? { name: f.route, params: { pid } } : undefined"
        class="relative flex w-10 flex-col items-center gap-0.5 rounded-sm py-1 transition-colors"
        :class="
          pid
            ? 'text-fg-3 hover:text-fg-1 hover:bg-base-2'
            : 'text-fg-4 cursor-not-allowed opacity-50'
        "
        :active-class="pid ? 'text-accent bg-base-2' : undefined"
        :title="pid ? tip(f) : `${f.title} · 需要先打开项目`"
      >
        <component :is="f.icon" :size="16" :stroke-width="1.6" />
        <span class="w-full truncate text-center text-[9px] leading-none">
          {{ NAV_LABEL[f.id] ?? f.title }}
        </span>
        <Lock v-if="!f.ready" :size="8" class="text-fg-4 absolute top-0.5 right-1" />
      </component>
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
