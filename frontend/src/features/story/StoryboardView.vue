<script setup lang="ts">
/**
 * Shot-first storyboard: Scene remains data context, not an interaction layer.
 *
 * 卡片之间那条线（`lane.links`）与幕与幕之间那条（`lane.next_link`）是这一页的第二种主体：
 *
 *   - **没配过就是无转场**——两个镜头直接硬切，什么都不生成；
 *   - 配成「转场」就会在这两镜之间补一段过渡视频（上一镜真末帧 → 下一镜真首帧），
 *     这是为「能引用设定图的模型做不了严格首尾帧」准备的那条路；
 *   - 配了却还没出片时线上写一行**转场暂未生成**——判断只认后端的 `pending`，
 *     界面不自己拿 `transition_shot_id` 再算一遍（镜头造出来了但任务还在排队，
 *     那仍然是「暂未生成」）；
 *   - **接缝两侧都出片了才补得出来**：转场是把上一镜真末帧接到下一镜真首帧，少一头就无从下手。
 *     那个「生成」能不能点只看后端的 `can_generate`，拦下来的原因写在 `blocked` /
 *     `blocked_how` 上——按钮灰着却不说为什么，和静默失败一样糟。
 *
 * 补出来的转场镜头**不在卡片行里**：它照旧在 `lane.shots` 里（导出顺序与时间线装配靠它），
 * 这里只是把它画到那条线上，免得它混在导演排的戏中间。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChevronLeft,
  ChevronRight,
  Image,
  ListVideo,
  Mic,
  RefreshCw,
  Scissors,
  Sliders,
  Sparkles,
  Trash2,
  User,
  Zap,
} from '@lucide/vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import ConfirmErrorDialog from '@/shared/ui/ConfirmErrorDialog.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import SegmentPlayer from '@/shared/ui/SegmentPlayer.vue'
import IngestVideoModal from './components/IngestVideoModal.vue'
import DubModal from './components/DubModal.vue'
import RefineModal from './components/RefineModal.vue'
import SceneConfigModal from './components/SceneConfigModal.vue'
import { fileUrl } from '@/shared/api/files'
import { ApiError, confirmFlagOf } from '@/shared/api/client'
import { SHOT_STATUS, SHOT_STATUS_LABEL, storyApi, type PosterResult, type ShotStatus, type StoryboardCard, type StoryboardConnector, type StoryboardLane } from '@/shared/api/story'
import { useConsoleStore } from '@/stores/console'
import { useStoryStore } from '@/stores/story'
import { generationApi } from '@/shared/api/generation'
import { LINK_MODES, LINK_MODE_LABEL, SHOT_LINK_MODES, SHOT_LINK_MODE_LABEL, sequenceApi, type TransitionRun } from '@/shared/api/sequence'

const route = useRoute()
const router = useRouter()
const story = useStoryStore()
const consolePanel = useConsoleStore()
const pid = computed(() => String(route.params.pid ?? ''))
const filter = ref<'all' | 'issues' | ShotStatus>('all')
const preview = ref<{
  shotId: string
  title: string
  path: string
  in_point?: number | null
  out_point?: number | null
} | null>(null)

const allCards = computed(() => story.lanes.flatMap((lane) => lane.shots))
/** 导演排的戏。转场是系统按衔接补出来的，不算「一个镜头」，也不进卡片行。 */
const realCards = computed(() => allCards.value.filter((s) => s.kind !== 'transition'))
const total = computed(() => ({
  shots: realCards.value.length,
  transitions: allCards.value.length - realCards.value.length,
  duration: allCards.value.reduce((n, s) => n + s.duration, 0),
  issues: allCards.value.filter((s) => !s.context_ok).length,
}))
const pendingPosters = computed(() => allCards.value.filter((s) => s.poster_pending))
/** 两级衔接里「配了转场却还没出片」的条数。 */
const pendingLinks = computed(() =>
  story.lanes
    .flatMap((lane) => [...lane.links, ...(lane.next_link ? [lane.next_link] : [])])
    .filter((l) => l.pending),
)
/** 其中现在真能补的那些（接缝两侧都已出片），就是一键生成转场这一次要做的。 */
const readyLinks = computed(() => pendingLinks.value.filter((l) => l.can_generate))
/** 其中在等成片的那些。数量要显示出来，否则「按钮灰着」看起来像坏了。 */
const blockedLinks = computed(() => pendingLinks.value.filter((l) => !l.can_generate))
const transitionTitle = computed(() => {
  if (readyLinks.value.length)
    return `补齐 ${readyLinks.value.length} 条已经能补的衔接（镜头之间与幕之间一起）；已经有成片的一条都不重做${blockedLinks.value.length ? `。另外 ${blockedLinks.value.length} 条在等成片，这一次不做` : ''}`
  if (blockedLinks.value.length)
    return `${blockedLinks.value.length} 条衔接配了转场，但接缝两侧还没都生成过视频——${blockedLinks.value[0]?.blocked_how ?? '先把这两个镜头各自生成出来'}`
  return '没有配成转场却还没出片的衔接'
})

function realShots(lane: StoryboardLane): StoryboardCard[] {
  return lane.shots.filter((s) => s.kind !== 'transition')
}
function visible(shots: StoryboardCard[]): StoryboardCard[] {
  if (filter.value === 'all') return shots
  if (filter.value === 'issues') return shots.filter((s) => !s.context_ok)
  return shots.filter((s) => s.status === filter.value)
}
/**
 * 卡片 + 卡片右边那条线。筛选把中间某张卡片藏起来时那条线**不画**——
 * 此时屏幕上相邻的两张并不是真的相邻，画出来就是在骗人。
 */
function rows(lane: StoryboardLane): { card: StoryboardCard; link: StoryboardConnector | null }[] {
  const cards = visible(realShots(lane))
  return cards.map((card, at) => {
    const next = cards[at + 1]
    const link = next
      ? (lane.links.find((l) => l.from_shot_id === card.id && l.to_shot_id === next.id) ?? null)
      : null
    return { card, link }
  })
}
function transitionOf(lane: StoryboardLane, link: StoryboardConnector): StoryboardCard | null {
  return link.transition_shot_id
    ? (lane.shots.find((s) => s.id === link.transition_shot_id) ?? null)
    : null
}
function statusTone(status: string): 'neutral' | 'accent' | 'ok' | 'warn' {
  if (status === 'locked') return 'ok'
  if (status === 'review') return 'warn'
  if (status === 'generated' || status === 'ready') return 'accent'
  return 'neutral'
}
function file(path: string | null): string {
  return path ? fileUrl(pid.value, path) : ''
}
function thumb(card: StoryboardCard): string {
  return file(card.thumbnail_path)
}
function fmtDuration(n: number | null | undefined): string {
  return n == null ? '—' : `${Math.round(n * 10) / 10}s`
}

