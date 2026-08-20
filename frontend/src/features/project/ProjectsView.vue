<script setup lang="ts">
/**
 * 起始页：项目的新建 / 打开 / 最近列表。
 *
 * 后端 M1 才会提供 /projects，所以这里不画假列表——按钮明确禁用并写清
 * 「为什么现在不能用、什么时候能用」，同时把工程目录结构讲清楚，
 * 让人知道打开项目之后磁盘上会长出什么。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { FolderOpen, FolderPlus, RefreshCw } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import { useSystemStore } from '@/stores/system'

const sys = useSystemStore()
const router = useRouter()
const connected = computed(() => sys.health !== null)

/** 工程目录的组成部分：一个项目 = 一个自包含的目录，可整体拷走。 */
const LAYOUT: { path: string; body: string }[] = [
  { path: 'project.aivs.json', body: '工程清单：名称、分辨率、帧率、默认 Workflow' },
  { path: 'project.db', body: 'SQLite（WAL）：Character / Scene / Shot / Version 的唯一真源' },
  { path: 'assets/', body: '角色表、场景参考、道具图等落盘素材' },
  { path: 'generations/', body: '每次生成的输出与参数快照，永不覆盖' },
  { path: 'proxies/', body: '720p 代理流，仅用于时间线预览' },
]
</script>

<template>
  <div class="min-h-0 flex-1 overflow-auto p-2">
    <section class="border-line-1 bg-base-1 border p-4">
      <h1 class="text-fg-1 text-base font-medium">项目</h1>
      <p class="text-fg-2 mt-1 text-xs">
        一个项目就是磁盘上的一个目录，工程与素材都在里面，拷走即可换机继续。
      </p>
      <div class="mt-3 flex flex-wrap items-center gap-1.5">
        <AppButton variant="primary" :disabled="!connected" title="M1 启用：POST /projects">
          <FolderPlus :size="12" />新建项目
        </AppButton>
        <AppButton :disabled="!connected" title="M1 启用：打开已有工程目录">
          <FolderOpen :size="12" />打开项目
        </AppButton>
        <AppButton variant="ghost" @click="sys.refresh()">
          <RefreshCw :size="12" />重新自检
        </AppButton>
        <AppBadge tone="accent">M1</AppBadge>
      </div>
      <p v-if="!connected" class="text-st-failed mt-2 text-2xs">
        后端未连接，无法新建或打开项目。启动命令：
        <code class="text-fg-2">cd backend &amp;&amp; .venv/Scripts/python -m app.main</code>
      </p>
    </section>

    <div class="mt-2 grid gap-2 lg:grid-cols-2">
      <AppPanel title="最近打开">
        <EmptyState
          title="还没有任何项目"
          body="项目列表接口在 M1 落地；在那之前可以先检查环境是否就绪。"
        >
          <AppButton size="sm" variant="ghost" @click="router.push('/settings')">
            查看环境设置
          </AppButton>
          <AppButton size="sm" variant="ghost" @click="router.push('/')">回到工作台首页</AppButton>
        </EmptyState>
      </AppPanel>

      <AppPanel title="工程目录长什么样">
        <ul class="divide-line-1 divide-y text-xs">
          <li v-for="item in LAYOUT" :key="item.path" class="px-3 py-1.5">
            <p class="text-fg-1 font-mono text-2xs">{{ item.path }}</p>
            <p class="text-fg-4 text-2xs">{{ item.body }}</p>
          </li>
        </ul>
      </AppPanel>
    </div>

    <AppPanel v-if="sys.lastError" title="最近一次错误" class="mt-2">
      <div class="p-3 text-xs">
        <p class="text-st-failed">{{ sys.lastError.title }}</p>
        <p class="text-fg-3 mt-0.5 text-2xs">{{ sys.lastError.detail }}</p>
        <ul class="text-fg-4 mt-1 space-y-0.5 text-2xs">
          <li v-for="s in sys.lastError.suggestions" :key="s">· {{ s }}</li>
        </ul>
      </div>
    </AppPanel>
  </div>
</template>
