<script setup lang="ts">
/**
 * 时间线（Step 8 的前端）。
 *
 * 这一页是**不依赖 AI 的那一半**：ComfyUI 全程离线、LLM 没配置，装配 → 剪辑 → 导出
 * 照样能走完（硬约束 2）。所以工具栏里没有任何一个按钮会去碰生成。
 *
 * 四个刻意的设计：
 *   1. **编辑一律提交给后端**。移动、裁切、切分、删除都回一条完整时间线，前端整体覆盖，
 *      不在本地算 ripple 之后的位置——两套算法必然对不上。
 *   2. **撤销按钮只信后端的 can_undo / can_redo**。撤销栈在进程里，重启应用就空了；
 *      前端自己记一份会在重启后骗人。
 *   3. **导出先看命令**。「预检」把将要执行的 FFmpeg 参数原样摆出来，确认后才真的起进程。
 *      看不见命令的导出出问题时无从下手。
 *   4. **缺文件的片段自己举手**。`missing_file` 的片段画成红框并在工具栏计数——
 *      导出会在半路失败，不如提前说。
 *   5. **播放头是唯一的时间真相**。标尺上那根竖条、预览器播到哪、「在播放头处切分」用的
 *      都是同一个 `playhead`（`v-model:playhead` 双向绑定）：拖标尺画面跟着走，播着的
 *      时候竖条也跟着走。两边各记一个「当前时间」的话，看到的和切出来的会分叉。
 *   6. **声音有两条路**：视频片段自带的音轨（可静音 / 调音量），与音频轨上的独立片段
 *      （从画面「拆出声音」或导入配乐）。音频轨之间可以随意重叠——叠加是它存在的意义，
 *      不是错误；后端导出时用 `amix` 把它们混在一起。
 *
 * 拖拽只做三件事：拖块身 = 移动、拖左右边缘 = 裁切、拖标尺 = 移播放头。**提交仍然全在
 * 后端**：拖动期间只画一个位移预览，松手才发请求，返回的整条时间线整体覆盖。波形没有。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  AudioLines,
  Clapperboard,
  Download,
  FileSearch,
  Lock,
  LockOpen,
  Music,
  Plus,
  Redo2,
  RefreshCw,
  Scissors,
  Trash2,
  Undo2,
  Volume2,
  VolumeX,
  Wand2,
} from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import PreviewPlayer from './PreviewPlayer.vue'
import {
  TRACK_KIND_LABEL,
  TRANSITION_KINDS,
  TRANSITION_LABEL,
  type Clip,
  type Track,
} from '@/shared/api/timeline'
import { generationApi, type GenerationVersion } from '@/shared/api/generation'
import { assetsApi } from '@/shared/api/assets'
import { ApiError, confirmFlagOf } from '@/shared/api/client'
import { useTimelineStore } from '@/stores/timeline'

const route = useRoute()
const router = useRouter()
const tl = useTimelineStore()

const pid = computed(() => String(route.params.pid ?? ''))

/** 轨道区的比例尺：每秒多少像素。 */
const zoom = ref(24)
const selectedId = ref('')
const exportPath = ref('')
/** 选中片段所属镜头的版本列表——「换成另一个版本」的下拉数据。 */
const versions = ref<GenerationVersion[]>([])
/** 播放头（秒）。预览器与标尺上那根竖条共用它，见文件开头第 5 条。 */
const playhead = ref(0)
/** 导入配乐用的隐藏 input。 */
const audioInput = ref<HTMLInputElement | null>(null)
const importing = ref(false)
/** 上一次操作的说明（拆出声音 / 导入音频落到哪条轨上）——落地位置必须说出来。 */
const note = ref('')
/** 等确认的删轨：后端回 CONFLICT + `confirm: "force"` 时记下来，确认后带 force 重放。 */
const forceTrackId = ref('')

const total = computed(() => tl.timeline?.duration_total ?? 0)
/** 轨道区的宽度：末尾多留 40px，方便把片段拖到最后一段之后。 */
const laneWidth = computed(() => Math.max(320, total.value * zoom.value + 40))
/** 标尺刻度间隔：缩得很小时每秒一根线会糊成一片。 */
const tickStep = computed(() =>
  zoom.value >= 40 ? 1 : zoom.value >= 18 ? 2 : zoom.value >= 10 ? 5 : 10,
)
const ticks = computed(() => {
  const out: number[] = []
  for (let s = 0; s <= Math.ceil(laneWidth.value / zoom.value); s += tickStep.value) out.push(s)
  return out
})

function clampTime(value: number): number {
  return Math.max(0, Math.min(total.value, Math.round(value * 1000) / 1000))
}

const selected = computed<Clip | null>(
  () => tl.clips.find((c) => c.id === selectedId.value) ?? null,
)
/** 挂在选中片段上的转场（进或出）。 */
const clipTransitions = computed(() =>
  tl.transitions.filter(
    (t) => t.from_clip_id === selectedId.value || t.to_clip_id === selectedId.value,
  ),
)

/** 同一条轨道上排在选中片段后面的那一个——转场只能加在相邻两段之间。 */
const nextClip = computed<Clip | null>(() => {
  const cur = selected.value
  if (!cur) return null
  const lane = tl.timeline?.tracks.find((t) => t.id === cur.track_id)
  if (!lane) return null
  const at = lane.clips.findIndex((c) => c.id === cur.id)
  return lane.clips[at + 1] ?? null
})

const transitionKind = ref<string>('dissolve')
const transitionDuration = ref(0.5)

