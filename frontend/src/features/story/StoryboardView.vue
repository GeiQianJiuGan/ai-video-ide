<script setup lang="ts">
/**
 * 分镜板（Step 5 的鸟瞰视图）。
 *
 * 一行一个 Scene（泳道），一张卡片一个 Shot。卡片上写清四件事：状态、时长、出场角色、
 * 上下文缺不缺——「缺什么」比「有什么」更值得占地方，所以缺上下文的卡片会自己举手。
 *
 * 顺序即时间顺序：移动镜头一律提交给后端重排（`POST /shots/{id}/move`），前端拿返回的
 * 整块分镜板覆盖本地，不自己算 index_no。本轮没有拖拽，用箭头按钮移动——左右在本场内
 * 换位，上下跨场搬。
 *
 * 生成按钮直接入队（队列页负责跑）。整场生成会返回被跳过的镜头与结构化理由，
 * 这一页必须把它们逐条列出来——「排了 5 个、跳过 2 个」里那 2 个才是要处理的事。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  ListVideo,
  RefreshCw,
  Sparkles,
  Trash2,
} from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import { fileUrl } from '@/shared/api/files'
import { assetsApi, type Asset } from '@/shared/api/assets'
import { ApiError } from '@/shared/api/client'
import {
  SHOT_STATUS,
  SHOT_STATUS_LABEL,
  type ShotStatus,
  type StoryboardCard,
} from '@/shared/api/story'
import { useConsoleStore } from '@/stores/console'
import { useStoryStore } from '@/stores/story'
import { generationApi, type EnqueueSceneResult } from '@/shared/api/generation'

const route = useRoute()
const story = useStoryStore()
const consolePanel = useConsoleStore()

const pid = computed(() => String(route.params.pid ?? ''))

/** 'all' / 某个状态 / 'issues'（只看上下文不完整的）。 */
const filter = ref<'all' | 'issues' | ShotStatus>('all')
const assets = ref<Asset[]>([])
const assetError = ref<ApiError | null>(null)

const assetById = computed(() => new Map(assets.value.map((a) => [a.id, a])))
const showAssetError = computed(
  () => assetError.value !== null && assetError.value.code !== story.lastError?.code,
)

const total = computed(() => {
  const shots = story.lanes.flatMap((l) => l.shots)
  return {
    shots: shots.length,
    duration: shots.reduce((n, s) => n + s.duration, 0),
    issues: shots.filter((s) => !s.context_ok).length,
  }
})

function visible(shots: StoryboardCard[]): StoryboardCard[] {
  if (filter.value === 'all') return shots
  if (filter.value === 'issues') return shots.filter((s) => !s.context_ok)
  return shots.filter((s) => s.status === filter.value)
}

function thumb(assetId: string | null): string {
  if (!assetId) return ''
  const asset = assetById.value.get(assetId)
  if (!asset || asset.missing) return ''
  return fileUrl(pid.value, asset.path)
}

function statusTone(status: string): 'neutral' | 'accent' | 'ok' | 'warn' {
  if (status === 'locked') return 'ok'
  if (status === 'review') return 'warn'
  if (status === 'generated' || status === 'ready') return 'accent'
  return 'neutral'
}

async function loadAssets(): Promise<void> {
  if (!pid.value) return
  try {
    assets.value = await assetsApi.list(pid.value)
    assetError.value = null
  } catch (err) {
    assets.value = []
    assetError.value = err instanceof ApiError ? err : null
  }
}

async function reload(): Promise<void> {
  if (!pid.value) return
  await Promise.all([story.loadBoard(pid.value).catch(() => {}), loadAssets()])
}

onMounted(reload)
watch(pid, reload)

function fmtDuration(n: number): string {
  return `${Math.round(n * 10) / 10}s`
}

/** 本场内换位：把它放到目标下标（0-based），后端重排整场。 */
async function moveWithin(laneId: string, shotId: string, delta: number): Promise<void> {
  const lane = story.lanes.find((l) => l.id === laneId)
  if (!lane) return
  const at = lane.shots.findIndex((s) => s.id === shotId)
  const to = at + delta
  if (at < 0 || to < 0 || to >= lane.shots.length) return
  await story.moveShot(pid.value, shotId, laneId, to).catch(() => {})
}

/** 跨场搬：落在目标场的末尾。 */
async function moveLane(laneId: string, shotId: string, delta: number): Promise<void> {
  const at = story.lanes.findIndex((l) => l.id === laneId)
  const target = story.lanes[at + delta]
  if (!target) return
  await story.moveShot(pid.value, shotId, target.id).catch(() => {})
}

