/**
 * 项目容器接口（M1）。
 *
 * 字段与后端 app/api/projects.py 的 ProjectOut / RecentOut 一一对应，
 * 不在前端另起别名——后端是唯一真源，改字段时两边一起报错比悄悄错更好。
 */

import { api, type ErrorPayload } from './client'
import type { PresetRow } from './settings'
import type { GenerationMode } from './workflows'

export type DurationUnit = 'frames' | 'seconds'

export interface Project {
  id: string
  name: string
  dir: string
  width: number
  height: number
  fps: number
  aspect_ratio: string
  duration_unit: DurationUnit
  schema_version: number
  /** 打开旧工程时被自动升级的来源版本；没升级就是 null。 */
  migrated_from: number | null
  created_at: string
  updated_at: string
}

export interface ProjectPreset {
  name: string | null
  preset: PresetRow | null
  r2v_name: string | null
  r2v_preset: PresetRow | null
  flf_name: string | null
  flf_preset: PresetRow | null
}

export interface RecentProject {
  id: string
  name: string
  dir: string
  schema_version: number
  opened_at: string
  /** 目录是否还在（被移动或删除时为 false，条目不隐藏，让人能主动忘记它）。 */
  exists: boolean
  is_open: boolean
}

export interface CreateProjectInput {
  dir: string
  name: string
  width: number
  height: number
  fps: number
  duration_unit: DurationUnit
}

/**
 * 这个答案是谁给的：`project` 工程显式选了这条 / `settings` 跟随设置页 /
 * `default` 谁都没选过（用的是后端代码里那个默认值）。
 */
export type RouteSource = 'project' | 'settings' | 'default'

/**
 * 「这个答案是谁给的」摆给界面看。照 `settings.ts::SOURCE_LABEL` 的作风收在一处：
 * 概览页、Workflow 管理页那条横幅、二次处理弹窗三处都要标这句话，各写一遍必然分叉。
 *
 * `settings` 与 `default` 对用户的意义是一样的（**都是设置页说的**），但分开说得清
 * 「你在设置页改过」和「谁都没选过，用的是代码里那个默认值」——排查时这两者方向不同。
 */
export const ROUTE_SOURCE_LABEL: Record<RouteSource, string> = {
  project: '工程指定',
  settings: '跟随设置页',
  default: '默认值',
}

/**
 * 这条路要绑的是什么。**界面照它分岔，不照调用方式的名字**（硬约束 1）：
 * `preset` 两份预设 / `base_url` 一个地址 / `workflow` 四个能力各一份图。
 * 未知调用方式是空串。
 */
export type RouteBinds = 'preset' | 'base_url' | 'workflow' | ''

/**
 * 一次能喂几个参考素材。**`null` = 不限制，`0` 是有意义的答案**
 * （绑的那份图一个参考图槽位都没标），所以两者不能都画成「—」。
 */
export interface RouteSlots {
  /** 这个数字是哪份图 / 哪条合同给的。 */
  source: string
  detail: string
  image: number | null
  video: number | null
  audio: number | null
}

/** 一条能力在这条路上的解析结果。同一个工程的两条能力可以给出两个不同答案。 */
export interface RouteCapability {
  capability: string
  /** 中文名由后端给（`route.CAPABILITY_LABEL`），前端不写第二份。 */
  capability_label: string
  provider: string
  label: string
  source: RouteSource
  binds: RouteBinds
  binds_workflow: boolean
  /** 预设那条路才有值，且已经按继承顺序解析到具体那一份。 */
  preset: string | null
  workflow_id: string | null
  workflow_name: string | null
  /** REST 那条路才有值；**永不带密钥**。 */
  base_url: string | null
  ready: boolean
  /** 缺什么。四要素形状，suggestions 原样显示（硬约束 4）。 */
  issues: ErrorPayload[]
  slots: RouteSlots
}

/**
 * 调用方式下拉的一项。第一项是「跟随设置页」（`name: ''`、`inherit: true`）。
 *
 * `binds` 是**这一条选中之后要绑什么**，于是界面能说出「四个能力下拉要改成哪一条才生效」
 * 而不必在前端写死 `comfy_workflow` 这个名字（硬约束 1）。
 */
