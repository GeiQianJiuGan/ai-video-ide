<script setup lang="ts">
/**
 * 状态点：Shot / Job 状态的唯一可视化来源。
 * 颜色与 docs/02 §7 状态色一一对应，禁止在别处硬编码状态颜色。
 */
import { computed } from 'vue'

export type Status =
  | 'draft'
  | 'ready'
  | 'queued'
  | 'waiting_upstream'
  | 'generating'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'needs_review'

const props = withDefaults(defineProps<{ status: Status; label?: boolean }>(), { label: false })

const MAP: Record<Status, { color: string; text: string; pulse: boolean }> = {
  draft: { color: 'bg-st-draft', text: '草稿', pulse: false },
  ready: { color: 'bg-st-draft', text: '就绪', pulse: false },
  queued: { color: 'bg-st-queued', text: '排队中', pulse: false },
  waiting_upstream: { color: 'bg-st-queued', text: '等待上游', pulse: false },
  generating: { color: 'bg-st-running', text: '生成中', pulse: true },
  running: { color: 'bg-st-running', text: '运行中', pulse: true },
  completed: { color: 'bg-st-done', text: '已完成', pulse: false },
  failed: { color: 'bg-st-failed', text: '失败', pulse: false },
  cancelled: { color: 'bg-st-draft', text: '已取消', pulse: false },
  needs_review: { color: 'bg-st-review', text: '待复核', pulse: false },
}

const meta = computed(() => MAP[props.status])
</script>

<template>
  <span class="inline-flex items-center gap-1.5" :title="meta.text">
    <span
      class="size-1.5 shrink-0 rounded-full"
      :class="[meta.color, meta.pulse && 'animate-pulse']"
    />
    <span v-if="label" class="text-fg-2 text-xs">{{ meta.text }}</span>
  </span>
</template>
