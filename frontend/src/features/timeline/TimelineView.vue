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
 *   3. **导出状态集中到控制台**。预览区只保留导出成片与打开成片文件夹，
 *      运行状态、失败详情和后端日志统一落到任务 / 日志框。
 *   4. **缺文件的片段自己举手**。`missing_file` 的片段画成红框并在工具栏计数——
 *      导出会在半路失败，不如提前说。
 *   5. **播放头是唯一的时间真相**。标尺上那根竖条、预览器播到哪、「在播放头处切分」用的
 *      都是同一个 `playhead`（`v-model:playhead` 双向绑定）：拖标尺画面跟着走，播着的
 *      时候竖条也跟着走。两边各记一个「当前时间」的话，看到的和切出来的会分叉。
 *   6. **声音有两条路**：视频片段自带的音轨（可静音 / 调音量），与音频轨上的独立片段
 *      （从画面「拆出声音」或导入配乐）。音频轨之间可以随意重叠——叠加是它存在的意义，
 *      不是错误；后端导出时用 `amix` 把它们混在一起。
 *   7. **片段属性里没有转场**。两段之间怎么接是**分镜那一页**的事：每两个 Shot（以及每两幕）
 *      之间那条线选「转场」就会生成一段真的过渡视频，装配到这里就是一个正常片段。
 *      在这里再摆一份 ffmpeg 层的叠化只会变成第二处配置，和分镜上那条线互相打架。
 *      后端的 `Transition` 接口原样留着（兼容路径），只是不再有界面入口。
 *
 * 拖拽只做三件事：拖块身 = 移动、拖左右边界线 = 生成裁切草稿、拖标尺 = 移播放头。
 * 裁切草稿只在片段内预览，右键确认后才发请求，返回的整条时间线整体覆盖。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  AudioLines,
  Check,
  Clapperboard,
  Crop,
  Download,
  FolderOpen,
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
import AppDialog from '@/shared/ui/AppDialog.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import SplitPane from '@/shared/ui/SplitPane.vue'
import PreviewPlayer from './PreviewPlayer.vue'
import { TRACK_KIND_LABEL, type Clip, type Track } from '@/shared/api/timeline'
import { generationApi, type GenerationVersion } from '@/shared/api/generation'
import { assetsApi, type Asset } from '@/shared/api/assets'
import { ApiError, confirmFlagOf } from '@/shared/api/client'
import { useConsoleStore } from '@/stores/console'
import { useTimelineStore } from '@/stores/timeline'

const route = useRoute()
const router = useRouter()
const tl = useTimelineStore()
const consolePanel = useConsoleStore()

const pid = computed(() => String(route.params.pid ?? ''))

/** 轨道区的比例尺：每秒多少像素。 */
const zoom = ref(24)
const selectedId = ref('')
/** 选中片段所属镜头的版本列表——「换成另一个版本」的下拉数据。 */
const versions = ref<GenerationVersion[]>([])
/** 播放头（秒）。预览器与标尺上那根竖条共用它，见文件开头第 5 条。 */
const playhead = ref(0)
/** 导入配乐用的隐藏 input。 */
const audioInput = ref<HTMLInputElement | null>(null)
const materialInput = ref<HTMLInputElement | null>(null)
const materialKind = ref<'video' | 'audio'>('video')
const materialAssets = ref<Asset[]>([])
const blankDuration = ref(2)
const importing = ref(false)
/** 上一次操作的说明（拆出声音 / 导入音频落到哪条轨上）——落地位置必须说出来。 */
const note = ref('')
/** 等确认的删轨：后端回 CONFLICT + `confirm: "force"` 时记下来，确认后带 force 重放。 */
const forceTrackId = ref('')
const trimDialogOpen = ref(false)

interface TrimDraft {
  inOffset: number
  outOffset: number
}

interface ClipMenu {
  clipId: string
  x: number
  y: number
}