export interface RouteOption {
  name: string
  label: string
  inherit: boolean
  binds: RouteBinds
  legacy?: boolean
}

/**
 * 冻结进任务 / 版本参数里的那条路（`job.params.route`，后端 `Route.frozen()`）。
 *
 * **只有事实，没有当时的 readiness**：`ready` / `issues` 说的是解析那一刻缺什么，冻进去
 * 只会让半年后翻参数的人把它当成这次任务的失败原因。地址进档（排查时第一个要看的
 * 东西），**密钥永不进档**。
 */
export interface FrozenRoute {
  provider: string
  label: string
  source: RouteSource
  capability: string
  workflow_id: string | null
  workflow_name: string | null
  preset: string | null
  base_url: string | null
}

/**
 * 从冻结参数里把那条路读出来。**队列页与版本轨共用这一份**，两处各解一遍必然分叉。
 *
 * 老任务（这次改造之前入队的那些）里没有这一项，所以回 `null` 而不是替它编一条
 * 「ComfyUI 预设」——谎报走了哪条路正是这次要修的 bug 的形状。
 */
export function frozenRoute(params: Record<string, unknown> | null | undefined): FrozenRoute | null {
  const raw = params?.route
  if (!raw || typeof raw !== 'object') return null
  const row = raw as Partial<FrozenRoute>
  return typeof row.provider === 'string' && typeof row.label === 'string'
    ? (row as FrozenRoute)
    : null
}

/**
 * `GET /projects/{pid}/route`：**「这个工程怎么出片」一个请求画完。**
 *
 * `mode` 是工程那一列的原样值，**空串 = 跟随设置页**而不是「没配置」——绝大多数工程是
 * 这一种，所以 `provider` / `source` 要一起读：前者是最终走哪条路，后者说清是谁给的答案。
 * `options` 与两条能力的中文名都由后端给，前端一个调用方式的名字都不写死。
 */
export interface ProjectRoute {
  mode: GenerationMode
  provider: string
  label: string
  source: RouteSource
  binds: RouteBinds
  binds_workflow: boolean
  options: RouteOption[]
  /** 「留空会走哪条」：`mode` 清空后就是它。 */
  settings_provider: string
  capabilities: RouteCapability[]
  /** REST 那条路上服务端要实现什么。写死在前端的话，改合同就得改两处。 */
  contract: string[]
}

export const projectsApi = {
  create: (input: CreateProjectInput) => api.post<Project>('/projects', input),
  open: (dir: string) => api.post<Project>('/projects/open', { dir }),
  recent: () => api.get<RecentProject[]>('/projects/recent'),
  forget: (dir: string) => api.post<void>('/projects/recent/forget', { dir }),
  get: (pid: string) => api.get<Project>(`/projects/${pid}`),
  close: (pid: string) => api.post<void>(`/projects/${pid}/close`),
  preset: (pid: string) => api.get<ProjectPreset>(`/projects/${pid}/preset`),
  setPreset: (pid: string, name: string | null) =>
    api.put<ProjectPreset>(`/projects/${pid}/preset`, { name }),
  setVideoPresets: (pid: string, r2v_name: string | null, flf_name: string | null) =>
    api.put<ProjectPreset>(`/projects/${pid}/preset`, { r2v_name, flf_name }),
  /**
   * 只改一个角色那一份预设。**后端认的是键在不在**（`"r2v_name" in payload`），所以另一个角色
   * 不发就一个字都不会动。
   *
   * 两个都发是有害的：工程没指定 FL2VA 预设时，界面上显示的是**设置页那一份**（继承来的），
   * 原样回发就把它写进了工程列——用户只改了 R2V，另一份却从「跟随设置页」变成「工程指定」，
   * 此后设置页再改也带不动它了。
   */
  setVideoPreset: (pid: string, role: 'r2v' | 'flf', name: string | null) =>
    api.put<ProjectPreset>(`/projects/${pid}/preset`, { [`${role}_name`]: name }),
  /** 走哪条路、要绑什么、绑没绑上、缺什么。**只读，绝不抛**（缺什么在 `issues` 里）。 */
  route: (pid: string) => api.get<ProjectRoute>(`/projects/${pid}/route`),
}
