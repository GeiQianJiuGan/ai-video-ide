<script setup lang="ts">
/**
 * 项目列表。M0 阶段后端还没有 /projects，这里展示地基连通状态，
 * 并把 M1 将要接入的接口列出来，避免出现「看起来能用其实是假的」界面。
 */
import { computed } from 'vue'
import { RefreshCw } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import { useSystemStore } from '@/stores/system'

const sys = useSystemStore()
const connected = computed(() => sys.health !== null)
</script>

<template>
  <div class="grid min-h-0 flex-1 grid-cols-2 gap-2 p-2">
    <AppPanel title="项目">
      <template #actions>
        <AppButton size="sm" variant="ghost" @click="sys.refresh()">
          <RefreshCw :size="11" />刷新
        </AppButton>
      </template>
      <div class="text-fg-3 space-y-2 p-3 text-xs">
        <p v-if="connected">
          后端已连接，但 <code class="text-fg-2">GET /projects</code> 尚未实现（M1）。
        </p>
        <p v-else class="text-st-failed">
          后端未连接。请先启动 backend：<br />
          <code class="text-fg-2"
            >cd backend &amp;&amp; AIVS_PORT=8765 .venv/Scripts/python -m app.main</code
          >
        </p>
        <ul class="text-fg-4 space-y-0.5">
          <li>M1 将接入：新建工程目录 / 打开工程 / 最近打开 / 工程设置</li>
          <li>工程目录含 project.aivs.json + project.db + assets/ + generations/</li>
        </ul>
      </div>
    </AppPanel>

    <AppPanel title="地基自检">
      <dl class="divide-line-1 divide-y text-xs">
        <div class="flex items-center gap-2 px-3 py-1.5">
          <dt class="text-fg-3 w-24 shrink-0">后端</dt>
          <dd class="text-fg-1 tnum">
            {{ sys.health ? `${sys.health.app} v${sys.health.version}` : '未连接' }}
          </dd>
        </div>
        <div v-for="dep in sys.deps" :key="dep.name" class="flex gap-2 px-3 py-1.5">
          <dt class="text-fg-3 w-24 shrink-0">{{ dep.name }}</dt>
          <dd class="min-w-0">
            <span :class="dep.ok ? 'text-st-done' : 'text-st-failed'">{{ dep.detail }}</span>
            <p v-if="dep.hint" class="text-fg-4 mt-0.5">{{ dep.hint }}</p>
          </dd>
        </div>
        <div v-if="sys.lastError" class="px-3 py-1.5">
          <dt class="text-st-failed">{{ sys.lastError.title }}</dt>
          <dd class="text-fg-3 mt-0.5">{{ sys.lastError.detail }}</dd>
          <ul class="text-fg-4 mt-1 space-y-0.5">
            <li v-for="s in sys.lastError.suggestions" :key="s">· {{ s }}</li>
          </ul>
        </div>
      </dl>
    </AppPanel>
  </div>
</template>