/** 边界线只是裁切草稿；用户确认前不写后端。偏移量相对当前片段。 */
const trimDrafts = ref<Record<string, TrimDraft>>({})
const clipMenu = ref<ClipMenu | null>(null)

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
const menuClip = computed<Clip | null>(
  () => tl.clips.find((c) => c.id === clipMenu.value?.clipId) ?? null,
)
const latestExport = computed(() => tl.exports[0] ?? null)

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
  await Promise.all([tl.load(pid.value).catch(() => {}), loadMaterials()])
}

onMounted(reload)
watch(pid, reload)
onMounted(() => window.addEventListener('click', closeClipMenu))
onUnmounted(() => window.removeEventListener('click', closeClipMenu))

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

function isVideoAsset(asset: Asset): boolean {
  return Boolean(
    asset.mime?.startsWith('video/') || /\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(asset.path),
  )
}

function isAudioAsset(asset: Asset): boolean {
  return Boolean(
    asset.mime?.startsWith('audio/') || /\.(mp3|wav|m4a|aac|flac|ogg|opus)$/i.test(asset.path),
  )
}

const materials = computed(() =>
  materialAssets.value.filter((asset) => isVideoAsset(asset) || isAudioAsset(asset)),
)

async function loadMaterials(): Promise<void> {
  if (!pid.value) return
  materialAssets.value = (await assetsApi.list(pid.value).catch(() => [])).filter(
    (asset) => asset.kind === 'upload' || asset.kind === 'audio',
  )
}

function materialName(asset: Asset): string {
  return asset.path.split(/[\\/]/).pop() || asset.path
}

function pickMaterial(kind: 'video' | 'audio'): void {
  materialKind.value = kind
  materialInput.value?.click()
}

async function onPickMaterial(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  importing.value = true
  importError.value = null
  try {
    const asset = await assetsApi.upload(
      pid.value,
      file,
      materialKind.value === 'audio' ? 'audio' : 'upload',
    )
    materialAssets.value = [asset, ...materialAssets.value.filter((row) => row.id !== asset.id)]
    note.value = `「${file.name}」已加入素材框，可拖入${materialKind.value === 'audio' ? '音频' : '视频'}轨。`
  } catch (err) {
    importError.value = err instanceof ApiError ? err : null
  } finally {
    importing.value = false
  }
}

function dragMaterial(event: DragEvent, asset: Asset | null): void {
  if (!event.dataTransfer) return
  event.dataTransfer.effectAllowed = 'copy'
  event.dataTransfer.setData(
    'application/x-ai-video-material',
    JSON.stringify(
      asset
        ? { type: isAudioAsset(asset) ? 'audio' : 'video', assetId: asset.id }
        : { type: 'blank', duration: Math.max(MIN_LEN, blankDuration.value) },
    ),
  )
}

async function dropMaterial(event: DragEvent, track: Track): Promise<void> {
  event.preventDefault()
  const raw = event.dataTransfer?.getData('application/x-ai-video-material')
  if (!raw || track.locked) return
  let payload: { type: 'video' | 'audio' | 'blank'; assetId?: string; duration?: number }
  try {
    payload = JSON.parse(raw) as typeof payload
  } catch {
    return
  }
  if ((payload.type === 'audio') !== (track.kind === 'audio')) {
    note.value = payload.type === 'audio' ? '音频素材只能放入音频轨。' : '视频与空白段只能放入视频轨。'
    return
  }
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const start = Math.max(0, (event.clientX - rect.left) / zoom.value)
  const clipId =
    payload.type === 'blank'
      ? await tl
          .addBlankClip(pid.value, track.id, { duration: payload.duration ?? blankDuration.value })
          .catch(() => '')
      : await tl
          .addClip(pid.value, track.id, { asset_id: payload.assetId ?? '', start })
          .catch(() => '')
  if (!clipId) return
  selectedId.value = clipId
  note.value =
    track.kind === 'video'
      ? `素材已放入「${track.name}」并与前后画面自动贴紧。`
      : `素材已放入「${track.name}」的 ${fmt(start)}；同轨片段不能重叠。`
}

