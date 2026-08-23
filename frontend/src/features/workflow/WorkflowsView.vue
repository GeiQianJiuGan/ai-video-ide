<script setup lang="ts">
/**
 * Workflow 管理（Step 4 的前端）。
 *
 * 这一页是硬约束「业务层不绑定具体视频模型」的操作面：镜头只说要什么能力，
 * 哪套图、哪个节点的哪个字段接 prompt，全在这里配。配完能力矩阵才会亮。
 *
 * 两个刻意的设计：
 *   1. **绑定下拉只列图里真实存在的标量字段**（后端 `nodes[].fields` 给的），
 *      所以不可能绑到一个不存在的字段上；连线出来的输入不在列表里，因为它不能被覆盖。
 *   2. **校验分两段**——「校验绑定」不碰 ComfyUI（`probe=false`），「校验 + 探测节点」才连。
 *      ComfyUI 不在线是常态而不是故障，所以离线时默认那颗按钮仍然可用。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
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
import { useWorkflowStore } from '@/stores/workflows'

const route = useRoute()
const wf = useWorkflowStore()

const pid = computed(() => String(route.params.pid ?? ''))

const importing = ref(false)
const importName = ref('')
const importCapability = ref<Capability>('text2image')
const importJson = ref('')
const importNotes = ref('')
/** 选文件时就地校验 JSON——把「这不是一份 API 格式的图」提前到导入之前说。 */
const importProblem = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

/** 绑定草稿：改了才提交，避免每动一个下拉就打一次接口。 */
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
  await wf.load(pid.value).catch(() => {})
}

onMounted(reload)
watch(pid, reload)

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
    const graph = JSON.parse(text) as unknown
    const count =
      graph && typeof graph === 'object' && !Array.isArray(graph)
        ? Object.keys(graph as Record<string, unknown>).length
        : 0
    importProblem.value =
      count === 0 ? '这份 JSON 里没有节点。要的是 ComfyUI 的「API 格式」导出。' : ''
  } catch {
    importProblem.value = '这不是合法的 JSON。请用 ComfyUI 的「Save (API Format)」重新导出。'
  }
  importing.value = true
}

async function confirmImport(): Promise<void> {
  if (pid.value) return
  const name = importName.value.trim()
  if (!name || !importJson.value) return
  try {
    await wf.importWorkflow(pid.value, {
      name,
      capability: importCapability.value,
      api_json: importJson.value,
      notes: importNotes.value.trim() || null,
    })
    importing.value = false
    importJson.value = ''
  } catch {
    // 错误已经进 store.lastError，弹窗留着让用户改名字或换能力再试
  }
}

async function saveBindings(): Promise<void> {
  if (pid.value) return
  const wid = detail.value?.id
  if (!wid) return
  // 空字符串表示「不绑」，不要把它当成一个绑定发过去
  const body: Record<string, string> = {}
  for (const [slot, value] of Object.entries(draft.value)) if (value) body[slot] = value
  await wf.bind(pid.value, wid, body).catch(() => {})
}

async function runValidate(probe: boolean): Promise<void> {
  if (pid.value) return
  const wid = detail.value?.id
  if (!wid) return
  await wf.validate(pid.value, wid, probe)
}

async function saveField(key: 'name' | 'notes' | 'status', value: string): Promise<void> {
  if (pid.value) return
  const wid = detail.value?.id
  if (!wid) return
  await wf.update(pid.value, wid, { [key]: value }).catch(() => {})
}

async function saveProjectBinding(capability: Capability, value: string): Promise<void> {
  if (!pid.value) return
  await wf.setProjectBindings(pid.value, {
    ...projectBindings.value,
    [capability]: value || null,
  }).catch(() => {})
}

