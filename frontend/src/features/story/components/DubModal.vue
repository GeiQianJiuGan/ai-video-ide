<script setup lang="ts">
/**
 * AI 音频重构与配音弹窗 (DubModal)。
 *
 * 功能：
 *   - 自动级联台词（镜头台词 -> 幕级台词 -> 用户输入）；
 *   - 支持声音描述、音色参考音频、对口型参考画面；
 *   - 先账单再入队：清晰预览哪些镜头配音、哪些跳过。
 */

import { computed, ref, watch } from 'vue'
import { Mic, Sparkles, Volume2 } from '@lucide/vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import { ApiError } from '@/shared/api/client'
import { assetsApi, type Asset } from '@/shared/api/assets'
import { dubApi, type DubPlanResult } from '@/shared/api/dub'

const props = defineProps<{
  open: boolean
  pid: string
  shotId?: string
  sceneId?: string
  initialDialogue?: string
}>()

const emit = defineEmits<{
  'update:open': [boolean]
  done: []
}>()

const busy = ref(false)
const error = ref<ApiError | null>(null)

const dialogue = ref('')
const soundPrompt = ref('')
const voiceRefAssetId = ref<string | null>(null)
const withVideo = ref(false)
const planResult = ref<DubPlanResult | null>(null)
const audioAssets = ref<Asset[]>([])

const isBatch = computed(() => !!props.sceneId && !props.shotId)

watch(
  () => props.open,
  async (opened) => {
    if (opened) {
      error.value = null
      dialogue.value = props.initialDialogue || ''
      soundPrompt.value = ''
      voiceRefAssetId.value = null
      withVideo.value = false
      planResult.value = null
      await loadAssets()
      await fetchPlan()
    }
  },
)

async function loadAssets() {
  try {
    const list = await assetsApi.list(props.pid, 'audio')
    audioAssets.value = list
  } catch {
    audioAssets.value = []
  }
}

