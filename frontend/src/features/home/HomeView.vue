<script setup lang="ts">
/**
 * 工作台首页 —— 打开应用第一眼看到的东西。
 *
 * 三个问题必须在这一屏内被回答完：
 *   1. 这个软件怎么工作？        → 核心链路流水线
 *   2. 它有哪些功能、各自干什么？→ 按创作层分组的功能卡片
 *   3. 我现在能不能开工？        → 环境自检条
 */
import { computed } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { ArrowRight, ChevronRight, FolderOpen, Lock, RefreshCw, Settings } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import { CHAIN, GROUPS, REQUIREMENT_LABEL, featuresOf, type Feature } from '@/app/features'
import { useSystemStore } from '@/stores/system'

const sys = useSystemStore()
const router = useRouter()

/** 首页在未打开项目时也要可用：功能卡片可点，落到「需要先打开项目」的引导。 */
const pid = computed<string | null>(() => null)

const readiness = computed(() => [
  {
    key: 'backend',
    label: '后端服务',
    ok: sys.health !== null,
    detail: sys.health ? `v${sys.health.version} · schema ${sys.health.schema_version}` : '未连接',
    hint: '未连接时所有功能都不可用，请先启动 backend。',
  },
  ...sys.deps.map((d) => ({
    key: d.name,
    label: REQUIREMENT_LABEL[d.name],
    ok: d.ok,
    detail: d.detail,
    hint: d.hint,
  })),
])

function open(f: Feature): void {
  if (pid.value) void router.push({ name: f.route, params: { pid: pid.value } })
  else void router.push('/projects')
}
</script>

<template>
  <div class="min-h-0 flex-1 overflow-auto p-2">
    <!-- 定位：一句话说清人 / AI / 系统各自的角色 -->
    <section class="border-line-1 bg-base-1 border p-4">
      <h1 class="text-fg-1 text-base font-medium">视频工程与编排器</h1>
      <p class="text-fg-2 mt-1 text-xs">
        <span class="text-accent">AI</span> 负责生产素材 ·
        <span class="text-accent">系统</span> 负责工程与编排 ·
        <span class="text-accent">你</span> 负责导演决策
      </p>
      <div class="mt-3 flex flex-wrap items-center gap-1.5">
        <AppButton variant="primary" @click="router.push('/projects')">
          <FolderOpen :size="12" />新建 / 打开项目
        </AppButton>
        <AppButton variant="ghost" @click="router.push('/settings')">
          <Settings :size="12" />环境设置
        </AppButton>
        <AppButton variant="ghost" @click="sys.refresh()">
          <RefreshCw :size="12" />重新自检
        </AppButton>
      </div>
    </section>

    <!-- 核心链路：把「系统怎么工作」画出来，每一节点都可点进去 -->
    <AppPanel title="核心链路" class="mt-2">
      <div class="flex flex-wrap items-stretch gap-1 p-3">
        <template v-for="(node, i) in CHAIN" :key="node.label">
          <ChevronRight v-if="i > 0" :size="12" class="text-fg-4 self-center" />
          <RouterLink
            :to="node.route && pid ? { name: node.route, params: { pid } } : '/projects'"
            class="border-line-1 bg-base-2 hover:border-accent/50 hover:bg-base-3 flex min-w-24 flex-col gap-0.5 rounded-sm border px-2 py-1.5"
          >
            <span class="text-fg-1 font-mono text-2xs">{{ node.label }}</span>
            <span class="text-fg-4 text-2xs leading-tight">{{ node.desc }}</span>
          </RouterLink>
        </template>
      </div>
      <p class="text-fg-4 border-line-1 border-t px-3 py-2 text-2xs">
        业务层不绑定任何具体视频模型：差异全部下沉到 Workflow Adapter。生成版本永不覆盖。
      </p>
    </AppPanel>

    <!-- 环境自检：能不能开工 -->
    <AppPanel title="能不能开工" class="mt-2">
      <ul class="divide-line-1 divide-y">
        <li v-for="r in readiness" :key="r.key" class="flex items-start gap-2 px-3 py-1.5">
          <span
            class="mt-1.5 size-1.5 shrink-0 rounded-full"
            :class="r.ok ? 'bg-st-done' : 'bg-st-failed'"
          />
          <div class="min-w-0 flex-1">
            <p class="text-fg-1 text-xs">{{ r.label }} — {{ r.detail }}</p>
            <p v-if="!r.ok && r.hint" class="text-fg-4 text-2xs">{{ r.hint }}</p>
          </div>
        </li>
      </ul>
    </AppPanel>

    <!-- 全部功能：按创作层分组，每张卡片说清用它能拿到什么 -->
    <section v-for="g in GROUPS" :key="g.id" class="mt-2">
      <AppPanel :title="`${g.title} — ${g.question}`">
        <div class="grid gap-1.5 p-2 sm:grid-cols-2 xl:grid-cols-3">
          <button
            v-for="f in featuresOf(g.id)"
            :key="f.id"
            type="button"
            class="border-line-1 bg-base-2 hover:border-accent/50 hover:bg-base-3 group flex flex-col gap-1 rounded-sm border p-2.5 text-left"
            @click="open(f)"
          >
            <div class="flex w-full items-center gap-1.5">
              <component :is="f.icon" :size="13" :stroke-width="1.6" class="text-accent" />
              <span class="text-fg-1 truncate text-xs font-medium">{{ f.title }}</span>
              <Lock v-if="!f.ready" :size="10" class="text-fg-4 ml-auto" />
              <ArrowRight
                v-else
                :size="11"
                class="text-fg-4 group-hover:text-accent ml-auto opacity-0 group-hover:opacity-100"
              />
            </div>
            <p class="text-fg-3 text-2xs leading-relaxed">{{ f.purpose }}</p>
            <div class="mt-0.5 flex flex-wrap gap-1">
              <AppBadge tone="accent">{{ f.milestone }}</AppBadge>
              <AppBadge v-for="req in f.requires" :key="req">{{ REQUIREMENT_LABEL[req] }}</AppBadge>
            </div>
          </button>
        </div>
      </AppPanel>
    </section>

    <p class="text-fg-4 px-1 py-3 text-2xs">
      带锁的功能尚未实现，点进去可以看到它的完整工作区骨架与操作说明。按
      <kbd class="text-fg-3 font-mono">Ctrl K</kbd> 可随时搜索跳转。
    </p>
  </div>
</template>
