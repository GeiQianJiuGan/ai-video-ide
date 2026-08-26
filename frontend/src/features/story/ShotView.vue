<script setup lang="ts">
/**
 * 镜头编辑器（Step 6 / Step 7 的前端）。
 *
 * 这一页是「这条片段是怎么来的」的唯一答案页：左边是镜头本身与出场，
 * 中间是**上下文账单**——真正喂给模型的每一条参考，连没被采用的也列出来并写明理由，
 * 右边是版本轨（只增不改），底部是 prompt 与参数。
 *
 * 三个刻意的设计：
 *   1. **账单里没被采用的条目照样显示**。「为什么这张角色表没进去」比「进去了哪几张」
 *      更常是用户要问的问题，藏起来等于让人去猜。
 *   2. **入队被拒是正常结果**。上下文不完整时后端拒绝，页面把理由显示出来，
 *      同时留一颗「跳过检查强行入队」——它是显式选择，不是默认值。
 *   3. **手动导入的成片也走版本系统**。不接 AI 也能把工程做完（硬约束 2）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  GitBranch,
  ListVideo,
  Mic,
  Play,
  RefreshCw,
  RotateCcw,
  Scissors,
  Sparkles,
  Star,
  Upload,
  VolumeX,
  X,
  Zap,
} from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import SegmentPlayer from '@/shared/ui/SegmentPlayer.vue'
import DubModal from './components/DubModal.vue'
import RefineModal from './components/RefineModal.vue'
import { fileUrl } from '@/shared/api/files'
import { ApiError, confirmFlagOf } from '@/shared/api/client'
import { assetsApi, type Asset } from '@/shared/api/assets'
import { castApi, type AppearanceRow, type Character } from '@/shared/api/cast'
import { worldApi, type Prop } from '@/shared/api/world'
import type { AudioVersionItem } from '@/shared/api/dub'
import { refineApi, type VersionLineageResult } from '@/shared/api/refine'
import {
  CONTEXT_KIND_LABEL,
  CONTEXT_MEDIA_LABEL,
  type ContextCapacityMedia,
  type ContextItem,
  type GenerationVersion,
} from '@/shared/api/generation'
import { SHOT_STATUS, SHOT_STATUS_LABEL } from '@/shared/api/story'
import { useConsoleStore } from '@/stores/console'
import { useShotStore } from '@/stores/shot'
import { useStoryStore } from '@/stores/story'

const route = useRoute()
const router = useRouter()
const editor = useShotStore()
const story = useStoryStore()
const consolePanel = useConsoleStore()

const pid = computed(() => String(route.params.pid ?? ''))
const sid = computed(() => String(route.params.sid ?? ''))

/** 出场角色可选项：所有角色的所有形象拉平成一张表。 */
const appearances = ref<{ row: AppearanceRow; character: Character }[]>([])
const props_ = ref<Prop[]>([])
const assets = ref<Asset[]>([])
const sideError = ref<ApiError | null>(null)
const uploading = ref(false)
const versionInput = ref<HTMLInputElement | null>(null)
const contextInput = ref<HTMLInputElement | null>(null)
const frameInput = ref<HTMLInputElement | null>(null)
/** 首帧 / 末帧共用一个 file input，记住正在填哪一个槽位。 */
const pickingSlot = ref<FrameSlotKey>('first_frame_asset_id')

type FrameSlotKey = 'first_frame_asset_id' | 'last_frame_asset_id'
/** 某一族的槽位账 + 它是哪一族——界面上每族一行，所以把 key 带进值里。 */
type CapBlock = ContextCapacityMedia & { media: 'image' | 'video' | 'audio' }

const shot = computed(() => editor.shot)
const bill = computed(() => editor.bill)
/**
 * 这一次模型端能收几个参考素材。上限不是我们配的数字，而是那份图的事实
 * （预设里标了几个 `AIVS_REF_*` / `AIVS_REF_VIDEO_*` / `AIVS_REF_AUDIO_*`；
 * REST 合同与「还没选预设」都是不限张数）。
 */
const cap = computed(() => bill.value?.capacity ?? null)
/**
 * 三族分开报。混在一起数的话「图多音频少」会显示成装得下，然后那段音频被安静丢掉。
 * 只列有素材的那几族，一条都没有的族不占位置。
 */
const capBlocks = computed<CapBlock[]>(() => {
  const per = cap.value?.media
  if (!per) return []
  const out: CapBlock[] = []
  for (const media of ['image', 'video', 'audio'] as const) {
    const block = per[media]
    if (block && (block.ref_count > 0 || block.over)) out.push({ ...block, media })
  }
  return out
})
const capText = computed(() => {
  const c = cap.value
  if (!c) return ''
  const blocks = capBlocks.value
  if (!blocks.length) {
    return c.limit === null ? '参考素材 0 个 · 不限' : `参考图 0 / ${c.limit}`
  }
  return blocks
    .map((b) => `${b.label} ${b.ref_count}${b.limit === null ? '' : ` / ${b.limit}`}`)
    .join(' · ')
})
/** 会被挤掉的那几个，按族说清——「装不下」的原因每族不一样。 */
const dropText = computed(() =>
  capBlocks.value
    .filter((b) => b.over)
    .map(
      (b) =>
        `${b.label}采用了 ${b.ref_count} 个、只能喂 ${b.limit} 个（会丢：${b.dropped_labels.join('、')}）`,
    )
    .join('；'),
)
/** 装不下时那一次确认：记住刚才是不是「跳过检查」，确认后原样重来。 */
const pendingDrop = ref<{ skipContext: boolean } | null>(null)
const askDrop = computed(
  () => pendingDrop.value !== null && Boolean(confirmFlagOf(editor.lastError)),
)
/**
 * 首 / 末帧槽位只能挑图片：模型端那两个入口接的是 LoadImage。
 * 视频 / 音频不是「不能用」，是该当参考素材加进上下文里。
 */
const IMAGE_SUFFIX = /\.(png|jpe?g|webp|bmp|gif|tiff?)$/i
const imageAssets = computed(() =>
  assets.value.filter((a) => !a.missing && IMAGE_SUFFIX.test(a.path)),
)

/**
 * 首帧来源：显式槽位 + 上游镜头（如果有的话）。
 * **有上游时，上游显示在最上面且槽位禁用**——首帧强制从上游末帧来。
 * **转场镜头首帧不能手动设置**——首帧来自上游镜头末帧，自动确定。
 */
interface FrameSource {
  type: 'upstream' | 'slot'
  label: string
  hint: string
  assetId: string | null
  path: string | null
  disabled?: boolean
  key?: FrameSlotKey
}

const firstFrameSources = computed<FrameSource[]>(() => {
  const sources: FrameSource[] = []
  const s = shot.value
  if (!s) return sources

  const isTransition = s.kind === 'transition'

  // 上游镜头：如果有，强制显示在最上面
  if (s.prev_shot_id) {
    const prevItem = bill.value?.items.find(
      (i) => i.kind === 'prev_frame' && i.role === 'first_frame',
    )
    sources.push({
      type: 'upstream',
      label: '上游镜头',
      hint: prevItem
        ? `从上游镜头的末帧继承（tail_frame 衔接）。${isTransition ? '转场镜头自动确定，不能手动改。' : '要用自己的首帧就先断开上游。'}`
        : '上游镜头还未生成，等它出片后会自动抽末帧。',
      assetId: prevItem?.asset_id ?? null,
      path: prevItem?.asset_path ?? null,
      disabled: true, // 上游来源不可修改
    })
  }

  // 显式槽位：有上游或转场时禁用
  sources.push({
    type: 'slot',
    label: s.prev_shot_id || isTransition ? '首帧（自动确定）' : '首帧',
    hint: isTransition
      ? '转场镜头的首帧来自上游镜头末帧，自动确定，不能手动设置。'
      : s.prev_shot_id
        ? '有上游镜头时首帧强制从上游末帧来，这个槽位被禁用。要用自己的首帧就先断开上游。'
        : '画面的第一格。留空就是不指定首帧，模型自己起画。',
    assetId: s.first_frame_asset_id ?? null,
    path: s.first_frame_path ?? null,
    disabled: !!s.prev_shot_id || isTransition,
    key: 'first_frame_asset_id',
  })

  return sources
})

