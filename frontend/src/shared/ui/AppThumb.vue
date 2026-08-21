<script setup lang="ts">
/**
 * 缩略图：后端给的**相对工程目录**路径 → 一个方块图。
 *
 * 三个刻意的取舍：
 *   1. **只收路径，URL 在这里拼**（`fileUrl`）。挑人物 / 地点是看图的活，这个方块会出现在
 *      好几张清单里，「路径怎么变成 URL」只该有一处口径。
 *   2. **没有图不是错误**：给一个划掉的图片图标当占位。没有角色表的形象照样能挂，
 *      只是生成时喂不出参考图——那句话由旁边的徽标去说，不是这里。
 *   3. **只画图片**。后端的 `thumbnail_path` 已经按后缀过滤过（`story.py::_image_path`），
 *      所以这里可以放心用 `<img>`；把 `.mp4` 喂给它只会得到一个坏图标。
 */
import { computed } from 'vue'
import { ImageOff } from '@lucide/vue'
import { fileUrl } from '@/shared/api/files'

const props = withDefaults(
  defineProps<{
    pid: string
    path?: string | null
    /** 同时当 `alt` 与 `title`：清单里鼠标停住能看清是谁。 */
    label?: string
    /** 方块边长：卡片上的小头像用 xs，清单行用 sm，需要看清脸时用 md。 */
    size?: 'xs' | 'sm' | 'md'
  }>(),
  { path: null, label: '', size: 'sm' },
)

const SIZE = { xs: 'size-3.5', sm: 'size-6', md: 'size-10' } as const
const ICON = { xs: 8, sm: 10, md: 14 } as const

const src = computed(() => (props.path ? fileUrl(props.pid, props.path) : ''))
</script>

<template>
  <span
    class="border-line-2 bg-base-3 inline-flex shrink-0 items-center justify-center overflow-hidden rounded-xs border"
    :class="SIZE[size]"
    :title="src ? label || undefined : label ? `${label}（无图）` : '无图'"
  >
    <img v-if="src" :src="src" :alt="label" class="size-full object-cover" />
    <ImageOff v-else :size="ICON[size]" class="text-fg-4" />
  </span>
</template>
