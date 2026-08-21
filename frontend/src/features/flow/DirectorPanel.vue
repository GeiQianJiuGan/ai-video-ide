<script setup lang="ts">
/**
 * AI 协作栏（幕流程图右栏）。
 *
 * 这一栏的全部意义是**把「加一幕雨夜追车」变成一份可逐条审阅的 Diff**。四个刻意的设计：
 *
 *   1. **提案不是改动**。这里列出来的每一条，数据库里都还没有发生。只有按下「采用」
 *      才落库，所以每条都要给出 `before → after`——不是「AI 说它要改点东西」。
 *   2. **丢弃是本地动作**。没落库的东西不需要「取消落库」：直接从待审列表里拿掉
 *      （照 story 的老规矩，等价于把 op 改成 reject）。
 *   3. **LLM 没配置不是红叉**。这一栏改成一条去设置页的引导——流程图上手动加幕、
 *      改衔接、编排生成全都不依赖 LLM，这一栏关掉整条链路照旧能走完。
 *   4. **警告与失败都摆出来**。`warnings` 是「能落但有点不对」（角色名对不上），
 *      `failed` 里每条带四要素错误，一条失败不影响其余几条已经落进去的。
 */
import { onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Check, Eraser, Send, Settings, Sparkles, X } from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import { OP_LABEL, type DirectorOp } from '@/shared/api/director'
import { useDirectorStore } from '@/stores/director'

const props = defineProps<{ pid: string }>()
/** 落库成功后通知外面重拉流程图——幕数、镜头数、衔接都可能变了。 */
const emit = defineEmits<{ applied: [] }>()

const router = useRouter()
const director = useDirectorStore()
const draft = ref('')

/** 字段名在界面上叫什么。对不上的直接显示原名，不猜。 */
const FIELD_LABEL: Record<string, string> = {
  title: '标题',
  summary: '概要',
  time_of_day: '时间',
  location_variant_id: '地点变体',
  prompt: '画面描述',
  cast: '出场角色',
  props: '道具',
  shots: '镜头',
  mode: '衔接方式',
  duration: '时长',
  order: '顺序',
  titles: '顺序（标题）',
  shot_count: '镜头数',
  from_title: '从',
  to_title: '到',
  from_scene_id: '上一幕',
  to_scene_id: '下一幕',
}

/** 把任意值压成一行能看的字。角色 / 道具那种数组显示 label，不显示 id。 */
function show(value: unknown): string {
  if (value === null || value === undefined || value === '') return '（空）'
  if (Array.isArray(value)) {
    if (!value.length) return '（空）'
    return value
      .map((item) =>
        item && typeof item === 'object'
          ? String(
              (item as Record<string, unknown>).label ??
                (item as Record<string, unknown>).title ??
                JSON.stringify(item),
            )
          : String(item),
      )
      .join('、')
  }
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

interface DiffRow {
  key: string
  label: string
  from: string
  to: string
  changed: boolean
}

/** before / after 并成几行。id 那种给人看没意义的字段不进 Diff。 */
const HIDDEN_KEYS = new Set(['from_scene_id', 'to_scene_id', 'location_variant_id', 'order'])

function rowsOf(op: DirectorOp): DiffRow[] {
  const before = op.before ?? {}
  const after = op.after ?? {}
  const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])].filter(
    (k) => !HIDDEN_KEYS.has(k),
  )
  return keys.map((key) => {
    const from = show((before as Record<string, unknown>)[key])
    const to = op.after === null ? '（删掉）' : show((after as Record<string, unknown>)[key])
    return { key, label: FIELD_LABEL[key] ?? key, from, to, changed: from !== to }
  })
}

function opLabel(op: DirectorOp): string {
  return OP_LABEL[op.op] ?? op.op
}

async function reload(): Promise<void> {
  if (!props.pid) return
  await director.load(props.pid).catch(() => {})
}

onMounted(reload)
watch(() => props.pid, reload)

