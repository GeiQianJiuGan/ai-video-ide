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
import { NAV_FEATURES } from '@/app/features'

const props = defineProps<{ open: boolean; projectId: string | null }>()
const emit = defineEmits<{ 'update:open': [boolean] }>()

const router = useRouter()
const query = ref('')
const cursor = ref(0)

interface Entry {
  key: string
  title: string
  desc: string
  badge: string
  go: () => void
  disabled: boolean
}

const entries = computed<Entry[]>(() => {
  const list: Entry[] = [
    {
      key: 'home',
      title: '工作台首页',
      desc: '核心链路总览与全部功能入口',
      badge: '可用',
      go: () => void router.push('/'),
      disabled: false,
    },
    {
      key: 'projects',
      title: '项目管理',
      desc: '新建 / 打开工程目录',
      badge: 'M1',
      go: () => void router.push('/projects'),
      disabled: false,
    },
    {
      key: 'settings',
      title: '设置与环境自检',
      desc: 'FFmpeg / ComfyUI / LLM 状态与修复建议',
      badge: '可用',
      go: () => void router.push('/settings'),
      disabled: false,
    },
    ...NAV_FEATURES.map((f) => ({
      key: f.id,
      title: f.title,
      desc: f.purpose,
      badge: f.milestone,
      disabled: props.projectId === null,
      go: () => {
        if (props.projectId) void router.push({ name: f.route, params: { pid: props.projectId } })
      },
    })),
  ]
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
  if (!entry || entry.disabled) return
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
            :class="[
              i === cursor ? 'bg-base-3' : 'hover:bg-base-2',
              e.disabled && 'cursor-not-allowed opacity-40',
            ]"
            @mouseenter="cursor = i"
            @click="pick(i)"
          >
            <div class="min-w-0 flex-1">
              <p class="text-fg-1 truncate text-xs">{{ e.title }}</p>
              <p class="text-fg-4 truncate text-2xs">{{ e.desc }}</p>
            </div>
            <AppBadge v-if="e.disabled" tone="warn">需先打开项目</AppBadge>
            <AppBadge v-else :tone="e.badge === '可用' ? 'ok' : 'neutral'">{{ e.badge }}</AppBadge>
          </li>
          <li v-if="!entries.length" class="text-fg-4 px-3 py-3 text-xs">没有匹配的功能。</li>
        </ul>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
