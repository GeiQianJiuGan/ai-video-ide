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
  /** 参考图比模型端那份图能收的多。**不是失败，是一次确认**（见 isConfirmable）。 */
  | 'REF_OVER_CAPACITY'
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
  related_ids: Record<string, unknown>
}

export class ApiError extends Error {
  readonly code: ErrorCode
  /** 面向用户的一句话标题（与 message 同值，便于模板直读）。 */
  readonly title: string
  readonly detail: string
  readonly suggestions: string[]
  readonly relatedIds: Record<string, unknown>
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

/**
 * 「确认一下就能继续」判定，返回要带回去的那个参数名（不是确认类错误时是空串）。
 *
 * 有些拦截不是失败而是一次确认：参考图比模型端那份图能收的多时后端回
 * `REF_OVER_CAPACITY`，**一个任务都还没入队**，说明会丢几张、丢哪几张；用户点确认后
 * 带上 `related_ids.confirm` 里写的那个参数（值 true）重新调同一个入口即可。
 * 判定看的是这个字段而不是 code 白名单——以后再多一种确认，UI 一行都不用改。
 */
export function confirmFlagOf(err: unknown): string {
  return err instanceof ApiError ? String(err.relatedIds.confirm ?? '') : ''
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
  /** 下载二进制（见下面的 download）。 */
  download: downloadRequest,
  /** SSE（见下面的 stream）。 */
  stream: streamRequest,
}

/** 下载回来的一份文件：字节 + 后端说的那个文件名。 */
export interface Downloaded {
  blob: Blob
  filename: string
}

/**
 * GET 一个二进制附件。
 *
 * **刻意不用 `<a href>` 直连**：握手开着时后端只在路径里含 `/files/` 的 GET 上接受
 * `?token=`（`app/main.py::_authorized`），别的入口一律要 `X-AIVS-Token` 头，而
 * `<a href>` 带不了自定义头。所以走 fetch + Blob，再由 `saveBlob` 交给浏览器保存。
 *
 * 失败仍然是 `ApiError`：后端在开始流之前把 `{error}` 抛出来（工程没打开、磁盘满…），
 * 这里照 `request()` 那套解出来——否则用户只会看到一个下载失败的空文件（静默失败）。
 */
async function downloadRequest(path: string): Promise<Downloaded> {
  const headers: Record<string, string> = {}
  if (endpoint.token) headers['X-AIVS-Token'] = endpoint.token

  let resp: Response
  try {
    resp = await fetch(`${endpoint.baseUrl}${path}`, { method: 'GET', headers })
  } catch (cause) {
    throw networkError(cause)
  }

  if (!resp.ok) {
    const text = await resp.text().catch(() => '')
    let wrapped: { error?: ErrorPayload } | null = null
    try {
      wrapped = text ? (JSON.parse(text) as { error?: ErrorPayload }) : null
    } catch {
      wrapped = null
    }
    if (!wrapped?.error) throw offContractError(resp.status, text)
    throw new ApiError(wrapped.error, resp.status)
  }

  return {
    blob: await resp.blob(),
    filename: filenameOf(resp.headers.get('Content-Disposition') ?? ''),
  }
}

/**
 * 从 `Content-Disposition` 里取文件名。
 *
 * 两支都要认：中文名走 RFC 5987 的 `filename*=utf-8''%E5%B8%A6...`（Starlette 对非
 * ASCII 名字只给这一支），纯 ASCII 名字给的是 `filename="x.aivspkg"`。只认后者的话
 * 中文工程名会下载成一串百分号编码。
 */
function filenameOf(disposition: string): string {
  const star = /filename\*=(?:utf-8|UTF-8)''([^;]+)/.exec(disposition)?.[1]
  if (star !== undefined) {
    try {
      return decodeURIComponent(star.trim())
    } catch {
      return star.trim()
    }
  }
  const plain = /filename="?([^";]+)"?/.exec(disposition)?.[1]
  return plain === undefined ? '' : plain.trim()
}

/**
 * 把一份下载好的字节交给浏览器保存。
 *
 * 桌面版里这一步同样有效（WebView 会弹系统保存对话框），所以不需要为 Tauri 再走一条
 * 路。对象 URL 必须撤销，否则这几个 G 的 Blob 会一直挂在内存里；延迟一下是因为点击
 * 触发的下载是异步开始的，立刻撤销会把文件抽走。
 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename || 'download'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

/** 一条 SSE 事件。`data` 后端保证是一行 JSON。 */
export interface StreamEvent {
  event: string
  data: unknown
}

/**
 * POST 一个 SSE 流，逐条 yield 事件。
 *
 * **刻意不用 `EventSource`**：它只会 GET，而且带不了 `X-AIVS-Token`（Tauri 里握手是
 * 必须的）。所以走 `fetch` + ReadableStream，自己按空行切帧。
 *
 * 三条与 `request()` 一致的规矩：
 *   1. **开流之前的失败仍是 `ApiError`**——后端把「消息是空的 / LLM 没配 / 工程没打开」
 *      放在 200 之前抛，所以这里照 `request()` 那套把 `{error}` 解出来，调用方
 *      只需要 catch 一种东西；
 *   2. **连不上也是 `ApiError`**（`NETWORK_ERROR`），不是一个裸的 TypeError；
 *   3. **`signal` 由调用方给**：用户点「停」就 abort，abort 不算错误——
 *      迭代直接结束，已经收到的事件照旧有效。
 */
async function* streamRequest(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const headers: Record<string, string> = {
    Accept: 'text/event-stream',
    'Content-Type': 'application/json',
  }
  if (endpoint.token) headers['X-AIVS-Token'] = endpoint.token

  let resp: Response
  try {
    resp = await fetch(`${endpoint.baseUrl}${path}`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body ?? {}),
      signal,
    })
  } catch (cause) {
    if (signal?.aborted) return
    throw networkError(cause)
  }

  if (!resp.ok || !resp.body) {
    const text = await resp.text().catch(() => '')
    let wrapped: { error?: ErrorPayload } | null = null
    try {
      wrapped = text ? (JSON.parse(text) as { error?: ErrorPayload }) : null
    } catch {
      wrapped = null
    }
    if (!wrapped?.error) throw offContractError(resp.status, text)
    throw new ApiError(wrapped.error, resp.status)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      // 一帧到空行为止；最后那段可能还没收完，留在 buf 里等下一片。
      let cut = buf.indexOf('\n\n')
      while (cut >= 0) {
        const frame = buf.slice(0, cut)
        buf = buf.slice(cut + 2)
        const parsed = parseFrame(frame)
        if (parsed) yield parsed
        cut = buf.indexOf('\n\n')
      }
    }
    const tail = parseFrame(buf)
    if (tail) yield tail
  } catch (cause) {
    if (signal?.aborted) return
    throw networkError(cause)
  } finally {
    reader.cancel().catch(() => undefined)
  }
}

/** 一帧文本 → `{event, data}`。读不懂的帧丢掉（心跳注释、空帧都走这条）。 */
function parseFrame(frame: string): StreamEvent | null {
  let name = ''
  const data: string[] = []
  for (const raw of frame.split('\n')) {
    const line = raw.replace(/\r$/, '')
    if (line.startsWith('event:')) name = line.slice(6).trim()
    else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
  }
  if (!name || !data.length) return null
  try {
    return { event: name, data: JSON.parse(data.join('\n')) }
  } catch {
    return null
  }
}
