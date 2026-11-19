<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import SegmentPlayer from '@/shared/ui/SegmentPlayer.vue'
import { framesApi, type FrameCard, type FrameSourceVideo } from '@/shared/api/frames'
import { fileUrl } from '@/shared/api/files'

const props = defineProps<{ open: boolean; pid: string; shotId: string; cards: FrameCard[] }>()
const emit = defineEmits<{
  'update:open': [boolean]
  done: [result: { card_id: string; asset_id: string }]
}>()
const source = ref<FrameSourceVideo | null>(null)
const player = ref<InstanceType<typeof SegmentPlayer> | null>(null)
const target = ref('')
const position = ref(0)
const busy = ref(false)
const loading = ref(false)
const error = ref('')
const selectedPreview = ref('')
const notice = ref('')
let revision = 0
const playable = computed(() => source.value && !/^([a-zA-Z]:[\\/]|\/|\\\\)/.test(source.value.path))
const message = (e: unknown) => e instanceof Error ? e.message + ('detail' in e ? `：${e.detail}` : '') : String(e)
watch(() => [props.open, props.pid, props.shotId] as const, async () => {
  const ticket = ++revision
  if (!props.open) { player.value?.pause(); return }
  loading.value = true
  error.value = ''
  notice.value = ''
  source.value = null
  selectedPreview.value = ''
  target.value = ''
  position.value = 0
  try {
    const data = await framesApi.sourceVideo(props.pid, props.shotId)
    if (ticket === revision) source.value = data
  } catch (e) {
    if (ticket === revision) error.value = message(e)
  } finally {
    if (ticket === revision) loading.value = false
  }
}, { immediate: true })
function previewTime() {
  player.value?.previewPosition(position.value)
}
async function select() {
  if (!source.value || busy.value) return
  const current = source.value
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const at = playable.value && player.value ? player.value.currentPosition() : current.in_point + position.value
    // 播放器在段尾暂停时，其时间会等于下一段起点，需退回本段。
    const selectedAt = at >= current.out_point ? Math.max(current.in_point, current.out_point - 0.08) : at
    const result = await framesApi.selectSourceFrame(props.pid, props.shotId, {
      source_version_id: current.source_version_id, at: selectedAt, card_id: target.value,
    })
    selectedPreview.value = fileUrl(props.pid, result.path)
    position.value = result.at - current.in_point
    notice.value = `已${target.value ? '更换制图参考' : '添加画面'}，本段 ${position.value.toFixed(3)} 秒。可继续选择其他画面。`
    emit('done', result)
  } catch (e) {
    error.value = message(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AppDialog :open="open" title="从本段视频选择画面" subtitle="暂停或拖动进度条，选择要用来制作图片的画面" size="lg" @update:open="!busy && emit('update:open', $event)">
    <div class="p-4 space-y-3 text-xs">
      <p v-if="loading">正在读取源片段…</p>
      <p v-if="error" role="alert" class="text-st-failed">{{ error }}</p>
      <template v-if="source">
        <SegmentPlayer v-if="playable" ref="player" :src="fileUrl(pid, source.path)" :in-point="source.in_point" :out-point="source.out_point" class="h-[42vh]" />
        <p v-else class="text-fg-4">源视频在工程外，浏览器无法直接预览。可填写本段内的秒数选帧，或将源视频复制进工程后预览。</p>
        <div class="flex items-center gap-2">
          <label>本段内时间（秒）<input v-model.number="position" type="number" min="0" :max="source.duration - 0.001" step="0.01" class="border-line-1 bg-base-2 ml-2 w-24 border p-1" @change="previewTime" /></label>
          <AppButton v-if="playable" size="sm" :disabled="busy" @click="previewTime">预览此位置</AppButton>
        </div>
        <label class="block">将选定画面用于
          <select v-model="target" :disabled="busy" class="border-line-1 bg-base-2 mt-1 w-full border p-2">
            <option value="">添加为新的制图画面</option>
            <option v-for="card in cards" :key="card.id" :value="card.id">更换「{{ card.title }}」的源片参考</option>
          </select>
        </label>
        <p class="text-fg-4">源片选帧用于制图参考。更换参考时保留提示词和已生成、已采用的图片；用新参考生成候选后再审阅采用。</p>
        <AppButton :disabled="busy || loading" @click="select">{{ busy ? '正在提取画面…' : target ? '用当前画面更换参考' : '添加当前画面' }}</AppButton>
        <p v-if="notice" role="status" class="text-accent">{{ notice }}</p>
        <img v-if="selectedPreview" :src="selectedPreview" alt="刚选定的制图参考画面" class="max-h-32 object-contain" />
      </template>
    </div>
  </AppDialog>
</template>
