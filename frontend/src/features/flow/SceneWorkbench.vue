<script setup lang="ts">
/**
 * 幕工作台（两级场景系统的第二级）。
 *
 * 这一页只回答一个问题：**这一幕怎么做出来**。所以它把「一幕」当作工作单位——
 * 左边是这一幕本身（地点变体、时间、镜头清单），中间是当前镜头的**首帧来源**与上下文账单，
 * 右边是版本轨，底部是 prompt 与参数。想看「某一条片段是怎么来的」全部真相，去镜头编辑器。
 *
 * 三个刻意的设计：
 *   1. **首帧来源写成四张卡，而不是一个下拉**。角色表 / 地点参考 / 上传 三条都是
 *      「往上下文里加一张图」，上游末帧那条是「把 prev_shot_id 指过去、生成前抽真末帧」——
 *      这两件事的后果完全不同，界面上不能长得一样。
 *   2. **账单里没被采用的照样列出来**。和镜头编辑器同一个理由：用户问的是「为什么没进去」。
 *   3. **整幕入队会返回一张账单式结果**（入队了几条、跳过了哪几条与原因），
 *      跳过不当成失败弹红叉——它是设计里的门槛。
 *
 * 采用的每一张都标出**它当首帧还是当参考图**（`item.role`，规则只在后端的
 * `services/context.py::_assign_roles`）；版本卡上则显示当次**真正喂进去几张**
 * 与适配器的降级说明（冻结在 `params.refs` / `params.ref_notes` 里）——
 * 账单说 5 张、只喂了 3 张，这件事必须在界面上看得见。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  Film,
  Layers,
  ListVideo,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Sparkles,
  Star,
  Trash2,
  Upload,
  X,
} from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppThumb from '@/shared/ui/AppThumb.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import { ApiError } from '@/shared/api/client'
import { fileUrl } from '@/shared/api/files'
import {
  CONTEXT_KIND_LABEL,
  CONTEXT_ROLE_LABEL,
  type GenerationVersion,
} from '@/shared/api/generation'
import { SHOT_STATUS, SHOT_STATUS_LABEL, type ShotStatus } from '@/shared/api/story'
import { useConsoleStore } from '@/stores/console'
import { useSceneStore } from '@/stores/scene'

const route = useRoute()
const router = useRouter()
const workbench = useSceneStore()
const consolePanel = useConsoleStore()

const pid = computed(() => String(route.params.pid ?? ''))
const sid = computed(() => String(route.params.sid ?? ''))

const sideError = ref<ApiError | null>(null)
const busyFile = ref(false)
const frameInput = ref<HTMLInputElement | null>(null)
const versionInput = ref<HTMLInputElement | null>(null)
const newShotTitle = ref('')
/** 整幕入队的结果：入队了几条、跳过了哪几条。null = 这次会话还没整幕入过。 */
const sceneRun = ref<{ queued: string[]; skipped: unknown[] } | null>(null)

const scene = computed(() => workbench.scene)
const shot = computed(() => workbench.shot)
const bill = computed(() => workbench.bill)
const castIds = computed(() => new Set((shot.value?.cast ?? []).map((c) => c.appearance_id)))
/** 同一件事只报一次（后端重启后两边都会 404「项目未打开」）。 */
const showSideError = computed(
  () => sideError.value !== null && sideError.value.code !== workbench.lastError?.code,
)
/** 上游末帧那张卡的状态：指了谁、它出片了没有。 */
const prevShot = computed(
  () => workbench.shots.find((s) => s.id === shot.value?.prev_shot_id) ?? null,
)
const prevReady = computed(() => Boolean(prevShot.value?.current_version_id))
/** 这一幕选的地点变体那张参考图——`<select>` 里塞不进图，所以在它下面单独给一眼。 */
const sceneVariant = computed(
  () => workbench.variants.find((v) => v.id === scene.value?.location_variant_id) ?? null,
)

/** 账单条目自带相对路径，不用再查资产表。 */
function itemThumb(path: string | null): string {
  return path ? fileUrl(pid.value, path) : ''
}

