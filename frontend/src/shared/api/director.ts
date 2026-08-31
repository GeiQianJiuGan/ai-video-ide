/**
 * AI 导演接口（幕流程图右栏的协作栏）。
 *
 * 字段与后端 app/api/director.py + services/director.py 一一对应。
 *
 * 三个形状上的要点：
 *   1. **提案不是改动**——`chat()` 回来的 `ops` 只是提案，数据库一行没变；
 *      只有 `apply()` 才落库，而且只落 `op !== 'reject'` 的条目。
 *   2. **`before` / `after` 成对给**，前端才能画出真正的 Diff：`before` 是库里现在的样子
 *      （新增时是 null），`after` 是提案要改成什么（删除时是 null）。
 *   3. **`llm.configured === false` 不是错误**——协作栏据此显示去设置页的引导，
 *      而不是一个红叉。手动编排本来就能走完全程。
 */

import { ApiError, api, type ErrorPayload } from './client'

/** 用户现在开着哪一页。只影响后端拼系统提示词的那一句，不落库。 */
export type DirectorScope = 'script' | 'flow'

/**
 * 提案里的字段名在界面上叫什么。**放在这里而不是组件里**：剧本页与流程图页共用同一个
 * 协作栏组件，这份口径也得只有一处。对不上的直接显示原名，不猜。
 */
export const OP_FIELD_LABEL: Record<string, string> = {
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
  // 镜头级
  description: '描述', // 镜头是「镜头描述」，素材是「设定」，两族共用一张表，取通用那个词
  camera: '景别',
  movement: '运镜',
  camera_motion: '机位与运镜',
  visual_prompt: '画面内容',
  audio_dialogue: '声音与对白',
  negative_prompt: '负向提示词',
  skill: '照的 SKILL',
  scene_title: '所属幕',
  index_no: '第几镜',
  position: '插在第几个',
  // 素材级（角色 / 地点 / 道具 + 顺带出一张参考图）
  name: '名字',
  variant: '地点变体',
  image_prompt: '图片提示词',
  target_kind: '素材类型',
  target_label: '出图对象',
  target_id: '素材',
  /** 这一句落在哪一列（形象上是 traits，其余是 description）。 */
  field: '写进哪一列',
  generate_image: '顺带出一张图',
}

/** 写工具名 = 提案的 op。用户丢弃一条时，前端把它改成 'reject'。 */
export const DIRECTOR_OPS = [
  'add_scene',
  'update_scene',
  'set_scene_prompt',
  'set_scene_cast',
  'set_scene_props',
  'set_link',
  'reorder_scenes',
  'delete_scene',
  'add_shot',
  'update_shot',
  'delete_shot',
  'reorder_shots',
  'set_shot_link',
  'add_character',
  'add_location',
  'add_prop',
  'generate_reference',
  'set_description',
] as const
export type DirectorOpName = (typeof DIRECTOR_OPS)[number]

/** 每种提案在界面上叫什么。文案在这里写一遍，别在组件里散着写。 */
export const OP_LABEL: Record<string, string> = {
  add_scene: '加一幕',
  update_scene: '改这一幕',
  set_scene_prompt: '改整幕画面描述',
  set_scene_cast: '改整幕出场角色',
  set_scene_props: '改整幕道具',
  set_link: '改衔接方式',
  reorder_scenes: '重排幕顺序',
  delete_scene: '删掉一幕',
  add_shot: '加一个镜头',
  update_shot: '改这个镜头',
  delete_shot: '删掉一个镜头',
  reorder_shots: '重排镜头顺序',
  set_shot_link: '改镜头之间的衔接',
  add_character: '加一个角色',
  add_location: '加一个地点',
  add_prop: '加一个道具',
  generate_reference: '生成参考图',
  set_description: '补一句描述',
}

export interface DirectorOp {
  /** 写工具名；丢弃时被改成 'reject'。 */
  op: string
  target: 'scene' | 'link' | 'shot' | 'shot_link' | 'material' | string
  temp_id: string
  scene_id?: string
  shot_id?: string
  /** 库里现在的样子；新增时是 null。 */
  before: Record<string, unknown> | null
  /** 提案要改成什么；删除时是 null。 */
  after: Record<string, unknown> | null
  why: string
  /** 「能落，但有点不对」——比如角色名对不上。绝不静默丢掉。 */
  warnings: string[]
}

export interface DirectorTurn {
  id: string
  /** user / assistant / proposal / applied */
  role: string
  content: Record<string, unknown>
  created_at: string
}

