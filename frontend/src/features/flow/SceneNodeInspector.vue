<script setup lang="ts">
/**
 * 选中那一幕的检查器（幕流程图右栏）。
 *
 * 节点上只**看**，改动全在这里做。四个刻意的设计：
 *
 *   1. **prompt 是必填的**，所以它排第一，空的时候保存按钮禁用并在 tooltip 里说明原因——
 *      按下去没反应比按不下去更让人怀疑功能坏了。
 *   2. **人物 / 地点可以一个都不选**，但各自不能超过 `node_limit`。到上限后未选中的那些
 *      直接禁用，tooltip 里写「上限可改：设置页…」——和后端拒绝时给的建议是同一句话。
 *   3. **地点的第一条就是主地点**（同步 `scene.location_variant_id`），所以能改顺序：
 *      「设为主地点」= 把它挪到第一位，不是另一个字段。
 *   4. **采用是镜头级的**，所以视频列表按镜头分组：一幕下面有很多镜头，每个镜头各自
 *      独立生成很多段，「用哪一段」只能一个镜头一个镜头地定，时间线装配认的也正是它
 *      （`Shot.current_version_id`）。采用只改「用哪一段」，一条版本都不会被删（硬约束 3）；
 *      幕上刻意**没有**「主视频」这种东西——那种指针和导出用的那一段迟早会各说一套。
 *      不能当候选的那些连原因一起列出来。
 *   5. **挑人物 / 地点要看得见图**：两张清单每行左边是缩略图（`thumbnail_path`，
 *      后端给的相对路径），只给名字的话用户得先去角色页 / 地点页翻一遍才知道哪个是哪个。
 *      没有图的照旧列出来并给占位——没有角色表的形象能挂，只是喂不出参考图。
 */
import { computed, ref, watch } from 'vue'
import { Check, MapPin, PackagePlus, Star, Type, Users } from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppThumb from '@/shared/ui/AppThumb.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import { fileUrl } from '@/shared/api/files'
import type { FlowNode } from '@/shared/api/sequence'
import ExportPackageDialog from '@/features/packages/ExportPackageDialog.vue'
import { useFlowStore } from '@/stores/flow'

const props = defineProps<{ pid: string; node: FlowNode }>()
/** 「进这一幕的工作台」由外面负责跳转，这一栏不认识路由。 */
const emit = defineEmits<{ open: [] }>()

const flow = useFlowStore()
const prompt = ref(props.node.prompt ?? '')
/** 「导出这一幕」：把这一幕的设定搬到另一个工程去（人物 / 地点 / 道具 / 镜头结构 + 素材）。 */
const exporting = ref(false)
/** 列表里正在预览的那一段。一次只开一个播放器，十几段视频同时解码没必要。 */
const previewId = ref('')

watch(
  () => [props.node.id, props.node.prompt] as const,
  ([, text]) => {
    prompt.value = text ?? ''
  },
)
watch(
  () => props.node.id,
  () => {
    previewId.value = ''
    // 换了一幕就关掉导出弹窗：里面那份账单说的是上一幕，留着只会导错。
    exporting.value = false
  },
)

const castIds = computed(() => props.node.cast.map((c) => c.appearance_id))
const castSet = computed(() => new Set(castIds.value))
const locIds = computed(() => props.node.locations.map((l) => l.location_variant_id))
const locSet = computed(() => new Set(locIds.value))
const castFull = computed(() => castIds.value.length >= props.node.node_limit)
const locFull = computed(() => locIds.value.length >= props.node.node_limit)
const dirty = computed(() => prompt.value.trim() !== (props.node.prompt ?? '').trim())
const saveHint = computed(() => {
  if (!prompt.value.trim()) return 'prompt 是这一幕唯一必填的小节点，不能存成空的'
  if (!dirty.value) return '和已保存的一样，没有要存的改动'
  return '保存这一幕的 prompt'
})

