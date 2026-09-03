<script setup lang="ts">
/**
 * AI 导演协作栏。**全应用只有这一个实例、一份会话**（同一份 `DirectorTurn`）——所以它放在
 * `features/director/` 而不是某一个 feature 下面，而挂它的地方是常驻外壳里的
 * `app/layout/DirectorDock.vue`（右侧停靠栏，换页不卸载）。
 *
 * 以前它内嵌在剧本页与幕流程图页上，一离开那两页就卸载，正在写的那段话与手上那几条待审提案
 * 跟着一起消失；现在那两页只负责把这一栏叫出来（`shell.showDirector()`）。
 *
 * 这一栏的全部意义是**把「加一幕雨夜追车」变成一份可逐条审阅的 Diff**。十个刻意的设计：
 *
 *   1. **提案不是改动**。这里列出来的每一条，数据库里都还没有发生。只有按下「采用」
 *      才落库，所以每条都要给出 `before → after`——不是「AI 说它要改点东西」。
 *   2. **提案产出即可审**。流式那条路把 `op` 夹在过程里给（见 `stores/director.ts`），
 *      所以第一条提案出来时就能看，不用等这一轮说完；一轮拆解常常是 1 幕 + 8 镜，
 *      所以按对象分组，每组能一起采用。
 *   3. **正在写的那段话是一条临时气泡**（`live`）。它还没落库，所以不进 `messages`——
 *      历史只有一份真源；收尾时被落库的那条 assistant 记录顶掉。
 *   4. **工具足迹要看得见**。「它现在在查什么」是这一栏最容易变成黑盒的地方。失败的那一步
 *      标红**但不代表这一轮废了**：后端把错误回喂给模型，它常常自己换个做法再来。
 *   5. **丢弃是本地动作**。没落库的东西不需要「取消落库」：直接从待审列表里拿掉
 *      （照 story 的老规矩，等价于把 op 改成 reject）。
 *   6. **失败长在自己那张卡上**。`apply` 回来的 `failed` 按 temp_id 贴回对应的提案卡，
 *      而不是在底下另开一个列表——一条失败不影响其余几条已经落进去的。
 *   7. **LLM 没配置不是红叉**。这一栏改成一条去设置页的引导——手动加幕、改衔接、
 *      编排生成全都不依赖 LLM，这一栏关掉整条链路照旧能走完。
 *   8. **`scope` 只是一句提示**。它透传给后端拼系统提示词（用户现在在哪一页），
 *      不落库、不分会话——换页不该让历史对话变味。
 *   9. **附件是输入法，不是暗地里带上的东西**。一份 Word 剧本 / Excel 分镜表抽成文字后
 *      **原样塞进输入框**（前后各一行界标），用户看得见、删得掉、改得动；「按 gb18030
 *      读的」「太长截断了」这些话贴在输入框上方。绝不做「文件跟着请求偷偷走一遍」——
 *      那样用户永远不知道模型到底读到了什么。
 *  10. **落库回执里那几句话必须显示出来**。落成了的那张提案卡会走掉，于是「同一批新建的
 *      角色接上了没有」「参考图排上没排上」「哪个名字对不上」只剩这里能说了。只给一行
 *      「已落库 N 条」等于把降级藏起来（硬约束 4）——少接一个人不该让用户等到成片才发现。
 *
 * 宽度由**调用方**给（停靠栏是拖出来的像素宽度，见 `DirectorDock.vue`）。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  Check,
  ChevronDown,
  ChevronRight,
  Eraser,
  PanelRightClose,
  Paperclip,
  Send,
  Settings,
  Sparkles,
  Square,
  Wrench,
  X,
} from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import {
  OP_FIELD_LABEL,
  OP_LABEL,
  type DirectorApplyFail,
  type DirectorAttachment,
  type DirectorOp,
  type DirectorScope,
  type DirectorTurn,
} from '@/shared/api/director'
import { useDirectorStore } from '@/stores/director'
const props = withDefaults(
  defineProps<{
    pid: string
    /** 用户现在开着哪一页。只影响后端拼的系统提示词。 */
    scope?: DirectorScope
    title?: string
    placeholder?: string
    emptyBody?: string
    /** 一排 chip，点一下把这句话填进输入框（不直接发送——发出去之前用户还能改）。 */
    quickActions?: string[]
    /** 停靠栏模式：多画一个「收起」。内嵌进页面时不需要。 */
    closable?: boolean
  }>(),
  {
    scope: 'flow',
    title: 'AI 导演',
    placeholder: '要它做什么？Enter 发送，Shift+Enter 换行',
    emptyBody:
      '例如「在第 2 幕后面加一幕雨夜追车」「把第 1 幕的出场角色改成只有阿岚」。它会先看清现状，再提一份提案——按下采用之前，库里什么都不会变。',
    quickActions: () => [],
    closable: false,
  },
)
/** 落库成功后通知外面重拉——幕数、镜头数、衔接都可能变了。 */
const emit = defineEmits<{ applied: []; close: [] }>()