/**
 * 版本轨上那一格。**视频走 `<video>`、图片走 `<img>`**，两个字段由后端分开给
 * （`generation._version_media`）——以前这里拿版本的 `asset_id` 直接塞进 `<img>`，
 * 而版本的资产几乎总是一段 `.mp4`，于是每一格都是一个坏图标。
 */
function versionVideo(v: GenerationVersion): string {
  return v.video_path ? fileUrl(pid.value, v.video_path) : ''
}

function versionPoster(v: GenerationVersion): string {
  return v.thumbnail_path ? fileUrl(pid.value, v.thumbnail_path) : ''
}

function fmt(n: number | null | undefined): string {
  return n === null || n === undefined ? '—' : `${Math.round(n * 10) / 10}s`
}

/**
 * 这个版本实际喂进去了几张参考图 —— 冻结在 `params.refs` 里，不是现在重算的。
 * 账单上标了 5 张而这里只有 3 张，就是模型端那份图槽位不够（说明在 `ref_notes` 里）。
 */
function refCount(params: Record<string, unknown>): number {
  const refs = params.refs
  return Array.isArray(refs) ? refs.length : 0
}

/** 适配器的降级说明，原样显示：喂少了几张必须看得见，不能悄悄过去。 */
function refNotes(params: Record<string, unknown>): string[] {
  const notes = params.ref_notes
  return Array.isArray(notes) ? notes.map(String) : []
}

async function reload(): Promise<void> {
  if (!pid.value) return
  await workbench.load(pid.value, sid.value).catch(() => {})
}

onMounted(reload)
watch([pid, sid], () => reload())

async function saveSceneField(
  key: 'title' | 'summary' | 'location_variant_id' | 'time_of_day' | 'notes',
  value: string,
): Promise<void> {
  const nullable = key !== 'title'
  await workbench.saveScene(pid.value, { [key]: nullable ? value || null : value }).catch(() => {})
}

async function saveShotText(
  key: 'title' | 'prompt' | 'negative_prompt' | 'description' | 'camera' | 'movement',
  value: string,
): Promise<void> {
  const nullable = key !== 'title'
  await workbench.saveShot(pid.value, { [key]: nullable ? value || null : value }).catch(() => {})
}

async function saveShotNumber(key: 'duration' | 'seed' | 'steps', value: string): Promise<void> {
  const n = Number(value)
  if (value !== '' && !Number.isFinite(n)) return
  await workbench.saveShot(pid.value, { [key]: value === '' ? null : n }).catch(() => {})
}

async function saveStatus(value: string): Promise<void> {
  await workbench.saveShot(pid.value, { status: value }).catch(() => {})
}

/** 挂 / 摘一个形象。挂上之后它的角色表会自动出现在账单里——这就是「从角色表取首帧」。 */
async function toggleCast(appearanceId: string): Promise<void> {
  const next = new Set(castIds.value)
  if (next.has(appearanceId)) next.delete(appearanceId)
  else next.add(appearanceId)
  await workbench.setCast(pid.value, [...next]).catch(() => {})
}

/** 指上游镜头：后端在生成前抽它的**真末帧**，不是把整段视频喂进去。 */
async function setPrev(value: string): Promise<void> {
  await workbench.saveShot(pid.value, { prev_shot_id: value || null }).catch(() => {})
}

async function addShot(): Promise<void> {
  const title = newShotTitle.value.trim() || `镜头 ${workbench.realShots.length + 1}`
  newShotTitle.value = ''
  await workbench.addShot(pid.value, title).catch(() => {})
}

async function onPickFrame(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  busyFile.value = true
  try {
    await workbench.uploadFirstFrame(pid.value, file)
  } catch (err) {
    sideError.value = err instanceof ApiError ? err : null
  } finally {
    busyFile.value = false
  }
}

/** 手动导入一个成片版本：不接 AI 也能把这一幕做完。 */
async function onPickVersion(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  busyFile.value = true
  try {
    await workbench.addVersion(pid.value, file)
  } catch (err) {
    sideError.value = err instanceof ApiError ? err : null
  } finally {
    busyFile.value = false
  }
}