function isBlankClip(clip: Clip | null): boolean {
  return Boolean(clip && !clip.asset_id && !clip.shot_id && !clip.version_id)
}

async function saveBlankDuration(value: string): Promise<void> {
  const clip = selected.value
  const duration = Number(value)
  if (!isBlankClip(clip) || !clip || !Number.isFinite(duration) || duration < MIN_LEN) return
  await tl.resizeBlankClip(pid.value, clip.id, duration).catch(() => {})
}

async function moveAudioToNewTrack(): Promise<void> {
  const clip = selected.value
  if (!clip || clip.track_kind !== 'audio') return
  const trackId = await tl.moveToNewAudioTrack(pid.value, clip.id).catch(() => null)
  if (trackId) note.value = '音频片段已移到新音频轨，可与原轨同一时间叠加。'
  closeClipMenu()
}

async function assemble(replace: boolean): Promise<void> {
  await tl.assemble(pid.value, replace).catch(() => {})
}

async function saveStart(value: string): Promise<void> {
  const clip = selected.value
  const start = Number(value)
  if (!clip || !Number.isFinite(start) || start < 0) return
  await tl.move(pid.value, clip.id, start).catch(() => {})
}

function draftOf(clip: Clip): TrimDraft {
  return (
    trimDrafts.value[clip.id] ?? {
      inOffset: Math.max(0, clip.in_point),
      outOffset: clip.out_point ?? clip.in_point + clip.duration,
    }
  )
}

function trimBounds(clip: Clip): { inOffset: number; outOffset: number } {
  const draft = draftOf(clip)
  const sourceEnd = Math.max(MIN_LEN, clip.source_duration)
  const out = Math.max(MIN_LEN, Math.min(draft.outOffset, sourceEnd))
  const inOffset = Math.max(0, Math.min(draft.inOffset, out - MIN_LEN))
  return {
    inOffset,
    outOffset: Math.max(inOffset + MIN_LEN, out),
  }
}

function hasTrimDraft(clip: Clip): boolean {
  const draft = trimBounds(clip)
  const currentEnd = clip.out_point ?? clip.in_point + clip.duration
  return (
    Math.abs(draft.inOffset - clip.in_point) > 0.001 ||
    Math.abs(draft.outOffset - currentEnd) > 0.001
  )
}

function closeClipMenu(): void {
  clipMenu.value = null
}

function selectClip(clip: Clip): void {
  selectedId.value = clip.id
  // 有裁切草稿时，“最左侧进度点”是左裁切线；否则就是片段起点。
  const left = trimBounds(clip).inOffset
  playhead.value = clampTime(clip.start + left - clip.in_point)
}

function openClipMenu(event: MouseEvent, clip: Clip): void {
  event.preventDefault()
  event.stopPropagation()
  selectClip(clip)
  clipMenu.value = {
    clipId: clip.id,
    x: Math.min(event.clientX, window.innerWidth - 190),
    y: Math.min(event.clientY, window.innerHeight - 150),
  }
}

function requestTrim(): void {
  if (!menuClip.value) return
  trimDialogOpen.value = true
  closeClipMenu()
}

async function confirmTrim(): Promise<void> {
  const clip = selected.value
  if (!clip) return
  const draft = trimBounds(clip)
  if (!hasTrimDraft(clip) || draft.outOffset - draft.inOffset < MIN_LEN) return
  trimDialogOpen.value = false
  try {
    await tl.trim(pid.value, clip.id, {
      in_point: draft.inOffset,
      out_point: draft.outOffset,
      start: clip.start + (draft.inOffset - clip.in_point),
      ripple: false,
    })
  } catch {
    return
  }
  const next = { ...trimDrafts.value }
  delete next[clip.id]
  trimDrafts.value = next
}