const router = useRouter()
const director = useDirectorStore()
const draft = ref('')
/** 只看变化的字段。整幕覆盖那种提案 before/after 大半是同值，全列出来反而看不清。 */
const onlyChanged = ref(true)
/** 展开了哪几处长文本，键是 `${temp_id}:${字段}`。 */
const opened = ref<Set<string>>(new Set())
/** 对话区的滚动容器：流式时要跟着往下走。 */
const feed = ref<HTMLElement | null>(null)
/** 输入框本体。抽完附件要把光标放到最后，否则用户得在两万字里自己找地方打字。 */
const composer = ref<HTMLTextAreaElement | null>(null)
/** 藏起来的那个 `<input type=file>`。`accept` 只认后端给的那一份。 */
const filePicker = ref<HTMLInputElement | null>(null)

/** 提案改的是什么对象。分组标题用它。 */
const TARGET_LABEL: Record<string, string> = {
  scene: '幕',
  shot: '镜头',
  link: '幕衔接',
  shot_link: '镜头衔接',
}

/** 一处值超过这么多字就先折起来。镜头 prompt 动辄几百字，全铺开就看不见别的了。 */
const LONG = 90
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
const HIDDEN_KEYS = new Set([
  'from_scene_id',
  'to_scene_id',
  'from_shot_id',
  'to_shot_id',
  'scene_id',
  'location_variant_id',
  'order',
])

function rowsOf(op: DirectorOp): DiffRow[] {
  const before = op.before ?? {}
  const after = op.after ?? {}
  const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])].filter(
    (k) => !HIDDEN_KEYS.has(k),
  )
  return keys.map((key) => {
    const from = show((before as Record<string, unknown>)[key])
    const to = op.after === null ? '（删掉）' : show((after as Record<string, unknown>)[key])
    return { key, label: OP_FIELD_LABEL[key] ?? key, from, to, changed: from !== to }
  })
}
/** 照「只看变化的」过滤。**一条都不剩时退回全部**——空白比几行同值更像坏了。 */
function visibleRows(op: DirectorOp): DiffRow[] {
  const rows = rowsOf(op)
  if (!onlyChanged.value) return rows
  const changed = rows.filter((r) => r.changed)
  return changed.length ? changed : rows
}

/** 这条提案被折起来了几行（给「展开全部字段」那个开关做提示）。 */
function hiddenCount(op: DirectorOp): number {
  return onlyChanged.value ? rowsOf(op).length - visibleRows(op).length : 0
}

function opLabel(op: DirectorOp): string {
  return OP_LABEL[op.op] ?? op.op
}

/** 一条对话记录说了什么。后端两种角色都写在 `content.text` 里。 */
function textOf(turn: DirectorTurn): string {
  return String((turn.content as { text?: unknown }).text ?? '')
}

function keyOf(op: DirectorOp, row: DiffRow): string {
  return `${op.temp_id}:${row.key}`
}

function isLong(text: string): boolean {
  return text.length > LONG
}

function clip(text: string): string {
  return `${text.slice(0, LONG)}…`
}

function isOpen(key: string): boolean {
  return opened.value.has(key)
}