/**
 * 预览地址是**光秃秃的文件地址**：不带 `#t=in,out`。
 * 区间交给 `SegmentPlayer`——片段锚点配上原生进度条量的是整个文件，长视频切出来的
 * 一段在 40 分钟的长片里只有半个像素，那不是「单段预览」而是「限制了播放时间的长片」。
 */
const previewVideoUrl = computed(() => preview.value?.path ?? '')

function previewShot(card: StoryboardCard): void {
  story.selectShot(pid.value, card.id).catch(() => {})
  const version = card.versions.find((v) => v.is_current) ?? card.versions[0]
  if (!version?.video_path) return
  const inPt = version.in_point ?? card.in_point
  const outPt = version.out_point ?? card.out_point
  preview.value = {
    shotId: card.id,
    title: `${card.index_no}. ${card.title} · v${version.version_no}`,
    path: file(version.video_path),
    in_point: inPt,
    out_point: outPt,
  }
}

function previewVersion(card: StoryboardCard, versionId: string): void {
  const version = card.versions.find((v) => v.id === versionId)
  if (version?.video_path) {
    const inPt = version.in_point ?? card.in_point
    const outPt = version.out_point ?? card.out_point
    preview.value = {
      shotId: card.id,
      title: `${card.index_no}. ${card.title} · v${version.version_no}`,
      path: file(version.video_path),
      in_point: inPt,
      out_point: outPt,
    }
  }
}
function openShot(shotId: string): void { void router.push({ name: 'shot', params: { pid: pid.value, sid: shotId } }) }
async function adoptVersion(versionId: string): Promise<void> { await generationApi.setCurrent(pid.value, versionId).catch(() => {}); await reload() }
async function reload(): Promise<void> {
  if (!pid.value) return
  await story.loadBoard(pid.value).catch(() => {})
  // 幕列表换了以后旧的锚点可能已经不在了，回落到第一幕，别让锚点条整条不高亮
  if (!story.lanes.some((l) => l.id === activeLaneId.value)) activeLaneId.value = story.lanes[0]?.id ?? ''
}
onMounted(reload)
watch(pid, reload)
async function moveWithin(laneId: string, shotId: string, delta: number): Promise<void> {
  const lane = story.lanes.find((l) => l.id === laneId); if (!lane) return
  const at = lane.shots.findIndex((s) => s.id === shotId); const to = at + delta
  if (at >= 0 && to >= 0 && to < lane.shots.length) await story.moveShot(pid.value, shotId, laneId, to).catch(() => {})
}
async function removeShot(shotId: string): Promise<void> { await story.removeShot(pid.value, shotId).catch(() => {}) }
async function saveShot(key: 'title' | 'camera' | 'movement' | 'status', value: string): Promise<void> {
  const shotId = story.shot?.id
  if (!shotId) return
  await story.updateShot(pid.value, shotId, { [key]: key === 'title' || key === 'status' ? value : value || null }).catch(() => {})
}
async function saveDuration(value: string): Promise<void> {
  const shotId = story.shot?.id
  const duration = Number(value)
  if (shotId && Number.isFinite(duration) && duration > 0) await story.updateShot(pid.value, shotId, { duration }).catch(() => {})
}

const posterBusy = ref(false)
const posterError = ref<ApiError | null>(null)
const posterResult = ref<PosterResult | null>(null)
async function extractPosters(): Promise<void> {
  if (!pid.value || posterBusy.value) return
  posterBusy.value = true; posterError.value = null
  try { posterResult.value = await storyApi.extractPosters(pid.value); await reload() } catch (err) { posterError.value = err instanceof ApiError ? err : null } finally { posterBusy.value = false }
}
const enqueuing = ref(false)
const enqueueError = ref<ApiError | null>(null)
const enqueueNote = ref('')
const skipped = ref<Array<{ shot_id?: string; index_no?: number; error?: { title?: string; detail?: string } | null }>>([])
const pendingDrop = ref<'shot' | 'scene' | 'transition' | null>(null)
/** 转场那次的结果。跳过的每条形状和 Shot 不一样（认的是衔接 id），所以单独存一份。 */
const transitionRun = ref<TransitionRun | null>(null)
const transitionOnly = ref<string[] | undefined>(undefined)
function resetEnqueue(): void { enqueueError.value = null; enqueueNote.value = ''; skipped.value = []; pendingDrop.value = null; transitionRun.value = null }
async function generateShot(allowRefDrop = false): Promise<void> {
  const shot = story.shot; if (!shot) return
  resetEnqueue(); enqueuing.value = true
  try { const job = await generationApi.enqueueShot(pid.value, shot.id, { allow_ref_drop: allowRefDrop }); enqueueNote.value = `已入队：${shot.index_no}. ${shot.title}（${job.kind}）`; await reload() }
  catch (err) { enqueueError.value = err instanceof ApiError ? err : null; pendingDrop.value = confirmFlagOf(err) ? 'shot' : null }
  finally { enqueuing.value = false }
}
async function generateScene(allowRefDrop = false): Promise<void> {
  const sceneId = story.shot?.scene_id; if (!sceneId) return
  resetEnqueue(); enqueuing.value = true
  try { const out = await generationApi.enqueueScene(pid.value, sceneId, 100, allowRefDrop); enqueueNote.value = `这一场入队：${out.queued.length} / ${out.total} 个 Shot`; skipped.value = out.skipped; await reload() }
  catch (err) { enqueueError.value = err instanceof ApiError ? err : null; pendingDrop.value = confirmFlagOf(err) ? 'scene' : null }
  finally { enqueuing.value = false }
}
async function runSequential(): Promise<void> {
  resetEnqueue(); enqueuing.value = true
  try {
    const plan = await sequenceApi.plan(pid.value, 'sequential')
    const out = await sequenceApi.run(pid.value, 'sequential')
    enqueueNote.value = `单线程续接已入队：${out.queued.length} / ${plan.total_jobs} 个 Shot；下一条会等待上一条产出末帧`
    skipped.value = out.skipped
    await reload()
  } catch (err) {
    enqueueError.value = err instanceof ApiError ? err : null
  } finally { enqueuing.value = false }
}

