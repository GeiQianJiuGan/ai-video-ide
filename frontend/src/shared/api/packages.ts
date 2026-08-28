/**
 * 导入导出包（`.aivspkg`）：把「一套能跑起来的环境」搬到另一台机器上。
 *
 * 对应后端 `app/api/packages.py` + `services/packages.py`。两个粒度、一律**先账单再动手**：
 *   - 工程包：整个工程搬走（`project.db` + 素材，成片可选）+ 一份**环境要求清单**；
 *   - 场景包：只搬一幕的设定，能导进**任意已打开的工程**（id 全部重映射、同名实体默认复用）。
 *
 * 路径刻意是 `/package` 而不是 `/export`：`POST /projects/{pid}/export` 早就是时间线的
 * 「导出成片」了（产出 mp4），两者语义完全不同。
 *
 * `omitted` 是「带不走的东西」，界面**必须原样显示**——跳过不是失败，但不能不说。
 */

import { api } from './client'
import type { Project } from './projects'

export type PackageScope = 'project' | 'scene'

/** 「带不走的东西」清单里的一条。 */
export interface OmittedItem {
  kind: string
  label: string
  reason: string
  count: number | null
}

/** 库里有这一行、但文件已经不在磁盘上的资产。导出不会失败，但换机后照旧缺图。 */
export interface MissingAsset {
  id: string
  kind: string
  path: string
}

/** 包里带的是「要一份标了这几个入口的图」，不是图本身。 */
export interface PresetRequirement {
  role: string
  name: string
  markers: string[]
  unreadable: boolean
}

export interface PackageEnv {
  video_provider?: string
  audio_provider?: string
  generation_mode?: string
  presets?: PresetRequirement[]
  needs_ffmpeg?: boolean
  needs_llm?: boolean
  schema_version?: number
}

/** 环境要求与本机的逐条比对。**只报告，不抛**——缺什么要让用户先看见。 */
export interface EnvCheck {
  presets: {
    role: string
    label: string
    name: string
    markers: string[]
    present: boolean
    ready: boolean
    impact: string
  }[]
  video_provider: { wanted: string | null; current: string; matches: boolean }
  audio_provider: { wanted: string | null; current: string; matches: boolean }
  ffmpeg: { present: boolean; source: string }
  schema: { wanted: number; current: number; ok: boolean }
  missing: string[]
}

export interface ProjectExportPlan {
  scope: 'project'
  project: { id: string; name: string; dir: string }
  include_generated: boolean
  db_bytes: number
  groups: { dir: string; files: number; bytes: number; included: boolean }[]
  files: number
  total_bytes: number
  counts: { scenes: number; shots: number; assets: number }
  missing: MissingAsset[]
  env: PackageEnv
  omitted: OmittedItem[]
  suggested_filename: string
}

export interface SceneExportPlan {
  scope: 'scene'
  project: { id: string; name: string }
  scene: { id: string; title: string | null; index_no: number }
  include_generated: boolean
  counts: Record<string, number>
  files: number
  total_bytes: number
  missing: MissingAsset[]
  env: PackageEnv
  omitted: OmittedItem[]
  suggested_filename: string
}

export interface ExportResult {
  path: string
  bytes: number
  files: number
  missing: MissingAsset[]
  omitted: OmittedItem[]
}

/** 只读清单、不解包。导入前必须先看这一份。 */
export interface PackageInfo {
  path: string
  bytes: number
  scope: PackageScope | null
  package_version: number | null
  package_id: string | null
  app: string | null
  created_at: string | null
  schema_version: number
  include_generated: boolean
  project: { name?: string; width?: number; height?: number; fps?: number }
  scene: { id?: string; title?: string | null; index_no?: number }
  counts: Record<string, number>
  omitted: OmittedItem[]
  env: PackageEnv
  env_check: EnvCheck
}

export interface ImportProjectResult {
  project: Project
  files: number
  package: { path: string; package_id: string | null; include_generated: boolean }
  migrated_from: number | null
  env_check: EnvCheck
}

/** 一个人物 / 地点 / 道具是复用已有的还是新建一个。 */
export interface EntityPlanItem {
  kind: 'character' | 'location' | 'prop'
  name: string
  action: 'reuse' | 'create'
  target_id?: string
}

export interface SceneImportPlan {
  scope: 'scene'
  path: string
  target_project: { id: string; name: string }
  scene: { id?: string; title?: string | null; index_no?: number }
  reuse_by_name: boolean
  counts: Record<string, number>
  entities: EntityPlanItem[]
  assets: { total: number; reuse: number; copy: number }
  omitted: OmittedItem[]
  env: PackageEnv
  env_check: EnvCheck
}

export interface SceneImportResult {
  scope: 'scene'
  project: { id: string; name: string }
  scene: { id: string; title: string | null; index_no: number }
  shots: number
  shot_links: number
  adopted_versions: number
  assets: { assets_new: number; assets_reused: number; assets_missing: number }
  entities: EntityPlanItem[]
  reuse_by_name: boolean
  omitted: OmittedItem[]
  env_check: EnvCheck
}

export const packagesApi = {
  planProject: (pid: string, includeGenerated = false) =>
    api.post<ProjectExportPlan>(`/projects/${pid}/package/plan`, {
      include_generated: includeGenerated,
    }),

  exportProject: (pid: string, outDir: string, filename = '', includeGenerated = false) =>
    api.post<ExportResult>(`/projects/${pid}/package`, {
      out_dir: outDir,
      filename,
      include_generated: includeGenerated,
    }),

  planScene: (pid: string, sid: string, includeGenerated = false) =>
    api.post<SceneExportPlan>(`/projects/${pid}/scenes/${sid}/package/plan`, {
      include_generated: includeGenerated,
    }),

  exportScene: (
    pid: string,
    sid: string,
    outDir: string,
    filename = '',
    includeGenerated = false,
  ) =>
    api.post<ExportResult>(`/projects/${pid}/scenes/${sid}/package`, {
      out_dir: outDir,
      filename,
      include_generated: includeGenerated,
    }),

  /** 只读清单：这是什么包、带了什么，以及**它要的环境本机齐不齐**。 */
  inspect: (path: string) => api.post<PackageInfo>('/packages/inspect', { path }),

  /** 还原成一个工程并打开它。**导入的副本会拿到一个新的工程 id。** */
  importProject: (path: string, dir: string) =>
    api.post<ImportProjectResult>('/packages/import/project', { path, dir }),

  planSceneImport: (pid: string, path: string, reuseByName = true) =>
    api.post<SceneImportPlan>(`/projects/${pid}/packages/import/scene/plan`, {
      path,
      reuse_by_name: reuseByName,
    }),

  importScene: (pid: string, path: string, reuseByName = true) =>
    api.post<SceneImportResult>(`/projects/${pid}/packages/import/scene`, {
      path,
      reuse_by_name: reuseByName,
    }),
}
