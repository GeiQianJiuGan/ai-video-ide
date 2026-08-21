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

import { api } from './client'

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
}

export interface DirectorOp {
  /** 写工具名；丢弃时被改成 'reject'。 */
  op: string
  target: 'scene' | 'link' | string
  temp_id: string
  scene_id?: string
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

export interface DirectorHistory {
  turns: DirectorTurn[]
  llm: { configured: boolean; provider: string; model: string | null; hint: string }
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

export const directorApi = {
  history: (pid: string) => api.get<DirectorHistory>(`/projects/${pid}/director`),
  chat: (pid: string, message: string) =>
    api.post<DirectorChat>(`/projects/${pid}/director/chat`, { message }),
  apply: (pid: string, ops: DirectorOp[]) =>
    api.post<DirectorApply>(`/projects/${pid}/director/apply`, { ops }),
  clear: (pid: string) => api.del<void>(`/projects/${pid}/director`),
}
