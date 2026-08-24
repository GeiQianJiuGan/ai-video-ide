<script setup lang="ts">
/**
 * 成片预览器：**逐段实时播放**，不渲染任何中间文件。
 *
 * **两个 `<video>` 轮换（双缓冲）**播视频轨，音频轨上每一段各挂一个 `<audio>`，播放头是
 * 唯一的真相，谁都从它算自己该停在哪一秒。四个刻意的取舍：
 *
 *   1. **不预渲染，但预载下一段**。要「实时看整体效果」，最快的路是让浏览器直接播原始
 *      文件——拖到哪看到哪，不用等 FFmpeg。代价是转场与特效不渲染，这句必须写在界面上
 *      （说「实时预览」却和成片不一样，比没有预览更糟）。
 *      换段那一下的停顿是另一回事，**它不是取舍而是缺陷**：以前一个 `<video>` 换 src，
 *      每到接缝就要销毁元素 → 重新取文件 → 等 metadata → seek，几百毫秒的黑屏。现在两
 *      个元素轮换：前台那个在播，后台那个已经把**下一段**取好并 seek 到它的 in_point
 *      （第一帧已解码好，只是被前台盖着），到接缝只需换一下 z-index 再 `play()`。
 *      代价是常驻多一个解码器 + 一段的缓冲，**只预载一段**，不做更深的预取。
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
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
/** 提前多少秒把下一段准备好（只影响什么时候开始缓冲，不影响播到哪一帧）。 */
const PRELOAD_IDLE_MS = 250

const at = ref(props.playhead)
const playing = ref(false)
/**
 * 两个视频槽位轮换：`front` 是正在播的那个，另一个（`1 - front`）预载下一段。
 * 元素**从不销毁**（销毁就得重新取文件），只换 `src`；同一个文件换段时连 src 都不换。
 * 刻意写成两个具名 ref 而不是数组：槽位永远只有两个，用下标索引换不来任何好处，
 * 只会让每一处都得先应付「可能是 undefined」。
 */
const el0 = ref<HTMLVideoElement | null>(null)
const el1 = ref<HTMLVideoElement | null>(null)
const front = ref(0)

/** 一个槽位当前装着哪个片段 / 哪个文件，以及 metadata 到了之后要停到哪一秒。 */
interface SlotState {
  /** 装的是哪个片段（换段判断认它）。 */
  clip: string
  /** 装的是哪个文件。src 相同就只 seek，不重新加载（切开的两段共用文件）。 */
  url: string
  /** metadata 还没到时先记下要 seek 到哪，`loadedmetadata` 里补。 */
  seek: number
}
const state0: SlotState = { clip: '', url: '', seek: 0 }
const state1: SlotState = { clip: '', url: '', seek: 0 }

function slotAt(i: number): HTMLVideoElement | null {
  return i === 0 ? el0.value : el1.value
}
function setSlot(i: number, el: HTMLVideoElement | null): void {
  if (i === 0) el0.value = el
  else el1.value = el
}
function stateAt(i: number): SlotState {
  return i === 0 ? state0 : state1
}

/** 音频元素按片段 id 存：`v-for` 里拿不到稳定的下标，删一段会串。 */
const audioEls = new Map<string, HTMLAudioElement>()
/** 播不了要说出来（大多是浏览器解不了这个编码，导出照旧能用 FFmpeg 走完）。 */
const mediaError = ref('')

let frame = 0
let wall = 0
let preloadTimer = 0

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

const activeIdx = computed(() =>
  program.value.findIndex(
    (c) => at.value >= c.start - 1e-6 && at.value < c.start + c.duration - 1e-6,
  ),
)
const active = computed<Clip | null>(() => program.value[activeIdx.value] ?? null)
/** 有画面可播（不是空白段、文件也在）——这一位决定视频元素显不显、要不要盖住提示文案。 */
const hasPicture = computed(() => Boolean(active.value && playable(active.value)))
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

function frontEl(): HTMLVideoElement | null {
  return slotAt(front.value)
}

function trySeek(el: HTMLMediaElement, time: number): void {
  try {
    el.currentTime = time
  } catch {
    /* 元数据还没到，loadedmetadata 之后会照 slotSeek 补上 */
  }
}

function applyMix(el: HTMLVideoElement, clip: Clip): void {
  el.muted = videoMuted.value || Boolean(clip.muted)
  el.volume = Math.min(1, Math.max(0, clip.volume))
}

function applyVideoMix(): void {
  const el = frontEl()
  const clip = active.value
  if (el && clip) applyMix(el, clip)
}

/**
 * 把一个槽位装上某个片段并停在指定时刻。**幂等**：
 * 文件没换就只调 `currentTime`，绝不重新 `load()`——切开的两段共用同一个文件，
 * 重新加载等于把已经缓冲好的东西全丢掉，那正是原来那段停顿的来源。
 */
