<script setup lang="ts">
/**
 * 起始页：项目的新建 / 打开 / 最近列表。
 *
 * 一个项目 = 磁盘上一个自包含目录。这里的三条硬要求：
 *   1. 目录用后端提供的目录浏览器选（浏览器拿不到绝对路径），也允许手输兜底；
 *   2. 失败必须看得见：结构化错误连 suggestions 一起画出来，绝不静默；
 *   3. 最近列表里目录不在了的条目不隐藏，标成「目录不存在」并给「忘记」动作。
 *
 * 版式上刻意把「填什么」搬进弹窗、页面只留「有什么」：
 * 新建 / 打开都是一次性动作，常驻两张表单会把每次进来都要看的最近列表挤到下面去。
 * 环境自检也不在这里长驻——底部状态条一直在显示后端与依赖状态，这里只在
 * 后端没连上时留一条挡路的提示，因为那时候新建和打开都做不了。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  FolderOpen,
  FolderPlus,
  FolderSearch,
  GraduationCap,
  PackageOpen,
  RefreshCw,
  Trash2,
} from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import DirPicker from '@/shared/ui/DirPicker.vue'
import ImportProjectDialog from '@/features/packages/ImportProjectDialog.vue'
import type { DurationUnit } from '@/shared/api/projects'
import { useProjectStore } from '@/stores/project'
import { useOnboardingStore } from '@/stores/onboarding'
import { useSystemStore } from '@/stores/system'

const sys = useSystemStore()
const proj = useProjectStore()
const wiz = useOnboardingStore()
const router = useRouter()
const connected = computed(() => sys.health !== null)

/** 弹窗开关。两个动作互斥，同时开两张表单没有意义。 */
const creating = ref(false)
const opening = ref(false)

/**
 * 跨机搬迁：这一页只留「导入」。
 *
 * **导出不在这里**——导出的是「某一个工程」的库与素材，而这一页上根本没有打开的工程，
 * 以前那颗按钮指的是「上次打开的那个」，disabled 与否全靠 `proj.current` 碰运气。
 * 打开工程之后标题栏、命令面板、概览页三处都有「导出工程」（同一个弹窗，挂在
 * `WorkbenchLayout` 上），那才是它该在的地方。
 *
 * 导入相反：它的产物是一个**还不存在的工程**，只能在这一页做。
 */
const importing = ref(false)

const form = ref({
  dir: '',
  name: '',
  width: 1920,
  height: 1080,
  fps: 25,
  duration_unit: 'frames' as DurationUnit,
})
const openDir = ref('')

/** 两个路径输入框共用一个目录选择器，picking 记住这次选的是哪一个。 */
const picking = ref<'create' | 'open' | null>(null)
const pickerStart = computed(() =>
  picking.value === 'create' ? form.value.dir : picking.value === 'open' ? openDir.value : '',
)

function browse(target: 'create' | 'open'): void {
  picking.value = target
}

function picked(path: string): void {
  if (picking.value === 'create') form.value.dir = path
  else if (picking.value === 'open') openDir.value = path
  picking.value = null
}

const canCreate = computed(
  () =>
    connected.value && !proj.busy && form.value.dir.trim() !== '' && form.value.name.trim() !== '',
)

/**
 * 错误显示在哪：弹窗开着就显示在弹窗里（就地改路径重试），否则显示在页面上
 * （最近列表里点「打开」失败走这条）。同一条错误只画一次。
 */
const inDialog = computed(() => creating.value || opening.value)
const pageError = computed(() => (inDialog.value ? null : proj.lastError))

/** 工程目录的组成部分：一个项目 = 一个自包含的目录，可整体拷走。 */
const LAYOUT: { path: string; body: string }[] = [
  { path: 'project.aivs.json', body: '工程清单：名称、分辨率、帧率、默认 Workflow' },
  { path: 'project.db', body: 'SQLite（WAL）：Character / Scene / Shot / Version 的唯一真源' },
  { path: 'assets/', body: '角色表、场景参考、道具图等落盘素材' },
  { path: 'generations/', body: '每次生成的输出与参数快照，永不覆盖' },
  { path: 'proxies/', body: '720p 代理流，仅用于时间线预览' },
]

