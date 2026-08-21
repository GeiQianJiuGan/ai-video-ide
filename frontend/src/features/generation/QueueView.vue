<script setup lang="ts">
/**
 * 生成队列（Step 7 的前端）。
 *
 * 队列的**常驻界面是底部控制台**（`app/layout/ConsolePanel.vue` 的任务框）：那里能看
 * 谁在跑、按暂停、重试失败。这一页是「点开细看」的那一层——失败现场、入队时冻结的
 * 参数、优先级细调，屏幕够大才摆得下，所以它不进左栏导航。
 *
 * 队列页要回答的只有三个问题：谁在跑、谁在等（等谁）、失败的为什么失败。
 *
 * 四个刻意的设计：
 *   1. **WS 订阅不归它**。订阅挂在常驻的控制台上，这一页只 `load()` 做一次对齐——
 *      以前是这里 `onUnmounted` 时 disconnect，于是一离开页面实时通道就断了。
 *   2. **等待要能解释**。`waiting` 的任务把 `wait_reason` 写在行里——
 *      「等上游 Shot 14 出当前版本」和「卡住了」在界面上必须长得不一样。
 *   3. **失败现场是一整块，不是一行红字**。右栏放选中任务的结构化错误四要素，
 *      建议一条条列出来，旁边就是「重试」——看见原因的地方就能动手。
 *   4. **暂停不等于取消**。工具栏那句提示写明：已经在跑的照常跑完。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowUp, Ban, Pause, Play, RefreshCw, RotateCcw } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import { JOB_STATUS, JOB_STATUS_LABEL, type Job } from '@/shared/api/generation'
import { useQueueStore } from '@/stores/queue'

const route = useRoute()
const router = useRouter()
const queue = useQueueStore()

const pid = computed(() => String(route.params.pid ?? ''))
const selectedId = ref('')
const statusFilter = ref('')

const rows = computed(() =>
  statusFilter.value ? queue.jobs.filter((j) => j.status === statusFilter.value) : queue.jobs,
)
const selected = computed<Job | null>(
  () => queue.jobs.find((j) => j.id === selectedId.value) ?? null,
)

/** 状态点的颜色：跑/等/失败三种要一眼能分开。 */
const TONE: Record<string, 'neutral' | 'accent' | 'ok' | 'warn' | 'fail'> = {
  queued: 'neutral',
  waiting: 'warn',
  running: 'accent',
  done: 'ok',
  failed: 'fail',
  canceled: 'neutral',
  paused: 'warn',
}

/** 只做一次全量对齐；实时订阅由底部控制台常驻持有，离开这一页不该把它带走。 */
function start(): void {
  if (!pid.value) return
  void queue.load(pid.value).catch(() => {})
}

onMounted(start)
watch(pid, start)

/** 选中的任务被清掉后不要留一块空的失败现场。 */
watch(rows, (list) => {
  if (selectedId.value && !list.some((j) => j.id === selectedId.value)) selectedId.value = ''
})

