<script setup lang="ts">
/**
 * 设置页：可写配置 + 外部依赖状态。
 *
 * 三条约定写在这里，因为它们是「绝不静默失败」在配置页的具体样子：
 *
 *   1. 每一项都标出**值是从哪来的**（配置文件 / 环境变量 / 默认）——排查时唯一有用的信息；
 *   2. 「测试连接」失败显示后端给的四要素错误，**不是一个红叉**；
 *   3. API Key 输入框永远是空的（后端不回明文），敲了才提交；要清除有专门的按钮。
 *
 * 旧的 Workflow 绑定页降级成了高级/兼容路径，入口收在最后的折叠区里。
 */
import { onMounted, ref } from 'vue'
import { ChevronRight, PlugZap, RefreshCw, RotateCcw, Save, Trash2, Upload } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import StatusDot from '@/shared/ui/StatusDot.vue'
import { SOURCE_LABEL, type SettingField } from '@/shared/api/settings'
import { useSettingsStore } from '@/stores/settings'
import { useSystemStore } from '@/stores/system'

const cfg = useSettingsStore()
const sys = useSystemStore()

const DEP_TITLE: Record<string, string> = {
  ffmpeg: 'FFmpeg — 抽帧 / 代理转码 / 导出',
  comfyui: 'ComfyUI — 视频与图像生成',
  llm: 'LLM — AI 协作（可选，非必需）',
}

const GROUP_HINT: Record<string, string> = {
  llm: '给「幕」页面的 AI 协作栏用。不配也行——手动编排能走完全程。',
  video:
    'comfy_preset 直接连 ComfyUI 并按节点标题注参数，模型端的图由模型端维护；http_api 走通用合同。',
  comfy: 'comfy_preset 方式下的目标地址，同时也是节点探测与状态栏用的那一个。',
  runtime: '并发数与 FFmpeg。FFmpeg 留空或裸名字表示用应用自带的那份。',
}

/** 哪一组下面挂「测试连接」。 */
const PROBE_OF: Record<string, 'llm' | 'video'> = { llm: 'llm', video: 'video' }

const advanced = ref(false)
const presetName = ref('')
const presetText = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

onMounted(() => {
  void cfg.load()
})

function tone(field: SettingField): 'accent' | 'neutral' {
  return field.source === 'file' ? 'accent' : 'neutral'
}

async function onPickPreset(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file) await cfg.uploadPreset(file).catch(() => {})
}