/**
 * 末帧槽位（单独一个，逻辑简单）。
 * **转场镜头末帧不能手动设置**——末帧来自下游镜头首帧，自动确定。
 */
const lastFrameSlot = computed<FrameSource | null>(() => {
  const s = shot.value
  if (!s) return null
  const isTransition = s.kind === 'transition'
  return {
    type: 'slot',
    label: isTransition ? '末帧（自动确定）' : '末帧',
    hint: isTransition
      ? '转场镜头的末帧来自下游镜头首帧，自动确定，不能手动设置。'
      : '画面的最后一格，只有首尾帧能力的模型用得上。',
    assetId: s.last_frame_asset_id ?? null,
    path: s.last_frame_path ?? null,
    disabled: isTransition,
    key: 'last_frame_asset_id',
  }
})

/**
 * 上下文账单中的参考素材（排除首尾帧，避免重复显示）。
 * 首尾帧已经在上面的槽位区显示了，账单里只列参考素材。
 * **转场镜头不显示参考素材**——只要首尾帧，加角色表 / 地点图只会让画面跑偏。
 */
const referenceMaterials = computed(() => {
  if (shot.value?.kind === 'transition') {
    return []
  }
  return editor.included.filter((item) => item.role === 'reference')
})
const castIds = computed(() => new Set((shot.value?.cast ?? []).map((c) => c.appearance_id)))
const propState = computed(
  () => new Map((shot.value?.props ?? []).map((p) => [p.prop_id, p.state])),
)
/** 同一件事只报一次（后端重启后两边都会 404「项目未打开」）。 */
const showSideError = computed(
  () => sideError.value !== null && sideError.value.code !== editor.lastError?.code,
)

/** 分镜板拉平成一张镜头清单，供顶部下拉在镜头之间跳。 */
const allShots = computed(() =>
  story.lanes.flatMap((l) =>
    l.shots
      .filter((s) => s.kind !== 'transition')
      .map((s) => ({ id: s.id, label: `${s.index_no}. ${s.title} · ${l.title}`, prev_shot_id: s.prev_shot_id })),
  ),
)

const upstreamCandidates = computed(() => {
  const current = shot.value?.id
  if (!current) return []
  const byId = new Map(allShots.value.map((s) => [s.id, s]))
  return allShots.value.filter((candidate) => {
    if (candidate.id === current) return false
    const seen = new Set<string>()
    let node: string | null = candidate.id
    while (node) {
      if (node === current || seen.has(node)) return false
      seen.add(node)
      node = byId.get(node)?.prev_shot_id ?? null
    }
    return true
  })
})

const transitionPeers = computed(() => {
  const current = shot.value
  if (!current || current.kind !== 'transition') return null
  for (let li = 0; li < story.lanes.length; li += 1) {
    const lane = story.lanes[li]
    if (!lane) continue
    const at = lane.shots.findIndex((s) => s.id === current.id)
    if (at < 0) continue
    const beforeRows = lane.shots.slice(0, at).filter((s) => s.kind !== 'transition')
    const before = beforeRows[beforeRows.length - 1]
    const after = lane.shots.slice(at + 1).find((s) => s.kind !== 'transition')
    const nextLane = story.lanes[li + 1]
    const next = after ?? nextLane?.shots.find((s) => s.kind !== 'transition')
    return { before: before?.title ?? '上游镜头', after: next?.title ?? '下游镜头' }
  }
  return null
})

/**
 * 账单条目那一格的 URL。**用条目自己带的 `asset_path`**，不去资产总账里查——
 * 上游末帧是抽出来的临时帧（`cache/frames/`），它压根不在总账里（`TRANSIENT_KINDS`），
 * 按 id 查只会得到一个空白格。
 */
function itemUrl(item: ContextItem): string {
  return item.asset_path && !item.missing_file ? fileUrl(pid.value, item.asset_path) : ''
}

/** 图片走 `<img>`、视频走 `<video>`、音频走 `<audio>`：把 `.mp4` 塞进 `<img>` 只会得到坏图标。 */
function mediaOf(item: ContextItem): string {
  return item.media ?? 'image'
}

/** 槽位缩略图。资产被删掉时后端给 null，此时要显示「指定的图已不在」而不是空白。 */
function slotUrl(path: string | null): string {
  return path ? fileUrl(pid.value, path) : ''
}

/**
 * 版本轨上那一格：**视频走 `<video>`、图片走 `<img>`**。两个字段由后端分开给
 * （`generation._version_media`），因为版本的资产几乎总是一段 `.mp4`——
 * 把它塞进 `<img>` 只会得到一个坏图标。
 *
 * 地址是光秃秃的文件地址，**不带 `#t=in,out`**：长视频切出来的版本共用一个源文件，
 * 区间交给 `SegmentPlayer`（它的进度条只有本段）。原生进度条量的是整个文件，
 * 那样得到的是「限制了播放时间的长片」而不是单段预览。
 */
function versionVideo(v: GenerationVersion): string {
  return v.video_path ? fileUrl(pid.value, v.video_path) : ''
}

function versionPoster(v: GenerationVersion): string {
  return v.thumbnail_path ? fileUrl(pid.value, v.thumbnail_path) : ''
}

const currentLane = computed(() => {
  if (!editor.shot) return null
  return story.lanes.find((l) => l.id === editor.shot?.scene_id) ?? null
})

async function promoteShotPromptToScene(): Promise<void> {
  const promptVal = editor.shot?.prompt
  const lane = currentLane.value
  if (!promptVal || !lane || !pid.value) return
  await story.updateScene(pid.value, lane.id, { prompt: promptVal }).catch(() => {})
  await story.loadBoard(pid.value).catch(() => {})
}

async function clearShotPromptToInherit(): Promise<void> {
  if (!pid.value) return
  await editor.save(pid.value, { prompt: '' }).catch(() => {})
  await reload()
}

async function loadSide(): Promise<void> {
  if (!pid.value) return
  try {
    const [chars, propRows, assetRows] = await Promise.all([
      castApi.characters(pid.value),
      worldApi.props(pid.value),
      assetsApi.list(pid.value),
    ])
    const nested = await Promise.all(
      chars.map(async (c) => ({ c, rows: await castApi.appearances(pid.value, c.id) })),
    )
    appearances.value = nested.flatMap(({ c, rows }) => rows.map((row) => ({ row, character: c })))
    props_.value = propRows
    assets.value = assetRows
    sideError.value = null
  } catch (err) {
    sideError.value = err instanceof ApiError ? err : null
  }
}

async function reload(): Promise<void> {
  if (!pid.value) return
  await Promise.all([
    editor.load(pid.value, sid.value).catch(() => {}),
    story.loadBoard(pid.value).catch(() => {}),
    loadSide(),
  ])
}

onMounted(reload)
watch([pid, sid], () => {
  editor.load(pid.value, sid.value).catch(() => {})
})

