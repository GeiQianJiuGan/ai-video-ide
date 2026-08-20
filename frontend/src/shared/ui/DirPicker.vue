<script setup lang="ts">
/**
 * 目录选择器（Phase 1）。
 *
 * 浏览器拿不到绝对路径（showDirectoryPicker 只给句柄，webkitdirectory 只给相对名），
 * 而工程目录必须是能落盘的绝对路径——所以目录树由后端 /fs/* 提供，
 * 浏览器与 Tauri 壳里行为完全一致。
 *
 * 三条要求：
 *   1. 只列目录，看不到任何文件，也读不到内容；
 *   2. 已经是工程 / 素材库的目录要标出来，能一眼看出「这里已经有东西了」；
 *   3. 失败连 suggestions 一起画出来（没权限、目录没了），绝不静默变成空列表。
 */
import { computed, ref, watch } from 'vue'
import { DialogContent, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui'
import { ChevronUp, FolderPlus, HardDrive, Home, RefreshCw } from '@lucide/vue'
import AppButton from './AppButton.vue'
import AppBadge from './AppBadge.vue'
import { fsApi, type FsDir, type FsRoots } from '@/shared/api/fs'
import { ApiError } from '@/shared/api/client'

const props = withDefaults(
  defineProps<{
    open: boolean
    /** 打开时从哪个目录起步；空则从驱动器列表起步。 */
    start?: string
    title?: string
    /** 确认按钮文案，「选这里新建工程」和「打开这个工程」语义不同。 */
    confirmLabel?: string
  }>(),
  { start: '', title: '选择文件夹', confirmLabel: '选择这个文件夹' },
)

const emit = defineEmits<{ 'update:open': [boolean]; pick: [string] }>()

const roots = ref<FsRoots | null>(null)
const dir = ref<FsDir | null>(null)
const manual = ref('')
const busy = ref(false)
const error = ref<ApiError | null>(null)
const newName = ref('')
const creating = ref(false)

const current = computed(() => dir.value?.path ?? '')

async function guarded(run: () => Promise<void>): Promise<void> {
  busy.value = true
  error.value = null
  try {
    await run()
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause : null
    if (!(cause instanceof ApiError)) throw cause
  } finally {
    busy.value = false
  }
}

async function go(path: string): Promise<void> {
  await guarded(async () => {
    const next = await fsApi.dirs(path)
    dir.value = next
    manual.value = next.path
  })
}

async function loadRoots(): Promise<void> {
  await guarded(async () => {
    roots.value = await fsApi.roots()
  })
}

async function createFolder(): Promise<void> {
  const name = newName.value.trim()
  if (!name || !dir.value) return
  await guarded(async () => {
    const made = await fsApi.mkdir(dir.value!.path, name)
    newName.value = ''
    creating.value = false
    await go(made.path)
  })
}

function confirm(): void {
  const path = manual.value.trim() || current.value
  if (!path) return
  emit('pick', path)
  emit('update:open', false)
}

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    error.value = null
    creating.value = false
    newName.value = ''
    if (!roots.value) await loadRoots()
    const from = props.start.trim() || dir.value?.path || roots.value?.home || ''
    if (from) await go(from).catch(() => void loadRoots())
    else dir.value = null
  },
  { immediate: true },
)
</script>

