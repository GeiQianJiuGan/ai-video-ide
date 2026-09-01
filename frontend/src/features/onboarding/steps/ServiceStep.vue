<script setup lang="ts">
/**
 * 第三步：连上生成服务。
 *
 * 字段、候选值、影响说明**全部来自 `GET /settings` 的投影**（`fields` / `choices` /
 * `choice_labels` / `impact`），所以这一步与设置页共用同一份真源，前端不写第二张对照表；
 * 后端加一家 API 只改协议表那一个 dict，这里一行都不用动。
 *
 * 三条链分开摆，各自一颗「测试连接」（`POST /settings/probe?what=…`）：
 * 视频是必须的，图片是可选的（素材图可以手动上传），LLM 更是可选的（手动模式走完全程）。
 *
 * 密钥永不回明文：输入框永远是空的，敲了才提交（`stores/settings.ts` 那条口径）。
 */
import { onMounted } from 'vue'
import { PlugZap, RotateCcw, Save } from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import StatusDot from '@/shared/ui/StatusDot.vue'
import { SOURCE_LABEL } from '@/shared/api/settings'
import { useSettingsStore } from '@/stores/settings'

const cfg = useSettingsStore()

/** 这一步只带用户过这四组；其余（并发数、节点上限…）留给设置页。 */
const FAMILIES: Array<{
  group: string
  probe?: 'llm' | 'video' | 'image'
  title: string
  why: string
}> = [
  {
    group: 'comfy',
    title: 'ComfyUI 地址',
    why: 'comfy_preset 方式下的目标地址，同时也是状态栏与节点探测用的那一个。本机默认 http://127.0.0.1:8188。',
  },
  {
    group: 'video',
    probe: 'video',
    title: '视频生成（必须）',
    why: 'comfy_preset 连你自己那台 ComfyUI，按节点标题注参数；http_api 走通用 REST 合同。没有它做不出画面。',
  },
  {
    group: 'image',
    probe: 'image',
    title: '图片生成（可选）',
    why: '角色四视图 / 地点参考图 / 道具图走这一族。不配也行——素材图照旧可以手动上传。',
  },
  {
    group: 'llm',
    probe: 'llm',
    title: 'AI 协作（可选）',
    why: '拆剧本、写镜头 prompt、看图补描述用它。不配也行：手动编排能走完全程，真源始终是工程库。',
  },
]

onMounted(() => {
  if (!cfg.snapshot) void cfg.load()
})
</script>

<template>
  <div class="space-y-2">
    <ErrorPanel :error="cfg.lastError" @dismiss="cfg.clearError()" />

    <AppPanel v-for="fam in FAMILIES" :key="fam.group" :title="fam.title">
      <template #actions>
        <AppButton
          v-if="fam.probe"
          size="sm"
          :disabled="cfg.probes[fam.probe].busy"
          @click="cfg.probe(fam.probe)"
        >
          <PlugZap :size="10" />{{ cfg.probes[fam.probe].busy ? '探测中…' : '测试连接' }}
        </AppButton>
      </template>

      <p class="text-fg-4 border-line-1 border-b px-3 py-1.5 text-2xs leading-relaxed">
        {{ fam.why }}
      </p>

      <ul class="divide-line-1 divide-y">
        <li v-for="field in cfg.fieldsOf(fam.group)" :key="field.key" class="px-3 py-1.5">
          <div class="flex items-center gap-2">
            <span class="text-fg-2 w-20 shrink-0 text-xs">{{ field.label }}</span>

            <select
              v-if="field.kind === 'enum'"
              :value="cfg.draft[field.key]"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 min-w-40 border px-1 text-2xs outline-none"
              @change="cfg.setOne(field.key, ($event.target as HTMLSelectElement).value)"
            >
              <option v-for="(c, i) in field.choices" :key="c" :value="c">
                {{ field.choice_labels[i] || c }}
              </option>
            </select>

            <label
              v-else-if="field.kind === 'bool'"
              class="text-fg-2 flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 text-2xs"
            >
              <input
                type="checkbox"
                :checked="Boolean(cfg.draft[field.key])"
                class="accent-accent"
                @change="cfg.setOne(field.key, ($event.target as HTMLInputElement).checked)"
              />
              {{ Boolean(cfg.draft[field.key]) ? '开启' : '关闭' }}
            </label>

            <!-- 长文本（系统提示词）不在这一步露面：它属于设置页的调优，不属于「先连上」 -->
            <input
              v-else-if="field.kind !== 'text'"
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

            <AppBadge :tone="field.source === 'file' ? 'accent' : 'neutral'">
              {{ SOURCE_LABEL[field.source] }}
            </AppBadge>
            <AppBadge v-if="cfg.isDirty(field.key)" tone="warn">未保存</AppBadge>
          </div>
          <p v-if="field.impact" class="text-fg-4 mt-0.5 pl-22 text-2xs">{{ field.impact }}</p>
        </li>
      </ul>

      <div
        v-if="fam.probe && cfg.probes[fam.probe].result"
        class="border-line-1 flex items-center gap-2 border-t px-3 py-1.5"
      >
        <StatusDot status="completed" />
        <span class="text-fg-2 min-w-0 flex-1 text-2xs">
          {{ cfg.probes[fam.probe].result?.detail }}
        </span>
        <span class="text-fg-4 truncate font-mono text-2xs">
          {{ cfg.probes[fam.probe].result?.target }}
        </span>
      </div>
      <div v-if="fam.probe && cfg.probes[fam.probe].error" class="p-2">
        <ErrorPanel
          :error="cfg.probes[fam.probe].error"
          @dismiss="cfg.probes[fam.probe].error = null"
        />
      </div>
    </AppPanel>

    <div class="flex items-center gap-2">
      <AppButton variant="primary" :disabled="!cfg.dirty || cfg.busy" @click="cfg.save()">
        <Save :size="11" />保存{{ cfg.dirty ? `（${cfg.dirtyKeys.length} 项）` : '' }}
      </AppButton>
      <AppButton variant="ghost" :disabled="!cfg.dirty" @click="cfg.resetDraft()">
        <RotateCcw :size="11" />撤销未保存的改动
      </AppButton>
      <span class="text-fg-4 min-w-0 truncate font-mono text-2xs">{{ cfg.path }}</span>
    </div>
    <p class="text-fg-4 text-2xs leading-relaxed">
      模型列表可以自动获取、系统提示词可以改，这些都在设置页里——这一步只求「连得上」。
    </p>
  </div>
</template>
