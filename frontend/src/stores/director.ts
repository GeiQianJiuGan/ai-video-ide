/**
 * AI 导演协作栏 store（剧本页与幕流程图页共用这一份）。
 *
 * 与 stores/flow.ts 同构：pid 由页面传入、`busy` + `lastError`、动作后重拉。
 *
 * 六个刻意的取舍：
 *   1. **待审提案能活过刷新**——它不存在前端内存里，而是从历史里最后一条 `proposal`
 *      记录恢复出来的；后面若已经有 `applied` 记录，说明这一批审完了，不再恢复。
 *      审到一半刷新页面还得从头聊一遍，那一半功夫就白费了。
 *   2. **丢弃就是把 op 改成 'reject'**——照 story 的老规矩。本地直接把它从待审列表里拿掉，
 *      不发请求：没落库的东西不需要「取消落库」。
 *   3. **一条失败不影响其余**——`apply` 回来的 `failed` 留在 `lastApply` 里显示（含
 *      suggestions），成功的那几条正常消失。
 *   4. **LLM 没配置不是错误**——`llm.configured === false` 时页面显示去设置页的引导；
 *      这一栏关掉，流程图手动编排照旧能走完全程。
 *   5. **说话走流式**（`send()` → `directorApi.chatStream`）：文字进 `live`、工具调用进
 *      `trace`、提案一条条进 `pending`——**提案产出即可审**，不用等这一轮说完。
 *      流里的 `error` 已经被接口层抛成 `ApiError`，所以这里对「开流前失败」与
 *      「半路挂了」只有一套处理。
 *   6. **点「停」= abort，不是失败**：已经收到的照旧有效，但那一轮**没落成记录**
 *      （后端是在收尾时才写的），所以 `unsaved` 置真，界面必须说出「刷新会丢」——
 *      静默丢掉用户看过的东西是最糟的一种。
 *   7. **附件只是输入法**（`attach()`）：一份 Word 剧本 / Excel 分镜表抽成文字**塞进
 *      `draft`**，没落库、没落盘、没出网，也不要求配好 LLM。抽出来什么用户先看得见、
 *      改得动，按下发送才跟着那句话一起走；`attached` 留着「按 gb18030 读的」
 *      「太长截断了」这些话，界面必须显示出来。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  directorApi,
  type DirectorApply,
  type DirectorAttachment,
  type DirectorHistory,
  type DirectorOp,
  type DirectorScope,
  type DirectorToolStep,
  type DirectorTurn,
} from '@/shared/api/director'

/** 从一条 proposal 记录里把 ops 取出来。坏数据退回空数组，不抛。 */
function opsOf(turn: DirectorTurn): DirectorOp[] {
  const raw = (turn.content as { ops?: unknown }).ops
  return Array.isArray(raw) ? (raw as DirectorOp[]) : []
}

/** 这一轮 AI 动过哪些工具。`running` 是「开始了还没回来」。 */
export interface ToolTrace {
  name: string
  running: boolean
  ok: boolean
  /** 失败时的一句话标题。 */
  error: string
}

