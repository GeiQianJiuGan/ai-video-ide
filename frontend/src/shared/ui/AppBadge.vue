<script setup lang="ts">
/** 徽标：里程碑、能力要求、计数等短标签的统一外观。 */
withDefaults(
  defineProps<{
    tone?: 'neutral' | 'accent' | 'ok' | 'warn' | 'fail'
  }>(),
  { tone: 'neutral' },
)

const TONE = {
  neutral: 'border-line-2 text-fg-3 bg-base-2',
  accent: 'border-accent/40 text-accent bg-accent-dim/40',
  ok: 'border-st-done/40 text-st-done bg-st-done/10',
  warn: 'border-st-review/40 text-st-review bg-st-review/10',
  fail: 'border-st-failed/40 text-st-failed bg-st-failed/10',
} as const
</script>

<template>
  <span
    class="inline-flex shrink-0 items-center gap-1 rounded-xs border px-1.5 py-px text-2xs leading-tight whitespace-nowrap"
    :class="TONE[tone]"
  >
    <slot />
  </span>
</template>
