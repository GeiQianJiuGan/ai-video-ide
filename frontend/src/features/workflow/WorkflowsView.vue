<script setup lang="ts">
/**
 * Workflow 管理（Step 4 的前端）。
 *
 * 这一页是硬约束「业务层不绑定具体视频模型」的操作面：镜头只说要什么能力，
 * 哪套图、哪个节点的哪个字段接 prompt，全在这里配。配完能力矩阵才会亮。
 *
 * 功能点：
 *   1. 导入 Workflow：支持自动识别节点（Prompt, Negative Prompt, 首尾帧, 参考图, 种子, 采样步数, 宽高, 时长等）；
 *      如果自动识别不到，可在导入弹窗中直接手动选择节点字段对上。
 *   2. 已经添加的工作流支持编辑名称、备注、能力分类、节点绑定、状态等。
 *   3. 校验绑定与节点探测，并可设为默认或删除。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  CheckCircle2,
  CircleSlash,
  Plug,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Star,
  Trash2,
} from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import {
  CAPABILITIES,
  CAPABILITY_LABEL,
  SLOTS,
  type Capability,
  type GenerationMode,
  type Workflow,
} from '@/shared/api/workflows'
import { projectsApi, ROUTE_SOURCE_LABEL, type ProjectRoute } from '@/shared/api/projects'
import { useWorkflowStore } from '@/stores/workflows'

const route = useRoute()
const wf = useWorkflowStore()

const pid = computed(() => String(route.params.pid ?? ''))

/**
 * 这个工程走哪条路（`GET /projects/{pid}/route`）。这一页只是**绑定那条路**的操作面，
 * 所以第一件事得说清「你现在走的是哪条」——以前这里只有一个下拉，选了 ComfyUI 预设的人
 * 在这儿配了半天四个能力绑定，而那些绑定一个都不会被用到（H9 就是这句话缺失）。
 */
const routeInfo = ref<ProjectRoute | null>(null)

/** 这条路要不要绑图。**四个能力下拉的 `:disabled` 只看这一个布尔**，不看调用方式的名字。 */
const bindsWorkflow = computed(() => routeInfo.value?.binds_workflow ?? false)

/** 为什么禁用 —— 禁了不说原因就是静默失败（硬约束 4）。 */
const bindsHint = computed(() => {
  const info = routeInfo.value
  if (!info) return ''
  if (info.binds_workflow) return '这条路按下面四个能力各自绑的那份图提交。'
  if (info.binds === 'preset')
    return `「${info.label}」不需要工作流绑定：它按预设图里的 AIVS_* 节点标题填参数，要换图请去概览页选预设。`
  if (info.binds === 'base_url')
    return `「${info.label}」不需要工作流绑定：首末帧与参考素材整组按 REST 合同发过去，地址在设置页配。`
  return `「${info.label}」不需要工作流绑定。`
})

/** 「跟随设置页」跟的是哪一条。标签从候选里查，前端不写第二份对照表。 */
const settingsLabel = computed(() => {
  const info = routeInfo.value
  if (!info) return ''
  return info.options.find((o) => !o.inherit && o.name === info.settings_provider)?.label ?? ''
})

/** 绑图的那条调用方式叫什么。**按后端给的 `binds` 找**，不按名字猜（硬约束 1）。 */
const workflowOptionLabel = computed(
  () => routeInfo.value?.options.find((o) => o.binds === 'workflow')?.label ?? '绑图的那一条',
)

const importing = ref(false)
const importName = ref('')
const importCapability = ref<Capability>('text2image')
const importJson = ref('')
const importNotes = ref('')
const importProblem = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

interface ExtractedNode {
  id: string
  class_type: string
  title: string | null
  fields: string[]
}

const importNodes = ref<ExtractedNode[]>([])
const importBindings = ref<Record<string, string>>({})
const autoDetectedSlots = ref<Set<string>>(new Set())

/** 选中的工作流绑定草稿：改了才提交，避免每动一个下拉就打一次接口。 */
const draft = ref<Record<string, string>>({})

const detail = computed(() => wf.detail)

const capabilityRow = computed(() =>
  wf.capabilities.find((c) => c.capability === detail.value?.capability),
)
const requiredSlots = computed(() => capabilityRow.value?.required_slots ?? [])
const projectBindings = computed(() => wf.projectBindings)

/** 图里所有可绑定的「节点.字段」，下拉只从这里取。 */
const fieldOptions = computed(() =>
  (detail.value?.nodes ?? []).flatMap((n) =>
    n.fields.map((f) => ({
      value: `${n.id}.${f}`,
      label: `#${n.id} ${n.title || n.class_type} · ${f}`,
    })),
  ),
)

const importFieldOptions = computed(() =>
  importNodes.value.flatMap((n) =>
    n.fields.map((f) => ({
      value: `${n.id}.${f}`,
      label: `#${n.id} ${n.title || n.class_type} · ${f}`,
    })),
  ),
)

