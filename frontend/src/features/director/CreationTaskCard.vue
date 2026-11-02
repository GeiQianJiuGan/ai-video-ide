<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { creationApi, type CreationGoal, type CreationTask } from '@/shared/api/creation'
import type { DirectorAttachment } from '@/shared/api/director'
import { loadProductionMode, productionModeForRoute } from '@/app/productionNavigation'
import { FRAME_ROLE_LABEL } from '@/shared/api/frames'

const props = defineProps<{ pid: string }>()
const emit = defineEmits<{ applied: []; settled: [] }>()
const route = useRoute()
const task = ref<CreationTask | null>(null)
const error = ref('')
const pollError = ref('')
let revision = 0
const busy = ref(false)
const working = computed(
  () => busy.value || ['running', 'applying'].includes(task.value?.status || ''),
)
const labels: Record<string, string> = {
  running: '创作中',
  applying: '正在保存',
  paused: '已暂停',
  failed: '待恢复',
  review: '待审阅',
  completed: '已完成',
  discarded: '草稿已丢弃',
}
let published = ''
let settled = ''
let polling = false
let stopped = false
function accept(row: CreationTask | null) {
  task.value = row
  if (
    row &&
    ['review', 'failed', 'completed'].includes(row.status) &&
    settled !== `${row.id}:${row.status}`
  ) {
    settled = `${row.id}:${row.status}`
    emit('settled')
  }
  if (row?.status === 'completed' && published !== row.id) {
    published = row.id
    emit('applied')
  }
}
async function refresh() {
  if (polling || busy.value || !props.pid) return
  polling = true
  const pid = props.pid
  const rev = revision
  try {
    const out = await creationApi.latest(pid)
    if (!stopped && pid === props.pid && rev === revision) {
      accept(out)
      pollError.value = ''
    }
  } catch (e) {
    if (pid === props.pid) pollError.value = e instanceof Error ? e.message : String(e)
  } finally {
    polling = false
  }
}
async function start(
  message: string,
  goal: CreationGoal,
  attachments: DirectorAttachment[],
): Promise<boolean> {
  if (working.value) return false
  revision++
  busy.value = true
  error.value = ''
  const pid = props.pid
  try {
    const explicit = route.query.production
    const mode =
      productionModeForRoute(String(route.name || '')) ||
      (explicit === 'frames' || explicit === 'direct' ? explicit : loadProductionMode(pid))
    const out = await creationApi.start(pid, message, goal, mode, attachments)
    if (pid === props.pid) accept(out)
    return true
  } catch (e) {
    error.value =
      e instanceof Error ? e.message + ('detail' in e ? `：${e.detail}` : '') : String(e)
    return false
  } finally {
    busy.value = false
  }
}
async function control(action: 'pause' | 'resume' | 'apply' | 'discard') {
  if (!task.value || busy.value) return
  revision++
  busy.value = true
  error.value = ''
  const pid = props.pid
  try {
    const out = await creationApi.control(pid, task.value.id, action)
    if (pid === props.pid) accept(out)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
watch(
  () => props.pid,
  () => {
    revision++
    task.value = null
    error.value = ''
    pollError.value = ''
    void refresh()
  },
  { immediate: true },
)
const timer = window.setInterval(() => {
  void refresh()
}, 2000)
onBeforeUnmount(() => {
  stopped = true
  window.clearInterval(timer)
})
defineExpose({ start, working })
</script>

<template>
  <section
    v-if="task || error || pollError"
    class="border-line-1 bg-base-2 mx-2 my-2 max-h-[45%] shrink-0 overflow-auto rounded border p-2 text-xs"
    aria-label="创作任务"
  >
    <p v-if="error || pollError" role="alert" class="text-st-failed">{{ error || pollError }}</p>
    <template v-if="task">
      <div class="flex items-center justify-between gap-2">
        <strong>{{ task.state.goal === 'screenplay' ? '完整剧本' : '完整分镜' }}</strong
        ><span>{{ labels[task.status] }}</span>
      </div>
      <p class="text-fg-3 mt-1">
        {{ task.state.mode === 'frames' ? '制帧制作' : '直接制作' }} · 进度自动保存
      </p>
      <p class="text-fg-2 my-1" role="status">{{ task.state.message }}</p>
      <p v-if="task.state.outlines" class="text-fg-3">
        {{ task.state.scenes.length }} / {{ task.state.outlines.length }} 幕已完成 ·
        {{ task.state.scenes.reduce((n, s) => n + s.shots.length, 0) }} 个镜头
      </p>
      <p v-if="task.state.error" class="text-st-failed mt-1">{{ task.state.error }}</p>
      <details v-if="task.state.draft" class="my-2">
        <summary class="cursor-pointer text-accent">查看完整草稿</summary>
        <div class="mt-2 max-h-72 overflow-auto whitespace-pre-wrap text-fg-2">
          {{ task.state.draft.screenplay_md }}
          <div v-for="(scene, i) in task.state.scenes" :key="i" class="mt-3">
            <strong>{{ scene.title }}</strong>
            <div v-for="(shot, j) in scene.shots" :key="j" class="mt-2">
              <p>{{ j + 1 }}. {{ shot.title }} — {{ shot.description }}</p>
              <template v-if="task.state.mode === 'frames'">
                <p v-if="shot.video_prompt" class="mt-1">视频提示词：{{ shot.video_prompt }}</p>
                <div v-for="(frame, k) in shot.frames" :key="k" class="ml-2 mt-1">
                  <strong>
                    画面 {{ k + 1 }} · {{ frame.title }}
                    {{ frame.role ? `（${FRAME_ROLE_LABEL[frame.role]}）` : '' }}
                  </strong>
                  <p v-if="frame.story">对应剧情：{{ frame.story }}</p>
                  <p>图片提示词：{{ frame.prompt }}</p>
                </div>
              </template>
            </div>
          </div>
        </div>
      </details>
      <div class="mt-2 flex flex-wrap gap-2">
        <button
          v-if="['running', 'applying'].includes(task.status)"
          :disabled="busy"
          class="text-accent"
          @click="control('pause')"
        >
          暂停
        </button>
        <button
          v-if="['paused', 'failed'].includes(task.status)"
          :disabled="busy"
          class="text-accent"
          @click="control('resume')"
        >
          从检查点恢复
        </button>
        <button
          v-if="task.status === 'review'"
          :disabled="busy"
          class="text-accent"
          @click="control('apply')"
        >
          采用完整结果
        </button>
        <button
          v-if="['review', 'paused', 'failed'].includes(task.status)"
          :disabled="busy"
          class="text-fg-3"
          @click="control('discard')"
        >
          丢弃未采用草稿
        </button>
      </div>
    </template>
  </section>
</template>
