<script setup lang="ts">
/** 基础按钮。密度与 --spacing-row（28px）对齐。 */
withDefaults(
  defineProps<{
    variant?: 'default' | 'primary' | 'ghost' | 'danger'
    size?: 'sm' | 'md'
    disabled?: boolean
  }>(),
  { variant: 'default', size: 'md', disabled: false },
)

const VARIANT = {
  default: 'bg-base-2 hover:bg-base-3 text-fg-1 border-line-1',
  primary: 'bg-accent-dim hover:bg-accent/25 text-accent-hi border-accent/40',
  ghost: 'bg-transparent hover:bg-base-2 text-fg-2 hover:text-fg-1 border-transparent',
  danger: 'bg-base-2 hover:bg-st-failed/20 text-st-failed border-line-1',
} as const

const SIZE = { sm: 'h-5 px-1.5 text-2xs', md: 'h-row px-2.5 text-xs' } as const
</script>

<template>
  <button
    type="button"
    :disabled="disabled"
    class="inline-flex shrink-0 items-center gap-1.5 rounded-sm border transition-colors disabled:cursor-not-allowed disabled:opacity-40"
    :class="[VARIANT[variant], SIZE[size]]"
  >
    <slot />
  </button>
</template>