const importRequiredSlots = computed(() => {
  const cap = importCapability.value
  if (cap === 'text2image') return ['prompt']
  if (cap === 'image2video') return ['first_frame']
  if (cap === 'first_last_frame') return ['first_frame', 'last_frame']
  if (cap === 'upscale') return ['source_image']
  return []
})

const dirty = computed(() => {
  const saved = detail.value?.bindings ?? {}
  const keys = new Set([...Object.keys(saved), ...Object.keys(draft.value)])
  for (const k of keys) if ((saved[k] ?? '') !== (draft.value[k] ?? '')) return true
  return false
})

const validation = computed(() => wf.lastValidation ?? detail.value?.validation ?? null)

function resetDraft(): void {
  draft.value = { ...(detail.value?.bindings ?? {}) }
}

watch(detail, resetDraft, { immediate: true })

function statusTone(status: string): 'neutral' | 'accent' | 'ok' | 'warn' | 'fail' {
  if (status === 'ready') return 'ok'
  if (status === 'invalid') return 'fail'
  if (status === 'disabled') return 'neutral'
  return 'warn'
}

const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  ready: '就绪',
  invalid: '不可用',
  disabled: '已停用',
}

function byCapability(cap: string): Workflow[] {
  return wf.list.filter((w) => w.capability === cap)
}

async function reload(): Promise<void> {
  await Promise.all([wf.load(pid.value).catch(() => {}), loadRoute()])
}

/** 只读、绝不抛：缺什么在 `issues` 里，拉不到就整块不画，不该把这一页拖崩。 */
async function loadRoute(): Promise<void> {
  if (!pid.value) {
    routeInfo.value = null
    return
  }
  routeInfo.value = await projectsApi.route(pid.value).catch(() => null)
}

onMounted(reload)
watch(pid, reload)

/**
 * ComfyUI 导出的「API 格式」图：整份都是用户拖进来的外部 JSON，
 * 任何一层都可能缺、可能是别的类型，所以按 `unknown` 收进来再逐字段收窄——
 * 标成 `any` 的话下面那些 `node.inputs` 就没人替我们检查了。
 */
type RawGraph = Record<string, unknown>

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function extractNodesFromGraph(graph: RawGraph): ExtractedNode[] {
  const out: ExtractedNode[] = []
  for (const [nodeId, raw] of Object.entries(graph)) {
    const node = asRecord(raw)
    if (!node) continue
    const inputs = asRecord(node.inputs) ?? {}
    const fields = Object.keys(inputs).filter((k) => !Array.isArray(inputs[k]))
    const meta = asRecord(node._meta)
    out.push({
      id: nodeId,
      class_type: String(node.class_type || ''),
      title: meta?.title ? String(meta.title) : null,
      fields: fields.sort(),
    })
  }
  return out.sort((a, b) => Number(a.id) - Number(b.id) || a.id.localeCompare(b.id))
}

