<script setup lang="ts">
/**
 * 导出成包：工程整包，或只导一幕的设定。**先账单再动手**（与 adopt / ingest 同一个规矩）。
 *
 * 两个 scope 共用一个框，因为流程一模一样：打开就出账单 → 选落点 → 写包。
 * 账单里那两张清单（带不走的东西、环境要求）由 `PackageBillPanel` 画，口径只有一处。
 *
 * **落点两条路，主路是「下载到我的电脑」**：界面跑在浏览器 / WebView 里，用户要的是一个
 * 下载好的文件；「后端机器上的某个目录」他很可能压根访问不到，那条路以前却是唯一的选择。
 * 所以默认走 `GET …/package/download`（包写进后端的临时目录、当附件流回来、流完就删）
 * 再由 `saveBlob` 交给浏览器保存。**写进后端机器上的目录**降级成第二条路，仍然留着：
 * 桌面版里两台机器其实是同一台，几个 G 的包不必从自己这儿传给自己一遍。
 *
 * 「带上成片」默认关着：包会小很多，设定照旧完整。改这个勾要重新出账单——
 * 大小和「带不走什么」都会跟着变，拿旧账单按下导出就是骗人。
 */
import { computed, ref, watch } from 'vue'
import { Download, FolderSearch, PackageCheck, RefreshCw } from '@lucide/vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import DirPicker from '@/shared/ui/DirPicker.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import PackageBillPanel from './PackageBillPanel.vue'
import { humanBytes } from '@/shared/api/library'
import { packagesApi } from '@/shared/api/packages'
import type { ExportResult, ProjectExportPlan, SceneExportPlan } from '@/shared/api/packages'
import { saveBlob } from '@/shared/api/client'
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
const emit = defineEmits<{
  'update:open': [boolean]
  /**
   * 导出完成了。两条落点的产物不是一回事（一个已经存到用户电脑上的文件 / 后端机器上一个
   * 路径），所以带上是哪一条——只报一个「成功了」，调用方就没法把结果说清楚。
   */
  done: [
    | { where: 'download'; filename: string; bytes: number }
    | { where: 'folder'; result: ExportResult },
  ]
}>()

type Plan = ProjectExportPlan | SceneExportPlan

/** 落点。`download` 是主路（存到用户电脑上），`folder` 写进后端机器上的目录。 */
type Where = 'download' | 'folder'

const plan = ref<Plan | null>(null)
const includeGenerated = ref(false)
const where = ref<Where>('download')
const outDir = ref('')
const filename = ref('')
const picking = ref(false)
const busy = ref(false)
const error = ref<ApiError | null>(null)
const result = ref<ExportResult | null>(null)
/** 下载那条路的回执：浏览器已经把这个文件保存下来了。 */
const saved = ref<{ filename: string; bytes: number } | null>(null)
const finished = computed(() => result.value !== null || saved.value !== null)

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
    if (where.value === 'download') await runDownload()
    else await runFolder()
  } catch (e) {
    error.value = e as ApiError
  } finally {
    busy.value = false
  }
}

/**
 * 主路：包写在后端的临时目录，当附件流回来由浏览器保存。
 *
 * 文件名以**后端说的那个**为准（`Content-Disposition`，中文名走 RFC 5987），它才是真正
 * 写进包里的那个名字；拿不到时才退回输入框里那个与账单里的建议名。
 */
async function runDownload(): Promise<void> {
  const wanted = filename.value.trim()
  const dl = isScene.value
    ? await packagesApi.downloadScene(props.pid, props.sid, includeGenerated.value, wanted)
    : await packagesApi.downloadProject(props.pid, includeGenerated.value, wanted)
  const as = dl.filename || wanted || plan.value?.suggested_filename || 'package.aivspkg'
  saveBlob(dl.blob, as)
  saved.value = { filename: as, bytes: dl.blob.size }
  emit('done', { where: 'download', filename: as, bytes: dl.blob.size })
}