function elapsed(job: Job): string {
  if (!job.started_at) return '—'
  const end = job.finished_at ? Date.parse(job.finished_at) : Date.now()
  const sec = Math.max(0, Math.round((end - Date.parse(job.started_at)) / 1000))
  return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m${sec % 60}s`
}

function goShot(shotId: string): void {
  void router.push({ name: 'shot', params: { pid: pid.value, sid: shotId } })
}

/** 提到队首：比当前最小 priority 再小 1（后端按 priority 升序取）。 */
async function bump(job: Job): Promise<void> {
  const min = Math.min(...queue.jobs.map((j) => j.priority), job.priority)
  await queue.setPriority(pid.value, job.id, min - 1)
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />
    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1.5 border-b px-2">
      <AppButton
        v-if="!queue.paused"
        size="sm"
        variant="primary"
        :disabled="queue.busy"
        title="停止取新任务。已经在跑的不会中断，也不会被取消"
        @click="queue.pause(pid)"
      >
        <Pause :size="10" />暂停队列
      </AppButton>
      <AppButton
        v-else
        size="sm"
        variant="primary"
        :disabled="queue.busy"
        title="继续取新任务"
        @click="queue.resume(pid)"
      >
        <Play :size="10" />继续队列
      </AppButton>
      <AppButton
        size="sm"
        :disabled="queue.busy || queue.failed.length === 0"
        title="沿用原参数重跑所有失败任务；旧版本一条都不会被覆盖"
        @click="queue.retryFailed(pid)"
      >
        <RotateCcw :size="10" />重试失败（{{ queue.failed.length }}）
      </AppButton>
      <span class="text-fg-4 text-2xs">筛选</span>
      <select
        v-model="statusFilter"
        class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 w-24 border px-1 text-2xs outline-none"
      >
        <option value="">全部</option>
        <option v-for="s in JOB_STATUS" :key="s" :value="s">{{ JOB_STATUS_LABEL[s] }}</option>
      </select>
      <span class="text-fg-4 tnum text-2xs">
        并发 {{ queue.active }} / {{ queue.state?.worker_limit ?? '—' }}
        <template v-for="s in JOB_STATUS" :key="s">
          <template v-if="queue.counts[s]">
            · {{ JOB_STATUS_LABEL[s] }} {{ queue.counts[s] }}
          </template>
        </template>
      </span>
      <span
        class="ml-auto text-2xs"
        :class="queue.conn === 'open' ? 'text-st-done' : 'text-st-review'"
        :title="
          queue.conn === 'open'
            ? '实时事件已连上：任务状态会自己更新'
            : '实时事件断开了，先用「刷新」手动对齐——事件可丢失，重连后也会自动重拉一次'
        "
      >
        {{ queue.conn === 'open' ? '实时' : '未连实时' }}
      </span>
      <AppButton size="sm" variant="ghost" :disabled="queue.busy" @click="queue.load(pid)">
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>

    <ErrorPanel
      v-if="queue.lastError"
      class="mx-2 mt-2"
      :error="queue.lastError"
      @dismiss="queue.clearError()"
    />

    <div class="flex min-h-0 flex-1 gap-2 p-2">
      <AppPanel title="任务列表" class="min-w-0 flex-1">
        <template #actions>
          <AppBadge v-if="queue.paused" tone="warn">队列已暂停</AppBadge>
          <AppBadge v-else-if="queue.idle" tone="neutral">空闲</AppBadge>
        </template>
        <div class="p-2">
          <EmptyState
            v-if="rows.length === 0"
            :title="statusFilter ? '这个状态下没有任务' : '队列里什么都没有'"
            :body="
              statusFilter
                ? '换个状态看看，或者把筛选清成「全部」。'
                : '去镜头页或分镜页入队一个镜头。任务会带着它的上下文与 Workflow 一起排进来，跑完落成一个新版本。'
            "
          />
          <table v-else class="w-full border-collapse text-2xs">
            <thead>
              <tr class="text-fg-4 text-left">
                <th class="border-line-1 border-b py-1 pr-1 font-normal">状态</th>
                <th class="border-line-1 border-b py-1 pr-1 font-normal">镜头</th>
                <th class="border-line-1 border-b py-1 pr-1 font-normal">能力</th>
                <th class="border-line-1 border-b py-1 pr-1 font-normal">进度</th>
                <th class="border-line-1 tnum border-b py-1 pr-1 font-normal">优先级</th>
                <th class="border-line-1 tnum border-b py-1 pr-1 font-normal">耗时</th>
                <th class="border-line-1 border-b py-1 font-normal">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="job in rows"
                :key="job.id"
                class="hover:bg-base-2 cursor-pointer"
                :class="job.id === selectedId ? 'bg-accent-dim/40' : ''"
                @click="selectedId = job.id"
              >
                <td class="border-line-1 border-b py-1 pr-1 align-top">
                  <AppBadge :tone="TONE[job.status] ?? 'neutral'">
                    {{ JOB_STATUS_LABEL[job.status] ?? job.status }}
                  </AppBadge>
                  <span v-if="job.attempt > 1" class="text-fg-4 tnum ml-1">
                    第 {{ job.attempt }} 次
                  </span>
                </td>
                <td class="border-line-1 text-fg-2 border-b py-1 pr-1 align-top">
                  <button class="hover:text-accent text-left" @click.stop="goShot(job.shot_id)">
                    {{ job.shot_index_no ?? '?' }}. {{ job.shot_title ?? job.shot_id }}
                  </button>
                  <p v-if="job.wait_reason" class="text-st-review">{{ job.wait_reason }}</p>
                </td>
                <td class="border-line-1 text-fg-3 border-b py-1 pr-1 align-top">{{ job.kind }}</td>
                <td class="border-line-1 border-b py-1 pr-1 align-top">
                  <div class="bg-base-3 h-1 w-20">
                    <div
                      class="bg-accent h-1"
                      :style="{ width: `${Math.round(job.progress * 100)}%` }"
                    />
                  </div>
                  <span class="text-fg-4 tnum">{{ Math.round(job.progress * 100) }}%</span>
                </td>
                <td class="border-line-1 text-fg-3 tnum border-b py-1 pr-1 align-top">
                  {{ job.priority }}
                </td>
                <td class="border-line-1 text-fg-3 tnum border-b py-1 pr-1 align-top">
                  {{ elapsed(job) }}
                </td>
                <td class="border-line-1 border-b py-1 align-top">
                  <div class="flex items-center gap-1">
                    <button
                      v-if="['queued', 'waiting'].includes(job.status)"
                      class="text-fg-4 hover:text-accent"
                      title="提到队首（把优先级调到当前最小值之前）"
                      @click.stop="bump(job)"
                    >
                      <ArrowUp :size="10" />
                    </button>
                    <button
                      v-if="['queued', 'waiting', 'running'].includes(job.status)"
                      class="text-fg-4 hover:text-st-failed"
                      title="取消这个任务。已经产生的版本不会被删"
                      @click.stop="queue.cancel(pid, job.id)"
                    >
                      <Ban :size="10" />
                    </button>
                    <button
                      v-if="['failed', 'canceled'].includes(job.status)"
                      class="text-fg-4 hover:text-accent"
                      title="沿用原参数重跑"
                      @click.stop="queue.retry(pid, job.id)"
                    >
                      <RotateCcw :size="10" />
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </AppPanel>

      <AppPanel title="失败现场" class="w-72 shrink-0">
        <div class="space-y-2 p-2">
          <EmptyState
            v-if="!selected"
            title="选一个任务"
            body="左边点一行，这里显示它的参数与失败现场：错误码、原因、下一步怎么做。"
          />
          <template v-else>
            <div>
              <p class="text-fg-1 text-2xs">
                {{ selected.shot_index_no ?? '?' }}. {{ selected.shot_title ?? selected.shot_id }}
              </p>
              <p class="text-fg-4 text-2xs">
                {{ JOB_STATUS_LABEL[selected.status] ?? selected.status }} · {{ selected.kind }} ·
                第 {{ selected.attempt }} 次尝试
              </p>
              <p v-if="selected.depends_on" class="text-st-review mt-0.5 text-2xs">
                {{ selected.wait_reason ?? `等上游任务 ${selected.depends_on}` }}
              </p>
            </div>

            <div v-if="selected.error" class="border-st-failed/40 bg-base-2 border p-1.5">
              <p class="text-st-review text-2xs">{{ selected.error.title }}</p>
              <p class="text-fg-2 mt-0.5 text-2xs">{{ selected.error.detail }}</p>
              <ul
                v-if="selected.error.suggestions.length"
                class="text-fg-2 mt-1 space-y-px text-2xs"
              >
                <li v-for="s in selected.error.suggestions" :key="s">· {{ s }}</li>
              </ul>
              <p class="text-fg-4 mt-1 text-2xs">{{ selected.error.code }}</p>
              <AppButton
                size="sm"
                class="mt-1"
                :disabled="queue.busy"
                title="沿用原参数重跑；旧版本一条都不会被覆盖"
                @click="queue.retry(pid, selected.id)"
              >
                <RotateCcw :size="10" />重试这一个
              </AppButton>
            </div>
            <p v-else class="text-fg-4 text-2xs">这个任务没有失败现场。</p>

            <div class="border-line-1 border-t pt-2">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">入队时冻结的参数</p>
              <pre
                class="text-fg-3 bg-base-2 border-line-1 mt-1 overflow-auto border p-1 text-2xs"
                >{{ JSON.stringify(selected.params, null, 2) }}</pre>
              <p class="text-fg-4 mt-1 text-2xs">
                这些值是入队那一刻定下的，之后改镜头也不影响这条任务。
              </p>
            </div>

            <div class="border-line-1 border-t pt-2">
              <p class="text-fg-4 text-2xs">
                Workflow：{{ selected.workflow_id ?? '按能力取默认' }}<br />
                版本：{{ selected.version_id ?? '还没产出' }}<br />
                入队 {{ selected.created_at.slice(0, 16) }}
              </p>
              <AppButton
                size="sm"
                variant="ghost"
                class="mt-1"
                title="去这条镜头的编辑器看上下文与版本轨"
                @click="goShot(selected.shot_id)"
              >
                打开镜头
              </AppButton>
            </div>
          </template>
        </div>
      </AppPanel>
    </div>
  </div>
</template>
