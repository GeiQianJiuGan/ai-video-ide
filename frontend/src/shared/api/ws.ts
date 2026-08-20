/**
 * WebSocket 客户端：自动重连 + 频道订阅。
 *
 * 硬约束（docs/03 §5）：事件幂等、可丢失。重连后必须由调用方走 REST 做全量对齐，
 * 因此这里不缓存、不重放任何事件。
 */

import { endpoint } from './endpoint'

export type Channel = 'job' | 'queue' | 'shot' | 'version' | 'asset' | 'system' | 'error'

export interface BusEvent<P = Record<string, unknown>> {
  channel: Channel
  event: string
  project_id: string | null
  ts: string
  payload: P
}

export type ConnState = 'connecting' | 'open' | 'closed'

type Handler = (ev: BusEvent) => void

const RECONNECT_MS = [500, 1000, 2000, 4000, 8000] as const

export class EventClient {
  private socket: WebSocket | null = null
  private handlers = new Set<Handler>()
  private stateHandlers = new Set<(s: ConnState) => void>()
  private reconnectHandlers = new Set<() => void>()
  private attempt = 0
  private timer: number | null = null
  private closedByUser = false
  private hadConnection = false

  constructor(
    private projectId: string | null = null,
    private channels: Channel[] = [],
  ) {}

  get state(): ConnState {
    if (!this.socket) return 'closed'
    return this.socket.readyState === WebSocket.OPEN ? 'open' : 'connecting'
  }

  connect(): void {
    this.closedByUser = false
    this.open()
  }

  private url(): string {
    const params = new URLSearchParams()
    if (this.projectId) params.set('project_id', this.projectId)
    if (this.channels.length) params.set('channels', this.channels.join(','))
    if (endpoint.token) params.set('token', endpoint.token)
    const qs = params.toString()
    return qs ? `${endpoint.wsUrl}?${qs}` : endpoint.wsUrl
  }

  private open(): void {
    this.emitState('connecting')
    const socket = new WebSocket(this.url())
    this.socket = socket

    socket.onopen = () => {
      this.attempt = 0
      this.emitState('open')
      // 重连成功：通知调用方做一次 REST 全量对齐
      if (this.hadConnection) this.reconnectHandlers.forEach((h) => h())
      this.hadConnection = true
    }
    socket.onmessage = (raw) => {
      const ev = JSON.parse(raw.data as string) as BusEvent
      if (ev.event === 'system.ping') return
      this.handlers.forEach((h) => h(ev))
    }
    socket.onclose = () => {
      this.socket = null
      this.emitState('closed')
      if (!this.closedByUser) this.scheduleReconnect()
    }
    socket.onerror = () => socket.close()
  }

  private scheduleReconnect(): void {
    const delay = RECONNECT_MS[Math.min(this.attempt, RECONNECT_MS.length - 1)] ?? 8000
    this.attempt += 1
    this.timer = window.setTimeout(() => this.open(), delay)
  }

  private emitState(s: ConnState): void {
    this.stateHandlers.forEach((h) => h(s))
  }

  on(handler: Handler): () => void {
    this.handlers.add(handler)
    return () => this.handlers.delete(handler)
  }

  onState(handler: (s: ConnState) => void): () => void {
    this.stateHandlers.add(handler)
    return () => this.stateHandlers.delete(handler)
  }

  /** 重连成功后触发，调用方在此重新拉取 /jobs、/shots 等做全量对齐。 */
  onReconnect(handler: () => void): () => void {
    this.reconnectHandlers.add(handler)
    return () => this.reconnectHandlers.delete(handler)
  }

  close(): void {
    this.closedByUser = true
    if (this.timer !== null) window.clearTimeout(this.timer)
    this.socket?.close()
    this.socket = null
  }
}
