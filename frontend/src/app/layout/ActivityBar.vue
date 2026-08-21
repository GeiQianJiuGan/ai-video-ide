<script setup lang="ts">
/**
 * 左侧 Activity Bar：全局导航。
 *
 * **两级互斥**，段的存在由「有没有打开工程」决定，两段绝不同时出现：
 *   没打开工程 —— 只有 scope: app 的项目管理 / 素材库。这是应用的起始状态；
 *   打开工程后 —— 顶上一个「← 项目列表」，下面全是按创作流程分组的项目内功能。
 *     项目管理与素材库此刻**消失**：它们不属于这个工程，混在同一条栏里会让
 *     「点一下就掉出工程」变成随时可能踩到的地雷。
 *
 * 退出工程只有一个入口，就是顶上那一条。它做的事是「离开这个工作区」而不是
 * 「关掉后端的工程」：队列继续在后台跑（这也是控制台常驻的理由），回来时
 * 从最近列表点进去就接着用，不会打断正在生成的镜头。
 *
 * 还有第三种状态：**工程还开着，但人在应用级页面上**（点了设置，或从某个对话框
 * 跳去素材库）。那不是退出，所以顶上给一条「返回工程」把人送回去——否则去一趟
 * 设置页就得从最近列表重新找回自己的工程，那和退出没有区别。
 *
 * 没打开工程时不画一排灰锁：那些功能此刻根本没有对象可操作，画出来只是噪音。
 * 每项 hover 出现全名与作用，未启用的功能带锁标记。
 */
import { computed } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ArrowLeft, CornerUpLeft, Lock, Settings } from '@lucide/vue'
import {
  APP_NAV_FEATURES,
  GROUPS,
  NAV_LABEL,
  PROJECT_NAV_FEATURES,
  type Feature,
} from '@/app/features'
import { useProjectStore } from '@/stores/project'

const route = useRoute()
const router = useRouter()
const proj = useProjectStore()

const pid = computed(() => (route.params.pid as string | undefined) ?? null)

/** 应用级功能只在没打开工程时出现——这就是「两级」的那道界。 */
const appItems = computed<Feature[]>(() => (pid.value ? [] : APP_NAV_FEATURES))

/** 项目内分组：没打开工程时整段为空，模板里连标题都不画。 */
const grouped = computed<{ title: string; items: Feature[] }[]>(() => {
  if (!pid.value) return []
  return GROUPS.map((g) => ({
    title: g.title,
    items: PROJECT_NAV_FEATURES.filter((f) => f.group === g.id),
  })).filter((g) => g.items.length > 0)
})

/** 人在应用级页面上，但工程还开着——给一条回去的路，不然设置页就是个单向门。 */
const resumable = computed(() => (pid.value ? null : proj.current))

function tip(f: Feature): string {
  return `${f.title} · ${f.purpose}${f.ready ? '' : `（${f.milestone} 启用）`}`
}

/**
 * 退出工程。清掉前端持有的「当前工程」，但**不调后端的 close**——
 * 那会关掉 SQLite 连接却不停 pump，正在跑的生成会断在半路。
 */
function leaveProject(): void {
  proj.leave()
  void router.push({ name: 'projects' })
}
</script>

<template>
  <nav
    class="bg-base-1 border-line-1 flex w-rail shrink-0 flex-col items-center gap-0.5 overflow-y-auto border-r py-1.5"
  >
    <!-- 打开工程后：唯一的出口。它替掉了原来那两个应用级菜单 -->
    <button
      v-if="pid"
      type="button"
      class="text-fg-3 hover:text-fg-1 hover:bg-base-2 flex w-10 shrink-0 flex-col items-center gap-0.5 rounded-sm py-1 transition-colors"
      :title="`返回项目列表 · 离开这个工程的工作区（已入队的生成任务继续在后台跑，回来时接着看）${proj.current ? ` · 当前：${proj.current.name}` : ''}`"
      @click="leaveProject()"
    >
      <ArrowLeft :size="16" :stroke-width="1.6" />
      <span class="w-full truncate text-center text-[9px] leading-none">项目列表</span>
    </button>

    <!-- 工程还开着但人在应用级页面上：回去的路 -->
    <RouterLink
      v-if="resumable"
      :to="{ name: 'dashboard', params: { pid: resumable.id } }"
      class="text-accent hover:bg-base-2 flex w-10 shrink-0 flex-col items-center gap-0.5 rounded-sm py-1 transition-colors"
      :title="`回到《${resumable.name}》· 这个工程还开着，没有退出`"
    >
      <CornerUpLeft :size="16" :stroke-width="1.6" />
      <span class="w-full truncate text-center text-[9px] leading-none">返回工程</span>
    </RouterLink>

    <!-- 没打开工程时才有应用级功能：项目管理 / 素材库 -->
    <RouterLink
      v-for="f in appItems"
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
      title="设置 · 应用级配置、外部依赖状态与实时事件（不会退出当前工程）"
    >
      <Settings :size="16" :stroke-width="1.6" />
      <span class="text-[9px] leading-none">设置</span>
    </RouterLink>
  </nav>
</template>
