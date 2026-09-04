<script setup lang="ts">
/**
 * 底部控制台：任务框 + 日志框。常驻在状态条之上，默认收起。
 *
 * 队列本来是左栏里的一个菜单项，但它不是一个「要走过去看」的地方——它是**一直在跑
 * 的东西**，你在别的页面干活时也想瞄一眼。所以它下沉成控制台的一个页签，入口是
 * 状态条上那个任务标识（点一下开、再点一下收）。完整的队列页还在（失败现场、冻结
 * 参数、优先级细调），只是从「主路」降成「点开细看」。
 *
 * 五个刻意的设计：
 *   1. **WS 订阅归它**。以前是队列页 `onUnmounted` 时 disconnect，于是一离开页面
 *      实时通道就断了；控制台常驻，订阅挂在这里，收起来也照样在收事件——
 *      状态条上的计数才可能是真的。切 / 关工程时 `queue.reset()`，绝不把上一个
 *      工程的任务列表留在界面上。
 *   2. **任务框按入队时间倒序，位置不随状态变**（口径在 `stores/queue.ts::rows`）：
 *      跑完的、失败的都停在原地，只是状态点变色。按「要不要管」重排会让刚失败的那条
 *      从眼前挪走，于是每次找报错都得重新扫一遍列表——队列页那侧同一个道理，
 *      后端 `list_jobs` 的排序里也没有状态这一项。
 *   3. **失败不静默**：`queue.lastError` 用 `ErrorPanel` 显示四要素；每一行失败任务
 *      旁边就是「重试」——沿用原参数重跑，一条旧版本都不会被覆盖（硬约束 3）。
 *   4. **日志框最新在最上面**。事件是环形缓冲（最近 200 条）且**可丢失**，
 *      倒序省掉一整套自动滚动的机关，也让「刚刚发生了什么」永远在视线里。
 *      它清的只是前端这份缓冲，后端什么都不会被删。
 *   5. **高度可拖，记在 localStorage**（`stores/console.ts`）。控制台高度是布局偏好，
 *      刷新一次就回到默认值会让人不想用它。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowUp,
  Ban,
  ChevronDown,
  ChevronRight,
  Eraser,
  ExternalLink,
  Layers,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Trash2,
  Wand2,
} from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import { JOB_STATUS_LABEL, type Job, type JobBatch } from '@/shared/api/generation'
import type { BusEvent, Channel } from '@/shared/api/ws'
import { useConsoleStore } from '@/stores/console'
import { useQueueStore } from '@/stores/queue'
import { useSystemStore } from '@/stores/system'

const props = defineProps<{ projectId: string | null }>()

const panel = useConsoleStore()
const queue = useQueueStore()
const sys = useSystemStore()
const router = useRouter()

const pid = computed(() => props.projectId ?? '')

/** 订阅跟着工程走：没有工程就断开并清空，绝不显示上一个工程的任务。 */
watch(
  pid,
  (id) => {
    if (!id) {
      queue.reset()
      return
    }
    void queue.load(id).catch(() => {})
    queue.connect(id)
  },
  { immediate: true },
)

onUnmounted(() => queue.disconnect())

/** 状态点的颜色：跑 / 等 / 失败三种要一眼能分开（与队列页同一张表）。 */
const TONE: Record<string, 'neutral' | 'accent' | 'ok' | 'warn' | 'fail'> = {
  queued: 'neutral',
  waiting: 'warn',
  running: 'accent',
  done: 'ok',
  failed: 'fail',
  canceled: 'neutral',
  paused: 'warn',
}

/**
 * 任务框的行来自 store 的 `rows`：**一次编排合并成一条，零散任务各一条，按入队时间倒序**。
 * 以前这里按状态重排（running → queued → failed → 其余），于是一条跑完就掉到最底下，
 * 用户以为它丢了；而一次「单线程续接」会刷出几十行长得一模一样的东西。
 * 排序与合并的口径只留一份（`stores/queue.ts::rows`），这里只管画。
 */
const rows = computed(() => queue.rows)

/** 展开了哪几条合并任务。**默认全收起**：控制台只有两百来像素高。 */
const opened = ref<Set<string>>(new Set())

function toggle(batchId: string): void {
  const next = new Set(opened.value)
  if (!next.delete(batchId)) next.add(batchId)
  opened.value = next
}

