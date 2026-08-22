<script setup lang="ts">
/**
 * 错误面板：把 ApiError 的四要素完整显示出来。
 *
 * 硬约束「绝不静默失败」在 UI 侧的落点——code / title / detail / suggestions
 * 一个都不能省。suggestions 是后端专门为「怎么修」写的，丢掉它等于让用户卡死。
 *
 * 「项目未打开」是它的一个特例：后端重启后进程内没有已打开的工程，这是设计而不是
 * bug，所以这里不画成崩溃，而是给一颗回起始页重开的按钮。
 *
 * **确认类拦截**（`related_ids.confirm`，如参考图装不下的 `REF_OVER_CAPACITY`）也不是崩溃：
 * 后端一个任务都没入队，只是要问一句「这样也继续吗」。所以换成警示色并把 code 旁边标成
 * 「需要确认」，那颗确认按钮由各页面从 `actions` 插槽塞进来（它才知道要重调哪个入口）。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { AlertTriangle, FolderOpen, X } from '@lucide/vue'
import AppButton from './AppButton.vue'
import { confirmFlagOf, isProjectNotOpen, type ApiError } from '@/shared/api/client'

const props = withDefaults(
  defineProps<{
    error: ApiError | null
    /** 允许用户关掉这条错误（一次性动作的失败）。列表加载失败就别给关。 */
    dismissible?: boolean
  }>(),
  { dismissible: true },
)

const emit = defineEmits<{ dismiss: [] }>()
const router = useRouter()

const notOpen = computed(() => isProjectNotOpen(props.error))
const askOnly = computed(() => Boolean(confirmFlagOf(props.error)))
/**
 * 确认类用警示色，真失败才是红的——两种一个颜色的话，用户会以为生成已经废了。
 * 类名写成完整字面量：Tailwind 是扫源码生成的，`text-${x}` 那种拼法生成不出来。
 */
const box = computed(() =>
  askOnly.value ? 'border-st-review/40 bg-st-review/5' : 'border-st-failed/40 bg-st-failed/5',
)
const ink = computed(() => (askOnly.value ? 'text-st-review' : 'text-st-failed'))
</script>

<template>
  <div v-if="error" class="border px-2 py-1.5 text-2xs" :class="box">
    <div class="flex items-start gap-1.5">
      <AlertTriangle :size="12" class="mt-px shrink-0" :class="ink" />
      <div class="min-w-0 flex-1">
        <p :class="ink">{{ error.title }}</p>
        <p class="text-fg-2 mt-0.5 break-words">{{ error.detail }}</p>
        <ul v-if="error.suggestions.length" class="text-fg-3 mt-0.5 space-y-px">
          <li v-for="s in error.suggestions" :key="s">· {{ s }}</li>
        </ul>
        <div class="mt-1 flex items-center gap-1.5">
          <span class="text-fg-4 font-mono">{{ error.code }}</span>
          <span v-if="askOnly" :class="ink">需要确认</span>
          <AppButton
            v-if="notOpen"
            size="sm"
            variant="primary"
            @click="router.push({ name: 'projects' })"
          >
            <FolderOpen :size="10" />回起始页重开
          </AppButton>
          <slot name="actions" />
        </div>
      </div>
      <button
        v-if="dismissible"
        class="text-fg-4 hover:text-fg-1 shrink-0"
        title="关闭"
        @click="emit('dismiss')"
      >
        <X :size="12" />
      </button>
    </div>
  </div>
</template>