/** 没带 sid 时落到第一个镜头，URL 也跟着变——刷新后还在同一个镜头上。 */
watch(allShots, (list) => {
  if (!sid.value && list.length) {
    void router.replace({ name: 'shot', params: { pid: pid.value, sid: list[0]?.id } })
  }
})

function goShot(shotId: string): void {
  void router.push({ name: 'shot', params: { pid: pid.value, sid: shotId } })
}

function fmt(n: number | null | undefined): string {
  return n === null || n === undefined ? '—' : `${Math.round(n * 10) / 10}s`
}

async function saveText(key: 'prompt' | 'negative_prompt' | 'description' | 'dialogue', value: string) {
  await editor.save(pid.value, { [key]: value || null }).catch(() => {})
}

const versionTab = ref<'video' | 'audio'>('video')
const dubModalOpen = ref(false)
const refineModalOpen = ref(false)
const refineVersionId = ref<string | undefined>(undefined)
const splitDialogOpen = ref(false)
const splitAtSeconds = ref(2.0)
const audioInput = ref<HTMLInputElement | null>(null)
const lineageDialogOpen = ref(false)
const lineageResult = ref<VersionLineageResult | null>(null)

function audioUrl(v: AudioVersionItem): string {
  return v.audio_path ? fileUrl(pid.value, v.audio_path) : ''
}