async function enter(pid: string): Promise<void> {
  await router.push({ name: 'dashboard', params: { pid } })
}

async function create(): Promise<void> {
  try {
    const project = await proj.create({
      ...form.value,
      dir: form.value.dir.trim(),
      name: form.value.name.trim(),
    })
    creating.value = false
    await enter(project.id)
  } catch {
    // 错误已由 store 记入 lastError，弹窗内的错误面板负责展示；弹窗保持打开好改路径
  }
}

async function open(dir: string): Promise<void> {
  try {
    const project = await proj.open(dir.trim())
    opening.value = false
    await enter(project.id)
  } catch {
    /* 同上 */
  }
}

/** 打开弹窗前先清掉上一次的失败，否则一进来就看到一条与本次无关的红字。 */
function startCreate(): void {
  proj.clearError()
  creating.value = true
}

function startOpen(): void {
  proj.clearError()
  opening.value = true
}

/**
 * 包已经还原好了，后端那边这个工程也已经打开。这里再走一次 `open(dir)`：
 * 前端的 store 需要持有那份 Project（`current` + 最近列表），而 open 是幂等的。
 */
async function imported(dir: string): Promise<void> {
  importing.value = false
  await open(dir)
}

function fmtTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

onMounted(() => void proj.refreshRecent())
</script>

