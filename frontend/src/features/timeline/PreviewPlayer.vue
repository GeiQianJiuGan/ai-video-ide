<script setup lang="ts">
/**
 * 成片预览器：**逐段实时播放**，不渲染任何中间文件。
 *
 * 一个 `<video>` 按视频轨的顺序换 src，音频轨上每一段各挂一个 `<audio>`，播放头是唯一
 * 的真相，谁都从它算自己该停在哪一秒。四个刻意的取舍：
 *
 *   1. **不预渲染**。要「实时看整体效果」，最快的路是让浏览器直接播原始文件——拖到哪
 *      看到哪，不用等 FFmpeg。代价是换段时有一小段接缝、转场与特效不渲染，这两句必须
 *      写在界面上（说「实时预览」却和成片不一样，比没有预览更糟）。
 *   2. **画面只认第一条视频轨**，和导出（`timeline.build_command`）同一条规矩。预览器
 *      自己另挑一条的话，看到的就不是将要导出的东西。
 *   3. **空白视频段是真黑场**：这是时间线上的显式片段，预览与导出都会保留；历史数据中
 *      若仍有真正的空档，服务层会在下一次编辑时自动贴紧视频轨。
 *   4. **音量在预览里最高只能到 1.0**（HTMLMediaElement 的硬限制），而后端允许到 4。
 *      放大过的片段在这里听起来偏轻，这件事要说出来而不是让人以为设置没生效。
 *
 * 播放头是双向的（`v-model:playhead`）：轨道区拖标尺 → 这里跟着 seek；这里播着 →
 * 轨道区的竖条跟着走。内部另存一份 `at`，为的是**先把值定下来再去 seek**——直接读 prop
 * 的话，父组件还没重渲染，媒体元素会按上一帧的位置去对。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Pause, Play, SkipBack } from '@lucide/vue'
import { fileUrl } from '@/shared/api/files'
import type { Clip, Timeline } from '@/shared/api/timeline'

const props = defineProps<{
  pid: string
  timeline: Timeline | null
  playhead: number
}>()

const emit = defineEmits<{ 'update:playhead': [number] }>()

const EPS = 1e-4

const at = ref(props.playhead)
const playing = ref(false)
const videoEl = ref<HTMLVideoElement | null>(null)
/** 音频元素按片段 id 存：`v-for` 里拿不到稳定的下标，删一段会串。 */
const audioEls = new Map<string, HTMLAudioElement>()
/** 播不了要说出来（大多是浏览器解不了这个编码，导出照旧能用 FFmpeg 走完）。 */
const mediaError = ref('')

let frame = 0
let wall = 0

const videoTrack = computed(() => props.timeline?.tracks.find((t) => t.kind === 'video') ?? null)
/** 节目单：第一条视频轨，按时间线位置排。 */
const program = computed<Clip[]>(() =>
  [...(videoTrack.value?.clips ?? [])].sort((a, b) => a.start - b.start),
)
const videoMuted = computed(() => Boolean(videoTrack.value?.muted))
const total = computed(() => props.timeline?.duration_total ?? 0)

function playable(clip: Clip): boolean {
  return Boolean(clip.asset_path) && !clip.missing_file
}

const active = computed<Clip | null>(
  () =>
    program.value.find(
      (c) => at.value >= c.start - 1e-6 && at.value < c.start + c.duration - 1e-6,
    ) ?? null,
)
const activeSrc = computed(() => {
  const clip = active.value
  return clip && clip.asset_path && playable(clip) ? fileUrl(props.pid, clip.asset_path) : ''
})
const activeIsBlank = computed(() => Boolean(active.value && !active.value.asset_path))

interface Part {
  clip: Clip
  url: string
  muted: boolean
  volume: number
}