const breakdownRows = computed(() =>
  [...queue.breakdownTasks].sort((a, b) => {
    const rank = (status: string) => (status === 'running' ? 0 : status === 'failed' ? 1 : 2)
    return rank(a.status) - rank(b.status)
  }),
)

const CHANNELS: Channel[] = ['job', 'queue', 'shot', 'version', 'asset', 'system', 'error']
const logChannel = ref<Channel | ''>('')

/** 倒序：最新一条在最上面，不用做自动滚动。 */
const logs = computed(() => {
  const list = logChannel.value
    ? sys.events.filter((e) => e.channel === logChannel.value)
    : sys.events
  return [...list].reverse()
})

/**
 * 时间只取 `HH:MM:SS`。事件是从线上来的数据，**不假设字段一定齐**——
 * 少一个 `ts` 就让整块日志白屏，那是最没道理的一种失败。
 */
function timeOf(ev: BusEvent): string {
  return ev.ts?.slice(11, 19) || ev.ts || '—'
}

function payloadOf(ev: BusEvent): string {
  try {
    const text = JSON.stringify(ev.payload ?? {})
    return text === '{}' ? '' : text
  } catch {
    return ''
  }
}

function elapsed(job: Job): string {
  if (!job.started_at) return '—'
  const end = job.finished_at ? Date.parse(job.finished_at) : Date.now()
  const sec = Math.max(0, Math.round((end - Date.parse(job.started_at)) / 1000))
  return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m${sec % 60}s`
}

/**
 * 合并任务显示「执行到第 N/M 步」，**不显示百分比进度条**——ComfyUI 不回显进度，
 * 后端那个 `progress` 是按等待秒数编出来的假爬升，画成进度条就是拿编的数字骗人。
 * 「12 步里做完了 3 步」这句话是真的。
 */
function stepText(batch: JobBatch): string {
  if (batch.status === 'running') return `执行到第 ${batch.step}/${batch.total} 步`
  if (batch.status === 'queued') return `已完成 ${batch.settled}/${batch.total} 步，正在排队`
  if (batch.status === 'failed') return `第 ${batch.step}/${batch.total} 步失败，后面的没有继续`
  if (batch.status === 'canceled') return `已取消，做到第 ${batch.settled}/${batch.total} 步`
  return `${batch.total} 步全部完成`
}

/** 一批用了多久：第一条开始到最后一条结束（没开始过就是 —）。 */
function batchElapsed(members: Job[]): string {
  const starts = members.map((m) => m.started_at).filter(Boolean) as string[]
  if (!starts.length) return '—'
  const begin = Math.min(...starts.map((s) => Date.parse(s)))
  const done = members.every((m) => !['queued', 'waiting', 'running'].includes(m.status))
  const ends = members.map((m) => m.finished_at).filter(Boolean) as string[]
  const end = done && ends.length ? Math.max(...ends.map((s) => Date.parse(s))) : Date.now()
  const sec = Math.max(0, Math.round((end - begin) / 1000))
  return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m${sec % 60}s`
}

/**
 * 提到队首：比当前最大 priority 再大 1。
 *
 * **后端按 priority 降序取**（`_claim` 里的 `-j.priority`，默认值 100），所以「队首」
 * 是数字最大的那一条。这里以前算的是 `min - 1`，点下去反而把它排到了所有任务后面。
 */
async function bump(job: Job): Promise<void> {
  const max = Math.max(...queue.jobs.map((j) => j.priority), job.priority)
  await queue.setPriority(pid.value, job.id, max + 1)
}

/** 跳到那个镜头。**出图任务没有镜头**（`shot_id` 是空的），此时这一下什么都不做。 */
function goShot(shotId: string | null): void {
  if (!pid.value || !shotId) return
  void router.push({ name: 'shot', params: { pid: pid.value, sid: shotId } })
}

/** 队列页仍在：失败现场、入队时冻结的参数、优先级细调都在那儿。 */
function openQueuePage(): void {
  if (!pid.value) return
  void router.push({ name: 'queue', params: { pid: pid.value } })
}

