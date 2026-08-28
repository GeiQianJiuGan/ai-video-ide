<script setup lang="ts">
/**
 * 导出成包：工程整包，或只导一幕的设定。**先账单再动手**（与 adopt / ingest 同一个规矩）。
 *
 * 两个 scope 共用一个框，因为流程一模一样：打开就出账单 → 选落点目录 → 写包。
 * 账单里那两张清单（带不走的东西、环境要求）由 `PackageBillPanel` 画，口径只有一处。
 *
 * 「带上成片」默认关着：包会小很多，设定照旧完整。改这个勾要重新出账单——
 * 大小和「带不走什么」都会跟着变，拿旧账单按下导出就是骗人。
 */
import { computed, ref, watch } from 'vue'
import { FolderSearch, PackageCheck, RefreshCw } from '@lucide/vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import DirPicker from '@/shared/ui/DirPicker.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import PackageBillPanel from './PackageBillPanel.vue'
import { humanBytes } from '@/shared/api/library'
import { packagesApi } from '@/shared/api/packages'
import type { ExportResult, ProjectExportPlan, SceneExportPlan } from '@/shared/api/packages'
import type { ApiError } from '@/shared/api/client'

const props = withDefaults(
  defineProps<{
    open: boolean
    pid: string
    /** 传了就是导一幕的设定，不传就是整个工程。 */
    sid?: string
  }>(),
  { sid: '' },
)
const emit = defineEmits<{ 'update:open': [boolean]; done: [ExportResult] }>()

type Plan = ProjectExportPlan | SceneExportPlan

const plan = ref<Plan | null>(null)
const includeGenerated = ref(false)
const outDir = ref('')
const filename = ref('')
const picking = ref(false)
const busy = ref(false)
const error = ref<ApiError | null>(null)
const result = ref<ExportResult | null>(null)

const isScene = computed(() => props.sid !== '')
const title = computed(() => (isScene.value ? '导出这一幕的设定' : '导出整个工程'))

async function loadPlan(): Promise<void> {
  busy.value = true
  error.value = null
  try {
    plan.value = isScene.value
      ? await packagesApi.planScene(props.pid, props.sid, includeGenerated.value)
      : await packagesApi.planProject(props.pid, includeGenerated.value)
    if (filename.value === '') filename.value = plan.value.suggested_filename
  } catch (e) {
    error.value = e as ApiError
  } finally {
    busy.value = false
  }
}

async function run(): Promise<void> {
  busy.value = true
  error.value = null
  try {
    result.value = isScene.value
      ? await packagesApi.exportScene(
          props.pid,
          props.sid,
          outDir.value.trim(),
          filename.value.trim(),
          includeGenerated.value,
        )
      : await packagesApi.exportProject(
          props.pid,
          outDir.value.trim(),
          filename.value.trim(),
          includeGenerated.value,
        )
    emit('done', result.value)
  } catch (e) {
    error.value = e as ApiError
  } finally {
    busy.value = false
  }
}

/** 换了「带成片」这个勾，旧账单就作废——大小与带不走什么都会变。 */
watch(includeGenerated, () => {
  if (props.open) void loadPlan()
})

watch(
  () => props.open,
  (now) => {
    if (!now) return
    plan.value = null
    result.value = null
    error.value = null
    filename.value = ''
    void loadPlan()
  },
  { immediate: true },
)

const canExport = computed(
  () => plan.value !== null && !busy.value && outDir.value.trim() !== '' && result.value === null,
)
</script>