<template>
  <div class="min-h-0 flex-1 overflow-auto p-2">
    <section class="border-line-1 bg-base-1 border p-4">
      <div class="flex items-start gap-2">
        <div class="min-w-0 flex-1">
          <h1 class="text-fg-1 text-base font-medium">项目</h1>
          <p class="text-fg-2 mt-1 text-xs">
            一个项目就是磁盘上的一个目录，工程与素材都在里面，拷走即可换机继续。
          </p>
        </div>
        <AppButton variant="primary" :disabled="!connected" @click="startCreate()">
          <FolderPlus :size="12" />新建项目
        </AppButton>
        <AppButton :disabled="!connected" @click="startOpen()">
          <FolderOpen :size="12" />打开工程
        </AppButton>
      </div>

      <!-- 跨机搬迁：这一页只有「导入」，导出在工程里面（标题栏 / 命令面板 / 概览页） -->
      <div class="border-line-1 mt-3 flex items-center gap-2 border-t pt-2">
        <p class="text-fg-4 min-w-0 flex-1 text-2xs">
          换机器继续：在另一台机器上把工程导出成一个 <code class="text-fg-3">.aivspkg</code> 包，
          在这里导入。包里带工程库与素材，密钥与服务地址一律不进包。
          <span class="text-fg-3">导出</span> 在工程里面做——打开工程后标题栏、命令面板与概览页
          都有「导出工程」。
        </p>
        <AppButton size="sm" :disabled="!connected" @click="importing = true">
          <PackageOpen :size="10" />导入工程包
        </AppButton>
      </div>

      <!-- 后端没连上时新建和打开都会失败，这条提示挡在动作前面，比事后报错有用 -->
      <div
        v-if="!connected"
        class="border-st-failed/40 bg-st-failed/5 mt-3 border px-2 py-1.5 text-2xs"
      >
        <p class="text-st-failed">后端未连接，现在无法新建或打开项目。</p>
        <p class="text-fg-3 mt-0.5">
          启动命令：
          <code class="text-fg-2">
            cd backend &amp;&amp; AIVS_PORT=8765 .venv/Scripts/python -m app.main
          </code>
        </p>
        <AppButton size="sm" variant="ghost" class="mt-1" @click="sys.refresh()">
          <RefreshCw :size="10" />重新自检
        </AppButton>
      </div>
    </section>

    <ErrorPanel v-if="pageError" class="mt-2" :error="pageError" @dismiss="proj.clearError()" />

    <AppPanel title="最近打开" class="mt-2">
      <template #actions>
        <AppButton size="sm" variant="ghost" @click="proj.refreshRecent()">
          <RefreshCw :size="10" />刷新
        </AppButton>
      </template>
      <EmptyState
        v-if="proj.recent.length === 0"
        title="还没有任何项目"
        body="用右上角的「新建项目」建一个工程目录，之后它会出现在这里，下次一键打开。想先看看系统是怎么组织一部片子的，就打开演示项目。"
      >
        <AppButton size="sm" variant="primary" :disabled="!connected" @click="startCreate()">
          <FolderPlus :size="10" />新建项目
        </AppButton>
        <AppButton size="sm" :disabled="!connected" @click="wiz.reopen('demo')">
          <GraduationCap :size="10" />打开演示项目
        </AppButton>
      </EmptyState>
      <ul v-else class="divide-line-1 divide-y">
        <li
          v-for="item in proj.recent"
          :key="item.dir"
          class="flex items-center gap-2 px-3 py-1.5 text-xs"
        >
          <div class="min-w-0 flex-1">
            <p class="text-fg-1 flex items-center gap-1.5 truncate">
              {{ item.name }}
              <AppBadge v-if="item.is_open" tone="accent">已打开</AppBadge>
              <AppBadge v-if="!item.exists">目录不存在</AppBadge>
            </p>
            <p class="text-fg-4 truncate font-mono text-2xs">{{ item.dir }}</p>
            <p class="text-fg-4 text-2xs">
              schema {{ item.schema_version }} · 上次打开 {{ fmtTime(item.opened_at) }}
            </p>
          </div>
          <AppButton size="sm" :disabled="!connected || !item.exists" @click="open(item.dir)">
            <FolderOpen :size="10" />打开
          </AppButton>
          <AppButton
            size="sm"
            variant="ghost"
            title="只从最近列表移除，不动磁盘上的工程"
            @click="proj.forget(item.dir)"
          >
            <Trash2 :size="10" />忘记
          </AppButton>
        </li>
      </ul>
    </AppPanel>

    <!-- 新建：目录 + 名称 + 画布参数，附带「这个目录里会长出什么」 -->
    <AppDialog
      v-model:open="creating"
      title="新建项目"
      subtitle="POST /projects · 目录不存在会被创建"
    >
      <form id="create-project" class="space-y-2 p-3" @submit.prevent="create">
        <label class="block">
          <span class="text-fg-3 text-2xs">工程目录（绝对路径，不存在会被创建）</span>
          <div class="mt-0.5 flex items-center gap-1.5">
            <input
              v-model="form.dir"
              type="text"
              placeholder="E:/aivs/我的片子"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 font-mono text-xs outline-none"
            />
            <AppButton type="button" title="浏览本机文件夹" @click="browse('create')">
              <FolderSearch :size="12" />浏览…
            </AppButton>
          </div>
        </label>
        <label class="block">
          <span class="text-fg-3 text-2xs">项目名称</span>
          <input
            v-model="form.name"
            type="text"
            placeholder="无名之城"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-0.5 h-row w-full rounded-sm border px-2 text-xs outline-none"
          />
        </label>
        <div class="grid grid-cols-4 gap-1.5">
          <label class="block">
            <span class="text-fg-3 text-2xs">宽</span>
            <input
              v-model.number="form.width"
              type="number"
              min="64"
              max="8192"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-0.5 h-row tnum w-full rounded-sm border px-2 text-xs outline-none"
            />
          </label>
          <label class="block">
            <span class="text-fg-3 text-2xs">高</span>
            <input
              v-model.number="form.height"
              type="number"
              min="64"
              max="8192"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-0.5 h-row tnum w-full rounded-sm border px-2 text-xs outline-none"
            />
          </label>
          <label class="block">
            <span class="text-fg-3 text-2xs">帧率</span>
            <input
              v-model.number="form.fps"
              type="number"
              min="1"
              max="240"
              step="0.001"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-0.5 h-row tnum w-full rounded-sm border px-2 text-xs outline-none"
            />
          </label>
          <label class="block">
            <span class="text-fg-3 text-2xs">时长单位</span>
            <select
              v-model="form.duration_unit"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-0.5 h-row w-full rounded-sm border px-1 text-xs outline-none"
            >
              <option value="frames">帧</option>
              <option value="seconds">秒</option>
            </select>
          </label>
        </div>
        <p class="text-fg-4 text-2xs">目录里若已有别的工程会被拒绝，一个字节都不会被覆盖。</p>

        <div class="border-line-1 border-t pt-2">
          <p class="text-fg-3 text-2xs">建好之后这个目录里会有：</p>
          <ul class="mt-1 space-y-0.5">
            <li v-for="item in LAYOUT" :key="item.path" class="flex gap-1.5 text-2xs">
              <span class="text-fg-2 w-36 shrink-0 font-mono">{{ item.path }}</span>
              <span class="text-fg-4 min-w-0 flex-1">{{ item.body }}</span>
            </li>
          </ul>
        </div>
      </form>

      <ErrorPanel
        v-if="proj.lastError"
        class="mx-3 mb-3"
        :error="proj.lastError"
        @dismiss="proj.clearError()"
      />

      <template #footer>
        <p class="text-fg-4 min-w-0 flex-1 text-2xs">建完直接进这个工程的概览页。</p>
        <AppButton variant="ghost" @click="creating = false">取消</AppButton>
        <AppButton type="submit" form="create-project" variant="primary" :disabled="!canCreate">
          <FolderPlus :size="12" />{{ proj.busy ? '创建中…' : '新建项目' }}
        </AppButton>
      </template>
    </AppDialog>

    <!-- 打开：只要一个目录 -->
    <AppDialog
      v-model:open="opening"
      title="打开已有工程"
      subtitle="目录里得有 project.aivs.json"
      size="sm"
    >
      <form id="open-project" class="space-y-2 p-3" @submit.prevent="open(openDir)">
        <label class="block">
          <span class="text-fg-3 text-2xs">工程目录</span>
          <div class="mt-0.5 flex items-center gap-1.5">
            <input
              v-model="openDir"
              type="text"
              placeholder="E:/aivs/我的片子"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 font-mono text-xs outline-none"
            />
            <AppButton type="button" title="浏览本机文件夹" @click="browse('open')">
              <FolderSearch :size="12" />浏览…
            </AppButton>
          </div>
        </label>
        <p class="text-fg-4 text-2xs">
          旧版本工程会在打开时自动升级 schema，升级结果显示在底部状态条上。
        </p>
      </form>

      <ErrorPanel
        v-if="proj.lastError"
        class="mx-3 mb-3"
        :error="proj.lastError"
        @dismiss="proj.clearError()"
      />

      <template #footer>
        <span class="flex-1" />
        <AppButton variant="ghost" @click="opening = false">取消</AppButton>
        <AppButton
          type="submit"
          form="open-project"
          variant="primary"
          :disabled="!connected || proj.busy || openDir.trim() === ''"
        >
          <FolderOpen :size="12" />打开项目
        </AppButton>
      </template>
    </AppDialog>

    <DirPicker
      :open="picking !== null"
      :start="pickerStart"
      :title="picking === 'create' ? '选择新工程要放在哪个文件夹' : '选择已有的工程目录'"
      :confirm-label="picking === 'create' ? '在这里新建' : '选择这个工程'"
      @update:open="picking = $event ? picking : null"
      @pick="picked"
    />

    <ImportProjectDialog v-model:open="importing" @done="imported" />
  </div>
</template>
