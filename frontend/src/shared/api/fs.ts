/**
 * 本机目录浏览接口（Phase 1）。
 *
 * 浏览器拿不到绝对路径，所以「选文件夹」由后端 /fs/* 提供，
 * 字段与 backend/app/api/fs.py 的响应模型一一对应。
 */

import { api } from './client'

export interface FsRoot {
  name: string
  path: string
  /** drive = 驱动器 / 根；place = 主目录、桌面、文档 */
  kind: 'drive' | 'place'
}

export interface FsRoots {
  roots: FsRoot[]
  home: string
  sep: string
}

export interface FsEntry {
  name: string
  path: string
  /** 已经是一个 aivs 工程目录，可以直接打开 */
  is_project: boolean
  /** 已经是一个 aivs 素材库目录 */
  is_library: boolean
  has_children: boolean
  writable: boolean
}

export interface FsCrumb {
  name: string
  path: string
}

export interface FsDir {
  path: string
  /** 盘符没有上一级，这时是 null */
  parent: string | null
  name: string
  is_project: boolean
  is_library: boolean
  writable: boolean
  entries: FsEntry[]
  /** 子目录过多时只返回了前一批 */
  truncated: boolean
  crumbs: FsCrumb[]
}

export const fsApi = {
  roots: () => api.get<FsRoots>('/fs/roots'),
  dirs: (path: string) => api.get<FsDir>(`/fs/dirs?path=${encodeURIComponent(path)}`),
  mkdir: (parent: string, name: string) =>
    api.post<{ path: string; name: string }>('/fs/mkdir', { parent, name }),
}
