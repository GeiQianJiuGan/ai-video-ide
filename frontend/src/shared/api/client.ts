/**
 * REST 客户端。
 *
 * 硬约束：后端所有失败都返回 { error: { code, title, detail, suggestions, related_ids } }。
 * 这里统一转成 ApiError 抛出，UI 永远能拿到「为什么失败、怎么修」，绝不静默。
 */

import { endpoint } from './endpoint'

export type ErrorCode =
  | 'WORKFLOW_ERROR'
  | 'MISSING_ASSET'
  | 'MISSING_INPUT'
  | 'MISSING_CAPABILITY'
  | 'INVALID_WORKFLOW'
  | 'DEPENDENCY_CYCLE'
  | 'UPSTREAM_NOT_READY'
  | 'CONTEXT_INCOMPLETE'
  | 'COMFY_OFFLINE'
  | 'COMFY_NODE_MISSING'
  | 'COMFY_LOST'
  | 'GPU_OOM'
  | 'FFMPEG_ERROR'
  | 'FFMPEG_MISSING'
  | 'LLM_UNAVAILABLE'
  | 'LLM_INVALID_OUTPUT'
  | 'DISK_FULL'
  | 'SCHEMA_MISMATCH'
  | 'NOT_FOUND'
  | 'CONFLICT'
  | 'VALIDATION_ERROR'
  | 'UNAUTHORIZED'
  | 'INTERNAL'
  | 'NETWORK_ERROR'

export interface ErrorPayload {
  code: ErrorCode
  title: string
  detail: string
  suggestions: string[]
  related_ids: Record<string, string>
}

export class ApiError extends Error {
  readonly code: ErrorCode
  /** 面向用户的一句话标题（与 message 同值，便于模板直读）。 */
  readonly title: string
  readonly detail: string
  readonly suggestions: string[]
  readonly relatedIds: Record<string, string>
  readonly status: number

  constructor(payload: ErrorPayload, status: number) {
    super(payload.title)
    this.name = 'ApiError'
    this.code = payload.code
    this.title = payload.title
    this.detail = payload.detail
    this.suggestions = payload.suggestions ?? []
    this.relatedIds = payload.related_ids ?? {}
    this.status = status
  }
}

/**
 * 「项目未打开」判定。
 *
 * 每工程一个库、没有全局数据库：ProjectService 是进程内注册表，后端重启后进程里
 * 没有任何已打开的工程，此时 /projects/{pid}/... 一律 404。这是设计而不是 bug，
 * 所以 UI 要引导回起始页重开，而不是当成崩溃。
 * 靠 related_ids.project_id 与普通的「找不到某条记录」区分开（见 services/projects.py::get）。
 */
export function isProjectNotOpen(err: unknown): boolean {
  return err instanceof ApiError && err.code === 'NOT_FOUND' && 'project_id' in err.relatedIds
}

function networkError(cause: unknown): ApiError {
  return new ApiError(
    {
      code: 'NETWORK_ERROR',
      title: '无法连接后端服务',
      detail: cause instanceof Error ? cause.message : String(cause),
      suggestions: ['确认后端进程已启动', '重启应用让 Tauri 重新拉起 sidecar'],
      related_ids: {},
    },
    0,
  )
}

/**
 * 应答不符合 `{error: {...}}` 契约时的兜底。
 *
 * 后端自己的三个 handler 会把连未捕获异常都归一成契约形状（`app/main.py`），所以
 * **拿到非契约响应基本等于「答话的不是后端」**——开发期是 Vite 代理在后端没起时
 * 回的 500、生产是 sidecar 挂了。此时照样要给出可执行的下一步，不能只丢一个
 * 「HTTP 500」了事，否则等于静默失败。
 */
function offContractError(status: number, text: string): ApiError {
  return new ApiError(
    {
      code: 'NETWORK_ERROR',
      title: '后端服务没有按契约应答',
      detail: `HTTP ${status}${text ? `：${text.slice(0, 300)}` : '（响应为空）'}`,
      suggestions: [
        '确认后端进程还活着（开发期：cd backend && AIVS_PORT=8765 python -m app.main）',
        '若是桌面版，重启应用让 Tauri 重新拉起 sidecar',
        '后端确实在跑却仍报这个，看后端日志里同一时刻的堆栈',
      ],
      related_ids: {},
    },
    status,
  )
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const form = body instanceof FormData
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (endpoint.token) headers['X-AIVS-Token'] = endpoint.token
  // multipart 的 Content-Type 必须由浏览器带 boundary 生成，写死就传不上去
  if (body !== undefined && !form) headers['Content-Type'] = 'application/json'

  let resp: Response
  try {
    resp = await fetch(`${endpoint.baseUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : form ? (body as FormData) : JSON.stringify(body),
    })
  } catch (cause) {
    throw networkError(cause)
  }

  if (resp.status === 204) return undefined as T

  const text = await resp.text()
  // 代理或反代可能回 HTML；JSON.parse 直接抛会变成没人认领的异常（= 静默失败）
  let parsed: unknown = null
  let jsonOk = true
  try {
    parsed = text ? JSON.parse(text) : null
  } catch {
    jsonOk = false
  }

  if (!resp.ok) {
    const wrapped = jsonOk ? (parsed as { error?: ErrorPayload } | null) : null
    if (!wrapped?.error) throw offContractError(resp.status, text)
    throw new ApiError(wrapped.error, resp.status)
  }
  if (!jsonOk) throw offContractError(resp.status, text)
  return parsed as T
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body ?? {}),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body ?? {}),
  del: <T>(path: string) => request<T>('DELETE', path),
  /**
   * 上传文件（multipart）。不能走 request()——它会把 body 序列化成 JSON，
   * 而且 Content-Type 必须由浏览器自己带 boundary，手写就传不上去。
   * 错误处理与 request() 完全一致，失败同样是 ApiError。
   */
  upload: <T>(path: string, file: File, fields: Record<string, string> = {}) => {
    const form = new FormData()
    form.append('file', file, file.name)
    for (const [k, v] of Object.entries(fields)) if (v) form.append(k, v)
    return request<T>('POST', path, form)
  },
}
