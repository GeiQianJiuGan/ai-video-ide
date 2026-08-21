<script setup lang="ts">
/**
 * 采用确认框（Phase 4）。
 *
 * 采用会把文件复制进用户的工程目录，所以这里的顺序不能颠倒：
 *   先 `POST /adopt/plan` 出账单（复制几个文件、多大、进哪个目录、哪些已经有了），
 *   用户看过再 `POST /adopt` 动手。
 *
 * 还要把「单向」说出来：采用后库改了不回流工程，工程改了也不影响库。
 * 用户以为这是同步关系时，之后在库里改角色会白改一整天。
 */
import { ref, watch } from 'vue'
import { DialogContent, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui'
import { Copy, FileWarning } from '@lucide/vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import { ApiError } from '@/shared/api/client'
import {
  humanBytes,
  libraryApi,
  type AdoptKind,
  type AdoptPlan,
  type AdoptResult,
} from '@/shared/api/library'

const props = defineProps<{
  open: boolean
  /** 采用到哪个工程。没打开工程时父组件不该把这个框打开。 */
  pid: string
  kind: AdoptKind
  libraryId: string
}>()

const emit = defineEmits<{ 'update:open': [boolean]; adopted: [AdoptResult] }>()

const plan = ref<AdoptPlan | null>(null)
const busy = ref(false)
const error = ref<ApiError | null>(null)

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

async function confirm(): Promise<void> {
  await guarded(async () => {
    const out = await libraryApi.adopt(props.pid, props.kind, props.libraryId)
    emit('adopted', out)
    emit('update:open', false)
  })
}

watch(
  () => [props.open, props.libraryId] as const,
  async ([open]) => {
    if (!open) return
    plan.value = null
    error.value = null
    if (!props.pid || !props.libraryId) return
    await guarded(async () => {
      plan.value = await libraryApi.adoptPlan(props.pid, props.kind, props.libraryId)
    })
  },
  { immediate: true },
)
</script>

<template>
  <DialogRoot :open="open" @update:open="emit('update:open', $event)">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-40 bg-black/50" />
      <DialogContent
        class="border-line-2 bg-base-1 fixed top-[10vh] left-1/2 z-50 flex max-h-[76vh] w-[min(34rem,92vw)] -translate-x-1/2 flex-col overflow-hidden rounded-md border shadow-2xl"
      >
        <DialogTitle
          class="border-line-1 text-fg-1 flex h-9 shrink-0 items-center gap-2 border-b px-3 text-xs"
        >
          采用到当前项目
          <span v-if="plan" class="text-fg-4 text-2xs">{{ plan.label }} · {{ plan.name }}</span>
        </DialogTitle>

        <div class="min-h-0 flex-1 overflow-auto">
          <div v-if="plan" class="space-y-2 p-3 text-xs">
            <!-- 账单：动手之前把代价说清 -->
            <p class="text-fg-1">
              会复制
              <span class="text-accent tnum">{{ plan.copy_count }}</span>
              个文件（约
              <span class="tnum">{{ humanBytes(plan.total_bytes) }}</span>
              ）进
            </p>
            <p
              class="text-fg-2 border-line-1 bg-base-2 truncate border px-2 py-1 font-mono text-2xs"
            >
              {{ plan.project_dir }}
            </p>
            <p v-if="plan.reuse_count > 0" class="text-fg-3 text-2xs">
              另有 {{ plan.reuse_count }} 个文件工程里已经有了（内容相同），不会再占一份空间。
            </p>
            <p
              v-if="plan.missing_count > 0"
              class="text-st-failed flex items-center gap-1 text-2xs"
            >
              <FileWarning :size="11" />
              {{ plan.missing_count }} 个文件在库目录里找不到了，采用无法进行。
            </p>

            <ul v-if="plan.files.length" class="divide-line-1 border-line-1 divide-y border">
              <li
                v-for="f in plan.files"
                :key="f.library_asset_id"
                class="flex items-center gap-1.5 px-2 py-1"
              >
                <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">{{ f.title }}</span>
                <span class="text-fg-4 tnum text-2xs">{{ humanBytes(f.size_bytes) }}</span>
                <AppBadge v-if="f.missing" tone="fail">文件不见了</AppBadge>
                <AppBadge v-else-if="f.already_in_project" tone="ok">已在工程里</AppBadge>
              </li>
            </ul>
            <p v-else class="text-fg-4 text-2xs">这一条没有关联任何文件，只复制文字设定。</p>

            <p class="text-st-review border-line-1 border-t pt-2 text-2xs">{{ plan.one_way }}</p>
          </div>
          <p v-else-if="!error" class="text-fg-4 p-3 text-2xs">正在算账单…</p>
        </div>

        <div v-if="error" class="border-line-1 border-t px-3 py-2 text-2xs">
          <p class="text-st-failed">{{ error.title }}</p>
          <p class="text-fg-3 mt-0.5">{{ error.detail }}</p>
          <ul class="text-fg-4 mt-0.5">
            <li v-for="s in error.suggestions" :key="s">· {{ s }}</li>
          </ul>
          <p class="text-fg-4 mt-0.5 font-mono">{{ error.code }}</p>
        </div>

        <div class="border-line-1 flex shrink-0 items-center justify-end gap-1.5 border-t p-2">
          <AppButton variant="ghost" @click="emit('update:open', false)">取消</AppButton>
          <AppButton
            variant="primary"
            :disabled="busy || plan === null || plan.missing_count > 0"
            @click="confirm()"
          >
            <Copy :size="11" />{{ busy ? '复制中…' : '确认采用' }}
          </AppButton>
        </div>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
