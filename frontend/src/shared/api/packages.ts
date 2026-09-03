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
 * **落点的主路是用户那台机器**：界面跑在浏览器 / WebView 里，拿不到也不该猜后端机器上的
 * 路径。所以导出走 `downloadProject` / `downloadScene`（附件流回来，`saveBlob` 保存），
 * 导入走 `upload`（把文件传上去落进暂存区，回的形状**和 `inspect` 一样**，于是
 * `importProject` / `importScene` 一行都不用改）。用户看了账单又取消时调 `discardStaged`
 * 把那份临时副本删掉——几个 G 的东西攒起来是实打实的磁盘问题。
 *
 * 「写进后端机器上某个目录 / 读那台机器上某个路径」那条老路照旧留着（`exportProject` 收
 * `outDir`、`inspect` 收路径）：桌面版里两台机器其实是同一台，几个 G 的包不必从自己这儿
 * 传给自己一遍。
 *
 * `omitted` 是「带不走的东西」，界面**必须原样显示**——跳过不是失败，但不能不说。
 */

import { api } from './client'
import type { Downloaded } from './client'
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

/**
 * 刚上传上来、还躺在暂存区里的一份包。
 *
 * 形状与 `PackageInfo` 完全一样（同一段后端代码出的账单），只多两项：`staged` 认出它是
 * 临时副本，`name` 是给人看的原文件名。`path` 照旧交给 `importProject` / `importScene`。
 */
export interface StagedPackage extends PackageInfo {
  staged: boolean
  name: string
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

/** 下载那两个入口的查询串。空值不发，后端两个参数都有默认值。 */
function query(includeGenerated: boolean, filename: string): string {
  const parts: string[] = []
  if (includeGenerated) parts.push('include_generated=true')
  if (filename.trim()) parts.push(`filename=${encodeURIComponent(filename.trim())}`)
  return parts.length ? `?${parts.join('&')}` : ''
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

  /**
   * 导出并**下载到用户那台机器**（导出的主路）。包写在后端的临时目录，流完就删。
   *
   * 走 GET + `api.download`：握手开着时 `<a href>` 带不了 `X-AIVS-Token`，所以必须
   * fetch 回 Blob 再交给 `saveBlob`。
   */
  downloadProject: (pid: string, includeGenerated = false, filename = ''): Promise<Downloaded> =>
    api.download(`/projects/${pid}/package/download${query(includeGenerated, filename)}`),

  downloadScene: (
    pid: string,
    sid: string,
    includeGenerated = false,
    filename = '',
  ): Promise<Downloaded> =>
    api.download(
      `/projects/${pid}/scenes/${sid}/package/download${query(includeGenerated, filename)}`,
    ),

  /** 只读清单：这是什么包、带了什么，以及**它要的环境本机齐不齐**。 */
  inspect: (path: string) => api.post<PackageInfo>('/packages/inspect', { path }),

  /**
   * 把用户电脑上的一个包传上去（导入的主路），回**和 `inspect` 一样**的那份账单。
   *
   * 回来的 `path` 是暂存副本的路径，直接交给 `importProject` / `importScene`；
   * 用户看了账单又取消时调 `discardStaged(path)`。
   */
  upload: (file: File) => api.upload<StagedPackage>('/packages/upload', file),

  /** 丢掉一份上传上来的临时副本。只认暂存区里的路径，指到别处后端会拒。 */
  discardStaged: (path: string) =>
    api.post<{ ok: boolean; discarded: string }>('/packages/staged/discard', { path }),

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
