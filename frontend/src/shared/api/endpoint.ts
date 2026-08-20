/**
 * 后端接入点解析。
 *
 * Tauri 运行时由壳注入 window.__AIVS_ENDPOINT__（随机端口 + 握手 token）；
 * 浏览器开发期回退到 Vite 代理的同源 /api/v1。
 */

export interface Endpoint {
  baseUrl: string
  wsUrl: string
  token: string
}

function fromInjection(): Endpoint | null {
  const injected = (window as unknown as { __AIVS_ENDPOINT__?: Endpoint }).__AIVS_ENDPOINT__
  if (!injected?.baseUrl) return null
  return injected
}

function fromSameOrigin(): Endpoint {
  const wsScheme = location.protocol === 'https:' ? 'wss' : 'ws'
  return {
    baseUrl: '/api/v1',
    wsUrl: `${wsScheme}://${location.host}/api/v1/ws`,
    token: '',
  }
}

export const endpoint: Endpoint = fromInjection() ?? fromSameOrigin()
