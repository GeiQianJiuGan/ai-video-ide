<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import {
  frameVideoDraft,
  framesApi,
  type FrameCard,
  type FrameVideo,
  type SharedFramePrompts,
} from '@/shared/api/frames'
import { assetsApi, type Asset } from '@/shared/api/assets'
import { fileUrl } from '@/shared/api/files'
import AppButton from '@/shared/ui/AppButton.vue'
import { useShellStore } from '@/stores/shell'
import { frameCardImageState, frameVideoChecks } from '@/features/frames/frameVideoReadiness'

const props = defineProps<{
  pid: string
  shotId: string
  duration: number
  sourceVideo?: boolean
}>()
const shell = useShellStore()
const cards = ref<FrameCard[]>([])
const assets = ref<Asset[]>([])
const video = ref<FrameVideo>(frameVideoDraft({}, props.duration))
const presets = ref<{ name: string; first_frame_ok: boolean; flf_ready: boolean }[]>([])
const loaded = ref<{ pid: string; sid: string } | null>(null)
const error = ref('')
const loadError = ref('')
const configError = ref('')
const dirty = ref(false)
const loading = ref(false)
const checking = ref(false)
const inputPlan = ref<{ unused_cards: string[] } | null>(null)
let revision = 0
let request = 0
let pending: Promise<unknown> | null = null
const selected = computed(() =>
  [video.value.first_card_id, video.value.last_card_id]
    .filter(Boolean)
    .map((id) => cards.value.find((c) => c.id === id))
    .filter((c): c is FrameCard => !!c),
)
const checks = computed(() =>
  frameVideoChecks({ ...video.value, duration: props.duration }, cards.value, assets.value),
)
const inputBlocker = computed(() => {
  if (loading.value) return '正在读取制帧输入，请稍候。'
  if (loadError.value) return `制帧输入读取失败：${loadError.value}。请点击「刷新输入」。`
  if (loaded.value?.pid !== props.pid || loaded.value?.sid !== props.shotId)
    return '制帧输入尚未读取，请点击「刷新输入」。'
  return checks.value.find((check) => !check.ready)?.detail || ''
})
const ready = computed(() => !inputBlocker.value)
const url = (card: FrameCard) => {
  const asset = assets.value.find((a) => a.id === card.adopted_asset_id)
  return asset ? fileUrl(props.pid, asset.path) : ''
}
const message = (e: unknown) =>
  e instanceof Error
    ? e.message +
      ('detail' in e ? `：${e.detail}` : '') +
      ('suggestions' in e && Array.isArray(e.suggestions) ? `。${e.suggestions.join('；')}` : '')
    : String(e)
function markDirty() {
  revision++
  dirty.value = true
  inputPlan.value = null
}
async function save(): Promise<void> {
  if (pending) await pending
  if (!dirty.value || !loaded.value) return
  const current = { ...loaded.value },
    rev = revision
  pending = framesApi.saveVideo(current.pid, current.sid, { ...video.value })
  try {
    await pending
    if (rev === revision) dirty.value = false
  } finally {
    pending = null
  }
  if (dirty.value) await save()
}
async function changed() {
  revision++
  dirty.value = true
  inputPlan.value = null
  try {
    await save()
    error.value = ''
  } catch (e) {
    error.value = message(e)
  }
}
async function loadInputs() {
  const ticket = ++request
  const pid = props.pid,
    sid = props.shotId
  loading.value = true
  error.value = ''
  loadError.value = ''
  configError.value = ''
  try {
    await save()
    const [workspace, all, extracted, cap] = await Promise.all([
      framesApi.get(pid, sid),
      assetsApi.list(pid),
      assetsApi.list(pid, 'frame'),
      framesApi.capabilities(pid).then(
        (data) => ({ data, error: '' }),
        (e: unknown) => ({ data: null, error: message(e) }),
      ),
    ])
    if (ticket !== request) return
    loaded.value = { pid, sid }
    video.value = {
      ...frameVideoDraft(workspace.video, props.duration),
      duration: props.duration,
    }
    cards.value = workspace.cards
    assets.value = [...all, ...extracted]
    presets.value = cap.data?.presets || []
    configError.value = cap.error
    inputPlan.value = null
  } catch (e) {
    if (ticket === request) loadError.value = message(e)
  } finally {
    if (ticket === request) loading.value = false
  }
}
watch(() => [props.pid, props.shotId, shell.appliedTick], loadInputs, { immediate: true })
async function reload() {
  if (!loading.value && !checking.value) await loadInputs()
}
function onFocus() {
  if (!document.hidden) void reload()
}
onMounted(() => window.addEventListener('focus', onFocus))
onBeforeUnmount(() => {
  request++
  window.removeEventListener('focus', onFocus)
})
async function check() {
  checking.value = true
  try {
    if (!ready.value) {
      error.value =
        checks.value
          .filter((item) => !item.ready)
          .map((item) => `${item.label}：${item.detail}`)
          .join('；') || inputBlocker.value
      return
    }
    await save()
    inputPlan.value = await framesApi.plan(props.pid, props.shotId, {
      ...video.value,
      duration: props.duration,
    })
    error.value = ''
  } catch (e) {
    error.value = message(e)
  } finally {
    checking.value = false
  }
}
async function leave() {
  try {
    await save()
  } catch (e) {
    error.value = message(e)
    return false
  }
}
onBeforeRouteLeave(leave)
onBeforeRouteUpdate(leave)
function applySharedPrompts(data: SharedFramePrompts) {
  video.value.shared_prompt = data.video_prompt.trim()
  if (data.clear_overrides) video.value.prompt = ''
  inputPlan.value = null
}
defineExpose({ save, ready, blocker: inputBlocker, reload, applySharedPrompts })
</script>