// --- 卡片之间那条线 ---

const linkBusy = ref('')
function linkKey(link: StoryboardConnector): string { return link.id ?? `${link.from_shot_id ?? link.from_scene_id}` }
/** 改这条线的模式或时长。镜头级走 `shot-links`，幕级走 `links`，两边都是 upsert。 */
async function saveLink(link: StoryboardConnector, patch: { mode?: string; duration?: number }): Promise<void> {
  resetEnqueue(); linkBusy.value = linkKey(link)
  const mode = patch.mode ?? link.mode
  const duration = patch.duration ?? link.duration ?? 1.5
  try {
    if (link.level === 'shot' && link.from_shot_id && link.to_shot_id) await sequenceApi.setShotLink(pid.value, { from_shot_id: link.from_shot_id, to_shot_id: link.to_shot_id, mode, duration })
    else if (link.level === 'scene' && link.from_scene_id && link.to_scene_id) await sequenceApi.setLink(pid.value, { from_scene_id: link.from_scene_id, to_scene_id: link.to_scene_id, mode, duration })
    await reload()
  } catch (err) { enqueueError.value = err instanceof ApiError ? err : null } finally { linkBusy.value = '' }
}
function onDuration(link: StoryboardConnector, value: string): void {
  const duration = Number(value)
  if (Number.isFinite(duration)) void saveLink(link, { duration })
}
/**
 * 一键生成转场（或线上那个单条「生成」）。`only` 就是那一条衔接的 id。
 *
 * 已经出片的转场一条都不会重做（版本永不覆盖），跳过的每条都带原因，绝不静默少做。
 */
async function runTransitions(only?: string[], allowRefDrop = false): Promise<void> {
  resetEnqueue(); enqueuing.value = true; transitionOnly.value = only
  try {
    const out = await sequenceApi.transitionRun(pid.value, { only, allowRefDrop })
    transitionRun.value = out
    enqueueNote.value = out.transitions.length ? `转场已入队：${out.queued.length} 段（账单里一共 ${out.plan.total} 段要生成）` : (out.plan.notes.join('') || '没有需要生成的转场。')
    await reload()
  } catch (err) { enqueueError.value = err instanceof ApiError ? err : null; pendingDrop.value = confirmFlagOf(err) ? 'transition' : null }
  finally { enqueuing.value = false }
}
async function confirmDrop(): Promise<void> {
  const scope = pendingDrop.value
  if (scope === 'scene') await generateScene(true)
  else if (scope === 'shot') await generateShot(true)
  else if (scope === 'transition') await runTransitions(transitionOnly.value, true)
}

/**
 * 确认类拦截（参考图装不下）走弹窗而不是顶上那块方框：它一个任务都没入队，
 * 要的是一句回答。真失败照旧留在 `ErrorPanel` 里。
 */
const dropAsk = computed(() =>
  pendingDrop.value && confirmFlagOf(enqueueError.value) ? enqueueError.value : null,
)
function cancelDrop(): void {
  pendingDrop.value = null
  enqueueError.value = null
}

const ingestModalOpen = ref(false)
const dubModalOpen = ref(false)
const dubSceneId = ref<string | undefined>(undefined)
const dubInitialDialogue = ref<string | undefined>(undefined)
const refineModalOpen = ref(false)
const refineSceneId = ref<string | undefined>(undefined)

function openSceneDub(lane: StoryboardLane) {
  dubSceneId.value = lane.id
  dubInitialDialogue.value = lane.dialogue || undefined
  dubModalOpen.value = true
}

function openSceneRefine(lane: StoryboardLane) {
  refineSceneId.value = lane.id
  refineModalOpen.value = true
}

const sceneConfigModalOpen = ref(false)
const sceneToConfig = ref<StoryboardLane | null>(null)

function openSceneConfig(lane: StoryboardLane): void {
  sceneToConfig.value = lane
  sceneConfigModalOpen.value = true
}

const currentLane = computed(() => {
  if (!story.shot) return null
  return story.lanes.find((l) => l.id === story.shot?.scene_id) ?? null
})

async function saveShotPrompt(val: string): Promise<void> {
  const shotId = story.shot?.id
  if (!shotId || !pid.value) return
  await story.updateShot(pid.value, shotId, { prompt: val.trim() ? val : '' }).catch(() => {})
  await reload()
}

async function promoteShotPromptToScene(): Promise<void> {
  const promptVal = story.shot?.prompt
  const lane = currentLane.value
  if (!promptVal || !lane || !pid.value) return
  await story.updateScene(pid.value, lane.id, { prompt: promptVal }).catch(() => {})
  await reload()
}

async function clearShotPromptToInherit(): Promise<void> {
  const shotId = story.shot?.id
  if (!shotId || !pid.value) return
  await story.updateShot(pid.value, shotId, { prompt: '' }).catch(() => {})
  await reload()
}

/**
 * 幕锚点：幕一多，泳道就是一条几屏高的长列表，靠滚轮找「第 12 幕」是在碰运气。
 * 顶上那一排锚点按钮把「跳到某一幕」变成一次点击——它只滚动，不选中、不改数据，
 * 所以点错了没有代价。滚动容器与每个幕节点各存一个 ref：`scrollIntoView` 在
 * 嵌套滚动容器里会把外层也一起滚，这里只滚泳道那一层。
 */
const laneScroller = ref<HTMLElement | null>(null)
const laneEls = ref<Record<string, HTMLElement | null>>({})
const activeLaneId = ref('')

function setLaneEl(id: string, el: unknown): void {
  laneEls.value[id] = el instanceof HTMLElement ? el : null
}

function jumpToLane(id: string): void {
  const box = laneScroller.value
  const el = laneEls.value[id]
  if (!box || !el) return
  // offsetTop 是相对定位父级的，这里滚动容器就是那个父级（relative），所以能直接用
  box.scrollTo({ top: Math.max(0, el.offsetTop - 8), behavior: 'smooth' })
  activeLaneId.value = id
}

