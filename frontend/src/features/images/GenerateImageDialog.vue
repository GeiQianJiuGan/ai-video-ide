<script setup lang="ts">
/**
 * 「生成一张参考图」弹窗（第三条生成链在界面上的唯一入口）。
 *
 * 角色形象 / 地点变体 / 道具 / 镜头首末帧候选全都用这一个框——出图不是第 16 个功能页，
 * 给它单开一页会逼用户离开正在看的角色去另一处填提示词。
 *
 * 三条规矩照后端的口径来，界面上不再解释第二遍：
 *
 *   1. **文案全部来自 `imagesApi.skills()`**——SKILL 的标题、适用场景、系统会固定补齐的
 *      那几句都是后端给的，组件里不抄第二份。加一份 SKILL 时这里一行不用改。
 *   2. **先账单再动手**——一进来就 `plan()`（只读），把「用哪个协议、拼出来的正 / 负向
 *      prompt 全文、图会落到哪里、缺什么」摆出来，再给「生成」按钮。
 *   3. **用户那段话只写「长什么样」**——占位符照 `skill.lead` 写；四视图、纯白背景、
 *      无文字那些结构由 SKILL 补，用户重复写只会互相打架。
 *
 * 图片服务没配置时**不画假界面**：账单照旧给（`can_generate === false` + `missing[]`
 * 是四要素错误），「生成」按钮禁用，去设置页的引导由 `ErrorPanel` 的 suggestions 带出来。
 */
import { computed, ref, watch } from 'vue'
import { Sparkles } from '@lucide/vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import { ApiError } from '@/shared/api/client'
import {
  imagesApi,
  type ImageJob,
  type ImagePlan,
  type ImageSkill,
  type ImageSkills,
} from '@/shared/api/images'

const props = defineProps<{
  open: boolean
  pid: string
  /** appearance / location_variant / prop / shot_first_frame / shot_last_frame */
  targetKind: string
  targetId: string
  /** 标题上那一句「给谁出图」。留空就用账单里的 `target_label`。 */
  what?: string
}>()

const emit = defineEmits<{
  'update:open': [boolean]
  /** 入队成功。调用方据此刷新自己那一页（图要等队列跑完才落进来）。 */
  queued: [ImageJob]
}>()

const listing = ref<ImageSkills | null>(null)
const skill = ref('')
const text = ref('')
const plan = ref<ImagePlan | null>(null)
const planning = ref(false)
const submitting = ref(false)
const error = ref<ApiError | null>(null)
const queued = ref<ImageJob | null>(null)

const current = computed<ImageSkill | null>(
  () =>
    listing.value?.items.find((s) => s.name === (plan.value?.skill.name ?? skill.value)) ?? null,
)
/** 账单里那条「现在做不了」的四要素错误。没有就是能生成。 */
const blocked = computed(() => plan.value?.missing[0] ?? null)
const canGenerate = computed(
  () => Boolean(plan.value?.can_generate) && !submitting.value && !planning.value,
)

/** 账单是只读的，所以敲字的时候可以放心重算——但别每个字符打一次请求。 */
let timer: ReturnType<typeof setTimeout> | null = null

async function refreshPlan(): Promise<void> {
  if (!props.open || !props.pid || !props.targetId) return
  planning.value = true
  try {
    plan.value = await imagesApi.plan(props.pid, {
      target_kind: props.targetKind,
      target_id: props.targetId,
      prompt: text.value,
      skill: skill.value || null,
    })
    error.value = null
  } catch (err) {
    plan.value = null
    error.value = err instanceof ApiError ? err : null
  } finally {
    planning.value = false
  }
}

function schedulePlan(): void {
  if (timer) clearTimeout(timer)
  timer = setTimeout(refreshPlan, 300)
}

async function reset(): Promise<void> {
  text.value = ''
  skill.value = ''
  plan.value = null
  queued.value = null
  error.value = null
  if (!listing.value) {
    try {
      listing.value = await imagesApi.skills(props.pid)
    } catch (err) {
      error.value = err instanceof ApiError ? err : null
    }
  }
  await refreshPlan()
}

watch(
  () => [props.open, props.targetId] as const,
  ([open]) => {
    if (open) void reset()
  },
  { immediate: true },
)