export interface DirectorLlm {
  configured: boolean
  provider: string
  label: string
  model: string | null
  /** false = 这个端不支持工具调用，走一次性产出提案的退化路径（提案形状一样）。 */
  supports_tools: boolean
  /** false = 这个端整段返回，不会有 delta；协作栏据此不画那个光标。 */
  supports_stream: boolean
  hint: string
}

export interface DirectorHistory {
  turns: DirectorTurn[]
  llm: DirectorLlm
  note: string
}

export interface DirectorChat {
  turns: DirectorTurn[]
  ops: DirectorOp[]
  /** true = 这个端不支持工具调用，走的是一次性产出提案的退化路径。 */
  degraded: boolean
}

export interface DirectorApplyFail {
  op: string
  temp_id?: string
  error: { code: string; title: string; detail: string; suggestions: string[] }
}

export interface DirectorApply {
  applied: Record<string, unknown>[]
  /** 一条失败不回滚其余几条，失败的连四要素错误一起回来。 */
  failed: DirectorApplyFail[]
  count: number
}

/** 一次工具调用的开始 / 结束。`done` 那条带 `ok`，失败时 `error` 是一句话标题。 */
export interface DirectorToolStep {
  name: string
  phase: 'start' | 'done'
  ok?: boolean
  error?: string
}

/** 流式正常收尾。`turns` 是刚落下的记录（assistant + 有提案时的 proposal）。 */
export interface DirectorDone {
  turns: DirectorTurn[]
  ops: DirectorOp[]
  degraded: boolean
  /** 这一轮和模型往返了几次。转满 `MAX_ROUNDS` 会走 error 那条。 */
  rounds: number
}

/**
 * 流式事件。**`error` 不在这里**——它被 `chatStream()` 抛成 `ApiError`，
 * 于是调用方对「开流前失败」和「半路挂了」只有一套错误处理。
 */
export type DirectorStreamEvent =
  | { event: 'delta'; data: { text: string } }
  | { event: 'tool'; data: DirectorToolStep }
  | { event: 'op'; data: DirectorOp }
  | { event: 'done'; data: DirectorDone }

export const directorApi = {
  history: (pid: string) => api.get<DirectorHistory>(`/projects/${pid}/director`),
  /**
   * 说一句话，拿回一份提案。**一行库都不动。**
   *
   * `scope` 是「用户现在开着哪一页」（`script` 剧本页 / `flow` 幕流程图页）。它只影响这一次
   * 请求拼出来的系统提示词，不落库——两页共用同一个会话，换页不该让历史对话变味。
   *
   * 这是不流式那条（兼容路径）。界面走的是下面的 `chatStream()`。
   */
  chat: (pid: string, message: string, scope: DirectorScope = 'flow') =>
    api.post<DirectorChat>(`/projects/${pid}/director/chat`, { message, scope }),
  /**
   * 同一件事，但一边说一边给。**照旧一行库都不动**（`done` 里的 `turns` 只是聊天记录）。
   *
   * 三条与不流式那条一致的规矩：
   *   1. **`done` 与 `error` 互斥且必有其一**——`error` 在这里表现为抛 `ApiError`，
   *      所以正常收尾一定是最后那条 `done`；
   *   2. **半路挂了也不白干**：抛出来之前收到的 `op` 全部有效，后端已经把它们落成记录，
   *      刷新页面还在；
   *   3. **`signal` 用来「停」**：abort 之后迭代直接结束，不抛——已经收到的照旧有效。
   */
  chatStream: (
    pid: string,
    message: string,
    scope: DirectorScope = 'flow',
    signal?: AbortSignal,
  ): AsyncGenerator<DirectorStreamEvent> => stream(pid, message, scope, signal),
  apply: (pid: string, ops: DirectorOp[]) =>
    api.post<DirectorApply>(`/projects/${pid}/director/apply`, { ops }),
  clear: (pid: string) => api.del<void>(`/projects/${pid}/director`),
}

async function* stream(
  pid: string,
  message: string,
  scope: DirectorScope,
  signal?: AbortSignal,
): AsyncGenerator<DirectorStreamEvent> {
  const path = `/projects/${pid}/director/chat/stream`
  for await (const frame of api.stream(path, { message, scope }, signal)) {
    if (frame.event === 'error') {
      // 状态码是 200——错误是在流里来的。但对调用方来说它和开流前失败没有区别。
      throw new ApiError((frame.data as { error: ErrorPayload }).error, 200)
    }
    yield frame as DirectorStreamEvent
  }
}
