/**
 * 世界 store：地点 + 变体 + 道具。
 *
 * 与 stores/cast.ts 同构（pid 由页面传入、busy / lastError、动作后重拉）。
 * 一个刻意的取舍：变体的参考图与「被哪些 Scene 用着」是按需拉的
 * （selectVariant 时才请求），因为一个工程的地点可能有很多变体，
 * 进页面就把所有变体的图全拉一遍纯属浪费。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/shared/api/client'
import {
  worldApi,
  type Location,
  type LocationPatch,
  type LocationReference,
  type Prop,
  type PropPatch,
  type PropReference,
  type VariantPatch,
  type VariantUsage,
} from '@/shared/api/world'

export const useWorldStore = defineStore('world', () => {
  const locations = ref<Location[]>([])
  const selectedLocationId = ref('')
  const selectedVariantId = ref('')
  const references = ref<LocationReference[]>([])
  const usage = ref<VariantUsage[]>([])

  const props = ref<Prop[]>([])
  const selectedPropId = ref('')
  const propReferences = ref<PropReference[]>([])

  const busy = ref(false)
  const lastError = ref<ApiError | null>(null)

  const selectedLocation = computed(
    () => locations.value.find((l) => l.id === selectedLocationId.value) ?? null,
  )
  const variants = computed(() => selectedLocation.value?.variants ?? [])
  const selectedVariant = computed(
    () => variants.value.find((v) => v.id === selectedVariantId.value) ?? null,
  )
  const selectedProp = computed(
    () => props.value.find((p) => p.id === selectedPropId.value) ?? null,
  )

  function fail(err: unknown): never {
    lastError.value = err instanceof ApiError ? err : null
    throw err
  }

  function clearError(): void {
    lastError.value = null
  }

  async function guarded<T>(run: () => Promise<T>): Promise<T> {
    busy.value = true
    try {
      const out = await run()
      lastError.value = null
      return out
    } catch (err) {
      return fail(err)
    } finally {
      busy.value = false
    }
  }

  async function loadVariantDetail(pid: string): Promise<void> {
    if (!selectedVariantId.value) {
      references.value = []
      usage.value = []
      return
    }
    const [refs, used] = await Promise.all([
      worldApi.variantReferences(pid, selectedVariantId.value),
      worldApi.variantUsage(pid, selectedVariantId.value),
    ])
    references.value = refs
    usage.value = used
  }

  /** 地点列表里已经带着 variants，所以刷新一次就够；变体详情随后按需补。 */
  async function loadLocations(pid: string): Promise<void> {
    locations.value = await worldApi.locations(pid)
    if (!locations.value.some((l) => l.id === selectedLocationId.value)) {
      selectedLocationId.value = locations.value[0]?.id ?? ''
      selectedVariantId.value = ''
    }
    if (!variants.value.some((v) => v.id === selectedVariantId.value)) {
      selectedVariantId.value = variants.value[0]?.id ?? ''
    }
    await loadVariantDetail(pid)
  }

  async function loadProps(pid: string): Promise<void> {
    props.value = await worldApi.props(pid)
    if (!props.value.some((p) => p.id === selectedPropId.value)) {
      selectedPropId.value = props.value[0]?.id ?? ''
    }
    await loadPropDetail(pid)
  }

  async function loadPropDetail(pid: string): Promise<void> {
    if (!selectedPropId.value) {
      propReferences.value = []
      return
    }
    propReferences.value = await worldApi.propReferences(pid, selectedPropId.value)
  }

  // --- 地点 ---

  async function loadWorld(pid: string): Promise<void> {
    await guarded(() => loadLocations(pid))
  }

  async function selectLocation(pid: string, lid: string): Promise<void> {
    if (selectedLocationId.value === lid) return
    selectedLocationId.value = lid
    selectedVariantId.value = variants.value[0]?.id ?? ''
    await guarded(() => loadVariantDetail(pid))
  }

  async function selectVariant(pid: string, vid: string): Promise<void> {
    selectedVariantId.value = vid
    await guarded(() => loadVariantDetail(pid))
  }

  async function createLocation(pid: string, name: string): Promise<Location> {
    return guarded(async () => {
      const row = await worldApi.createLocation(pid, { name })
      selectedLocationId.value = row.id
      selectedVariantId.value = ''
      await loadLocations(pid)
      return row
    })
  }

  async function updateLocation(pid: string, lid: string, patch: LocationPatch): Promise<void> {
    await guarded(async () => {
      await worldApi.updateLocation(pid, lid, patch)
      await loadLocations(pid)
    })
  }

  async function removeLocation(pid: string, lid: string): Promise<void> {
    await guarded(async () => {
      await worldApi.deleteLocation(pid, lid)
      if (selectedLocationId.value === lid) {
        selectedLocationId.value = ''
        selectedVariantId.value = ''
      }
      await loadLocations(pid)
    })
  }

  async function createVariant(pid: string, lid: string, patch: VariantPatch): Promise<void> {
    await guarded(async () => {
      const row = await worldApi.createVariant(pid, lid, patch)
      selectedVariantId.value = row.id
      await loadLocations(pid)
    })
  }

  async function updateVariant(pid: string, vid: string, patch: VariantPatch): Promise<void> {
    await guarded(async () => {
      await worldApi.updateVariant(pid, vid, patch)
      await loadLocations(pid)
    })
  }

  /** 仍被 Scene 引用时后端会拒绝并列出是谁在用——这里只负责把错误留给 UI。 */
  async function removeVariant(pid: string, vid: string): Promise<void> {
    await guarded(async () => {
      await worldApi.deleteVariant(pid, vid)
      if (selectedVariantId.value === vid) selectedVariantId.value = ''
      await loadLocations(pid)
    })
  }

  async function addVariantReference(
    pid: string,
    vid: string,
    assetId: string,
    camera?: string,
  ): Promise<void> {
    await guarded(async () => {
      await worldApi.addVariantReference(pid, vid, assetId, camera)
      await loadVariantDetail(pid)
    })
  }

  // --- 道具 ---

  async function loadPropsAll(pid: string): Promise<void> {
    await guarded(() => loadProps(pid))
  }

  async function selectProp(pid: string, propId: string): Promise<void> {
    selectedPropId.value = propId
    await guarded(() => loadPropDetail(pid))
  }

  async function createProp(pid: string, name: string): Promise<Prop> {
    return guarded(async () => {
      const row = await worldApi.createProp(pid, { name })
      selectedPropId.value = row.id
      await loadProps(pid)
      return row
    })
  }

  async function updateProp(pid: string, propId: string, patch: PropPatch): Promise<void> {
    await guarded(async () => {
      await worldApi.updateProp(pid, propId, patch)
      await loadProps(pid)
    })
  }

  async function removeProp(pid: string, propId: string): Promise<void> {
    await guarded(async () => {
      await worldApi.deleteProp(pid, propId)
      if (selectedPropId.value === propId) selectedPropId.value = ''
      await loadProps(pid)
    })
  }

  async function addPropReference(pid: string, propId: string, assetId: string): Promise<void> {
    await guarded(async () => {
      await worldApi.addPropReference(pid, propId, assetId)
      await loadProps(pid)
    })
  }

  return {
    locations,
    selectedLocationId,
    selectedLocation,
    variants,
    selectedVariantId,
    selectedVariant,
    references,
    usage,
    props,
    selectedPropId,
    selectedProp,
    propReferences,
    busy,
    lastError,
    loadWorld,
    selectLocation,
    selectVariant,
    createLocation,
    updateLocation,
    removeLocation,
    createVariant,
    updateVariant,
    removeVariant,
    addVariantReference,
    loadProps: loadPropsAll,
    selectProp,
    createProp,
    updateProp,
    removeProp,
    addPropReference,
    clearError,
  }
})