async function generate(): Promise<void> {
  submitting.value = true
  try {
    const job = await imagesApi.generate(props.pid, {
      target_kind: props.targetKind,
      target_id: props.targetId,
      prompt: text.value,
      skill: skill.value || null,
    })
    queued.value = job
    error.value = null
    emit('queued', job)
  } catch (err) {
    error.value = err instanceof ApiError ? err : null
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <AppDialog
    :open="open"
    title="生成参考图"
    :subtitle="what || plan?.target_label || ''"
    size="lg"
    @update:open="emit('update:open', $event)"
  >
    <div class="space-y-3 p-3">
      <ErrorPanel v-if="error" :error="error" @dismiss="error = null" />

      <!-- 入队之后只剩一句回执：图要等队列跑完才落进来，这里不假装已经有图了 -->
      <div v-if="queued" class="border-accent/40 bg-accent-dim/20 border px-2 py-1.5 text-2xs">
        <p class="text-accent-hi">已加入生成队列</p>
        <p class="text-fg-2 mt-0.5">
          {{ queued.target_label }} · 任务 {{ queued.id }}。跑完之后
          {{ plan?.lands ?? '会自动挂到这个素材上' }}
        </p>
        <p class="text-fg-4 mt-0.5">进度在底部控制台的任务框里（Ctrl + ` 开合）。</p>
      </div>

      <label class="block">
        <span class="text-fg-3 text-2xs">照哪份提示词结构</span>
        <select
          v-model="skill"
          class="border-line-1 bg-base-2 text-fg-1 mt-0.5 h-row w-full border px-1.5 text-xs outline-none"
          @change="refreshPlan()"
        >
          <option value="">按素材类型自动选</option>
          <option v-for="s in listing?.items ?? []" :key="s.name" :value="s.name">
            {{ s.title }}
          </option>
        </select>
      </label>

      <!-- 「系统会固定补哪几句」必须先摆出来，用户才知道自己那段话只需要写什么 -->
      <section v-if="current" class="border-line-1 bg-base-2 border px-2 py-1.5">
        <p class="text-fg-3 text-2xs">{{ current.title }} · {{ current.when }}</p>
        <p class="text-fg-2 mt-1 text-2xs">系统固定补齐：{{ current.fixed }}</p>
        <p v-if="current.note" class="text-fg-4 mt-0.5 text-2xs">{{ current.note }}</p>
      </section>

      <label class="block">
        <span class="text-fg-3 text-2xs">{{ current?.lead || '写这个素材长什么样' }}</span>
        <textarea
          v-model="text"
          rows="3"
          :placeholder="current?.lead || '二十出头，褪色军绿夹克，短发'"
          class="border-line-1 bg-base-2 text-fg-1 mt-0.5 w-full border px-1.5 py-1 text-xs outline-none"
          @input="schedulePlan()"
        />
        <span v-if="listing?.rule" class="text-fg-4 mt-0.5 block text-2xs">
          {{ listing.rule }}
        </span>
      </label>

      <!-- 账单：先账单再动手，界面上也照这个顺序 -->
      <section v-if="plan" class="border-line-1 border">
        <p
          class="border-line-1 text-fg-3 flex items-center gap-1.5 border-b px-2 py-1 text-2xs tracking-wide uppercase"
        >
          账单（只读，还没有生成任何东西）
          <span v-if="planning" class="text-fg-4 normal-case">重算中…</span>
        </p>
        <dl class="divide-line-1 divide-y text-2xs">
          <div class="flex gap-2 px-2 py-1">
            <dt class="text-fg-4 w-20 shrink-0">出图服务</dt>
            <dd class="text-fg-2 min-w-0 flex-1">
              {{ plan.provider.label }}
              <span v-if="plan.provider.model" class="text-fg-4">· {{ plan.provider.model }}</span>
              <span v-if="plan.provider.preset" class="text-fg-4">
                · 预设 {{ plan.provider.preset }}
              </span>
              <span class="text-fg-4">· {{ plan.provider.size }}</span>
            </dd>
          </div>
          <div class="flex gap-2 px-2 py-1">
            <dt class="text-fg-4 w-20 shrink-0">正向提示词</dt>
            <dd class="text-fg-2 min-w-0 flex-1 break-words whitespace-pre-wrap">
              {{ plan.prompt || '（还没写「长什么样」，所以只有结构那几句）' }}
            </dd>
          </div>
          <div class="flex gap-2 px-2 py-1">
            <dt class="text-fg-4 w-20 shrink-0">负向提示词</dt>
            <dd class="text-fg-2 min-w-0 flex-1 break-words">{{ plan.negative_prompt }}</dd>
          </div>
          <div class="flex gap-2 px-2 py-1">
            <dt class="text-fg-4 w-20 shrink-0">图落到哪</dt>
            <dd class="text-fg-2 min-w-0 flex-1">{{ plan.lands }}</dd>
          </div>
        </dl>
        <ul v-if="plan.warnings.length" class="border-line-1 border-t px-2 py-1">
          <li v-for="w in plan.warnings" :key="w" class="text-st-review text-2xs">· {{ w }}</li>
        </ul>
      </section>

      <!-- 缺服务 / 缺预设：原样显示四要素，去设置页的路就在 suggestions 里 -->
      <div v-if="blocked" class="border-st-failed/40 bg-st-failed/5 border px-2 py-1.5 text-2xs">
        <p class="text-st-failed">{{ blocked.title }}</p>
        <p class="text-fg-2 mt-0.5 break-words">{{ blocked.detail }}</p>
        <ul class="text-fg-3 mt-0.5 space-y-px">
          <li v-for="s in blocked.suggestions" :key="s">· {{ s }}</li>
        </ul>
        <p class="text-fg-4 mt-1 font-mono">{{ blocked.code }}</p>
      </div>
    </div>

    <template #footer>
      <span class="text-fg-4 flex-1 text-2xs">
        {{
          plan?.can_generate
            ? '点「生成」才会入队，队列里能取消。'
            : '现在还生成不了——上面那条说明写清了缺什么。'
        }}
      </span>
      <AppButton variant="ghost" @click="emit('update:open', false)">
        {{ queued ? '关闭' : '取消' }}
      </AppButton>
      <AppButton variant="primary" :disabled="!canGenerate" @click="generate()">
        <Sparkles :size="11" />{{ submitting ? '入队中…' : '生成' }}
      </AppButton>
    </template>
  </AppDialog>
</template>