/** 第二条路：写进后端机器上的目录（桌面版里那就是本机，省掉自己传给自己一遍）。 */
async function runFolder(): Promise<void> {
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
  emit('done', { where: 'folder', result: result.value })
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
    saved.value = null
    error.value = null
    filename.value = ''
    // 落点刻意不重置：上一次选的那条路更可能还是他这次要的（桌面版里常年是「写目录」）
    void loadPlan()
  },
  { immediate: true },
)

const canExport = computed(
  () =>
    plan.value !== null &&
    !busy.value &&
    !finished.value &&
    (where.value === 'download' || outDir.value.trim() !== ''),
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
      <!-- 写完之后只剩「东西现在在哪」这一件事要说，两条落点各说各的 -->
      <div v-if="saved" class="border-st-done/40 bg-st-done/5 border p-2 text-2xs">
        <p class="text-st-done">包已下载到你的电脑，{{ humanBytes(saved.bytes) }}。</p>
        <p class="text-fg-2 mt-0.5 font-mono break-all">{{ saved.filename }}</p>
        <p class="text-fg-4 mt-1">
          在浏览器的下载列表里找它（桌面版会弹保存对话框）。换机之后用起始页的「导入工程包」
          还原；预设图不在包里，要在那台机器上自己准备。
        </p>
      </div>
      <div v-else-if="result" class="border-st-done/40 bg-st-done/5 border p-2 text-2xs">
        <p class="text-st-done">
          包已写好，{{ result.files }} 个文件、{{ humanBytes(result.bytes) }}。
        </p>
        <p class="text-fg-2 mt-0.5 font-mono break-all">{{ result.path }}</p>
        <p class="text-fg-4 mt-1">
          这个路径在<span class="text-fg-3">后端那台机器</span>上（桌面版里就是本机）。换机之后
          用起始页的「导入工程包」还原；预设图不在包里，要在那台机器上自己准备。
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

        <!-- 落点：默认存到用户自己的电脑；写进后端机器上的目录是第二条路 -->
        <div class="border-line-1 bg-base-2 border p-2">
          <span class="text-fg-3 block text-2xs">包放到哪</span>
          <label class="mt-1 flex cursor-pointer items-start gap-1.5 text-xs">
            <input v-model="where" type="radio" value="download" class="accent-accent mt-0.5" />
            <span>
              <span class="text-fg-2">下载到我的电脑（默认）</span>
              <span class="text-fg-4 block text-2xs">
                包在后端写进临时目录、流回来之后就删，由浏览器保存到你的下载目录。
              </span>
            </span>
          </label>
          <label class="mt-1.5 flex cursor-pointer items-start gap-1.5 text-xs">
            <input v-model="where" type="radio" value="folder" class="accent-accent mt-0.5" />
            <span>
              <span class="text-fg-2">写进后端机器上的一个目录</span>
              <span class="text-fg-4 block text-2xs">
                桌面版里那就是本机，几个 G 的包不必从自己这儿传给自己一遍。
              </span>
            </span>
          </label>

          <label v-if="where === 'folder'" class="mt-2 block">
            <span class="text-fg-3 text-2xs">保存到哪个目录（必须已存在）</span>
            <div class="mt-0.5 flex items-center gap-1.5">
              <input
                v-model="outDir"
                type="text"
                placeholder="E:/包"
                class="border-line-1 bg-base-1 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 font-mono text-xs outline-none"
              />
              <AppButton title="浏览后端机器上的文件夹" @click="picking = true">
                <FolderSearch :size="12" />浏览…
              </AppButton>
            </div>
          </label>
        </div>

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
      <AppButton v-if="!finished" variant="ghost" :disabled="busy" @click="loadPlan()">
        <RefreshCw :size="12" />重算账单
      </AppButton>
      <AppButton variant="ghost" @click="emit('update:open', false)">
        {{ finished ? '关闭' : '取消' }}
      </AppButton>
      <AppButton v-if="!finished" variant="primary" :disabled="!canExport" @click="run()">
        <Download v-if="where === 'download'" :size="12" />
        <PackageCheck v-else :size="12" />
        {{ busy ? '处理中…' : where === 'download' ? '导出并下载' : '写包' }}
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
