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
 *
 * 本轮没有拖拽与波形：位置用数字输入提交，轨道区只按秒数比例画块。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Clapperboard,
  Download,
  FileSearch,
  Redo2,
  RefreshCw,
  Scissors,
  Trash2,
  Undo2,
  Wand2,
} from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import {
  TRACK_KIND_LABEL,
  TRANSITION_KINDS,
  TRANSITION_LABEL,
  type Clip,
} from '@/shared/api/timeline'
import { generationApi, type GenerationVersion } from '@/shared/api/generation'
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

async function doRemove(clipId: string, ripple: boolean): Promise<void> {
  await tl.remove(pid.value, clipId, ripple).catch(() => {})
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
    />

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
        <!-- 轨道区：按秒数比例画块，宽度就是时长 -->
        <AppPanel title="轨道区" class="min-h-0 flex-1">
          <template #actions>
            <span class="text-fg-4 text-2xs">
              点一个片段在右边改它的入出点与版本；顺序与位置一律由后端重排
            </span>
          </template>
          <EmptyState
            v-if="tl.clips.length === 0"
            title="轨道上还没有片段"
            body="点「自动装配」把每个镜头的当前版本按 Scene / Shot 顺序铺上来。没有当前版本的镜头会被跳过并写明理由——这一步不需要 ComfyUI。"
          />
          <div v-else class="space-y-2 overflow-x-auto p-2">
            <section v-for="track in tl.timeline?.tracks ?? []" :key="track.id">
              <header class="flex items-center gap-1.5 pb-1">
                <span class="text-fg-3 text-2xs">
                  {{ TRACK_KIND_LABEL[track.kind] ?? track.kind }} · {{ track.name }}
                </span>
                <AppBadge>{{ track.clips.length }} 段</AppBadge>
                <AppBadge v-if="track.muted" tone="warn">静音</AppBadge>
                <AppBadge v-if="track.locked" tone="warn">已锁</AppBadge>
              </header>
              <div
                class="border-line-1 bg-base-2 relative h-14 border"
                :style="{
                  width: `${Math.max(320, (tl.timeline?.duration_total ?? 0) * zoom + 40)}px`,
                }"
              >
                <p
                  v-if="track.clips.length === 0"
                  class="text-fg-4 absolute top-1 left-1.5 text-2xs"
                >
                  这条轨道是空的。
                </p>
                <button
                  v-for="clip in track.clips"
                  :key="clip.id"
                  class="absolute top-0.5 bottom-0.5 overflow-hidden border px-1 text-left"
                  :class="[
                    clip.id === selectedId ? 'bg-accent-dim/40' : 'bg-base-1 hover:bg-base-3',
                    clip.missing_file
                      ? 'border-st-failed/60'
                      : clip.id === selectedId
                        ? 'border-accent/60'
                        : 'border-line-1',
                  ]"
                  :style="{
                    left: `${clip.start * zoom}px`,
                    width: `${Math.max(18, clip.duration * zoom)}px`,
                  }"
                  :title="`${clip.label ?? clip.id} · 起点 ${fmt(clip.start)} · 时长 ${fmt(clip.duration)}${clip.missing_file ? ' · 文件已丢失' : ''}`"
                  @click="selectedId = clip.id"
                >
                  <span class="text-fg-1 block truncate text-2xs">
                    {{ clip.shot_index_no ?? '—' }}. {{ clip.label ?? '未命名片段' }}
                  </span>
                  <span class="text-fg-4 tnum block truncate text-2xs">{{
                    fmt(clip.duration)
                  }}</span>
                  <span v-if="clip.version_no" class="text-fg-4 block truncate text-2xs">
                    v{{ clip.version_no }}
                  </span>
                  <span v-if="clip.missing_file" class="text-st-review block truncate text-2xs">
                    文件丢失
                  </span>
                </button>
              </div>
            </section>
            <p v-if="tl.missing.length" class="text-st-review text-2xs">
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
                {{ tl.plan.clips }} 个片段
              </p>
              <pre
                class="text-fg-3 bg-base-2 border-line-1 mt-1 max-h-24 overflow-auto border p-1 text-2xs"
                >{{ tl.plan.command.join(' ') }}</pre>
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