async function isolateAudioSelection(): Promise<void> {
  const clip = menuClip.value ?? selected.value
  if (!clip || clip.track_kind !== 'audio' || !hasTrimDraft(clip)) return
  const draft = trimBounds(clip)
  const out = await tl
    .isolateAudioSelection(pid.value, clip.id, {
      in_point: draft.inOffset,
      out_point: draft.outOffset,
    })
    .catch(() => null)
  if (!out) return
  const next = { ...trimDrafts.value }
  delete next[clip.id]
  trimDrafts.value = next
  selectedId.value = out.selectedClipId
  note.value = `选中音频已独立切成一段，本次共拆为 ${out.segments} 段。`
  closeClipMenu()
}

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
  originIn: number
  originOut: number
}

const drag = ref<Drag | null>(null)
/** 小于这个像素当一次点击而不是拖动：手抖不该改剪辑。 */
const DRAG_SLOP = 3
/** 裁到比这更短就没有意义了（后端也会拒绝入点越过出点）。 */
const MIN_LEN = 0.05

function beginDrag(event: PointerEvent, clip: Clip, mode: DragMode, track: Track): void {
  if (event.button !== 0) return
  selectClip(clip)
  if (track.locked) return // 锁了的轨道只能看
  capture(event)
  const draft = trimBounds(clip)
  drag.value = {
    id: clip.id,
    mode,
    originX: event.clientX,
    dx: 0,
    originIn: draft.inOffset,
    originOut: draft.outOffset,
  }
}

function onDragMove(event: PointerEvent): void {
  if (!drag.value) return
  drag.value.dx = event.clientX - drag.value.originX
  const clip = tl.clips.find((c) => c.id === drag.value?.id)
  if (!clip || drag.value.mode === 'move') return
  const delta = drag.value.dx / zoom.value
  const currentEnd = clip.out_point ?? clip.in_point + clip.duration
  const next =
    drag.value.mode === 'left'
      ? {
          inOffset: Math.min(
            drag.value.originOut - MIN_LEN,
            Math.max(clip.in_point, drag.value.originIn + delta),
          ),
          outOffset: drag.value.originOut,
        }
      : {
          inOffset: drag.value.originIn,
          outOffset: Math.max(
            drag.value.originIn + MIN_LEN,
            Math.min(currentEnd, drag.value.originOut + delta),
          ),
        }
  trimDrafts.value = { ...trimDrafts.value, [clip.id]: next }
}

/** 外框只预览整段移动；裁切草稿绝不改变 Shot 外框的起点或长度。 */
function clipStyle(clip: Clip): Record<string, string> {
  const d = drag.value?.id === clip.id ? drag.value : null
  const left = clip.start * zoom.value + (d?.mode === 'move' ? d.dx : 0)
  return {
    left: `${Math.max(0, left)}px`,
    width: `${Math.max(8, clip.duration * zoom.value)}px`,
  }
}

function trimLinePercent(clip: Clip, side: 'left' | 'right'): number {
  const bounds = trimBounds(clip)
  const value = side === 'left' ? bounds.inOffset : bounds.outOffset
  return Math.max(0, Math.min(100, ((value - clip.in_point) / clip.duration) * 100))
}

function trimShadeStyle(clip: Clip, side: 'left' | 'right'): Record<string, string> {
  const left = trimLinePercent(clip, 'left')
  const right = trimLinePercent(clip, 'right')
  return side === 'left'
    ? { left: '0', width: `${left}%` }
    : { left: `${right}%`, right: '0' }
}

async function endDrag(event: PointerEvent): Promise<void> {
  const d = drag.value
  drag.value = null
  release(event)
  const clip = d ? tl.clips.find((c) => c.id === d.id) : null
  if (!d || !clip || Math.abs(d.dx) < DRAG_SLOP) return
  const delta = d.dx / zoom.value
  if (d.mode === 'move') {
    await tl.move(pid.value, clip.id, Math.max(0, clip.start + delta)).catch(() => {})
    return
  }
  // 裁切边界只保存草稿；右键选择“裁切”并在确认框中确认后才提交。
}