async function removeShot(shotId: string): Promise<void> {
  await story.removeShot(pid.value, shotId).catch(() => {})
}

async function saveShot(
  key: 'title' | 'camera' | 'movement' | 'status',
  value: string,
): Promise<void> {
  const shotId = story.shot?.id
  if (!shotId) return
  await story
    .updateShot(pid.value, shotId, {
      [key]: key === 'title' || key === 'status' ? value : value || null,
    })
    .catch(() => {})
}

async function saveDuration(value: string): Promise<void> {
  const shotId = story.shot?.id
  const duration = Number(value)
  if (!shotId || !Number.isFinite(duration) || duration <= 0) return
  await story.updateShot(pid.value, shotId, { duration }).catch(() => {})
}

/** 入队结果：一行结论 + 被跳过的镜头逐条理由。两者都不能只藏在 console 里。 */
const enqueuing = ref(false)
const enqueueError = ref<ApiError | null>(null)
const enqueueNote = ref('')
const skipped = ref<EnqueueSceneResult['skipped']>([])

function resetEnqueue(): void {
  enqueueError.value = null
  enqueueNote.value = ''
  skipped.value = []
}

async function generateShot(): Promise<void> {
  const shot = story.shot
  if (!shot) return
  resetEnqueue()
  enqueuing.value = true
  try {
    const job = await generationApi.enqueueShot(pid.value, shot.id, {
      workflow_id: shot.workflow_id ?? null,
    })
    enqueueNote.value = `已入队：${shot.index_no}. ${shot.title}（${job.kind}）`
    await reload()
  } catch (err) {
    enqueueError.value = err instanceof ApiError ? err : null
  } finally {
    enqueuing.value = false
  }
}