/** 和后端 `story.py::LIMIT_HINT` 说的是同一件事，用户在两处看到同一句话。 */
const LIMIT_HINT = '上限可改：设置页「幕（流程图节点）」→「一幕里人物 / 地点的上限」'

function capTitle(what: string): string {
  return `这一幕的${what}已到上限 ${props.node.node_limit} 个，先去掉一个。${LIMIT_HINT}`
}

function clipUrl(path: string | null): string {
  return path ? fileUrl(props.pid, path) : ''
}

function fmt(n: number | null): string {
  return n ? `${Math.round(n * 10) / 10}s` : '—'
}

async function savePrompt(): Promise<void> {
  if (!prompt.value.trim() || !dirty.value) return
  await flow.saveScene(props.pid, props.node.id, { prompt: prompt.value.trim() }).catch(() => {})
}

async function toggleCast(appearanceId: string): Promise<void> {
  const next = castSet.value.has(appearanceId)
    ? castIds.value.filter((i) => i !== appearanceId)
    : [...castIds.value, appearanceId]
  await flow.setSceneCast(props.pid, props.node.id, next).catch(() => {})
}

async function toggleLocation(variantId: string): Promise<void> {
  const next = locSet.value.has(variantId)
    ? locIds.value.filter((i) => i !== variantId)
    : [...locIds.value, variantId]
  await flow.setSceneLocations(props.pid, props.node.id, next).catch(() => {})
}

/** 主地点 = 列表第一条，所以「设为主地点」就是把它挪到最前面。 */
async function makePrimary(variantId: string): Promise<void> {
  const next = [variantId, ...locIds.value.filter((i) => i !== variantId)]
  await flow.setSceneLocations(props.pid, props.node.id, next).catch(() => {})
}

/** 采用为**这一段所属镜头**的成片。没有取消——换一段就是再采用一次。 */
async function adopt(versionId: string): Promise<void> {
  await flow.adoptShotVideo(props.pid, props.node.id, versionId).catch(() => {})
}
</script>

