/**
 * Workflow 能力层接口（Step 4）。
 *
 * 这一层是「业务不绑定具体视频模型」那条硬约束的落点：镜头只说要什么能力
 * （`text2image` / `image2video` / `first_last_frame` / `upscale`），
 * 哪套图、哪个节点的哪个字段接 prompt，全在这里的绑定表里。
 *
 * 两个形状上的要点：
 *   1. **校验分两段**——`validate?probe=false` 只做本地绑定检查（离线可用），
 *      `probe=true` 才去 ComfyUI 探测自定义节点。所以 ComfyUI 不在线也能把绑定配对。
 *   2. **校验不通过是结构化错误**——`INVALID_WORKFLOW` / `COMFY_NODE_MISSING`
 *      会带 `problems` / `missing_nodes`，页面照常显示 suggestions，不要吞掉。
 */

import { api } from './client'

/** 四种能力，顺序与后端 `models_gen.CAPABILITIES` 一致。 */
export const CAPABILITIES = ['text2image', 'image2video', 'first_last_frame', 'upscale'] as const
export type Capability = (typeof CAPABILITIES)[number]

export const CAPABILITY_LABEL: Record<Capability, string> = {
  text2image: '文生图',
  image2video: '图生视频',
  first_last_frame: '首尾帧',
  upscale: '超分',
}

/** 可绑定的槽位。哪些是必填由能力决定（`capability_matrix` 里的 `required_slots`）。 */
export const SLOTS = [
  'prompt',
  'negative_prompt',
  'reference_image',
  'first_frame',
  'last_frame',
  'source_image',
  'seed',
  'steps',
  'width',
  'height',
  'duration',
] as const
export type Slot = (typeof SLOTS)[number]

/** 图里的一个节点。`fields` 只含标量字段——连线出来的输入不能被绑定覆盖。 */
export interface WorkflowNode {
  id: string
  class_type: string
  title: string | null
  fields: string[]
}

/** 一次校验的结果。也会被后端写进 `validation_json`，所以列表里就能看到上次结果。 */
export interface ValidationResult {
  ok: boolean
  problems: string[]
  missing_slots: string[]
  required_nodes: string[]
  missing_nodes: string[]
  /** 「已探测 N 个节点」/「已跳过节点探测」/「未能探测（…）」 */
  probe: string
  checked_at: string
}

export interface Workflow {
  id: string
  name: string
  capability: string
  /** draft / ready / invalid / disabled —— 只有 ready 的能被 `resolve` 选中。 */
  status: string
  is_default: number
  notes: string | null
  /** 槽位 → `"节点id.字段名"`。 */
  bindings: Record<string, string>
  reference_image_slots?: string[]
  reference_image_count?: number
  nodes: WorkflowNode[]
  required_nodes: string[]
  validation: ValidationResult | null
  missing_slots: string[]
  created_at: string
  updated_at: string
  /** 只有 `GET /workflows/{wid}` 带原始 API 图，列表里没有。 */
  api_json?: string
}

/** 能力矩阵的一行。`impact` 是「缺了会做不出什么」，没缺时为 null。 */
export interface CapabilityRow {
  capability: string
  ready: boolean
  workflow_count: number
  ready_count: number
  default_workflow_id: string | null
  default_workflow_name: string | null
  required_slots: string[]
  impact: string | null
}

export interface ComfyStatus {
  online: boolean
  base_url: string
  detail: string
}

export interface CapabilityMatrix {
  capabilities: CapabilityRow[]
  comfy: ComfyStatus
  project_bindings?: ProjectWorkflowBindings
}

export type GenerationMode = 'comfy_preset' | 'http_api' | 'workflow_api'
export interface ProjectWorkflowBindings {
  generation_mode: GenerationMode
  text2image: string | null
  image2video: string | null
  first_last_frame: string | null
  upscale: string | null
}

export interface ImportWorkflowBody {
  name: string
  capability: string
  api_json: string
  bindings?: Record<string, string>
  notes?: string | null
}

export const workflowsApi = {
  globalList: () => api.get<Workflow[]>('/workflows'),
  globalGet: (wid: string) => api.get<Workflow>(`/workflows/${wid}`),
  globalImport: (body: ImportWorkflowBody) => api.post<Workflow>('/workflows', body),
  globalBind: (wid: string, bindings: Record<string, string>) =>
    api.put<Workflow>(`/workflows/${wid}/bindings`, { bindings }),
  globalValidate: (wid: string, probe = true) =>
    api.post<ValidationResult>(`/workflows/${wid}/validate?probe=${probe ? 'true' : 'false'}`, {}),
  globalSetDefault: (wid: string) => api.post<Workflow>(`/workflows/${wid}/default`, {}),
  globalRemove: (wid: string) => api.del<void>(`/workflows/${wid}`),
  list: (_pid: string) => api.get<Workflow[]>('/workflows'),
  get: (_pid: string, wid: string) => api.get<Workflow>(`/workflows/${wid}`),
  matrix: (pid: string) =>
    api.get<CapabilityMatrix>(pid ? `/projects/${pid}/capabilities` : '/capabilities'),
  import: (_pid: string, body: ImportWorkflowBody) =>
    api.post<Workflow>('/workflows', body),
  update: (_pid: string, wid: string, patch: { name?: string; notes?: string; status?: string }) =>
    api.patch<Workflow>(`/workflows/${wid}`, patch),
  bind: (_pid: string, wid: string, bindings: Record<string, string>) =>
    api.put<Workflow>(`/workflows/${wid}/bindings`, { bindings }),
  /** `probe=false` 时不碰 ComfyUI，只查绑定；离线也能把工作流配到「就绪」以外的所有问题查清。 */
  validate: (_pid: string, wid: string, probe = true) =>
    api.post<ValidationResult>(
      `/workflows/${wid}/validate?probe=${probe ? 'true' : 'false'}`,
      {},
    ),
  setDefault: (_pid: string, wid: string) =>
    api.post<Workflow>(`/workflows/${wid}/default`, {}),
  remove: (_pid: string, wid: string) => api.del<void>(`/workflows/${wid}`),
  projectBindings: (pid: string) =>
    api.get<ProjectWorkflowBindings>(`/projects/${pid}/workflow-bindings`),
  setProjectBindings: (pid: string, bindings: ProjectWorkflowBindings) =>
    api.put<ProjectWorkflowBindings>(`/projects/${pid}/workflow-bindings`, bindings),
}
