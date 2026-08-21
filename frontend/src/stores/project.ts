/**
 * 项目 store：当前打开的工程 + 最近列表 + 升级提示。
 *
 * 两条硬约束在这里体现：
 *   1. 绝不静默失败——每个动作失败都把 ApiError 留在 lastError 里给 UI 展示；
 *   2. schema 升级必须可见——open 返回 migrated_from 时生成一条状态条通知。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  projectsApi,
  type CreateProjectInput,
  type Project,
  type RecentProject,
} from '@/shared/api/projects'

export interface MigrationNotice {
  projectName: string
  from: number
  to: number
}

export const useProjectStore = defineStore('project', () => {
  const current = ref<Project | null>(null)
  const recent = ref<RecentProject[]>([])
  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)
  /** 「工程已升级 schema 1 → 2」——状态条上那一行的数据源。 */
  const migration = ref<MigrationNotice | null>(null)

  const isOpen = computed(() => current.value !== null)

  function fail(err: unknown): never {
    lastError.value = err instanceof ApiError ? err : null
    throw err
  }

  async function refreshRecent(): Promise<void> {
    try {
      recent.value = await projectsApi.recent()
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
    }
  }

  function adopt(project: Project): Project {
    current.value = project
    lastError.value = null
    if (project.migrated_from !== null) {
      migration.value = {
        projectName: project.name,
        from: project.migrated_from,
        to: project.schema_version,
      }
    }
    return project
  }

  async function create(input: CreateProjectInput): Promise<Project> {
    busy.value = true
    try {
      const project = adopt(await projectsApi.create(input))
      await refreshRecent()
      return project
    } catch (err) {
      return fail(err)
    } finally {
      busy.value = false
    }
  }

  async function open(dir: string): Promise<Project> {
    busy.value = true
    try {
      const project = adopt(await projectsApi.open(dir))
      await refreshRecent()
      return project
    } catch (err) {
      return fail(err)
    } finally {
      busy.value = false
    }
  }

  /** 路由带着 pid 进来（刷新页面、深链接）时补齐当前工程。 */
  async function ensure(pid: string): Promise<void> {
    if (current.value?.id === pid) return
    try {
      current.value = await projectsApi.get(pid)
    } catch (err) {
      current.value = null
      lastError.value = err instanceof ApiError ? err : null
    }
  }

  /**
   * 离开这个工程的工作区（Activity Bar 上的「← 项目列表」）。
   *
   * 刻意**不调 close**：后端的 `close` 关掉 SQLite 连接却不停这个工程的 pump，
   * 正在跑的生成会断在半路。用户说的「退出项目」指的是「我不在这儿看了」，
   * 不是「把机器上正在做的活停掉」——所以这里只忘掉前端持有的那份引用，
   * 已入队的任务照旧跑完，从最近列表点回来就接着看。
   */
  function leave(): void {
    current.value = null
    migration.value = null
    void refreshRecent()
  }

  async function close(): Promise<void> {
    const pid = current.value?.id
    if (!pid) return
    try {
      await projectsApi.close(pid)
    } finally {
      current.value = null
      migration.value = null
      await refreshRecent()
    }
  }

  async function forget(dir: string): Promise<void> {
    try {
      await projectsApi.forget(dir)
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
    }
    await refreshRecent()
  }

  function dismissMigration(): void {
    migration.value = null
  }

  function clearError(): void {
    lastError.value = null
  }

  return {
    current,
    recent,
    busy,
    lastError,
    migration,
    isOpen,
    refreshRecent,
    create,
    open,
    ensure,
    leave,
    close,
    forget,
    dismissMigration,
    clearError,
  }
})