function detectAutoBindings(
  nodes: ExtractedNode[],
  capability: Capability,
): { bindings: Record<string, string>; detected: Set<string> } {
  const bindings: Record<string, string> = {}
  const detected = new Set<string>()
  const titled: Record<string, string> = {}
  const refTitled: Array<{ index: number; target: string }> = []

  for (const n of nodes) {
    const t = (n.title || '').trim().toUpperCase()
    if (!t) continue
    for (const f of n.fields) {
      const target = `${n.id}.${f}`
      if (!titled[t]) titled[t] = target
      const match = t.match(/^AIVS_REF_(\d+)$/)
      if (match && !refTitled.some((r) => r.index === Number(match[1]))) {
        refTitled.push({ index: Number(match[1]), target })
      }
    }
  }

  const aliases: Record<string, string[]> = {
    prompt: ['AIVS_PROMPT'],
    negative_prompt: ['AIVS_NEGATIVE', 'AIVS_NEGATIVE_PROMPT'],
    first_frame: ['AIVS_FIRST_FRAME'],
    last_frame: ['AIVS_LAST_FRAME'],
    source_image: ['AIVS_SOURCE_IMAGE', 'AIVS_FIRST_FRAME'],
    seed: ['AIVS_SEED'],
    steps: ['AIVS_STEPS'],
    width: ['AIVS_WIDTH'],
    height: ['AIVS_HEIGHT'],
    duration: ['AIVS_DURATION'],
  }

  for (const [slot, names] of Object.entries(aliases)) {
    for (const name of names) {
      if (titled[name]) {
        bindings[slot] = titled[name]
        detected.add(slot)
        break
      }
    }
  }

  if (!bindings.reference_image) {
    refTitled.sort((a, b) => a.index - b.index)
    if (refTitled.length && refTitled[0]) {
      bindings.reference_image = refTitled[0].target
      detected.add('reference_image')
    } else if (capability === 'image2video' && titled.AIVS_FIRST_FRAME) {
      bindings.reference_image = titled.AIVS_FIRST_FRAME
      detected.add('reference_image')
    }
  }

  // 启发式探测回退
  const promptNodes = nodes.filter(
    (n) =>
      ['CLIPTextEncode', 'CLIPTextEncodeSDXL', 'ShowText', 'PrimitiveNode', 'Text'].includes(
        n.class_type,
      ) ||
      n.fields.includes('text') ||
      n.fields.includes('prompt'),
  )
  const posPrompts = promptNodes.filter((n) => !/(neg|负|反向)/i.test(n.title || ''))
  const negPrompts = promptNodes.filter((n) => /(neg|负|反向)/i.test(n.title || ''))

  if (!bindings.prompt && posPrompts.length && posPrompts[0]) {
    const firstPos = posPrompts[0]
    const f = firstPos.fields.find((x) => x === 'text' || x === 'prompt') || firstPos.fields[0]
    if (f) {
      bindings.prompt = `${firstPos.id}.${f}`
      detected.add('prompt')
    }
  }

  if (!bindings.negative_prompt) {
    if (negPrompts.length && negPrompts[0]) {
      const firstNeg = negPrompts[0]
      const f = firstNeg.fields.find((x) => x === 'text' || x === 'prompt') || firstNeg.fields[0]
      if (f) {
        bindings.negative_prompt = `${firstNeg.id}.${f}`
        detected.add('negative_prompt')
      }
    } else if (posPrompts.length > 1 && posPrompts[1]) {
      const secondPos = posPrompts[1]
      const f = secondPos.fields.find((x) => x === 'text' || x === 'prompt') || secondPos.fields[0]
      if (f && bindings.prompt !== `${secondPos.id}.${f}`) {
        bindings.negative_prompt = `${secondPos.id}.${f}`
        detected.add('negative_prompt')
      }
    }
  }

  const loadImgNodes = nodes.filter(
    (n) => n.class_type.includes('LoadImage') || n.fields.includes('image'),
  )
  if (loadImgNodes.length && loadImgNodes[0]) {
    const firstImg =
      loadImgNodes.find((n) => /(first|首|start|起始)/i.test(n.title || '')) || loadImgNodes[0]
    const lastImg =
      loadImgNodes.find((n) => /(last|末|尾|end|结束)/i.test(n.title || '')) ||
      (loadImgNodes.length > 1 ? loadImgNodes[1] : null)
    const firstImgField = firstImg.fields.find((x) => x === 'image') || firstImg.fields[0]
    const fImgTarget = firstImgField ? `${firstImg.id}.${firstImgField}` : ''

    if (fImgTarget) {
      if (capability === 'first_last_frame') {
        if (!bindings.first_frame) {
          bindings.first_frame = fImgTarget
          detected.add('first_frame')
        }
        if (!bindings.last_frame && lastImg) {
          const lastField = lastImg.fields.find((x) => x === 'image') || lastImg.fields[0]
          if (lastField) {
            bindings.last_frame = `${lastImg.id}.${lastField}`
            detected.add('last_frame')
          }
        }
      } else if (capability === 'image2video') {
        if (!bindings.first_frame) {
          bindings.first_frame = fImgTarget
          detected.add('first_frame')
        }
        if (!bindings.reference_image) {
          bindings.reference_image = fImgTarget
          detected.add('reference_image')
        }
        if (!bindings.source_image) {
          bindings.source_image = fImgTarget
          detected.add('source_image')
        }
      } else if (capability === 'text2image') {
        if (!bindings.reference_image) {
          bindings.reference_image = fImgTarget
          detected.add('reference_image')
        }
      } else if (capability === 'upscale') {
        if (!bindings.source_image) {
          bindings.source_image = fImgTarget
          detected.add('source_image')
        }
      }
    }
  }

  const samplers = nodes.filter((n) => n.class_type.includes('Sampler'))
  if (samplers.length && samplers[0]) {
    const firstSampler = samplers[0]
    if (!bindings.seed) {
      const sf = firstSampler.fields.find((x) => x === 'seed' || x === 'noise_seed')
      if (sf) {
        bindings.seed = `${firstSampler.id}.${sf}`
        detected.add('seed')
      }
    }
    if (!bindings.steps) {
      const stf = firstSampler.fields.find((x) => x === 'steps')
      if (stf) {
        bindings.steps = `${firstSampler.id}.${stf}`
        detected.add('steps')
      }
    }
  }

  const latents = nodes.filter((n) => n.class_type.includes('Latent'))
  if (latents.length && latents[0]) {
    const firstLatent = latents[0]
    if (!bindings.width) {
      const wf = firstLatent.fields.find((x) => x === 'width')
      if (wf) {
        bindings.width = `${firstLatent.id}.${wf}`
        detected.add('width')
      }
    }
    if (!bindings.height) {
      const hf = firstLatent.fields.find((x) => x === 'height')
      if (hf) {
        bindings.height = `${firstLatent.id}.${hf}`
        detected.add('height')
      }
    }
    if (!bindings.duration) {
      const df = firstLatent.fields.find((x) =>
        ['length', 'num_frames', 'frames', 'duration'].includes(x),
      )
      if (df) {
        bindings.duration = `${firstLatent.id}.${df}`
        detected.add('duration')
      }
    }
  }

  return { bindings, detected }
}

