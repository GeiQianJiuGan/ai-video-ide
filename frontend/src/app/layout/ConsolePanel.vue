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
 *   2. **任务框按「要不要管」排序**，不按入队顺序：正在跑 → 排队 / 等上游 → 失败
 *      → 其余。控制台只有两百来像素高，最该看见的必须在最上面。
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
  Eraser,
  ExternalLink,
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
import { JOB_STATUS_LABEL, type Job } from '@/shared/api/generation'
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

/** 排序权重：越小越靠上。同权重之间保持后端给的顺序（sort 是稳定的）。 */
const RANK: Record<string, number> = { running: 0, waiting: 1, queued: 1, failed: 2 }

const rows = computed(() =>
  [...queue.jobs].sort((a, b) => (RANK[a.status] ?? 3) - (RANK[b.status] ?? 3)),
)

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

/** 提到队首：比当前最小 priority 再小 1（后端按 priority 升序取）。 */
async function bump(job: Job): Promise<void> {
  const min = Math.min(...queue.jobs.map((j) => j.priority), job.priority)
  await queue.setPriority(pid.value, job.id, min - 1)
}

function goShot(shotId: string): void {
  if (!pid.value) return
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
        <span v-if="queue.jobs.length + queue.breakdownTasks.length" class="tnum text-fg-4">
          {{ queue.jobs.length + queue.breakdownTasks.length }}
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
            <tr v-for="job in rows" :key="job.id" class="hover:bg-base-2">
              <td class="border-line-1 w-20 border-b px-1.5 py-1 align-top">
                <AppBadge :tone="TONE[job.status] ?? 'neutral'">
                  {{ JOB_STATUS_LABEL[job.status] ?? job.status }}
                </AppBadge>
              </td>
              <td class="border-line-1 text-fg-2 min-w-0 border-b py-1 pr-1 align-top">
                <button class="hover:text-accent truncate text-left" @click="goShot(job.shot_id)">
                  {{ job.shot_index_no ?? '?' }}. {{ job.shot_title ?? job.shot_id }}
                </button>
                <p v-if="job.wait_reason" class="text-st-review">{{ job.wait_reason }}</p>
                <p v-else-if="job.error" class="text-st-failed" :title="job.error.detail">
                  {{ job.error.title }}
                  <span class="text-fg-4">{{ job.error.code }}</span>
                </p>
              </td>
              <td class="border-line-1 text-fg-3 w-16 border-b py-1 pr-1 align-top">
                {{ job.kind }}
              </td>
              <td class="border-line-1 w-24 border-b py-1 pr-1 align-top">
                <div class="bg-base-3 h-1 w-16">
                  <div
                    class="bg-accent h-1"
                    :style="{ width: `${Math.round(job.progress * 100)}%` }"
                  />
                </div>
                <span class="text-fg-4 tnum">{{ Math.round(job.progress * 100) }}%</span>
              </td>
              <td class="border-line-1 text-fg-3 tnum w-14 border-b py-1 pr-1 align-top">
                {{ elapsed(job) }}
              </td>
              <td class="border-line-1 w-16 border-b py-1 pr-1.5 align-top">
                <div class="flex items-center gap-1">
                  <button
                    v-if="['queued', 'waiting'].includes(job.status)"
                    class="text-fg-4 hover:text-accent"
                    title="提到队首（把优先级调到当前最小值之前）"
                    @click="bump(job)"
                  >
                    <ArrowUp :size="10" />
                  </button>
                  <button
                    v-if="['queued', 'waiting', 'running'].includes(job.status)"
                    class="text-fg-4 hover:text-st-failed"
                    title="取消这个任务。已经产生的版本不会被删"
                    @click="queue.cancel(pid, job.id)"
                  >
                    <Ban :size="10" />
                  </button>
                  <button
                    v-if="['failed', 'canceled'].includes(job.status)"
                    class="text-fg-4 hover:text-accent"
                    title="沿用原参数重跑；旧版本一条都不会被覆盖"
                    @click="queue.retry(pid, job.id)"
                  >
                    <RotateCcw :size="10" />
                  </button>
                  <button
                    v-if="['failed', 'canceled', 'done'].includes(job.status)"
                    class="text-fg-4 hover:text-st-failed"
                    title="删除这条任务记录"
                    @click="queue.deleteJob(pid, job.id)"
                  >
                    <Trash2 :size="10" />
                  </button>
                </div>
              </td>
            </tr>
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