/** 音频轨上能出声的片段。轨道静音、片段静音、音量 0 都还挂着元素，只是不出声。 */
const parts = computed<Part[]>(() => {
  const out: Part[] = []
  for (const track of props.timeline?.tracks ?? []) {
    if (track.kind !== 'audio') continue
    for (const clip of track.clips) {
      if (!clip.asset_path || clip.missing_file) continue
      out.push({
        clip,
        url: fileUrl(props.pid, clip.asset_path),
        muted: Boolean(track.muted) || Boolean(clip.muted),
        volume: Math.min(1, Math.max(0, clip.volume)),
      })
    }
  }
  return out
})

/** 兼容历史数据：显式空白片段不是空档；只统计真正没有片段的时间。 */
const gapSeconds = computed(() => {
  let gaps = 0
  let prev = 0
  for (const clip of program.value) {
    gaps += Math.max(0, clip.start - prev)
    prev = clip.start + clip.duration
  }
  return gaps
})
const brokenCount = computed(() => program.value.filter((c) => !playable(c)).length)
/** 有没有片段被放大过（预览到不了那个音量）。 */
const boosted = computed(
  () => program.value.some((c) => c.volume > 1) || parts.value.some((p) => p.clip.volume > 1),
)

function clamp(value: number): number {
  return Math.max(0, Math.min(total.value, Math.round(value * 1000) / 1000))
}

function videoTime(clip: Clip): number {
  return clip.in_point + Math.max(0, at.value - clip.start)
}

function setAudioEl(id: string, el: unknown): void {
  if (el) audioEls.set(id, el as HTMLAudioElement)
  else audioEls.delete(id)
}

function applyVideoMix(): void {
  const el = videoEl.value
  const clip = active.value
  if (!el || !clip) return
  el.muted = videoMuted.value || Boolean(clip.muted)
  el.volume = Math.min(1, Math.max(0, clip.volume))
}

/** 让所有音频元素对准播放头。`force` 用于拖动 / 换段，平时只在偏差超过 0.25s 时纠。 */
function syncAudio(force: boolean): void {
  for (const part of parts.value) {
    const el = audioEls.get(part.clip.id)
    if (!el) continue
    el.muted = part.muted
    el.volume = part.volume
    const end = part.clip.start + part.clip.duration
    const inside = at.value >= part.clip.start - 1e-6 && at.value < end - 1e-6
    if (!inside) {
      if (!el.paused) el.pause()
      continue
    }
    const want = part.clip.in_point + (at.value - part.clip.start)
    if (force || Math.abs(el.currentTime - want) > 0.25) {
      try {
        el.currentTime = want
      } catch {
        /* 元数据还没到，@loadedmetadata 之后的下一次 sync 会补上 */
      }
    }
    if (playing.value && el.paused) void el.play().catch(() => {})
    if (!playing.value && !el.paused) el.pause()
  }
}

/** 拖动播放头之后让媒体追上来。换段时元素会被重建，真正的 seek 在 `onLoaded` 里。 */
function seekAll(): void {
  const clip = active.value
  const el = videoEl.value
  if (clip && el) {
    try {
      el.currentTime = videoTime(clip)
    } catch {
      /* 同上 */
    }
  }
  syncAudio(true)
}

function tick(now: number): void {
  frame = requestAnimationFrame(tick)
  const clip = active.value
  const el = videoEl.value
  const dt = wall ? Math.max(0, (now - wall) / 1000) : 0
  wall = now
  let next: number
  if (clip && el && el.readyState >= 2 && !el.seeking) {
    // 时钟跟着画面走：这样音频对齐的是「真的播到哪了」，不是墙上时间
    next = clip.start + Math.max(0, el.currentTime - clip.in_point)
  } else {
    // 空档 / 文件缺失 / 还在加载：没有画面能报时，只能按墙上时间推
    next = at.value + dt
  }
  if (next >= total.value - 1e-3) {
    at.value = total.value
    pause()
    return
  }
  at.value = clamp(next)
  syncAudio(false)
}

