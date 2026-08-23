<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RefreshCw, Trash2, Upload } from '@lucide/vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import StatusDot from '@/shared/ui/StatusDot.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import { useSettingsStore } from '@/stores/settings'

const cfg = useSettingsStore()
const fileInput = ref<HTMLInputElement | null>(null)
const name = ref('')
const text = ref('')

onMounted(() => void cfg.load())

async function pick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  await cfg.uploadPreset(file).catch(() => {})
}

async function save(): Promise<void> {
  if (!name.value.trim() || !text.value.trim()) return
  await cfg.savePreset(name.value.trim(), text.value).catch(() => {})
  if (!cfg.lastError) {
    name.value = ''
    text.value = ''
  }
}
</script>

<template>
  <div class="min-h-0 flex-1 overflow-auto p-2">
    <FeatureHeader />
    <ErrorPanel v-if="cfg.lastError" :error="cfg.lastError" class="mb-2" @dismiss="cfg.clearError()" />
    <AppPanel title="预设 Workflow（ComfyUI API 图）">
      <template #actions>
        <AppButton size="sm" variant="ghost" :disabled="cfg.busy" @click="cfg.loadPresets()">
          <RefreshCw :size="10" />刷新
        </AppButton>
        <AppButton size="sm" variant="primary" :disabled="cfg.busy" @click="fileInput?.click()">
          <Upload :size="10" />上传 API json
        </AppButton>
        <input ref="fileInput" type="file" accept=".json,application/json" class="hidden" @change="pick" />
      </template>
      <p v-if="cfg.presets" class="text-fg-4 border-line-1 border-b px-3 py-1.5 text-2xs">
        预设目录：{{ cfg.presets.dir }}。节点标题按 AIVS_PROMPT、AIVS_NEGATIVE、AIVS_FIRST_FRAME、AIVS_LAST_FRAME、AIVS_REF_* 规范设置后会自动识别。
      </p>
      <ul class="divide-line-1 divide-y">
        <li v-for="row in cfg.presets?.items ?? []" :key="row.name" class="px-3 py-2">
          <div class="flex items-center gap-2">
            <StatusDot :status="row.ready ? 'completed' : 'failed'" />
            <span class="text-fg-1 text-xs">{{ row.name }}</span>
            <AppBadge v-if="row.ready" :tone="row.ref_slots ? 'neutral' : 'warn'">
              参考图 {{ row.ref_slots }} 槽
            </AppBadge>
            <span class="text-fg-4 min-w-0 flex-1 truncate text-2xs">
              {{ row.ready ? row.ref_hint : row.impact }}
            </span>
            <AppButton size="sm" variant="danger" :disabled="cfg.busy" @click="cfg.removePreset(row.name)">
              <Trash2 :size="10" />
            </AppButton>
          </div>
        </li>
        <li v-if="!(cfg.presets?.items ?? []).length" class="text-fg-4 px-3 py-3 text-2xs">
          还没有预设 Workflow。请从 ComfyUI 导出 API 格式 json 后导入。
        </li>
      </ul>
      <div class="border-line-1 space-y-1 border-t p-2">
        <input v-model="name" placeholder="预设名，例如 minimax-h3-fast" class="border-line-1 bg-base-2 text-fg-1 h-6 w-full border px-1.5 text-2xs outline-none" />
        <textarea v-model="text" rows="5" placeholder="也可以直接粘贴 API 格式 json" class="border-line-1 bg-base-2 text-fg-1 w-full border px-1.5 py-1 font-mono text-2xs outline-none" />
        <AppButton size="sm" variant="primary" :disabled="cfg.busy || !name.trim() || !text.trim()" @click="save()">保存预设 Workflow</AppButton>
      </div>
    </AppPanel>
  </div>
</template>