function toggle(key: string): void {
  const next = new Set(opened.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  opened.value = next
}

/** 提案按对象分组。一轮拆解常常是「1 幕 + 8 镜」，混成一长条谁也审不动。 */
const groups = computed(() => {
  const out: { target: string; label: string; ops: DirectorOp[] }[] = []
  for (const op of director.pending) {
    const target = String(op.target || 'scene')
    const hit = out.find((g) => g.target === target)
    if (hit) hit.ops.push(op)
    else out.push({ target, label: TARGET_LABEL[target] ?? target, ops: [op] })
  }
  return out
})
/** 落库失败按 temp_id 贴回对应那张卡。 */
const failByTemp = computed(() => {
  const map = new Map<string, DirectorApplyFail>()
  for (const f of director.lastApply?.failed ?? []) if (f.temp_id) map.set(f.temp_id, f)
  return map
})
/** 认不回哪张卡的失败（没带 temp_id）还是要显示——绝不静默丢掉。 */
const orphanFails = computed(() => (director.lastApply?.failed ?? []).filter((f) => !f.temp_id))

/**
 * 落库回执里**有话要说**的那几个键。`scene_id` / `shot_id` 那些给人看没有意义，
 * 而这一族是后端唯一会交代「顺带发生了什么」的地方：
 * 同一批新建的角色 / 道具 / 地点接上了没有，以及参考图为什么没排上。
 *
 * 键名照后端（`services/director.py::_wire_pending` 与 `_maybe_image`），前端不猜、不改写
 * 那几句话——它们本来就是给用户看的整句中文，改写只会变成第二份口径。
 */
const APPLIED_NOTES: { key: string; tone: 'done' | 'warn' }[] = [
  { key: 'cast_wired', tone: 'done' },
  { key: 'props_wired', tone: 'done' },
  { key: 'location_wired', tone: 'done' },
  { key: 'cast_skipped', tone: 'warn' },
  { key: 'props_skipped', tone: 'warn' },
  { key: 'location_skipped', tone: 'warn' },
  { key: 'image_skipped', tone: 'warn' },
]

interface AppliedRow {
  key: string
  label: string
  headline: string
  notes: { text: string; tone: 'done' | 'warn' }[]
}

/**
 * 这一次落库里有话要说的那几条。**没话说的不列**——一轮拆解常常是「1 幕 + 8 镜」，
 * 全列出来只会把真正该看的那两句埋掉，条数本身由下面那行「已落库 N 条」交代。
 */
const appliedRows = computed<AppliedRow[]>(() => {
  const out: AppliedRow[] = []
  ;(director.lastApply?.applied ?? []).forEach((row, i) => {
    const label = String(row.target_label ?? '')
    const headline = String(row.title ?? row.name ?? '') || label
    const notes: { text: string; tone: 'done' | 'warn' }[] = []
    if (row.job_id) {
      // 出图对象那句话在标题上已经有了就不重复一遍（`generate_reference` 那一族）。
      const who = label && label !== headline ? `：${label}` : ''
      notes.push({ text: `参考图已排进队列${who}。进度在底部控制台的任务框里看。`, tone: 'done' })
    }
    for (const spec of APPLIED_NOTES) {
      const text = String(row[spec.key] ?? '')
      if (text) notes.push({ text, tone: spec.tone })
    }
    if (!notes.length) return
    const op = String(row.op ?? '')
    out.push({
      key: `${String(row.temp_id ?? '')}-${i}`,
      label: OP_LABEL[op] ?? op,
      headline,
      notes,
    })
  })
  return out
})

async function reload(): Promise<void> {
  if (!props.pid) return
  await director.load(props.pid).catch(() => {})
}

onMounted(reload)
watch(() => props.pid, reload)

/**
 * 流式时跟着往下滚。**只在本来就贴着底部时才滚**——用户往上翻着看前面几轮时,
 * 每来一个字就把他拽回底部是最烦人的那种「贴心」。
 */
function follow(): void {
  const el = feed.value
  if (!el) return
  const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  if (!near) return
  nextTick(() => {
    if (feed.value) feed.value.scrollTop = feed.value.scrollHeight
  })
}

watch(() => director.live, follow)
watch(() => director.trace.length, follow)
watch(() => director.pending.length, follow)
watch(() => director.messages.length, follow)

async function send(): Promise<void> {
  const text = draft.value.trim()
  if (!text) return
  draft.value = ''
  await director.send(props.pid, text, props.scope)
}

function pickFile(): void {
  filePicker.value?.click()
}

/**
 * 抽出来的文字**塞进输入框**，前后各一行界标。
 *
 * 界标是给两边看的：模型得分清哪一段是文档、哪一句是用户自己说的话；用户得看见这一整段
 * 会跟着发出去（所以它能删、能改、能只留要用的那几段）。
 */
function spliceIn(att: DirectorAttachment): void {
  const block = `【附件 ${att.filename} · ${att.kind_label}】\n${att.text}\n【附件结束】`
  const had = draft.value.replace(/\s+$/, '')
  draft.value = had ? `${had}\n\n${block}\n` : `${block}\n`
  // 光标落到最后：接着打字就是「照这份文档做什么」，不用在两万字里找位置。
  nextTick(() => {
    const el = composer.value
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
    el.scrollTop = el.scrollHeight
  })
}

/**
 * 选了几份就一份份抽。**一份抽不了不影响其余几份**——原因（连 suggestions）显示在
 * 输入框上方那块错误里，抽成了的照旧进输入框。
 *
 * 收尾把 `value` 清空：不清的话同一个文件第二次选不出 change 事件。
 */
async function onPicked(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const files = [...(input.files ?? [])]
  input.value = ''
  for (const file of files) {
    const out = await director.attach(props.pid, file)
    if (out) spliceIn(out)
  }
}

async function accept(op: DirectorOp): Promise<void> {
  const out = await director.accept(props.pid, op.temp_id)
  if (out && out.count) emit('applied')
}
async function acceptAll(): Promise<void> {
  const out = await director.acceptAll(props.pid)
  if (out && out.count) emit('applied')
}

/** 一组一起采用（「这一幕的 8 个镜头都要」）。失败的照旧留在原位显示错误。 */
async function acceptGroup(ops: DirectorOp[]): Promise<void> {
  const out = await director.apply(props.pid, [...ops])
  if (out && out.count) emit('applied')
}

function discardGroup(ops: DirectorOp[]): void {
  for (const op of ops) director.discard(op.temp_id)
}
</script>

<template>
  <AppPanel :title="title" :scroll="false">
    <template #actions>
      <AppBadge
        v-if="director.degraded"
        tone="warn"
        title="这个模型端不支持工具调用，走的是一次性产出提案的退化路径；提案形状完全一样"
      >
        退化模式
      </AppBadge>
      <AppBadge
        v-if="director.unsaved"
        tone="warn"
        title="这一轮被中途停下，下面这几条还没落成记录——刷新页面会丢，要留就先采用"
      >
        未存记录
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
      <!--
        收起不是清空：会话、待审提案、正在流的那一轮都活在 store 里，
        再打开还是刚才那一栏（`streaming` 甚至不会中断）。
      -->
      <AppButton
        v-if="closable"
        size="sm"
        variant="ghost"
        title="收起这一栏。对话与待审提案都留着，随时从标题栏再叫出来"
        @click="emit('close')"
      >
        <PanelRightClose :size="10" />
      </AppButton>
    </template>

    <div class="flex h-full min-h-0 flex-col">
      <!-- 没配 LLM：一条引导，不是红叉。手动编排本来就能走完全程 -->
      <div v-if="director.history && !director.configured" class="min-h-0 flex-1 overflow-auto p-2">
        <EmptyState
          title="还没有配置 LLM"
          :body="
            director.llm?.hint ||
            '去设置页填一个模型地址，这一栏就能帮你加幕、挂角色、定衔接。不配也行——手动加幕、改衔接、编排生成全都不依赖它。'
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
        <div ref="feed" class="min-h-0 flex-1 space-y-1.5 overflow-auto p-2">
          <EmptyState
            v-if="!director.messages.length && !director.live && !director.streaming"
            title="说一句话，它先看清现状再提一份提案"
            :body="emptyBody"
          />

          <!-- 落了库的对话。提案不在这里，走下面那份可逐条审阅的 Diff -->
          <div
            v-for="turn in director.messages"
            :key="turn.id"
            class="text-2xs"
            :class="turn.role === 'user' ? 'pl-6' : ''"
          >
            <p class="text-fg-4 mb-0.5 flex items-center gap-1">
              <Sparkles v-if="turn.role === 'assistant'" :size="10" class="text-accent" />
              {{ turn.role === 'user' ? '我' : 'AI 导演' }}
            </p>
            <p
              class="border-line-1 border px-2 py-1.5 leading-relaxed whitespace-pre-wrap"
              :class="turn.role === 'user' ? 'bg-base-2 text-fg-2' : 'bg-base-1 text-fg-1'"
            >
              {{ textOf(turn) }}
            </p>
          </div>

          <!--
            正在写的那段：**一条临时气泡，还没落库**，所以不进 `messages`——
            历史只有一份真源，收尾时它被落库的那条 assistant 记录顶掉。
          -->
          <div v-if="director.live || director.streaming" class="text-2xs">
            <p class="text-fg-4 mb-0.5 flex items-center gap-1">
              <Sparkles :size="10" class="text-accent animate-pulse" />
              AI 导演 · 正在写
            </p>
            <p
              class="border-accent/30 bg-base-1 text-fg-1 border px-2 py-1.5 leading-relaxed whitespace-pre-wrap"
            >
              {{ director.live
              }}<span class="bg-accent ml-px inline-block h-3 w-1.5 animate-pulse align-middle" />
            </p>
          </div>
          <!--
            工具足迹：「它现在在查什么」是这一栏最容易变成黑盒的地方。
            失败的那一步标红**但不代表这一轮废了**——后端把错误回喂给模型，它常常自己换个做法再来。
          -->
          <div v-if="director.trace.length" class="border-line-1 bg-base-2 border px-2 py-1.5">
            <p class="text-fg-4 mb-1 text-2xs">这一轮它动过的工具</p>
            <ul class="space-y-px">
              <li
                v-for="(t, i) in director.trace"
                :key="`${t.name}-${i}`"
                class="text-2xs flex items-start gap-1"
              >
                <Wrench
                  :size="10"
                  class="mt-px shrink-0"
                  :class="
                    t.running ? 'text-accent animate-pulse' : t.ok ? 'text-fg-4' : 'text-st-failed'
                  "
                />
                <span class="font-mono" :class="t.ok ? 'text-fg-3' : 'text-st-failed'">
                  {{ t.name }}
                </span>
                <span v-if="t.running" class="text-fg-4">…</span>
                <span v-else-if="!t.ok" class="text-st-failed min-w-0 break-words">
                  {{ t.error || '这一步没成' }}
                </span>
              </li>
            </ul>
            <p
              v-if="director.trace.some((t) => !t.running && !t.ok)"
              class="text-fg-4 mt-1 text-2xs"
            >
              有一步没成不代表这一轮废了——它会拿到错误再换个做法。
            </p>
          </div>
          <!--
            提案。**这里列出来的每一条，库里都还没有发生**——按下「采用」才落库。
            一轮拆解常常是「1 幕 + 8 镜」，所以按对象分组，每组能一起采用。
          -->
          <div v-for="g in groups" :key="g.target" class="space-y-1">
            <div class="flex items-center gap-1.5">
              <AppBadge tone="accent">{{ g.label }} · {{ g.ops.length }} 条</AppBadge>
              <template v-if="g.ops.length > 1">
                <AppButton
                  size="sm"
                  variant="primary"
                  :disabled="director.busy"
                  title="这一组一起落库。哪条失败了就留在原位显示原因，不影响其余几条"
                  @click="acceptGroup(g.ops)"
                >
                  <Check :size="10" />采用本组
                </AppButton>
                <AppButton
                  size="sm"
                  variant="ghost"
                  title="从待审列表里拿掉这一组。没落库的东西不需要「取消落库」"
                  @click="discardGroup(g.ops)"
                >
                  <X :size="10" />丢弃本组
                </AppButton>
              </template>
            </div>
            <div
              v-for="op in g.ops"
              :key="op.temp_id"
              class="border-line-1 bg-base-2 border"
              :class="failByTemp.get(op.temp_id) ? 'border-st-failed/40' : ''"
            >
              <div class="border-line-1 flex items-center gap-1.5 border-b px-2 py-1">
                <span class="text-fg-1 min-w-0 flex-1 truncate text-2xs">{{ opLabel(op) }}</span>
                <AppButton
                  size="sm"
                  variant="primary"
                  :disabled="director.busy"
                  title="落库这一条"
                  @click="accept(op)"
                >
                  <Check :size="10" />采用
                </AppButton>
                <AppButton
                  size="sm"
                  variant="ghost"
                  title="不要这一条。库里本来就没有它，所以只是从待审列表里拿掉"
                  @click="director.discard(op.temp_id)"
                >
                  <X :size="10" />
                </AppButton>
              </div>
              <div class="space-y-1 px-2 py-1.5">
                <p v-if="op.why" class="text-fg-3 text-2xs leading-relaxed">{{ op.why }}</p>
                <!-- before → after。**不是「它要改点东西」，而是改成什么样** -->
                <div
                  v-for="row in visibleRows(op)"
                  :key="row.key"
                  class="text-2xs flex items-baseline gap-1"
                >
                  <span class="text-fg-4 w-14 shrink-0 truncate" :title="row.label">
                    {{ row.label }}
                  </span>
                  <div class="min-w-0 flex-1">
                    <p v-if="op.before" class="text-fg-4 break-words">
                      {{ isLong(row.from) && !isOpen(keyOf(op, row)) ? clip(row.from) : row.from }}
                    </p>
                    <p class="break-words" :class="row.changed ? 'text-accent-hi' : 'text-fg-3'">
                      <span v-if="op.before" class="text-fg-4">→ </span
                      >{{ isLong(row.to) && !isOpen(keyOf(op, row)) ? clip(row.to) : row.to }}
                    </p>
                    <button
                      v-if="isLong(row.from) || isLong(row.to)"
                      class="text-fg-4 hover:text-fg-2 mt-px inline-flex items-center gap-0.5"
                      @click="toggle(keyOf(op, row))"
                    >
                      <component
                        :is="isOpen(keyOf(op, row)) ? ChevronDown : ChevronRight"
                        :size="10"
                      />
                      {{ isOpen(keyOf(op, row)) ? '收起' : '展开全文' }}
                    </button>
                  </div>
                </div>
                <p v-if="hiddenCount(op)" class="text-fg-4 text-2xs">
                  还有 {{ hiddenCount(op) }} 个字段没变；要全看关掉下面的「只看变化的」。
                </p>
                <!-- 「能落，但有点不对」——绝不静默丢掉 -->
                <ul v-if="op.warnings.length" class="space-y-px">
                  <li v-for="w in op.warnings" :key="w" class="text-st-review text-2xs break-words">
                    · {{ w }}
                  </li>
                </ul>

                <!-- 落库失败长在自己那张卡上，一条失败不影响其余几条已经落进去的 -->
                <div
                  v-if="failByTemp.get(op.temp_id)"
                  class="border-st-failed/40 bg-st-failed/5 border px-1.5 py-1"
                >
                  <p class="text-st-failed text-2xs">
                    {{ failByTemp.get(op.temp_id)?.error.title }}
                  </p>
                  <p class="text-fg-2 text-2xs mt-0.5 break-words">
                    {{ failByTemp.get(op.temp_id)?.error.detail }}
                  </p>
                  <ul class="text-fg-3 mt-0.5 space-y-px">
                    <li
                      v-for="s in failByTemp.get(op.temp_id)?.error.suggestions ?? []"
                      :key="s"
                      class="text-2xs"
                    >
                      · {{ s }}
                    </li>
                  </ul>
                  <p class="text-fg-4 mt-0.5 font-mono text-2xs">
                    {{ failByTemp.get(op.temp_id)?.error.code }}
                  </p>
                </div>
              </div>
            </div>
          </div>

          <!-- 认不回哪张卡的失败（没带 temp_id）也要显示——绝不静默丢掉 -->
          <div
            v-for="(f, i) in orphanFails"
            :key="`orphan-${i}`"
            class="border-st-failed/40 bg-st-failed/5 border px-2 py-1.5"
          >
            <p class="text-st-failed text-2xs">{{ f.error.title }}</p>
            <p class="text-fg-2 text-2xs mt-0.5 break-words">{{ f.error.detail }}</p>
            <ul class="text-fg-3 mt-0.5 space-y-px">
              <li v-for="s in f.error.suggestions" :key="s" class="text-2xs">· {{ s }}</li>
            </ul>
            <p class="text-fg-4 mt-0.5 font-mono text-2xs">{{ f.op }} · {{ f.error.code }}</p>
          </div>

          <!--
            这一次落了什么。落库是不可见的，不说一句就等于没发生——而**落成了的那张提案卡
            已经走掉了**，所以「同一批新建的角色接上了没有」「图排上没排上」只剩这里能说。
          -->
          <div v-if="director.lastApply && director.lastApply.count" class="space-y-1">
            <p class="text-st-done text-2xs">
              已落库 {{ director.lastApply.count }} 条{{
                director.lastApply.failed.length
                  ? `，另有 ${director.lastApply.failed.length} 条没落成（原因贴在对应那张卡上）`
                  : ''
              }}。
            </p>
            <div
              v-for="row in appliedRows"
              :key="row.key"
              class="border-line-1 bg-base-2 border px-2 py-1"
            >
              <p class="text-fg-3 text-2xs">
                {{ row.label
                }}<span v-if="row.headline" class="text-fg-2"> · {{ row.headline }}</span>
              </p>
              <ul class="mt-0.5 space-y-px">
                <li
                  v-for="n in row.notes"
                  :key="n.text"
                  class="text-2xs break-words"
                  :class="n.tone === 'done' ? 'text-st-done' : 'text-st-review'"
                >
                  · {{ n.text }}
                </li>
              </ul>
            </div>
          </div>
        </div>

        <div class="border-line-1 shrink-0 space-y-1.5 border-t p-2">
          <!-- 四要素错误照原样显示。开流前失败与半路挂了走的是同一条 -->
          <ErrorPanel :error="director.lastError" @dismiss="director.clearError()" />

          <!-- 停在半路 = 这一轮没落成记录。必须说出来，不能让用户刷新之后发现东西没了 -->
          <p v-if="director.unsaved" class="text-st-review text-2xs">
            这一轮被停在半路，上面这些还没落成记录——刷新页面会丢。要留就先采用。
          </p>

          <!-- chip 只把话填进输入框，不直接发送：发出去之前用户还能改 -->
          <div v-if="quickActions.length && !director.streaming" class="flex flex-wrap gap-1">
            <button
              v-for="q in quickActions"
              :key="q"
              class="border-line-1 bg-base-2 text-fg-3 hover:text-fg-1 hover:bg-base-3 rounded-sm border px-1.5 py-px text-2xs"
              @click="draft = q"
            >
              {{ q }}
            </button>
          </div>
          <!--
            抽进输入框的附件。**这几条不是「待上传的文件」**——文字已经在输入框里了，
            这里留着的是「按什么读的 / 有没有截断」这些必须显示出来的话。
          -->
          <div v-if="director.attached.length" class="space-y-1">
            <div
              v-for="att in director.attached"
              :key="att.filename"
              class="border-line-1 bg-base-2 border px-1.5 py-1"
            >
              <div class="flex items-center gap-1">
                <Paperclip :size="10" class="text-fg-4 shrink-0" />
                <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs" :title="att.filename">
                  {{ att.filename }}
                </span>
                <span class="text-fg-4 shrink-0 text-2xs">
                  {{ att.kind_label }} · {{ att.chars }} 字
                </span>
                <AppBadge v-if="att.truncated" tone="warn">已截断</AppBadge>
                <button
                  class="text-fg-4 hover:text-fg-2 shrink-0"
                  title="只把这条提示收起来。文字已经在输入框里，要去掉请在输入框里删那一段"
                  @click="director.forgetAttachment(att.filename)"
                >
                  <X :size="10" />
                </button>
              </div>
              <!-- 「按 gb18030 读的」「日期是原始值」这类话必须显示，否则用户不知道读到了什么 -->
              <ul v-if="att.notes.length" class="mt-0.5 space-y-px">
                <li v-for="n in att.notes" :key="n" class="text-fg-4 text-2xs break-words">
                  · {{ n }}
                </li>
              </ul>
            </div>
          </div>
          <textarea
            ref="composer"
            v-model="draft"
            rows="3"
            :placeholder="placeholder"
            :disabled="director.streaming"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 w-full resize-none border px-1.5 py-1 text-2xs leading-relaxed outline-none focus:border-accent/50 disabled:opacity-50"
            @keydown.enter.exact.prevent="send()"
          />

          <!-- 停靠栏能被拖窄，所以这一排允许换行——挤成一行会把「发送」推出去 -->
          <div class="flex flex-wrap items-center gap-1.5">
            <!--
              附件：抽成文字填进输入框，**不落库、不落盘、不出网**。
              `accept` 只认后端那一份（`core/doctext.py::KINDS`），前端不写死后缀清单。
            -->
            <input
              ref="filePicker"
              type="file"
              multiple
              class="hidden"
              :accept="director.attachInfo?.accept"
              @change="onPicked($event)"
            />
            <AppButton
              size="sm"
              variant="ghost"
              :disabled="!director.attachInfo || director.attaching || director.streaming"
              :title="
                director.attachInfo
                  ? `一份 Word / Excel / PPT / 文本 → 一段文字填进输入框（最大 ${director.attachInfo.max_mb} MB，最多 ${director.attachInfo.max_chars} 字）。${director.attachInfo.note}`
                  : '还在读这一栏的配置'
              "
              @click="pickFile()"
            >
              <Paperclip :size="10" />{{ director.attaching ? '抽文字…' : '附件' }}
            </AppButton>
            <label
              class="text-fg-4 flex items-center gap-1 text-2xs"
              title="整幕覆盖那种提案 before/after 大半是同值，全列出来反而看不清"
            >
              <input v-model="onlyChanged" type="checkbox" class="accent-accent h-3 w-3" />
              只看变化的
            </label>
            <AppButton
              v-if="director.hasPending"
              size="sm"
              variant="ghost"
              title="全部丢弃。库里本来就没有它们，所以只是清空待审列表"
              @click="director.discardAll()"
            >
              <X :size="10" />全丢
            </AppButton>
            <AppButton
              v-if="director.hasPending"
              size="sm"
              variant="primary"
              :disabled="director.busy"
              title="把待审的每一条都落库。失败的留在原位显示原因"
              @click="acceptAll()"
            >
              <Check :size="10" />全部采用
            </AppButton>
            <div class="ml-auto flex items-center gap-1.5">
              <AppButton
                v-if="director.streaming"
                size="sm"
                title="停下这一轮。已经收到的照旧有效，但这一轮不会落成记录"
                @click="director.stop()"
              >
                <Square :size="10" />停
              </AppButton>
              <AppButton
                v-else
                size="sm"
                variant="primary"
                :disabled="!draft.trim() || director.busy"
                @click="send()"
              >
                <Send :size="10" />发送
              </AppButton>
            </div>
          </div>
          <!-- 用的是哪个端。整段返回的端不会有 delta，那不是卡住了 -->
          <p v-if="director.llm" class="text-fg-4 text-2xs">
            {{ director.llm.label }}
            <span v-if="director.llm.model" class="font-mono">· {{ director.llm.model }}</span>
            <span v-if="!director.llm.supports_stream"> · 这个端整段返回，出字之前会静一会儿 </span>
            <span v-else-if="!director.llm.supports_tools">
              · 这个端不支持工具调用，走一次性产出提案的退化路径
            </span>
          </p>
          <p v-if="director.note" class="text-fg-4 text-2xs">{{ director.note }}</p>
        </div>
      </template>
    </div>
  </AppPanel>
</template>
