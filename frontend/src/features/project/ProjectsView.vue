<script setup lang="ts">
/**
 * 起始页：项目的新建 / 打开 / 最近列表。
 *
 * 一个项目 = 磁盘上一个自包含目录。这里的三条硬要求：
 *   1. 目录用后端提供的目录浏览器选（浏览器拿不到绝对路径），也允许手输兜底；
 *   2. 失败必须看得见：结构化错误连 suggestions 一起画出来，绝不静默；
 *   3. 最近列表里目录不在了的条目不隐藏，标成「目录不存在」并给「忘记」动作。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { FolderOpen, FolderPlus, FolderSearch, RefreshCw, Trash2 } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import DirPicker from '@/shared/ui/DirPicker.vue'
import type { DurationUnit } from '@/shared/api/projects'
import { REQUIREMENT_LABEL } from '@/app/features'
import { useProjectStore } from '@/stores/project'
import { useSystemStore } from '@/stores/system'

const sys = useSystemStore()
const proj = useProjectStore()
const router = useRouter()
const connected = computed(() => sys.health !== null)

/**
 * 环境自检条：从原来的工作台首页搬到这里。
 * 「能不能开工」必须在开工的那一页回答——后端没连上时新建和打开都会失败，
 * 与其点下去再报错，不如一进来就看见。
 */
const readiness = computed(() => [
  {
    key: 'backend',
    label: '后端服务',
    ok: sys.health !== null,
    detail: sys.health ? `v${sys.health.version} · schema ${sys.health.schema_version}` : '未连接',
    hint: '未连接时无法新建或打开项目，请先启动 backend。',
  },
  ...sys.deps.map((d) => ({
    key: d.name,
    label: REQUIREMENT_LABEL[d.name],
    ok: d.ok,
    detail: d.detail,
    hint: d.hint,
  })),
])

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
    await enter(project.id)
  } catch {
    // 错误已由 store 记入 lastError，下方错误面板负责展示
  }
}