/** 滚到哪一幕了：锚点条要跟着高亮，否则长列表里不知道自己在哪。 */
function onLaneScroll(): void {
  const box = laneScroller.value
  if (!box) return
  let current = ''
  for (const lane of story.lanes) {
    const el = laneEls.value[lane.id]
    if (!el) continue
    if (el.offsetTop - 12 <= box.scrollTop) current = lane.id
    else break
  }
  activeLaneId.value = current || story.lanes[0]?.id || ''
}

const sceneToDelete = ref<StoryboardLane | null>(null)
const deleteSceneDialogOpen = ref(false)

function promptRemoveScene(lane: StoryboardLane): void {
  sceneToDelete.value = lane
  deleteSceneDialogOpen.value = true
}

async function confirmDeleteScene(): Promise<void> {
  if (!sceneToDelete.value || !pid.value) return
  try {
    await story.removeScene(pid.value, sceneToDelete.value.id)
    deleteSceneDialogOpen.value = false
    sceneToDelete.value = null
    await story.loadBoard(pid.value)
  } catch {
    // handled by store
  }
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />
    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1.5 border-b px-2">
      <span class="text-fg-4 text-2xs">筛选</span>
      <select v-model="filter" class="border-line-1 bg-base-2 text-fg-1 h-5 border px-1 text-2xs outline-none"><option value="all">全部 Shot</option><option value="issues">只看上下文不完整</option><option v-for="s in SHOT_STATUS" :key="s" :value="s">{{ SHOT_STATUS_LABEL[s] }}</option></select>
      <span class="text-fg-3 text-2xs">{{ total.shots }} Shot<span v-if="total.transitions"> · {{ total.transitions }} 段转场</span> · {{ fmtDuration(total.duration) }}<span v-if="total.issues" class="text-st-review"> · {{ total.issues }} 个缺上下文</span></span>
      <AppButton size="sm" variant="ghost" title="导入本地长视频并自动切分为镜头分镜（零复制）" @click="ingestModalOpen = true"><Scissors :size="10" />导入长视频加工</AppButton>
      <AppButton size="sm" :disabled="!story.shot || enqueuing || story.busy" title="生成当前 Shot 所在场景的全部镜头" @click="generateScene()"><Sparkles :size="10" />生成本场 Shot</AppButton>
      <AppButton size="sm" variant="primary" :disabled="enqueuing || story.busy || !realCards.length" title="按 Shot 顺序串行生成；上一条完成并产出末帧后才执行下一条" @click="runSequential()"><Sparkles :size="10" />单线程续接</AppButton>
      <AppButton size="sm" :disabled="enqueuing || story.busy || !readyLinks.length" :title="transitionTitle" @click="runTransitions()"><Sparkles :size="10" />一键生成转场<span v-if="readyLinks.length" class="tnum"> {{ readyLinks.length }}</span><span v-else-if="blockedLinks.length" class="text-st-review tnum"> 等 {{ blockedLinks.length }}</span></AppButton>
      <AppButton v-if="pendingPosters.length" size="sm" variant="ghost" :disabled="posterBusy" @click="extractPosters()"><Image :size="10" />补首帧 {{ pendingPosters.length }}</AppButton>
      <AppButton size="sm" variant="ghost" @click="consolePanel.openWith('jobs')"><ListVideo :size="10" />任务</AppButton>
      <AppButton size="sm" variant="ghost" class="ml-auto" :disabled="story.busy" @click="reload()"><RefreshCw :size="10" />刷新</AppButton>
    </div>
    <ErrorPanel v-if="story.lastError" class="mx-2 mt-2" :error="story.lastError" @dismiss="story.clearError()" />
    <ErrorPanel v-if="posterError" class="mx-2 mt-2" :error="posterError" @dismiss="posterError = null" />
    <ErrorPanel v-if="enqueueError && !dropAsk" class="mx-2 mt-2" :error="enqueueError" @dismiss="enqueueError = null" />
    <div v-if="enqueueNote" class="border-line-1 bg-base-2 mx-2 mt-2 border p-1.5 text-2xs"><p class="text-fg-2">{{ enqueueNote }}</p><p v-for="s in skipped" :key="s.shot_id" class="text-st-review">跳过 {{ s.index_no }}：{{ s.error?.title }} — {{ s.error?.detail }}</p><template v-if="transitionRun"><p v-for="made in transitionRun.transitions" :key="made.shot_id" class="text-fg-3">{{ made.reused ? '已有成片，跳过' : '已入队' }}：{{ made.note ?? made.shot_id }}</p><p v-for="s in transitionRun.skipped" :key="s.link_id" class="text-st-review">跳过 {{ s.where }}：{{ s.error ? `${s.error.title} — ${s.error.detail}` : s.reason }}</p><p v-for="b in transitionRun.plan.blocked" :key="b.link_id" class="text-st-review">{{ b.why }} — {{ b.how }}</p></template></div>
    <div class="flex min-h-0 flex-1 gap-2 p-2">
      <section class="border-line-1 bg-base-1 flex min-h-0 min-w-0 flex-1 flex-col border">
        <header class="border-line-1 flex shrink-0 items-center gap-2 border-b px-2 py-1.5">
          <span class="text-fg-1 text-xs">Scene 泳道</span>
          <span class="text-fg-4 text-2xs">点击图片预览 · 双击卡片进入 Shot 工作台 · 卡片之间那条线是转场，没配过就是无转场（直接硬切）</span>
          <span v-if="filter !== 'all'" class="text-st-review text-2xs">筛选中：屏幕上相邻的两张卡片不一定真的相邻，所以只在「全部 Shot」下画那条线</span>
        </header>
        <!-- 幕锚点：只滚动，不选中，也不改任何数据 -->
        <nav v-if="story.lanes.length > 1" class="border-line-1 flex shrink-0 items-center gap-1 overflow-x-auto border-b px-2 py-1">
          <span class="text-fg-4 shrink-0 text-2xs">跳到</span>
          <button
            v-for="lane in story.lanes"
            :key="`anchor-${lane.id}`"
            type="button"
            class="border-line-1 shrink-0 border px-1.5 py-px text-2xs"
            :class="activeLaneId === lane.id ? 'border-accent/60 text-fg-1 bg-base-2' : 'text-fg-3 hover:text-fg-1'"
            :title="`滚动到第 ${lane.index_no} 幕 · ${lane.title}`"
            @click="jumpToLane(lane.id)"
          >
            <span class="tnum">{{ lane.index_no }}</span>
            <span class="ml-1 inline-block max-w-24 truncate align-bottom">{{ lane.title }}</span>
          </button>
        </nav>
        <EmptyState v-if="!story.lanes.length" title="还没有 Shot" body="去剧本页创建镜头，或让 AI 先拆解剧本。" />
        <div
          v-else
          ref="laneScroller"
          class="relative min-h-0 flex-1 space-y-2 overflow-y-auto p-2"
          @scroll="onLaneScroll()"
        >
          <section
            v-for="lane in story.lanes"
            :key="lane.id"
            :ref="(el) => setLaneEl(lane.id, el)"
            class="border-line-1 border bg-base-2"
          >
            <header class="border-line-1 flex items-center gap-1.5 border-b px-2 py-1">
              <span class="text-fg-4 tnum text-2xs">{{ lane.index_no }}</span>
              <span class="text-fg-1 min-w-0 flex-1 truncate text-xs">{{ lane.title }}</span>
              <AppBadge v-if="lane.kind === 'ingested'" tone="accent">长视频切段</AppBadge>
              <AppBadge :tone="lane.param_mode === 'per_shot' ? 'neutral' : 'accent'">
                {{ lane.param_mode === 'per_shot' ? '逐镜独立' : '共用参数' }}
              </AppBadge>
              <AppBadge>{{ realShots(lane).length }} 镜</AppBadge>
              <AppBadge v-if="lane.shots.length > realShots(lane).length">{{ lane.shots.length - realShots(lane).length }} 段转场</AppBadge>
              <AppBadge v-if="!lane.location_variant_id" tone="warn">未挂地点</AppBadge>
              <div class="ml-auto flex items-center gap-1">
                <AppButton size="sm" variant="ghost" class="text-accent hover:bg-accent-dim/30 border border-accent/30" title="配置本幕共用 Prompt、出场人物与地点" @click.stop="openSceneConfig(lane)">
                  <Sliders :size="10" />本幕设定
                </AppButton>
                <AppButton size="sm" variant="ghost" title="整幕一键批量配音" @click.stop="openSceneDub(lane)"><Mic :size="10" />整幕配音</AppButton>
                <AppButton size="sm" variant="ghost" title="整幕成片二次超分/插帧优化" @click.stop="openSceneRefine(lane)"><Zap :size="10" />整幕优化</AppButton>
                <AppButton
                  size="sm"
                  variant="ghost"
                  class="text-fg-4 hover:text-st-failed"
                  :title="lane.kind === 'ingested' ? '一键删除长视频切段（包括该幕下的全部分镜镜头）' : '删除整幕（包括该幕下的全部分镜镜头）'"
                  @click.stop="promptRemoveScene(lane)"
                >
                  <Trash2 :size="10" />{{ lane.kind === 'ingested' ? '删除长视频' : '删除本幕' }}
                </AppButton>
              </div>
            </header>

            <!-- 幕级共用设定摘要栏 -->
            <div
              v-if="lane.prompt || (lane.cast_names && lane.cast_names.length) || lane.location_variant_id"
              class="border-line-1 bg-base-1/60 flex items-center gap-3 px-2 py-1 text-3xs border-b text-fg-3"
            >
              <span v-if="lane.prompt" class="truncate max-w-md flex items-center gap-1" :title="lane.prompt">
                <Sparkles :size="9" class="text-accent shrink-0" />
                <span class="text-fg-4">共用 Prompt:</span>
                <span class="text-fg-2 truncate font-mono">{{ lane.prompt }}</span>
              </span>
              <span v-if="lane.cast_names && lane.cast_names.length" class="truncate flex items-center gap-1">
                <User :size="9" class="text-accent shrink-0" />
                <span class="text-fg-4">本幕人物:</span>
                <span class="text-fg-2">{{ lane.cast_names.join(' / ') }}</span>
              </span>
              <button type="button" class="text-accent hover:underline ml-auto shrink-0" @click.stop="openSceneConfig(lane)">
                修改本幕共用设定
              </button>
            </div>

            <p v-if="visible(realShots(lane)).length === 0" class="text-fg-4 px-2 py-2 text-2xs">{{ realShots(lane).length ? '没有符合筛选条件的 Shot。' : '这一场还没有 Shot。' }}</p>
            <div v-else class="flex items-stretch gap-1 overflow-x-auto p-2">
              <template v-for="row in rows(lane)" :key="row.card.id">
                <article class="w-40 shrink-0 border bg-base-1" :class="row.card.id === story.selectedShotId ? 'border-accent/60' : 'border-line-1'" @click="story.selectShot(pid, row.card.id).catch(() => {})" @dblclick="openShot(row.card.id)">
                  <button class="bg-base-3 flex h-24 w-full items-center justify-center overflow-hidden" title="预览当前采纳的视频版本" @click.stop="previewShot(row.card)"><img v-if="thumb(row.card)" :src="thumb(row.card)" alt="" class="h-full w-full object-cover" /><span v-else class="text-fg-4 px-1 text-center text-2xs">{{ row.card.versions.length ? '点击预览视频' : '尚无画面' }}</span></button>
                  <div class="text-left">
                    <span class="flex items-center gap-1 px-1.5 pt-1"><span class="text-fg-4 tnum text-2xs">{{ row.card.index_no }}</span><span class="text-fg-1 min-w-0 flex-1 truncate text-2xs">{{ row.card.title }}</span><span class="text-fg-3 tnum text-2xs">{{ fmtDuration(row.card.duration) }}</span></span>
                    <span class="flex flex-wrap items-center gap-1 px-1.5 pt-1">
                      <AppBadge :tone="statusTone(row.card.status)">{{ SHOT_STATUS_LABEL[row.card.status as ShotStatus] ?? row.card.status }}</AppBadge>
                      <AppBadge>{{ row.card.versions.length }} 版</AppBadge>
                      <AppBadge v-if="row.card.has_audio_version" tone="ok">音轨</AppBadge>
                      <AppBadge v-if="!row.card.context_ok" tone="warn">缺 {{ row.card.context_issues.length }} 项</AppBadge>
                    </span>
                    <span v-if="row.card.dialogue" class="text-fg-3 block truncate px-1.5 pt-0.5 text-2xs italic" :title="row.card.dialogue">“{{ row.card.dialogue }}”</span>
                    <span class="text-fg-4 block truncate px-1.5 pt-0.5 pb-1 text-2xs">{{ row.card.cast_names.length ? row.card.cast_names.join(' / ') : '没有出场角色' }}</span>
                  </div>
                  <footer class="border-line-1 flex items-center gap-px border-t px-1 py-0.5"><AppButton size="sm" variant="ghost" title="本场内前移" @click.stop="moveWithin(lane.id, row.card.id, -1)"><ChevronLeft :size="10" /></AppButton><AppButton size="sm" variant="ghost" title="本场内后移" @click.stop="moveWithin(lane.id, row.card.id, 1)"><ChevronRight :size="10" /></AppButton><AppButton size="sm" variant="ghost" class="ml-auto" title="删除这个 Shot" @click.stop="removeShot(row.card.id)"><Trash2 :size="10" /></AppButton></footer>
                </article>
                <div v-if="row.link" class="flex w-28 shrink-0 flex-col items-center justify-center gap-1 px-1">
                  <span class="border-line-1 h-0 w-full border-t" />
                  <select :value="row.link.mode" :disabled="linkBusy === linkKey(row.link) || story.busy" class="border-line-1 bg-base-2 text-fg-1 h-5 w-full border px-1 text-2xs outline-none" :title="row.link.mode === 'cut' ? '无转场：两镜直接硬切，不生成任何东西' : '转场：在这两镜之间补一段过渡视频（上一镜真末帧 → 下一镜真首帧）'" @change="saveLink(row.link!, { mode: ($event.target as HTMLSelectElement).value })"><option v-for="m in SHOT_LINK_MODES" :key="m" :value="m">{{ SHOT_LINK_MODE_LABEL[m] }}</option></select>
                  <template v-if="row.link.mode !== 'cut'">
                    <label class="flex w-full items-center gap-1"><span class="text-fg-4 text-2xs">时长</span><input :value="row.link.duration ?? 1.5" type="number" min="0.5" max="4" step="0.1" :disabled="linkBusy === linkKey(row.link) || story.busy" class="border-line-1 bg-base-2 text-fg-1 tnum h-5 min-w-0 flex-1 border px-1 text-2xs outline-none" title="这段转场几秒（0.5 ~ 4）" @change="onDuration(row.link!, ($event.target as HTMLInputElement).value)" /></label>
                    <p v-if="row.link.pending" class="text-st-review w-full text-center text-2xs">{{ row.link.blocked ? '等前后出片' : '转场暂未生成' }}</p>
                    <p v-if="row.link.blocked" class="text-fg-4 w-full text-center text-2xs">{{ row.link.blocked_how }}</p>
                    <AppButton v-if="row.link.pending" size="sm" variant="primary" :disabled="enqueuing || story.busy || !row.link.can_generate" :title="row.link.blocked ?? '只生成这一条转场'" @click="runTransitions([row.link!.id!])"><Sparkles :size="10" />生成</AppButton>
                  <button v-else-if="transitionOf(lane, row.link)" class="bg-base-3 border-line-1 h-10 w-full overflow-hidden border" title="预览这段转场；双击进入转场编辑" @click="previewShot(transitionOf(lane, row.link!)!)" @dblclick.stop="openShot(transitionOf(lane, row.link!)!.id)"><img v-if="thumb(transitionOf(lane, row.link)!)" :src="thumb(transitionOf(lane, row.link)!)" alt="" class="h-full w-full object-cover" /><span v-else class="text-fg-4 text-2xs">转场已生成</span></button>
                  </template>
                  <template v-else-if="row.link.transition_shot_id">
                    <p class="text-st-review w-full text-center text-2xs">改成无转场了，但之前补出来的那段还在，导出照旧带上它</p>
                    <AppButton size="sm" variant="ghost" :disabled="story.busy" title="删掉那个转场镜头（成片版本一起没了，不可撤销）" @click="removeShot(row.link!.transition_shot_id!)"><Trash2 :size="10" />删掉那段转场</AppButton>
                  </template>
                </div>
              </template>
            </div>
            <footer v-if="lane.next_link" class="border-line-1 flex flex-wrap items-center gap-1.5 border-t px-2 py-1">
              <span class="text-fg-4 text-2xs">接第 {{ lane.next_link.to_index_no }} 幕{{ lane.next_link.to_title ? ` · ${lane.next_link.to_title}` : '' }}</span>
              <select :value="lane.next_link.mode" :disabled="linkBusy === linkKey(lane.next_link) || story.busy" class="border-line-1 bg-base-2 text-fg-1 h-5 border px-1 text-2xs outline-none" title="幕与幕之间怎么接：无转场硬切 / 补一段转场 / 让下一幕首镜续接这一幕末帧" @change="saveLink(lane.next_link!, { mode: ($event.target as HTMLSelectElement).value })"><option v-for="m in LINK_MODES" :key="m" :value="m">{{ LINK_MODE_LABEL[m] }}</option></select>
              <label v-if="lane.next_link.mode === 'transition'" class="flex items-center gap-1"><span class="text-fg-4 text-2xs">时长</span><input :value="lane.next_link.duration ?? 1.5" type="number" min="0.5" max="4" step="0.1" :disabled="linkBusy === linkKey(lane.next_link) || story.busy" class="border-line-1 bg-base-2 text-fg-1 tnum h-5 w-16 border px-1 text-2xs outline-none" @change="onDuration(lane.next_link!, ($event.target as HTMLInputElement).value)" /></label>
              <span v-if="lane.next_link.pending" class="text-st-review text-2xs">{{ lane.next_link.blocked ?? '转场暂未生成' }}</span>
              <span v-if="lane.next_link.blocked" class="text-fg-4 text-2xs">{{ lane.next_link.blocked_how }}</span>
              <AppButton v-if="lane.next_link.pending" size="sm" variant="primary" :disabled="enqueuing || story.busy || !lane.next_link.can_generate" :title="lane.next_link.blocked ?? '只生成这一条转场'" @click="runTransitions([lane.next_link!.id!])"><Sparkles :size="10" />生成转场</AppButton>
              <span v-else-if="lane.next_link.generated" class="text-fg-3 text-2xs">{{ lane.next_link.mode === 'cut' ? '改成无转场了，但之前补出来的那段还在，导出照旧带上它' : '转场已生成，排在这一幕最后' }}</span>
              <AppButton v-if="lane.next_link.mode === 'cut' && lane.next_link.transition_shot_id" size="sm" variant="ghost" :disabled="story.busy" title="删掉那个转场镜头（成片版本一起没了，不可撤销）" @click="removeShot(lane.next_link!.transition_shot_id!)"><Trash2 :size="10" />删掉那段转场</AppButton>
            </footer>
          </section>
        </div>
      </section>
      <aside class="border-line-1 bg-base-1 w-80 shrink-0 overflow-auto border">
        <header class="border-line-1 flex items-center gap-2 border-b px-2 py-1.5"><span class="text-fg-1 text-xs">Shot 属性</span><span v-if="story.shot" class="text-fg-4 min-w-0 flex-1 truncate text-2xs">{{ story.shot.title }}</span></header>
        <EmptyState v-if="!story.shot" title="尚无选中 Shot" body="点击卡片选中，点击图片预览视频，双击进入完整工作台。" />
        <div v-else class="space-y-3 p-2">
          <!-- 基础属性 -->
          <section>
            <div class="flex items-center justify-between">
              <p class="text-fg-3 text-2xs">第 {{ story.shot.scene_index_no }} 场 · {{ story.shot.scene_title }}</p>
              <AppBadge v-if="currentLane?.param_mode !== 'per_shot'" tone="accent">共用参数幕</AppBadge>
            </div>
            <label class="mt-1 block">
              <span class="text-fg-4 text-2xs">标题</span>
              <input :value="story.shot.title" class="border-line-1 bg-base-2 text-fg-1 mt-px h-6 w-full border px-1.5 text-2xs outline-none" @change="saveShot('title', ($event.target as HTMLInputElement).value)" />
            </label>
            <div class="mt-1 grid grid-cols-2 gap-1">
              <label><span class="text-fg-4 text-2xs">时长</span><input :value="story.shot.duration" type="number" min="0.1" step="0.1" class="border-line-1 bg-base-2 text-fg-1 tnum mt-px h-6 w-full border px-1.5 text-2xs outline-none" @change="saveDuration(($event.target as HTMLInputElement).value)" /></label>
              <label><span class="text-fg-4 text-2xs">状态</span><select :value="story.shot.status" class="border-line-1 bg-base-2 text-fg-1 mt-px h-6 w-full border px-1 text-2xs outline-none" @change="saveShot('status', ($event.target as HTMLSelectElement).value)"><option v-for="s in SHOT_STATUS" :key="s" :value="s">{{ SHOT_STATUS_LABEL[s] }}</option></select></label>
              <label><span class="text-fg-4 text-2xs">机位</span><input :value="story.shot.camera ?? ''" placeholder="中景 / 特写" class="border-line-1 bg-base-2 text-fg-1 mt-px h-6 w-full border px-1.5 text-2xs outline-none" @change="saveShot('camera', ($event.target as HTMLInputElement).value)" /></label>
              <label><span class="text-fg-4 text-2xs">运镜</span><input :value="story.shot.movement ?? ''" placeholder="推 / 摇 / 固定" class="border-line-1 bg-base-2 text-fg-1 mt-px h-6 w-full border px-1.5 text-2xs outline-none" @change="saveShot('movement', ($event.target as HTMLInputElement).value)" /></label>
            </div>
          </section>

          <!-- Prompt 编辑与共用继承关系 -->
          <section class="border-line-1 border-t pt-2 space-y-1">
            <div class="flex items-center justify-between">
              <span class="text-fg-3 text-2xs font-medium">Prompt 指令</span>
              <AppBadge v-if="story.shot.prompt" tone="warn">当前 Shot 专属覆盖</AppBadge>
              <AppBadge v-else tone="accent">继承本幕共用</AppBadge>
            </div>

            <textarea
              :value="story.shot.prompt ?? ''"
              rows="3"
              :placeholder="currentLane?.prompt ? `（留空继承本幕共用 Prompt: ${currentLane.prompt}）` : '输入专属 Prompt；留空则继承本幕共用 Prompt'"
              class="border-line-1 bg-base-2 text-fg-1 w-full border p-1.5 text-2xs outline-none focus:border-accent resize-none font-mono"
              @change="saveShotPrompt(($event.target as HTMLTextAreaElement).value)"
            />

            <div class="flex items-center justify-between gap-1 text-3xs">
              <template v-if="story.shot.prompt">
                <button
                  type="button"
                  class="text-accent hover:underline flex items-center gap-0.5"
                  title="将此 Shot 的 Prompt 提升设为整幕的共用 Prompt，同幕其他镜头自动同步"
                  @click="promoteShotPromptToScene"
                >
                  <Sparkles :size="9" />设为本幕共用 Prompt
                </button>
                <button
                  type="button"
                  class="text-fg-4 hover:text-fg-2"
                  title="清空此 Shot 的独立 Prompt，恢复为继承本幕 Prompt"
                  @click="clearShotPromptToInherit"
                >
                  清空以继承共用
                </button>
              </template>
              <template v-else>
                <span class="text-fg-4 truncate max-w-[170px]" :title="currentLane?.prompt || ''">
                  {{ currentLane?.prompt ? `共用: ${currentLane.prompt}` : '本幕尚未填写共用 Prompt' }}
                </span>
                <button
                  v-if="currentLane"
                  type="button"
                  class="text-accent hover:underline shrink-0"
                  @click="openSceneConfig(currentLane)"
                >
                  配置本幕共用
                </button>
              </template>
            </div>
          </section>

          <!-- 角色与场景 -->
          <section class="border-line-1 border-t pt-2 space-y-1">
            <div class="flex items-center justify-between">
              <p class="text-fg-3 text-2xs font-medium">角色与场景</p>
              <button
                v-if="currentLane"
                type="button"
                class="text-accent text-3xs hover:underline"
                @click="openSceneConfig(currentLane)"
              >
                配置本幕人物/地点
              </button>
            </div>
            <div class="text-fg-2 text-2xs">
              <span v-if="story.shot.cast.length">
                {{ story.shot.cast.map((c) => c.appearance_name ?? c.character_name).join(' / ') }}
                <span class="text-fg-4 text-3xs">（Shot 独立指定）</span>
              </span>
              <span v-else-if="currentLane?.cast_names?.length" class="text-fg-3">
                {{ currentLane.cast_names.join(' / ') }}
                <span class="text-accent text-3xs">（继承本幕人物）</span>
              </span>
              <span v-else class="text-fg-4">没有出场角色</span>
            </div>
            <p class="text-fg-4 mt-0.5 text-2xs">{{ story.shot.props.length ? story.shot.props.map((p) => p.prop_name).join(' / ') : '没有道具' }}</p>
            <p class="text-fg-4 text-3xs">道具与深度上下文账单可双击进入单 Shot 工作台配置。</p>
          </section>

          <section class="border-line-1 border-t pt-2"><div class="flex items-center gap-1"><p class="text-fg-3 flex-1 text-2xs">已生成版本</p><AppBadge>{{ story.shot.versions.length }} 个</AppBadge></div><div v-if="story.shot.versions.length" class="mt-1 space-y-1"><div v-for="version in story.shot.versions" :key="version.id" class="border-line-1 bg-base-2 border p-1"><div class="flex items-center gap-1"><span class="text-fg-2 tnum text-2xs">v{{ version.version_no }}</span><AppBadge v-if="version.id === story.shot.current_version_id" tone="ok">已采纳</AppBadge><span class="text-fg-4 flex-1 text-right text-2xs">{{ fmtDuration(version.duration) }}</span></div><div class="mt-1 flex gap-1"><AppButton v-if="allCards.some((card) => card.id === story.shot?.id)" size="sm" variant="ghost" @click="previewVersion(allCards.find((card) => card.id === story.shot?.id)!, version.id)">预览</AppButton><AppButton v-if="version.id !== story.shot.current_version_id" size="sm" variant="primary" @click="adoptVersion(version.id)">采纳</AppButton></div></div></div><p v-else class="text-fg-4 mt-1 text-2xs">尚无版本。</p><div class="mt-1 flex gap-1"><AppButton size="sm" variant="primary" :disabled="enqueuing || story.busy" @click="generateShot()"><Sparkles :size="10" />生成 Shot</AppButton><AppButton size="sm" variant="ghost" @click="openShot(story.shot.id)">打开工作台</AppButton></div></section>
        </div>
      </aside>
    </div>
    <div v-if="preview" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6" @click.self="preview = null">
      <div class="border-line-1 bg-base-1 w-full max-w-3xl border p-2 shadow-xl">
        <div class="flex items-center gap-2 px-1 pb-2">
          <span class="text-fg-1 min-w-0 flex-1 truncate text-xs">{{ preview.title }}</span>
          <AppBadge v-if="preview.in_point != null || preview.out_point != null" tone="accent">
            切片区间: {{ preview.in_point ?? 0 }}s ~ {{ preview.out_point ?? '末尾' }}s
          </AppBadge>
          <AppButton size="sm" variant="ghost" @click="preview = null">关闭</AppButton>
          <AppButton size="sm" variant="primary" @click="openShot(preview.shotId)">进入 Shot 工作台</AppButton>
        </div>
        <SegmentPlayer
          :key="previewVideoUrl"
          :src="previewVideoUrl"
          :in-point="preview.in_point"
          :out-point="preview.out_point"
          autoplay
          class="bg-black max-h-[70vh] w-full"
        />
      </div>
    </div>

    <!-- 长视频导入加工切段弹窗 -->
    <IngestVideoModal v-model:open="ingestModalOpen" :pid="pid" @done="reload()" />

    <!-- 整幕 AI 批量配音弹窗 -->
    <DubModal
      v-model:open="dubModalOpen"
      :pid="pid"
      :scene-id="dubSceneId"
      :initial-dialogue="dubInitialDialogue"
      @done="reload()"
    />

    <!-- 整幕成片二次优化处理弹窗 -->
    <RefineModal
      v-model:open="refineModalOpen"
      :pid="pid"
      :scene-id="refineSceneId"
      @done="reload()"
    />

    <!-- 幕设定与共用参数配置弹窗 -->
    <SceneConfigModal
      v-model:open="sceneConfigModalOpen"
      :pid="pid"
      :lane="sceneToConfig"
      @done="reload()"
    />

    <!-- 参考图装不下：弹窗问一句，不是顶上那块找不到按钮的方框 -->
    <ConfirmErrorDialog
      :error="dropAsk"
      :busy="enqueuing"
      confirm-label="知道会少喂几张，确认执行"
      @confirm="confirmDrop()"
      @cancel="cancelDrop()"
    />

    <!-- 一键删除长视频 / 整幕确认对话框 -->
    <AppDialog
      :open="deleteSceneDialogOpen"
      :title="sceneToDelete?.kind === 'ingested' ? '一键删除长视频切段' : '删除整幕与分镜'"
      subtitle="此操作不可撤销，请确认是否继续"
      size="sm"
      @update:open="deleteSceneDialogOpen = $event"
    >
      <div v-if="sceneToDelete" class="p-3 space-y-2 text-2xs">
        <p class="text-fg-1 font-medium">
          确定要删除第 {{ sceneToDelete.index_no }} 幕「{{ sceneToDelete.title }}」吗？
        </p>
        <div class="border-line-1 bg-base-2 p-2 border text-fg-3 space-y-1">
          <div>该幕类型：<strong class="text-fg-1">{{ sceneToDelete.kind === 'ingested' ? '长视频导入切段' : '正片分镜幕' }}</strong></div>
          <div>包含镜头数：<strong class="text-fg-1">{{ realShots(sceneToDelete).length }} 镜</strong></div>
        </div>
        <p class="text-st-failed">
          ⚠️ 确认删除后，本幕以及其下包含的全部 {{ realShots(sceneToDelete).length }} 个镜头分镜将被彻底清理。
        </p>
      </div>
      <template #footer>
        <div class="ml-auto flex items-center gap-2">
          <AppButton size="sm" variant="ghost" @click="deleteSceneDialogOpen = false">取消</AppButton>
          <AppButton
            size="sm"
            variant="primary"
            class="!bg-st-failed !text-white hover:opacity-90"
            :disabled="story.busy"
            @click="confirmDeleteScene"
          >
            <Trash2 :size="10" />确认删除
          </AppButton>
        </div>
      </template>
    </AppDialog>
  </div>
</template>