<template>
  <DialogRoot :open="open" @update:open="emit('update:open', $event)">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-40 bg-black/50" />
      <DialogContent
        class="border-line-2 bg-base-1 fixed top-[8vh] left-1/2 z-50 flex max-h-[80vh] w-[min(46rem,92vw)] -translate-x-1/2 flex-col overflow-hidden rounded-md border shadow-2xl"
      >
        <DialogTitle
          class="border-line-1 text-fg-1 flex h-9 shrink-0 items-center gap-2 border-b px-3 text-xs"
        >
          {{ title }}
          <span class="text-fg-4 text-2xs">只列文件夹，不读取任何文件内容</span>
        </DialogTitle>

        <!-- 面包屑：每一级都能点回去 -->
        <div class="border-line-1 flex h-8 shrink-0 items-center gap-1 border-b px-2">
          <AppButton
            size="sm"
            variant="ghost"
            :disabled="!dir?.parent || busy"
            title="上一级"
            @click="dir?.parent && go(dir.parent)"
          >
            <ChevronUp :size="10" />上一级
          </AppButton>
          <div class="min-w-0 flex-1 overflow-x-auto whitespace-nowrap">
            <template v-for="(c, i) in dir?.crumbs ?? []" :key="c.path">
              <span v-if="i > 0" class="text-fg-4 px-0.5 text-2xs">/</span>
              <button
                type="button"
                class="text-fg-2 hover:text-accent text-2xs"
                @click="go(c.path)"
              >
                {{ c.name }}
              </button>
            </template>
          </div>
          <AppButton
            size="sm"
            variant="ghost"
            :disabled="!current || busy"
            title="重新读取"
            @click="go(current)"
          >
            <RefreshCw :size="10" />
          </AppButton>
        </div>

        <div class="flex min-h-0 flex-1">
          <!-- 起点：驱动器与常用位置 -->
          <ul class="border-line-1 w-40 shrink-0 overflow-auto border-r py-1">
            <li v-for="r in roots?.roots ?? []" :key="r.path">
              <button
                type="button"
                class="hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left text-2xs"
                :class="current === r.path ? 'bg-base-3 text-fg-1' : 'text-fg-2'"
                @click="go(r.path)"
              >
                <HardDrive v-if="r.kind === 'drive'" :size="11" class="shrink-0" />
                <Home v-else :size="11" class="shrink-0" />
                <span class="truncate">{{ r.name }}</span>
              </button>
            </li>
          </ul>

          <!-- 子目录 -->
          <div class="min-h-0 min-w-0 flex-1 overflow-auto">
            <ul class="divide-line-1 divide-y">
              <li v-for="e in dir?.entries ?? []" :key="e.path">
                <button
                  type="button"
                  class="hover:bg-base-2 flex w-full items-center gap-2 px-3 py-1 text-left"
                  @click="go(e.path)"
                >
                  <span class="text-fg-1 min-w-0 flex-1 truncate text-xs">{{ e.name }}</span>
                  <AppBadge v-if="e.is_project" tone="accent">工程</AppBadge>
                  <AppBadge v-if="e.is_library" tone="ok">素材库</AppBadge>
                  <AppBadge v-if="!e.writable" tone="warn">只读</AppBadge>
                  <span v-if="e.has_children" class="text-fg-4 text-2xs">›</span>
                </button>
              </li>
            </ul>
            <p v-if="dir && dir.entries.length === 0" class="text-fg-4 px-3 py-3 text-2xs">
              这个文件夹里没有子文件夹。可以直接选它，或在下面新建一个。
            </p>
            <p v-if="dir?.truncated" class="text-st-review px-3 py-2 text-2xs">
              子文件夹太多，只列出了前一批。请在下面直接粘贴完整路径。
            </p>
          </div>
        </div>

        <div v-if="error" class="border-line-1 border-t px-3 py-2 text-2xs">
          <p class="text-st-failed">{{ error.title }}</p>
          <p class="text-fg-3 mt-0.5">{{ error.detail }}</p>
          <ul class="text-fg-4 mt-0.5">
            <li v-for="s in error.suggestions" :key="s">· {{ s }}</li>
          </ul>
        </div>

        <!-- 底部：手输路径兜底 + 新建文件夹 + 确认 -->
        <div class="border-line-1 shrink-0 border-t p-2">
          <div v-if="creating" class="mb-1.5 flex items-center gap-1.5">
            <input
              v-model="newName"
              placeholder="新文件夹名称"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 text-xs outline-none"
              @keydown.enter.prevent="createFolder()"
            />
            <AppButton :disabled="busy || newName.trim() === ''" @click="createFolder()">
              创建
            </AppButton>
            <AppButton variant="ghost" @click="creating = false">取消</AppButton>
          </div>
          <div class="flex items-center gap-1.5">
            <input
              v-model="manual"
              placeholder="也可以直接粘贴绝对路径"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 font-mono text-xs outline-none"
              @keydown.enter.prevent="go(manual)"
            />
            <AppButton
              variant="ghost"
              :disabled="!dir?.writable || busy"
              :title="dir?.writable ? '在当前文件夹里新建' : '这个文件夹没有写权限'"
              @click="creating = true"
            >
              <FolderPlus :size="11" />新建文件夹
            </AppButton>
            <AppButton variant="ghost" @click="emit('update:open', false)">取消</AppButton>
            <AppButton
              variant="primary"
              :disabled="busy || manual.trim() === ''"
              @click="confirm()"
            >
              {{ confirmLabel }}
            </AppButton>
          </div>
        </div>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
