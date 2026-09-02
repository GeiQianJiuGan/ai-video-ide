<script setup lang="ts">
/**
 * 项目概览页（Step 9 的前端）。
 *
 * 它回答三件事，一件一栏：现在到哪了（进度 + 计数 + 镜头状态分布）、
 * 下一步做什么（继续上次工作 / 空工程引导）、哪里不对（连续性检查 + 环境）。
 *
 * 三条口径不在前端重造：
 *   1. 镜头状态中文名用后端给的 `label`（STATUS_LABEL 是唯一真源）；
 *   2. 连续性问题的 title / detail / suggestions 原样显示——检查只报事实不改数据，
 *      判断权留给导演；
 *   3. 环境探测失败不是页面失败：ComfyUI 离线只说明哪些路径受影响，
 *      手动路径照旧能走完（LLM / ComfyUI 都不是必选项）。
 *
 * 空工程刻意不画空图表（0% 的进度条 + 全 0 的分布条只是噪音），改画下一步引导。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  Activity,
  CheckCircle2,
  CircleSlash,
  Library,
  PlayCircle,
  RefreshCw,
  ScanSearch,
  Users,
} from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import ChainStrip from '@/shared/ui/ChainStrip.vue'
import { ApiError, type ErrorPayload } from '@/shared/api/client'
import { COUNT_CARDS } from '@/shared/api/overview'
import {
  projectsApi,
  ROUTE_SOURCE_LABEL,
  type ProjectRoute,
  type RouteCapability,
} from '@/shared/api/projects'
import { settingsApi, type PresetRow } from '@/shared/api/settings'
import {
  CAPABILITIES,
  CAPABILITY_LABEL,
  workflowsApi,
  type Capability,
  type GenerationMode,
  type ProjectWorkflowBindings,
  type Workflow,
} from '@/shared/api/workflows'
import { useOverviewStore } from '@/stores/overview'

const route = useRoute()
const ov = useOverviewStore()

const pid = computed(() => String(route.params.pid ?? ''))
const presets = ref<PresetRow[]>([])
const presetBusy = ref(false)

/**
 * 「这个工程怎么出片」那一块的全部数据（`GET /projects/{pid}/route`，一个请求够了）：
 * 走哪条路、这条路要绑什么、绑没绑上、缺什么。
 *
 * **界面照 `binds` 分岔，不照调用方式的名字**（硬约束 1）：预设那条路画两个预设下拉，
 * 绑图那条路画四个能力下拉，REST 那条路只有一行说明——它压根不需要在本工具里绑什么。
 * 候选、中文名、合同都由后端给，这一页一个调用方式的名字都不写死。
 */
const routeInfo = ref<ProjectRoute | null>(null)

/**
 * 整份工程绑定表。换调用方式时**整份一起 PUT**：后端那个 body 用的是 `model_dump()`，
 * 只发 `generation_mode` 会把四个能力绑定当成 `null` 一起清空。
 */
const bindings = ref<ProjectWorkflowBindings | null>(null)

/** 绑图那条路才要的图清单；不走那条路就不去拉。 */
const workflows = ref<Workflow[]>([])
const routeBusy = ref(false)
const routeError = ref<ApiError | null>(null)

/** 分布条按 count 过滤：0 的状态不占宽度，也不占图例。 */
const buckets = computed(() => (ov.summary?.shot_status ?? []).filter((b) => b.count > 0))

const STATUS_COLOR: Record<string, string> = {
  draft: 'bg-fg-4/50',
  ready: 'bg-accent/50',
  queued: 'bg-accent/70',
  generating: 'bg-st-running',
  generated: 'bg-st-done',
  approved: 'bg-st-done',
  failed: 'bg-st-failed',
}

const SEVERITY_TONE = { error: 'fail', warning: 'warn', info: 'neutral' } as const

/** 「内置」和「你机器上那份」不是一回事：版本不同，出问题时排查方向也不同。 */
const FFMPEG_SOURCE: Record<string, string> = {
  bundled: '内置',
  path: '系统 PATH',
  configured: '配置指定',
}

async function reload(): Promise<void> {
  if (!pid.value) return
  await ov.load(pid.value)
  presets.value = (
    await settingsApi.presets().catch(() => ({ items: [] }) as { items: PresetRow[] })
  ).items
  await loadRoute()
}

/**
 * 走哪条路 + 整份绑定表。**只读，不抛**：缺什么在 `issues` 里，这一块不该因为解析不出
 * 一条路就把整页的环境栏干掉。
 */
