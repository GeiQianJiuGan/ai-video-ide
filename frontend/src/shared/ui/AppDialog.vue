<script setup lang="ts">
/**
 * 弹窗外壳：标题条 + 可滚动主体 + 底部动作条。
 *
 * 抽出来的原因是 `AdoptDialog` / `LibraryPickDialog` / `DirPicker` 里那套 reka-ui
 * 结构（overlay + content + title 的 class）已经被抄了三遍，再加两个「新建项目」
 * 「选素材库目录」就是五遍——外壳一处改不动就会开始互相不像。
 *
 * 只做外壳，不碰业务：里面放什么、什么时候能提交，全由调用方决定。
 * 关闭一律通过 `update:open`（点遮罩、按 Esc 也走它），调用方不必自己接这些。
 *
 * 层级：内嵌 DirPicker 这类「弹窗里再开弹窗」的情况靠 portal 的挂载顺序解决——
 * 后打开的后挂载、同 z-index 下自然压在上面，所以这里不需要维护 z 轴计数器。
 */
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogRoot,
  DialogTitle,
} from 'reka-ui'
import { X } from '@lucide/vue'

withDefaults(
  defineProps<{
    open: boolean
    title: string
    /** 标题右边的一句灰字，用来说明这个框会做什么。 */
    subtitle?: string
    /** 宽度档位：表单用 md，需要列表 / 目录树的用 lg。 */
    size?: 'sm' | 'md' | 'lg'
  }>(),
  { subtitle: '', size: 'md' },
)

const emit = defineEmits<{ 'update:open': [boolean] }>()

const WIDTH = {
  sm: 'w-[min(26rem,92vw)]',
  md: 'w-[min(34rem,92vw)]',
  lg: 'w-[min(46rem,92vw)]',
} as const
</script>

<template>
  <DialogRoot :open="open" @update:open="emit('update:open', $event)">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-40 bg-black/50" />
      <DialogContent
        class="border-line-2 bg-base-1 fixed top-[10vh] left-1/2 z-50 flex max-h-[80vh] -translate-x-1/2 flex-col overflow-hidden rounded-md border shadow-2xl"
        :class="WIDTH[size]"
        v-bind="subtitle ? {} : { 'aria-describedby': undefined }"
      >
        <DialogTitle
          class="border-line-1 text-fg-1 flex h-9 shrink-0 items-center gap-2 border-b px-3 text-xs"
        >
          <slot name="icon" />
          {{ title }}
          <!-- 副标题当无障碍描述用：reka-ui 会把 aria-describedby 指到它；没有副标题时
               上面把这个属性去掉，否则它指向一个不存在的节点（控制台会警告）。 -->
          <DialogDescription v-if="subtitle" as="span" class="text-fg-4 min-w-0 truncate text-2xs">
            {{ subtitle }}
          </DialogDescription>
          <span class="ml-auto flex items-center gap-1.5">
            <slot name="title-actions" />
            <DialogClose class="text-fg-4 hover:text-fg-1" title="关闭">
              <X :size="12" />
            </DialogClose>
          </span>
        </DialogTitle>

        <div class="min-h-0 flex-1 overflow-auto">
          <slot />
        </div>

        <div
          v-if="$slots.footer"
          class="border-line-1 flex shrink-0 items-center gap-1.5 border-t p-2"
        >
          <slot name="footer" />
        </div>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