/**
 * 窗口被藏起来时（切到别的页、最小化）浏览器**不再跑 rAF**，`tick` 就停了，而声音和画面
 * 照旧在播——回来一看播放头停在离开时那一秒，和实际听到的对不上。`timeupdate` 隐藏时
 * 每秒还有几次，用它把播放头按画面的真实位置推着走：rAF 管顺滑，这个管不说谎。
 */
function onTimeUpdate(): void {
  if (!playing.value || !document.hidden) return
  const clip = active.value
  const el = videoEl.value
  if (!clip || !el) return
  const next = clip.start + Math.max(0, el.currentTime - clip.in_point)
  if (next >= total.value - 1e-3) {
    at.value = total.value
    pause()
    return
  }
  at.value = clamp(next)
  syncAudio(false)
}

async function play(): Promise<void> {
  if (total.value <= 0) return
  mediaError.value = ''
  if (at.value >= total.value - 1e-3) at.value = 0
  playing.value = true
  wall = 0
  const el = videoEl.value
  if (el) {
    try {
      await el.play()
    } catch (err) {
      playing.value = false
      mediaError.value = `浏览器拒绝播放这一段：${String(err)}。`
      return
    }
  }
  syncAudio(true)
  if (!frame) frame = requestAnimationFrame(tick)
}

function pause(): void {
  playing.value = false
  if (frame) {
    cancelAnimationFrame(frame)
    frame = 0
  }
  wall = 0
  videoEl.value?.pause()
  for (const el of audioEls.values()) if (!el.paused) el.pause()
}

function toggle(): void {
  if (playing.value) pause()
  else void play()
}

function rewind(): void {
  at.value = 0
  seekAll()
}

function onScrub(value: string): void {
  const next = Number(value)
  if (!Number.isFinite(next)) return
  at.value = clamp(next)
  seekAll()
}

function onLoaded(): void {
  const el = videoEl.value
  const clip = active.value
  if (!el || !clip) return
  try {
    el.currentTime = videoTime(clip)
  } catch {
    /* 极少数容器读不出 duration，让它从头播总比停住好 */
  }
  applyVideoMix()
  if (playing.value) void el.play().catch(() => {})
}

/** 这一段播完了：把播放头推过它的尾巴，下一段（或空档）自然接上。 */
function onEnded(): void {
  const clip = active.value
  if (!clip) return
  at.value = clamp(clip.start + clip.duration + 0.005)
  wall = 0
  if (at.value >= total.value - 1e-3) pause()
}

function onVideoError(): void {
  const clip = active.value
  pause()
  mediaError.value = clip
    ? `这一段在浏览器里播不了：${clip.label ?? clip.id}（${clip.asset_path ?? '没有文件'}）。` +
      '多半是浏览器解不了这个编码——导出走 FFmpeg，不受这个限制。'
    : '预览播放失败。'
}

function onPartError(part: Part): void {
  mediaError.value =
    `音频段「${part.clip.label ?? part.clip.id}」在浏览器里播不了（${part.clip.asset_path}）。` +
    '导出走 FFmpeg，不一定受影响。'
}

// 外部（轨道区标尺）拖动播放头：同步进来并让媒体追上
watch(
  () => props.playhead,
  (value) => {
    if (Math.abs(value - at.value) < EPS) return
    at.value = clamp(value)
    seekAll()
  },
)
watch(at, (value) => {
  if (Math.abs(value - props.playhead) > EPS) emit('update:playhead', value)
})
watch([active, videoMuted], () => applyVideoMix())
// 片段被删光 / 换了工程：正在播的东西已经不存在了，别让时钟继续跑
watch(total, (value) => {
  if (value <= 0) pause()
  else if (at.value > value) {
    at.value = value
    seekAll()
  }
})

onBeforeUnmount(pause)