async function send(): Promise<void> {
  const text = draft.value.trim()
  if (!text) return
  draft.value = ''
  await director.send(props.pid, text)
}

async function accept(op: DirectorOp): Promise<void> {
  const out = await director.accept(props.pid, op.temp_id)
  if (out && out.count) emit('applied')
}

async function acceptAll(): Promise<void> {
  const out = await director.acceptAll(props.pid)
  if (out && out.count) emit('applied')
}
</script>

<template>
  <AppPanel title="AI 协作" class="w-80 shrink-0">
    <template #actions>
      <AppBadge
        v-if="director.degraded"
        tone="warn"
        title="这个模型端不支持工具调用，走的是一次性产出提案的退化路径；提案形状完全一样"
      >
        退化模式
      </AppBadge>
      <AppButton
        v-if="director.turns.length"
        size="sm"
        variant="ghost"
        title="清空对话与提案记录。已经落库的改动不受影响——那是库里的数据，不是聊天记录"
        @click="director.clear(pid).catch(() => {})"
      >
        <Eraser :size="10" />清空
      </AppButton>
    </template>

    <div class="flex min-h-0 flex-1 flex-col">
      <!-- 没配 LLM：一条引导，不是红叉。手动编排本来就能走完全程 -->
      <div v-if="director.history && !director.configured" class="min-h-0 flex-1 overflow-auto p-2">
        <EmptyState
          title="还没有配置 LLM"
          :body="
            director.llm?.hint ||
            '去设置页填一个模型地址，这一栏就能帮你加幕、挂角色、定衔接。不配也行——左边手动加幕、改衔接、编排生成全都不依赖它。'
          "
        />
        <AppButton
          size="sm"
          variant="primary"
          class="mt-2"
          @click="router.push({ name: 'settings' })"
        >
          <Settings :size="10" />去设置页
        </AppButton>
      </div>

      <template v-else>
        <ErrorPanel
          v-if="director.lastError"
          class="m-2"
          :error="director.lastError"
          @dismiss="director.clearError()"
        />

        <!-- 对话 -->
        <div class="min-h-0 flex-1 overflow-auto p-2">
          <EmptyState
            v-if="!director.messages.length"
            title="跟它说一句话"
            body="例如「在第 2 幕后面加一幕雨夜追车」「把第 1 幕的出场角色改成只有阿岚」。它会先看清现状，再提一份提案——按下采用之前，库里什么都不会变。"
          />
          <ul v-else class="space-y-1.5">
            <li
              v-for="turn in director.messages"
              :key="turn.id"
              class="border px-1.5 py-1"
              :class="
                turn.role === 'user'
                  ? 'border-line-1 bg-base-2'
                  : 'border-accent/40 bg-accent-dim/20'
              "
            >
              <p class="text-fg-4 text-2xs">{{ turn.role === 'user' ? '你' : 'AI 导演' }}</p>
              <p class="text-fg-1 text-2xs leading-relaxed whitespace-pre-wrap">
                {{ String(turn.content.text ?? '') }}
              </p>
            </li>
          </ul>

          <!-- 提案：逐条 Diff -->
          <template v-if="director.hasPending">
            <div class="mt-2 flex items-center gap-1">
              <span class="text-fg-3 text-2xs tracking-wide uppercase">
                提案（{{ director.pending.length }}）
              </span>
              <AppButton
                size="sm"
                variant="ghost"
                class="ml-auto"
                title="全部丢弃。没落库的东西不需要「取消落库」"
                @click="director.discardAll()"
              >
                <X :size="10" />全部丢弃
              </AppButton>
              <AppButton
                size="sm"
                variant="primary"
                :disabled="director.busy"
                title="把还留着的这几条一起落库"
                @click="acceptAll()"
              >
                <Check :size="10" />全部采用
              </AppButton>
            </div>
            <p class="text-fg-4 mt-1 text-2xs">{{ director.note }}</p>
            <section
              v-for="op in director.pending"
              :key="op.temp_id"
              class="border-accent/40 bg-base-2 mt-1.5 border"
            >
              <header class="border-line-1 flex items-center gap-1 border-b px-1.5 py-1">
                <Sparkles :size="10" class="text-accent shrink-0" />
                <span class="text-fg-1 min-w-0 flex-1 truncate text-2xs">{{ opLabel(op) }}</span>
                <AppButton
                  size="sm"
                  variant="ghost"
                  title="丢弃这一条"
                  @click="director.discard(op.temp_id)"
                >
                  <X :size="10" />
                </AppButton>
                <AppButton
                  size="sm"
                  variant="primary"
                  :disabled="director.busy"
                  title="只落这一条"
                  @click="accept(op)"
                >
                  <Check :size="10" />采用
                </AppButton>
              </header>
              <p v-if="op.why" class="text-fg-3 px-1.5 py-1 text-2xs">{{ op.why }}</p>
              <ul class="divide-line-1 divide-y">
                <li
                  v-for="row in rowsOf(op)"
                  :key="row.key"
                  class="px-1.5 py-0.5 text-2xs"
                  :class="row.changed ? '' : 'opacity-50'"
                >
                  <span class="text-fg-4">{{ row.label }}</span>
                  <div class="flex items-start gap-1">
                    <span v-if="op.before" class="text-fg-4 min-w-0 flex-1 line-through">
                      {{ row.from }}
                    </span>
                    <span class="text-fg-1 min-w-0 flex-1">{{ row.to }}</span>
                  </div>
                </li>
              </ul>
              <ul v-if="op.warnings.length" class="text-st-review px-1.5 py-1 space-y-px text-2xs">
                <li v-for="w in op.warnings" :key="w">· {{ w }}</li>
              </ul>
            </section>
          </template>

          <!-- 落库结果：成功的走掉了，失败的连四要素一起摆出来 -->
          <template v-if="director.lastApply">
            <p class="text-fg-3 mt-2 text-2xs tracking-wide uppercase">这次落了什么</p>
            <p class="text-fg-2 mt-1 text-2xs">
              落库 {{ director.lastApply.count }} 条{{
                director.lastApply.failed.length
                  ? ` · 失败 ${director.lastApply.failed.length} 条`
                  : ''
              }}
            </p>
            <ul v-if="director.lastApply.failed.length" class="mt-1 space-y-px">
              <li
                v-for="(f, i) in director.lastApply.failed"
                :key="`${f.temp_id ?? f.op}-${i}`"
                class="border-st-failed/40 bg-base-2 border px-1 py-0.5"
              >
                <p class="text-fg-1 text-2xs">{{ OP_LABEL[f.op] ?? f.op }}：{{ f.error.title }}</p>
                <p class="text-fg-2 text-2xs">{{ f.error.detail }}</p>
                <ul class="text-fg-4 space-y-px text-2xs">
                  <li v-for="s in f.error.suggestions" :key="s">· {{ s }}</li>
                </ul>
              </li>
            </ul>
          </template>
        </div>

        <!-- 输入 -->
        <div class="border-line-1 shrink-0 border-t p-2">
          <textarea
            v-model="draft"
            rows="2"
            placeholder="要它做什么？Enter 发送，Shift+Enter 换行"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 w-full resize-none border p-1.5 text-2xs leading-relaxed outline-none"
            @keydown.enter.exact.prevent="send()"
          />
          <div class="mt-1 flex items-center gap-1">
            <span class="text-fg-4 min-w-0 flex-1 truncate text-2xs">
              {{ director.llm?.model ? `${director.llm.provider} · ${director.llm.model}` : '' }}
            </span>
            <AppButton
              size="sm"
              variant="primary"
              :disabled="director.busy || !draft.trim()"
              @click="send()"
            >
              <Send :size="10" />{{ director.busy ? '想…' : '发送' }}
            </AppButton>
          </div>
        </div>
      </template>
    </div>
  </AppPanel>
</template>