/** 整场生成：按当前选中镜头所在的场入队。 */
async function generateScene(): Promise<void> {
  const sceneId = story.shot?.scene_id
  if (!sceneId) return
  resetEnqueue()
  enqueuing.value = true
  try {
    const out = await generationApi.enqueueScene(pid.value, sceneId)
    enqueueNote.value = `整场入队：${out.queued.length} / ${out.total} 个镜头排进队列`
    skipped.value = out.skipped
    await reload()
  } catch (err) {
    enqueueError.value = err instanceof ApiError ? err : null
  } finally {
    enqueuing.value = false
  }
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />

    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1.5 border-b px-2">
      <span class="text-fg-4 text-2xs">筛选</span>
      <select
        v-model="filter"
        class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 border px-1 text-2xs outline-none"
      >
        <option value="all">全部镜头</option>
        <option value="issues">只看上下文不完整</option>
        <option v-for="s in SHOT_STATUS" :key="s" :value="s">{{ SHOT_STATUS_LABEL[s] }}</option>
      </select>
      <span class="text-fg-3 text-2xs">
        {{ story.lanes.length }} 场 · {{ total.shots }} 镜 · {{ fmtDuration(total.duration) }}
        <span v-if="total.issues > 0" class="text-st-review">
          · {{ total.issues }} 个镜头上下文不完整
        </span>
      </span>
      <AppButton
        size="sm"
        :disabled="!story.shot || enqueuing || story.busy"
        :title="
          story.shot
            ? `把第 ${story.shot.scene_index_no} 场的镜头全部入队；上下文不完整的会被跳过并写明理由`
            : '先在下面选一个镜头——整场生成按它所在的场入队'
        "
        @click="generateScene()"
      >
        <Sparkles :size="10" />生成整场
      </AppButton>
      <AppButton
        size="sm"
        variant="ghost"
        title="在底部控制台的任务框里看任务跑到哪了（不用离开这一页）"
        @click="consolePanel.openWith('jobs')"
      >
        <ListVideo :size="10" />任务
      </AppButton>
      <AppButton size="sm" variant="ghost" class="ml-auto" :disabled="story.busy" @click="reload()">
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>

    <ErrorPanel
      v-if="story.lastError"
      class="mx-2 mt-2"
      :error="story.lastError"
      @dismiss="story.clearError()"
    />
    <ErrorPanel
      v-if="showAssetError"
      class="mx-2 mt-2"
      :error="assetError"
      @dismiss="assetError = null"
    />
    <ErrorPanel
      v-if="enqueueError"
      class="mx-2 mt-2"
      :error="enqueueError"
      @dismiss="enqueueError = null"
    />
    <div
      v-if="enqueueNote"
      class="border-line-1 bg-base-2 mx-2 mt-2 flex items-start gap-2 border p-1.5"
    >
      <div class="min-w-0 flex-1">
        <p class="text-fg-2 text-2xs">{{ enqueueNote }}</p>
        <ul v-if="skipped.length" class="mt-1 space-y-0.5">
          <li v-for="s in skipped" :key="s.shot_id" class="text-2xs">
            <span class="text-st-review">跳过 {{ s.index_no }}：{{ s.error?.title }}</span>
            <span class="text-fg-4"> — {{ s.error?.detail }}</span>
          </li>
        </ul>
      </div>
      <button class="text-fg-4 hover:text-fg-1 text-2xs" @click="resetEnqueue()">关闭</button>
    </div>

    <div class="flex min-h-0 flex-1 gap-2 p-2">
      <!-- 主区：Scene 泳道 -->
      <AppPanel title="Scene 泳道" class="min-h-0 flex-1">
        <EmptyState
          v-if="story.lanes.length === 0"
          title="还没有分镜"
          body="分镜板只是 Scene / Shot 的另一种看法。先去剧本页建几场戏，这里就会长出泳道。"
        />
        <div v-else class="space-y-2 p-2">
          <section
            v-for="lane in story.lanes"
            :key="lane.id"
            class="border-line-1 border bg-base-2"
          >
            <header class="border-line-1 flex items-center gap-1.5 border-b px-2 py-1">
              <span class="text-fg-4 tnum text-2xs">{{ lane.index_no }}</span>
              <span class="text-fg-1 min-w-0 flex-1 truncate text-xs">{{ lane.title }}</span>
              <AppBadge>{{ lane.shots.length }} 镜</AppBadge>
              <AppBadge
                v-if="!lane.location_variant_id"
                tone="warn"
                title="这一场还没挂地点变体，镜头的上下文会缺参考图"
              >
                未挂地点
              </AppBadge>
            </header>
            <p v-if="visible(lane.shots).length === 0" class="text-fg-4 px-2 py-2 text-2xs">
              {{
                lane.shots.length === 0 ? '这一场还没有镜头。' : '这一场没有符合筛选条件的镜头。'
              }}
            </p>
            <div v-else class="flex gap-2 overflow-x-auto p-2">
              <article
                v-for="card in visible(lane.shots)"
                :key="card.id"
                class="w-40 shrink-0 border bg-base-1"
                :class="card.id === story.selectedShotId ? 'border-accent/60' : 'border-line-1'"
              >
                <button class="block w-full text-left" @click="story.selectShot(pid, card.id)">
                  <span class="bg-base-3 flex h-24 items-center justify-center overflow-hidden">
                    <img
                      v-if="thumb(card.thumbnail_asset_id)"
                      :src="thumb(card.thumbnail_asset_id)"
                      alt=""
                      class="h-full w-full object-cover"
                    />
                    <span v-else class="text-fg-4 text-2xs">还没有画面</span>
                  </span>
                  <span class="flex items-center gap-1 px-1.5 pt-1">
                    <span class="text-fg-4 tnum text-2xs">{{ card.index_no }}</span>
                    <span class="text-fg-1 min-w-0 flex-1 truncate text-2xs">{{ card.title }}</span>
                    <span class="text-fg-3 tnum text-2xs">{{ fmtDuration(card.duration) }}</span>
                  </span>
                  <span class="flex flex-wrap items-center gap-1 px-1.5 pt-1">
                    <AppBadge :tone="statusTone(card.status)">
                      {{ SHOT_STATUS_LABEL[card.status as ShotStatus] ?? card.status }}
                    </AppBadge>
                    <AppBadge v-if="card.version_count > 0">{{ card.version_count }} 版</AppBadge>
                    <AppBadge
                      v-if="!card.context_ok"
                      tone="warn"
                      :title="card.context_issues.join('；')"
                    >
                      缺 {{ card.context_issues.length }} 项
                    </AppBadge>
                  </span>
                  <span class="text-fg-4 block truncate px-1.5 pt-0.5 pb-1 text-2xs">
                    {{ card.cast_names.length ? card.cast_names.join(' / ') : '没有出场角色' }}
                  </span>
                </button>
                <footer class="border-line-1 flex items-center gap-px border-t px-1 py-0.5">
                  <AppButton
                    size="sm"
                    variant="ghost"
                    title="本场内前移"
                    @click="moveWithin(lane.id, card.id, -1)"
                  >
                    <ChevronLeft :size="10" />
                  </AppButton>
                  <AppButton
                    size="sm"
                    variant="ghost"
                    title="本场内后移"
                    @click="moveWithin(lane.id, card.id, 1)"
                  >
                    <ChevronRight :size="10" />
                  </AppButton>
                  <AppButton
                    size="sm"
                    variant="ghost"
                    title="搬到上一场末尾"
                    @click="moveLane(lane.id, card.id, -1)"
                  >
                    <ChevronUp :size="10" />
                  </AppButton>
                  <AppButton
                    size="sm"
                    variant="ghost"
                    title="搬到下一场末尾"
                    @click="moveLane(lane.id, card.id, 1)"
                  >
                    <ChevronDown :size="10" />
                  </AppButton>
                  <AppButton
                    size="sm"
                    variant="ghost"
                    class="ml-auto"
                    title="删除这个镜头"
                    @click="removeShot(card.id)"
                  >
                    <Trash2 :size="10" />
                  </AppButton>
                </footer>
              </article>
            </div>
          </section>
        </div>
      </AppPanel>
      <!-- 右：选中镜头 -->
      <AppPanel title="镜头" class="w-72 shrink-0">
        <EmptyState
          v-if="!story.shot"
          title="尚无选中镜头"
          body="点一张卡片，这里可以改标题、时长、机位与状态。提示词与 Workflow 属于镜头编辑器。"
        />
        <div v-else class="space-y-3 p-2">
          <section>
            <p class="text-fg-3 text-2xs tracking-wide uppercase">
              第 {{ story.shot.scene_index_no }} 场 · {{ story.shot.scene_title }}
            </p>
            <div class="mt-1 space-y-1">
              <label class="block">
                <span class="text-fg-4 text-2xs">标题</span>
                <input
                  :value="story.shot.title"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveShot('title', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <div class="grid grid-cols-2 gap-1">
                <label class="block">
                  <span class="text-fg-4 text-2xs">时长（秒）</span>
                  <input
                    :value="story.shot.duration"
                    type="number"
                    min="0.1"
                    step="0.1"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                    @change="saveDuration(($event.target as HTMLInputElement).value)"
                  />
                </label>
                <label class="block">
                  <span class="text-fg-4 text-2xs">状态</span>
                  <select
                    :value="story.shot.status"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                    @change="saveShot('status', ($event.target as HTMLSelectElement).value)"
                  >
                    <option v-for="s in SHOT_STATUS" :key="s" :value="s">
                      {{ SHOT_STATUS_LABEL[s] }}
                    </option>
                  </select>
                </label>
                <label class="block">
                  <span class="text-fg-4 text-2xs">机位</span>
                  <input
                    :value="story.shot.camera ?? ''"
                    placeholder="中景 / 特写"
                    class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                    @change="saveShot('camera', ($event.target as HTMLInputElement).value)"
                  />
                </label>
                <label class="block">
                  <span class="text-fg-4 text-2xs">运镜</span>
                  <input
                    :value="story.shot.movement ?? ''"
                    placeholder="推 / 摇 / 固定"
                    class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                    @change="saveShot('movement', ($event.target as HTMLInputElement).value)"
                  />
                </label>
              </div>
            </div>
          </section>

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">出场与道具</p>
            <p class="text-fg-2 mt-1 text-2xs">
              {{
                story.shot.cast.length
                  ? story.shot.cast.map((c) => c.appearance_name ?? c.character_name).join(' / ')
                  : '还没有出场角色'
              }}
            </p>
            <p class="text-fg-4 mt-0.5 text-2xs">
              {{
                story.shot.props.length
                  ? story.shot.props.map((p) => p.prop_name).join(' / ')
                  : '没有道具'
              }}
            </p>
            <p class="text-fg-4 mt-1 text-2xs">
              挂角色形象与道具在镜头编辑器里做——那里能同时看到上下文账单，改完立刻知道还缺什么。
            </p>
          </section>

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">生成版本</p>
            <p class="text-fg-2 mt-1 text-2xs">
              已有 <span class="tnum text-fg-1">{{ story.shot.version_count }}</span> 个版本
              <template v-if="story.shot.current_version_id">（有当前版本）</template>
            </p>
            <AppButton
              size="sm"
              class="mt-1.5"
              :disabled="enqueuing || story.busy"
              title="入队生成一个新版本；版本永不覆盖，每次生成都会在这里多一条"
              @click="generateShot()"
            >
              <Sparkles :size="10" />生成这个镜头
            </AppButton>
          </section>
        </div>
      </AppPanel>
    </div>
  </div>
</template>
