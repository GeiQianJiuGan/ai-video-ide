<script setup lang="ts">
/**
 * 空状态：告诉用户「这里将出现什么」以及「现在该做什么」。
 * 硬要求——不允许出现没有下一步动作的空白区域。
 */
defineProps<{
  title: string
  body?: string
}>()
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col items-center justify-center gap-1.5 p-6 text-center">
    <p class="text-fg-2 text-xs font-medium">{{ title }}</p>
    <p v-if="body" class="text-fg-4 max-w-md text-xs leading-relaxed">{{ body }}</p>
    <div v-if="$slots.default" class="mt-2 flex items-center gap-1.5"><slot /></div>
  </div>
</template>