async function loadRoute(): Promise<void> {
  if (!pid.value) {
    routeInfo.value = null
    bindings.value = null
    return
  }
  const [info, rows] = await Promise.all([
    projectsApi.route(pid.value).catch(() => null),
    workflowsApi.projectBindings(pid.value).catch(() => null),
  ])
  routeInfo.value = info
  bindings.value = rows
  // 四个能力下拉只有绑图那条路才画，图的清单也只在那时才拉。
  if (info?.binds === 'workflow' && workflows.value.length === 0) {
    workflows.value = await workflowsApi.globalList().catch(() => [])
  }
}

/**
 * 换调用方式。**整份绑定表一起提交**（理由见 `bindings`），换完重新解析一次——
 * 「缺什么」的答案跟着换：同一份预设在 REST 那条路上根本不会被读。
 */
async function selectMode(value: string): Promise<void> {
  if (!pid.value || !bindings.value) return
  routeBusy.value = true
  routeError.value = null
  try {
    bindings.value = await workflowsApi.setProjectBindings(pid.value, {
      ...bindings.value,
      generation_mode: value as GenerationMode,
    })
    await Promise.all([loadRoute(), ov.load(pid.value)])
  } catch (err) {
    routeError.value = err instanceof ApiError ? err : null
  } finally {
    routeBusy.value = false
  }
}

/** 给一条能力绑一份图（只有绑图那条路读它）。同样整份提交。 */
async function bindCapability(capability: Capability, wid: string): Promise<void> {
  if (!pid.value || !bindings.value) return
  routeBusy.value = true
  routeError.value = null
  try {
    bindings.value = await workflowsApi.setProjectBindings(pid.value, {
      ...bindings.value,
      [capability]: wid || null,
    })
    await Promise.all([loadRoute(), ov.load(pid.value)])
  } catch (err) {
    routeError.value = err instanceof ApiError ? err : null
  } finally {
    routeBusy.value = false
  }
}

/** 两条能力全就绪才算这条路走得通——一条能生成一条不能，那不是「已就绪」。 */
const routeReady = computed(
  () => !!routeInfo.value && routeInfo.value.capabilities.every((c) => c.ready),
)

/** 「跟随设置页」跟的是哪一条。标签从后端给的候选里查，前端不写第二份对照表。 */
const settingsLabel = computed(() => {
  const info = routeInfo.value
  return info?.options.find((o) => !o.inherit && o.name === info.settings_provider)?.label ?? ''
})

/**
 * 一次能喂几个参考素材。**`null` = 不限制，`0` 是有意义的答案**（绑的那份图一个参考图槽位
 * 都没标），所以两者不能都画成「—」。
 */
function slotNumber(n: number | null): string {
  return n === null ? '不限' : String(n)
}

/** 某条能力下可绑的图：只列 ready 的，没校验过的绑上去只会在生成那一刻失败。 */
function readyWorkflows(capability: Capability): Workflow[] {
  return workflows.value.filter((w) => w.capability === capability && w.status === 'ready')
}

/** 概览页那一块只画两条能力（普通镜头 / 衔接与转场），按名字取其中一条。 */
function capOf(capability: string): RouteCapability | null {
  return routeInfo.value?.capabilities.find((c) => c.capability === capability) ?? null
}

/**
 * 两个预设下拉显示的是**真正会提交的那一份**（`route` 已经按继承顺序解析过），所以工程没指定
 * 时这里显示的是设置页那一份。写只写被改动的那一个角色（见 `setVideoPreset`）。
 */
const r2vPreset = computed(() => capOf('image2video')?.preset ?? '')
const flfPreset = computed(() => capOf('first_last_frame')?.preset ?? '')

/** 这条能力最终绑到了什么。**照事实说，不照调用方式的名字**（硬约束 1）。 */
function bindingText(cap: RouteCapability): string {
  return cap.preset ?? cap.workflow_name ?? cap.base_url ?? '还没绑上'
}

/**
 * 「这条路的服务在不在」（`POST /settings/probe` 那一下，概览页替用户做过了）。
 *
 * 它**不回答「绑没绑上」**——那半句在每条能力的 `issues` 里。两件事分开说：地址配对了但机器
 * 没开着，和压根没配地址，出路完全不同。
 */
const probe = computed(() => ov.environment?.generation?.service ?? null)