function assign(i: number, clip: Clip | null, time: number): void {
  const el = slotAt(i)
  const slot = stateAt(i)
  if (!el) return
  if (!clip || !clip.asset_path || !playable(clip)) {
    if (slot.url) {
      el.pause()
      el.removeAttribute('src')
      el.load()
      slot.url = ''
    }
    slot.clip = ''
    return
  }
  const url = fileUrl(props.pid, clip.asset_path)
  slot.clip = clip.id
  slot.seek = time
  if (slot.url === url) {
    if (el.readyState >= 1 && Math.abs(el.currentTime - time) > 0.04) trySeek(el, time)
    return
  }
  slot.url = url
  el.src = url
}

/** 下一段：只看紧邻的那一个，空白段 / 缺文件的段没什么可预载。 */
function nextClip(): Clip | null {
  const idx = activeIdx.value
  if (idx < 0) return null
  const clip = program.value[idx + 1]
  return clip && playable(clip) ? clip : null
}

function assignBack(): void {
  const back = 1 - front.value
  const nxt = nextClip()
  assign(back, nxt, nxt ? nxt.in_point : 0)
  const el = slotAt(back)
  if (el && !el.paused) el.pause()
}

/**
 * 预载安排在什么时候：播着就立刻（接缝随时会到），暂停时等 250ms 静默——
 * 拖动播放头会连着换很多段，每一下都去取文件纯属浪费。
 */
function scheduleBack(): void {
  if (preloadTimer) {
    clearTimeout(preloadTimer)
    preloadTimer = 0
  }
  if (playing.value) {
    assignBack()
    return
  }
  preloadTimer = window.setTimeout(() => {
    preloadTimer = 0
    assignBack()
  }, PRELOAD_IDLE_MS)
}

/**
 * 让两个槽位对上「当前段 + 下一段」。换段、拖动、时间线刷新后都走这里，**幂等**。
 *
 * 第一件事是**先看要播的这一段是不是已经装在后台槽位上**（刚播完一段空白视频、或者往回
 * 拖了一段）：那就换一下 `front` 完事，别把已经缓冲好的东西丢掉重新加载。`advance` 走的
 * 是同一个道理，只是它在接缝上更急，不等这里。
 */
