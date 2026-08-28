<script setup lang="ts">
/**
 * 幕流程图（两级场景系统的第一级）。
 *
 * 整片就这一张图：一个节点是一幕，节点之间那一条是**衔接**。这一页要回答两个问题——
 * 「这片子由哪几幕组成、每一幕做到哪一步了」和「两幕之间怎么接上」。
 *
 * 五个刻意的设计：
 *   1. **衔接是可点的一等公民**，不是节点上的一个字段。硬切 / 转场 / 续接末帧三种，
 *      每种的一句话解释由后端给（`link.hint`），前端不复制一份文案。
 *   2. **先账单再动手**。「编排生成」是两步：`plan` 只读地把「要生成几条、要补几段转场、
 *      缺什么」列出来，看完了才 `run`。改了衔接账单立刻作废，逼你重新看一眼。
 *   3. **跳过不是失败**。`run` 回来的 `skipped` 每条都带四要素错误，照常显示，不弹红叉。
 *   4. 节点用 HTML 卡片而不是 SVG 图元——里面要放视频、下拉、徽标，
 *      这些在 `foreignObject` 里的行为在各平台上不一致，连线用一根 1px 的横线就够了。
 *   5. **单击选中、双击进第二级**：节点上只看（成片能直接播、小节点一眼扫完），
 *      改动全在右边的检查器里做。这一层不做镜头级的事。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowRight,
  Bot,
  ClipboardList,
  ListVideo,
  PackageOpen,
  Play,
  Plus,
  RefreshCw,
  X,
} from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import DirectorPanel from '../director/DirectorPanel.vue'
import SceneNodeCard from './SceneNodeCard.vue'
import SceneNodeInspector from './SceneNodeInspector.vue'
import ImportSceneDialog from '@/features/packages/ImportSceneDialog.vue'
import {
  LINK_MODES,
  LINK_MODE_LABEL,
  SEQUENCE_MODES,
  SEQUENCE_MODE_LABEL,
  type LinkMode,
  type SequenceMode,
} from '@/shared/api/sequence'
import { useConsoleStore } from '@/stores/console'
import { useFlowStore } from '@/stores/flow'

const route = useRoute()
const router = useRouter()
const flow = useFlowStore()
const consolePanel = useConsoleStore()

const pid = computed(() => String(route.params.pid ?? ''))
const newSceneTitle = ref('')
/** AI 协作栏。默认开着——它是这一级的核心；关掉也不影响手动编排走完全程。 */
const showDirector = ref(true)
/** 从别的工程导一幕的设定进来。 */
const importing = ref(false)

/** 相邻两幕之间那一段：图上画的连线就是这张表。 */
const segments = computed(() =>
  flow.nodes.slice(0, -1).map((from, i) => {
    const to = flow.nodes[i + 1]!
    const link = flow.linkBetween(from.id, to.id)
    return {
      key: `${from.id}->${to.id}`,
      from,
      to,
      mode: (link?.mode ?? 'cut') as LinkMode,
      duration: link?.duration ?? 1.5,
      hint: link?.hint ?? '不生成任何东西，两幕直接接上。',
      linkId: link?.id ?? '',
      shotId: link?.shot_id ?? null,
    }
  }),
)

function fmt(n: number): string {
  return `${Math.round(n * 10) / 10}s`
}

async function reload(): Promise<void> {
  if (!pid.value) return
  await flow.load(pid.value).catch(() => {})
}

onMounted(reload)
watch(pid, () => reload())

async function addScene(): Promise<void> {
  const title = newSceneTitle.value.trim() || `第 ${flow.nodes.length + 1} 幕`
  newSceneTitle.value = ''
  await flow.addScene(pid.value, { title }).catch(() => {})
}

async function setMode(seg: { from: { id: string }; to: { id: string } }, mode: string) {
  await flow.setLink(pid.value, seg.from.id, seg.to.id, mode as LinkMode).catch(() => {})
}

async function setDuration(seg: { from: { id: string }; to: { id: string } }, value: string) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return
  await flow.setLink(pid.value, seg.from.id, seg.to.id, 'transition', n).catch(() => {})
}

