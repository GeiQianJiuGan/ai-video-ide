/**
 * 素材库 store：应用级的一个库 + 四类内容 + 标签。
 *
 * 与 stores/project.ts 同构，但少一个维度——库是应用级的，同一时刻只打开一个，
 * 所以这里没有 pid，也没有「最近列表」。
 *
 * 「没配置素材库」不是错误：status.configured 为 false 时 UI 画引导，
 * 而 /library/* 的真实失败（目录被占、清单太新、文件不见）一律落到 lastError，
 * 由 UI 把 suggestions 显示出来。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  libraryApi,
  type AdoptKind,
  type AdoptPlan,
  type AdoptResult,
  type LibraryAsset,
  type LibraryCharacter,
  type LibraryKind,
  type LibraryLocation,
  type LibraryProp,
  type LibraryStatus,
  type LibraryTag,
} from '@/shared/api/library'

export const useLibraryStore = defineStore('library', () => {
  const status = ref<LibraryStatus | null>(null)
  const assets = ref<LibraryAsset[]>([])
  const characters = ref<LibraryCharacter[]>([])
  const locations = ref<LibraryLocation[]>([])
  const props = ref<LibraryProp[]>([])
  const tags = ref<LibraryTag[]>([])
  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const configured = computed(() => status.value?.configured === true)
  const info = computed(() => status.value?.library ?? null)

  function fail(err: unknown): never {
    lastError.value = err instanceof ApiError ? err : null
    throw err
  }

  function clearError(): void {
    lastError.value = null
  }

  function reset(): void {
    assets.value = []
    characters.value = []
    locations.value = []
    props.value = []
    tags.value = []
  }

  /** 库里四类内容 + 标签一次拉齐：库是本地 SQLite，量级不值得做分页。 */
  async function reload(): Promise<void> {
    if (!configured.value) {
      reset()
      return
    }
    busy.value = true
    try {
      const [a, c, l, p, t] = await Promise.all([
        libraryApi.assets(),
        libraryApi.characters(),
        libraryApi.locations(),
        libraryApi.props(),
        libraryApi.tags(),
      ])
      assets.value = a
      characters.value = c
      locations.value = l
      props.value = p
      tags.value = t
    } catch (err) {
      lastError.value = err instanceof ApiError ? err : null
    } finally {
      busy.value = false
    }
  }

  /** 进页面先问一次「有没有库」。这一步失败不该拦住 UI，所以只记错误。 */
  async function refresh(): Promise<void> {
    try {
      status.value = await libraryApi.status()
    } catch (err) {
      status.value = null
      lastError.value = err instanceof ApiError ? err : null
      return
    }
    await reload()
  }

  async function configure(dir: string): Promise<void> {
    busy.value = true
    try {
      status.value = await libraryApi.configure(dir)
      lastError.value = null
    } catch (err) {
      fail(err)
    } finally {
      busy.value = false
    }
    await reload()
  }

  /** 「不再使用」只忘掉位置：库文件与内容都还在，重新选回来就恢复。 */
  async function close(): Promise<void> {
    busy.value = true
    try {
      status.value = await libraryApi.close()
      reset()
    } catch (err) {
      fail(err)
    } finally {
      busy.value = false
    }
  }

  async function upload(file: File, kind: LibraryKind, title?: string): Promise<LibraryAsset> {
    busy.value = true
    try {
      const row = await libraryApi.upload(file, kind, title)
      await reload()
      return row
    } catch (err) {
      return fail(err)
    } finally {
      busy.value = false
    }
  }

  /** 删素材前后端会先算 ref_count；被预设占用时 409，UI 拿到 suggestions 再问要不要 force。 */
  async function deleteAsset(aid: string, force = false): Promise<void> {
    busy.value = true
    try {
      await libraryApi.deleteAsset(aid, force)
      await reload()
    } catch (err) {
      fail(err)
    } finally {
      busy.value = false
    }
  }

  /** 建角色 / 地点 / 道具预设。库侧只要一个名字，其余字段进去再补。 */
  async function createPreset(
    kind: 'character' | 'location' | 'prop',
    name: string,
    defaultAssetId: string,
  ): Promise<void> {
    busy.value = true
    try {
      if (kind === 'character') await libraryApi.createCharacter({ name, default_asset_id: defaultAssetId })
      else if (kind === 'location') await libraryApi.createLocation({ name, default_asset_id: defaultAssetId })
      else await libraryApi.createProp({ name, default_asset_id: defaultAssetId })
      await reload()
    } catch (err) {
      fail(err)
    } finally {
      busy.value = false
    }
  }

  /** 地点的参考图挂在变体上（雨夜 / 白天各一套），所以先要有变体。 */
  async function createVariant(lid: string, name: string): Promise<void> {
    busy.value = true
    try {
      await libraryApi.createVariant(lid, { name })
      await reload()
    } catch (err) {
      fail(err)
    } finally {
      busy.value = false
    }
  }

  async function deletePreset(kind: 'character' | 'location' | 'prop', id: string): Promise<void> {
    busy.value = true
    try {
      if (kind === 'character') await libraryApi.deleteCharacter(id)
      else if (kind === 'location') await libraryApi.deleteLocation(id)
      else await libraryApi.deleteProp(id)
      await reload()
    } catch (err) {
      fail(err)
    } finally {
      busy.value = false
    }
  }

  /** 把库里的图挂到形象 / 变体 / 道具上，让预设带着参考图一起被采用。 */
  async function attachReference(
    target: { kind: 'appearance' | 'variant' | 'prop'; id: string },
    assetId: string,
  ): Promise<void> {
    busy.value = true
    try {
      if (target.kind === 'appearance') await libraryApi.addSheet(target.id, assetId)
      else if (target.kind === 'variant') await libraryApi.addVariantReference(target.id, assetId)
      else await libraryApi.addPropReference(target.id, assetId)
      await reload()
    } catch (err) {
      fail(err)
    } finally {
      busy.value = false
    }
  }

  async function deleteReference(
    target: { kind: 'sheet' | 'variant' | 'prop'; id: string },
  ): Promise<void> {
    busy.value = true
    try {
      if (target.kind === 'sheet') await libraryApi.deleteSheet(target.id)
      else if (target.kind === 'variant') await libraryApi.deleteVariantReference(target.id)
      else await libraryApi.deletePropReference(target.id)
      await reload()
    } catch (err) {
      fail(err)
    } finally {
      busy.value = false
    }
  }

  async function createTag(name: string): Promise<void> {
    busy.value = true
    try {
      await libraryApi.createTag(name)
      tags.value = await libraryApi.tags()
    } catch (err) {
      fail(err)
    } finally {
      busy.value = false
    }
  }

  async function tagAsset(tid: string, assetId: string): Promise<void> {
    busy.value = true
    try {
      await libraryApi.attachTag(tid, 'asset', assetId)
      assets.value = await libraryApi.assets()
    } catch (err) {
      fail(err)
    } finally {
      busy.value = false
    }
  }

  /** 采用前的账单。文件要进用户的工程目录，所以这一步永远先走。 */
  async function adoptPlan(pid: string, kind: AdoptKind, libraryId: string): Promise<AdoptPlan> {
    busy.value = true
    try {
      return await libraryApi.adoptPlan(pid, kind, libraryId)
    } catch (err) {
      return fail(err)
    } finally {
      busy.value = false
    }
  }

  async function adopt(pid: string, kind: AdoptKind, libraryId: string): Promise<AdoptResult> {
    busy.value = true
    try {
      return await libraryApi.adopt(pid, kind, libraryId)
    } catch (err) {
      return fail(err)
    } finally {
      busy.value = false
    }
  }

  return {
    status,
    assets,
    characters,
    locations,
    props,
    tags,
    busy,
    lastError,
    configured,
    info,
    refresh,
    reload,
    configure,
    close,
    upload,
    deleteAsset,
    createPreset,
    createVariant,
    deletePreset,
    attachReference,
    deleteReference,
    createTag,
    tagAsset,
    adoptPlan,
    adopt,
    clearError,
  }
})