/** 一条 issue 的身份。`detail` 也算进来：两个角色缺预设时标题一样，缺的却是两份图。 */
function issueKey(issue: ErrorPayload): string {
  return `${issue.code}|${issue.title}|${issue.detail}`
}

/**
 * 两条能力**一字不差地报同一条**（缺地址这种整条路的问题）：摆在上面说一次就够。
 *
 * 逐条重复不是「更完整」——同样四条 suggestions 连着印两遍，用户会以为那是两个不同的问题，
 * 而真正只影响一条能力的那些（缺 R2V / 缺 FL2VA 那份图）反而被埋掉了。
 */
const sharedIssues = computed<ErrorPayload[]>(() => {
  const caps = routeInfo.value?.capabilities ?? []
  const first = caps.length > 1 ? caps[0] : undefined
  if (!first) return []
  return first.issues.filter((issue) =>
    caps.every((cap) => cap.issues.some((other) => issueKey(other) === issueKey(issue))),
  )
})

/** 只有这条能力才有的那些问题（整条路的那些已经在上面说过了）。 */
function ownIssues(cap: RouteCapability): ErrorPayload[] {
  const shared = new Set(sharedIssues.value.map(issueKey))
  return cap.issues.filter((issue) => !shared.has(issueKey(issue)))
}

/**
 * 选一份预设。**只提交这一个角色**：另一份可能是继承来的，原样回发会把它写成工程指定
 * （从此设置页改了也带不动它）。改完重新解析——「缺什么」的答案跟着换。
 */
async function selectPreset(role: 'r2v' | 'flf', name: string): Promise<void> {
  if (!pid.value) return
  presetBusy.value = true
  routeError.value = null
  try {
    await projectsApi.setVideoPreset(pid.value, role, name || null)
    await Promise.all([loadRoute(), ov.load(pid.value)])
  } catch (err) {
    routeError.value = err instanceof ApiError ? err : null
  } finally {
    presetBusy.value = false
  }
}

onMounted(reload)
watch(pid, reload)

/**
 * 下拉里那一段槽位说明。`<option>` 里塞不进徽标，所以拼成一行文字。
 *
 * **三族分开写**：参考图 0 槽要提醒（角色表喂不进去），参考视频 / 参考音频 0 槽是常态
 * （绝大多数图只收图片），所以后两项只在真标了槽位时才出现。
 */
function slotText(preset: PresetRow): string {
  const parts = [`参考图 ${preset.ref_slots} 槽`]
  if (preset.ref_video_slots) parts.push(`视频 ${preset.ref_video_slots} 槽`)
  if (preset.ref_audio_slots) parts.push(`音频 ${preset.ref_audio_slots} 槽`)
  return parts.join(' · ')
}