<template>
  <AppPanel :title="`第 ${node.index_no} 幕`" class="min-h-0 flex-1">
    <template #actions>
      <AppButton
        size="sm"
        variant="ghost"
        title="把这一幕的设定导出成一个包，搬到另一个工程去"
        @click="exporting = true"
      >
        <PackagePlus :size="10" />导出这一幕
      </AppButton>
      <AppButton
        size="sm"
        variant="ghost"
        title="进第二级：这一幕的工作台（双击节点也一样）"
        @click="emit('open')"
      >
        进工作台
      </AppButton>
    </template>
    <div class="min-h-0 flex-1 overflow-auto p-2">
      <p class="text-fg-1 truncate text-2xs" :title="node.title">{{ node.title }}</p>
      <p class="text-fg-4 text-2xs">
        {{ node.generated_count }}/{{ node.shot_count }} 镜头已出片 · {{ fmt(node.duration_total) }}
      </p>
      <ul v-if="node.issues.length" class="text-st-review mt-1 space-y-px text-2xs">
        <li v-for="i in node.issues" :key="i">· {{ i }}</li>
      </ul>

      <!-- 小节点 1：prompt。唯一必填的那个 -->
      <section class="border-line-1 mt-2 border-t pt-2">
        <p class="text-fg-3 flex items-center gap-1 text-2xs tracking-wide uppercase">
          <Type :size="10" />prompt
          <AppBadge :tone="node.prompt_ok ? 'ok' : 'warn'">
            {{ node.prompt_ok ? '已填' : '必填' }}
          </AppBadge>
        </p>
        <textarea
          v-model="prompt"
          rows="3"
          placeholder="这一幕要看到什么。镜头自己写了 prompt 时以镜头为准，这里是兜底"
          class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-1 w-full border px-1.5 py-1 text-2xs outline-none"
        ></textarea>
        <AppButton
          size="sm"
          variant="primary"
          class="mt-1"
          :disabled="flow.busy || !dirty || !prompt.trim()"
          :title="saveHint"
          @click="savePrompt()"
        >
          <Check :size="10" />保存 prompt
        </AppButton>
      </section>

      <!-- 小节点 2：人物。可以一个都不选，但不能超过上限 -->
      <section class="border-line-1 mt-2 border-t pt-2">
        <p class="text-fg-3 flex items-center gap-1 text-2xs tracking-wide uppercase">
          <Users :size="10" />人物
          <AppBadge
            :tone="castFull ? 'warn' : 'neutral'"
            :title="castFull ? capTitle('人物') : LIMIT_HINT"
          >
            {{ node.cast.length }}/{{ node.node_limit }}
          </AppBadge>
        </p>
        <p v-if="flow.castOptions.length === 0" class="text-fg-4 mt-1 text-2xs">
          还没有角色形象。先去角色页建一个——挂上形象，它的角色表才会进这一幕的上下文。
        </p>
        <ul v-else class="mt-1 space-y-px">
          <li v-for="c in flow.castOptions" :key="c.appearance_id">
            <label
              class="hover:bg-base-2 flex items-center gap-1 px-0.5 py-0.5"
              :title="!castSet.has(c.appearance_id) && castFull ? capTitle('人物') : c.label"
            >
              <input
                type="checkbox"
                :checked="castSet.has(c.appearance_id)"
                :disabled="flow.busy || (castFull && !castSet.has(c.appearance_id))"
                class="accent-accent"
                @change="toggleCast(c.appearance_id)"
              />
              <AppThumb :pid="pid" :path="c.thumbnail_path" :label="c.label" />
              <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">{{ c.label }}</span>
              <AppBadge v-if="!c.has_sheet" tone="warn" title="这个形象还没有角色表，进不了上下文">
                无角色表
              </AppBadge>
            </label>
          </li>
        </ul>
      </section>
      <!-- 小节点 3：地点。第一条同时是主地点 -->
      <section class="border-line-1 mt-2 border-t pt-2">
        <p class="text-fg-3 flex items-center gap-1 text-2xs tracking-wide uppercase">
          <MapPin :size="10" />地点
          <AppBadge
            :tone="locFull ? 'warn' : 'neutral'"
            :title="locFull ? capTitle('地点') : LIMIT_HINT"
          >
            {{ node.locations.length }}/{{ node.node_limit }}
          </AppBadge>
        </p>
        <p v-if="flow.variantOptions.length === 0" class="text-fg-4 mt-1 text-2xs">
          还没有地点变体。先去地点页建一个——「城南旧宅 · 雨夜」这样的变体才带得动参考图。
        </p>
        <ul v-else class="mt-1 space-y-px">
          <li v-for="v in flow.variantOptions" :key="v.id">
            <label
              class="hover:bg-base-2 flex items-center gap-1 px-0.5 py-0.5"
              :title="!locSet.has(v.id) && locFull ? capTitle('地点') : v.label"
            >
              <input
                type="checkbox"
                :checked="locSet.has(v.id)"
                :disabled="flow.busy || (locFull && !locSet.has(v.id))"
                class="accent-accent"
                @change="toggleLocation(v.id)"
              />
              <AppThumb :pid="pid" :path="v.thumbnail_path" :label="v.label" />
              <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">{{ v.label }}</span>
              <AppBadge
                v-if="locIds[0] === v.id"
                tone="accent"
                title="主地点：镜头的地点参考图从它来"
              >
                主
              </AppBadge>
              <button
                v-else-if="locSet.has(v.id)"
                class="text-fg-4 hover:text-accent"
                title="设为主地点（挪到第一位）"
                @click.prevent="makePrimary(v.id)"
              >
                <Star :size="10" />
              </button>
            </label>
          </li>
        </ul>
      </section>
      <!-- 已生成的视频：**按镜头分组**，每个镜头各自采用一段（= 它的当前版本） -->
      <section class="border-line-1 mt-2 border-t pt-2">
        <p class="text-fg-3 text-2xs tracking-wide uppercase">
          每个镜头采用哪一段（{{ flow.videos?.adopted_count ?? 0 }}/{{
            flow.videos?.shots.length ?? 0
          }}
          已采用）
        </p>
        <EmptyState
          v-if="!flow.videos || flow.videos.total === 0"
          title="暂无已生成视频"
          body="这一幕还没有可播的成片。去工作台生成，或在上面按「先看账单 → 执行编排」。"
        />
        <div v-else class="mt-1 space-y-2">
          <div v-for="g in flow.videos.shots" :key="g.shot_id">
            <p class="flex items-center gap-1">
              <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs" :title="g.title">
                {{ g.index_no }}. {{ g.title }}
              </span>
              <AppBadge v-if="g.kind === 'transition'" tone="neutral">转场</AppBadge>
              <AppBadge
                :tone="g.adopted_version_id ? 'ok' : 'warn'"
                :title="
                  g.adopted_version_id
                    ? '这个镜头已经定了用哪一段：时间线装配、下游抽末帧都认它'
                    : '这个镜头还没定用哪一段，时间线装配时会缺这一格'
                "
              >
                {{ g.adopted_version_id ? '已采用' : '未采用' }}
              </AppBadge>
            </p>
            <ul class="mt-0.5 space-y-1">
              <li
                v-for="v in g.items"
                :key="v.id"
                class="bg-base-2 border px-1 py-0.5"
                :class="v.is_adopted ? 'border-accent/60' : 'border-line-1'"
              >
                <div class="flex items-center gap-1">
                  <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs"
                    >v{{ v.version_no }}</span
                  >
                  <span class="text-fg-4 tnum text-2xs">{{ fmt(v.duration) }}</span>
                </div>
                <div class="mt-0.5 flex flex-wrap items-center gap-1">
                  <AppBadge v-if="v.is_adopted" tone="ok" title="时间线导出的就是这一段">
                    已采用
                  </AppBadge>
                  <AppBadge v-if="v.source === 'manual'" tone="neutral" title="手工挂进来的版本">
                    手工
                  </AppBadge>
                  <AppButton
                    size="sm"
                    variant="ghost"
                    :title="previewId === v.id ? '收起预览' : '在这里播一下'"
                    @click="previewId = previewId === v.id ? '' : v.id"
                  >
                    {{ previewId === v.id ? '收起' : '预览' }}
                  </AppButton>
                  <AppButton
                    v-if="!v.is_adopted"
                    size="sm"
                    variant="primary"
                    :disabled="flow.busy"
                    title="采用这一段：把它设成这个镜头的当前版本，旧版本一条都不会删"
                    @click="adopt(v.id)"
                  >
                    采用
                  </AppButton>
                </div>
                <video
                  v-if="previewId === v.id && clipUrl(v.asset_path)"
                  :src="clipUrl(v.asset_path)"
                  controls
                  preload="metadata"
                  class="bg-base-3 mt-1 max-h-40 w-full"
                />
              </li>
            </ul>
            <ul v-if="g.omitted.length" class="mt-0.5 space-y-px">
              <li
                v-for="v in g.omitted"
                :key="v.id"
                class="border-line-1 bg-base-2 border border-dashed px-1 py-0.5"
              >
                <p class="text-fg-2 truncate text-2xs">v{{ v.version_no }} · 不能当候选</p>
                <p class="text-fg-4 text-2xs">{{ v.reason }}</p>
              </li>
            </ul>
          </div>
        </div>
        <p v-if="flow.videos" class="text-fg-4 mt-1 text-2xs">{{ flow.videos.note }}</p>
      </section>
    </div>

    <ExportPackageDialog v-model:open="exporting" :pid="pid" :sid="node.id" />
  </AppPanel>
</template>