async function fetchPlan() {
  busy.value = true
  error.value = null
  try {
    planResult.value = await dubApi.plan(props.pid, {
      shot_ids: props.shotId ? [props.shotId] : undefined,
      scene_id: props.sceneId || undefined,
      text: dialogue.value.trim() || undefined,
      prompt: soundPrompt.value.trim() || undefined,
      voice_ref_asset_id: voiceRefAssetId.value || undefined,
      with_video: withVideo.value,
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
    await dubApi.run(props.pid, {
      shot_ids: props.shotId ? [props.shotId] : undefined,
      scene_id: props.sceneId || undefined,
      text: dialogue.value.trim() || undefined,
      prompt: soundPrompt.value.trim() || undefined,
      voice_ref_asset_id: voiceRefAssetId.value || undefined,
      with_video: withVideo.value,
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
    :title="isBatch ? '整幕 AI 批量配音' : '镜头 AI 配音与重构'"
    :subtitle="isBatch ? '为整幕内有台词或声音描述的镜头一键配音' : '生成声音并绑定为镜头独立音轨，画面不重跑'"
    size="md"
    @update:open="emit('update:open', $event)"
  >
    <template #icon>
      <Mic :size="14" class="text-accent" />
    </template>

    <div class="space-y-3 p-3">
      <ErrorPanel v-if="error" :error="error" @dismiss="error = null" />

      <!-- 模式/服务状态提示 -->
      <div v-if="planResult" class="border-line-1 bg-base-2 flex items-center justify-between border px-2.5 py-1.5 text-2xs">
        <div class="flex items-center gap-1.5">
          <Volume2 :size="12" class="text-accent" />
          <span class="text-fg-3">音源提供方:</span>
          <strong class="text-fg-1">{{ planResult.provider_label }}</strong>
        </div>
        <AppBadge :tone="planResult.configured ? 'ok' : 'fail'">
          {{ planResult.configured ? '已就绪' : '未配置服务' }}
        </AppBadge>
      </div>

      <!-- 未配置音源指引 -->
      <div v-if="planResult && !planResult.configured" class="border-line-1 bg-base-3 p-2 text-2xs space-y-1 text-fg-3 border">
        <p class="text-fg-2 font-medium">还没有配置音源生成服务：</p>
        <ul class="list-disc pl-4 space-y-0.5 text-fg-4">
          <li v-for="(h, idx) in planResult.how_to" :key="idx">{{ h }}</li>
        </ul>
      </div>

      <!-- 台词输入 (单镜头时为主要输入，整幕时为统一覆盖) -->
      <label class="block">
        <span class="text-fg-3 text-2xs font-medium">
          {{ isBatch ? '统一覆盖台词（留空则继承各分镜自带台词）' : '镜头台词 (Dialogue)' }}
        </span>
        <textarea
          v-model="dialogue"
          rows="3"
          placeholder="输入角色台词或旁白文本..."
          class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-1 w-full resize-none border p-2 text-xs outline-none"
          @change="fetchPlan"
        />
      </label>

      <!-- 声音描述 (Prompt) -->
      <label class="block">
        <span class="text-fg-3 text-2xs font-medium">声音特征描述 (Sound Prompt / Style)</span>
        <input
          v-model="soundPrompt"
          type="text"
          placeholder="例如: 低沉青年男声，平静略带忧伤，环境有轻微风声"
          class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-1 h-7 w-full border px-2 text-xs outline-none"
          @change="fetchPlan"
        />
      </label>

      <!-- 音色参考音频 -->
      <div class="grid grid-cols-2 gap-2">
        <label class="block">
          <span class="text-fg-4 text-2xs">音色参考音频（几秒干净人声）</span>
          <select
            v-model="voiceRefAssetId"
            class="border-line-1 bg-base-2 text-fg-1 mt-1 h-6 w-full border px-1.5 text-2xs outline-none"
            @change="fetchPlan"
          >
            <option :value="null">无音色参考</option>
            <option v-for="a in audioAssets" :key="a.id" :value="a.id">
              {{ a.path.split(/[/\\]/).pop() }}
            </option>
          </select>
        </label>

        <label class="flex items-center gap-1.5 cursor-pointer mt-4">
          <input v-model="withVideo" type="checkbox" class="accent-accent" @change="fetchPlan" />
          <span class="text-fg-2 text-2xs">参考画面（口型对齐 / S2V）</span>
        </label>
      </div>

      <!-- 配音账单结果列表 -->
      <div v-if="planResult && planResult.items.length" class="space-y-1.5 border-line-1 border-t pt-2">
        <div class="flex items-center justify-between text-2xs">
          <span class="text-fg-2 font-medium">拟配音镜头清单（共 {{ planResult.total }} 镜）</span>
        </div>
        <div class="border-line-1 max-h-32 overflow-y-auto border p-1 space-y-1 bg-base-2">
          <div
            v-for="item in planResult.items"
            :key="item.shot_id"
            class="flex items-center justify-between px-2 py-1 bg-base-1 text-2xs rounded-xs"
          >
            <span class="text-fg-1 font-medium">Shot {{ item.shot_index_no }}</span>
            <span class="text-fg-3 truncate max-w-[14rem]">{{ item.text || item.prompt }}</span>
            <AppBadge tone="accent">{{ item.duration }}s</AppBadge>
          </div>
        </div>
      </div>

      <!-- 跳过项提示 -->
      <div v-if="planResult?.skipped.length" class="border-line-1 bg-base-3 p-2 text-2xs border">
        <span class="text-fg-4">跳过 {{ planResult.skipped.length }} 个镜头（缺少台词和声音描述）</span>
      </div>
    </div>

    <template #footer>
      <div class="ml-auto flex items-center gap-2">
        <AppButton size="sm" variant="ghost" @click="emit('update:open', false)">取消</AppButton>
        <AppButton
          size="sm"
          variant="primary"
          :disabled="busy || !planResult?.configured || !planResult?.items.length"
          @click="handleRun"
        >
          <Sparkles :size="12" />入队生成配音 ({{ planResult?.items.length || 0 }} 条)
        </AppButton>
      </div>
    </template>
  </AppDialog>
</template>