async function generate(skipContext: boolean): Promise<void> {
  await workbench.enqueue(pid.value, !skipContext)
}

async function generateScene(): Promise<void> {
  sceneRun.value = await workbench.enqueueScene(pid.value)
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />
    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1.5 border-b px-2">
      <AppButton
        size="sm"
        variant="ghost"
        title="回到幕流程图，看这一幕在整片里的位置与前后衔接"
        @click="router.push({ name: 'flow', params: { pid } })"
      >
        <ArrowLeft :size="10" />流程图
      </AppButton>
      <span class="text-fg-4 text-2xs">镜头</span>
      <select
        :value="workbench.selectedShotId"
        class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 w-56 border px-1 text-2xs outline-none"
        @change="workbench.select(pid, ($event.target as HTMLSelectElement).value)"
      >
        <option value="">未选择</option>
        <option v-for="s in workbench.shots" :key="s.id" :value="s.id">
          {{ s.index_no }}. {{ s.title }}{{ s.kind === 'transition' ? '（转场）' : '' }}
        </option>
      </select>
      <AppButton
        size="sm"
        variant="primary"
        :disabled="!shot || workbench.busy"
        title="按当前首帧与上下文入队生成一个新版本；旧版本一条都不会被覆盖"
        @click="generate(false)"
      >
        <Sparkles :size="10" />生成本镜头
      </AppButton>
      <AppButton
        size="sm"
        :disabled="!shot || workbench.busy || (bill?.complete ?? false)"
        title="上下文不完整时仍然入队。这是显式选择，不是默认值"
        @click="generate(true)"
      >
        <Play :size="10" />跳过检查入队
      </AppButton>
      <AppButton
        size="sm"
        :disabled="!scene || workbench.busy"
        title="把这一幕里所有可生成的镜头一次入队；缺上下文的会被跳过并写明原因"
        @click="generateScene()"
      >
        <Layers :size="10" />整幕入队
      </AppButton>
      <AppButton
        size="sm"
        variant="ghost"
        title="在底部控制台的任务框里看它跑到哪了（不用离开这一页）"
        @click="consolePanel.openWith('jobs')"
      >
        <ListVideo :size="10" />任务
      </AppButton>
      <span v-if="workbench.lastJob" class="text-fg-4 text-2xs">
        最近入队 · {{ workbench.lastJob.status }}
      </span>
      <AppButton
        size="sm"
        variant="ghost"
        class="ml-auto"
        :disabled="workbench.busy"
        @click="reload()"
      >
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>
    <ErrorPanel
      v-if="workbench.lastError"
      class="mx-2 mt-2"
      :error="workbench.lastError"
      @dismiss="workbench.clearError()"
    />
    <ErrorPanel
      v-if="showSideError"
      class="mx-2 mt-2"
      :error="sideError"
      @dismiss="sideError = null"
    />
    <div
      v-if="sceneRun"
      class="border-line-1 bg-base-2 mx-2 mt-2 shrink-0 border px-2 py-1"
      data-testid="scene-run"
    >
      <p class="text-fg-2 text-2xs">
        整幕入队：成功 {{ sceneRun.queued.length }} 条，跳过 {{ sceneRun.skipped.length }} 条。
        跳过的每一条在队列页与账单里都写着原因，不是静默少做一件事。
      </p>
    </div>
    <EmptyState
      v-if="!scene"
      class="flex-1"
      title="这一幕不在这个工程里"
      body="从幕流程图上点一个节点进来。后端重启过的话，先回起始页把工程重新打开。"
    />
    <template v-else>
      <div class="flex min-h-0 flex-1 gap-2 p-2">
        <!-- 左：这一幕本身 + 镜头清单 -->
        <AppPanel title="这一幕" class="w-64 shrink-0">
          <div class="min-h-0 flex-1 space-y-3 overflow-auto p-2">
            <section>
              <p class="text-fg-3 text-2xs tracking-wide uppercase">第 {{ scene.index_no }} 幕</p>
              <div class="mt-1 space-y-1">
                <label class="block">
                  <span class="text-fg-4 text-2xs">标题</span>
                  <input
                    :value="scene.title"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                    @change="saveSceneField('title', ($event.target as HTMLInputElement).value)"
                  />
                </label>
                <label class="block">
                  <span class="text-fg-4 text-2xs">地点变体（决定这一幕的场景参考图）</span>
                  <select
                    :value="scene.location_variant_id ?? ''"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                    @change="
                      saveSceneField(
                        'location_variant_id',
                        ($event.target as HTMLSelectElement).value,
                      )
                    "
                  >
                    <option value="">未选定</option>
                    <option v-for="v in workbench.variants" :key="v.id" :value="v.id">
                      {{ v.label }}
                    </option>
                  </select>
                </label>
                <p v-if="!scene.location_variant_id" class="text-st-review text-2xs">
                  没选地点变体，这一幕的每个镜头上下文都不完整，入队会被拒。
                </p>
                <div v-else class="mt-px flex items-center gap-1.5">
                  <AppThumb
                    :pid="pid"
                    :path="sceneVariant?.thumbnail_path ?? null"
                    :label="sceneVariant?.label ?? ''"
                    size="md"
                  />
                  <p class="text-fg-4 min-w-0 flex-1 text-2xs">
                    {{
                      sceneVariant?.thumbnail_path
                        ? '这张参考图会进这一幕每个镜头的上下文账单。'
                        : '这个变体还没有参考图，去地点页给它挂一张，否则账单里少这一条。'
                    }}
                  </p>
                </div>
                <label class="block">
                  <span class="text-fg-4 text-2xs">时间</span>
                  <input
                    :value="scene.time_of_day ?? ''"
                    placeholder="夜 / 黄昏"
                    class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                    @change="
                      saveSceneField('time_of_day', ($event.target as HTMLInputElement).value)
                    "
                  />
                </label>
                <label class="block">
                  <span class="text-fg-4 text-2xs">这一幕讲什么</span>
                  <textarea
                    :value="scene.summary ?? ''"
                    rows="3"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px w-full resize-none border px-1.5 py-1 text-2xs outline-none"
                    @change="
                      saveSceneField('summary', ($event.target as HTMLTextAreaElement).value)
                    "
                  />
                </label>
              </div>
            </section>
            <section class="border-line-1 border-t pt-2">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">
                镜头（{{ workbench.realShots.length }}）
              </p>
              <ul class="mt-1 space-y-px">
                <li v-for="s in workbench.realShots" :key="s.id">
                  <button
                    class="flex w-full items-center gap-1 px-1 py-0.5 text-left"
                    :class="
                      s.id === workbench.selectedShotId
                        ? 'bg-accent-dim/40 text-fg-1'
                        : 'text-fg-2 hover:bg-base-2'
                    "
                    @click="workbench.select(pid, s.id)"
                  >
                    <span class="tnum text-2xs">{{ s.index_no }}</span>
                    <span class="min-w-0 flex-1 truncate text-2xs">{{ s.title }}</span>
                    <AppBadge v-if="s.current_version_id" tone="accent">已出片</AppBadge>
                    <span class="text-fg-4 tnum text-2xs">{{ fmt(s.duration) }}</span>
                  </button>
                </li>
              </ul>
              <div class="mt-1 flex gap-1">
                <input
                  v-model="newShotTitle"
                  placeholder="新镜头标题"
                  class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-5 min-w-0 flex-1 border px-1.5 text-2xs outline-none"
                  @keyup.enter="addShot()"
                />
                <AppButton size="sm" variant="ghost" :disabled="workbench.busy" @click="addShot()">
                  <Plus :size="10" />加
                </AppButton>
              </div>
              <template v-if="workbench.transitionShots.length">
                <p class="text-fg-3 mt-2 text-2xs tracking-wide uppercase">
                  转场（{{ workbench.transitionShots.length }}）
                </p>
                <ul class="mt-1 space-y-px">
                  <li v-for="s in workbench.transitionShots" :key="s.id">
                    <button
                      class="flex w-full items-center gap-1 px-1 py-0.5 text-left"
                      :class="
                        s.id === workbench.selectedShotId
                          ? 'bg-accent-dim/40 text-fg-1'
                          : 'text-fg-2 hover:bg-base-2'
                      "
                      @click="workbench.select(pid, s.id)"
                    >
                      <Film :size="10" class="text-fg-4" />
                      <span class="min-w-0 flex-1 truncate text-2xs">{{ s.title }}</span>
                    </button>
                  </li>
                </ul>
                <p class="text-fg-4 mt-1 text-2xs">
                  转场是按流程图上的衔接补出来的，排在本幕最后，导出顺序天然落在两幕之间。
                </p>
              </template>
            </section>

            <section v-if="shot" class="border-line-1 border-t pt-2">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">本镜头出场</p>
              <p v-if="workbench.castOptions.length === 0" class="text-fg-4 mt-1 text-2xs">
                还没有角色形象。先去角色页建一个——挂上形象，它的角色表才会进上下文。
              </p>
              <ul v-else class="mt-1 space-y-px">
                <li v-for="c in workbench.castOptions" :key="c.appearance_id">
                  <label class="hover:bg-base-2 flex items-center gap-1 px-0.5 py-0.5">
                    <input
                      type="checkbox"
                      :checked="castIds.has(c.appearance_id)"
                      class="accent-accent"
                      @change="toggleCast(c.appearance_id)"
                    />
                    <AppThumb :pid="pid" :path="c.thumbnail_path" :label="c.label" />
                    <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">{{ c.label }}</span>
                    <AppBadge
                      v-if="!c.has_sheet"
                      tone="warn"
                      title="这个形象还没有角色表，进不了上下文"
                    >
                      无角色表
                    </AppBadge>
                  </label>
                </li>
              </ul>
              <AppButton
                size="sm"
                variant="ghost"
                class="mt-1"
                :disabled="workbench.busy"
                title="删掉这个镜头。它的版本与资产不会被删，只是不再属于任何镜头"
                @click="workbench.removeShot(pid, shot.id).catch(() => {})"
              >
                <Trash2 :size="10" />删除本镜头
              </AppButton>
            </section>
          </div>
        </AppPanel>

        <!-- 中：首帧来源 + 上下文账单 -->
        <AppPanel title="首帧与上下文" class="min-w-0 flex-1">
          <template #actions>
            <span v-if="bill" class="text-fg-4 tnum text-2xs">
              {{ bill.included_count }} / {{ bill.limit }} 张参考
            </span>
            <AppButton
              size="sm"
              variant="ghost"
              :disabled="workbench.busy"
              title="丢掉所有人工干预，回到自动解析的结果"
              @click="workbench.override(pid, { action: 'reset' }).catch(() => {})"
            >
              <RotateCcw :size="10" />恢复自动
            </AppButton>
          </template>
          <div class="min-h-0 flex-1 overflow-auto p-2">
            <EmptyState
              v-if="!shot"
              title="这一幕还没有镜头"
              body="左边加一个镜头。一幕由若干镜头组成，每个镜头出一段视频。"
            />
            <template v-else>
              <p class="text-fg-3 text-2xs tracking-wide uppercase">首帧从哪来（四条路）</p>
              <div class="mt-1 grid grid-cols-2 gap-1.5">
                <div class="border-line-1 bg-base-2 border p-1.5">
                  <p class="text-fg-1 text-2xs">1 · 从角色表</p>
                  <p class="text-fg-4 mt-0.5 text-2xs">
                    左栏挂上形象，它的角色表自动进账单。挂了 {{ castIds.size }} 个。
                  </p>
                </div>
                <div class="border-line-1 bg-base-2 border p-1.5">
                  <p class="text-fg-1 text-2xs">2 · 从地点参考</p>
                  <p class="text-fg-4 mt-0.5 text-2xs">
                    {{
                      scene.location_variant_name
                        ? `本幕用「${scene.location_variant_name}」的参考图`
                        : '左栏选一个地点变体，它的参考图自动进账单。'
                    }}
                  </p>
                </div>
                <div class="border-line-1 bg-base-2 border p-1.5">
                  <p class="text-fg-1 text-2xs">3 · 上传一张</p>
                  <p class="text-fg-4 mt-0.5 text-2xs">
                    直接给一张首帧，记成人工添加，优先级最高。
                  </p>
                  <AppButton
                    size="sm"
                    variant="ghost"
                    class="mt-1"
                    :disabled="busyFile || workbench.busy"
                    @click="frameInput?.click()"
                  >
                    <Upload :size="10" />选图
                  </AppButton>
                  <input
                    ref="frameInput"
                    type="file"
                    accept="image/*"
                    class="hidden"
                    @change="onPickFrame"
                  />
                </div>
                <div class="border-line-1 bg-base-2 border p-1.5">
                  <p class="text-fg-1 text-2xs">4 · 接上游末帧</p>
                  <select
                    :value="shot.prev_shot_id ?? ''"
                    class="border-line-1 bg-base-3 text-fg-1 focus:border-accent/60 mt-1 h-5 w-full border px-1 text-2xs outline-none"
                    @change="setPrev(($event.target as HTMLSelectElement).value)"
                  >
                    <option value="">不接上游</option>
                    <option v-for="s in workbench.prevCandidates" :key="s.id" :value="s.id">
                      {{ s.index_no }}. {{ s.title }}
                    </option>
                  </select>
                  <p v-if="!shot.prev_shot_id" class="text-fg-4 mt-0.5 text-2xs">
                    这一条不是「加一张图」：它让后端在生成前抽上游的真末帧，接不上的话队列里会写明在等谁。
                  </p>
                  <p v-else-if="prevReady" class="text-st-done mt-0.5 text-2xs">
                    上游「{{ prevShot?.title }}」已出片，生成前会抽它的真末帧当首帧。
                  </p>
                  <p v-else class="text-st-review mt-0.5 text-2xs">
                    上游「{{
                      prevShot?.title
                    }}」还没有当前版本，本镜头会排成可解释的等待，不是卡住。
                  </p>
                </div>
              </div>
              <div
                v-if="bill && !bill.complete"
                class="border-st-failed/40 bg-base-2 mt-3 border p-1.5"
              >
                <p class="text-st-review text-2xs">
                  上下文不完整，直接「生成本镜头」会被后端拒掉：
                </p>
                <ul class="text-fg-2 mt-0.5 space-y-px text-2xs">
                  <li v-for="p in bill.problems" :key="p">· {{ p }}</li>
                </ul>
              </div>
              <p v-else-if="bill" class="text-st-done mt-3 text-2xs">
                上下文完整{{ bill.at_limit ? '（已到参考图上限，多出来的会被省略）' : '' }}。
              </p>

              <p class="text-fg-3 mt-3 text-2xs tracking-wide uppercase">
                已采用（{{ workbench.included.length }}）
              </p>
              <EmptyState
                v-if="workbench.included.length === 0"
                title="一条参考都没有"
                body="用上面四条路里的任意一条给它首帧——账单会立刻重算。"
              />
              <ul v-else class="mt-1 grid grid-cols-4 gap-1.5">
                <li
                  v-for="item in workbench.included"
                  :key="item.key"
                  class="border-line-1 bg-base-2 border"
                >
                  <div class="bg-base-3 flex h-20 items-center justify-center overflow-hidden">
                    <img
                      v-if="itemThumb(item.asset_path)"
                      :src="itemThumb(item.asset_path)"
                      class="max-h-full max-w-full object-contain"
                      :alt="item.label"
                    />
                    <span v-else class="text-fg-4 text-2xs">
                      {{ item.missing_file ? '文件不在磁盘上' : '无图' }}
                    </span>
                  </div>
                  <div class="p-1">
                    <div class="flex items-center gap-1">
                      <!-- 这一张是当首帧还是当参考图：规则在后端，这里只把它标出来 -->
                      <AppBadge :tone="item.role === 'first_frame' ? 'ok' : 'accent'">
                        {{ CONTEXT_ROLE_LABEL[item.role ?? ''] ?? '参考图' }}
                      </AppBadge>
                      <AppBadge tone="neutral">
                        {{ CONTEXT_KIND_LABEL[item.kind] ?? item.kind }}
                      </AppBadge>
                      <AppBadge v-if="item.manual" tone="warn">人工</AppBadge>
                      <button
                        class="text-fg-4 hover:text-st-failed ml-auto"
                        title="从这次上下文里移除（记成人工覆写，可「恢复自动」撤销）"
                        @click="
                          workbench
                            .override(pid, { action: 'remove', key: item.key })
                            .catch(() => {})
                        "
                      >
                        <X :size="10" />
                      </button>
                    </div>
                    <p class="text-fg-2 mt-0.5 truncate text-2xs" :title="item.label">
                      {{ item.label }}
                    </p>
                  </div>
                </li>
              </ul>
              <p class="text-fg-3 mt-3 text-2xs tracking-wide uppercase">
                未采用（{{ workbench.omitted.length }}）
              </p>
              <p v-if="workbench.omitted.length === 0" class="text-fg-4 mt-1 text-2xs">
                没有被省略的条目。
              </p>
              <ul v-else class="mt-1 space-y-px">
                <li
                  v-for="item in workbench.omitted"
                  :key="item.key"
                  class="border-line-1 hover:bg-base-2 flex items-center gap-1.5 border px-1 py-0.5 opacity-70"
                >
                  <AppBadge tone="neutral">
                    {{ CONTEXT_KIND_LABEL[item.kind] ?? item.kind }}
                  </AppBadge>
                  <span class="text-fg-2 min-w-0 shrink-0 truncate text-2xs">{{ item.label }}</span>
                  <span class="text-fg-4 min-w-0 flex-1 truncate text-2xs" :title="item.reason">
                    {{ item.reason }}
                  </span>
                </li>
              </ul>
            </template>
          </div>
        </AppPanel>

        <!-- 右：版本轨。只增不改，换当前版本是唯一的「修改」 -->
        <AppPanel title="版本轨" class="w-56 shrink-0">
          <template #actions>
            <AppButton
              size="sm"
              variant="ghost"
              :disabled="!shot || busyFile || workbench.busy"
              title="手动导入一个成片版本：不接 AI 也能把这一幕做完"
              @click="versionInput?.click()"
            >
              <Upload :size="10" />导入
            </AppButton>
            <input
              ref="versionInput"
              type="file"
              accept="image/*,video/*"
              class="hidden"
              @change="onPickVersion"
            />
          </template>
          <div class="min-h-0 flex-1 overflow-auto p-2">
            <EmptyState
              v-if="workbench.versions.length === 0"
              title="还没有任何版本"
              body="「生成本镜头」入队一个，或者「导入」一个已有的成片。版本只增不改。"
            />
            <ul v-else class="space-y-1">
              <li
                v-for="v in workbench.versions"
                :key="v.id"
                class="border p-1"
                :class="
                  v.is_current
                    ? 'border-accent/60 bg-accent-dim/40'
                    : 'border-line-1 bg-base-2 hover:bg-base-3'
                "
              >
                <div class="flex items-center gap-1">
                  <span class="text-fg-1 tnum text-2xs">v{{ v.version_no }}</span>
                  <AppBadge :tone="v.source === 'manual' ? 'neutral' : 'accent'">
                    {{ v.source === 'manual' ? '手动' : '生成' }}
                  </AppBadge>
                  <AppBadge v-if="v.status !== 'done'" tone="warn">{{ v.status }}</AppBadge>
                  <button
                    v-if="!v.is_current"
                    class="text-fg-4 hover:text-accent ml-auto"
                    title="采用这一段：设为本镜头的当前版本（时间线装配、下游镜头抽末帧都只认它）"
                    @click="workbench.setCurrent(pid, v.id).catch(() => {})"
                  >
                    <Star :size="10" />
                  </button>
                  <Star
                    v-else
                    :size="10"
                    class="text-accent ml-auto"
                    title="本镜头采用的就是这一段"
                  />
                </div>
                <!-- 视频给播放器，图片才走 <img>：两个字段绝不混用 -->
                <div
                  v-if="versionVideo(v) || versionPoster(v)"
                  class="bg-base-3 mt-1 flex h-16 items-center justify-center overflow-hidden"
                >
                  <video
                    v-if="versionVideo(v)"
                    :src="versionVideo(v)"
                    :poster="versionPoster(v) || undefined"
                    controls
                    preload="metadata"
                    class="max-h-full max-w-full"
                  />
                  <img
                    v-else
                    :src="versionPoster(v)"
                    class="max-h-full max-w-full object-contain"
                    :alt="`v${v.version_no}`"
                  />
                </div>
                <p class="text-fg-4 mt-0.5 text-2xs">
                  {{ v.kind }} · {{ fmt(v.duration) }} · {{ v.created_at.slice(0, 16) }}
                  <template v-if="refCount(v.params)">
                    · 参考图 {{ refCount(v.params) }} 张
                  </template>
                </p>
                <!-- 适配器说「少喂了几张」时原样显示：这就是形象跑偏的现场证据 -->
                <p
                  v-for="note in refNotes(v.params)"
                  :key="note"
                  class="text-st-review mt-0.5 text-2xs"
                >
                  {{ note }}
                </p>
                <p v-if="v.error" class="text-st-review mt-0.5 text-2xs">这个版本是失败现场</p>
              </li>
            </ul>
          </div>
        </AppPanel>
      </div>
      <!-- 底：prompt 与参数。入队那一刻这些值会被冻结进版本里 -->
      <div v-if="shot" class="border-line-1 bg-base-1 shrink-0 border-t p-2">
        <div class="flex gap-2">
          <label class="min-w-0 flex-1">
            <span class="text-fg-4 text-2xs">Prompt（这一条镜头要画什么）</span>
            <textarea
              :value="shot.prompt ?? ''"
              rows="3"
              placeholder="账单里「参考图」那几张会连标签一起喂给模型（要求预设图里有 AIVS_REF_* 槽位）。本轮只做 R2V：必须有首帧。"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px w-full resize-none border px-1.5 py-1 text-2xs outline-none"
              @change="saveShotText('prompt', ($event.target as HTMLTextAreaElement).value)"
            />
          </label>
          <label class="min-w-0 flex-1">
            <span class="text-fg-4 text-2xs">Negative Prompt</span>
            <textarea
              :value="shot.negative_prompt ?? ''"
              rows="3"
              placeholder="不要出现的东西"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px w-full resize-none border px-1.5 py-1 text-2xs outline-none"
              @change="
                saveShotText('negative_prompt', ($event.target as HTMLTextAreaElement).value)
              "
            />
          </label>
          <div class="grid w-72 shrink-0 grid-cols-2 gap-1">
            <label class="block">
              <span class="text-fg-4 text-2xs">时长（秒）</span>
              <input
                :value="shot.duration"
                type="number"
                min="0.1"
                step="0.1"
                class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                @change="saveShotNumber('duration', ($event.target as HTMLInputElement).value)"
              />
            </label>
            <label class="block">
              <span class="text-fg-4 text-2xs">状态</span>
              <select
                :value="shot.status"
                class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                @change="saveStatus(($event.target as HTMLSelectElement).value)"
              >
                <option v-for="s in SHOT_STATUS" :key="s" :value="s">
                  {{ SHOT_STATUS_LABEL[s as ShotStatus] }}
                </option>
              </select>
            </label>
            <label class="block">
              <span class="text-fg-4 text-2xs">Seed（空 = 随机）</span>
              <input
                :value="shot.seed ?? ''"
                type="number"
                class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                @change="saveShotNumber('seed', ($event.target as HTMLInputElement).value)"
              />
            </label>
            <label class="block">
              <span class="text-fg-4 text-2xs">机位</span>
              <input
                :value="shot.camera ?? ''"
                placeholder="中景 / 特写"
                class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                @change="saveShotText('camera', ($event.target as HTMLInputElement).value)"
              />
            </label>
            <p class="text-fg-4 col-span-2 text-2xs">
              用哪套图、怎么调模型由模型端负责，这里只给入口参数——本工具不维护模型端的图。
            </p>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
