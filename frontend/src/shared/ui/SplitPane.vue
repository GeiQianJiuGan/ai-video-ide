<script setup lang="ts">
/**
 * 可拖拽分栏。行为来自 reka-ui 的 Splitter（键盘可达 + ARIA 完备），
 * 外观全部自研 —— 这正是本项目的组件库路线。
 */
import { SplitterGroup, SplitterPanel, SplitterResizeHandle } from 'reka-ui'

withDefaults(
  defineProps<{
    /** localStorage 键，用于记住用户的布局 */
    id: string
    direction?: 'horizontal' | 'vertical'
    /** 各栏初始百分比，长度需与 slot 数量一致 */
    sizes?: number[]
    minSizes?: number[]
  }>(),
  { direction: 'horizontal', sizes: () => [50, 50], minSizes: () => [10, 10] },
)
</script>

<template>
  <SplitterGroup
    :id="id"
    :direction="direction"
    :auto-save-id="`aivs-split-${id}`"
    class="flex min-h-0 min-w-0 flex-1"
  >
    <template v-for="(size, i) in sizes" :key="i">
      <SplitterResizeHandle
        v-if="i > 0"
        class="bg-line-1 hover:bg-accent/60 data-[state=drag]:bg-accent shrink-0 transition-colors"
        :class="direction === 'horizontal' ? 'w-px cursor-col-resize' : 'h-px cursor-row-resize'"
      />
      <SplitterPanel
        :default-size="size"
        :min-size="minSizes[i] ?? 10"
        class="flex min-h-0 min-w-0 flex-col"
      >
        <slot :name="`pane-${i}`" />
      </SplitterPanel>
    </template>
  </SplitterGroup>
</template>
