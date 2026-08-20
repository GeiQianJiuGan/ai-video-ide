/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, unknown>
  export default component
}

/** Tauri 壳在加载前端前注入的后端接入点（见 backend/app/main.py::_write_endpoint）。 */
interface AivsEndpoint {
  baseUrl: string
  wsUrl: string
  token: string
  version: string
}

interface Window {
  __AIVS_ENDPOINT__?: AivsEndpoint
}