async function doRemove(clipId: string, ripple: boolean): Promise<void> {
  await tl.remove(pid.value, clipId, ripple).catch(() => {})
  const next = { ...trimDrafts.value }
  delete next[clipId]
  trimDrafts.value = next
  closeClipMenu()
}

function cancelTrimDraft(clipId: string): void {
  const next = { ...trimDrafts.value }
  delete next[clipId]
  trimDrafts.value = next
  closeClipMenu()
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

async function runExport(): Promise<void> {
  consolePanel.show('logs')
  await tl.runExport(pid.value, null)
}

async function openExportFolder(): Promise<void> {
  consolePanel.show('logs')
  await tl.openExportFolder(pid.value)
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

    <div class="min-h-0 flex-1 p-2">
      <SplitPane id="timeline-main" direction="horizontal" :sizes="[78, 22]" :min-sizes="[55, 18]">
        <template #pane-0>
          <div class="flex min-h-0 min-w-0 flex-1 gap-1.5 pr-1.5">
            <AppPanel title="素材" class="w-52 shrink-0">
              <template #actions>
                <button
                  class="text-fg-3 hover:text-fg-1"
                  :disabled="importing"
                  title="上传视频素材"
                  @click="pickMaterial('video')"
                >
                  <Plus :size="11" />
                </button>
              </template>
              <div class="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto p-1.5">
                <input
                  ref="materialInput"
                  type="file"
                  class="hidden"
                  accept="video/*,audio/*"
                  @change="onPickMaterial"
                />
                <div class="grid grid-cols-2 gap-1">
                  <AppButton size="sm" variant="ghost" :disabled="importing" @click="pickMaterial('video')">
                    <Clapperboard :size="10" />视频
                  </AppButton>
                  <AppButton size="sm" variant="ghost" :disabled="importing" @click="pickMaterial('audio')">
                    <Music :size="10" />音频
                  </AppButton>
                </div>
                <label class="border-line-1 bg-base-2 flex items-center justify-between border px-1.5 py-1 text-2xs">
                  <span class="text-fg-3">空白视频段</span>
                  <input
                    v-model.number="blankDuration"
                    type="number"
                    min="0.05"
                    step="0.1"
                    class="border-line-1 bg-base-1 text-fg-1 tnum h-5 w-14 border px-1 text-right text-2xs outline-none"
                    title="拖入视频轨时使用的黑场时长"
                  />
                </label>
                <button
                  draggable="true"
                  class="border-line-1 bg-base-2 text-fg-2 hover:border-accent/60 flex items-center gap-1 border p-1.5 text-left text-2xs"
                  title="拖入视频轨添加黑场"
                  @dragstart="dragMaterial($event, null)"
                >
                  <span class="bg-black inline-block h-7 w-10 border border-white/20" />
                  <span class="min-w-0 truncate">黑场 · {{ fmt(blankDuration) }}</span>
                </button>
                <div
                  v-for="asset in materials"
                  :key="asset.id"
                  draggable="true"
                  class="border-line-1 bg-base-2 text-fg-2 hover:border-accent/60 flex items-center gap-1 border p-1.5 text-left text-2xs"
                  :title="`${materialName(asset)} · 拖入${isAudioAsset(asset) ? '音频' : '视频'}轨`"
                  @dragstart="dragMaterial($event, asset)"
                >
                  <span class="bg-base-3 flex h-7 w-10 shrink-0 items-center justify-center">
                    <Music v-if="isAudioAsset(asset)" :size="12" />
                    <Clapperboard v-else :size="12" />
                  </span>
                  <span class="min-w-0 truncate">{{ materialName(asset) }}</span>
                </div>
                <p v-if="materials.length === 0" class="text-fg-4 px-1 text-2xs">
                  上传视频或音频后，从这里拖到对应轨道。
                </p>
              </div>
            </AppPanel>
            <div class="flex min-h-0 min-w-0 flex-1 flex-col gap-1.5">
            <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1 border px-1.5">
              <span class="text-fg-3 text-2xs">成片预览</span>
              <span class="text-fg-4 text-2xs">{{ clock(playhead) }} / {{ clock(total) }}</span>
              <span v-if="latestExport" class="text-fg-4 max-w-[28rem] truncate text-2xs" :title="latestExport.path">
                · 最近 {{ latestExport.status === 'done' ? '导出完成' : latestExport.status }}：{{ latestExport.path }}
              </span>
              <div class="ml-auto flex items-center gap-1">
                <AppButton size="sm" :disabled="tl.busy || tl.clips.length === 0" @click="runExport()">
                  <Download :size="10" />导出成片
                </AppButton>
                <AppButton size="sm" variant="ghost" :disabled="tl.busy" @click="openExportFolder()">
                  <FolderOpen :size="10" />打开成片文件夹
                </AppButton>
              </div>
            </div>
            <SplitPane id="timeline-preview" direction="vertical" :sizes="[42, 58]" :min-sizes="[24, 28]">
              <template #pane-0>
                <PreviewPlayer
                  v-model:playhead="playhead"
                  :pid="pid"
                  :timeline="tl.timeline"
                  class="min-h-0 flex-1"
                />
              </template>
              <template #pane-1>
        <!-- 轨道区：按秒数比例画块 + 一根贯穿所有轨道的播放头竖条 -->
        <AppPanel title="轨道区" class="min-h-0 flex-1">
          <template #actions>
            <span class="text-fg-4 text-2xs">
              拖标尺移播放头 · 拖块身移动 · 拖左右边界线生成裁切草稿 · 右键确认后提交
            </span>
          </template>
          <EmptyState
            v-if="tl.clips.length === 0"
            title="轨道上还没有片段"
            body="点「自动装配」把每个镜头的当前版本按 Scene / Shot 顺序铺上来。没有当前版本的镜头会被跳过并写明理由——这一步不需要 ComfyUI。"
          />
          <div class="overflow-x-auto p-2">
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
                  @dragover.prevent
                  @drop="dropMaterial($event, track)"
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
                    :title="`${clip.label ?? clip.id} · 起点 ${fmt(clip.start)} · 时长 ${fmt(clip.duration)}${clip.muted ? ' · 已静音' : ''}${clip.missing_file ? ' · 文件已丢失' : ''}${track.locked ? ' · 轨道已锁，拖不动' : ' · 拖块身移动；拖内部边界线生成裁切草稿'}`"
                    @click="selectClip(clip)"
                    @contextmenu="openClipMenu($event, clip)"
                    @pointerdown="beginDrag($event, clip, 'move', track)"
                    @pointermove="onDragMove($event)"
                    @pointerup="endDrag($event)"
                    @pointercancel="endDrag($event)"
                  >
                    <!-- 草稿只移动两条内部裁切线；Shot 外框与时长在确认前保持不变。 -->
                    <template v-if="hasTrimDraft(clip)">
                      <span
                        class="pointer-events-none absolute top-0 bottom-0 z-[1] bg-black/45"
                        :style="trimShadeStyle(clip, 'left')"
                      />
                      <span
                        class="pointer-events-none absolute top-0 bottom-0 z-[1] bg-black/45"
                        :style="trimShadeStyle(clip, 'right')"
                      />
                      <span
                        class="border-accent/60 pointer-events-none absolute top-0 bottom-0 z-[2] border-y bg-accent/10"
                        :style="{
                          left: `${trimLinePercent(clip, 'left')}%`,
                          right: `${100 - trimLinePercent(clip, 'right')}%`,
                        }"
                      />
                    </template>
                    <!-- 左线确认后才同时改变入点与时间线起点。 -->
                    <span
                      v-if="!track.locked"
                      class="hover:bg-accent/80 absolute top-0 bottom-0 z-10 w-1 cursor-ew-resize bg-white/80"
                      :style="{ left: `calc(${trimLinePercent(clip, 'left')}% - 2px)` }"
                      title="拖动左裁切线；Shot 长度在确认前不会变化"
                      @pointerdown.stop="beginDrag($event, clip, 'left', track)"
                      @pointermove.stop="onDragMove($event)"
                      @pointerup.stop="endDrag($event)"
                      @pointercancel.stop="endDrag($event)"
                    />
                    <span
                      v-if="!track.locked"
                      class="hover:bg-accent/80 absolute top-0 bottom-0 z-10 w-1 cursor-ew-resize bg-white/80"
                      :style="{ left: `calc(${trimLinePercent(clip, 'right')}% - 2px)` }"
                      title="拖动右裁切线；Shot 长度在确认前不会变化"
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
                        <template v-if="hasTrimDraft(clip)">
                          → {{ fmt(trimBounds(clip).outOffset - trimBounds(clip).inOffset) }} 待确认
                        </template>
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
            <div
              v-if="clipMenu && menuClip"
              class="border-line-2 bg-base-1 fixed z-50 w-44 border p-1 shadow-xl"
              :style="{ left: `${clipMenu.x}px`, top: `${clipMenu.y}px` }"
              @click.stop
            >
              <button
                class="text-fg-1 hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left text-2xs disabled:cursor-not-allowed disabled:opacity-40"
                :disabled="!hasTrimDraft(menuClip) || tl.busy"
                @click="requestTrim()"
              >
                <Crop :size="11" />确认裁切边界
              </button>
              <button
                v-if="hasTrimDraft(menuClip)"
                class="text-fg-1 hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left text-2xs"
                :disabled="tl.busy"
                @click="cancelTrimDraft(menuClip.id)"
              >
                <Undo2 :size="11" />取消裁切草稿
              </button>
              <button
                v-if="menuClip.track_kind === 'audio' && hasTrimDraft(menuClip)"
                class="text-fg-1 hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left text-2xs"
                :disabled="tl.busy"
                title="保留全部音频，把两条裁切线之间的选区独立成一段"
                @click="isolateAudioSelection()"
              >
                <Scissors :size="11" />选中切断
              </button>
              <button
                v-if="menuClip.track_kind === 'video'"
                class="text-fg-1 hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left text-2xs"
                :disabled="tl.busy || !menuClip.asset_path || menuClip.missing_file"
                @click="detachAudio(); closeClipMenu()"
              >
                <AudioLines :size="11" />分割音频到独立轨
              </button>
              <button
                v-if="menuClip.track_kind === 'audio'"
                class="text-fg-1 hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left text-2xs"
                :disabled="tl.busy"
                title="把裁切后的整段音频移到一条新轨道，可与旧轨道重叠"
                @click="moveAudioToNewTrack()"
              >
                <Plus :size="11" />移到新音频轨
              </button>
              <div class="bg-line-1 my-1 h-px" />
              <button
                class="text-st-failed hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left text-2xs"
                :disabled="tl.busy"
                title="删除片段并让后续片段自动贴紧"
                @click="doRemove(menuClip.id, true)"
              >
                <Trash2 :size="11" />删除并贴紧
              </button>
              <button
                v-if="menuClip.track_kind === 'audio'"
                class="text-fg-1 hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left text-2xs"
                :disabled="tl.busy"
                title="删除片段但保留原位置的空档"
                @click="doRemove(menuClip.id, false)"
              >
                <Trash2 :size="11" />删除并留空档
              </button>
            </div>
            <p v-if="tl.missing.length" class="text-st-review mt-2 text-2xs">
              有 {{ tl.missing.length }} 个片段的文件已经不在磁盘上：导出会在半路失败。
              先把它们换成别的版本或删掉。
            </p>
          </div>
        </AppPanel>
              </template>
            </SplitPane>
          </div>
          </div>
        </template>
        <template #pane-1>
      <AppPanel title="片段属性" class="min-h-0 flex-1">
        <EmptyState
          v-if="!selected"
          title="尚无选中片段"
          body="点轨道上的一块，这里可以改起点、切分、拆出声音、换版本或添加转场。"
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
                  :disabled="selectedTrack?.kind === 'video'"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  :title="selectedTrack?.kind === 'video' ? '视频轨不允许空档；左右拖动片段只调整顺序' : '音频可放在本轨空闲位置'"
                  @change="saveStart(($event.target as HTMLInputElement).value)"
                />
              </label>
              <label v-if="isBlankClip(selected)" class="block">
                <span class="text-fg-4 text-2xs">空白时长（秒）</span>
                <input
                  :value="selected.duration"
                  type="number"
                  min="0.05"
                  step="0.1"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveBlankDuration(($event.target as HTMLInputElement).value)"
                />
              </label>
              <div class="text-fg-4 text-2xs">
                <span class="block">当前保留区间</span>
                <span class="text-fg-1 tnum mt-px block">
                  {{ fmt(selected.in_point) }} → {{ fmt(selected.out_point ?? selected.in_point + selected.duration) }}
                </span>
              </div>
              <div class="text-fg-4 text-2xs">
                <span class="block">原素材长度</span>
                <span class="text-fg-1 tnum mt-px block">{{ fmt(selected.source_duration) }}</span>
              </div>
            </div>
            <p v-if="hasTrimDraft(selected)" class="text-st-review mt-1 text-2xs">
              待确认保留 {{ fmt(trimBounds(selected).inOffset) }} → {{ fmt(trimBounds(selected).outOffset) }}；右键片段选择“确认裁切”。
            </p>
            <AppButton
              v-if="selected.track_kind === 'audio' && hasTrimDraft(selected)"
              size="sm"
              variant="ghost"
              class="mt-1"
              :disabled="tl.busy"
              title="不删除选区外音频，把选中范围独立为一段"
              @click="isolateAudioSelection()"
            >
              <Scissors :size="10" />选中切断
            </AppButton>
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
            <p class="text-fg-4 mt-1 text-2xs">
              两段之间怎么接在分镜那一页配：每两个 Shot（以及每两幕）之间那条线选「转场」，
              生成出来的过渡视频装配到这里就是一个正常片段。
            </p>
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
                v-if="selectedTrack?.kind === 'audio'"
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
              </template>
      </SplitPane>
    </div>
    <AppDialog
      v-model:open="trimDialogOpen"
      title="确认裁切"
      subtitle="确认后才会写入时间线，原视频文件不会被修改"
      size="sm"
    >
      <div v-if="selected" class="space-y-2 p-3 text-2xs">
        <p class="text-fg-2">{{ selected.label ?? '未命名片段' }}</p>
        <div class="bg-base-2 border-line-1 grid grid-cols-2 gap-2 border p-2">
          <div>
            <p class="text-fg-4">保留原视频区间</p>
            <p class="text-fg-1 tnum mt-0.5">
              {{ fmt(trimBounds(selected).inOffset) }} → {{ fmt(trimBounds(selected).outOffset) }}
            </p>
          </div>
          <div>
            <p class="text-fg-4">保留时长</p>
            <p class="text-fg-1 tnum mt-0.5">
              {{ fmt(trimBounds(selected).outOffset - trimBounds(selected).inOffset) }}
            </p>
          </div>
        </div>
        <p class="text-fg-4">原素材长度 {{ fmt(selected.source_duration) }}。只保留两条边界线之间的内容。</p>
      </div>
      <template #footer>
        <AppButton variant="ghost" @click="trimDialogOpen = false">取消</AppButton>
        <AppButton
          variant="primary"
          :disabled="!selected || !hasTrimDraft(selected) || tl.busy"
          @click="confirmTrim()"
        >
          <Check :size="10" />确认裁切
        </AppButton>
      </template>
    </AppDialog>
  </div>
</template>
