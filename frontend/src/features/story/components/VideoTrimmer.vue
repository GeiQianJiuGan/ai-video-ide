<script setup lang="ts">
/**
 * 片头片尾裁切条：**切段之前先把不要的两头框出来。**
 *
 * 几乎每段成片前面都有一截台标 / 倒计时，后面还有一截字幕滚动。它们不该先被自动切成两个
 * 镜头、再让用户一个个删掉——所以导入的第二步是这一屏：先看，再框，然后才出切段账单。
 *
 * 三件事刻意这么做：
 *   · **这一屏量的是整个文件**（与 `SegmentPlayer` 相反：那个量的是一段）。选片头片尾就是
 *     在整片上挑两个绝对位置，所以原生 `controls` 在这里是对的，进度条和裁切条一一对应；
 *   · **只出一对数字，不动源文件**。区间会作为 `range_in` / `range_out` 送进 `ingest/plan`，
 *     源视频一帧都不裁（与「零文件复制」那条一致，改主意重新出一次账单就行）；
 *   · **探不出长度时不假装能拖**：`duration` 未知时禁用两个手柄并说明原因，而不是画一条
 *     长度是猜的轨道让用户在上面拖。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RotateCcw } from '@lucide/vue'

const props = withDefaults(
  defineProps<{
    src: string
    /** 后端探出来的文件时长（秒）。null = 探不出来，此时等 `loadedmetadata`。 */
    duration?: number | null
    rangeIn: number
    /** 片尾开始的位置；null = 到文件末尾。 */
    rangeOut: number | null
  }>(),
  { duration: null },
)

const emit = defineEmits<{
  'update:rangeIn': [number]
  'update:rangeOut': [number | null]
}>()

/** 保留区间不许比这更短：再短切不出任何一段（后端也会拒，见 `_range`）。 */
const MIN_KEEP = 0.5

const el = ref<HTMLVideoElement | null>(null)
const bar = ref<HTMLElement | null>(null)
const probed = ref(0)
const at = ref(0)
const mediaError = ref('')
let dragging: 'in' | 'out' | null = null

/** 文件总长：后端探出来的优先，其次是浏览器自己读到的。0 = 都不知道。 */
const total = computed(() => props.duration || probed.value || 0)
const usable = computed(() => total.value > MIN_KEEP)
const low = computed(() => Math.max(0, Math.min(props.rangeIn, Math.max(0, total.value - MIN_KEEP))))
const high = computed(() => {
  const want = props.rangeOut ?? total.value
  return total.value ? Math.min(total.value, Math.max(low.value + MIN_KEEP, want)) : want
})
const keep = computed(() => Math.max(0, high.value - low.value))

function pct(sec: number): number {
  if (!total.value) return 0
  return Math.max(0, Math.min(100, (sec / total.value) * 100))
}