/** 往上拖变高。夹在 store 的上下限之间，拖不出一个挤掉主区的控制台。 */
function startResize(e: PointerEvent): void {
  const startY = e.clientY
  const startH = panel.height
  const move = (m: PointerEvent): void => panel.setHeight(startH + (startY - m.clientY))
  const up = (): void => {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
}
</script>

<template>
  <section
    v-if="panel.open"
    class="border-line-1 bg-base-1 flex shrink-0 flex-col border-t"
    :style="{ height: `${panel.height}px` }"
  >
    <!-- 拖上边缘改高度 -->
    <div
      class="hover:bg-accent/60 -mt-px h-px shrink-0 cursor-row-resize"
      title="上下拖动改变控制台高度"
      @pointerdown.prevent="startResize"
    />

    <div class="border-line-1 flex h-row shrink-0 items-center gap-1 border-b px-1.5">
      <button
        type="button"
        class="h-5 px-1.5 text-2xs"
        :class="
          panel.tab === 'jobs'
            ? 'border-accent text-fg-1 border-b'
            : 'text-fg-3 hover:text-fg-1 border-b border-transparent'
        "
        @click="panel.tab = 'jobs'"
      >
        任务框
        <span v-if="rows.length + queue.breakdownTasks.length" class="tnum text-fg-4">
          {{ rows.length + queue.breakdownTasks.length }}
        </span>
      </button>
      <button
        type="button"
        class="h-5 px-1.5 text-2xs"
        :class="
          panel.tab === 'logs'
            ? 'border-accent text-fg-1 border-b'
            : 'text-fg-3 hover:text-fg-1 border-b border-transparent'
        "
        @click="panel.tab = 'logs'"
      >
        日志框
        <span v-if="sys.events.length" class="tnum text-fg-4">{{ sys.events.length }}</span>
      </button>

      <!-- 任务框的动作 -->
      <template v-if="panel.tab === 'jobs' && pid">
        <span class="border-line-1 mx-1 h-3.5 border-l" />
        <AppButton
          v-if="!queue.paused"
          size="sm"
          :disabled="queue.busy"
          title="停止取新任务。已经在跑的不会中断，也不会被取消"
          @click="queue.pause(pid)"
        >
          <Pause :size="10" />暂停
        </AppButton>
        <AppButton
          v-else
          size="sm"
          variant="primary"
          :disabled="queue.busy"
          title="继续取新任务"
          @click="queue.resume(pid)"
        >
          <Play :size="10" />继续
        </AppButton>
        <AppButton
          size="sm"
          :disabled="queue.busy || queue.failed.length === 0"
          title="沿用原参数重跑所有失败任务；旧版本一条都不会被覆盖"
          @click="queue.retryFailed(pid)"
        >
          <RotateCcw :size="10" />重试失败（{{ queue.failed.length }}）
        </AppButton>
        <AppButton
          size="sm"
          variant="ghost"
          :disabled="
            queue.busy ||
            (queue.failed.length === 0 && !queue.breakdownTasks.some((t) => t.status === 'failed'))
          "
          title="清空所有失败任务记录"
          @click="queue.clearFailed(pid)"
        >
          <Trash2 :size="10" />清空失败
        </AppButton>
        <AppButton
          size="sm"
          variant="ghost"
          class="text-st-failed hover:bg-st-failed/10"
          :disabled="queue.busy || queue.active === 0"
          title="一键取消所有排队与运行中的任务"
          @click="queue.cancelAll(pid)"
        >
          <Ban :size="10" />取消全部
        </AppButton>
        <span class="text-fg-4 tnum text-2xs">
          并发 {{ queue.active }} / {{ queue.state?.worker_limit ?? '—' }}
        </span>
        <span
          class="text-2xs"
          :class="queue.conn === 'open' ? 'text-st-done' : 'text-st-review'"
          :title="
            queue.conn === 'open'
              ? '实时事件已连上：任务状态会自己更新'
              : '实时事件断开了，先按「刷新」手动对齐——事件可丢失，重连后也会自动重拉一次'
          "
        >
          {{ queue.conn === 'open' ? '实时' : '未连实时' }}
        </span>
        <AppButton size="sm" variant="ghost" :disabled="queue.busy" @click="queue.load(pid)">
          <RefreshCw :size="10" />刷新
        </AppButton>
        <AppButton
          size="sm"
          variant="ghost"
          title="打开队列页：失败现场、入队时冻结的参数、优先级细调都在那儿"
          @click="openQueuePage()"
        >
          <ExternalLink :size="10" />队列页
        </AppButton>
      </template>

      <!-- 日志框的动作 -->
      <template v-if="panel.tab === 'logs'">
        <span class="border-line-1 mx-1 h-3.5 border-l" />
        <span class="text-fg-4 text-2xs">频道</span>
        <select
          v-model="logChannel"
          class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 w-20 border px-1 text-2xs outline-none"
        >
          <option value="">全部</option>
          <option v-for="c in CHANNELS" :key="c" :value="c">{{ c }}</option>
        </select>
        <AppButton
          size="sm"
          variant="ghost"
          :disabled="sys.events.length === 0"
          title="只清前端这份缓冲（最近 200 条），后端什么都不会被删"
          @click="sys.clearEvents()"
        >
          <Eraser :size="10" />清空
        </AppButton>
      </template>

      <button
        type="button"
        class="text-fg-4 hover:text-fg-1 ml-auto"
        title="收起控制台（再点状态条上的任务标识可以打开）"
        @click="panel.close()"
      >
        <ChevronDown :size="12" />
      </button>
    </div>

    <!-- 任务框 -->
    <div v-if="panel.tab === 'jobs'" class="flex min-h-0 flex-1 flex-col overflow-auto">
      <EmptyState
        v-if="!pid"
        title="还没有打开工程"
        body="任务是按工程排队的。先在项目管理页打开一个工程，这里就会显示它的生成任务。"
      />
      <template v-else>
        <ErrorPanel
          v-if="queue.lastError"
          class="m-1.5"
          :error="queue.lastError"
          @dismiss="queue.clearError()"
        />
        <EmptyState
          v-if="rows.length === 0 && breakdownRows.length === 0"
          title="队列里什么都没有"
          body="去幕流程图、幕工作台或镜头页入队一个镜头。任务会带着它的上下文一起排进来，跑完落成一个新版本。"
        />
        <table v-if="rows.length" class="w-full border-collapse text-2xs">
          <tbody>
            <template v-for="row in rows" :key="row.key">
              <!-- 一次编排合并成的那一条：进度是「第 N/M 步」，不是编出来的百分比 -->
              <tr v-if="row.batch" class="hover:bg-base-2">
                <td class="border-line-1 w-20 border-b px-1.5 py-1 align-top">
                  <AppBadge :tone="TONE[row.batch.status] ?? 'neutral'">
                    {{ JOB_STATUS_LABEL[row.batch.status] ?? row.batch.status }}
                  </AppBadge>
                </td>
                <td class="border-line-1 text-fg-2 min-w-0 border-b py-1 pr-1 align-top">
                  <button
                    class="hover:text-accent flex items-center gap-1 text-left"
                    :title="
                      opened.has(row.batch.id) ? '收起这一批的成员' : '展开看这一批里的每一条任务'
                    "
                    @click="toggle(row.batch.id)"
                  >
                    <ChevronRight
                      :size="10"
                      class="shrink-0 transition-transform"
                      :class="opened.has(row.batch.id) ? 'rotate-90' : ''"
                    />
                    <Layers :size="10" class="text-fg-4 shrink-0" />
                    <span class="text-fg-1 truncate">{{ row.batch.label }}</span>
                  </button>
                  <p class="text-fg-4">
                    {{ stepText(row.batch) }}
                    <span v-if="row.batch.running_label" class="text-accent">
                      · 正在做 {{ row.batch.running_label }}
                    </span>
                  </p>
                  <p v-if="row.batch.error" class="text-st-failed" :title="row.batch.error.detail">
                    {{ row.batch.error.title }}
                    <span class="text-fg-4">{{ row.batch.error.code }}</span>
                    <span v-if="row.batch.failed_count > 1" class="text-fg-4">
                      （共 {{ row.batch.failed_count }} 条失败）
                    </span>
                  </p>
                </td>
                <td class="border-line-1 text-fg-3 w-16 border-b py-1 pr-1 align-top">
                  {{ row.batch.kind || '一批' }}
                </td>
                <td class="border-line-1 text-fg-3 tnum w-24 border-b py-1 pr-1 align-top">
                  {{ row.batch.settled }}/{{ row.batch.total }} 步
                </td>
                <td class="border-line-1 text-fg-3 tnum w-14 border-b py-1 pr-1 align-top">
                  {{ batchElapsed(row.members) }}
                </td>
                <td class="border-line-1 w-16 border-b py-1 pr-1.5 align-top">
                  <div class="flex items-center gap-1">
                    <button
                      v-if="['queued', 'running'].includes(row.batch.status)"
                      class="text-fg-4 hover:text-st-failed"
                      title="整批取消：还没了结的一起停，已经出的版本一条都不动"
                      @click="queue.cancelBatch(pid, row.batch.id)"
                    >
                      <Ban :size="10" />
                    </button>
                    <button
                      v-if="row.batch.retryable"
                      class="text-fg-4 hover:text-accent"
                      title="整批重跑：失败与已取消的成员重新排上去（单线程一条失败会连带停掉后面全部）。已完成的一条都不重做"
                      @click="queue.retryBatch(pid, row.batch.id)"
                    >
                      <RotateCcw :size="10" />
                    </button>
                  </div>
                </td>
              </tr>
              <!-- 展开后的成员：每一条还是那条真任务，动作与零散任务完全一样 -->
              <tr
                v-for="m in row.batch && opened.has(row.batch.id) ? row.members : []"
                :key="`m-${m.id}`"
                class="hover:bg-base-2 bg-base-2/40"
              >
                <td class="border-line-1 w-20 border-b py-0.5 pr-1.5 pl-4 align-top">
                  <AppBadge :tone="TONE[m.status] ?? 'neutral'">
                    {{ JOB_STATUS_LABEL[m.status] ?? m.status }}
                  </AppBadge>
                </td>
                <td class="border-line-1 text-fg-2 min-w-0 border-b py-0.5 pr-1 align-top">
                  <button
                    class="truncate text-left"
                    :class="m.shot_id ? 'hover:text-accent' : 'cursor-default'"
                    :disabled="!m.shot_id"
                    @click="goShot(m.shot_id)"
                  >
                    <span class="text-fg-4 tnum mr-1">{{ m.batch_seq ?? '·' }}.</span>
                    {{ m.label }}
                  </button>
                  <p v-if="m.wait_reason" class="text-st-review">{{ m.wait_reason }}</p>
                  <p v-else-if="m.error" class="text-st-failed" :title="m.error.detail">
                    {{ m.error.title }} <span class="text-fg-4">{{ m.error.code }}</span>
                  </p>
                </td>
                <td class="border-line-1 text-fg-3 w-16 border-b py-0.5 pr-1 align-top">
                  {{ m.kind }}
                </td>
                <td class="border-line-1 border-b py-0.5 pr-1 align-top" />
                <td class="border-line-1 text-fg-3 tnum w-14 border-b py-0.5 pr-1 align-top">
                  {{ elapsed(m) }}
                </td>
                <td class="border-line-1 w-16 border-b py-0.5 pr-1.5 align-top">
                  <div class="flex items-center gap-1">
                    <button
                      v-if="['queued', 'waiting', 'running'].includes(m.status)"
                      class="text-fg-4 hover:text-st-failed"
                      title="只取消这一条。已经产生的版本不会被删"
                      @click="queue.cancel(pid, m.id)"
                    >
                      <Ban :size="10" />
                    </button>
                    <button
                      v-if="['failed', 'canceled'].includes(m.status)"
                      class="text-fg-4 hover:text-accent"
                      title="只重跑这一条；想把整批接着跑完请用合并那一行的重跑"
                      @click="queue.retry(pid, m.id)"
                    >
                      <RotateCcw :size="10" />
                    </button>
                  </div>
                </td>
              </tr>
              <!-- 零散任务：单个镜头的生成不属于任何编排，照旧一行一条 -->
              <tr v-if="!row.batch && row.job" class="hover:bg-base-2">
                <td class="border-line-1 w-20 border-b px-1.5 py-1 align-top">
                  <AppBadge :tone="TONE[row.job.status] ?? 'neutral'">
                    {{ JOB_STATUS_LABEL[row.job.status] ?? row.job.status }}
                  </AppBadge>
                </td>
                <td class="border-line-1 text-fg-2 min-w-0 border-b py-1 pr-1 align-top">
                  <button
                    class="truncate text-left"
                    :class="row.job.shot_id ? 'hover:text-accent' : 'cursor-default'"
                    :disabled="!row.job.shot_id"
                    @click="goShot(row.job.shot_id)"
                  >
                    {{ row.job.label }}
                  </button>
                  <p v-if="row.job.wait_reason" class="text-st-review">{{ row.job.wait_reason }}</p>
                  <p v-else-if="row.job.error" class="text-st-failed" :title="row.job.error.detail">
                    {{ row.job.error.title }}
                    <span class="text-fg-4">{{ row.job.error.code }}</span>
                  </p>
                </td>
                <td class="border-line-1 text-fg-3 w-16 border-b py-1 pr-1 align-top">
                  {{ row.job.kind }}
                </td>
                <td class="border-line-1 text-fg-4 w-24 border-b py-1 pr-1 align-top">
                  <!-- 刻意留空：ComfyUI 不回显进度，这里没有一个真数字可写 -->
                  {{ row.job.status === 'running' ? '正在跑' : '' }}
                </td>
                <td class="border-line-1 text-fg-3 tnum w-14 border-b py-1 pr-1 align-top">
                  {{ elapsed(row.job) }}
                </td>
                <td class="border-line-1 w-16 border-b py-1 pr-1.5 align-top">
                  <div class="flex items-center gap-1">
                    <button
                      v-if="['queued', 'waiting'].includes(row.job.status)"
                      class="text-fg-4 hover:text-accent"
                      title="提到队首（把优先级调到当前最小值之前）"
                      @click="bump(row.job)"
                    >
                      <ArrowUp :size="10" />
                    </button>
                    <button
                      v-if="['queued', 'waiting', 'running'].includes(row.job.status)"
                      class="text-fg-4 hover:text-st-failed"
                      title="取消这个任务。已经产生的版本不会被删"
                      @click="queue.cancel(pid, row.job.id)"
                    >
                      <Ban :size="10" />
                    </button>
                    <button
                      v-if="['failed', 'canceled'].includes(row.job.status)"
                      class="text-fg-4 hover:text-accent"
                      title="沿用原参数重跑；旧版本一条都不会被覆盖"
                      @click="queue.retry(pid, row.job.id)"
                    >
                      <RotateCcw :size="10" />
                    </button>
                    <button
                      v-if="['failed', 'canceled', 'done'].includes(row.job.status)"
                      class="text-fg-4 hover:text-st-failed"
                      title="删除这条任务记录"
                      @click="queue.deleteJob(pid, row.job.id)"
                    >
                      <Trash2 :size="10" />
                    </button>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <ul v-if="breakdownRows.length" class="divide-line-1 divide-y">
          <li
            v-for="task in breakdownRows"
            :key="task.id"
            class="hover:bg-base-2 flex items-start gap-1.5 px-1.5 py-1 text-2xs"
          >
            <AppBadge
              :tone="
                task.status === 'running' ? 'accent' : task.status === 'failed' ? 'fail' : 'ok'
              "
            >
              {{
                task.status === 'running' ? '正在跑' : task.status === 'failed' ? '失败' : '完成'
              }}
            </AppBadge>
            <span class="text-fg-2 min-w-0 flex-1">
              <span class="text-fg-1">{{ task.title }}</span>
              <span class="text-fg-4 ml-1">{{ task.detail }}</span>
              <span v-if="task.error" class="text-st-failed ml-1">{{ task.error }}</span>
            </span>
            <Wand2 v-if="task.status === 'running'" :size="10" class="text-accent shrink-0" />
          </li>
        </ul>
      </template>
    </div>

    <!-- 日志框 -->
    <div v-else class="flex min-h-0 flex-1 flex-col overflow-auto">
      <EmptyState
        v-if="logs.length === 0"
        title="还没有事件"
        :body="
          logChannel
            ? `最近 200 条里没有 ${logChannel} 频道的事件。换个频道或看「全部」。`
            : '生成、入队、资产登记都会在这里留一行。事件可丢失，所以它是线索而不是账本——真相在各页面的 REST 数据里。'
        "
      />
      <ul v-else class="divide-line-1 divide-y">
        <li
          v-for="(ev, i) in logs"
          :key="`${ev.ts}-${i}`"
          class="hover:bg-base-2 flex items-baseline gap-1.5 px-1.5 py-0.5 text-2xs"
        >
          <span class="text-fg-4 tnum shrink-0">{{ timeOf(ev) }}</span>
          <AppBadge :tone="ev.channel === 'error' ? 'fail' : 'neutral'">{{ ev.channel }}</AppBadge>
          <span class="shrink-0" :class="ev.channel === 'error' ? 'text-st-failed' : 'text-fg-2'">
            {{ ev.event }}
          </span>
          <span class="text-fg-4 min-w-0 flex-1 truncate" :title="payloadOf(ev)">
            {{ payloadOf(ev) }}
          </span>
        </li>
      </ul>
    </div>
  </section>
</template>