async function onPickAudioFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || !pid.value) return
  uploading.value = true
  sideError.value = null
  try {
    const asset = await assetsApi.upload(pid.value, file, 'audio')
    await editor.importAudio(pid.value, asset.path, true)
    await loadSide()
  } catch (err) {
    sideError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

function openRefine(versionId?: string) {
  refineVersionId.value = versionId
  refineModalOpen.value = true
}

async function inspectLineage(versionId: string) {
  if (!pid.value) return
  try {
    lineageResult.value = await refineApi.lineage(pid.value, versionId)
    lineageDialogOpen.value = true
  } catch (err) {
    sideError.value = err instanceof ApiError ? err : null
  }
}

function openSplitDialog() {
  const dur = shot.value?.duration || 4.0
  splitAtSeconds.value = Math.max(0.5, Math.round((dur / 2) * 10) / 10)
  splitDialogOpen.value = true
}

async function executeSplit() {
  if (!pid.value || !shot.value) return
  try {
    const newShotId = await editor.splitShot(pid.value, splitAtSeconds.value)
    splitDialogOpen.value = false
    await story.loadBoard(pid.value)
    if (newShotId) {
      void router.push({ name: 'shot', params: { pid: pid.value, sid: newShotId } })
    }
  } catch {
    // editor.lastError handles it
  }
}

async function saveNumber(key: 'seed' | 'steps' | 'duration', value: string) {
  const n = Number(value)
  if (value !== '' && !Number.isFinite(n)) return
  await editor.save(pid.value, { [key]: value === '' ? null : n }).catch(() => {})
}

async function saveField(
  key: 'title' | 'camera' | 'movement' | 'status' | 'workflow_id' | 'prev_shot_id',
  value: string,
) {
  const nullable = key !== 'title' && key !== 'status'
  const next = key === 'prev_shot_id' ? value || '' : nullable ? value || null : value
  await editor.save(pid.value, { [key]: next }).catch(() => {})
  if (key === 'prev_shot_id') await story.loadBoard(pid.value).catch(() => {})
}

async function toggleCast(appearanceId: string): Promise<void> {
  const next = new Set(castIds.value)
  if (next.has(appearanceId)) next.delete(appearanceId)
  else next.add(appearanceId)
  await editor.setCast(pid.value, [...next]).catch(() => {})
}

async function setPropState(propId: string, state: string): Promise<void> {
  const items = (shot.value?.props ?? [])
    .filter((p) => p.prop_id !== propId)
    .map((p) => ({ prop_id: p.prop_id, state: p.state }))
  if (state) items.push({ prop_id: propId, state })
  await editor.setProps(pid.value, items).catch(() => {})
}

/**
 * 写一个首 / 末帧槽位。**清空传 `''`**——`null` 会被后端 `exclude_none` 吃掉，等于没改。
 * 挑了视频 / 音频后端会用 422「首帧只能是图片」拦下来，错误面板照常显示建议。
 */
async function setSlot(key: FrameSlotKey, assetId: string): Promise<void> {
  await editor.save(pid.value, { [key]: assetId }).catch(() => {})
}

function pickSlotFile(key: FrameSlotKey): void {
  pickingSlot.value = key
  frameInput.value?.click()
}

/** 上传一张图并直接填进刚才那个槽位。 */
async function onPickFrameFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  uploading.value = true
  try {
    const asset = await assetsApi.upload(pid.value, file, 'upload')
    await loadSide()
    await setSlot(pickingSlot.value, asset.id)
  } catch (err) {
    sideError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

/**
 * 上传一个参考素材并直接挂进上下文（`manual` 优先级最高）。
 * **图 / 视频 / 音频都收**：参考素材要回答的是「谁出场、在哪儿、什么动作、什么声音」，
 * 不只是「长什么样」。认不出后缀的文件后端会列出来但不采用，并写清理由。
 */
async function onPickContextFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  uploading.value = true
  try {
    const asset = await assetsApi.upload(pid.value, file, 'upload')
    await editor.override(pid.value, { action: 'add', asset_id: asset.id, label: file.name })
    await loadSide()
  } catch (err) {
    sideError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

/** 手动导入一个成片版本：不生成也能把工程做完。 */
async function onPickVersionFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  uploading.value = true
  try {
    const video = file.type.startsWith('video')
    // 手动导入的不是生成物，落 assets/uploads 而不是 generations/
    const asset = await assetsApi.upload(pid.value, file, 'upload')
    await editor.addVersion(pid.value, asset.id, video ? 'video' : 'image')
    await loadSide()
  } catch (err) {
    sideError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

async function generate(skipContext: boolean, allowRefDrop = false): Promise<void> {
  const job = await editor.enqueue(pid.value, {
    workflowId: shot.value?.workflow_id ?? null,
    checkContext: !skipContext,
    allowRefDrop,
  })
  pendingDrop.value = confirmFlagOf(editor.lastError) ? { skipContext } : null
  if (job) await story.loadBoard(pid.value).catch(() => {})
}

/** 「知道会丢素材，继续」：把刚才那一次原样重来，只多带一个 `allow_ref_drop`。 */
async function confirmDrop(): Promise<void> {
  const pending = pendingDrop.value
  if (pending) await generate(pending.skipContext, true)
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />
    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1.5 border-b px-2">
      <span class="text-fg-4 text-2xs">镜头</span>
      <select
        :value="sid"
        class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 w-60 border px-1 text-2xs outline-none"
        @change="goShot(($event.target as HTMLSelectElement).value)"
      >
        <option value="">未选择</option>
        <option v-for="s in allShots" :key="s.id" :value="s.id">{{ s.label }}</option>
      </select>
      <AppButton
        size="sm"
        variant="primary"
        :disabled="!shot || editor.busy"
        title="按当前上下文与 Workflow 入队生成一个新版本；旧版本一条都不会被覆盖"
        @click="generate(false)"
      >
        <Sparkles :size="10" />生成
      </AppButton>
      <AppButton
        size="sm"
        :disabled="!shot || editor.busy || (bill?.complete ?? false)"
        title="上下文不完整时仍然入队。这是显式选择：出来的东西可能缺参考，但有时你就是想先看一眼"
        @click="generate(true)"
      >
        <Play :size="10" />跳过检查入队
      </AppButton>
      <AppButton
        size="sm"
        variant="ghost"
        title="在底部控制台的任务框里看它跑到哪了（不用离开这一页）"
        @click="consolePanel.openWith('jobs')"
      >
        <ListVideo :size="10" />任务
      </AppButton>
      <span v-if="editor.lastJob" class="text-fg-4 text-2xs">
        最近入队 {{ editor.lastJob.kind }} · {{ editor.lastJob.status }}
      </span>
      <AppButton
        size="sm"
        variant="ghost"
        class="ml-auto"
        :disabled="editor.busy"
        @click="reload()"
      >
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>

    <ErrorPanel
      v-if="editor.lastError"
      class="mx-2 mt-2"
      :error="editor.lastError"
      @dismiss="editor.clearError()"
    >
      <!-- 参考素材装不下不是失败，是一次确认：这颗按钮把刚才那一次原样重来 -->
      <template #actions>
        <AppButton
          v-if="askDrop"
          size="sm"
          variant="primary"
          :disabled="editor.busy"
          title="按模型端那份图每一族的槽位顺序喂前几个；少喂了哪几个会记进版本参数，事后查得到"
          @click="confirmDrop()"
        >
          <Sparkles :size="10" />知道会丢素材，继续生成
        </AppButton>
      </template>
    </ErrorPanel>
    <ErrorPanel
      v-if="showSideError"
      class="mx-2 mt-2"
      :error="sideError"
      @dismiss="sideError = null"
    />
    <EmptyState
      v-if="!shot"
      class="flex-1"
      title="尚无选中镜头"
      body="上面的下拉里选一个镜头，或先去剧本 / 分镜页建几个。这一页回答的是「这条片段是怎么来的」。"
    />
    <template v-else>
      <div class="flex min-h-0 flex-1 gap-2 p-2">
        <!-- 左：镜头信息 + 出场 -->
        <AppPanel title="镜头信息" class="w-64 shrink-0">
          <div class="space-y-3 p-2">
            <section>
              <p class="text-fg-3 text-2xs tracking-wide uppercase">
                第 {{ shot.scene_index_no }} 场 · {{ shot.scene_title }}
              </p>
              <div class="mt-1 space-y-1">
                <label v-if="shot.kind !== 'transition'" class="block">
                  <span class="text-fg-4 text-2xs">标题</span>
                  <input
                    :value="shot.title"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                    @change="saveField('title', ($event.target as HTMLInputElement).value)"
                  />
                </label>
                <div class="grid grid-cols-2 gap-1">
                  <label class="block">
                    <div class="flex items-center justify-between">
                      <span class="text-fg-4 text-2xs">时长（秒）</span>
                      <button
                        v-if="shot.kind !== 'transition' && (shot.duration || 0) > 0.4"
                        type="button"
                        class="text-accent hover:underline text-2xs flex items-center gap-0.5"
                        title="在此秒数处切为两镜"
                        @click="openSplitDialog"
                      >
                        <Scissors :size="9" />切分
                      </button>
                    </div>
                    <input
                      :value="shot.duration"
                      type="number"
                      min="0.1"
                      step="0.1"
                      class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                      @change="saveNumber('duration', ($event.target as HTMLInputElement).value)"
                    />
                  </label>
                  <label class="block">
                    <span class="text-fg-4 text-2xs">状态</span>
                    <select
                      :value="shot.status"
                      class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                      @change="saveField('status', ($event.target as HTMLSelectElement).value)"
                    >
                      <option v-for="s in SHOT_STATUS" :key="s" :value="s">
                        {{ SHOT_STATUS_LABEL[s] }}
                      </option>
                    </select>
                  </label>
                  <label class="block">
                    <span class="text-fg-4 text-2xs">机位</span>
                    <input
                      :value="shot.camera ?? ''"
                      placeholder="中景 / 特写"
                      class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                      @change="saveField('camera', ($event.target as HTMLInputElement).value)"
                    />
                  </label>
                  <label class="block">
                    <span class="text-fg-4 text-2xs">运镜</span>
                    <input
                      :value="shot.movement ?? ''"
                      placeholder="推 / 摇 / 固定"
                      class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                      @change="saveField('movement', ($event.target as HTMLInputElement).value)"
                    />
                  </label>
                </div>
                <!-- 镜头台词 (Dialogue) -->
                <div v-if="shot.kind !== 'transition'" class="block mt-1">
                  <div class="flex items-center justify-between">
                    <span class="text-fg-4 text-2xs">镜头台词 (Dialogue)</span>
                    <button
                      type="button"
                      class="text-accent hover:underline text-2xs flex items-center gap-0.5"
                      title="为该镜头生成独立 AI 配音"
                      @click="dubModalOpen = true"
                    >
                      <Mic :size="9" />AI 配音
                    </button>
                  </div>
                  <textarea
                    :value="shot.dialogue ?? ''"
                    rows="2"
                    placeholder="角色台词或旁白，AI 配音将基于此生成独立音轨..."
                    class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px w-full resize-none border px-1.5 py-1 text-2xs outline-none"
                    @change="saveText('dialogue', ($event.target as HTMLTextAreaElement).value)"
                  />
                </div>
                <label class="block">
                  <span class="text-fg-4 text-2xs">上游镜头（首尾帧连续性）</span>
                  <select
                    :value="shot.prev_shot_id ?? ''"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                    @change="saveField('prev_shot_id', ($event.target as HTMLSelectElement).value)"
                  >
                    <option value="">不接上游</option>
                    <option
                      v-for="s in upstreamCandidates"
                      :key="s.id"
                      :value="s.id"
                    >
                      {{ s.label }}
                    </option>
                  </select>
                </label>
                <div v-if="shot.kind === 'transition'" class="border-line-1 bg-base-2 border p-1.5">
                  <p class="text-fg-2 text-2xs">转场上下游已固定</p>
                  <p class="text-fg-4 mt-0.5 text-2xs">
                    负责「{{ transitionPeers?.before ?? '上游镜头' }} → {{ transitionPeers?.after ?? '下游镜头' }}」的转场；请回到这两个镜头修改衔接。
                  </p>
                </div>
                <p class="text-fg-4 text-2xs">
                  接了上游，本镜头会等它出当前版本再跑——队列里那条等待会写明原因，不是卡住。
                </p>
              </div>
            </section>

            <!--
              首尾帧和参考素材是两件事：首帧决定「画面从哪一格开始」，末帧决定「结束」，
              账单里的角色表 / 地点图决定「谁出场、在哪儿」。**有上游镜头时首帧强制从
              上游末帧来**，槽位被禁用——要用自己的首帧就先断开上游。
            -->
            <section class="border-line-1 border-t pt-2">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">首帧 / 末帧（只能是图片）</p>
              <div class="mt-1 space-y-2">
                <!-- 首帧：可能有多个来源（上游 + 槽位） -->
                <div v-for="(src, idx) in firstFrameSources" :key="idx" class="min-w-0">
                  <div class="flex items-center gap-1">
                    <AppBadge
                      :tone="src.type === 'upstream' ? 'warn' : src.assetId ? 'ok' : 'neutral'"
                    >
                      {{ src.label }}
                    </AppBadge>
                    <button
                      v-if="src.assetId && src.key && !src.disabled"
                      class="text-fg-4 hover:text-st-failed ml-auto"
                      title="清空这个槽位（不指定这一帧）"
                      @click="setSlot(src.key, '')"
                    >
                      <X :size="10" />
                    </button>
                  </div>
                  <div
                    class="bg-base-3 border-line-1 mt-1 flex h-16 items-center justify-center overflow-hidden border"
                    :class="{ 'opacity-50': src.disabled && !src.assetId }"
                  >
                    <img
                      v-if="slotUrl(src.path)"
                      :src="slotUrl(src.path)"
                      class="max-h-full max-w-full object-contain"
                      :alt="src.label"
                    />
                    <span v-else-if="src.assetId" class="text-st-failed px-1 text-center text-2xs">
                      指定的图已不在
                    </span>
                    <span v-else class="text-fg-4 text-2xs">
                      {{ src.type === 'upstream' ? '等上游生成' : '未指定' }}
                    </span>
                  </div>
                  <select
                    v-if="src.key"
                    :value="src.assetId ?? ''"
                    :disabled="src.disabled || uploading || editor.busy"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 disabled:opacity-50 mt-1 h-5 w-full border px-1 text-2xs outline-none"
                    :title="src.hint"
                    @change="setSlot(src.key!, ($event.target as HTMLSelectElement).value)"
                  >
                    <option value="">不指定</option>
                    <option v-for="a in imageAssets" :key="a.id" :value="a.id">
                      {{ a.path.split('/').pop() }}
                    </option>
                  </select>
                  <AppButton
                    v-if="src.key"
                    size="sm"
                    variant="ghost"
                    class="mt-1 w-full"
                    :disabled="src.disabled || uploading || editor.busy"
                    :title="src.disabled ? src.hint : '上传一张图直接填进这个槽位'"
                    @click="pickSlotFile(src.key!)"
                  >
                    <Upload :size="10" />上传
                  </AppButton>
                  <p v-if="src.type === 'upstream'" class="text-fg-4 mt-1 text-2xs">
                    {{ src.hint }}
                  </p>
                </div>

                <!-- 末帧：单独一个槽位 -->
                <div v-if="lastFrameSlot" class="min-w-0">
                  <div class="flex items-center gap-1">
                    <AppBadge :tone="lastFrameSlot.assetId ? 'ok' : 'neutral'">
                      {{ lastFrameSlot.label }}
                    </AppBadge>
                    <button
                      v-if="lastFrameSlot.assetId && lastFrameSlot.key"
                      class="text-fg-4 hover:text-st-failed ml-auto"
                      title="清空这个槽位（不指定这一帧）"
                      @click="setSlot(lastFrameSlot.key, '')"
                    >
                      <X :size="10" />
                    </button>
                  </div>
                  <div
                    class="bg-base-3 border-line-1 mt-1 flex h-16 items-center justify-center overflow-hidden border"
                  >
                    <img
                      v-if="slotUrl(lastFrameSlot.path)"
                      :src="slotUrl(lastFrameSlot.path)"
                      class="max-h-full max-w-full object-contain"
                      :alt="lastFrameSlot.label"
                    />
                    <span
                      v-else-if="lastFrameSlot.assetId"
                      class="text-st-failed px-1 text-center text-2xs"
                    >
                      指定的图已不在
                    </span>
                    <span v-else class="text-fg-4 text-2xs">未指定</span>
                  </div>
                  <select
                    :value="lastFrameSlot.assetId ?? ''"
                    :disabled="lastFrameSlot.disabled || uploading || editor.busy"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 disabled:opacity-50 mt-1 h-5 w-full border px-1 text-2xs outline-none"
                    :title="lastFrameSlot.hint"
                    @change="setSlot(lastFrameSlot.key!, ($event.target as HTMLSelectElement).value)"
                  >
                    <option value="">不指定</option>
                    <option v-for="a in imageAssets" :key="a.id" :value="a.id">
                      {{ a.path.split('/').pop() }}
                    </option>
                  </select>
                  <AppButton
                    size="sm"
                    variant="ghost"
                    class="mt-1 w-full"
                    :disabled="lastFrameSlot.disabled || uploading || editor.busy"
                    :title="lastFrameSlot.disabled ? lastFrameSlot.hint : '上传一张图直接填进这个槽位'"
                    @click="pickSlotFile(lastFrameSlot.key!)"
                  >
                    <Upload :size="10" />上传
                  </AppButton>
                </div>
              </div>
              <input
                ref="frameInput"
                type="file"
                accept="image/*"
                class="hidden"
                @change="onPickFrameFile"
              />
              <p class="text-fg-4 mt-1 text-2xs">
                留空就是不指定：账单里的角色表 / 地点图一律是参考素材，不会有一张被当成画面第一格。
                视频 / 音频请在上下文检查器里当参考素材加——这两个入口只收图片。
              </p>
            </section>

            <section class="border-line-1 border-t pt-2">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">出场角色形象</p>
              <p v-if="appearances.length === 0" class="text-fg-4 mt-1 text-2xs">
                还没有角色形象。先去角色页建一个——没有角色的镜头上下文不完整，入队会被拒。
              </p>
              <ul v-else class="mt-1 space-y-px">
                <li v-for="a in appearances" :key="a.row.id">
                  <label class="hover:bg-base-2 flex items-center gap-1 px-0.5 py-0.5">
                    <input
                      type="checkbox"
                      :checked="castIds.has(a.row.id)"
                      class="accent-accent"
                      @change="toggleCast(a.row.id)"
                    />
                    <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">
                      {{ a.character.name }} · {{ a.row.name }}
                    </span>
                    <AppBadge
                      v-if="!a.row.current_sheet"
                      tone="warn"
                      title="这个形象还没有角色表，进不了上下文"
                    >
                      无角色表
                    </AppBadge>
                  </label>
                </li>
              </ul>
            </section>

            <section class="border-line-1 border-t pt-2">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">道具</p>
              <p v-if="props_.length === 0" class="text-fg-4 mt-1 text-2xs">还没有道具。</p>
              <ul v-else class="mt-1 space-y-px">
                <li v-for="p in props_" :key="p.id" class="flex items-center gap-1">
                  <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">{{ p.name }}</span>
                  <select
                    :value="propState.get(p.id) ?? ''"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 w-20 border px-1 text-2xs outline-none"
                    @change="setPropState(p.id, ($event.target as HTMLSelectElement).value)"
                  >
                    <option value="">不出场</option>
                    <option value="present">在场</option>
                    <option value="discarded">已丢弃</option>
                  </select>
                </li>
              </ul>
              <p class="text-fg-4 mt-1 text-2xs">
                标成「已丢弃」的道具不会进上下文——连续性检查靠它判断伞是什么时候没的。
              </p>
            </section>
          </div>
        </AppPanel>

        <!-- 中：上下文账单。没被采用的照样列出来，理由写在旁边 -->
        <AppPanel title="上下文检查器" class="min-w-0 flex-1">
          <template #actions>
            <span v-if="cap" class="text-fg-4 tnum text-2xs" :title="cap.detail">
              {{ capText }}
            </span>
            <AppBadge v-if="cap?.over" tone="warn" :title="dropText">
              会丢 {{ capBlocks.reduce((n, b) => n + b.dropped, 0) }} 个
            </AppBadge>
            <AppButton
              size="sm"
              variant="ghost"
              :disabled="shot?.kind === 'transition' || uploading || editor.busy"
              :title="shot?.kind === 'transition' ? '转场镜头首尾帧固定，不接受参考素材' : '上传图 / 视频 / 音频直接挂进上下文，优先级最高（手动添加）'"
              @click="contextInput?.click()"
            >
              <Upload :size="10" />加素材
            </AppButton>
            <AppButton
              size="sm"
              variant="ghost"
              :disabled="editor.busy"
              title="丢掉所有人工干预，回到自动解析的结果"
              @click="editor.override(pid, { action: 'reset' }).catch(() => {})"
            >
              <RotateCcw :size="10" />恢复自动
            </AppButton>
            <input
              ref="contextInput"
              type="file"
              accept="image/*,video/*,audio/*"
              class="hidden"
              @change="onPickContextFile"
            />
          </template>
          <div class="min-h-0 flex-1 overflow-auto p-2">
            <div
              v-if="bill && !bill.complete"
              class="border-st-failed/40 bg-base-2 mb-2 border p-1.5"
            >
              <p class="text-st-review text-2xs">上下文不完整，直接「生成」会被后端拒掉：</p>
              <ul class="text-fg-2 mt-0.5 space-y-px text-2xs">
                <li v-for="p in bill.problems" :key="p">· {{ p }}</li>
              </ul>
            </div>
            <p v-else-if="bill" class="text-st-done mb-2 text-2xs">上下文完整。</p>
            <!-- 装不下不是 blocker：生成前会先问一句，确认了照样能生成。按族分开说 -->
            <div v-if="cap?.over" class="border-st-review/40 bg-base-2 mb-2 border p-1.5">
              <p
                v-for="b in capBlocks.filter((x) => x.over)"
                :key="b.media"
                class="text-st-review text-2xs"
              >
                {{ b.label }}采用了 {{ b.ref_count }} 个，当前预设只支持 {{ b.limit }} 个，超出的
                {{ b.dropped }} 个会被丢弃：{{ b.dropped_labels.join('、') }}
              </p>
              <p class="text-fg-4 mt-0.5 text-2xs">
                {{ cap.detail }}可以移除下方列表中不重要的素材，或更换支持更多槽位的预设。
              </p>
            </div>

            <p class="text-fg-3 text-2xs tracking-wide uppercase">
              参考素材（{{ referenceMaterials.length }}）
            </p>
            <p v-if="shot?.kind === 'transition'" class="text-fg-4 mt-1 text-2xs">
              转场镜头只要首尾帧，不需要参考素材——加角色表 / 地点图只会让两帧之间的过渡跑偏。
            </p>
            <p v-else class="text-fg-4 mt-1 text-2xs">
              首帧 / 末帧已在上面的槽位区显示，这里只列参考素材。
            </p>
            <EmptyState
              v-if="referenceMaterials.length === 0 && shot?.kind !== 'transition'"
              title="一条参考都没有"
              body="给镜头挂上出场角色、给所在场景选一个地点变体，或者直接「加素材」——账单会立刻重算。"
            />
            <ul v-else class="mt-1 grid grid-cols-3 gap-1.5">
              <li
                v-for="item in referenceMaterials"
                :key="item.key"
                class="border-line-1 bg-base-2 border"
              >
                <!-- 媒体各走各的标签：图 <img>、视频 <video>、音频 <audio> -->
                <div class="bg-base-3 flex h-20 items-center justify-center overflow-hidden">
                  <template v-if="itemUrl(item)">
                    <img
                      v-if="mediaOf(item) === 'image'"
                      :src="itemUrl(item)"
                      class="max-h-full max-w-full object-contain"
                      :alt="item.label"
                    />
                    <video
                      v-else-if="mediaOf(item) === 'video'"
                      :src="itemUrl(item)"
                      controls
                      preload="metadata"
                      class="max-h-full max-w-full"
                    />
                    <audio
                      v-else-if="mediaOf(item) === 'audio'"
                      :src="itemUrl(item)"
                      controls
                      preload="metadata"
                      class="w-full px-1"
                    />
                    <span v-else class="text-fg-4 text-2xs">无法预览</span>
                  </template>
                  <span v-else class="text-fg-4 text-2xs">
                    {{ item.missing_file ? '文件不在磁盘上' : '无图' }}
                  </span>
                </div>
                <div class="p-1">
                  <div class="flex items-center gap-1">
                    <!-- 参考素材不只有图：这一族决定它进哪一组槽位 -->
                    <AppBadge tone="accent" title="参考素材：决定谁出场 / 在哪儿 / 什么动作">
                      {{ CONTEXT_MEDIA_LABEL[mediaOf(item)] ?? mediaOf(item) }}
                    </AppBadge>
                    <AppBadge tone="neutral">
                      {{ CONTEXT_KIND_LABEL[item.kind] ?? item.kind }}
                    </AppBadge>
                    <AppBadge v-if="item.manual" tone="warn">人工</AppBadge>
                    <!-- 采用了、但模型端那份图收不下它。和「未采用」是两件事 -->
                    <AppBadge
                      v-if="item.over_capacity"
                      tone="fail"
                      title="这一族槽位不够，提交时这一个会被挤掉——生成前会先问一次"
                    >
                      装不下
                    </AppBadge>
                    <span class="text-fg-4 tnum ml-auto text-2xs">P{{ item.priority }}</span>
                    <button
                      class="text-fg-4 hover:text-st-failed"
                      title="从这次上下文里移除（记成人工覆写，可「恢复自动」撤销）"
                      @click="
                        editor.override(pid, { action: 'remove', key: item.key }).catch(() => {})
                      "
                    >
                      <X :size="10" />
                    </button>
                  </div>
                  <p class="text-fg-2 mt-0.5 truncate text-2xs" :title="item.label">
                    {{ item.label }}
                  </p>
                  <p class="text-fg-4 truncate text-2xs" :title="item.reason">{{ item.reason }}</p>
                </div>
              </li>
            </ul>

            <p class="text-fg-3 mt-3 text-2xs tracking-wide uppercase">
              未采用（{{ editor.omitted.length }}）
            </p>
            <p v-if="editor.omitted.length === 0" class="text-fg-4 mt-1 text-2xs">
              没有被省略的条目。
            </p>
            <ul v-else class="mt-1 space-y-px">
              <li
                v-for="item in editor.omitted"
                :key="item.key"
                class="border-line-1 hover:bg-base-2 flex items-center gap-1.5 border px-1 py-0.5 opacity-70"
              >
                <AppBadge tone="neutral">
                  {{ CONTEXT_KIND_LABEL[item.kind] ?? item.kind }}
                </AppBadge>
                <AppBadge v-if="item.media && item.media !== 'image'" tone="neutral">
                  {{ CONTEXT_MEDIA_LABEL[item.media] ?? item.media }}
                </AppBadge>
                <span class="text-fg-2 min-w-0 shrink-0 truncate text-2xs">{{ item.label }}</span>
                <span class="text-fg-4 min-w-0 flex-1 truncate text-2xs" :title="item.reason">
                  {{ item.reason }}
                </span>
                <AppBadge v-if="item.missing_file" tone="fail">文件丢失</AppBadge>
              </li>
            </ul>
            <p class="text-fg-4 mt-2 text-2xs">
              没被采用的也列在这儿：「为什么这张角色表没进去」比「进去了哪几张」更常是要问的问题。
            </p>
          </div>
        </AppPanel>
        <!-- 右：版本轨（画面 + 独立音轨双轨）。只增不改 -->
        <AppPanel title="版本与音轨" class="w-64 shrink-0">
          <template #actions>
            <div class="flex items-center gap-1">
              <AppButton
                v-if="versionTab === 'video'"
                size="sm"
                variant="ghost"
                :disabled="uploading || editor.busy"
                title="手动导入一个成片版本：不接 AI 也能把工程做完"
                @click="versionInput?.click()"
              >
                <Upload :size="10" />导入
              </AppButton>
              <AppButton
                v-else
                size="sm"
                variant="ghost"
                :disabled="uploading || editor.busy"
                title="导入外部音频作为该镜头的独立音轨"
                @click="audioInput?.click()"
              >
                <Upload :size="10" />导入音频
              </AppButton>
              <input
                ref="versionInput"
                type="file"
                accept="image/*,video/*"
                class="hidden"
                @change="onPickVersionFile"
              />
              <input
                ref="audioInput"
                type="file"
                accept="audio/*"
                class="hidden"
                @change="onPickAudioFile"
              />
            </div>
          </template>

          <!-- 画面 / 音频 Tab 切换 -->
          <div class="flex items-center border-line-1 border-b bg-base-2 text-2xs">
            <button
              class="flex-1 py-1.5 font-medium text-center transition-colors border-r border-line-1"
              :class="versionTab === 'video' ? 'bg-base-1 text-accent' : 'text-fg-4 hover:text-fg-2'"
              @click="versionTab = 'video'"
            >
              画面版本 ({{ editor.versions.length }})
            </button>
            <button
              class="flex-1 py-1.5 font-medium text-center transition-colors"
              :class="versionTab === 'audio' ? 'bg-base-1 text-accent' : 'text-fg-4 hover:text-fg-2'"
              @click="versionTab = 'audio'"
            >
              独立音轨 ({{ editor.audioVersions.length }})
            </button>
          </div>

          <div class="p-2">
            <!-- 画面版本 Tab -->
            <div v-if="versionTab === 'video'">
              <EmptyState
                v-if="editor.versions.length === 0"
                title="还没有任何画面版本"
                body="「生成」入队一个，或者「导入」一个已有的成片。版本只增不改，旧的一条都不会被覆盖。"
              />
              <ul v-else class="space-y-1.5">
                <li
                  v-for="v in editor.versions"
                  :key="v.id"
                  class="border p-1.5"
                  :class="
                    v.is_current
                      ? 'border-accent/60 bg-accent-dim/40'
                      : 'border-line-1 bg-base-2 hover:bg-base-3'
                  "
                >
                  <div class="flex items-center gap-1">
                    <span class="text-fg-1 tnum text-2xs font-medium">v{{ v.version_no }}</span>
                    <AppBadge :tone="v.source === 'manual' ? 'neutral' : 'accent'">
                      {{ v.source === 'manual' ? '手动' : v.source === 'imported' ? '导入' : v.source === 'upscaled' ? '超分' : v.source === 'interpolated' ? '插帧' : '生成' }}
                    </AppBadge>
                    <AppBadge v-if="v.status !== 'done'" tone="warn">{{ v.status }}</AppBadge>
                    <div class="ml-auto flex items-center gap-1">
                      <button
                        v-if="v.parent_version_id"
                        type="button"
                        class="text-fg-4 hover:text-accent p-0.5"
                        title="查看版本谱系衍生关系"
                        @click="inspectLineage(v.id)"
                      >
                        <GitBranch :size="10" />
                      </button>
                      <button
                        type="button"
                        class="text-fg-4 hover:text-accent p-0.5"
                        title="对此版本进行超分/插帧优化"
                        @click="openRefine(v.id)"
                      >
                        <Zap :size="10" />
                      </button>
                      <button
                        v-if="!v.is_current"
                        class="text-fg-4 hover:text-accent p-0.5"
                        title="设为当前版本（下游镜头取末帧、时间线取片段都用它）"
                        @click="editor.setCurrent(pid, v.id).catch(() => {})"
                      >
                        <Star :size="10" />
                      </button>
                      <Star v-else :size="10" class="text-accent" />
                    </div>
                  </div>

                  <!-- 切段区间标识 -->
                  <div v-if="v.in_point != null || v.out_point != null" class="mt-1 text-2xs text-accent flex items-center gap-1 font-mono">
                    <Scissors :size="9" />
                    <span>区间: {{ v.in_point ?? 0 }}s ~ {{ v.out_point ?? v.duration }}s</span>
                  </div>

                  <!-- 视频给单段播放器，图片才走 <img>：两个字段绝不混用 -->
                  <!-- 播放器自带一条「只有本段」的进度条，所以它比一格缩略图高一点 -->
                  <SegmentPlayer
                    v-if="versionVideo(v)"
                    :key="versionVideo(v)"
                    class="border-line-1 bg-base-3 mt-1 border"
                    :src="versionVideo(v)"
                    :in-point="v.in_point"
                    :out-point="v.out_point"
                    :poster="versionPoster(v)"
                  />
                  <div
                    v-else-if="versionPoster(v)"
                    class="bg-base-3 mt-1 flex h-20 items-center justify-center overflow-hidden"
                  >
                    <img
                      :src="versionPoster(v)"
                      class="max-h-full max-w-full object-contain"
                      :alt="`v${v.version_no}`"
                    />
                  </div>
                  <p class="text-fg-4 mt-0.5 text-2xs">
                    {{ v.kind }} · {{ fmt(v.duration) }} · {{ v.created_at.slice(0, 16) }}
                  </p>
                  <p v-if="v.error" class="text-st-review mt-0.5 text-2xs">这个版本是失败现场</p>
                </li>
              </ul>
            </div>

            <!-- 独立音轨 Tab -->
            <div v-else class="space-y-2">
              <div class="border-line-1 bg-base-2 p-1.5 border text-2xs flex items-center justify-between">
                <span class="text-fg-3">
                  {{ editor.currentAudioVersion ? '当前：使用独立配音轨' : '当前：使用画面原生声音' }}
                </span>
                <button
                  v-if="editor.currentAudioVersion"
                  type="button"
                  class="text-accent hover:underline flex items-center gap-0.5"
                  title="恢复为画面原声音轨"
                  @click="editor.muteAudio(pid)"
                >
                  <VolumeX :size="10" />恢复原声
                </button>
              </div>

              <EmptyState
                v-if="editor.audioVersions.length === 0"
                title="暂无独立配音轨"
                body="可点上方「导入音频」或使用左侧「AI 配音」为本镜头生成专属旁白/台词音轨。"
              />
              <ul v-else class="space-y-1.5">
                <li
                  v-for="av in editor.audioVersions"
                  :key="av.id"
                  class="border p-1.5"
                  :class="
                    av.is_current
                      ? 'border-accent/60 bg-accent-dim/40'
                      : 'border-line-1 bg-base-2 hover:bg-base-3'
                  "
                >
                  <div class="flex items-center gap-1">
                    <span class="text-fg-1 tnum text-2xs font-medium">v{{ av.version_no }}</span>
                    <AppBadge :tone="av.source === 'imported' ? 'neutral' : 'accent'">
                      {{ av.source === 'imported' ? '导入音频' : 'AI 配音' }}
                    </AppBadge>
                    <button
                      v-if="!av.is_current"
                      class="text-fg-4 hover:text-accent ml-auto p-0.5"
                      title="采纳为当前镜头的独立音轨"
                      @click="editor.setCurrent(pid, av.id).catch(() => {})"
                    >
                      <Star :size="10" />
                    </button>
                    <Star v-else :size="10" class="text-accent ml-auto" />
                  </div>

                  <div v-if="audioUrl(av)" class="mt-1">
                    <audio :src="audioUrl(av)" controls preload="metadata" class="w-full h-6" />
                  </div>
                  <p class="text-fg-4 mt-0.5 text-2xs">
                    {{ fmt(av.duration) }} · {{ av.created_at.slice(0, 16) }}
                  </p>
                </li>
              </ul>
            </div>
          </div>
        </AppPanel>
      </div>
      <!-- 底：prompt 与参数。这些值会在入队那一刻被冻结进版本里 -->
      <div class="border-line-1 bg-base-1 shrink-0 border-t p-2">
        <div class="flex gap-2">
          <label class="min-w-0 flex-1 block space-y-0.5">
            <div class="flex items-center justify-between">
              <span class="text-fg-4 text-2xs font-medium">Prompt</span>
              <div class="flex items-center gap-2">
                <AppBadge v-if="shot.prompt" tone="warn">当前 Shot 专属覆盖</AppBadge>
                <AppBadge v-else-if="currentLane?.prompt" tone="accent">继承本幕共用</AppBadge>
                <button
                  v-if="shot.prompt && currentLane"
                  type="button"
                  class="text-accent text-3xs hover:underline flex items-center gap-0.5 cursor-pointer"
                  title="将此 Prompt 同步为本幕所有镜头的共用 Prompt"
                  @click.prevent="promoteShotPromptToScene"
                >
                  <Sparkles :size="9" />设为本幕共用
                </button>
                <button
                  v-if="shot.prompt && currentLane?.prompt"
                  type="button"
                  class="text-fg-4 text-3xs hover:text-fg-2 cursor-pointer"
                  title="清空此 Shot 的独立 Prompt，恢复为继承本幕共用 Prompt"
                  @click.prevent="clearShotPromptToInherit"
                >
                  清空以恢复继承
                </button>
              </div>
            </div>
            <textarea
              :value="shot.prompt ?? ''"
              rows="3"
              :placeholder="currentLane?.prompt ? `（留空继承本幕共用 Prompt: ${currentLane.prompt}）` : '这条镜头要画什么。上下文里的参考素材（图 / 视频 / 音频）会和它一起喂给模型。'"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px w-full resize-none border px-1.5 py-1 text-2xs outline-none font-mono"
              @change="saveText('prompt', ($event.target as HTMLTextAreaElement).value)"
            />
          </label>
          <label class="min-w-0 flex-1">
            <span class="text-fg-4 text-2xs">Negative Prompt</span>
            <textarea
              :value="shot.negative_prompt ?? ''"
              rows="3"
              placeholder="不要出现的东西"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px w-full resize-none border px-1.5 py-1 text-2xs outline-none"
              @change="saveText('negative_prompt', ($event.target as HTMLTextAreaElement).value)"
            />
          </label>
          <div class="w-64 shrink-0 space-y-1">
            <label class="block"> </label>
            <div class="grid grid-cols-2 gap-1">
              <label class="block">
                <span class="text-fg-4 text-2xs">Seed（空 = 随机）</span>
                <input
                  :value="shot.seed ?? ''"
                  type="number"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveNumber('seed', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">Steps</span>
                <input
                  :value="shot.steps ?? ''"
                  type="number"
                  min="1"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveNumber('steps', ($event.target as HTMLInputElement).value)"
                />
              </label>
            </div>
            <p class="text-fg-4 text-2xs">
              项目概览里绑定的预设 Workflow
              决定本镜头实际使用的模型图；入队那一刻参数会被冻结进版本。
            </p>
          </div>
          <label class="w-56 shrink-0">
            <span class="text-fg-4 text-2xs">镜头描述（给人看的，不进 prompt）</span>
            <textarea
              :value="shot.description ?? ''"
              rows="3"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px w-full resize-none border px-1.5 py-1 text-2xs outline-none"
              @change="saveText('description', ($event.target as HTMLTextAreaElement).value)"
            />
          </label>
        </div>
      </div>

      <!-- 单镜头 AI 配音弹窗 -->
      <DubModal
        v-model:open="dubModalOpen"
        :pid="pid"
        :shot-id="shot.id"
        :initial-dialogue="shot.dialogue || ''"
        @done="reload()"
      />

      <!-- 二次超分 / 插帧优化弹窗 -->
      <RefineModal
        v-model:open="refineModalOpen"
        :pid="pid"
        :version-id="refineVersionId"
        :shot-id="shot.id"
        @done="reload()"
      />

      <!-- 镜头拆分对话框 -->
      <AppDialog
        :open="splitDialogOpen"
        title="拆分镜头与区间"
        subtitle="将当前镜头及对应源片区间在指定秒数处截断为两个独立镜头"
        size="sm"
        @update:open="splitDialogOpen = $event"
      >
        <div class="p-3 space-y-3">
          <label class="block">
            <span class="text-fg-3 text-2xs font-medium">
              拆分点（秒数：0.1 ~ {{ ((shot?.duration || 1) - 0.1).toFixed(1) }}s）
            </span>
            <input
              v-model.number="splitAtSeconds"
              type="number"
              min="0.1"
              :max="(shot?.duration || 1) - 0.1"
              step="0.1"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-1 h-7 w-full border px-2 text-xs outline-none"
            />
          </label>
          <div class="border-line-1 bg-base-2 p-2 border text-2xs space-y-1">
            <div class="flex justify-between text-fg-2">
              <span>前半段 (保留在当前镜):</span>
              <strong class="text-fg-1">{{ splitAtSeconds }}s</strong>
            </div>
            <div class="flex justify-between text-fg-2">
              <span>后半段 (作为新镜紧随其后):</span>
              <strong class="text-fg-1">{{ ((shot?.duration || 0) - splitAtSeconds).toFixed(1) }}s</strong>
            </div>
          </div>
          <p class="text-fg-4 text-2xs">
            若当前镜头包含长视频区间版本，后半段镜头将自动派生出继承源视频资产的新版本，并精准截取后半段区间（零文件复制）。
          </p>
        </div>
        <template #footer>
          <div class="ml-auto flex items-center gap-2">
            <AppButton size="sm" variant="ghost" @click="splitDialogOpen = false">取消</AppButton>
            <AppButton
              size="sm"
              variant="primary"
              :disabled="editor.busy || splitAtSeconds <= 0 || splitAtSeconds >= (shot?.duration || 0)"
              @click="executeSplit"
            >
              <Scissors :size="10" />确认拆分
            </AppButton>
          </div>
        </template>
      </AppDialog>

      <!-- 版本衍生谱系查看对话框 -->
      <AppDialog
        :open="lineageDialogOpen"
        title="版本衍生谱系"
        subtitle="查看该版本的祖先与下游派生版本链"
        size="sm"
        @update:open="lineageDialogOpen = $event"
      >
        <div v-if="lineageResult" class="p-3 space-y-2 text-2xs">
          <p class="text-fg-2 font-medium">祖先版本链（溯源）：</p>
          <div class="space-y-1 border-line-1 bg-base-2 p-2 border">
            <div
              v-for="a in lineageResult.ancestors"
              :key="a.id"
              class="flex items-center justify-between"
            >
              <span class="text-fg-1">v{{ a.version_no }} ({{ a.source }})</span>
              <AppBadge :tone="a.id === lineageResult.version_id ? 'accent' : 'neutral'">
                {{ a.id === lineageResult.version_id ? '当前查看版本' : '祖先' }}
              </AppBadge>
            </div>
          </div>
          <p v-if="lineageResult.children.length" class="text-fg-2 font-medium mt-2">
            下游衍生版本：
          </p>
          <div v-if="lineageResult.children.length" class="space-y-1 border-line-1 bg-base-2 p-2 border">
            <div
              v-for="c in lineageResult.children"
              :key="c.id"
              class="flex items-center justify-between"
            >
              <span class="text-fg-1">v{{ c.version_no }} ({{ c.source }})</span>
              <AppBadge tone="neutral">衍生</AppBadge>
            </div>
          </div>
        </div>
        <template #footer>
          <AppButton size="sm" variant="ghost" class="ml-auto" @click="lineageDialogOpen = false">
            关闭
          </AppButton>
        </template>
      </AppDialog>
    </template>
  </div>
</template>