function updateImportAutoDetection(): void {
  if (!importNodes.value.length) return
  const { bindings, detected } = detectAutoBindings(importNodes.value, importCapability.value)
  importBindings.value = { ...bindings }
  autoDetectedSlots.value = detected
}

watch(importCapability, () => {
  updateImportAutoDetection()
})

async function onPickFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  const text = await file.text()
  importJson.value = text
  importName.value = file.name.replace(/\.json$/i, '')
  importNotes.value = ''
  try {
    const graph = JSON.parse(text) as RawGraph
    const count =
      graph && typeof graph === 'object' && !Array.isArray(graph) ? Object.keys(graph).length : 0
    if (count === 0) {
      importProblem.value = '这份 JSON 里没有节点。要的是 ComfyUI 的「API 格式」导出。'
      importNodes.value = []
      importBindings.value = {}
    } else {
      importProblem.value = ''
      importNodes.value = extractNodesFromGraph(graph)
      updateImportAutoDetection()
    }
  } catch {
    importProblem.value = '这不是合法的 JSON。请用 ComfyUI 的「Save (API Format)」重新导出。'
    importNodes.value = []
    importBindings.value = {}
  }
  importing.value = true
}

async function confirmImport(): Promise<void> {
  const name = importName.value.trim()
  if (!name || !importJson.value) return
  try {
    const cleanBindings: Record<string, string> = {}
    for (const [slot, val] of Object.entries(importBindings.value)) {
      if (val) cleanBindings[slot] = val
    }
    await wf.importWorkflow(pid.value, {
      name,
      capability: importCapability.value,
      api_json: importJson.value,
      bindings: Object.keys(cleanBindings).length ? cleanBindings : undefined,
      notes: importNotes.value.trim() || null,
    })
    importing.value = false
    importJson.value = ''
  } catch {
    // 错误已经进 store.lastError，弹窗留着让用户改名字或换能力再试
  }
}

async function saveBindings(): Promise<void> {
  const wid = detail.value?.id
  if (!wid) return
  const body: Record<string, string> = {}
  for (const [slot, value] of Object.entries(draft.value)) if (value) body[slot] = value
  await wf.bind(pid.value, wid, body).catch(() => {})
}

async function runValidate(probe: boolean): Promise<void> {
  const wid = detail.value?.id
  if (!wid) return
  await wf.validate(pid.value, wid, probe)
}

async function saveField(
  key: 'name' | 'notes' | 'capability' | 'status',
  value: string,
): Promise<void> {
  const wid = detail.value?.id
  if (!wid) return
  await wf.update(pid.value, wid, { [key]: value }).catch(() => {})
}

async function saveProjectBinding(capability: Capability, value: string): Promise<void> {
  if (!pid.value) return
  await wf
    .setProjectBindings(pid.value, {
      ...projectBindings.value,
      [capability]: value || null,
    })
    .catch(() => {})
  // 绑上一份图之后「缺什么」就少一条，横幅那句话要跟着变。
  await loadRoute()
}

/**
 * 换调用方式。**整份绑定表一起提交**：`PUT /workflow-bindings` 收的是完整对象，
 * 只发 `generation_mode` 会把四个能力绑定一起清空。
 *
 * 换完要重新解析一次路由——「绑没绑上」的答案跟着换（同一份图在另一条路上根本不会被读）。
 */