async function open(dir: string): Promise<void> {
  try {
    const project = await proj.open(dir.trim())
    await enter(project.id)
  } catch {
    /* 同上 */
  }
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
      <h1 class="text-fg-1 text-base font-medium">项目</h1>
      <p class="text-fg-2 mt-1 text-xs">
        一个项目就是磁盘上的一个目录，工程与素材都在里面，拷走即可换机继续。
      </p>
      <p v-if="!connected" class="text-st-failed mt-2 text-2xs">
        后端未连接，无法新建或打开项目。启动命令：
        <code class="text-fg-2">
          cd backend &amp;&amp; .venv/Scripts/python -m uvicorn app.main:create_app --factory --port
          8765
        </code>
      </p>
    </section>

    <AppPanel title="能不能开工" class="mt-2">
      <template #actions>
        <AppButton size="sm" variant="ghost" @click="sys.refresh()">
          <RefreshCw :size="10" />重新自检
        </AppButton>
      </template>
      <ul class="divide-line-1 divide-y">
        <li v-for="r in readiness" :key="r.key" class="flex items-start gap-2 px-3 py-1.5">
          <span
            class="mt-1.5 size-1.5 shrink-0 rounded-full"
            :class="r.ok ? 'bg-st-done' : 'bg-st-failed'"
          />
          <div class="min-w-0 flex-1">
            <p class="text-fg-1 text-xs">{{ r.label }} — {{ r.detail }}</p>
            <p v-if="!r.ok && r.hint" class="text-fg-4 text-2xs">{{ r.hint }}</p>
          </div>
        </li>
      </ul>
    </AppPanel>

    <div class="mt-2 grid gap-2 lg:grid-cols-2">
      <AppPanel title="新建项目">
        <form class="space-y-2 p-3" @submit.prevent="create">
          <label class="block">
            <span class="text-fg-3 text-2xs">工程目录（绝对路径，不存在会被创建）</span>
            <div class="mt-0.5 flex items-center gap-1.5">
              <input
                v-model="form.dir"
                type="text"
                placeholder="E:/aivs/我的片子"
                class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 font-mono text-xs outline-none"
              />
              <AppButton title="浏览本机文件夹" @click="browse('create')">
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
                class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-0.5 h-row w-full rounded-sm border px-2 text-xs tnum outline-none"
              />
            </label>
            <label class="block">
              <span class="text-fg-3 text-2xs">高</span>
              <input
                v-model.number="form.height"
                type="number"
                min="64"
                max="8192"
                class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-0.5 h-row w-full rounded-sm border px-2 text-xs tnum outline-none"
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
                class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-0.5 h-row w-full rounded-sm border px-2 text-xs tnum outline-none"
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
          <div class="flex items-center gap-1.5 pt-0.5">
            <AppButton type="submit" variant="primary" :disabled="!canCreate">
              <FolderPlus :size="12" />{{ proj.busy ? '创建中…' : '新建项目' }}
            </AppButton>
            <AppBadge tone="accent">POST /projects</AppBadge>
          </div>
          <p class="text-fg-4 text-2xs">目录里若已有别的工程会被拒绝，一个字节都不会被覆盖。</p>
        </form>
      </AppPanel>

      <AppPanel title="打开已有工程">
        <form class="space-y-2 p-3" @submit.prevent="open(openDir)">
          <label class="block">
            <span class="text-fg-3 text-2xs">工程目录（内含 project.aivs.json）</span>
            <div class="mt-0.5 flex items-center gap-1.5">
              <input
                v-model="openDir"
                type="text"
                placeholder="E:/aivs/我的片子"
                class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 font-mono text-xs outline-none"
              />
              <AppButton title="浏览本机文件夹" @click="browse('open')">
                <FolderSearch :size="12" />浏览…
              </AppButton>
            </div>
          </label>
          <AppButton type="submit" :disabled="!connected || proj.busy || openDir.trim() === ''">
            <FolderOpen :size="12" />打开项目
          </AppButton>
          <p class="text-fg-4 text-2xs">
            旧版本工程会在打开时自动升级 schema，升级结果显示在底部状态条上。
          </p>
        </form>
      </AppPanel>
    </div>

    <AppPanel title="最近打开" class="mt-2">
      <template #actions>
        <AppButton size="sm" variant="ghost" @click="proj.refreshRecent()">
          <RefreshCw :size="10" />刷新
        </AppButton>
      </template>
      <EmptyState
        v-if="proj.recent.length === 0"
        title="还没有任何项目"
        body="用左上角的表单新建一个工程目录，之后它会出现在这里，下次一键打开。"
      >
        <AppButton size="sm" variant="ghost" @click="router.push('/settings')">
          查看环境设置
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

    <AppPanel v-if="proj.lastError" title="上一步失败了" class="mt-2">
      <template #actions>
        <AppButton size="sm" variant="ghost" @click="proj.clearError()">关闭</AppButton>
      </template>
      <div class="p-3 text-xs">
        <p class="text-st-failed">{{ proj.lastError.title }}</p>
        <p class="text-fg-3 mt-0.5 text-2xs">{{ proj.lastError.detail }}</p>
        <ul class="text-fg-4 mt-1 space-y-0.5 text-2xs">
          <li v-for="s in proj.lastError.suggestions" :key="s">· {{ s }}</li>
        </ul>
        <p class="text-fg-4 mt-1 font-mono text-2xs">{{ proj.lastError.code }}</p>
      </div>
    </AppPanel>

    <AppPanel title="工程目录长什么样" class="mt-2">
      <ul class="divide-line-1 divide-y text-xs">
        <li v-for="item in LAYOUT" :key="item.path" class="px-3 py-1.5">
          <p class="text-fg-1 font-mono text-2xs">{{ item.path }}</p>
          <p class="text-fg-4 text-2xs">{{ item.body }}</p>
        </li>
      </ul>
    </AppPanel>

    <DirPicker
      :open="picking !== null"
      :start="pickerStart"
      :title="picking === 'create' ? '选择新工程要放在哪个文件夹' : '选择已有的工程目录'"
      :confirm-label="picking === 'create' ? '在这里新建' : '选择这个工程'"
      @update:open="picking = $event ? picking : null"
      @pick="picked"
    />
  </div>
</template>