function openScene(sceneId: string): void {
  void router.push({ name: 'scene', params: { pid: pid.value, sid: sceneId } })
}

/** 换编排方式 = 换了要做的事，所以顺手把账单作废，逼用户重新看一眼。 */
function switchMode(value: string): void {
  flow.mode = value as SequenceMode
  flow.discardPlan()
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />
    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1.5 border-b px-2">
      <span class="text-fg-4 text-2xs">编排方式</span>
      <select
        :value="flow.mode"
        class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 w-32 border px-1 text-2xs outline-none"
        @change="switchMode(($event.target as HTMLSelectElement).value)"
      >
        <option v-for="m in SEQUENCE_MODES" :key="m" :value="m">
          {{ SEQUENCE_MODE_LABEL[m] }}
        </option>
      </select>
      <AppButton
        size="sm"
        :disabled="flow.busy || flow.nodes.length === 0"
        title="只算不做：把要生成几条、要补几段转场、缺什么全列出来"
        @click="flow.makePlan(pid)"
      >
        <ClipboardList :size="10" />先看账单
      </AppButton>
      <AppButton
        size="sm"
        variant="primary"
        :disabled="flow.busy || !flow.plan"
        :title="
          flow.plan
            ? `按这份账单入队 ${flow.plan.total_jobs} 个任务`
            : '先出一份账单再执行——不看账单就按下去，做出来的东西你不知道是按什么做的'
        "
        @click="flow.run(pid)"
      >
        <Play :size="10" />执行编排
      </AppButton>
      <AppButton
        size="sm"
        variant="ghost"
        title="在底部控制台的任务框里看它们跑到哪了（不用离开这一页）"
        @click="consolePanel.openWith('jobs')"
      >
        <ListVideo :size="10" />任务
      </AppButton>
      <span class="text-fg-4 tnum text-2xs">
        {{ flow.nodes.length }} 幕 · {{ flow.generatedTotal }}/{{ flow.shotTotal }} 镜头已出片
      </span>
      <input
        v-model="newSceneTitle"
        placeholder="新一幕的标题"
        class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 ml-auto h-5 w-36 border px-1.5 text-2xs outline-none"
        @keyup.enter="addScene()"
      />
      <AppButton size="sm" variant="ghost" :disabled="flow.busy" @click="addScene()">
        <Plus :size="10" />加一幕
      </AppButton>
      <AppButton
        size="sm"
        variant="ghost"
        title="把别的工程导出的一幕（人物 / 地点 / 道具 / 镜头结构 + 素材）导进这个工程"
        @click="importing = true"
      >
        <PackageOpen :size="10" />导入一幕
      </AppButton>
      <AppButton
        size="sm"
        :variant="showDirector ? 'primary' : 'ghost'"
        title="AI 协作栏：跟它说一句话，它提一份可逐条审阅的提案（按下采用之前库里什么都不会变）"
        @click="showDirector = !showDirector"
      >
        <Bot :size="10" />AI 协作
      </AppButton>
      <AppButton size="sm" variant="ghost" :disabled="flow.busy" @click="reload()">
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>
    <ErrorPanel
      v-if="flow.lastError"
      class="mx-2 mt-2"
      :error="flow.lastError"
      @dismiss="flow.clearError()"
    />
    <div class="flex min-h-0 flex-1 gap-2 p-2">
      <AppPanel title="幕流程图" class="min-w-0 flex-1">
        <template #actions>
          <span class="text-fg-4 text-2xs">{{ flow.graph?.note ?? '' }}</span>
        </template>
        <div class="min-h-0 flex-1 overflow-auto p-3">
          <EmptyState
            v-if="flow.nodes.length === 0"
            title="还没有一幕"
            body="右上角「加一幕」建第一幕，或者去剧本页让 AI 拆一版分镜。这张图就是整片的结构。"
          />
          <div v-else class="flex items-stretch gap-0">
            <template v-for="(node, i) in flow.nodes" :key="node.id">
              <!-- 节点：一幕。单击选中、双击进第二级 -->
              <SceneNodeCard
                :pid="pid"
                :node="node"
                :selected="node.id === flow.selectedSceneId"
                @select="flow.select(pid, node.id)"
                @open="openScene(node.id)"
                @remove="flow.removeScene(pid, node.id).catch(() => {})"
              />
              <!-- 连线：这两幕之间的衔接 -->
              <div
                v-if="segments[i]"
                class="flex w-36 shrink-0 flex-col items-center justify-center px-1"
              >
                <div class="border-line-2 w-full border-t"></div>
                <select
                  :value="segments[i]!.mode"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-1 h-5 w-full border px-1 text-2xs outline-none"
                  @change="setMode(segments[i]!, ($event.target as HTMLSelectElement).value)"
                >
                  <option v-for="m in LINK_MODES" :key="m" :value="m">
                    {{ LINK_MODE_LABEL[m] }}
                  </option>
                </select>
                <input
                  v-if="segments[i]!.mode === 'transition'"
                  :value="segments[i]!.duration"
                  type="number"
                  min="0.5"
                  max="4"
                  step="0.5"
                  title="转场视频的长度，1~2 秒最常用"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-1 h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="setDuration(segments[i]!, ($event.target as HTMLInputElement).value)"
                />
                <p class="text-fg-4 mt-1 text-center text-2xs">{{ segments[i]!.hint }}</p>
                <AppBadge v-if="segments[i]!.shotId" tone="accent" class="mt-1">已补转场</AppBadge>
                <ArrowRight :size="12" class="text-fg-4 mt-1" />
              </div>
            </template>
          </div>
          <p v-if="flow.nodes.length" class="text-fg-4 mt-3 text-2xs">
            节点上是这一幕的成片与小节点：单击选中（右边可以改 prompt、挂人物 / 地点、给每个镜头
            挑采用哪一段），双击进这一幕的工作台。连线上那个下拉是衔接：改了它，账单会立刻作废——两幕之间怎么接
            会改变要做多少事。
          </p>
        </div>
      </AppPanel>
      <!-- 右：选中那一幕的检查器 + 编排账单。「说好的」与「做了的」要能对上 -->
      <div class="flex w-80 shrink-0 flex-col gap-2">
        <SceneNodeInspector
          v-if="flow.selectedScene"
          :pid="pid"
          :node="flow.selectedScene"
          @open="openScene(flow.selectedScene.id)"
        />
        <AppPanel title="编排账单" class="min-h-0 flex-1">
          <template #actions>
            <AppButton
              v-if="flow.plan"
              size="sm"
              variant="ghost"
              title="丢掉这份账单"
              @click="flow.discardPlan()"
            >
              <X :size="10" />
            </AppButton>
          </template>
          <div class="min-h-0 flex-1 overflow-auto p-2">
            <EmptyState
              v-if="!flow.plan"
              title="还没有账单"
              body="按「先看账单」算一份：这次会入队几个任务、要补几段转场、哪一条缺什么。只算不做。"
            />
            <template v-else>
              <p class="text-fg-1 text-2xs">
                {{ SEQUENCE_MODE_LABEL[flow.plan.mode as SequenceMode] ?? flow.plan.mode }} · 入队
                {{ flow.plan.total_jobs }} 个任务
              </p>
              <p class="text-fg-4 text-2xs">
                要补 {{ flow.plan.transitions_to_create }} 段转场{{
                  flow.plan.ignored_transitions
                    ? `，忽略 ${flow.plan.ignored_transitions} 条配好的转场`
                    : ''
                }}
              </p>
              <ul v-if="flow.plan.notes.length" class="text-fg-4 mt-1 space-y-px text-2xs">
                <li v-for="n in flow.plan.notes" :key="n">· {{ n }}</li>
              </ul>

              <p class="text-fg-3 mt-2 text-2xs tracking-wide uppercase">按幕</p>
              <ul class="mt-1 space-y-px">
                <li
                  v-for="s in flow.plan.scenes"
                  :key="s.scene_id"
                  class="border-line-1 bg-base-2 border px-1 py-0.5"
                >
                  <div class="flex items-center gap-1">
                    <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">
                      {{ s.index_no }}. {{ s.title }}
                    </span>
                    <span class="text-fg-4 tnum text-2xs">
                      {{ s.ready_count }}/{{ s.shot_count }}
                    </span>
                  </div>
                  <p v-if="s.already_generated" class="text-fg-4 text-2xs">
                    已出片 {{ s.already_generated }} 条，这次照旧只追加新版本。
                  </p>
                  <ul v-if="s.missing.length" class="text-st-review space-y-px text-2xs">
                    <li v-for="m in s.missing" :key="m">· {{ m }}</li>
                  </ul>
                </li>
              </ul>

              <template v-if="flow.plan.blockers.length">
                <p class="text-fg-3 mt-2 text-2xs tracking-wide uppercase">
                  会被跳过（{{ flow.plan.blockers.length }}）
                </p>
                <ul class="mt-1 space-y-px">
                  <li
                    v-for="(b, i) in flow.plan.blockers"
                    :key="`${b.scene_id}-${b.shot_id ?? i}`"
                    class="border-st-failed/40 bg-base-2 border px-1 py-0.5"
                  >
                    <p class="text-fg-2 text-2xs">{{ b.why }}</p>
                    <p class="text-fg-4 text-2xs">怎么办：{{ b.how }}</p>
                  </li>
                </ul>
              </template>
              <template v-if="flow.lastRun">
                <p class="text-fg-3 mt-3 text-2xs tracking-wide uppercase">这次做了什么</p>
                <p class="text-fg-2 mt-1 text-2xs">
                  入队 {{ flow.lastRun.queued.length }} 条 · 补转场
                  {{ flow.lastRun.transitions.length }} 段 · 跳过
                  {{ flow.lastRun.skipped.length }} 条
                </p>
                <ul v-if="flow.lastRun.transitions.length" class="mt-1 space-y-px">
                  <li
                    v-for="t in flow.lastRun.transitions"
                    :key="t.shot_id"
                    class="border-line-1 bg-base-2 border px-1 py-0.5"
                  >
                    <p class="text-fg-2 text-2xs">
                      第 {{ t.link.from_index_no }} 幕 → 第 {{ t.link.to_index_no }} 幕
                      {{ t.reused ? '（已有成片，这次没重做）' : `· ${fmt(t.link.duration ?? 0)}` }}
                    </p>
                    <p v-if="t.note" class="text-fg-4 text-2xs">{{ t.note }}</p>
                  </li>
                </ul>
                <ul v-if="flow.lastRun.skipped.length" class="mt-1 space-y-px">
                  <li
                    v-for="(s, i) in flow.lastRun.skipped"
                    :key="`${s.shot_id ?? 'link'}-${i}`"
                    class="border-st-failed/40 bg-base-2 border px-1 py-0.5"
                  >
                    <p class="text-fg-1 text-2xs">{{ s.error.title }}</p>
                    <p class="text-fg-2 text-2xs">{{ s.error.detail }}</p>
                    <ul class="text-fg-4 space-y-px text-2xs">
                      <li v-for="sg in s.error.suggestions" :key="sg">· {{ sg }}</li>
                    </ul>
                  </li>
                </ul>
                <p v-if="flow.lastRun.chain?.length" class="text-fg-4 mt-1 text-2xs">
                  串成了一条 {{ flow.lastRun.chain.length }} 段的链：每段拿上一段的真末帧当首帧，
                  队列里那些等待都写着在等谁。
                </p>
              </template>
            </template>
          </div>
        </AppPanel>
      </div>
      <!-- 最右：AI 协作栏。提案落库后重拉整张图（幕数、镜头数、衔接都可能变了） -->
      <DirectorPanel v-if="showDirector" :pid="pid" @applied="reload()" />
    </div>

    <!-- 导进来的是一整幕，图上多了一个节点，所以照 DirectorPanel 的规矩重拉整张图 -->
    <ImportSceneDialog v-model:open="importing" :pid="pid" @done="reload()" />
  </div>
</template>
