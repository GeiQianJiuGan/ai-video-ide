<script setup lang="ts">
/**
 * 命令面板（Ctrl/Cmd + K）。
 *
 * 存在的理由：功能必须「可见且可达」。导航栏只放图标，
 * 这里用全名 + 一句话说明把全部功能摊开，键盘即可跳转。
 * 行为壳用 reka-ui Dialog（焦点陷阱 + ARIA），列表与键盘导航自研。
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { DialogContent, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui'
import { Search } from '@lucide/vue'
import AppBadge from './AppBadge.vue'
import { APP_NAV_FEATURES, PROJECT_ADVANCED_FEATURES, PROJECT_NAV_FEATURES } from '@/app/features'
import { useOnboardingStore } from '@/stores/onboarding'
import { useShellStore } from '@/stores/shell'

const props = defineProps<{ open: boolean; projectId: string | null }>()
const emit = defineEmits<{ 'update:open': [boolean] }>()

const router = useRouter()
const wiz = useOnboardingStore()
const shell = useShellStore()
const query = ref('')
const cursor = ref(0)

interface Entry {
  key: string
  title: string
  desc: string
  badge: string
  go: () => void
}

/**
 * 候选项按 scope 组装，与 Activity Bar 同一套规则：
 * 应用级功能永远列出；项目内功能只在打开了工程时才出现——
 * 列一堆点了没反应的条目，比不列更让人困惑。所以这里没有 disabled 态。
 *
 * 打开工程时**项目内的排在前面**，应用级那两条排后面并写明「会离开这个工程的工作区」：
 * 左栏里它们此刻是藏起来的（两级互斥），面板里仍然搜得到，但不能让人不知道
 * 点下去会掉出工程——那正是这次要修掉的那个惊吓。
 */
const entries = computed<Entry[]>(() => {
  const pid = props.projectId
  const list: Entry[] = []
  if (pid) {
    list.push(
      ...PROJECT_NAV_FEATURES.map((f) => ({
        key: f.id,
        title: f.title,
        desc: f.purpose,
        badge: f.ready ? '可用' : f.milestone,
        go: () => void router.push({ name: f.route, params: { pid } }),
      })),
      // 高级 / 兼容路径不进左栏，但搜得到——已经配好的东西必须还能进去
      ...PROJECT_ADVANCED_FEATURES.map((f) => ({
        key: f.id,
        title: f.title,
        desc: f.purpose,
        badge: '高级',
        go: () => void router.push({ name: f.route, params: { pid } }),
      })),
      // 下面两条不是页面而是**动作**：一个开右侧停靠栏，一个开导出弹窗。
      // 它们进面板的理由和「高级路径」一样——不在左栏里，但必须找得到。
      {
        key: 'director',
        title: shell.directorOpen ? 'AI 导演 · 收起右侧那一栏' : 'AI 导演 · 停在右侧',
        desc: '说一句话，它提一份可逐条审阅的提案；这一栏跟着你换页（Ctrl I）',
        badge: '可用',
        go: () => shell.toggleDirector(),
      },
      {
        key: 'export-package',
        title: '导出当前工程为工程包',
        desc: '打成一个 .aivspkg，换机器导入。先出账单再动手，密钥与服务地址不进包',
        badge: '可用',
        go: () => shell.openExport(),
      },
    )
  }
  list.push(
    ...APP_NAV_FEATURES.map((f) => ({
      key: f.id,
      title: f.title,
      desc: pid ? `${f.purpose}（会离开这个工程的工作区）` : f.purpose,
      badge: f.ready ? '可用' : f.milestone,
      go: () => void router.push({ name: f.route }),
    })),
    {
      key: 'settings',
      title: '设置与环境自检',
      desc: 'FFmpeg / ComfyUI / LLM 状态与修复建议',
      badge: '可用',
      go: () => void router.push('/settings'),
    },
    {
      key: 'onboarding',
      title: '新手引导 · 重新走一遍',
      desc: '演示工程、怎么配 ComfyUI 或 REST API、怎么绑定、每个功能干什么',
      badge: '可用',
      go: () => void wiz.reopen(),
    },
  )
  const q = query.value.trim().toLowerCase()
  if (!q) return list
  return list.filter((e) => (e.title + e.desc).toLowerCase().includes(q))
})

watch([query, () => props.open], () => (cursor.value = 0))

function move(delta: number): void {
  const n = entries.value.length
  if (!n) return
  cursor.value = (cursor.value + delta + n) % n
}

function commit(): void {
  const entry = entries.value[cursor.value]
  if (!entry) return
  entry.go()
  emit('update:open', false)
}

function pick(index: number): void {
  cursor.value = index
  commit()
}
</script>

<template>
  <DialogRoot :open="open" @update:open="emit('update:open', $event)">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-40 bg-black/50" />
      <DialogContent
        class="border-line-2 bg-base-1 fixed top-[15vh] left-1/2 z-50 flex max-h-[60vh] w-[min(38rem,90vw)] -translate-x-1/2 flex-col overflow-hidden rounded-md border shadow-2xl"
        @keydown.down.prevent="move(1)"
        @keydown.up.prevent="move(-1)"
        @keydown.enter.prevent="commit()"
      >
        <DialogTitle class="sr-only">命令面板</DialogTitle>
        <div class="border-line-1 flex h-9 shrink-0 items-center gap-2 border-b px-3">
          <Search :size="13" class="text-fg-3 shrink-0" />
          <input
            v-model="query"
            autofocus
            placeholder="搜索功能…"
            class="text-fg-1 placeholder:text-fg-4 min-w-0 flex-1 bg-transparent text-xs outline-none"
          />
          <span class="text-fg-4 text-2xs">↑↓ 选择 · Enter 打开 · Esc 关闭</span>
        </div>

        <ul class="min-h-0 flex-1 overflow-auto py-1">
          <li
            v-for="(e, i) in entries"
            :key="e.key"
            class="flex cursor-pointer items-center gap-2 px-3 py-1.5"
            :class="i === cursor ? 'bg-base-3' : 'hover:bg-base-2'"
            @mouseenter="cursor = i"
            @click="pick(i)"
          >
            <div class="min-w-0 flex-1">
              <p class="text-fg-1 truncate text-xs">{{ e.title }}</p>
              <p class="text-fg-4 truncate text-2xs">{{ e.desc }}</p>
            </div>
            <AppBadge :tone="e.badge === '可用' ? 'ok' : 'neutral'">{{ e.badge }}</AppBadge>
          </li>
          <li v-if="!entries.length" class="text-fg-4 px-3 py-3 text-xs">没有匹配的功能。</li>
        </ul>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
