/**
 * 本机文件的可显示 URL。
 *
 * Asset.path 是相对工程目录的字符串，浏览器不能直接读磁盘，所以缩略图与视频
 * 预览统一走后端的 /files 端点（见 backend/app/api/files.py）。
 *
 * 握手：Tauri 运行时开着 require_handshake，而 <img src> / <video src> 带不了
 * 自定义头，所以这里把 token 拼进 query——后端只对文件读取放宽这一条。
 */

import { endpoint } from './endpoint'

/** 逐段编码：路径里的 / 要保留成层级，中文与空格要转义。 */
function encodePath(rel: string): string {
  return rel.replace(/\\/g, '/').split('/').filter(Boolean).map(encodeURIComponent).join('/')
}

function withToken(url: string): string {
  if (!endpoint.token) return url
  return `${url}?token=${encodeURIComponent(endpoint.token)}`
}

/** 工程目录内的文件，rel 就是 Asset.path。 */
export function fileUrl(pid: string, rel: string): string {
  if (!pid || !rel) return ''
  return withToken(
    `${endpoint.baseUrl}/projects/${encodeURIComponent(pid)}/files/${encodePath(rel)}`,
  )
}

/** 素材库目录内的文件（Phase 3 起可用）。 */
export function libraryFileUrl(rel: string): string {
  if (!rel) return ''
  return withToken(`${endpoint.baseUrl}/library/files/${encodePath(rel)}`)
}