async function saveGenerationMode(value: GenerationMode): Promise<void> {
  if (!pid.value) return
  await wf
    .setProjectBindings(pid.value, {
      ...projectBindings.value,
      generation_mode: value,
    })
    .catch(() => {})
  await loadRoute()
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />

    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1 border-b px-2">
      <AppButton size="sm" variant="primary" :disabled="wf.busy" @click="fileInput?.click()">
        <Plus :size="10" />导入 Workflow
      </AppButton>
      <input
        ref="fileInput"
        type="file"
        accept=".json,application/json"
        class="hidden"
        @change="onPickFile"
      />
      <AppButton size="sm" :disabled="!detail || wf.busy" @click="runValidate(false)">
        <ShieldCheck :size="10" />校验绑定
      </AppButton>
      <AppButton
        size="sm"
        :disabled="!detail || wf.busy"
        title="连 ComfyUI 检查自定义节点是否都装了；离线时会明说探测失败，绑定检查照常"
        @click="runValidate(true)"
      >
        <Plug :size="10" />校验 + 探测节点
      </AppButton>
      <AppButton
        size="sm"
        :disabled="!detail || wf.busy || detail.is_default === 1"
        title="新镜头默认用这套 Workflow"
        @click="detail && wf.setDefault(pid, detail.id)"
      >
        <Star :size="10" />设为默认
      </AppButton>
      <span class="text-fg-4 text-2xs">
        {{ wf.list.length }} 套 · 能力就绪 {{ wf.readyCount }}/{{ wf.capabilities.length }}
      </span>
      <AppButton size="sm" variant="ghost" class="ml-auto" :disabled="wf.busy" @click="reload()">
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>

    <!--
      顶部横幅：**这一页配的东西只在「ComfyUI 工作流绑定」那条路上生效**，所以先把当前
      走的是哪条、这个答案是谁给的说清楚。走别的路时下面四个能力下拉是禁用的，
      禁了不说原因就是静默失败（硬约束 4）。
    -->
    <div
      v-if="routeInfo"
      class="border-line-1 bg-base-2 flex shrink-0 flex-wrap items-center gap-1.5 border-b px-2 py-1"
    >
      <AppBadge :tone="bindsWorkflow ? 'accent' : 'neutral'">
        {{ bindsWorkflow ? '这条路绑图' : '这条路不绑图' }}
      </AppBadge>
      <span class="text-fg-1 text-2xs">
        当前走「{{ routeInfo.label }}」·
        <span class="text-fg-4">{{ ROUTE_SOURCE_LABEL[routeInfo.source] }}</span>
      </span>
      <span class="text-fg-4 min-w-0 flex-1 truncate text-2xs">{{ bindsHint }}</span>
      <RouterLink
        v-if="pid"
        :to="{ name: 'dashboard', params: { pid } }"
        class="text-accent shrink-0 text-2xs hover:underline"
        title="调用方式与预设都在概览页的「这个工程怎么出片」里改"
      >
        去概览页改这条路
      </RouterLink>
    </div>

    <ErrorPanel
      v-if="wf.lastError"
      class="mx-2 mt-2"
      :error="wf.lastError"
      @dismiss="wf.clearError()"
    />

    <div class="flex min-h-0 flex-1 gap-2 p-2">
      <!-- 左：按能力分组的列表 -->
      <AppPanel title="Workflow" class="w-60 shrink-0">
        <EmptyState
          v-if="wf.list.length === 0"
          title="还没有导入 Workflow"
          body="在 ComfyUI 里把能跑通的图用「Save (API Format)」导出，拖进来登记成一种能力。镜头只说要什么能力，不关心用的是哪个模型。"
        />
        <div v-else class="space-y-2 p-1.5">
          <section v-for="cap in CAPABILITIES" :key="cap">
            <p class="text-fg-3 flex items-center gap-1 px-0.5 text-2xs tracking-wide uppercase">
              {{ CAPABILITY_LABEL[cap] }}
              <span class="text-fg-4">{{ byCapability(cap).length }}</span>
            </p>
            <p v-if="byCapability(cap).length === 0" class="text-fg-4 px-0.5 py-0.5 text-2xs">
              还没有，这项能力做不出来
            </p>
            <ul v-else class="mt-0.5 space-y-0.5">
              <li v-for="w in byCapability(cap)" :key="w.id">
                <button
                  class="hover:bg-base-2 flex w-full items-center gap-1.5 rounded-xs px-1.5 py-1 text-left"
                  :class="
                    w.id === wf.selectedId
                      ? 'bg-accent-dim/40 border border-accent/40'
                      : 'border border-transparent'
                  "
                  @click="wf.select(pid, w.id)"
                >
                  <span class="min-w-0 flex-1">
                    <span class="text-fg-1 block truncate text-xs">{{ w.name }}</span>
                    <span class="text-fg-4 block truncate text-2xs">
                      {{ w.nodes.length }} 节点 · {{ Object.keys(w.bindings).length }} 项绑定
                    </span>
                  </span>
                  <AppBadge v-if="w.is_default === 1" tone="accent" title="新镜头默认用它"
                    >默认</AppBadge
                  >
                  <AppBadge :tone="statusTone(w.status)">
                    {{ STATUS_LABEL[w.status] ?? w.status }}
                  </AppBadge>
                </button>
              </li>
            </ul>
          </section>
        </div>
      </AppPanel>

      <!-- 中：节点绑定 -->
      <AppPanel title="节点绑定与配置" class="min-h-0 flex-1">
        <template #actions>
          <span v-if="detail" class="text-fg-4 text-2xs"> 带 * 的是必填槽位 </span>
          <AppButton size="sm" variant="ghost" :disabled="!dirty" @click="resetDraft()">
            还原
          </AppButton>
          <AppButton
            size="sm"
            variant="primary"
            :disabled="!detail || !dirty || wf.busy"
            @click="saveBindings()"
          >
            <Save :size="10" />保存绑定
          </AppButton>
        </template>
        <EmptyState
          v-if="!detail"
          title="尚无选中 Workflow"
          body="选一套（或先导入一套），这里把 prompt、参考图、时长、种子等槽位绑到图里的具体节点字段。"
        />
        <EmptyState
          v-else-if="fieldOptions.length === 0"
          title="这份图里没有可绑定的字段"
          body="只有节点上的标量输入能被绑定——连线进来的输入由上游节点决定，覆盖不了。确认导出的是 API 格式。"
        />
        <div v-else class="p-2 space-y-2">
          <div
            class="border-line-1 bg-base-2 border p-1.5 text-2xs text-fg-3 flex items-center justify-between"
          >
            <span
              >当前正在配置 <strong>{{ detail.name }}</strong
              >（{{
                CAPABILITY_LABEL[detail.capability as Capability] ?? detail.capability
              }}）</span
            >
            <span class="text-fg-4"
              >{{ detail.nodes.length }} 个节点 · {{ fieldOptions.length }} 个可绑定输入项</span
            >
          </div>

          <table class="w-full text-2xs">
            <thead>
              <tr class="text-fg-3 border-line-1 border-b text-left">
                <th class="w-36 py-1 font-normal">输入槽位</th>
                <th class="py-1 font-normal">绑定的「节点 · 输入项」</th>
              </tr>
            </thead>
            <tbody class="divide-line-1 divide-y">
              <tr v-for="slot in SLOTS" :key="slot" class="hover:bg-base-2/50">
                <td class="py-1.5 align-middle">
                  <span
                    :class="requiredSlots.includes(slot) ? 'text-fg-1 font-medium' : 'text-fg-3'"
                  >
                    {{ slot
                    }}<span v-if="requiredSlots.includes(slot)" class="text-st-review">*</span>
                  </span>
                </td>
                <td class="py-1.5">
                  <div class="flex items-center gap-1.5">
                    <select
                      v-model="draft[slot]"
                      class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-6 min-w-0 flex-1 border px-1.5 text-2xs outline-none"
                    >
                      <option value="">未绑定</option>
                      <option v-for="o in fieldOptions" :key="o.value" :value="o.value">
                        {{ o.label }}
                      </option>
                    </select>
                    <AppBadge v-if="requiredSlots.includes(slot) && !draft[slot]" tone="warn">
                      必填未绑
                    </AppBadge>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <p class="text-fg-4 mt-2 text-2xs">
            没绑的槽位在生成时就是「这套图不接这个参数」——例如超分只需要 source_image，绑上 prompt
            也不会被用到。修改后请点击右上角「保存绑定」。
          </p>
        </div>
      </AppPanel>

      <!-- 右：能力矩阵 + 属性编辑 + 校验结果 -->
      <AppPanel title="属性与能力状态" class="w-80 shrink-0">
        <div class="space-y-3 p-2">
          <!-- 属性编辑 -->
          <section v-if="detail" class="space-y-2">
            <div class="flex items-center justify-between">
              <p class="text-fg-3 text-2xs font-medium tracking-wide uppercase">
                Workflow 信息编辑
              </p>
            </div>
            <div class="space-y-1.5 border-line-1 bg-base-2 border p-2">
              <label class="block">
                <span class="text-fg-4 text-2xs">工作流名称</span>
                <input
                  :value="detail.name"
                  class="border-line-1 bg-base-1 text-fg-1 focus:border-accent/60 mt-0.5 h-6 w-full border px-1.5 text-2xs outline-none font-medium"
                  placeholder="工作流名称"
                  @change="saveField('name', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">能力类型</span>
                <select
                  :value="detail.capability"
                  class="border-line-1 bg-base-1 text-fg-1 focus:border-accent/60 mt-0.5 h-6 w-full border px-1 text-2xs outline-none"
                  @change="saveField('capability', ($event.target as HTMLSelectElement).value)"
                >
                  <option v-for="c in CAPABILITIES" :key="c" :value="c">
                    {{ CAPABILITY_LABEL[c] }}
                  </option>
                </select>
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">备注说明</span>
                <input
                  :value="detail.notes ?? ''"
                  placeholder="例如：生图速度快，适合写实风"
                  class="border-line-1 bg-base-1 text-fg-1 focus:border-accent/60 mt-0.5 h-6 w-full border px-1.5 text-2xs outline-none"
                  @change="saveField('notes', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">启用状态</span>
                <select
                  :value="detail.status"
                  class="border-line-1 bg-base-1 text-fg-1 focus:border-accent/60 mt-0.5 h-6 w-full border px-1 text-2xs outline-none"
                  @change="saveField('status', ($event.target as HTMLSelectElement).value)"
                >
                  <option
                    v-for="s in ['draft', 'ready', 'invalid', 'disabled']"
                    :key="s"
                    :value="s"
                  >
                    {{ STATUS_LABEL[s] }}
                  </option>
                </select>
              </label>
            </div>
            <div class="flex items-center gap-2">
              <AppButton
                size="sm"
                variant="danger"
                class="w-full"
                :disabled="wf.busy"
                @click="wf.remove(pid, detail.id)"
              >
                <Trash2 :size="10" />删除此 Workflow
              </AppButton>
            </div>
          </section>

          <!-- 能力矩阵 -->
          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">能力矩阵</p>
            <ul class="mt-1 space-y-1">
              <li
                v-for="row in wf.capabilities"
                :key="row.capability"
                class="border-line-1 border bg-base-2 px-1.5 py-1"
              >
                <p class="flex items-center gap-1">
                  <CheckCircle2 v-if="row.ready" :size="10" class="text-st-done shrink-0" />
                  <CircleSlash v-else :size="10" class="text-st-review shrink-0" />
                  <span class="text-fg-1 text-2xs">
                    {{ CAPABILITY_LABEL[row.capability as Capability] ?? row.capability }}
                  </span>
                  <span class="text-fg-4 ml-auto tnum text-2xs">
                    就绪 {{ row.ready_count }}/{{ row.workflow_count }}
                  </span>
                </p>
                <p v-if="row.default_workflow_name" class="text-fg-4 mt-0.5 truncate text-2xs">
                  默认：{{ row.default_workflow_name }}
                </p>
                <p v-if="row.impact" class="text-st-review mt-0.5 text-2xs">{{ row.impact }}</p>
              </li>
            </ul>
          </section>

          <!-- 项目级生成绑定 -->
          <section v-if="pid" class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">当前项目生成方式绑定</p>
            <div class="mt-1 space-y-1">
              <label v-if="routeInfo" class="block">
                <span class="text-fg-4 text-2xs">项目调用方式</span>
                <!-- 候选全部来自后端（`route.options`，第一项是「跟随选设置页」那条空串）：
                     前端一个调用方式的名字都不写死，后端加一条路这里不用改。 -->
                <select
                  :value="projectBindings.generation_mode"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                  :disabled="wf.busy"
                  @change="
                    saveGenerationMode(($event.target as HTMLSelectElement).value as GenerationMode)
                  "
                >
                  <option v-for="opt in routeInfo.options" :key="opt.name" :value="opt.name">
                    {{ opt.label }}
                  </option>
                </select>
              </label>
              <p v-else class="text-st-review text-2xs">
                读不到这个工程走哪条路，先点右上角「刷新」；期间下面的绑定改了也可能不生效。
              </p>
              <!-- 「跟随设置页」得说清跟的是哪一条，否则这个下拉等于什么都没说。 -->
              <p v-if="routeInfo?.mode === '' && settingsLabel" class="text-fg-4 text-2xs">
                跟着设置页走，现在是「{{ settingsLabel }}」。
              </p>
              <!-- 禁用的原因摆在被禁的控件旁边，而不是只在页顶那条横幅里（硬约束 4）。 -->
              <p v-if="routeInfo && !bindsWorkflow" class="text-st-review text-2xs">
                {{ bindsHint }}下面四个下拉这条路不读，所以先禁着——改成「{{
                  workflowOptionLabel
                }}」才会生效。
              </p>
              <label v-for="cap in CAPABILITIES" :key="cap" class="block">
                <span class="text-fg-4 text-2xs">{{ CAPABILITY_LABEL[cap] }}</span>
                <select
                  :value="projectBindings[cap] ?? ''"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                  :disabled="wf.busy || !bindsWorkflow"
                  :title="bindsWorkflow ? '这条能力提交时用的就是这份图' : bindsHint"
                  @change="saveProjectBinding(cap, ($event.target as HTMLSelectElement).value)"
                >
                  <option value="">未绑定</option>
                  <option
                    v-for="w in byCapability(cap).filter((row) => row.status === 'ready')"
                    :key="w.id"
                    :value="w.id"
                  >
                    {{ w.name }}
                  </option>
                </select>
              </label>
            </div>
          </section>

          <!-- ComfyUI 状态 -->
          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">ComfyUI 状态</p>
            <p class="mt-1 flex items-center gap-1 text-2xs">
              <AppBadge :tone="wf.comfy?.online ? 'ok' : 'warn'">
                {{ wf.comfy?.online ? '在线' : '不在线' }}
              </AppBadge>
              <span class="text-fg-4 min-w-0 truncate">{{ wf.comfy?.base_url }}</span>
            </p>
            <p class="text-fg-4 mt-0.5 text-2xs">{{ wf.comfy?.detail }}</p>
          </section>

          <!-- 最近一次校验 -->
          <section v-if="validation" class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">最近一次校验</p>
            <p class="mt-1 flex items-center gap-1">
              <AppBadge :tone="validation.ok ? 'ok' : 'fail'">
                {{ validation.ok ? '通过' : '未通过' }}
              </AppBadge>
              <span class="text-fg-4 text-2xs">{{ validation.probe }}</span>
            </p>
            <ul v-if="validation.problems.length" class="text-fg-2 mt-1 space-y-px text-2xs">
              <li v-for="p in validation.problems" :key="p">· {{ p }}</li>
            </ul>
            <p v-if="validation.missing_nodes.length" class="text-st-review mt-1 text-2xs">
              缺自定义节点：{{ validation.missing_nodes.join('、') }}
            </p>
            <p v-if="validation.missing_slots.length" class="text-st-review mt-1 text-2xs">
              未绑槽位：{{ validation.missing_slots.join('、') }}
            </p>
          </section>
        </div>
      </AppPanel>
    </div>

    <!-- 导入 Workflow 弹窗：支持自动识别与手动选择绑定 -->
    <AppDialog
      v-model:open="importing"
      title="导入 Workflow"
      subtitle="从 ComfyUI 用「Save (API Format)」导出的 json"
      size="lg"
    >
      <div class="space-y-3 p-3 max-h-[75vh] overflow-y-auto">
        <p
          v-if="importProblem"
          class="border-st-failed/40 bg-st-failed/5 text-st-failed border px-2 py-1 text-2xs"
        >
          {{ importProblem }}
        </p>

        <div class="grid grid-cols-2 gap-2">
          <label class="block">
            <span class="text-fg-4 text-2xs">工作流名称</span>
            <input
              v-model="importName"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-6 w-full border px-1.5 text-2xs outline-none"
            />
          </label>
          <label class="block">
            <span class="text-fg-4 text-2xs">提供能力</span>
            <select
              v-model="importCapability"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-6 w-full border px-1 text-2xs outline-none"
            >
              <option v-for="c in CAPABILITIES" :key="c" :value="c">
                {{ CAPABILITY_LABEL[c] }}
              </option>
            </select>
          </label>
        </div>

        <label class="block">
          <span class="text-fg-4 text-2xs">备注（可选）</span>
          <input
            v-model="importNotes"
            placeholder="例如：分辨率上限 1024，出图约 12s"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px h-6 w-full border px-1.5 text-2xs outline-none"
          />
        </label>

        <!-- 节点识别与手动映射区 -->
        <div v-if="importNodes.length" class="border-line-1 bg-base-2/60 border p-2 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-fg-2 text-2xs font-medium"
              >节点识别与绑定映射（已自动识别，可手动调整）</span
            >
            <span class="text-fg-4 text-2xs">解析出 {{ importNodes.length }} 个节点</span>
          </div>

          <table class="w-full text-2xs border-collapse">
            <thead>
              <tr class="text-fg-3 border-line-1 border-b text-left">
                <th class="w-36 py-1 font-normal">槽位</th>
                <th class="py-1 font-normal">对应「节点 · 字段」</th>
                <th class="w-20 py-1 font-normal text-right">状态</th>
              </tr>
            </thead>
            <tbody class="divide-line-1 divide-y">
              <tr v-for="slot in SLOTS" :key="slot" class="hover:bg-base-2">
                <td class="py-1">
                  <span
                    :class="
                      importRequiredSlots.includes(slot) ? 'text-fg-1 font-medium' : 'text-fg-3'
                    "
                  >
                    {{ slot
                    }}<span v-if="importRequiredSlots.includes(slot)" class="text-st-review"
                      >*</span
                    >
                  </span>
                </td>
                <td class="py-1">
                  <select
                    v-model="importBindings[slot]"
                    class="border-line-1 bg-base-1 text-fg-1 focus:border-accent/60 h-5 w-full border px-1 text-2xs outline-none"
                  >
                    <option value="">未绑定</option>
                    <option v-for="o in importFieldOptions" :key="o.value" :value="o.value">
                      {{ o.label }}
                    </option>
                  </select>
                </td>
                <td class="py-1 text-right">
                  <AppBadge v-if="autoDetectedSlots.has(slot) && importBindings[slot]" tone="ok">
                    已识别
                  </AppBadge>
                  <AppBadge
                    v-else-if="importRequiredSlots.includes(slot) && !importBindings[slot]"
                    tone="warn"
                  >
                    必填未绑
                  </AppBadge>
                  <span v-else-if="importBindings[slot]" class="text-fg-4 text-3xs">手动选择</span>
                  <span v-else class="text-fg-4 text-3xs">—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <p class="text-fg-4 text-2xs">
          导入后可在列表选择该 Workflow 继续微调。完成绑定并校验通过后即可在镜头中选用。
        </p>
      </div>
      <template #footer>
        <span class="text-fg-4 text-2xs">能力选错了也不要紧，导入后支持随时修改。</span>
        <AppButton size="sm" variant="ghost" class="ml-auto" @click="importing = false">
          取消
        </AppButton>
        <AppButton
          size="sm"
          variant="primary"
          :disabled="wf.busy || !importName.trim() || !importJson || !!importProblem"
          @click="confirmImport()"
        >
          导入 Workflow
        </AppButton>
      </template>
    </AppDialog>
  </div>
</template>
