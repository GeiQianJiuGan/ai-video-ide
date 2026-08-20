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
  const parsed: unknown = text ? JSON.parse(text) : null

  if (!resp.ok) {
    const wrapped = parsed as { error?: ErrorPayload } | null
    throw new ApiError(
      wrapped?.error ?? {
        code: 'INTERNAL',
        title: `请求失败（HTTP ${resp.status}）`,
        detail: text.slice(0, 500),
        suggestions: [],
        related_ids: {},
      },
      resp.status,
    )
  }
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
