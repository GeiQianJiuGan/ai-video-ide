<script setup lang="ts">
/**
 * 视频二次处理弹窗 (RefineModal)。
 *
 * 对已生成的画面版本进行二次超分 (upscale)、插帧 (interpolate) 或重做 (recut)。
 * 产出同一镜头的新版本并记录 parent_version_id 谱系。
 */

import { ref, watch } from 'vue'
import { Sparkles, Zap } from '@lucide/vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import { ApiError } from '@/shared/api/client'
import { refineApi, type RefineKind, type RefinePlanResult } from '@/shared/api/refine'

const props = defineProps<{
  open: boolean
  pid: string
  versionId?: string
  shotId?: string
  sceneId?: string
}>()

const emit = defineEmits<{
  'update:open': [boolean]
  done: []
}>()

const busy = ref(false)
const error = ref<ApiError | null>(null)
const selectedKind = ref<'upscale' | 'interpolate' | 'recut'>('upscale')
const kindsList = ref<RefineKind[]>([])
const planResult = ref<RefinePlanResult | null>(null)

watch(
  () => props.open,
  async (opened) => {
    if (opened) {
      error.value = null
      planResult.value = null
      selectedKind.value = 'upscale'
      try {
        kindsList.value = await refineApi.kinds()
      } catch {
        kindsList.value = [
          { kind: 'upscale', label: '超分（提高分辨率）' },
          { kind: 'interpolate', label: '插帧（提高帧率 / 变慢）' },
          { kind: 'recut', label: '重做（同一段再过一遍图）' },
        ]
      }
      await fetchPlan()
    }
  },
)

async function fetchPlan() {
  busy.value = true
  error.value = null
  try {
    planResult.value = await refineApi.plan(props.pid, {
      version_ids: props.versionId ? [props.versionId] : undefined,
      shot_ids: props.shotId ? [props.shotId] : undefined,
      scene_id: props.sceneId || undefined,
      kind: selectedKind.value,
    })
  } catch (err) {
    error.value = err instanceof ApiError ? err : null
  } finally {
    busy.value = false
  }
}

async function handleRun() {
  if (!planResult.value || planResult.value.blocked || !planResult.value.items.length) return
  busy.value = true
  error.value = null
  try {
    await refineApi.run(props.pid, {
      version_ids: props.versionId ? [props.versionId] : undefined,
      shot_ids: props.shotId ? [props.shotId] : undefined,
      scene_id: props.sceneId || undefined,
      kind: selectedKind.value,
    })
    emit('done')
    emit('update:open', false)
  } catch (err) {
    error.value = err instanceof ApiError ? err : null
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AppDialog
    :open="open"
    title="视频二次优化处理"
    subtitle="对已有成片进行超分、插帧或重剪，保留原始版本随时可回退"
    size="md"
    @update:open="emit('update:open', $event)"
  >
    <template #icon>
      <Zap :size="14" class="text-accent" />
    </template>

    <div class="space-y-3 p-3">
      <ErrorPanel v-if="error" :error="error" @dismiss="error = null" />

      <!-- 处理种类选择 -->
      <div class="space-y-1">
        <span class="text-fg-3 text-2xs font-medium">优化种类</span>
        <div class="grid grid-cols-3 gap-2">
          <button
            v-for="k in kindsList"
            :key="k.kind"
            class="border p-2 text-left transition-all"
            :class="
              selectedKind === k.kind
                ? 'border-accent bg-accent-dim/40 text-fg-1'
                : 'border-line-1 bg-base-2 hover:bg-base-3 text-fg-3'
            "
            @click="selectedKind = k.kind; fetchPlan()"
          >
            <p class="text-xs font-medium">{{ k.kind === 'upscale' ? '高清超分' : k.kind === 'interpolate' ? '平滑插帧' : '画面重做' }}</p>
            <p class="text-fg-4 mt-0.5 text-2xs">{{ k.label }}</p>
          </button>
        </div>
      </div>

      <!-- 预设与状态说明 -->
      <div v-if="planResult" class="border-line-1 bg-base-2 border p-2 text-2xs space-y-1">
        <div class="flex items-center justify-between">
          <span class="text-fg-3">处理预设:</span>
          <strong class="text-fg-1">{{ planResult.preset || '默认视频预设' }}</strong>
        </div>
        <div v-if="!planResult.preset_ready" class="text-st-failed">
          {{ planResult.preset_detail }}
        </div>
      </div>

      <!-- 拟处理片段账单 -->
      <div v-if="planResult && planResult.items.length" class="space-y-1.5 border-line-1 border-t pt-2">
        <div class="flex items-center justify-between text-2xs">
          <span class="text-fg-2 font-medium">拟处理片段（共 {{ planResult.total }} 段）</span>
        </div>
        <div class="border-line-1 max-h-36 overflow-y-auto border p-1 space-y-1 bg-base-2">
          <div
            v-for="item in planResult.items"
            :key="item.version_id"
            class="flex items-center justify-between px-2 py-1 bg-base-1 text-2xs rounded-xs"
          >
            <span class="text-fg-1 font-medium">Shot {{ item.shot_index_no }} · v{{ item.version_no }}</span>
            <AppBadge tone="accent">{{ selectedKind }}</AppBadge>
            <span class="text-fg-3">{{ item.duration }}s</span>
          </div>
        </div>
      </div>

      <!-- 跳过项提示 -->
      <div v-if="planResult?.skipped.length" class="border-line-1 bg-base-3 p-2 text-2xs border text-fg-4">
        跳过 {{ planResult.skipped.length }} 个镜头（没有可处理的成片画面版本）
      </div>
    </div>

    <template #footer>
      <div class="ml-auto flex items-center gap-2">
        <AppButton size="sm" variant="ghost" @click="emit('update:open', false)">取消</AppButton>
        <AppButton
          size="sm"
          variant="primary"
          :disabled="busy || !planResult?.preset_ready || !planResult?.items.length"
          @click="handleRun"
        >
          <Sparkles :size="12" />入队二次处理 ({{ planResult?.items.length || 0 }} 段)
        </AppButton>
      </div>
    </template>
  </AppDialog>
</template>
