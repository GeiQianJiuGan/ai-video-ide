<script setup lang="ts">
/**
 * 底部状态条：后端 / ComfyUI / FFmpeg / LLM / WS 连接状态 + 任务标识。常驻，24px 高。
 *
 * 任务标识是**底部控制台的入口**（队列不再是左栏里的一个菜单项）：它一句话说清
 * 现在有没有事在跑，点一下从下面升起控制台的任务框。事件计数同理，落到日志框。
 * 计数之所以在任何页面都是真的，是因为 WS 订阅挂在常驻的控制台上而不是队列页。
 */
import { computed } from 'vue'
import { ListVideo } from '@lucide/vue'
import { useConsoleStore } from '@/stores/console'
import { useProjectStore } from '@/stores/project'
import { useQueueStore } from '@/stores/queue'
import { useSystemStore } from '@/stores/system'

const sys = useSystemStore()
const proj = useProjectStore()
const queue = useQueueStore()
const panel = useConsoleStore()

const backendLabel = computed(() =>
  sys.health ? `后端 v${sys.health.version} · schema ${sys.health.schema_version}` : '后端未连接',
)

const wsLabel = computed(
  () =>
    ({ open: '实时通道 已连接', connecting: '实时通道 连接中', closed: '实时通道 断开' })[
      sys.connState
    ],
)

/** 队列现状的一句话：跑几个、排几个、失败几个。都没有就是「空闲」。 */
const taskLabel = computed(() => {
  const c = queue.counts
  const parts: string[] = []
  const running = c.running ?? queue.active
  if (running) parts.push(`跑 ${running}`)
  const pending = (c.queued ?? 0) + (c.waiting ?? 0)
  if (pending) parts.push(`排 ${pending}`)
  if (queue.failed.length) parts.push(`失败 ${queue.failed.length}`)
  const breakdownRunning = queue.breakdownTasks.filter((task) => task.status === 'running').length
  const breakdownFailed = queue.breakdownTasks.filter((task) => task.status === 'failed').length
  if (breakdownRunning) parts.push(`拆解 ${breakdownRunning}`)
  if (breakdownFailed) parts.push(`拆解失败 ${breakdownFailed}`)
  if (queue.paused) parts.push('已暂停')
  return parts.length ? parts.join(' · ') : '空闲'
})

/** 有失败标红、有在跑用「运行中」的颜色、暂停用复核色，其余静默。 */
const taskColor = computed(() => {
  if (queue.failed.length || queue.breakdownTasks.some((task) => task.status === 'failed')) {
    return 'text-st-failed'
  }
  if (queue.paused) return 'text-st-review'
  if (!queue.idle || queue.breakdownTasks.some((task) => task.status === 'running')) {
    return 'text-st-running'
  }
  return 'text-fg-3'
})

function depColor(ok: boolean): string {
  return ok ? 'bg-st-done' : 'bg-st-failed'
}

const DEP_LABEL: Record<string, string> = {
  ffmpeg: 'FFmpeg',
  comfyui: 'ComfyUI',
  llm: 'LLM',
}
</script>

<template>
  <footer
    class="bg-base-1 border-line-1 text-fg-3 flex h-statusbar shrink-0 items-center gap-3 border-t px-2 text-2xs"
  >
    <span class="flex items-center gap-1.5">
      <span class="size-1.5 rounded-full" :class="sys.health ? 'bg-st-done' : 'bg-st-failed'" />
      {{ backendLabel }}
    </span>

    <span
      v-for="dep in sys.deps"
      :key="dep.name"
      class="flex items-center gap-1.5"
      :title="dep.detail + (dep.hint ? ` — ${dep.hint}` : '')"
    >
      <span class="size-1.5 rounded-full" :class="depColor(dep.ok)" />
      {{ DEP_LABEL[dep.name] ?? dep.name }}
    </span>

    <!-- schema 升级必须被看见：工程文件被改写过，用户有权知道 -->
    <button
      v-if="proj.migration"
      type="button"
      class="text-st-running hover:text-fg-1 flex items-center gap-1"
      title="点击关闭这条提示"
      @click="proj.dismissMigration()"
    >
      «{{ proj.migration.projectName }}» 已升级 schema {{ proj.migration.from }} →
      {{ proj.migration.to }}
    </button>

    <span class="ml-auto flex items-center gap-1.5" :title="wsLabel">
      <span
        class="size-1.5 rounded-full"
        :class="sys.connState === 'open' ? 'bg-st-done' : 'bg-st-running'"
      />
      {{ wsLabel }}
    </span>

    <!-- 任务标识：控制台的入口。点一下升起任务框，再点收起 -->
    <button
      type="button"
      class="hover:text-fg-1 flex items-center gap-1"
      :class="taskColor"
      :title="`生成任务：${taskLabel}。点击开合底部控制台的任务框（Ctrl + \`）`"
      @click="panel.openWith('jobs')"
    >
      <ListVideo :size="11" />
      <span class="tnum">任务 {{ taskLabel }}</span>
    </button>

    <button
      type="button"
      class="tnum hover:text-fg-1"
      title="点击开合底部控制台的日志框（最近 200 条事件；事件可丢失，只当线索看）"
      @click="panel.openWith('logs')"
    >
      事件 {{ sys.events.length }}
    </button>
  </footer>
</template>