function reconcile(): void {
  const clip = active.value
  const back = 1 - front.value
  if (
    clip &&
    playable(clip) &&
    stateAt(back).clip === clip.id &&
    stateAt(front.value).clip !== clip.id
  ) {
    const old = frontEl()
    front.value = back
    if (old && !old.paused) old.pause()
  }
  assign(front.value, clip, clip && playable(clip) ? videoTime(clip) : 0)
  const el = frontEl()
  if (clip && el) {
    applyMix(el, clip)
    // 元素本来就装好了（换槽位 / 同一个文件），不会再来 `loadedmetadata`，这里得自己起播
    if (playing.value && stateAt(front.value).url && el.paused && el.readyState >= 2) {
      void el.play().catch(() => {})
    }
  }
  scheduleBack()
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

/** 拖动播放头之后让媒体追上来。换段时前台槽位换 src，真正的 seek 在 `onLoaded` 里。 */
function seekAll(): void {
  reconcile()
  syncAudio(true)
}

/**
 * 走到这一段的尾巴：换到下一段。
 *
 * **接缝就在这一个函数里**。后台槽位已经装好下一段并停在它的 in_point（第一帧解码好了，
 * 只是被前台盖着），所以这里只需要：换 `front`（换的是 z-index，一次合成的事）→ 把播放头
 * 挪到下一段的开头 → `play()`。旧的那个立刻暂停，随后被 `reconcile` 拿去装再下一段。
 * 后台没准备好（缺文件、浏览器解不了、刚拖过来还没缓冲）时退回老路：把播放头推过尾巴，
 * 让 `reconcile` 现装现播——**慢一点，但绝不卡死**。
 */
function advance(): void {
  const idx = activeIdx.value
  const cur = program.value[idx]
  if (!cur) return
  const nxt = program.value[idx + 1] ?? null
  const back = 1 - front.value
  const backEl = slotAt(back)
  const ready = Boolean(
    nxt && playable(nxt) && backEl && stateAt(back).clip === nxt.id && backEl.readyState >= 2,
  )
  const old = frontEl()
  if (ready && nxt && backEl) {
    front.value = back
    at.value = clamp(nxt.start)
    applyMix(backEl, nxt)
    if (Math.abs(backEl.currentTime - nxt.in_point) > 0.04) trySeek(backEl, nxt.in_point)
    if (playing.value) void backEl.play().catch(() => {})
    if (old && !old.paused) old.pause()
    wall = 0
    syncAudio(true)
    scheduleBack()
    return
  }
  if (old && !old.paused) old.pause()
  at.value = clamp(cur.start + cur.duration + 0.005)
  wall = 0
  if (at.value >= total.value - 1e-3) {
    at.value = total.value
    pause()
  }
}

function tick(now: number): void {
  frame = requestAnimationFrame(tick)
  const clip = active.value
  const el = frontEl()
  const dt = wall ? Math.max(0, (now - wall) / 1000) : 0
  wall = now
  let next: number
  if (clip && el && stateAt(front.value).clip === clip.id && el.readyState >= 2 && !el.seeking) {
    // 时钟跟着画面走：这样音频对齐的是「真的播到哪了」，不是墙上时间
    const local = Math.max(0, el.currentTime - clip.in_point)
    // 裁过的片段不会触发 `ended`（文件还没播完），接缝只能在这里认出来
    if (local >= clip.duration - 1e-3) {
      advance()
      return
    }
    next = clip.start + local
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
function onTimeUpdate(i: number): void {
  if (!playing.value || !document.hidden || i !== front.value) return
  const clip = active.value
  const el = frontEl()
  if (!clip || !el) return
  const local = Math.max(0, el.currentTime - clip.in_point)
  if (local >= clip.duration - 1e-3) {
    advance()
    return
  }
  const next = clip.start + local
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
  reconcile()
  const el = frontEl()
  if (el && stateAt(front.value).url) {
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
  for (const el of [el0.value, el1.value]) el?.pause()
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

/**
 * 某个槽位的 metadata 到了：按 `slotSeek` 停到该停的那一帧。
 * **后台槽位也要 seek**——那一下 seek 就是「把第一帧解码好」，接缝的顺滑全靠它，
 * 但绝不 `play()`（后台在播就等于偷跑）。
 */
function onLoaded(i: number): void {
  const el = slotAt(i)
  if (!el) return
  trySeek(el, stateAt(i).seek)
  if (i !== front.value) {
    if (!el.paused) el.pause()
    return
  }
  applyVideoMix()
  if (playing.value) void el.play().catch(() => {})
}

/** 前台这一段播到文件末尾了（没裁过的段走这条；裁过的由 `tick` 提前认出来）。 */
function onEnded(i: number): void {
  if (i !== front.value) return
  advance()
}

function onVideoError(i: number): void {
  // 后台槽位加载失败不打断当前播放：`advance` 会发现它没准备好，自动退回慢路径
  if (i !== front.value) return
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
// 换段 / 时间线刷新 / 轨道静音变了：重新对齐两个槽位（幂等，src 没变就不重新加载）
watch([active, videoMuted], () => reconcile())
// 片段被删光 / 换了工程：正在播的东西已经不存在了，别让时钟继续跑
watch(total, (value) => {
  if (value <= 0) pause()
  else if (at.value > value) {
    at.value = value
    seekAll()
  }
})

onMounted(reconcile)
onBeforeUnmount(() => {
  pause()
  if (preloadTimer) {
    clearTimeout(preloadTimer)
    preloadTimer = 0
  }
})

function stamp(n: number): string {
  const s = Math.max(0, n)
  const m = Math.floor(s / 60)
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}.${Math.floor((s % 1) * 10)}`
}
</script>

<template>
  <div class="border-line-1 bg-base-1 flex min-h-0 flex-col border">
    <div class="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-black">
      <!--
        两个 video 轮换：前台那个在播（`z-10`），后台那个已经装好下一段并停在它的第一帧。
        两个都铺满 + `bg-black`，所以前台把后台整个盖住（`object-contain` 的黑边也盖得住）；
        换段只是换一下这个 z-index，元素本身从不销毁 —— 销毁就得重新取文件、重新等 metadata。
      -->
      <video
        v-for="i in [0, 1]"
        :key="i"
        :ref="(el) => setSlot(i, el as HTMLVideoElement | null)"
        class="absolute inset-0 h-full w-full bg-black object-contain"
        :class="i === front ? 'z-10' : 'z-0'"
        preload="auto"
        playsinline
        @loadedmetadata="onLoaded(i)"
        @timeupdate="onTimeUpdate(i)"
        @ended="onEnded(i)"
        @error="onVideoError(i)"
      />
      <!-- 没画面可播时把两个 video 盖掉（后台正在预载，不能让它露出来） -->
      <div
        v-if="!hasPicture"
        class="absolute inset-0 z-20 flex items-center justify-center bg-black"
      >
        <p v-if="program.length === 0" class="text-fg-4 px-3 text-center text-2xs">
          视频轨上还没有片段。先「自动装配」把镜头铺上来，这里就能看整体效果了。
        </p>
        <p v-else-if="activeIsBlank" class="text-fg-3 px-3 text-center text-2xs">
          空白视频段 · {{ active?.duration.toFixed(2) }} 秒
        </p>
        <p v-else-if="active" class="text-st-review px-3 text-center text-2xs">
          这一段登记的文件已经不在磁盘上，预览跳过它。
        </p>
        <p v-else class="text-fg-4 px-3 text-center text-2xs">当前播放头不在视频片段内。</p>
      </div>
      <span
        v-if="active"
        class="text-fg-2 absolute top-1 left-1 z-30 max-w-[70%] truncate bg-black/60 px-1 text-2xs"
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
        逐段实时预览：下一段会提前预载，换段基本无缝；转场 / 特效在这里不渲染——最终效果看导出。
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