/** 秒 → `MM:SS`，与时间线页同口径。 */
function duration(sec: number): string {
  const s = Math.max(0, Math.round(sec))
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />

    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1 border-b px-2">
      <AppButton
        size="sm"
        variant="primary"
        :disabled="!ov.summary?.resume"
        :title="
          ov.summary?.resume
            ? `镜头编辑器本轮还是外壳；这里先告诉你最近改的是 #${ov.summary.resume.index_no}`
            : '还没有改动过任何镜头'
        "
      >
        <PlayCircle :size="10" />继续上次工作
      </AppButton>
      <AppButton size="sm" :disabled="ov.checking" @click="ov.check(pid)">
        <ScanSearch :size="10" />{{ ov.checking ? '检查中…' : '连续性检查' }}
      </AppButton>
      <AppButton size="sm" variant="ghost" class="ml-auto" :disabled="ov.busy" @click="reload()">
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>

    <ErrorPanel
      v-if="ov.lastError"
      class="mx-2 mt-2"
      :dismissible="false"
      :error="ov.lastError"
      @dismiss="ov.clearError()"
    />

    <div class="min-h-0 flex-1 overflow-auto">
      <div class="flex gap-2 p-2">
        <!-- 主区 -->
        <div class="flex min-w-0 flex-1 flex-col gap-2">
          <AppPanel title="进度">
            <EmptyState
              v-if="ov.empty"
              title="这个工程还是空的"
              body="链路不能跳跃：先把角色与地点立起来，再写剧本、拆镜头。素材可以从应用级素材库采用，不必每部片子从零重建。"
            >
              <div class="flex flex-wrap items-center justify-center gap-1.5">
                <AppButton
                  variant="primary"
                  @click="$router.push({ name: 'characters', params: { pid } })"
                >
                  <Users :size="11" />去建角色
                </AppButton>
                <AppButton @click="$router.push({ name: 'locations', params: { pid } })">
                  去建地点
                </AppButton>
                <AppButton variant="ghost" @click="$router.push({ name: 'library' })">
                  <Library :size="11" />先看看素材库
                </AppButton>
              </div>
            </EmptyState>
            <div v-else-if="ov.summary" class="space-y-2 p-3">
              <div class="flex items-baseline gap-2">
                <span class="text-fg-1 tnum text-lg leading-none">
                  {{ ov.summary.progress.percent }}%
                </span>
                <span class="text-fg-3 text-2xs">
                  {{ ov.summary.progress.generated }} /
                  {{ ov.summary.progress.total }} 个镜头已有生成结果
                </span>
                <span class="text-fg-4 ml-auto text-2xs">
                  片长合计 <span class="tnum">{{ duration(ov.summary.duration_total) }}</span>
                </span>
              </div>
              <div class="bg-base-3 h-1.5 w-full overflow-hidden">
                <div
                  class="bg-accent h-full"
                  :style="{ width: `${ov.summary.progress.percent}%` }"
                />
              </div>

              <div>
                <div class="bg-base-3 mt-2 flex h-2.5 w-full overflow-hidden">
                  <div
                    v-for="b in buckets"
                    :key="b.status"
                    class="h-full"
                    :class="STATUS_COLOR[b.status] ?? 'bg-fg-4/40'"
                    :style="{ width: `${(b.count / ov.summary.counts.shots) * 100}%` }"
                    :title="`${b.label}：${b.count}`"
                  />
                </div>
                <div class="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                  <span
                    v-for="b in buckets"
                    :key="b.status"
                    class="text-fg-3 flex items-center gap-1 text-2xs"
                  >
                    <span class="h-1.5 w-1.5" :class="STATUS_COLOR[b.status] ?? 'bg-fg-4/40'" />
                    {{ b.label }} <span class="tnum text-fg-1">{{ b.count }}</span>
                  </span>
                </div>
              </div>

              <div
                v-if="ov.summary.queue.active || ov.summary.queue.failed"
                class="text-fg-3 flex items-center gap-2 text-2xs"
              >
                <AppBadge v-if="ov.summary.queue.active" tone="accent">
                  队列进行中 {{ ov.summary.queue.active }}
                </AppBadge>
                <AppBadge v-if="ov.summary.queue.failed" tone="fail">
                  失败 {{ ov.summary.queue.failed }}
                </AppBadge>
              </div>

              <div v-if="ov.summary.resume" class="border-line-1 border-t pt-2">
                <p class="text-fg-3 text-2xs tracking-wide uppercase">上次改到这里</p>
                <p class="text-fg-1 mt-0.5 text-xs">
                  #{{ ov.summary.resume.index_no }}
                  {{ ov.summary.resume.title || '未命名镜头' }}
                  <span class="text-fg-4">
                    · {{ ov.summary.resume.status_label }}
                    <template v-if="ov.summary.resume.scene_title">
                      · {{ ov.summary.resume.scene_title }}
                    </template>
                  </span>
                </p>
              </div>
            </div>
          </AppPanel>

          <AppPanel title="工程内容">
            <div class="grid grid-cols-4 gap-px p-px">
              <component
                :is="c.route ? RouterLink : 'div'"
                v-for="c in COUNT_CARDS"
                :key="c.key"
                :to="c.route ? { name: c.route, params: { pid } } : undefined"
                class="bg-base-2 hover:bg-base-3 flex flex-col gap-0.5 px-2 py-1.5"
              >
                <span class="text-fg-1 tnum text-sm leading-none">
                  {{ ov.summary?.counts[c.key] ?? 0 }}
                </span>
                <span class="text-fg-4 text-2xs">{{ c.label }}</span>
              </component>
            </div>
          </AppPanel>

          <AppPanel title="连续性检查">
            <EmptyState
              v-if="!ov.continuity"
              title="还没有跑过检查"
              body="它遍历全部镜头，找出缺角色表、缺参考图、上游未生成、时长异常之类的问题。只报事实与坐标，绝不自动改数据。"
            >
              <AppButton variant="primary" :disabled="ov.checking" @click="ov.check(pid)">
                <ScanSearch :size="11" />{{ ov.checking ? '检查中…' : '现在检查' }}
              </AppButton>
            </EmptyState>
            <div v-else-if="ov.continuity.clean" class="flex items-center gap-1.5 px-3 py-3">
              <CheckCircle2 :size="14" class="text-st-done" />
              <p class="text-fg-2 text-xs">没有发现问题。</p>
            </div>
            <div v-else>
              <div class="border-line-1 flex items-center gap-1.5 border-b px-2 py-1">
                <AppBadge v-if="ov.continuity.counts.error" tone="fail">
                  错误 {{ ov.continuity.counts.error }}
                </AppBadge>
                <AppBadge v-if="ov.continuity.counts.warning" tone="warn">
                  警告 {{ ov.continuity.counts.warning }}
                </AppBadge>
                <AppBadge v-if="ov.continuity.counts.info" tone="neutral">
                  提示 {{ ov.continuity.counts.info }}
                </AppBadge>
                <span class="text-fg-4 ml-auto text-2xs">按严重程度排序，修与不修由你决定</span>
              </div>
              <ul class="divide-line-1 divide-y">
                <li
                  v-for="(i, n) in ov.continuity.issues"
                  :key="`${i.kind}-${n}`"
                  class="px-2 py-1.5"
                >
                  <div class="flex items-center gap-1.5">
                    <AppBadge :tone="SEVERITY_TONE[i.severity]">{{ i.severity }}</AppBadge>
                    <span class="text-fg-1 min-w-0 truncate text-xs">{{ i.title }}</span>
                    <span
                      v-if="i.shot_index_no !== undefined"
                      class="text-fg-4 tnum ml-auto text-2xs"
                    >
                      #{{ i.shot_index_no }}
                    </span>
                  </div>
                  <p class="text-fg-3 mt-0.5 text-2xs">{{ i.detail }}</p>
                  <ul v-if="i.suggestions.length" class="text-fg-4 mt-0.5 space-y-px text-2xs">
                    <li v-for="s in i.suggestions" :key="s">· {{ s }}</li>
                  </ul>
                </li>
              </ul>
            </div>
          </AppPanel>
        </div>

        <!-- 右：活动 + 环境 -->
        <div class="flex w-72 shrink-0 flex-col gap-2">
          <AppPanel title="最近活动">
            <EmptyState
              v-if="ov.activity.length === 0"
              title="还没有活动"
              body="生成版本、失败与取消、导出都会记在这里，按时间倒序。"
            />
            <ul v-else class="divide-line-1 divide-y">
              <li v-for="(a, n) in ov.activity" :key="`${a.kind}-${n}`" class="px-2 py-1">
                <div class="flex items-start gap-1.5">
                  <Activity :size="10" class="text-fg-4 mt-0.5 shrink-0" />
                  <span class="text-fg-2 min-w-0 flex-1 text-2xs">{{ a.text }}</span>
                </div>
                <p v-if="a.at" class="text-fg-4 mt-px pl-4 text-2xs">{{ a.at }}</p>
              </li>
            </ul>
          </AppPanel>

          <AppPanel title="运行环境">
            <ErrorPanel v-if="ov.envError" :dismissible="false" :error="ov.envError" />
            <div v-else-if="ov.environment" class="space-y-2 p-2">
              <div>
                <div class="flex items-center gap-1.5">
                  <span class="text-fg-2 text-2xs">ComfyUI</span>
                  <AppBadge :tone="ov.environment.comfy.online ? 'ok' : 'warn'">
                    {{ ov.environment.comfy.online ? '在线' : '离线' }}
                  </AppBadge>
                </div>
                <p class="text-fg-4 mt-px text-2xs break-words">
                  {{ ov.environment.comfy.detail }}
                </p>
              </div>
              <div>
                <div class="flex items-center gap-1.5">
                  <span class="text-fg-2 text-2xs">FFmpeg</span>
                  <AppBadge :tone="ov.environment.ffmpeg.available ? 'ok' : 'warn'">
                    {{
                      ov.environment.ffmpeg.available
                        ? FFMPEG_SOURCE[ov.environment.ffmpeg.source] || '可用'
                        : '缺失'
                    }}
                  </AppBadge>
                </div>
                <p class="text-fg-4 mt-px text-2xs break-words">
                  {{ ov.environment.ffmpeg.detail }}
                </p>
                <p v-if="ov.environment.ffmpeg.impact" class="text-fg-3 mt-px text-2xs">
                  {{ ov.environment.ffmpeg.impact }}
                </p>
                <p v-if="ov.environment.ffmpeg.hint" class="text-fg-2 mt-0.5 text-2xs break-words">
                  {{ ov.environment.ffmpeg.hint }}
                </p>
              </div>
              <div>
                <div class="flex items-center gap-1.5">
                  <span class="text-fg-2 text-2xs">GPU</span>
                  <AppBadge :tone="ov.environment.gpu.available ? 'ok' : 'neutral'">
                    {{
                      ov.environment.gpu.available ? ov.environment.gpu.name || '可用' : '未探测到'
                    }}
                  </AppBadge>
                </div>
                <p class="text-fg-4 mt-px text-2xs break-words">{{ ov.environment.gpu.detail }}</p>
              </div>
              <!--
                「这个工程怎么出片」：走哪条路 → 这条路要绑什么 → 绑没绑上 → 缺什么。
                四段的顺序就是排查顺序。**照 `binds` 分岔，不照调用方式的名字**（硬约束 1）：
                预设那条路画两个预设下拉，绑图那条路画四个能力下拉，REST 那条路只有一行说明。
              -->
              <div v-if="routeInfo" class="border-line-1 border-t pt-1.5">
                <div class="flex items-center gap-1.5">
                  <p class="text-fg-3 text-2xs tracking-wide uppercase">这个工程怎么出片</p>
                  <AppBadge :tone="routeReady ? 'ok' : 'warn'">
                    {{ routeReady ? '这条路走得通' : '还缺东西' }}
                  </AppBadge>
                </div>

                <ErrorPanel
                  v-if="routeError"
                  class="mt-1"
                  :error="routeError"
                  @dismiss="routeError = null"
                />

                <label class="mt-1 block">
                  <span class="text-fg-4 text-2xs">调用方式</span>
                  <!-- 候选全部来自后端（第一项是「跟随设置页」= 空串）：加一条路这里不用改。 -->
                  <select
                    :value="routeInfo.mode"
                    class="border-line-1 bg-base-2 text-fg-1 mt-px h-6 w-full border px-1.5 text-2xs outline-none"
                    :disabled="routeBusy || !bindings"
                    @change="selectMode(($event.target as HTMLSelectElement).value)"
                  >
                    <option v-for="opt in routeInfo.options" :key="opt.name" :value="opt.name">
                      {{ opt.label }}
                    </option>
                  </select>
                </label>
                <!-- 「这个答案是谁给的」必须写出来：留空的工程占绝大多数，而它和「显式选了
                     这一条」在排查时方向完全不同。 -->
                <p class="text-fg-4 mt-px text-2xs break-words">
                  现在走「{{ routeInfo.label }}」· {{ ROUTE_SOURCE_LABEL[routeInfo.source] }}
                  <template v-if="routeInfo.mode === '' && settingsLabel">
                    （设置页选的是「{{ settingsLabel }}」，在那儿改这里跟着变）
                  </template>
                </p>

                <!-- 预设那条路：两份预设，普通镜头与衔接各一份 -->
                <template v-if="routeInfo.binds === 'preset'">
                  <label class="mt-1 block">
                    <span class="text-fg-4 text-2xs">SHOT · R2V</span>
                    <select
                      :value="r2vPreset"
                      class="border-line-1 bg-base-2 text-fg-1 mt-px h-6 w-full border px-1.5 text-2xs outline-none"
                      :disabled="presetBusy"
                      @change="selectPreset('r2v', ($event.target as HTMLSelectElement).value)"
                    >
                      <option value="">未选择 R2V 预设</option>
                      <option
                        v-for="preset in presets.filter((row) => row.r2v_ready ?? row.ready)"
                        :key="preset.name"
                        :value="preset.name"
                      >
                        {{ preset.name }} · {{ slotText(preset) }}
                      </option>
                    </select>
                  </label>
                  <label class="mt-1 block">
                    <span class="text-fg-4 text-2xs">衔接 · FL2VA 首尾帧</span>
                    <select
                      :value="flfPreset"
                      class="border-line-1 bg-base-2 text-fg-1 mt-px h-6 w-full border px-1.5 text-2xs outline-none"
                      :disabled="presetBusy"
                      @change="selectPreset('flf', ($event.target as HTMLSelectElement).value)"
                    >
                      <option value="">未选择 FL2VA 预设</option>
                      <option
                        v-for="preset in presets.filter((row) => row.flf_ready)"
                        :key="preset.name"
                        :value="preset.name"
                      >
                        {{ preset.name }}
                      </option>
                    </select>
                  </label>
                  <p class="text-fg-4 mt-1 text-2xs">
                    两项可以选择同一份预设；普通 Shot 只用 R2V，明确生成衔接时才用 FL2VA。
                    这里显示的是真正会提交的那一份——工程没指定时它来自设置页那份默认预设。
                  </p>
                  <RouterLink :to="{ name: 'presets' }" class="text-accent mt-1 inline-block text-2xs">
                    管理预设 Workflow
                  </RouterLink>
                </template>

                <!-- 绑图那条路：四个能力各绑一份图。清单只列 ready 的——draft / invalid 选了也不会被
                     `workflows.resolve()` 选中，摆出来只会让人以为绑上了。 -->
                <template v-else-if="routeInfo.binds === 'workflow'">
                  <label v-for="cap in CAPABILITIES" :key="cap" class="mt-1 block">
                    <span class="text-fg-4 text-2xs">{{ CAPABILITY_LABEL[cap] }}</span>
                    <select
                      :value="bindings?.[cap] ?? ''"
                      class="border-line-1 bg-base-2 text-fg-1 mt-px h-6 w-full border px-1.5 text-2xs outline-none"
                      :disabled="routeBusy || !bindings"
                      @change="bindCapability(cap, ($event.target as HTMLSelectElement).value)"
                    >
                      <option value="">未绑定</option>
                      <option v-for="row in readyWorkflows(cap)" :key="row.id" :value="row.id">
                        {{ row.name }}
                      </option>
                    </select>
                  </label>
                  <p class="text-fg-4 mt-1 text-2xs">
                    一份图只服务一个能力：普通镜头走「图生视频」，衔接与转场走「首尾帧」。
                    工程一条都没绑时，每条能力退到应用级那份默认图——下面每条写的才是真正会用的那一份。
                  </p>
                  <RouterLink
                    :to="{ name: 'workflows', params: { pid } }"
                    class="text-accent mt-1 inline-block text-2xs"
                  >
                    管理 Workflow 绑定
                  </RouterLink>
                </template>

                <!-- REST 那条路：没有什么可在工程里绑的，说清这件事本身就是这一块的内容。
                     服务端要实现什么由后端给（`contract`）——写死在前端的话，改合同就得改两处。 -->
                <template v-else-if="routeInfo.binds === 'base_url'">
                  <p class="text-fg-2 mt-1 text-2xs break-words">
                    这条路不需要工作流绑定：首末帧与参考素材整组按 REST
                    合同发过去；地址在设置页配，密钥不回显。
                  </p>
                  <p class="text-fg-4 mt-1 text-2xs break-words">
                    地址：{{ routeInfo.capabilities[0]?.base_url || '还没配' }}
                  </p>
                  <!-- 服务端要实现什么由后端给（`contract`）。**地址还没配时不在这儿印**：那种情况
                       下面那条四要素错误的 suggestions 里已经一条条写着了，连印两遍只会让人以为
                       是两件事。 -->
                  <ul
                    v-if="routeInfo.capabilities[0]?.base_url"
                    class="text-fg-4 mt-1 space-y-px text-2xs"
                  >
                    <li v-for="line in routeInfo.contract" :key="line">· {{ line }}</li>
                  </ul>
                  <RouterLink :to="{ name: 'settings' }" class="text-accent mt-1 inline-block text-2xs">
                    去设置页配地址与密钥
                  </RouterLink>
                </template>

                <!-- 两条能力一字不差地报同一条时（缺地址这种整条路的问题），摆在上面说一次。
                     逐条重复不是「更完整」：同样四条 suggestions 连着印两遍，用户会以为那是
                     两个不同的问题。 -->
                <div
                  v-for="(issue, i) in sharedIssues"
                  :key="`shared-${i}`"
                  class="border-st-failed/40 bg-base-2 mt-1.5 border p-1.5"
                >
                  <p class="text-st-review text-2xs">{{ issue.title }}</p>
                  <p class="text-fg-2 mt-px text-2xs break-words">{{ issue.detail }}</p>
                  <ul v-if="issue.suggestions.length" class="text-fg-3 mt-px space-y-px text-2xs">
                    <li v-for="s in issue.suggestions" :key="s">· {{ s }}</li>
                  </ul>
                  <p class="text-fg-4 mt-px text-2xs">{{ issue.code }}</p>
                </div>

                <!-- 绑没绑上 / 缺什么：**按能力各答一次**。同一个工程的普通镜头与衔接可以一条
                     走得通、一条缺东西（预设那条路上两份图本来就不同），合成一个「就绪」
                     就会让人以为补转场也能跑。 -->
                <div
                  v-for="cap in routeInfo.capabilities"
                  :key="cap.capability"
                  class="border-line-1 mt-1.5 border-t pt-1"
                >
                  <div class="flex items-center gap-1">
                    <CheckCircle2 v-if="cap.ready" :size="12" class="text-st-done shrink-0" />
                    <CircleSlash v-else :size="12" class="text-st-review shrink-0" />
                    <span class="text-fg-2 text-2xs">{{ cap.capability_label }}</span>
                    <span class="text-fg-4 min-w-0 flex-1 truncate text-2xs" :title="bindingText(cap)">
                      {{ bindingText(cap) }}
                    </span>
                  </div>
                  <!-- 一次能喂几个参考素材。**`null` = 不限，`0` 是有意义的答案**（绑的那份图一个
                       参考图槽位都没标），所以两者不能都画成「—」。 -->
                  <p class="text-fg-4 mt-px text-2xs break-words">
                    参考素材 图 {{ slotNumber(cap.slots.image) }} · 视频
                    {{ slotNumber(cap.slots.video) }} · 音频 {{ slotNumber(cap.slots.audio) }}
                    <!-- 还没绑上时后端没有「这个数字是谁给的」可说（`source` 是空串）：
                         那就一个字都不写，别留一个「来自」在那儿吊着。 -->
                    <template v-if="cap.slots.source">· 来自 {{ cap.slots.source }}</template>
                  </p>
                  <p class="text-fg-4 text-2xs break-words">{{ cap.slots.detail }}</p>
                  <!-- 缺什么：四要素原样摆出来，suggestions 一条都不省（硬约束 4）。
                       整条路都缺的那些已经在上面说过一次，这里只留**这条能力自己**的。 -->
                  <div
                    v-for="(issue, i) in ownIssues(cap)"
                    :key="i"
                    class="border-st-failed/40 bg-base-2 mt-1 border p-1.5"
                  >
                    <p class="text-st-review text-2xs">{{ issue.title }}</p>
                    <p class="text-fg-2 mt-px text-2xs break-words">{{ issue.detail }}</p>
                    <ul v-if="issue.suggestions.length" class="text-fg-3 mt-px space-y-px text-2xs">
                      <li v-for="s in issue.suggestions" :key="s">· {{ s }}</li>
                    </ul>
                    <p class="text-fg-4 mt-px text-2xs">{{ issue.code }}</p>
                  </div>
                </div>

                <!-- 「服务在不在」是另一个问题：上面那份账单只管**绑没绑上**，探测才知道那台机器
                     应不应答。所以这一行只在探测失败时出现（成功时上面的 ComfyUI 徽标已经说过
                     一遍），四要素照旧原样摆出来。

                     **还缺东西的时候不印**：地址那一栏是空的时，探测当然也连不上——上面那条
                     四要素错误说的是同一件事的起因，两条并列只会让人以为要修两处。 -->
                <div
                  v-if="routeReady && probe && !probe.ok"
                  class="border-st-failed/40 bg-base-2 mt-1.5 border p-1.5"
                >
                  <p class="text-st-review text-2xs">这条路的服务连不上</p>
                  <!-- `detail` 里已经带着地址（「http://… 无法访问：…」），再拼一次 `target`
                       只会把同一个地址说两遍。 -->
                  <p class="text-fg-2 mt-px text-2xs break-words">{{ probe.detail }}</p>
                  <ul
                    v-if="probe.error?.suggestions.length"
                    class="text-fg-3 mt-px space-y-px text-2xs"
                  >
                    <li v-for="s in probe.error.suggestions" :key="s">· {{ s }}</li>
                  </ul>
                </div>
              </div>
            </div>
            <EmptyState
              v-else
              title="尚未探测"
              body="打开工程后会自动探测一次 ComfyUI / FFmpeg / GPU。探测失败不影响手动路径。"
            />
          </AppPanel>
        </div>
      </div>

      <div class="border-line-1 border-t p-2">
        <ChainStrip />
      </div>
    </div>
  </div>
</template>