async function pastePreset(): Promise<void> {
  if (!presetName.value.trim() || !presetText.value.trim()) return
  await cfg.savePreset(presetName.value.trim(), presetText.value).catch(() => {})
  if (!cfg.lastError) {
    presetName.value = ''
    presetText.value = ''
  }
}
</script>
<template>
  <div class="min-h-0 flex-1 overflow-auto p-2">
    <ErrorPanel :error="cfg.lastError" class="mb-2" @dismiss="cfg.clearError()" />

    <!-- 可写配置：一组一块 -->
    <AppPanel v-for="group in cfg.groups" :key="group.id" :title="group.title" class="mb-2">
      <template #actions>
        <AppButton
          v-if="PROBE_OF[group.id]"
          size="sm"
          :disabled="cfg.probes[PROBE_OF[group.id]!].busy"
          @click="cfg.probe(PROBE_OF[group.id]!)"
        >
          <PlugZap :size="10" />{{ cfg.probes[PROBE_OF[group.id]!].busy ? '探测中…' : '测试连接' }}
        </AppButton>
      </template>

      <p class="text-fg-4 border-line-1 border-b px-3 py-1.5 text-2xs">
        {{ GROUP_HINT[group.id] }}
      </p>

      <ul class="divide-line-1 divide-y">
        <li v-for="field in cfg.fieldsOf(group.id)" :key="field.key" class="px-3 py-1.5">
          <div class="flex items-center gap-2">
            <span class="text-fg-2 w-20 shrink-0 text-xs">{{ field.label }}</span>

            <select
              v-if="field.kind === 'enum'"
              :value="cfg.draft[field.key]"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 min-w-40 border px-1 text-2xs outline-none"
              @change="cfg.setOne(field.key, ($event.target as HTMLSelectElement).value)"
            >
              <option v-for="c in field.choices" :key="c" :value="c">{{ c }}</option>
            </select>

            <input
              v-else
              v-model="cfg.draft[field.key]"
              :type="
                field.kind === 'secret' ? 'password' : field.kind === 'int' ? 'number' : 'text'
              "
              :placeholder="
                field.kind === 'secret'
                  ? field.has_value
                    ? `已保存 ${field.masked}（留空表示不改）`
                    : '未设置'
                  : '留空表示用环境变量 / 默认值'
              "
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-5 min-w-0 flex-1 border px-1.5 text-2xs outline-none"
              @keyup.enter="cfg.save()"
            />

            <AppBadge :tone="tone(field)">{{ SOURCE_LABEL[field.source] }}</AppBadge>
            <AppBadge v-if="cfg.isDirty(field.key)" tone="warn">未保存</AppBadge>
            <AppButton
              size="sm"
              variant="ghost"
              :disabled="field.source !== 'file' || cfg.busy"
              title="清除这项覆盖，回到环境变量或默认值"
              @click="cfg.clear(field.key)"
            >
              <RotateCcw :size="10" />
            </AppButton>
          </div>
          <p v-if="field.impact" class="text-fg-4 mt-0.5 pl-22 text-2xs">{{ field.impact }}</p>
        </li>
      </ul>

      <div
        v-if="PROBE_OF[group.id] && cfg.probes[PROBE_OF[group.id]!].result"
        class="border-line-1 flex items-center gap-2 border-t px-3 py-1.5"
      >
        <StatusDot status="completed" />
        <span class="text-fg-2 text-2xs">
          {{ cfg.probes[PROBE_OF[group.id]!].result?.detail }}
        </span>
        <span class="text-fg-4 font-mono text-2xs">
          {{ cfg.probes[PROBE_OF[group.id]!].result?.target }}
        </span>
      </div>
      <div v-if="PROBE_OF[group.id] && cfg.probes[PROBE_OF[group.id]!].error" class="p-2">
        <ErrorPanel
          :error="cfg.probes[PROBE_OF[group.id]!].error"
          @dismiss="cfg.probes[PROBE_OF[group.id]!].error = null"
        />
      </div>
    </AppPanel>

    <div class="mb-2 flex items-center gap-2">
      <AppButton variant="primary" :disabled="!cfg.dirty || cfg.busy" @click="cfg.save()">
        <Save :size="11" />保存{{ cfg.dirty ? `（${cfg.dirtyKeys.length} 项）` : '' }}
      </AppButton>
      <AppButton variant="ghost" :disabled="!cfg.dirty" @click="cfg.resetDraft()">
        撤销未保存的改动
      </AppButton>
      <span class="text-fg-4 truncate font-mono text-2xs">{{ cfg.path }}</span>
    </div>
    <!-- ComfyUI 预设：模型端那份图的本地副本 -->
    <AppPanel title="生成预设（ComfyUI 图）" class="mb-2">
      <template #actions>
        <AppButton size="sm" variant="ghost" @click="cfg.loadPresets()">
          <RefreshCw :size="10" />刷新
        </AppButton>
        <AppButton size="sm" variant="primary" :disabled="cfg.busy" @click="fileInput?.click()">
          <Upload :size="10" />上传 API 格式 json
        </AppButton>
        <input ref="fileInput" type="file" accept=".json" class="hidden" @change="onPickPreset" />
      </template>

      <ul v-if="cfg.presets" class="text-fg-4 border-line-1 border-b px-3 py-1.5 text-2xs">
        <li v-for="line in cfg.presets.how_to" :key="line">· {{ line }}</li>
      </ul>

      <ul class="divide-line-1 divide-y">
        <li
          v-for="row in cfg.presets?.items ?? []"
          :key="row.name"
          class="flex items-center gap-2 px-3 py-1.5"
        >
          <StatusDot :status="row.ready ? 'completed' : 'failed'" />
          <span class="text-fg-1 text-xs">{{ row.name }}</span>
          <AppBadge v-if="cfg.byKey['video.preset']?.value === row.name" tone="accent">
            默认
          </AppBadge>
          <span class="text-fg-4 min-w-0 flex-1 truncate text-2xs">
            {{ row.ready ? (row.found ?? []).join(' · ') : row.impact }}
          </span>
          <AppButton
            size="sm"
            variant="ghost"
            :disabled="!row.ready || cfg.busy"
            @click="cfg.setOne('video.preset', row.name)"
          >
            设为默认
          </AppButton>
          <AppButton
            size="sm"
            variant="danger"
            :disabled="cfg.busy"
            @click="cfg.removePreset(row.name)"
          >
            <Trash2 :size="10" />
          </AppButton>
        </li>
        <li v-if="!(cfg.presets?.items ?? []).length" class="text-fg-4 px-3 py-2 text-2xs">
          还没有预设。从 ComfyUI 里用「Save (API
          Format)」导出一份，入口节点的标题按上面的约定改好再上传。
        </li>
      </ul>

      <div class="border-line-1 space-y-1 border-t p-2">
        <input
          v-model="presetName"
          placeholder="预设名，例如 wan-i2v-快速"
          class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-5 w-full border px-1.5 text-2xs outline-none"
        />
        <textarea
          v-model="presetText"
          rows="3"
          placeholder="也可以直接粘贴 API 格式 json"
          class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 w-full border px-1.5 py-1 font-mono text-2xs outline-none"
        ></textarea>
        <AppButton
          size="sm"
          :disabled="!presetName.trim() || !presetText.trim() || cfg.busy"
          :title="
            !presetName.trim()
              ? '先给这份图起个名字——之后在 provider 设置里按名字选它'
              : !presetText.trim()
                ? '把 ComfyUI 导出的 API 格式 json 粘进下面那个框'
                : '把这份图存进应用级预设目录'
          "
          @click="pastePreset()"
        >
          保存这份图
        </AppButton>
      </div>
    </AppPanel>

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

    <!-- 高级 / 兼容：旧的 Workflow 绑定路径 -->
    <div class="border-line-1 bg-base-1 mt-2 border">
      <button
        class="text-fg-2 hover:text-fg-1 flex w-full items-center gap-1.5 px-3 py-1.5 text-xs"
        @click="advanced = !advanced"
      >
        <ChevronRight :size="11" :class="advanced ? 'rotate-90' : ''" />高级 / 兼容路径
      </button>
      <div v-if="advanced" class="border-line-1 border-t px-3 py-2">
        <p class="text-fg-3 text-2xs">
          旧的「Workflow 管理」把 prompt / 参考图 / 时长逐个绑到 ComfyUI 节点字段上。现在默认路径
          改成了按节点标题注参数——模型端的图由模型端维护，本工具不再跟着改。已经配好的工作流仍然
          可用：把上面的「调用方式」改成 <span class="font-mono">comfy_workflow</span> 就会走它。
        </p>
        <p class="text-fg-4 mt-1 text-2xs">
          绑定页在工程里：打开一个工程后按 Ctrl+K，搜「Workflow」就能进去。它不在左栏导航里——
          默认路径不需要它。
        </p>
      </div>
    </div>

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
