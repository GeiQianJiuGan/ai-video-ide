<script setup lang="ts">
/** 底部状态条：后端 / ComfyUI / FFmpeg / LLM / WS 连接状态。常驻，24px 高。 */
import { computed } from 'vue'
import { useProjectStore } from '@/stores/project'
import { useSystemStore } from '@/stores/system'

const sys = useSystemStore()
const proj = useProjectStore()

const backendLabel = computed(() =>
  sys.health ? `后端 v${sys.health.version} · schema ${sys.health.schema_version}` : '后端未连接',
)

const wsLabel = computed(
  () =>
    ({ open: '实时通道 已连接', connecting: '实时通道 连接中', closed: '实时通道 断开' })[
      sys.connState
    ],
)

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

    <span class="tnum">事件 {{ sys.events.length }}</span>
  </footer>
</template>