<template>
  <AppDialog
    :open="open"
    :title="title"
    subtitle="先看账单，再写包（.aivspkg）"
    size="lg"
    @update:open="emit('update:open', $event)"
  >
    <div class="space-y-2 p-3">
      <!-- 写完包之后只剩「落在哪」这一件事要说 -->
      <div v-if="result" class="border-st-done/40 bg-st-done/5 border p-2 text-2xs">
        <p class="text-st-done">
          包已写好，{{ result.files }} 个文件、{{ humanBytes(result.bytes) }}。
        </p>
        <p class="text-fg-2 mt-0.5 font-mono break-all">{{ result.path }}</p>
        <p class="text-fg-4 mt-1">
          换机之后用起始页的「导入工程包」还原；预设图不在包里，要在那台机器上自己准备。
        </p>
      </div>

      <template v-else>
        <div v-if="plan" class="border-line-1 bg-base-2 border p-2 text-2xs">
          <p class="text-fg-1">
            {{ isScene ? '这一幕' : '这个工程' }}会写进包里：{{ plan.files }} 个文件 ·
            {{ humanBytes(plan.total_bytes) }}
          </p>
          <ul v-if="'groups' in plan" class="mt-1 space-y-0.5">
            <li class="text-fg-4">
              <span class="text-fg-2 font-mono">project.db</span>
              · {{ humanBytes(plan.db_bytes) }} · 工程的唯一真源
            </li>
            <li v-for="g in plan.groups" :key="g.dir" class="text-fg-4">
              <span class="text-fg-2 font-mono">{{ g.dir }}/</span>
              · {{ g.files }} 个 · {{ humanBytes(g.bytes) }}
              <span :class="g.included ? 'text-st-done' : 'text-fg-4'">
                · {{ g.included ? '带' : '不带' }}
              </span>
            </li>
          </ul>
          <ul v-else class="text-fg-4 mt-1 space-y-0.5">
            <li v-for="(value, key) in plan.counts" :key="key">
              {{ key }}：<span class="tnum text-fg-2">{{ value }}</span>
            </li>
          </ul>
        </div>

        <label class="flex cursor-pointer items-start gap-1.5 text-xs">
          <input v-model="includeGenerated" type="checkbox" class="accent-accent mt-0.5" />
          <span>
            <span class="text-fg-2">带上已生成的成片</span>
            <span class="text-fg-4 block text-2xs">
              默认不带，包会小很多、设定照旧完整。<code>cache/</code> 与
              <code>proxies/</code> 永远不进包——那是可再生的派生物。
            </span>
          </span>
        </label>

        <label class="block">
          <span class="text-fg-3 text-2xs">保存到哪个目录（必须已存在）</span>
          <div class="mt-0.5 flex items-center gap-1.5">
            <input
              v-model="outDir"
              type="text"
              placeholder="E:/包"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 font-mono text-xs outline-none"
            />
            <AppButton title="浏览本机文件夹" @click="picking = true">
              <FolderSearch :size="12" />浏览…
            </AppButton>
          </div>
        </label>

        <label class="block">
          <span class="text-fg-3 text-2xs">文件名（留空用建议名）</span>
          <input
            v-model="filename"
            type="text"
            :placeholder="plan?.suggested_filename || 'my_film.aivspkg'"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-0.5 h-row w-full rounded-sm border px-2 font-mono text-xs outline-none"
          />
        </label>

        <PackageBillPanel
          v-if="plan"
          :omitted="plan.omitted"
          :missing="plan.missing"
          :env-check="null"
        />
      </template>
    </div>

    <ErrorPanel v-if="error" class="mx-3 mb-3" :error="error" @dismiss="error = null" />

    <template #footer>
      <p class="text-fg-4 min-w-0 flex-1 text-2xs">密钥与服务地址一律不进包。</p>
      <AppButton v-if="!result" variant="ghost" :disabled="busy" @click="loadPlan()">
        <RefreshCw :size="12" />重算账单
      </AppButton>
      <AppButton variant="ghost" @click="emit('update:open', false)">
        {{ result ? '关闭' : '取消' }}
      </AppButton>
      <AppButton v-if="!result" variant="primary" :disabled="!canExport" @click="run()">
        <PackageCheck :size="12" />{{ busy ? '处理中…' : '写包' }}
      </AppButton>
    </template>
  </AppDialog>

  <DirPicker
    :open="picking"
    :start="outDir"
    title="选择包保存在哪个文件夹"
    confirm-label="保存到这里"
    @update:open="picking = $event"
    @pick="
      (p) => {
        outDir = p
        picking = false
      }
    "
  />
</template>