function fmt(n: number): string {
  return `${Math.round(n * 100) / 100}s`
}

/** 秒 → mm:ss，时间线总长用它，比一串小数好读。 */
function clock(n: number): string {
  const s = Math.max(0, Math.round(n))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

async function reload(): Promise<void> {
  if (!pid.value) return
  await tl.load(pid.value).catch(() => {})
}

onMounted(reload)
watch(pid, reload)

/** 选中的片段被删掉后不要留一块空的检查器。 */
watch(
  () => tl.clips,
  (list) => {
    if (selectedId.value && !list.some((c) => c.id === selectedId.value)) selectedId.value = ''
  },
)

/** 换选片段时拉它所属镜头的版本；手工片段（没有 shot_id）没有可换的版本。 */
watch(selected, async (clip) => {
  if (!clip?.shot_id) {
    versions.value = []
    return
  }
  versions.value = await generationApi.versions(pid.value, clip.shot_id).catch(() => [])
})

async function assemble(replace: boolean): Promise<void> {
  await tl.assemble(pid.value, replace).catch(() => {})
}

async function saveStart(value: string): Promise<void> {
  const clip = selected.value
  const start = Number(value)
  if (!clip || !Number.isFinite(start) || start < 0) return
  await tl.move(pid.value, clip.id, start).catch(() => {})
}

async function saveTrim(key: 'in_point' | 'out_point', value: string): Promise<void> {
  const clip = selected.value
  if (!clip) return
  const num = value.trim() === '' ? null : Number(value)
  if (num !== null && !Number.isFinite(num)) return
  await tl.trim(pid.value, clip.id, { [key]: num, ripple: rippleTrim.value }).catch(() => {})
}

const rippleTrim = ref(true)
const splitAt = ref('')

async function doSplit(): Promise<void> {
  const clip = selected.value
  const at = Number(splitAt.value)
  if (!clip || !Number.isFinite(at)) return
  await tl.split(pid.value, clip.id, at).catch(() => {})
  splitAt.value = ''
}

// --- 播放头：拖标尺移动竖条，竖条底下那一段就是「当前片段」 ---

/** 选中片段所在的轨道（拖拽要看它锁没锁，拆声音要看它是不是视频轨）。 */
const selectedTrack = computed<Track | null>(
  () => tl.timeline?.tracks.find((t) => t.id === selected.value?.track_id) ?? null,
)

let scrubbing = false

function clipAt(track: Track | null, at: number): Clip | null {
  return track?.clips.find((c) => at >= c.start && at < c.start + c.duration) ?? null
}

/** 竖条底下那一段。先看已选中的那条轨（在音频轨上干活时不该被抢回视频轨），再看视频轨。 */
function underPlayhead(): Clip | null {
  return clipAt(selectedTrack.value, playhead.value) ?? clipAt(tl.videoTrack, playhead.value)
}

/**
 * 拖到哪儿就选中哪一段。**空档不清空选中**——拖过一段空白把右边的检查器抹掉纯属干扰，
 * 用户要取消选择有别的办法（点空轨道）。
 */
function selectUnderPlayhead(): void {
  const hit = underPlayhead()
  if (hit) selectedId.value = hit.id
}

/**
 * 抓住这个指针：拖出元素之外仍然能收到 move / up。抓不到**不算失败**——
 * 那样只是拖出去会丢事件，让整条拖拽根本开不了才是更糟的结果。
 */
function capture(event: PointerEvent): void {
  try {
    ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  } catch {
    /* 指针已经不在了：照常按坐标算 */
  }
}

function release(event: PointerEvent): void {
  const el = event.currentTarget as HTMLElement
  if (el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId)
}

function seekTo(event: PointerEvent): void {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  playhead.value = clampTime((event.clientX - rect.left) / zoom.value)
  selectUnderPlayhead()
}

function onRulerDown(event: PointerEvent): void {
  scrubbing = true
  capture(event)
  seekTo(event)
}

function onRulerMove(event: PointerEvent): void {
  if (scrubbing) seekTo(event)
}

function onRulerUp(event: PointerEvent): void {
  scrubbing = false
  release(event)
}

/** 在竖条处切一刀。播放头本来就是「我要在这里下刀」这件事，不用再填一个秒数。 */
async function splitAtPlayhead(): Promise<void> {
  const clip = underPlayhead()
  if (!clip) return
  selectedId.value = clip.id
  await tl.split(pid.value, clip.id, playhead.value).catch(() => {})
}

// --- 拖拽：块身 = 移动，左右边缘 = 裁切。松手才提交 ---

type DragMode = 'move' | 'left' | 'right'

/** 拖动期间只记「按下的位置」与「现在偏了多少像素」，真值等后端回来。 */
interface Drag {
  id: string
  mode: DragMode
  originX: number
  dx: number
}

const drag = ref<Drag | null>(null)
/** 小于这个像素当一次点击而不是拖动：手抖不该改剪辑。 */
const DRAG_SLOP = 3
/** 裁到比这更短就没有意义了（后端也会拒绝入点越过出点）。 */
const MIN_LEN = 0.05

function beginDrag(event: PointerEvent, clip: Clip, mode: DragMode, track: Track): void {
  selectedId.value = clip.id
  playhead.value = clampTime(clip.start)
  if (track.locked) return // 锁了的轨道只能看
  capture(event)
  drag.value = { id: clip.id, mode, originX: event.clientX, dx: 0 }
}

function onDragMove(event: PointerEvent): void {
  if (drag.value) drag.value.dx = event.clientX - drag.value.originX
}

/** 拖动中的位移预览：只是画一下，位置的真相仍然是后端返回的那一条时间线。 */
function clipStyle(clip: Clip): Record<string, string> {
  const d = drag.value?.id === clip.id ? drag.value : null
  const shift = d ? d.dx : 0
  const left = clip.start * zoom.value + (d && d.mode !== 'right' ? shift : 0)
  const grow = d ? (d.mode === 'right' ? shift : d.mode === 'left' ? -shift : 0) : 0
  return {
    left: `${Math.max(0, left)}px`,
    width: `${Math.max(8, clip.duration * zoom.value + grow)}px`,
  }
}

async function endDrag(event: PointerEvent): Promise<void> {
  const d = drag.value
  drag.value = null
  release(event)
  const clip = d ? tl.clips.find((c) => c.id === d.id) : null
  if (!d || !clip || Math.abs(d.dx) < DRAG_SLOP) return
  const delta = d.dx / zoom.value
  const outPoint = clip.out_point ?? clip.in_point + clip.duration
  if (d.mode === 'move') {
    await tl.move(pid.value, clip.id, Math.max(0, clip.start + delta)).catch(() => {})
    return
  }
  if (d.mode === 'left') {
    // 左边缘要同时改「从素材哪里开始」与「在时间线哪里开始」：一次请求、一格撤销。
    // ripple 关掉——用户刚刚亲手把这一边拖到了这个位置，紧接着又被贴走就是在跟人抢方向盘。
    const inPoint = Math.min(Math.max(0, clip.in_point + delta), outPoint - MIN_LEN)
    await tl
      .trim(pid.value, clip.id, {
        in_point: inPoint,
        start: Math.max(0, clip.start + (inPoint - clip.in_point)),
        ripple: false,
      })
      .catch(() => {})
    return
  }
  await tl
    .trim(pid.value, clip.id, {
      out_point: Math.max(clip.in_point + MIN_LEN, outPoint + delta),
      ripple: rippleTrim.value,
    })
    .catch(() => {})
}

async function doRemove(clipId: string, ripple: boolean): Promise<void> {
  await tl.remove(pid.value, clipId, ripple).catch(() => {})
}

// --- 声音：视频片段自带的那一路，与音频轨上的独立片段 ---

/** 导入配乐这一步不走 store（它是资产上传），错误得自己有地方显示。 */
const importError = ref<ApiError | null>(null)

async function saveMix(body: { muted?: boolean; volume?: number }): Promise<void> {
  const clip = selected.value
  if (!clip) return
  await tl.setMix(pid.value, clip.id, body).catch(() => {})
}

/** 拆出声音。新开了轨 / 复用了文件都要说出来——不然用户不知道那段声音去了哪里。 */
async function detachAudio(): Promise<void> {
  const clip = selected.value
  if (!clip) return
  note.value = ''
  const out = await tl.detachAudio(pid.value, clip.id).catch(() => null)
  if (!out) return
  selectedId.value = out.audio_clip_id
  note.value =
    `声音已拆到「${out.track_name}」` +
    (out.created_track ? '（这个时间段原有的音频轨都占着，为它新开了一条）' : '') +
    (out.reused_file ? '，复用了上次拆好的音频文件' : '') +
    '。源片段已静音，否则同一段声音会被听见两遍。'
}

async function addAudioTrack(): Promise<void> {
  const track = await tl.addTrack(pid.value, 'audio').catch(() => null)
  if (track) note.value = `已加一条音频轨「${track.name}」。音频轨之间可以重叠，导出时混在一起。`
}

/** 配乐放哪条轨：优先选中片段所在的那条，其次第一条音频轨，都没有就新开一条。 */
async function pickAudioTrack(): Promise<Track | null> {
  if (selectedTrack.value?.kind === 'audio') return selectedTrack.value
  return tl.audioTracks[0] ?? (await tl.addTrack(pid.value, 'audio').catch(() => null))
}

/** 导入一段音频放到音频轨上（配乐 / 配音）。落在播放头处——竖条在哪就放在哪。 */
async function onPickAudio(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  importing.value = true
  importError.value = null
  note.value = ''
  try {
    const track = await pickAudioTrack()
    if (!track) return
    // 用户导入的配乐是真资产（kind=audio，落 assets/），不是拆出来的临时文件
    const asset = await assetsApi.upload(pid.value, file, 'audio')
    const clipId = await tl
      .addClip(pid.value, track.id, { asset_id: asset.id, start: playhead.value })
      .catch(() => '')
    if (!clipId) return
    selectedId.value = clipId
    note.value = `「${file.name}」已放到「${track.name}」的 ${fmt(playhead.value)} 处。`
  } catch (err) {
    importError.value = err instanceof ApiError ? err : null
  } finally {
    importing.value = false
  }
}

// --- 轨道 ---

async function toggleTrack(track: Track, key: 'muted' | 'locked'): Promise<void> {
  await tl.patchTrack(pid.value, track.id, { [key]: !track[key] }).catch(() => {})
}

/**
 * 删轨道。上面还有片段时后端**一条都不删**，回 CONFLICT + `confirm: "force"`；
 * 确认按钮从 ErrorPanel 的 actions 插槽塞进去，点了才带 force 重放同一个请求。
 */
async function removeTrack(trackId: string, force = false): Promise<void> {
  forceTrackId.value = ''
  try {
    await tl.removeTrack(pid.value, trackId, force)
  } catch (err) {
    if (confirmFlagOf(err) === 'force') forceTrackId.value = trackId
  }
}

/** 最后一条视频轨删不掉（后端会拒），所以按钮直接不给。 */
function canRemoveTrack(track: Track): boolean {
  if (track.kind !== 'video') return true
  return (tl.timeline?.tracks.filter((t) => t.kind === 'video').length ?? 0) > 1
}

async function doReplaceVersion(versionId: string): Promise<void> {
  const clip = selected.value
  if (!clip || !versionId) return
  await tl.replaceVersion(pid.value, clip.id, versionId).catch(() => {})
}

async function addTransition(): Promise<void> {
  const from = selected.value
  const to = nextClip.value
  if (!from || !to) return
  await tl
    .addTransition(pid.value, {
      from_clip_id: from.id,
      to_clip_id: to.id,
      kind: transitionKind.value,
      duration: transitionDuration.value,
    })
    .catch(() => {})
}

/** 导出：预检与执行分成两个动作，不合并。 */
const exportedNote = ref('')

async function runExport(): Promise<void> {
  exportedNote.value = ''
  const rec = await tl.runExport(pid.value, exportPath.value.trim() || null)
  if (rec) {
    exportedNote.value =
      rec.status === 'done'
        ? `导出完成：${rec.path}`
        : `导出结束但状态是 ${rec.status}，下面的历史里有详情`
  }
}

function goShot(shotId: string | null): void {
  if (!shotId) return
  void router.push({ name: 'shot', params: { pid: pid.value, sid: shotId } })
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />
    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1.5 border-b px-2">
      <AppButton
        size="sm"
        variant="primary"
        :disabled="tl.busy"
        title="按 Scene / Shot 顺序把每个镜头的当前版本铺到视频轨上；没有当前版本的镜头会被跳过并写明理由"
        @click="assemble(true)"
      >
        <Wand2 :size="10" />自动装配
      </AppButton>
      <AppButton
        size="sm"
        :disabled="tl.busy"
        title="不清空现有轨道，把还没铺的镜头追加到末尾"
        @click="assemble(false)"
      >
        追加装配
      </AppButton>
      <AppButton
        size="sm"
        variant="ghost"
        :disabled="tl.busy || !tl.canUndo"
        title="撤销上一步编辑。撤销栈在进程里，重启应用后会清空"
        @click="tl.undo(pid).catch(() => {})"
      >
        <Undo2 :size="10" />撤销
      </AppButton>
      <AppButton
        size="sm"
        variant="ghost"
        :disabled="tl.busy || !tl.canRedo"
        title="重做"
        @click="tl.redo(pid).catch(() => {})"
      >
        <Redo2 :size="10" />重做
      </AppButton>
      <span class="bg-line-1 h-3.5 w-px shrink-0" />
      <AppButton
        size="sm"
        :disabled="tl.busy"
        title="加一条空音频轨。音频轨之间可以重叠，导出时用 amix 混在一起"
        @click="addAudioTrack()"
      >
        <Plus :size="10" />音频轨
      </AppButton>
      <AppButton
        size="sm"
        :disabled="tl.busy || importing"
        title="导入一段配乐 / 配音，放到音频轨上播放头所在的位置"
        @click="audioInput?.click()"
      >
        <Music :size="10" />{{ importing ? '导入中…' : '导入音频' }}
      </AppButton>
      <input
        ref="audioInput"
        type="file"
        accept="audio/*"
        class="hidden"
        @change="onPickAudio($event)"
      />
      <AppButton
        size="sm"
        variant="ghost"
        :disabled="tl.busy || !underPlayhead()"
        title="在竖条所在的位置把那一段切成两段"
        @click="splitAtPlayhead()"
      >
        <Scissors :size="10" />在播放头切分
      </AppButton>
      <span class="text-fg-3 tnum text-2xs">
        {{ tl.clips.length }} 片段 · 总长 {{ clock(tl.timeline?.duration_total ?? 0) }} ·
        {{ tl.timeline?.width }}×{{ tl.timeline?.height }} @{{ tl.timeline?.fps }}fps
        <span v-if="tl.missing.length" class="text-st-review">
          · {{ tl.missing.length }} 个片段文件已丢失
        </span>
      </span>
      <label class="text-fg-4 ml-auto flex items-center gap-1 text-2xs">
        缩放
        <input
          v-model.number="zoom"
          type="range"
          min="6"
          max="120"
          step="2"
          class="accent-accent"
        />
      </label>
      <AppButton size="sm" variant="ghost" :disabled="tl.busy" @click="reload()">
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>

    <ErrorPanel
      v-if="tl.lastError"
      class="mx-2 mt-2"
      :error="tl.lastError"
      @dismiss="tl.clearError()"
    >
      <template #actions>
        <!-- 「轨道上还有片段」不是失败而是一次确认：确认后带 force 重放同一个请求 -->
        <AppButton
          v-if="forceTrackId"
          size="sm"
          variant="danger"
          :disabled="tl.busy"
          @click="removeTrack(forceTrackId, true)"
        >
          <Trash2 :size="10" />连片段一起删
        </AppButton>
      </template>
    </ErrorPanel>
    <ErrorPanel
      v-if="importError"
      class="mx-2 mt-2"
      :error="importError"
      @dismiss="importError = null"
    />

    <!-- 拆声音 / 导入音频落在了哪里：位置必须说出来，不能让人自己去找 -->
    <p v-if="note" class="text-fg-2 border-line-1 bg-base-2 mx-2 mt-2 border px-2 py-1 text-2xs">
      {{ note }}
      <button class="text-fg-4 hover:text-fg-1 ml-1" @click="note = ''">关闭</button>
    </p>

    <!-- 装配结论：铺了几个 + 跳过的逐条理由。跳过的那几个才是要处理的事 -->
    <div
      v-if="tl.placedCount !== null"
      class="border-line-1 bg-base-2 mx-2 mt-2 flex items-start gap-2 border p-1.5"
    >
      <div class="min-w-0 flex-1">
        <p class="text-fg-2 text-2xs">
          装配完成：铺上 {{ tl.placedCount }} 个片段<template v-if="tl.skipped.length"
            >，跳过 {{ tl.skipped.length }} 个镜头</template
          >
        </p>
        <ul v-if="tl.skipped.length" class="mt-1 space-y-0.5">
          <li v-for="s in tl.skipped" :key="s.shot_id" class="text-2xs">
            <button class="text-st-review hover:underline" @click="goShot(s.shot_id)">
              跳过 {{ s.index_no }}
            </button>
            <span class="text-fg-4"> — {{ s.reason }}</span>
          </li>
        </ul>
      </div>
      <button class="text-fg-4 hover:text-fg-1 text-2xs" @click="tl.clearAssembleNote()">
        关闭
      </button>
    </div>

    <div class="flex min-h-0 flex-1 gap-2 p-2">
      <div class="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
        <!-- 预览器：轨道区正上方，逐段实时播放整体成片（不预渲染，见组件开头） -->
        <PreviewPlayer
          v-model:playhead="playhead"
          :pid="pid"
          :timeline="tl.timeline"
          class="h-64 shrink-0"
        />

        <!-- 轨道区：按秒数比例画块 + 一根贯穿所有轨道的播放头竖条 -->
        <AppPanel title="轨道区" class="min-h-0 flex-1">
          <template #actions>
            <span class="text-fg-4 text-2xs">
              拖标尺移播放头 · 拖块身移动 · 拖左右边缘裁切 · 松手才提交，位置由后端重排
            </span>
          </template>
          <EmptyState
            v-if="tl.clips.length === 0"
            title="轨道上还没有片段"
            body="点「自动装配」把每个镜头的当前版本按 Scene / Shot 顺序铺上来。没有当前版本的镜头会被跳过并写明理由——这一步不需要 ComfyUI。"
          />
          <div v-else class="overflow-x-auto p-2">
            <div class="relative" :style="{ width: `${laneWidth}px` }">
              <!-- 标尺：拖它移播放头。竖条底下那一段会被选中 -->
              <div
                class="border-line-1 bg-base-2 relative h-4 cursor-ew-resize border select-none"
                title="拖动这里移动播放头；预览器与这根竖条是同一个时间"
                @pointerdown="onRulerDown($event)"
                @pointermove="onRulerMove($event)"
                @pointerup="onRulerUp($event)"
                @pointercancel="onRulerUp($event)"
              >
                <span
                  v-for="t in ticks"
                  :key="t"
                  class="absolute top-0 bottom-0"
                  :style="{ left: `${t * zoom}px` }"
                >
                  <span class="bg-line-1 absolute top-0 bottom-0 left-0 w-px" />
                  <span class="text-fg-4 tnum absolute top-0 left-0.5 text-2xs whitespace-nowrap">
                    {{ clock(t) }}
                  </span>
                </span>
              </div>
              <section v-for="track in tl.timeline?.tracks ?? []" :key="track.id" class="mt-2">
                <header class="flex items-center gap-1.5 pb-1">
                  <span class="text-fg-3 text-2xs">
                    {{ TRACK_KIND_LABEL[track.kind] ?? track.kind }} · {{ track.name }}
                  </span>
                  <AppBadge>{{ track.clips.length }} 段</AppBadge>
                  <button
                    class="text-fg-4 hover:text-fg-1"
                    :title="
                      track.muted ? '这条轨道是静音的（不进成片）：点一下取消' : '静音整条轨道'
                    "
                    @click="toggleTrack(track, 'muted')"
                  >
                    <VolumeX v-if="track.muted" :size="10" class="text-st-review" />
                    <Volume2 v-else :size="10" />
                  </button>
                  <button
                    class="text-fg-4 hover:text-fg-1"
                    :title="track.locked ? '已锁：拖不动上面的片段，点一下解锁' : '锁住这条轨道'"
                    @click="toggleTrack(track, 'locked')"
                  >
                    <Lock v-if="track.locked" :size="10" class="text-st-review" />
                    <LockOpen v-else :size="10" />
                  </button>
                  <button
                    v-if="canRemoveTrack(track)"
                    class="text-fg-4 hover:text-st-failed"
                    title="删掉这条轨道。上面还有片段时会先问一句"
                    @click="removeTrack(track.id)"
                  >
                    <Trash2 :size="10" />
                  </button>
                  <span v-if="track.kind === 'audio'" class="text-fg-4 text-2xs">
                    音频轨可与别的音频轨重叠——叠加就是这么来的
                  </span>
                </header>
                <div
                  class="border-line-1 bg-base-2 relative h-14 border"
                  :style="{ width: `${laneWidth}px` }"
                >
                  <p
                    v-if="track.clips.length === 0"
                    class="text-fg-4 absolute top-1 left-1.5 text-2xs"
                  >
                    这条轨道是空的。<template v-if="track.kind === 'audio'"
                      >在视频片段上点「拆出声音」，或用工具栏的「导入音频」。</template
                    >
                  </p>
                  <button
                    v-for="clip in track.clips"
                    :key="clip.id"
                    class="absolute top-0.5 bottom-0.5 overflow-hidden border text-left"
                    :class="[
                      clip.id === selectedId ? 'bg-accent-dim/40' : 'bg-base-1 hover:bg-base-3',
                      track.locked ? 'cursor-not-allowed' : 'cursor-grab',
                      clip.missing_file
                        ? 'border-st-failed/60'
                        : clip.id === selectedId
                          ? 'border-accent/60'
                          : 'border-line-1',
                    ]"
                    :style="clipStyle(clip)"
                    :title="`${clip.label ?? clip.id} · 起点 ${fmt(clip.start)} · 时长 ${fmt(clip.duration)}${clip.muted ? ' · 已静音' : ''}${clip.missing_file ? ' · 文件已丢失' : ''}${track.locked ? ' · 轨道已锁，拖不动' : ' · 拖块身移动，拖左右边缘裁切'}`"
                    @click="selectedId = clip.id"
                    @pointerdown="beginDrag($event, clip, 'move', track)"
                    @pointermove="onDragMove($event)"
                    @pointerup="endDrag($event)"
                    @pointercancel="endDrag($event)"
                  >
                    <!-- 裁切把手。左边缘同时改入点与起点（一次请求、一格撤销） -->
                    <span
                      v-if="!track.locked"
                      class="hover:bg-accent/70 absolute top-0 bottom-0 left-0 z-10 w-1.5 cursor-ew-resize bg-white/10"
                      title="拖它改入点（在时间线上的起点跟着走）"
                      @pointerdown.stop="beginDrag($event, clip, 'left', track)"
                      @pointermove.stop="onDragMove($event)"
                      @pointerup.stop="endDrag($event)"
                      @pointercancel.stop="endDrag($event)"
                    />
                    <span
                      v-if="!track.locked"
                      class="hover:bg-accent/70 absolute top-0 right-0 bottom-0 z-10 w-1.5 cursor-ew-resize bg-white/10"
                      title="拖它改出点"
                      @pointerdown.stop="beginDrag($event, clip, 'right', track)"
                      @pointermove.stop="onDragMove($event)"
                      @pointerup.stop="endDrag($event)"
                      @pointercancel.stop="endDrag($event)"
                    />
                    <span class="block px-2 text-2xs">
                      <span class="text-fg-1 block truncate">
                        {{ clip.shot_index_no ?? '—' }}. {{ clip.label ?? '未命名片段' }}
                      </span>
                      <span class="text-fg-4 tnum block truncate">
                        {{ fmt(clip.duration) }}
                        <template v-if="clip.version_no">· v{{ clip.version_no }}</template>
                        <template v-if="clip.volume !== 1">· ×{{ clip.volume }}</template>
                      </span>
                      <span v-if="clip.missing_file" class="text-st-review block truncate">
                        文件丢失
                      </span>
                      <span v-else-if="clip.muted" class="text-fg-4 block truncate">
                        <VolumeX :size="9" class="inline" />
                        {{ clip.detached_audio_clip_id ? '声音已拆出' : '已静音' }}
                      </span>
                      <span v-else-if="clip.source_missing" class="text-st-review block truncate">
                        来源片段已不在
                      </span>
                    </span>
                  </button>
                </div>
              </section>
              <!--
                播放头：一根贯穿所有轨道的竖条。`pointer-events-none` 是必须的——
                它盖在片段上面，能接事件的话就永远点不到它底下那一段。
              -->
              <div
                class="bg-accent pointer-events-none absolute top-0 bottom-0 z-20 w-px"
                :style="{ left: `${playhead * zoom}px` }"
              >
                <span class="bg-accent absolute top-0 -left-[3px] h-1.5 w-[7px]" />
              </div>
            </div>
            <p v-if="tl.missing.length" class="text-st-review mt-2 text-2xs">
              有 {{ tl.missing.length }} 个片段的文件已经不在磁盘上：导出会在半路失败。
              先把它们换成别的版本或删掉。
            </p>
          </div>
        </AppPanel>

        <!-- 导出：先预检命令，再执行 -->
        <AppPanel title="导出" class="h-56 shrink-0">
          <template #actions>
            <input
              v-model="exportPath"
              placeholder="留空则写进工程 generations/exports/"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-5 w-60 border px-1.5 text-2xs outline-none"
            />
            <AppButton
              size="sm"
              :disabled="tl.busy || tl.clips.length === 0"
              title="只算命令不起进程：把将要执行的 FFmpeg 参数原样摆出来"
              @click="tl.loadPlan(pid).catch(() => {})"
            >
              <FileSearch :size="10" />预检命令
            </AppButton>
            <AppButton
              size="sm"
              variant="primary"
              :disabled="tl.busy || tl.clips.length === 0"
              title="真的起 FFmpeg 进程，用原始素材而不是代理"
              @click="runExport()"
            >
              <Download :size="10" />导出成片
            </AppButton>
          </template>
          <div class="space-y-2 p-2">
            <p v-if="exportedNote" class="text-fg-2 text-2xs">{{ exportedNote }}</p>
            <div v-if="tl.plan">
              <p class="text-fg-3 text-2xs">
                将写入 <span class="text-fg-1">{{ tl.plan.path }}</span> ·
                {{ tl.plan.clips }} 个画面片段 · {{ tl.plan.audio_clips }} 段独立音频参与混音
              </p>
              <!--
                预检的警告不是「可能有问题」而是「已经确定会被丢掉 / 说不准」：静音的轨、
                比画面长的声音、会被 concat 合掉的空档。它必须显示在命令旁边——
                导出完了才发现少了一条音轨，就得从头再来一遍。
              -->
              <ul v-if="tl.plan.warnings.length" class="mt-1 space-y-px">
                <li v-for="w in tl.plan.warnings" :key="w" class="text-st-review text-2xs">
                  · {{ w }}
                </li>
              </ul>
              <pre
                class="text-fg-3 bg-base-2 border-line-1 mt-1 max-h-24 overflow-auto border p-1 text-2xs"
                >{{ tl.plan.command }}</pre>
            </div>
            <p v-else class="text-fg-4 text-2xs">
              还没预检。导出走 FFmpeg，不碰 ComfyUI 也不碰 LLM——这条路在 AI 全部离线时照样能走完。
            </p>

            <div class="border-line-1 border-t pt-1.5">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">导出历史</p>
              <p v-if="tl.exports.length === 0" class="text-fg-4 mt-1 text-2xs">还没有导出记录。</p>
              <ul v-else class="mt-1 space-y-1">
                <li v-for="e in tl.exports" :key="e.id" class="text-2xs">
                  <span :class="e.status === 'done' ? 'text-st-done' : 'text-st-review'">
                    {{ e.status }}
                  </span>
                  <span class="text-fg-2"> {{ e.path }}</span>
                  <span class="text-fg-4">
                    · {{ e.version_ids.length }} 个版本 · {{ e.created_at.slice(0, 16) }}
                  </span>
                  <div v-if="e.error" class="border-st-failed/40 bg-base-2 mt-0.5 border p-1">
                    <p class="text-st-review">{{ e.error.title }}</p>
                    <p class="text-fg-2">{{ e.error.detail }}</p>
                    <ul v-if="e.error.suggestions.length" class="text-fg-2 mt-0.5 space-y-px">
                      <li v-for="s in e.error.suggestions" :key="s">· {{ s }}</li>
                    </ul>
                    <p class="text-fg-4 mt-0.5">{{ e.error.code }}</p>
                  </div>
                </li>
              </ul>
            </div>
          </div>
        </AppPanel>
      </div>

      <AppPanel title="片段属性" class="w-72 shrink-0">
        <EmptyState
          v-if="!selected"
          title="尚无选中片段"
          body="点轨道上的一块，这里可以改起点与入出点、切分、换成同一镜头的另一个版本，或者加一个转场。"
        />
        <div v-else class="space-y-3 p-2">
          <section>
            <p class="text-fg-1 text-2xs">
              {{ selected.shot_index_no ?? '—' }}. {{ selected.label ?? '未命名片段' }}
            </p>
            <p class="text-fg-4 text-2xs">
              时长 {{ fmt(selected.duration) }} ·
              <template v-if="selected.version_no">v{{ selected.version_no }}</template>
              <template v-else>手工片段</template>
            </p>
            <p v-if="selected.missing_file" class="text-st-review mt-0.5 text-2xs">
              这个片段登记的文件已经不在磁盘上，导出会失败。
            </p>
            <AppButton
              v-if="selected.shot_id"
              size="sm"
              variant="ghost"
              class="mt-1"
              title="去镜头编辑器看它的上下文与版本轨"
              @click="goShot(selected.shot_id)"
            >
              <Clapperboard :size="10" />打开镜头
            </AppButton>
          </section>

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">位置与裁切</p>
            <div class="mt-1 grid grid-cols-2 gap-1">
              <label class="block">
                <span class="text-fg-4 text-2xs">起点（秒）</span>
                <input
                  :value="selected.start"
                  type="number"
                  min="0"
                  step="0.1"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveStart(($event.target as HTMLInputElement).value)"
                />
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">入点（秒）</span>
                <input
                  :value="selected.in_point"
                  type="number"
                  min="0"
                  step="0.1"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveTrim('in_point', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">出点（秒，留空=到底）</span>
                <input
                  :value="selected.out_point ?? ''"
                  type="number"
                  min="0"
                  step="0.1"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveTrim('out_point', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label class="text-fg-4 flex items-end gap-1 pb-0.5 text-2xs">
                <input v-model="rippleTrim" type="checkbox" class="accent-accent" />
                裁切后贴紧后续
              </label>
            </div>
            <div class="mt-1 flex items-end gap-1">
              <label class="block min-w-0 flex-1">
                <span class="text-fg-4 text-2xs">在第几秒切开（时间线绝对秒数）</span>
                <input
                  v-model="splitAt"
                  type="number"
                  min="0"
                  step="0.1"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                />
              </label>
              <AppButton
                size="sm"
                :disabled="tl.busy || splitAt === ''"
                title="在这个位置把片段切成两段"
                @click="doSplit()"
              >
                <Scissors :size="10" />切分
              </AppButton>
            </div>
          </section>

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">声音</p>
            <div class="mt-1 flex items-center gap-1.5">
              <AppButton
                size="sm"
                variant="ghost"
                :disabled="tl.busy"
                :title="selected.muted ? '取消静音' : '静音这一段（不进成片）'"
                @click="saveMix({ muted: !selected.muted })"
              >
                <VolumeX v-if="selected.muted" :size="10" />
                <Volume2 v-else :size="10" />
                {{ selected.muted ? '已静音' : '出声' }}
              </AppButton>
              <span class="text-fg-4 tnum text-2xs">×{{ selected.volume.toFixed(2) }}</span>
            </div>
            <input
              type="range"
              min="0"
              max="4"
              step="0.05"
              :value="selected.volume"
              :disabled="tl.busy"
              class="accent-accent mt-1 w-full"
              title="音量倍数。上限 4；预览器最高只能到 1.0（浏览器限制），导出会按这里的值放大"
              @change="saveMix({ volume: Number(($event.target as HTMLInputElement).value) })"
            />
            <p v-if="selected.volume > 1" class="text-fg-4 text-2xs">
              大于 1 的部分预览里听不出来（浏览器最高 1.0），导出时才真的放大。
            </p>

            <!--
              「拆出声音」只对视频轨上有素材的片段有意义。拆完源片段会被静音——
              同一段声音出现两次比没声音更难查，所以这句必须写在按钮旁边。
            -->
            <template v-if="selectedTrack?.kind === 'video'">
              <AppButton
                v-if="!selected.detached_audio_clip_id"
                size="sm"
                class="mt-1.5"
                :disabled="tl.busy || !selected.asset_path || selected.missing_file"
                :title="
                  selected.asset_path
                    ? '把这段画面的声音抽成音频轨上的独立片段（FFmpeg 抽轨，源片段随之静音）'
                    : '这个片段没有素材文件，没有声音可拆'
                "
                @click="detachAudio()"
              >
                <AudioLines :size="10" />拆出声音
              </AppButton>
              <p v-else class="text-fg-4 mt-1.5 text-2xs">
                声音已经拆成独立片段了（源片段因此静音）。想收回就把那段音频删掉，再取消这里的静音。
              </p>
              <p class="text-fg-4 mt-0.5 text-2xs">
                这段视频没有音轨时后端会直接说出来，不会拆出一段无声文件。
              </p>
            </template>
            <p v-else-if="selected.source_clip_id" class="text-fg-4 mt-1.5 text-2xs">
              这段声音是从画面里拆出来的<template v-if="selected.source_missing"
                >，但那段画面已经不在了——它照旧能播，只是对不上任何画面</template
              >。
            </p>
            <p v-else class="text-fg-4 mt-1.5 text-2xs">
              导入的音频片段。音频轨之间可以重叠，导出时按各自的起点混在一起。
            </p>
          </section>

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">换成别的版本</p>
            <select
              v-if="versions.length"
              :value="selected.version_id ?? ''"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-1 h-5 w-full border px-1 text-2xs outline-none"
              @change="doReplaceVersion(($event.target as HTMLSelectElement).value)"
            >
              <option v-for="v in versions" :key="v.id" :value="v.id">
                v{{ v.version_no }} · {{ v.source === 'manual' ? '手工导入' : '生成' }}
                {{ v.is_current ? '（当前）' : '' }}
              </option>
            </select>
            <p v-else class="text-fg-4 mt-1 text-2xs">
              {{
                selected.shot_id
                  ? '这个镜头只有一个版本，没有可换的。'
                  : '手工片段不挂镜头，没有版本可换。'
              }}
            </p>
            <p class="text-fg-4 mt-1 text-2xs">
              换版本只改这一段，整条时间线不重排——所以别的片段的位置不会动。
            </p>
          </section>

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">转场</p>
            <div v-if="nextClip" class="mt-1 flex items-end gap-1">
              <select
                v-model="transitionKind"
                class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 min-w-0 flex-1 border px-1 text-2xs outline-none"
              >
                <option v-for="k in TRANSITION_KINDS" :key="k" :value="k">
                  {{ TRANSITION_LABEL[k] }}
                </option>
              </select>
              <input
                v-model.number="transitionDuration"
                type="number"
                min="0"
                step="0.1"
                class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum h-5 w-14 border px-1 text-2xs outline-none"
              />
              <AppButton size="sm" :disabled="tl.busy" @click="addTransition()">加转场</AppButton>
            </div>
            <p v-else class="text-fg-4 mt-1 text-2xs">
              它后面没有片段了：转场加在相邻两段之间，所以最后一段加不了。
            </p>
            <ul v-if="clipTransitions.length" class="mt-1 space-y-0.5">
              <li v-for="t in clipTransitions" :key="t.id" class="flex items-center gap-1 text-2xs">
                <span class="text-fg-2">
                  {{ TRANSITION_LABEL[t.kind] ?? t.kind }} · {{ fmt(t.duration) }}
                </span>
                <span class="text-fg-4">{{
                  t.from_clip_id === selected.id ? '（出）' : '（进）'
                }}</span>
                <button
                  class="text-fg-4 hover:text-st-failed ml-auto"
                  title="删掉这个转场"
                  @click="tl.removeTransition(pid, t.id).catch(() => {})"
                >
                  <Trash2 :size="10" />
                </button>
              </li>
            </ul>
          </section>

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">删除</p>
            <div class="mt-1 flex items-center gap-1">
              <AppButton
                size="sm"
                :disabled="tl.busy"
                title="删掉并把后面的片段贴上来（涟漪删除）"
                @click="doRemove(selected.id, true)"
              >
                <Trash2 :size="10" />删除并贴紧
              </AppButton>
              <AppButton
                size="sm"
                variant="ghost"
                :disabled="tl.busy"
                title="删掉但留一个空档，后面的片段不动"
                @click="doRemove(selected.id, false)"
              >
                留空档
              </AppButton>
            </div>
            <p class="text-fg-4 mt-1 text-2xs">
              删片段不动素材：版本与文件都还在，改主意了可以「撤销」或重新装配。
            </p>
          </section>
        </div>
      </AppPanel>
    </div>
  </div>
</template>
