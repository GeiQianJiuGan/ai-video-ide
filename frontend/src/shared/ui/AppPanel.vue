<script setup lang="ts">
/** 面板容器：28px 标题条 + 内容区。所有工作区面板的统一外壳。 */
withDefaults(
  defineProps<{
    title?: string
    /** 内容区是否可滚动 */
    scroll?: boolean
    /** 无边框，用于嵌套在已有边框的容器内 */
    flush?: boolean
  }>(),
  { title: '', scroll: true, flush: false },
)
</script>

<template>
  <section
    class="bg-base-1 flex min-h-0 min-w-0 flex-col"
    :class="flush ? '' : 'border-line-1 border'"
  >
    <header
      v-if="title || $slots.actions"
      class="border-line-1 bg-base-2 flex h-row shrink-0 items-center gap-2 border-b px-2"
    >
      <span class="text-fg-2 truncate text-xs font-medium tracking-wide uppercase">
        {{ title }}
      </span>
      <div class="ml-auto flex items-center gap-1"><slot name="actions" /></div>
    </header>
    <div class="min-h-0 flex-1" :class="scroll ? 'overflow-auto' : 'overflow-hidden'">
      <slot />
    </div>
  </section>
</template>
