/** 系统状态 store：后端健康、外部依赖、WS 连接。底部状态条的数据源。 */

import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import { ApiError, api } from '@/shared/api/client'
import { EventClient, type BusEvent, type ConnState } from '@/shared/api/ws'

export interface Health {
  status: string
  app: string
  version: string
  schema_version: number
}

export interface DepStatus {
  name: 'ffmpeg' | 'comfyui' | 'llm'
  ok: boolean
  detail: string
  hint: string
}

export const useSystemStore = defineStore('system', () => {
  const health = ref<Health | null>(null)
  const deps = ref<DepStatus[]>([])
  const connState = ref<ConnState>('closed')
  const lastError = ref<ApiError | null>(null)
  const events = shallowRef<BusEvent[]>([])

  let client: EventClient | null = null

  async function refresh(): Promise<void> {
    try {
      health.value = await api.get<Health>('/health')
      deps.value = await api.get<DepStatus[]>('/system/deps')
      lastError.value = null
    } catch (err) {
      health.value = null
      lastError.value = err instanceof ApiError ? err : null
    }
  }

  function connect(): void {
    if (client) return
    client = new EventClient(null, [])
    client.onState((s) => (connState.value = s))
    client.on((ev) => {
      // 环形缓冲：日志面板只保留最近 200 条
      events.value = [...events.value.slice(-199), ev]
    })
    // 事件可丢失，重连后必须做一次全量对齐
    client.onReconnect(() => void refresh())
    client.connect()
  }

  function disconnect(): void {
    client?.close()
    client = null
    connState.value = 'closed'
  }

  /** 清空日志框。只丢前端这份环形缓冲，后端什么都不会被删。 */
  function clearEvents(): void {
    events.value = []
  }

  return { health, deps, connState, lastError, events, refresh, connect, disconnect, clearEvents }
})
