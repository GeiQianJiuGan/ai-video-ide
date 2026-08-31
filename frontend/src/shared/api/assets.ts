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
  /**
   * 这张素材「长什么样」的那一句——**模型引用它时唯一看得到的说明**。
   * 空 / null = 没写，此时 prompt 里只有一个文件名（后端 `providers/base.py::ref_hint`）。
   * 清空要传 `''`：`null` 在后端是「这次不改」。
   */
  description: string | null
}

/** 缺描述的那一批（`GET /assets/undescribed`）。挂在谁身上由后端说，前端不反查。 */
export interface UndescribedAsset extends Asset {
  /** 后缀认出来的媒体类型：只有 image 能让 AI 真的看图。 */
  media: 'image' | 'video' | 'audio' | string
  owners: { owner_kind: string; owner_id: string; role: string | null }[]
}

export interface UndescribedList {
  items: UndescribedAsset[]
  count: number
  described: number
  /** 描述进 prompt 时的截断上限。**字数提示只认这个数**，前端不写死第二份。 */
  desc_max: number
  note: string
}

/** 反向引用：谁在用这个文件。owner_kind + owner_id 就是「破坏点」的地址。 */
export interface AssetRef {
  id: string
  asset_id: string
  owner_kind: string
  owner_id: string
  role: string | null
  created_at: string
}

/** 删除结果：文件到底删掉没有、强删破了几处引用、连带删了哪些临时帧，都要说出来。 */
export interface DeleteResult {
  id: string
  file_removed: boolean
  broken_refs: number
  /**
   * 跟着一起删掉的**临时帧**（从这段成片抽出来的首 / 末帧）。
   * 它们不算工程资产（不在列表里），生命周期挂在源文件上——源片没了就留不住。
   * 空数组是常态：大多数资产没派生过帧。
   */
  derived_removed: { id: string; path: string }[]
}

export const assetsApi = {
  list: (pid: string, kind?: AssetKind) =>
    api.get<Asset[]>(`/projects/${pid}/assets${kind ? `?kind=${kind}` : ''}`),
  /** 没有任何引用的资产——可以安全回收的那一批。 */
  orphans: (pid: string) => api.get<Asset[]>(`/projects/${pid}/assets/orphans`),
  refs: (pid: string, assetId: string) =>
    api.get<AssetRef[]>(`/projects/${pid}/assets/${assetId}/refs`),
  /** multipart 上传。kind 走 Form 字段，决定文件落进哪个子目录。 */
  upload: (pid: string, file: File, kind: AssetKind) =>
    api.upload<Asset>(`/projects/${pid}/assets/upload`, file, { kind }),
  /**
   * 改那一句描述。**只有 description 一个字段能改**（path / kind 是落盘事实）。
   * 清空传 `''`——`null` 是「这次不改」。
   */
  update: (pid: string, assetId: string, patch: { description: string }) =>
    api.patch<Asset>(`/projects/${pid}/assets/${assetId}`, patch),
  /** 还没有描述的素材。`desc_max` 也从这里来，前端不写死 120。 */
  undescribed: (pid: string) =>
    api.get<UndescribedList>(`/projects/${pid}/assets/undescribed`),
  /** 仍被引用时后端默认拒绝（CONFLICT 并列出是谁在用）；force 才强删。 */
  remove: (pid: string, assetId: string, force = false) =>
    api.del<DeleteResult>(`/projects/${pid}/assets/${assetId}?force=${force}`),
}

/** owner_kind 的中文名。反查面板里「appearance:apr_xxx」这种地址至少要说清是什么东西。 */
export const OWNER_KIND_LABEL: Record<string, string> = {
  appearance: '角色形象',
  character: '角色',
  location_variant: '地点变体',
  location: '地点',
  prop: '道具',
  shot: '镜头',
  version: '生成版本',
  timeline_clip: '时间线片段',
  scene: '场景',
}

export const ASSET_KIND_LABEL: Record<string, string> = {
  character_sheet: '角色表',
  location_reference: '场景参考',
  prop_reference: '道具图',
  audio: '音频',
  upload: '手动上传',
  generated_image: '生成图',
  generated_video: '生成视频',
  proxy: '代理',
  export: '导出成片',
}

/** 与 shared/api/library.ts::humanBytes 同口径，避免两处显示不一致。 */
export { humanBytes } from './library'
