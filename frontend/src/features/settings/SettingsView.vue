<script setup lang="ts">
/** 设置页：外部依赖状态与修复建议。M0 只读，M1 起支持编辑并回写后端。 */
import { RefreshCw } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import StatusDot from '@/shared/ui/StatusDot.vue'
import { useSystemStore } from '@/stores/system'

const sys = useSystemStore()

const DEP_TITLE: Record<string, string> = {
  ffmpeg: 'FFmpeg — 抽帧 / 代理转码 / 导出',
  comfyui: 'ComfyUI — 视频与图像生成',
  llm: 'LLM — AI Director（可选，非必需）',
}
</script>

<template>
  <div class="min-h-0 flex-1 overflow-auto p-2">
    <AppPanel title="外部依赖">
      <template #actions>
        <AppButton size="sm" variant="ghost" @click="sys.refresh()">
          <RefreshCw :size="11" />重新探测
        </AppButton>
      </template>
      <ul class="divide-line-1 divide-y">
        <li v-for="dep in sys.deps" :key="dep.name" class="px-3 py-2">
          <div class="flex items-center gap-2">
            <StatusDot :status="dep.ok ? 'completed' : 'failed'" />
            <span class="text-fg-1 text-xs">{{ DEP_TITLE[dep.name] ?? dep.name }}</span>
          </div>
          <p class="text-fg-2 mt-1 pl-4 text-xs">{{ dep.detail }}</p>
          <p v-if="dep.hint" class="text-fg-4 mt-0.5 pl-4 text-xs">{{ dep.hint }}</p>
        </li>
        <li v-if="!sys.deps.length" class="text-fg-4 px-3 py-2 text-xs">尚未获取到依赖状态。</li>
      </ul>
    </AppPanel>

    <AppPanel title="实时事件（最近 200 条）" class="mt-2">
      <ul class="divide-line-1 divide-y font-mono text-2xs">
        <li v-for="(ev, i) in [...sys.events].reverse()" :key="i" class="flex gap-2 px-3 py-1">
          <span class="text-fg-4 tnum shrink-0">{{ ev.ts.slice(11, 19) }}</span>
          <span class="text-accent shrink-0">{{ ev.channel }}</span>
          <span class="text-fg-2 shrink-0">{{ ev.event }}</span>
          <span class="text-fg-4 truncate">{{ JSON.stringify(ev.payload) }}</span>
        </li>
        <li v-if="!sys.events.length" class="text-fg-4 px-3 py-2 text-xs">
          暂无事件。生成任务开始后，进度与状态会实时出现在这里。
        </li>
      </ul>
    </AppPanel>
  </div>
</template>
