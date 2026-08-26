<script setup lang="ts">
/**
 * 单段预览器：**进度条只有这一段**。
 *
 * 长视频切段的版本共用一个源文件，各自只有一对 `in_point` / `out_point`（没有第二份
 * 文件，也刻意不去切一份出来——那要重编码，还会把「版本只增不改」搞成一堆临时产物）。
 * 于是「预览这一段」有两种做法，这里选了后一种：
 *
 *   1. 用浏览器原生 `controls` + `#t=in,out` 片段锚点，再靠 `timeupdate` 把播放头
 *      拉回来。**这是假的**：原生进度条量的是整个文件，一段 3 秒的镜头在 40 分钟的
 *      长片里只有半个像素宽，拖到哪都是别的镜头，还随时能拖出区间外；
 *   2. 关掉原生 `controls`，自己画一条 **0 ~ 本段时长** 的进度条，内部再换算成文件里的
 *      绝对秒数（`file = in + local`，与 `PreviewPlayer` 的 `local = currentTime -
 *      in_point` 是同一条换算）。用户看到的时间轴与这一段一一对应，拖不出去。
 *
 * 三件不许省的事：
 *   · **区间比文件长要说出来**（`rangeWarning`）。这多半是别的地方算错了区间，
 *     悄悄按文件长度截断的话，人只会觉得「这段怎么少了一截」；
 *   · **播不了要显示原因**（浏览器解不了这个编码是常事；导出走 FFmpeg 不受影响）；
 *   · 没有区间（普通成片）时照样能用：此时这一段就是整个文件。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Pause, Play, RotateCcw } from '@lucide/vue'

const props = withDefaults(
  defineProps<{
    src: string
    /** 段起点（文件里的绝对秒数）。null = 从文件开头。 */
    inPoint?: number | null
    /** 段终点（文件里的绝对秒数）。null = 到文件末尾。 */
    outPoint?: number | null
    poster?: string
    autoplay?: boolean
    /** 播完这一段是否从段首重播。默认停在段尾。 */
    loop?: boolean
  }>(),
  { inPoint: null, outPoint: null, poster: '', autoplay: false, loop: false },
)

const EPS = 1e-3

const el = ref<HTMLVideoElement | null>(null)
const playing = ref(false)
/** 段内位置（0 ~ 段时长），界面上那条进度条量的就是它。 */
const local = ref(0)
/** 文件总长，`loadedmetadata` 之后才知道。 */
const fileDuration = ref(0)
const mediaError = ref('')

let frame = 0

const segIn = computed(() => Math.max(0, props.inPoint ?? 0))
/** 段尾：给了 out_point 就用它，否则到文件末尾（metadata 还没到时先当 0）。 */
const segEnd = computed(() => {
  const want = props.outPoint ?? fileDuration.value
  if (!fileDuration.value) return Math.max(segIn.value, want)
  return Math.min(fileDuration.value, Math.max(segIn.value, want))
})
const segDuration = computed(() => Math.max(0, segEnd.value - segIn.value))

/** 区间超出了文件——不静默截断，把这句显示出来。 */
const rangeWarning = computed(() => {
  if (!fileDuration.value) return ''
  const want = props.outPoint
  if (want != null && want > fileDuration.value + 0.05) {
    return `这一段的区间标到 ${want.toFixed(2)}s，但文件只有 ${fileDuration.value.toFixed(2)}s，末尾这一截并不存在。`
  }
  if (segIn.value > fileDuration.value - 0.05) {
    return `这一段的起点 ${segIn.value.toFixed(2)}s 已经在文件末尾之后（文件长 ${fileDuration.value.toFixed(2)}s），没有画面可放。`
  }
  return ''
})

function clampLocal(value: number): number {
  return Math.max(0, Math.min(segDuration.value, Math.round(value * 1000) / 1000))
}

function seekTo(value: number): void {
  const media = el.value
  local.value = clampLocal(value)
  if (!media) return
  try {
    media.currentTime = segIn.value + local.value
  } catch {
    /* metadata 还没到，@loadedmetadata 里会按 local 补一次 */
  }
}

function stopClock(): void {
  if (frame) {
    cancelAnimationFrame(frame)
    frame = 0
  }
}