function stamp(n: number): string {
  const s = Math.max(0, n)
  const m = Math.floor(s / 60)
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}.${Math.floor((s % 1) * 10)}`
}
</script>

<template>
  <div class="border-line-1 bg-base-1 flex min-h-0 flex-col border">
    <div class="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-black">
      <!-- 一个 video 逐段换 src：key 换掉元素，loadedmetadata 才一定会再来一次 -->
      <video
        v-if="activeSrc"
        ref="videoEl"
        :key="activeSrc"
        :src="activeSrc"
        class="h-full w-full object-contain"
        preload="auto"
        playsinline
        @loadedmetadata="onLoaded"
        @timeupdate="onTimeUpdate"
        @ended="onEnded"
        @error="onVideoError"
      />
      <p v-else-if="program.length === 0" class="text-fg-4 px-3 text-center text-2xs">
        视频轨上还没有片段。先「自动装配」把镜头铺上来，这里就能看整体效果了。
      </p>
      <p v-else-if="activeIsBlank" class="text-fg-3 px-3 text-center text-2xs">
        空白视频段 · {{ active?.duration.toFixed(2) }} 秒
      </p>
      <p v-else-if="active && !playable(active)" class="text-st-review px-3 text-center text-2xs">
        这一段登记的文件已经不在磁盘上，预览跳过它。
      </p>
      <p v-else class="text-fg-4 px-3 text-center text-2xs">
        当前播放头不在视频片段内。
      </p>
      <span
        v-if="active"
        class="text-fg-2 absolute top-1 left-1 max-w-[70%] truncate bg-black/60 px-1 text-2xs"
      >
        {{ active.shot_index_no ?? '—' }}. {{ active.label ?? '未命名片段' }}
        <template v-if="active.version_no">· v{{ active.version_no }}</template>
        <template v-if="active.muted">· 已静音</template>
      </span>
    </div>

    <div class="border-line-1 flex items-center gap-1.5 border-t px-1.5 py-1">
      <button
        class="text-fg-2 hover:text-fg-1 disabled:text-fg-4 shrink-0"
        :disabled="total <= 0"
        :title="playing ? '暂停' : '从播放头开始播'"
        @click="toggle()"
      >
        <Pause v-if="playing" :size="12" />
        <Play v-else :size="12" />
      </button>
      <button
        class="text-fg-2 hover:text-fg-1 disabled:text-fg-4 shrink-0"
        :disabled="total <= 0"
        title="回到开头"
        @click="rewind()"
      >
        <SkipBack :size="12" />
      </button>
      <input
        type="range"
        min="0"
        :max="Math.max(0.01, total)"
        step="0.01"
        :value="at"
        :disabled="total <= 0"
        class="accent-accent min-w-0 flex-1"
        title="拖动预览。轨道区的竖条是同一个播放头"
        @input="onScrub(($event.target as HTMLInputElement).value)"
      />
      <span class="text-fg-3 tnum shrink-0 text-2xs">{{ stamp(at) }} / {{ stamp(total) }}</span>
    </div>

    <div class="border-line-1 space-y-px border-t px-1.5 py-1">
      <p class="text-fg-4 text-2xs">
        逐段近似预览：换段时会有一小段接缝，转场 / 特效在这里不渲染——最终效果看导出。
      </p>
      <p v-if="gapSeconds > 0.05" class="text-fg-4 text-2xs">
        历史数据中仍有 {{ gapSeconds.toFixed(2) }} 秒视频空档；编辑视频轨时会自动贴紧。
      </p>
      <p v-if="boosted" class="text-fg-4 text-2xs">
        有片段音量大于 1：预览最高只能到 1.0（浏览器限制），导出会按设定值放大。
      </p>
      <p v-if="brokenCount" class="text-st-review text-2xs">
        {{ brokenCount }} 段的文件已经不在磁盘上，预览与导出都会出问题。
      </p>
      <p v-if="mediaError" class="text-st-review text-2xs">{{ mediaError }}</p>
    </div>

    <!-- 音频轨：每段一个元素，叠加就是同时播好几个 -->
    <audio
      v-for="p in parts"
      :key="p.clip.id"
      :ref="(el) => setAudioEl(p.clip.id, el)"
      :src="p.url"
      preload="auto"
      class="hidden"
      @error="onPartError(p)"
    />
  </div>
</template>