async function saveGenerationMode(value: GenerationMode): Promise<void> {
  if (!pid.value) return
  await wf.setProjectBindings(pid.value, {
    ...projectBindings.value,
    generation_mode: value,
  }).catch(() => {})
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />

    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1 border-b px-2">
      <AppButton v-if="!pid" size="sm" variant="primary" :disabled="wf.busy" @click="fileInput?.click()">
        <Plus :size="10" />导入 Workflow
      </AppButton>
      <input
        ref="fileInput"
        type="file"
        accept=".json,application/json"
        class="hidden"
        @change="onPickFile"
      />
      <AppButton v-if="!pid" size="sm" :disabled="!detail || wf.busy" @click="runValidate(false)">
        <ShieldCheck :size="10" />校验绑定
      </AppButton>
      <AppButton
        v-if="!pid"
        size="sm"
        :disabled="!detail || wf.busy"
        title="连 ComfyUI 检查自定义节点是否都装了；离线时会明说探测失败，绑定检查照常"
        @click="runValidate(true)"
      >
        <Plug :size="10" />校验 + 探测节点
      </AppButton>
      <AppButton
        v-if="!pid"
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
            <ul v-else class="mt-0.5">
              <li v-for="w in byCapability(cap)" :key="w.id">
                <button
                  class="hover:bg-base-2 flex w-full items-center gap-1.5 px-1.5 py-1 text-left"
                  :class="w.id === wf.selectedId ? 'bg-accent-dim/40' : ''"
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
      <AppPanel title="节点绑定" class="min-h-0 flex-1">
        <template #actions>
          <span v-if="detail" class="text-fg-4 text-2xs"> 带 * 的是这项能力的必填槽位 </span>
          <AppButton v-if="!pid" size="sm" variant="ghost" :disabled="!dirty" @click="resetDraft()">
            还原
          </AppButton>
          <AppButton
            v-if="!pid"
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
        <div v-else class="p-2">
          <table class="w-full text-2xs">
            <thead>
              <tr class="text-fg-3 border-line-1 border-b text-left">
                <th class="w-32 py-1 font-normal">槽位</th>
                <th class="py-1 font-normal">绑到「节点 · 字段」</th>
              </tr>
            </thead>
            <tbody class="divide-line-1 divide-y">
              <tr v-for="slot in SLOTS" :key="slot">
                <td class="py-1 align-middle">
                  <span :class="requiredSlots.includes(slot) ? 'text-fg-1' : 'text-fg-3'">
                    {{ slot
                    }}<span v-if="requiredSlots.includes(slot)" class="text-st-review">*</span>
                  </span>
                </td>
                <td class="py-1">
                  <div class="flex items-center gap-1">
                    <select
                      v-model="draft[slot]"
                      :disabled="!!pid"
                      class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 min-w-0 flex-1 border px-1 text-2xs outline-none"
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
            没绑的槽位在生成时就是「这套图不接这个参数」——比如超分只需要 source_image，绑上 prompt
            也不会被用到。
          </p>
        </div>
      </AppPanel>

      <!-- 右：能力矩阵 + 校验结果 + 属性 -->
      <AppPanel title="能力矩阵" class="w-72 shrink-0">
        <div class="space-y-3 p-2">
          <section>
            <ul class="space-y-1">
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
                <p class="text-fg-4 mt-0.5 text-2xs">
                  必填槽位：{{ row.required_slots.join(' / ') }}
                </p>
              </li>
            </ul>
          </section>

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">
              当前项目绑定
            </p>
            <p v-if="!pid" class="text-fg-4 mt-1 text-2xs">
              这是应用级 Workflow 资源。打开项目后可在这里绑定项目能力。
            </p>
            <div v-else class="mt-1 space-y-1">
              <label class="block">
                <span class="text-fg-4 text-2xs">项目生成方式</span>
                <select
                  :value="projectBindings.generation_mode"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                  :disabled="wf.busy"
                  @change="saveGenerationMode(($event.target as HTMLSelectElement).value as GenerationMode)"
                >
                  <option value="comfy_preset">ComfyUI 预设</option>
                  <option value="http_api">通用 REST API</option>
                  <option value="workflow_api">Workflow API 图</option>
                </select>
                <span class="text-fg-4 mt-0.5 block text-2xs">选择 Workflow API 后，生成会使用下面绑定的应用级 Workflow。</span>
              </label>
              <label v-for="cap in CAPABILITIES" :key="cap" class="block">
                <span class="text-fg-4 text-2xs">{{ CAPABILITY_LABEL[cap] }}</span>
                <select
                  :value="projectBindings[cap] ?? ''"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                  :disabled="wf.busy || projectBindings.generation_mode !== 'workflow_api'"
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

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">ComfyUI</p>
            <p class="mt-1 flex items-center gap-1 text-2xs">
              <AppBadge :tone="wf.comfy?.online ? 'ok' : 'warn'">
                {{ wf.comfy?.online ? '在线' : '不在线' }}
              </AppBadge>
              <span class="text-fg-4 min-w-0 truncate">{{ wf.comfy?.base_url }}</span>
            </p>
            <p class="text-fg-4 mt-0.5 text-2xs">{{ wf.comfy?.detail }}</p>
            <p v-if="!wf.comfy?.online" class="text-fg-4 mt-0.5 text-2xs">
              离线不影响导入与绑定：「校验绑定」是纯本地检查，等 ComfyUI 起来再探测自定义节点即可。
            </p>
          </section>

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
              缺这些自定义节点：{{ validation.missing_nodes.join('、') }}
            </p>
            <p v-if="validation.missing_slots.length" class="text-st-review mt-1 text-2xs">
              还没绑：{{ validation.missing_slots.join('、') }}
            </p>
            <p class="text-fg-4 mt-1 text-2xs">{{ validation.checked_at }}</p>
          </section>

          <section v-if="detail" class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">属性</p>
            <div class="mt-1 space-y-1">
              <label class="block">
                <span class="text-fg-4 text-2xs">名字</span>
                <input
                  :value="detail.name"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  :disabled="!!pid"
                  @change="saveField('name', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">备注</span>
                <input
                  :value="detail.notes ?? ''"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  :disabled="!!pid"
                  @change="saveField('notes', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">状态</span>
                <select
                  :value="detail.status"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                  :disabled="!!pid"
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
            <p class="text-fg-4 mt-1 text-2xs">
              手工改成「就绪」不会跳过校验：镜头解析时仍按绑定取值，缺了就在入队前报错。
            </p>
            <AppButton
              v-if="!pid"
              size="sm"
              variant="danger"
              class="mt-1.5"
              :disabled="wf.busy"
              @click="wf.remove(pid, detail.id)"
            >
              <Trash2 :size="10" />删除这套 Workflow
            </AppButton>
          </section>
        </div>
      </AppPanel>
    </div>

    <AppDialog
      v-model:open="importing"
      title="导入 Workflow"
      subtitle="从 ComfyUI 用「Save (API Format)」导出的 json"
      size="md"
    >
      <div class="space-y-2 p-3">
        <p
          v-if="importProblem"
          class="border-st-failed/40 bg-st-failed/5 text-st-failed border px-2 py-1 text-2xs"
        >
          {{ importProblem }}
        </p>
        <label class="block">
          <span class="text-fg-4 text-2xs">名字</span>
          <input
            v-model="importName"
            class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-6 w-full border px-1.5 text-2xs outline-none"
          />
        </label>
        <label class="block">
          <span class="text-fg-4 text-2xs">它提供哪种能力</span>
          <select
            v-model="importCapability"
            class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-6 w-full border px-1 text-2xs outline-none"
          >
            <option v-for="c in CAPABILITIES" :key="c" :value="c">{{ CAPABILITY_LABEL[c] }}</option>
          </select>
        </label>
        <label class="block">
          <span class="text-fg-4 text-2xs">备注（可选）</span>
          <input
            v-model="importNotes"
            placeholder="例如：分辨率上限 1024，出图约 12s"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px h-6 w-full border px-1.5 text-2xs outline-none"
          />
        </label>
        <p class="text-fg-4 text-2xs">
          导入后状态是「草稿」：还要把
          prompt、参考图这些槽位绑到具体节点字段上，校验通过才会变「就绪」，
          也只有就绪的会被镜头选中。
        </p>
      </div>
      <template #footer>
        <span class="text-fg-4 text-2xs">能力选错了也不要紧，删掉重导即可——镜头数据不受影响。</span>
        <AppButton size="sm" variant="ghost" class="ml-auto" @click="importing = false"
          >取消</AppButton
        >
        <AppButton
          size="sm"
          variant="primary"
          :disabled="wf.busy || !importName.trim() || !importJson"
          @click="confirmImport()"
        >
          导入
        </AppButton>
      </template>
    </AppDialog>
  </div>
</template>
