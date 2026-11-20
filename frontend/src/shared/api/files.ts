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

function withQuery(url: string, query: Record<string, string> = {}): string {
  const params = new URLSearchParams(query)
  if (endpoint.token) params.set('token', endpoint.token)
  const text = params.toString()
  return text ? `${url}?${text}` : url
}

function withToken(url: string): string {
  return withQuery(url)
}

/** 缩略图宽度档位（px），与后端 `services/thumbs.py::WIDTHS` 一致。 */
export type ThumbWidth = 128 | 256 | 512 | 1024

const IMAGE_RE = /\.(png|jpe?g|webp|bmp)$/i
const VIDEO_RE = /\.(mp4|mov|m4v|webm|mkv|avi)$/i

/**
 * 工程目录内的文件，rel 就是 Asset.path。
 *
 * `variant` 是缩略图宽度时只对图片生效：后端缩一份 WebP 缓存起来再给（AI 出的 PNG
 * 动辄几 MB，40 px 的格子没必要下原图）。是 `'preview'` 时只对视频生效：已有预览代理
 * 就播代理（低码率、moov 前置），没有照旧播原片。导出与下载永远用不带参数的原文件。
 */
export function fileUrl(pid: string, rel: string, variant?: ThumbWidth | 'preview'): string {
  if (!pid || !rel) return ''
  const url = `${endpoint.baseUrl}/projects/${encodeURIComponent(pid)}/files/${encodePath(rel)}`
  if (variant === 'preview') return withQuery(url, VIDEO_RE.test(rel) ? { preview: 'true' } : {})
  return withQuery(url, variant && IMAGE_RE.test(rel) ? { w: String(variant) } : {})
}

/** 素材库目录内的文件（Phase 3 起可用）。 */
export function libraryFileUrl(rel: string): string {
  if (!rel) return ''
  return withToken(`${endpoint.baseUrl}/library/files/${encodePath(rel)}`)
}