function stamp(sec: number): string {
  const s = Math.max(0, sec)
  const m = Math.floor(s / 60)
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}.${Math.floor((s % 1) * 10)}`
}

function setIn(value: number): void {
  if (!total.value) return
  const next = Math.max(0, Math.min(value, high.value - MIN_KEEP))
  emit('update:rangeIn', Math.round(next * 1000) / 1000)
}

function setOut(value: number): void {
  if (!total.value) return
  const next = Math.min(total.value, Math.max(value, low.value + MIN_KEEP))
  //: 拖回文件末尾就是「片尾不裁」——存成 null 而不是那个恰好等于时长的数字，
  //: 不然长度探测差一点点就会莫名多出 0.05 秒的裁切。
  emit('update:rangeOut', next >= total.value - 0.05 ? null : Math.round(next * 1000) / 1000)
}

function seek(sec: number): void {
  const media = el.value
  at.value = Math.max(0, Math.min(total.value || sec, sec))
  if (!media) return
  try {
    media.currentTime = at.value
  } catch {
    /* metadata 还没到 */
  }
}

function secondsAt(event: PointerEvent | MouseEvent): number {
  const box = bar.value?.getBoundingClientRect()
  if (!box || !box.width || !total.value) return 0
  return ((event.clientX - box.left) / box.width) * total.value
}

function onBarPointerDown(event: PointerEvent): void {
  if (!usable.value) return
  seek(secondsAt(event))
}

function startDrag(which: 'in' | 'out', event: PointerEvent): void {
  if (!usable.value) return
  event.stopPropagation()
  dragging = which
  window.addEventListener('pointermove', onDrag)
  window.addEventListener('pointerup', endDrag)
}

function onDrag(event: PointerEvent): void {
  if (!dragging) return
  const sec = secondsAt(event)
  if (dragging === 'in') setIn(sec)
  else setOut(sec)
  //: 拖手柄时画面跟着走，否则「片头到底切在哪一格」只能靠数字猜。
  seek(sec)
}

function endDrag(): void {
  dragging = null
  window.removeEventListener('pointermove', onDrag)
  window.removeEventListener('pointerup', endDrag)
}

function onLoadedMetadata(): void {
  const media = el.value
  if (!media) return
  probed.value = Number.isFinite(media.duration) ? media.duration : 0
}

function onTimeUpdate(): void {
  if (dragging) return
  at.value = el.value?.currentTime ?? 0
}

function onError(): void {
  mediaError.value =
    '这段视频在浏览器里播不了（多半解不了这个编码）。裁切位置仍然可以按秒数填，切段与导出走 FFmpeg，不受影响。'
}

function reset(): void {
  emit('update:rangeIn', 0)
  emit('update:rangeOut', null)
}

watch(
  () => props.src,
  () => {
    probed.value = 0
    at.value = 0
    mediaError.value = ''
  },
)

onBeforeUnmount(endDrag)
</script>

<template>
  <div class="space-y-2">
    <div class="flex items-center justify-center bg-black">
      <!--
        这一屏挑的是整片上的两个绝对位置，所以原生 controls 在这里是对的：
        它的进度条与下面那条裁切条量的是同一段时间。
      -->
      <video
        ref="el"
        :src="src"
        controls
        preload="metadata"
        playsinline
        class="max-h-[38vh] w-full object-contain"
        @loadedmetadata="onLoadedMetadata"
        @timeupdate="onTimeUpdate"
        @error="onError"
      />
    </div>

    <!-- 裁切条：中间亮的一段才会进镜头，两头暗的是片头片尾 -->
    <div
      ref="bar"
      class="bg-base-3 relative h-7 w-full select-none"
      :class="usable ? 'cursor-pointer' : 'cursor-not-allowed opacity-60'"
      title="点一下跳到那里；拖两端的手柄框出要保留的部分"
      @pointerdown="onBarPointerDown"
    >
      <div class="bg-base-1/70 absolute inset-y-0 left-0" :style="{ width: `${pct(low)}%` }" />
      <div
        class="bg-accent/30 border-accent absolute inset-y-0 border-x-2"
        :style="{ left: `${pct(low)}%`, width: `${pct(high) - pct(low)}%` }"
      />
      <div
        class="bg-base-1/70 absolute inset-y-0 right-0"
        :style="{ width: `${100 - pct(high)}%` }"
      />
      <!-- 播放头 -->
      <div class="bg-fg-1 absolute inset-y-0 w-px" :style="{ left: `${pct(at)}%` }" />
      <!-- 两个手柄 -->
      <div
        class="bg-accent absolute inset-y-0 -ml-1 w-2 cursor-ew-resize"
        :style="{ left: `${pct(low)}%` }"
        title="片头结束的位置"
        @pointerdown="startDrag('in', $event)"
      />
      <div
        class="bg-accent absolute inset-y-0 -ml-1 w-2 cursor-ew-resize"
        :style="{ left: `${pct(high)}%` }"
        title="片尾开始的位置"
        @pointerdown="startDrag('out', $event)"
      />
    </div>

    <div class="flex flex-wrap items-center gap-2 text-2xs">
      <button
        class="border-line-1 text-fg-2 hover:text-fg-1 disabled:text-fg-4 border px-1.5 py-0.5"
        :disabled="!usable"
        title="把片头定在当前画面这一格"
        @click="setIn(at)"
      >
        以当前画面为片头
      </button>
      <button
        class="border-line-1 text-fg-2 hover:text-fg-1 disabled:text-fg-4 border px-1.5 py-0.5"
        :disabled="!usable"
        title="把片尾定在当前画面这一格"
        @click="setOut(at)"
      >
        以当前画面为片尾
      </button>
      <button
        class="border-line-1 text-fg-2 hover:text-fg-1 flex items-center gap-1 border px-1.5 py-0.5"
        title="整段都要（不裁片头片尾）"
        @click="reset()"
      >
        <RotateCcw :size="10" />整段都要
      </button>
      <span class="text-fg-3 tnum ml-auto">
        保留 {{ stamp(low) }} ~ {{ rangeOut == null ? '结尾' : stamp(high) }}
        （共 {{ keep.toFixed(2) }}s）
      </span>
    </div>

    <div class="grid grid-cols-2 gap-2">
      <label class="block">
        <span class="text-fg-4 text-2xs">片头结束（秒）</span>
        <input
          :value="low.toFixed(2)"
          type="number"
          min="0"
          step="0.1"
          class="border-line-1 bg-base-2 text-fg-1 mt-1 h-6 w-full border px-1.5 text-2xs outline-none"
          @change="setIn(Number(($event.target as HTMLInputElement).value))"
        />
      </label>
      <label class="block">
        <div class="flex items-center justify-between">
          <span class="text-fg-4 text-2xs">片尾开始（秒）</span>
          <span class="text-fg-4 text-2xs">空 = 到结尾</span>
        </div>
        <input
          :value="rangeOut == null ? '' : high.toFixed(2)"
          type="number"
          min="0"
          step="0.1"
          placeholder="到文件结尾"
          class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 mt-1 h-6 w-full border px-1.5 text-2xs outline-none"
          @change="
            ($event.target as HTMLInputElement).value.trim() === ''
              ? emit('update:rangeOut', null)
              : setOut(Number(($event.target as HTMLInputElement).value))
          "
        />
      </label>
    </div>

    <p class="text-fg-4 text-2xs leading-relaxed">
      裁掉的两头只是一对数字：源视频一帧都不会被改，切段只在保留区间里进行。改主意就把手柄拖回去，重新出一次账单。
    </p>
    <p v-if="!usable" class="text-st-review text-2xs">
      探不出这段视频的长度（ffprobe 与浏览器都没读到），裁切条没法按比例画——可以直接填秒数，或先确认这个文件能被读取。
    </p>
    <p v-if="mediaError" class="text-st-review text-2xs">{{ mediaError }}</p>
  </div>
</template>