/** 时钟跟着画面走：段尾由这里认出来（文件还没播完，`ended` 不会来）。 */
function tick(): void {
  frame = requestAnimationFrame(tick)
  const media = el.value
  if (!media) return
  const at = media.currentTime - segIn.value
  if (segDuration.value > 0 && at >= segDuration.value - EPS) {
    if (props.loop) {
      seekTo(0)
      return
    }
    local.value = segDuration.value
    pause()
    return
  }
  local.value = clampLocal(at)
}

async function play(): Promise<void> {
  const media = el.value
  if (!media || segDuration.value <= 0) return
  mediaError.value = ''
  if (local.value >= segDuration.value - EPS) seekTo(0)
  else seekTo(local.value)
  playing.value = true
  try {
    await media.play()
  } catch (err) {
    playing.value = false
    mediaError.value = `浏览器拒绝播放：${String(err)}`
    return
  }
  if (!frame) frame = requestAnimationFrame(tick)
}

function pause(): void {
  playing.value = false
  stopClock()
  el.value?.pause()
}

function toggle(): void {
  if (playing.value) pause()
  else void play()
}

function restart(): void {
  seekTo(0)
  if (!playing.value) void play()
}

function onLoadedMetadata(): void {
  const media = el.value
  if (!media) return
  fileDuration.value = Number.isFinite(media.duration) ? media.duration : 0
  seekTo(local.value)
  if (props.autoplay && !playing.value) void play()
}

function onError(): void {
  pause()
  mediaError.value =
    '这一段在浏览器里播不了，多半是解不了这个编码。导出走 FFmpeg，不受这个限制。'
}

/** 换段 / 换文件：回到新段的段首，别停在上一段的位置上。 */
watch(
  () => [props.src, props.inPoint, props.outPoint],
  () => {
    pause()
    fileDuration.value = 0
    mediaError.value = ''
    local.value = 0
    seekTo(0)
  },
)

onBeforeUnmount(() => pause())

function stamp(n: number): string {
  const s = Math.max(0, n)
  const m = Math.floor(s / 60)
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}.${Math.floor((s % 1) * 10)}`
}

defineExpose({ play, pause, restart })
</script>

<template>
  <div class="flex min-h-0 flex-col">
    <div class="relative flex min-h-0 flex-1 items-center justify-center bg-black">
      <!--
        原生 controls 刻意不开：它的进度条量的是整个文件，一段几秒的镜头在长片里连一个
        像素都占不到，拖到哪都是别的镜头。下面那条进度条只有本段。
      -->
      <video
        ref="el"
        :src="src"
        :poster="poster || undefined"
        preload="metadata"
        playsinline
        class="max-h-full w-full object-contain"
        @loadedmetadata="onLoadedMetadata"
        @error="onError"
        @click="toggle()"
      />
    </div>

    <div class="border-line-1 flex items-center gap-1.5 border-t px-1.5 py-1">
      <button
        class="text-fg-2 hover:text-fg-1 disabled:text-fg-4 shrink-0"
        :disabled="segDuration <= 0"
        :title="playing ? '暂停' : '播放这一段'"
        @click="toggle()"
      >
        <Pause v-if="playing" :size="12" />
        <Play v-else :size="12" />
      </button>
      <button
        class="text-fg-2 hover:text-fg-1 disabled:text-fg-4 shrink-0"
        :disabled="segDuration <= 0"
        title="回到本段开头"
        @click="restart()"
      >
        <RotateCcw :size="12" />
      </button>
      <input
        type="range"
        min="0"
        :max="Math.max(0.01, segDuration)"
        step="0.01"
        :value="local"
        :disabled="segDuration <= 0"
        class="accent-accent min-w-0 flex-1"
        title="这条进度条只有本段：0 就是本段开头"
        @input="seekTo(Number(($event.target as HTMLInputElement).value))"
      />
      <span class="text-fg-3 tnum shrink-0 text-2xs">
        {{ stamp(local) }} / {{ stamp(segDuration) }}
      </span>
    </div>
    <p
      v-if="inPoint != null || outPoint != null"
      class="text-fg-4 px-1.5 pb-0.5 text-3xs"
      title="进度条是本段的相对时间；这里是它在源文件里的位置"
    >
      源文件区间 {{ segIn.toFixed(2) }}s ~ {{ segEnd.toFixed(2) }}s（同一个文件里的一段，没有另存副本）
    </p>
    <p v-if="rangeWarning" class="text-st-review px-1.5 pb-0.5 text-2xs">{{ rangeWarning }}</p>
    <p v-if="mediaError" class="text-st-review px-1.5 pb-0.5 text-2xs">{{ mediaError }}</p>
  </div>
</template>