export const useDirectorStore = defineStore('director', () => {
  const history = ref<DirectorHistory | null>(null)
  /** 待审的提案。空数组 = 没有要审的东西。 */
  const pending = ref<DirectorOp[]>([])
  /** 最近一次落库的结果，含失败的每一条与四要素错误。 */
  const lastApply = ref<DirectorApply | null>(null)
  /** true = 这一轮走的是不支持工具调用的退化路径（提案形状一样，只是提示一句）。 */
  const degraded = ref(false)

  /** 正在写的那段话（流式）。收尾后清空——那时候它已经落成一条 assistant 记录了。 */
  const live = ref('')
  /** 这一轮的工具足迹。下一轮开始时清掉。 */
  const trace = ref<ToolTrace[]>([])
  /** true = 正在流。用来画光标与「停」按钮。 */
  const streaming = ref(false)
  /** true = 手上这几条提案还没落成记录（用户中途点了「停」），刷新会丢。 */
  const unsaved = ref(false)

  /**
   * 这一轮已经抽进输入框的附件。**它不是「待发送的文件」**——文字早就在 `draft` 里了，
   * 这几条留着的是「按什么读的 / 有没有截断」这些必须显示出来的话。
   */
  const attached = ref<DirectorAttachment[]>([])
  /** true = 正在抽某一份附件。与 `busy` 分开：抽文字不该把整栏锁住。 */
  const attaching = ref(false)

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  /** 当前那条流的 abort 句柄。刻意不放进 ref：它不参与渲染。 */
  let running: AbortController | null = null

  const turns = computed<DirectorTurn[]>(() => history.value?.turns ?? [])
  /** 只把人与 AI 说的话画成气泡；提案那几条走右边的 Diff 列表。 */
  const messages = computed(() =>
    turns.value.filter((t) => t.role === 'user' || t.role === 'assistant'),
  )
  const llm = computed(() => history.value?.llm ?? null)
  const configured = computed(() => Boolean(history.value?.llm.configured))
  const note = computed(() => history.value?.note ?? '')
  const hasPending = computed(() => pending.value.length > 0)
  /**
   * 附件能收什么。**后端那一份是唯一口径**（`core/doctext.py::KINDS`）——
   * 界面上的 `accept` 与「最大 N MB」都读它，前端不写死第二张后缀清单。
   * 历史还没拉回来时是 null，此时那颗按钮 disabled。
   */
  const attachInfo = computed(() => history.value?.attach ?? null)

  function clearError(): void {
    lastError.value = null
  }

  async function guarded<T>(run: () => Promise<T>): Promise<T> {
    busy.value = true
    try {
      const out = await run()
      lastError.value = null
      return out
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
      throw err
    } finally {
      busy.value = false
    }
  }

  /** 从历史里恢复待审提案：最后一条 proposal 之后没有 applied，才算还没审完。 */
  function restorePending(): void {
    const rows = turns.value
    const last = [...rows].reverse().find((t) => t.role === 'proposal' || t.role === 'applied')
    pending.value = last && last.role === 'proposal' ? opsOf(last) : []
  }

  async function load(pid: string): Promise<void> {
    await guarded(async () => {
      history.value = await directorApi.history(pid)
      restorePending()
      unsaved.value = false
    })
  }

  /** 一次工具调用的开始 / 结束落到足迹上。`done` 认最近那条同名的未完成项。 */
  function noteTool(step: DirectorToolStep): void {
    if (step.phase === 'start') {
      trace.value = [...trace.value, { name: step.name, running: true, ok: true, error: '' }]
      return
    }
    const rows = [...trace.value]
    for (let i = rows.length - 1; i >= 0; i -= 1) {
      const row = rows[i]
      if (row && row.name === step.name && row.running) {
        rows[i] = {
          name: step.name,
          running: false,
          ok: step.ok !== false,
          error: step.error ?? '',
        }
        break
      }
    }
    trace.value = rows
  }

  /**
   * 说一句话，一边收一边显示。**不落业务库**——回来的只是提案。
   *
   * `scope` 只透传给后端拼系统提示词（剧本页 / 流程图页），不落库：两页共用同一个会话。
   * 「转了太多轮」「半路断线」都是 `ApiError`，但**提案照旧保留**：后端在报错之前
   * 已经把它们落成记录了，用户还能接着审。
   */
  async function send(
    pid: string,
    message: string,
    scope: DirectorScope = 'flow',
  ): Promise<boolean> {
    if (streaming.value) return false
    const ctl = new AbortController()
    running = ctl
    streaming.value = true
    busy.value = true
    lastError.value = null
    lastApply.value = null
    degraded.value = false
    unsaved.value = false
    live.value = ''
    trace.value = []
    pending.value = []
    // 那段文字已经跟着这句话走了，输入框空了——附件那几条提示也就过期了。
    attached.value = []
    let ok = false
    try {
      for await (const event of directorApi.chatStream(pid, message, scope, ctl.signal)) {
        if (event.event === 'delta') live.value += event.data.text
        else if (event.event === 'tool') noteTool(event.data)
        else if (event.event === 'op') pending.value = [...pending.value, event.data]
        else if (event.event === 'done') {
          degraded.value = event.data.degraded
          ok = true
        }
      }
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
    } finally {
      streaming.value = false
      busy.value = false
      running = null
    }
    if (ctl.signal.aborted) {
      // 停在半路：这一轮没落成记录。已经收到的提案与文字留着，但必须说清刷新会丢。
      unsaved.value = pending.value.length > 0 || live.value.length > 0
      return false
    }
    // 正常收尾与半路报错，后端都已经落好记录了——重拉历史，把 live 交给那条 assistant 气泡。
    history.value = await directorApi.history(pid).catch(() => history.value)
    live.value = ''
    return ok
  }

  /** 点「停」。abort 不算失败：已经收到的照旧有效（但没落成记录，见 `unsaved`）。 */
  function stop(): void {
    running?.abort()
  }

  /**
   * 一份附件 → 一段纯文本，交给调用方塞进输入框。**一行库都不动，也不出网。**
   *
   * 抽不了（.pdf / .doc / 太大 / 整份都是图）时回 null，原因连 suggestions 一起进
   * `lastError`——和这一栏其它失败同一个显示位置。**刻意不走 `guarded()`**：
   * 抽文字不该把「停」和已经在手上的提案一起锁住。
   */
  async function attach(pid: string, file: File): Promise<DirectorAttachment | null> {
    attaching.value = true
    try {
      const out = await directorApi.attach(pid, file)
      lastError.value = null
      attached.value = [...attached.value, out]
      return out
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
      return null
    } finally {
      attaching.value = false
    }
  }

  /** 把某一条附件的提示从列表里去掉。**不动 `draft`**：那段文字是用户自己的了。 */
  function forgetAttachment(filename: string): void {
    const at = attached.value.findIndex((row) => row.filename === filename)
    if (at >= 0) attached.value = attached.value.filter((_, i) => i !== at)
  }

  /** 丢弃一条：本地把 op 改成 'reject' 并移出待审列表，不发请求。 */
  function discard(tempId: string): void {
    pending.value = pending.value.filter((op) => op.temp_id !== tempId)
  }

  function discardAll(): void {
    pending.value = []
  }

  async function apply(pid: string, ops: DirectorOp[]): Promise<DirectorApply | null> {
    if (!ops.length) return null
    try {
      const out = await guarded(() => directorApi.apply(pid, ops))
      lastApply.value = out
      const failed = new Set(out.failed.map((f) => f.temp_id ?? ''))
      const asked = new Set(ops.map((op) => op.temp_id))
      // 成功的走掉、失败的留下继续显示错误；没提交的那几条不动。
      pending.value = pending.value.filter((op) => !asked.has(op.temp_id) || failed.has(op.temp_id))
      if (!pending.value.length) unsaved.value = false
      history.value = await directorApi.history(pid).catch(() => history.value)
      return out
    } catch {
      return null
    }
  }

  /** 采用一条。 */
  async function accept(pid: string, tempId: string): Promise<DirectorApply | null> {
    const op = pending.value.find((o) => o.temp_id === tempId)
    return op ? apply(pid, [op]) : null
  }

  /** 全部采用。已经被丢弃的不在 `pending` 里，所以这里天然只落用户认可的。 */
  async function acceptAll(pid: string): Promise<DirectorApply | null> {
    return apply(pid, [...pending.value])
  }

  /** 清空协作记录。已经落库的改动不受影响——那是库里的数据，不是聊天记录。 */
  async function clear(pid: string): Promise<void> {
    await guarded(async () => {
      await directorApi.clear(pid)
      history.value = await directorApi.history(pid)
      pending.value = []
      lastApply.value = null
      live.value = ''
      trace.value = []
      unsaved.value = false
      attached.value = []
    })
  }

  return {
    history,
    turns,
    messages,
    llm,
    configured,
    note,
    pending,
    hasPending,
    lastApply,
    degraded,
    live,
    trace,
    streaming,
    unsaved,
    attachInfo,
    attached,
    attaching,
    busy,
    lastError,
    load,
    send,
    stop,
    attach,
    forgetAttachment,
    discard,
    discardAll,
    apply,
    accept,
    acceptAll,
    clear,
    clearError,
  }
})
