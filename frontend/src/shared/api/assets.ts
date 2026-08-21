/**
 * 工程内资产总账接口（Step 3）。
 *
 * 字段与后端 app/api/assets.py + services/assets.py 一一对应。
 *
 * 为什么素材页也要用它：sheet / reference 行上只有 asset_id，没有 path，
 * 而缩略图必须靠 path 才能算出 URL（见 shared/api/files.ts::fileUrl）。
 * 所以页面拉一次资产列表当 id → path 的字典用，而不是在每行上另开一个请求。
 */

import { api } from './client'

/** 资产类型。generations/ 只放生成物，手动素材一律进 assets/（后端 KIND_DIR 决定落哪）。 */
export type AssetKind =
  | 'character_sheet'
  | 'location_reference'
  | 'prop_reference'
  | 'audio'
  | 'upload'
  | 'generated_image'
  | 'generated_video'
  | 'proxy'
  | 'export'

export interface Asset {
  id: string
  kind: string
  /** 相对工程目录的路径，交给 fileUrl(pid, path) 变成可显示的 URL。 */
  path: string
  mime: string | null
  width: number | null
  height: number | null
  duration: number | null
  size_bytes: number
  sha1: string | null
  source: string
  created_at: string
  /** 工程内有多少处在引用它；删之前靠它说清会破坏什么。 */
  ref_count: number
  /** 文件被工程外的程序删掉了：登记还在，但显示不出来。 */
  missing: boolean
}

export const assetsApi = {
  list: (pid: string, kind?: AssetKind) =>
    api.get<Asset[]>(`/projects/${pid}/assets${kind ? `?kind=${kind}` : ''}`),
  /** multipart 上传。kind 走 Form 字段，决定文件落进哪个子目录。 */
  upload: (pid: string, file: File, kind: AssetKind) =>
    api.upload<Asset>(`/projects/${pid}/assets/upload`, file, { kind }),
}

/** 与 shared/api/library.ts::humanBytes 同口径，避免两处显示不一致。 */
export { humanBytes } from './library'
