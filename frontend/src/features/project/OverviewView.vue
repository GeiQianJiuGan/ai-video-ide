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
import { COUNT_CARDS } from '@/shared/api/overview'
import { projectsApi } from '@/shared/api/projects'
import { settingsApi, type PresetRow } from '@/shared/api/settings'
import { useOverviewStore } from '@/stores/overview'

const route = useRoute()
const ov = useOverviewStore()

const pid = computed(() => String(route.params.pid ?? ''))
const presets = ref<PresetRow[]>([])
const presetBusy = ref(false)

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
  presets.value = (await settingsApi.presets().catch(() => ({ items: [] } as { items: PresetRow[] }))).items
}

async function selectPreset(role: 'r2v' | 'flf', name: string): Promise<void> {
  presetBusy.value = true
  try {
    const generation = ov.environment?.generation
    await projectsApi.setVideoPresets(
      pid.value,
      role === 'r2v' ? name || null : generation?.r2v_name ?? null,
      role === 'flf' ? name || null : generation?.flf_name ?? null,
    )
    await ov.load(pid.value)
  } finally {
    presetBusy.value = false
  }
}

onMounted(reload)
watch(pid, reload)

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
              <div v-if="ov.environment.generation" class="border-line-1 border-t pt-1.5">
                <div class="flex items-center gap-1.5">
                  <p class="text-fg-3 text-2xs tracking-wide uppercase">项目视频 Workflow</p>
                  <AppBadge
                    :tone="
                      ov.environment.generation.r2v_ready && ov.environment.generation.flf_ready
                        ? 'ok'
                        : 'warn'
                    "
                  >
                    {{
                      ov.environment.generation.r2v_ready && ov.environment.generation.flf_ready
                        ? '双预设已就绪'
                        : '绑定未完整'
                    }}
                  </AppBadge>
                </div>
                <label class="mt-1 block">
                  <span class="text-fg-4 text-2xs">SHOT · R2V</span>
                  <select
                    :value="ov.environment.generation.r2v_name ?? ''"
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
                      {{ preset.name }} · 参考图 {{ preset.ref_slots }} 槽
                    </option>
                  </select>
                </label>
                <label class="mt-1 block">
                  <span class="text-fg-4 text-2xs">衔接 · FL2VA 首尾帧</span>
                  <select
                    :value="ov.environment.generation.flf_name ?? ''"
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
                <p class="text-fg-4 mt-1 text-2xs">{{ ov.environment.generation.detail }}</p>
                <p class="text-fg-4 mt-1 text-2xs">
                  两项可以选择同一份预设；普通 Shot 只用 R2V，明确生成衔接时才用 FL2VA。
                </p>
                <RouterLink :to="{ name: 'presets' }" class="text-accent mt-1 inline-block text-2xs">
                  管理预设 Workflow
                </RouterLink>
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
