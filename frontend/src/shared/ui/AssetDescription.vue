<script setup lang="ts">
/**
 * 一句素材描述：**模型引用这个素材时唯一看得到的说明**。
 *
 * 它摆在四处（素材页、角色的定妆图、地点变体的参考图、道具图）加素材库，所以是共用件。
 * 四个刻意的取舍：
 *   1. **AI 只填输入框，不落库**。`describe/suggest` 两头都是只读的，落库只有「用户按
 *      保存」这一条路（`PATCH /assets/{id}`，素材库那侧是已有的 `note`）。
 *   2. **先账单再动手**：进来先拉一次 `describe/plan`（只读、不出网），于是「这个端能不能
 *      看图」「这张图会不会被跳过」「字数上限是多少」在点之前就知道。
 *   3. **字数上限只认后端给的 `desc_max`**（截断规则只在 `providers/base.py::ref_hint`
 *      那一处），前端不写死第二份。
 *   4. **做不了就 disabled + tooltip 写原因**，绝不画假界面；手填那条路永远可用。
 */
import { computed, ref, watch } from 'vue'
import { Sparkles, Save } from '@lucide/vue'
import AppButton from './AppButton.vue'
import ErrorPanel from './ErrorPanel.vue'
import { ApiError } from '@/shared/api/client'
import { assetsApi } from '@/shared/api/assets'
import { libraryApi } from '@/shared/api/library'
import { describeApi, DESCRIBE_SOURCE_LABEL, type DescribePlan } from '@/shared/api/describe'

const props = withDefaults(
  defineProps<{
    /** 工程 id。`target === 'library'` 时用不到。 */
    pid?: string
    /** 工程资产 id，或素材库资产 id（看 `target`）。 */
    assetId: string
    /** 库里现在那一句。父组件刷新后传新值进来即可。 */
    description?: string | null
    /** 素材库那侧写的是已有的 `note` 列，且没有工程上下文，所以 AI 看图走不通。 */
    target?: 'project' | 'library'
  }>(),
  { pid: '', description: null, target: 'project' },
)

const emit = defineEmits<{ saved: [string] }>()

const text = ref('')
const plan = ref<DescribePlan | null>(null)
const err = ref<ApiError | null>(null)
const saving = ref(false)
const asking = ref(false)
/** 上一次 AI 那一句是怎么来的（看图 / 只按名字）。用户得知道该不该信它。 */
const source = ref('')
const notes = ref<string[]>([])

/** 上限只有后端那一份。还没拿到账单时不显示计数，也不假装知道是 120。 */
const descMax = computed(() => plan.value?.desc_max ?? 0)
const dirty = computed(() => text.value !== (props.description ?? ''))
const item = computed(() => plan.value?.items[0] ?? null)

/**
 * 「AI 补全」为什么不能点。空串 = 能点。
 * 端不认图不在这里——那时照旧能按名字与已有设定写一句，只是要标明来源。
 */
const aiOff = computed(() => {
  if (props.target === 'library')
    return '素材库里的素材没有工程上下文；AI 看图补全请在工程的素材页里做（这里可以手填）'
  if (!plan.value) return '正在看这一张能不能做…'
  if (plan.value.missing.length) {
    const m = plan.value.missing[0]
    if (m) return `${m.title}：${m.suggestions[0] ?? m.detail}`
  }
  if (!plan.value.can_run)
    return item.value?.warnings[0] ?? '这一张做不了（视频 / 音频或文件已不在），手填一句同样有效'
  return ''
})

async function loadPlan(): Promise<void> {
  plan.value = null
  if (props.target !== 'project' || !props.pid || !props.assetId) return
  try {
    plan.value = await describeApi.plan(props.pid, [props.assetId])
  } catch (e) {
    // 账单拉不到不该把输入框也废掉：手填那条路照旧走得通，原因显示出来就好。
    if (e instanceof ApiError) err.value = e
  }
}

watch(
  () => [props.assetId, props.description] as const,
  () => {
    text.value = props.description ?? ''
    source.value = ''
    notes.value = []
    err.value = null
  },
  { immediate: true },
)
watch(() => [props.assetId, props.target, props.pid] as const, loadPlan, { immediate: true })

/** 让 AI 写一句。**只填进输入框**——落库仍然要用户按下面那颗保存。 */
async function ask(): Promise<void> {
  if (aiOff.value || asking.value) return
  asking.value = true
  err.value = null
  try {
    const out = await describeApi.suggest(props.pid, [props.assetId])
    const row = out.items[0]
    if (!row) return
    if (row.error) {
      err.value = new ApiError(row.error, 200)
      return
    }
    text.value = row.suggestion
    source.value = DESCRIBE_SOURCE_LABEL[row.source] ?? row.source
    notes.value = row.warnings
  } catch (e) {
    if (e instanceof ApiError) err.value = e
  } finally {
    asking.value = false
  }
}

/** 落库。**清空传 `''`**：`null` 在后端是「这次不改」。 */
async function save(): Promise<void> {
  if (saving.value) return
  saving.value = true
  err.value = null
  try {
    if (props.target === 'library') await libraryApi.patchAsset(props.assetId, { note: text.value })
    else await assetsApi.update(props.pid, props.assetId, { description: text.value })
    source.value = ''
    emit('saved', text.value)
  } catch (e) {
    if (e instanceof ApiError) err.value = e
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="space-y-1">
    <div class="flex items-center justify-between gap-2">
      <span class="text-fg-3 text-2xs">
        {{ target === 'library' ? '备注（描述）' : '描述' }}
      </span>
      <span v-if="descMax" class="text-2xs" :class="text.length > descMax ? 'text-st-review' : 'text-fg-4'">
        {{ text.length }} / {{ descMax }}
        <template v-if="text.length > descMax">（超出的部分拼进 prompt 时会被截掉）</template>
      </span>
    </div>

    <textarea
      v-model="text"
      rows="3"
      placeholder="只写画面里看得见的：外形、服装、材质、光线、机位。留空的话，模型引用它时只看到一个文件名。"
      class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 w-full resize-y rounded-sm border px-1.5 py-1 text-2xs"
    />

    <p class="text-fg-4 text-2xs">模型引用这个素材时看到的就是这一句。</p>

    <div class="flex items-center gap-1.5">
      <AppButton size="sm" variant="primary" :disabled="!dirty || saving" @click="save">
        <Save :size="10" />{{ saving ? '保存中…' : '保存' }}
      </AppButton>
      <AppButton
        size="sm"
        :disabled="Boolean(aiOff) || asking"
        :title="aiOff || (plan && !plan.vision_count ? '这个端不能看图，只会按名字与已有设定写' : '让 AI 看着这张图写一句')"
        @click="ask"
      >
        <Sparkles :size="10" />{{ asking ? '让 AI 看看…' : 'AI 补全' }}
      </AppButton>
      <span v-if="source" class="text-fg-4 text-2xs">AI 的这一句：{{ source }} · 还没保存</span>
    </div>

    <ul v-if="notes.length" class="text-fg-4 text-2xs space-y-px">
      <li v-for="n in notes" :key="n">· {{ n }}</li>
    </ul>
    <p v-if="aiOff && target === 'project' && plan" class="text-fg-4 text-2xs">{{ aiOff }}</p>

    <ErrorPanel :error="err" @dismiss="err = null" />
  </div>
</template>