<template>
  <section class="border-line-1 border-t pt-2 space-y-2" aria-label="制帧视频输入">
    <p v-if="error" role="alert" class="text-st-failed text-2xs">{{ error }}</p>
    <p v-if="loadError" role="alert" class="text-st-failed text-2xs">{{ loadError }}</p>
    <div class="flex items-center justify-between">
      <strong class="text-fg-2 text-2xs">生成条件检查</strong>
      <AppButton size="sm" variant="ghost" :disabled="loading || checking" @click="reload"
        >刷新输入</AppButton
      >
    </div>
    <p v-if="loading" class="text-fg-4 text-2xs">正在读取制帧输入…</p>
    <template v-else-if="loaded?.pid === pid && loaded?.sid === shotId">
      <ul class="space-y-1 text-2xs" aria-label="制帧生成条件">
        <li
          v-for="item in checks"
          :key="item.key"
          :class="item.ready ? 'text-fg-3' : 'text-st-review'"
        >
          <strong>{{ item.ready ? '✓' : '待补齐' }} · {{ item.label }}</strong
          >：{{ item.detail }}
        </li>
      </ul>
      <RouterLink
        v-if="checks.some((item) => !item.ready && (item.key === 'first' || item.key === 'last'))"
        :to="{ name: 'frames', params: { pid }, query: { shot: shotId } }"
        class="text-accent text-2xs block"
        >去制作图片，审阅并采用候选 →</RouterLink
      >
      <p v-if="configError" class="text-st-review text-2xs">
        视频配置读取失败：{{ configError }}。可点击「检查实际输入」查看原因。
      </p>
      <div class="flex items-center justify-between">
        <span class="text-fg-3 text-2xs font-medium">Prompt 指令</span>
        <span class="text-accent text-3xs">制帧视频描述</span>
      </div>
      <textarea
        v-model="video.prompt"
        rows="4"
        aria-label="制帧视频提示词"
        class="border-line-1 bg-base-2 text-fg-1 w-full border p-1.5 text-2xs"
        :placeholder="
          video.shared_prompt
            ? `留空继承本幕：${video.shared_prompt}`
            : '描述帧图之间的动作、运镜、对白与声音'
        "
        @input="markDirty"
        @change="changed"
      />
      <div class="flex items-center justify-between">
        <span class="text-fg-3 text-2xs font-medium">帧图引用</span>
        <RouterLink
          :to="{ name: 'frames', params: { pid }, query: { shot: shotId } }"
          class="text-accent text-3xs"
          >制作图片 / 规划画面</RouterLink
        >
      </div>
      <p class="text-fg-4 text-3xs">视频采用制作图片中已选定的帧图。</p>
      <RouterLink
        v-if="sourceVideo"
        :to="{ name: 'frames', params: { pid }, query: { shot: shotId, pick_frame: '1' } }"
        class="text-accent text-2xs block"
        >从本段视频选择制图画面</RouterLink
      >
      <label class="block text-2xs"
        >首帧<select
          v-model="video.first_card_id"
          class="border-line-1 bg-base-2 mt-1 w-full border p-1"
          @change="changed"
        >
          <option value="">选择帧图</option>
          <option v-for="card in cards" :key="card.id" :value="card.id">
            {{ card.title }}（{{ frameCardImageState(card) }}）
          </option>
        </select></label
      >
      <label class="block text-2xs"
        >尾帧（可选）<select
          v-model="video.last_card_id"
          class="border-line-1 bg-base-2 mt-1 w-full border p-1"
          @change="changed"
        >
          <option value="">仅使用首帧</option>
          <option v-for="card in cards" :key="card.id" :value="card.id">
            {{ card.title }}（{{ frameCardImageState(card) }}）
          </option>
        </select></label
      >
      <div class="flex gap-2">
        <figure
          v-for="(card, index) in selected"
          :key="`${index}-${card.id}`"
          class="min-w-0 flex-1"
        >
          <img
            v-if="url(card)"
            :src="url(card)"
            :alt="card.title"
            class="h-20 w-full object-contain"
          />
          <figcaption class="text-fg-4 text-3xs">
            {{ index === 0 ? '首帧' : '尾帧' }} · {{ card.title }}
          </figcaption>
        </figure>
      </div>
      <label v-if="presets.length" class="block text-2xs"
        >视频预设<select
          v-model="video.preset"
          class="border-line-1 bg-base-2 mt-1 w-full border p-1"
          @change="changed"
        >
          <option value="">跟随项目默认</option>
          <option
            v-for="preset in presets.filter(
              (p) => p.first_frame_ok && (!video.last_card_id || p.flf_ready),
            )"
            :key="preset.name"
            :value="preset.name"
          >
            {{ preset.name }}
          </option>
        </select></label
      >
      <AppButton size="sm" variant="ghost" :disabled="checking || loading" @click="check"
        >检查实际输入</AppButton
      >
      <p v-if="ready && !inputPlan" class="text-fg-4 text-3xs">
        基础输入已齐全；「检查实际输入」可进一步验证生成服务、工作流与图片来源。
      </p>
      <p v-if="inputPlan" class="text-fg-4 text-3xs">
        首尾帧输入已通过检查；其余 {{ inputPlan.unused_cards.length }} 个画面本次不提交。
      </p>
    </template>
    <p v-else class="text-fg-4 text-2xs">尚未读到本镜头的制帧输入，请点击「刷新输入」。</p>
  </section>
</template>
