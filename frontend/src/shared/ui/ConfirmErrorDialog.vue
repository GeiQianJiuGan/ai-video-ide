<script setup lang="ts">
/**
 * 确认类拦截的弹窗（`related_ids.confirm`，目前只有参考图装不下的 `REF_OVER_CAPACITY`）。
 *
 * 为什么不复用 `ErrorPanel`：那是一块**贴在页面顶部的方框**，确认按钮混在四要素里，
 * 用户在几屏高的分镜页上滚到别处就再也找不到它了——「我在那里找不到确认的按钮」。
 * 这类拦截后端**一个任务都没入队**，它要的是一句「这样也继续吗」，那就是一个模态框
 * 该做的事：挡住页面、两颗按钮、不点不走。
 *
 * 仍然是四要素齐全（硬约束 4）：title / detail / suggestions / code 一个都不省，
 * 只是从「一块警示条」换成「一个必须回答的问题」。
 */
import { computed } from 'vue'
import { AlertTriangle } from '@lucide/vue'
import AppButton from './AppButton.vue'
import AppDialog from './AppDialog.vue'
import type { ApiError } from '@/shared/api/client'

const props = withDefaults(
  defineProps<{
    /** 触发这次确认的那条错误；null 时弹窗不显示。 */
    error: ApiError | null
    /** 确认按钮上的字，各页面自己说清楚「继续」到底会做什么。 */
    confirmLabel?: string
    title?: string
    /** 正在重调入口，两颗按钮都锁住，避免连点入两次队。 */
    busy?: boolean
  }>(),
  { confirmLabel: '确认执行', title: '需要你确认后才继续', busy: false },
)

const emit = defineEmits<{ confirm: []; cancel: [] }>()

const open = computed(() => props.error !== null)

function onOpen(next: boolean): void {
  // 点遮罩 / 按 Esc 等同于「取消执行」——默认不做比默默做了更安全
  if (!next) emit('cancel')
}
</script>

<template>
  <AppDialog :open="open" :title="title" subtitle="后端还没有入队任何任务" size="sm" @update:open="onOpen">
    <template #icon>
      <AlertTriangle :size="12" class="text-st-review" />
    </template>
    <div v-if="error" class="space-y-2 p-3 text-2xs">
      <p class="text-st-review">{{ error.title }}</p>
      <p class="text-fg-2 break-words">{{ error.detail }}</p>
      <ul v-if="error.suggestions.length" class="text-fg-3 space-y-px">
        <li v-for="s in error.suggestions" :key="s">· {{ s }}</li>
      </ul>
      <p class="text-fg-4 font-mono">{{ error.code }}</p>
    </div>
    <template #footer>
      <div class="ml-auto flex items-center gap-2">
        <AppButton size="sm" variant="ghost" :disabled="busy" @click="emit('cancel')">
          取消执行
        </AppButton>
        <AppButton size="sm" variant="primary" :disabled="busy" @click="emit('confirm')">
          {{ confirmLabel }}
        </AppButton>
      </div>
    </template>
  </AppDialog>
</template>
