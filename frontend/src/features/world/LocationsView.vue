<script setup lang="ts">
/**
 * 场景（地点 · 变体）工作台（Step 3 的前端）。
 *
 * 形状上的关键：参考图挂在**变体**上而不是地点上——「城南旧宅 · 雨夜」与「· 白天」
 * 各有一套多机位参考图，Scene 引用的也是变体。所以这页的层级是
 * 地点 → 变体 → 参考图，右栏改的是变体属性（时间 / 天气 / 光线）。
 *
 * 删除能不能做由后端一处决定（仍被 Scene 引用时拒绝并说清是谁在用），
 * 前端不做二次判断，只把「被 N 个 Scene 引用」摆在删除按钮旁边，
 * 并用 usage 列表说清是哪些 Scene。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Library, Plus, RefreshCw, Sparkles, Trash2, Upload } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import LibraryPickDialog from '@/features/library/LibraryPickDialog.vue'
import { fileUrl } from '@/shared/api/files'
import { assetsApi, type Asset } from '@/shared/api/assets'
import { VARIANT_TEXT_FIELDS, type VariantPatch } from '@/shared/api/world'
import { ApiError } from '@/shared/api/client'
import { useWorldStore } from '@/stores/world'
import { useSystemStore } from '@/stores/system'

const route = useRoute()
const world = useWorldStore()
const sys = useSystemStore()

const pid = computed(() => String(route.params.pid ?? ''))
const comfyReady = computed(() => sys.deps.find((d) => d.name === 'comfyui')?.ok ?? false)

const newName = ref('')
const createOpen = ref(false)
const createAssetId = ref('')
const variantName = ref('')
/** 上传时一并记下机位，之后镜头选参考图时才分得清正面 / 侧面 / 俯视。 */
const camera = ref('')
const picking = ref(false)
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const createFileInput = ref<HTMLInputElement | null>(null)
/** 资产总账当 id → path 字典用；不按 kind 过滤，sha1 去重会让 kind 停在首次登记的值。 */
const assets = ref<Asset[]>([])
const assetError = ref<ApiError | null>(null)

const assetById = computed(() => new Map(assets.value.map((a) => [a.id, a])))
const imageAssets = computed(() =>
  assets.value.filter(
    (asset) =>
      !asset.missing &&
      (asset.mime?.startsWith('image/') ||
        /\.(png|jpe?g|webp|gif|bmp)$/i.test(asset.path)) &&
      asset.kind !== 'audio',
  ),
)
const variant = computed(() => world.selectedVariant)
/**
 * 两个面板都在说同一件事时只留一个。
 *
 * 典型是后端重启后的「项目未打开」：地点列表与资产总账会同时 404，
 * 叠两块一模一样的错误面板只会让人以为出了两个问题。
 */
const showAssetError = computed(
  () => assetError.value !== null && assetError.value.code !== world.lastError?.code,
)

function thumb(assetId: string | null | undefined): string {
  if (!assetId) return ''
  const asset = assetById.value.get(assetId)
  if (!asset || asset.missing) return ''
  return fileUrl(pid.value, asset.path)
}

async function loadAssets(): Promise<void> {
  if (!pid.value) return
  try {
    assets.value = await assetsApi.list(pid.value)
    assetError.value = null
  } catch (err) {
    assets.value = []
    assetError.value = err instanceof ApiError ? err : null
  }
}

async function reload(): Promise<void> {
  await Promise.all([world.loadWorld(pid.value).catch(() => {}), loadAssets()])
}

onMounted(reload)
watch(pid, reload)

async function createLocation(): Promise<void> {
  const name = newName.value.trim()
  if (!name || !createAssetId.value) return
  newName.value = ''
  await world.createLocation(pid.value, name, createAssetId.value).catch(() => {})
  if (!world.lastError) {
    createAssetId.value = ''
    createOpen.value = false
  }
}

function openCreate(): void {
  world.clearError()
  assetError.value = null
  newName.value = ''
  createAssetId.value = ''
  createOpen.value = true
}

async function onCreateFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  uploading.value = true
  try {
    const asset = await assetsApi.upload(pid.value, file, 'location_reference')
    createAssetId.value = asset.id
    await loadAssets()
  } catch (err) {
    assetError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

async function createVariant(): Promise<void> {
  const lid = world.selectedLocationId
  if (!lid) return
  const name = variantName.value.trim() || '新变体'
  variantName.value = ''
  await world.createVariant(pid.value, lid, { name }).catch(() => {})
}

async function onPickFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  const vid = variant.value?.id
  if (!file || !vid) return
  uploading.value = true
  try {
    const asset = await assetsApi.upload(pid.value, file, 'location_reference')
    await world.addVariantReference(pid.value, vid, asset.id, camera.value.trim() || undefined)
    camera.value = ''
    await loadAssets()
  } catch (err) {
    assetError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

async function saveLocationField(
  key: 'name' | 'description' | 'notes',
  value: string,
): Promise<void> {
  const lid = world.selectedLocationId
  if (!lid) return
  await world.updateLocation(pid.value, lid, { [key]: value || null }).catch(() => {})
}

async function saveVariantField(key: keyof VariantPatch, value: string): Promise<void> {
  const vid = variant.value?.id
  if (!vid) return
  await world.updateVariant(pid.value, vid, { [key]: value || null }).catch(() => {})
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />

    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1 border-b px-2">
      <AppButton size="sm" variant="primary" :disabled="world.busy" title="新建地点并绑定默认场景参考图" @click="openCreate()">
        <Plus :size="10" />新建地点
      </AppButton>
      <AppButton size="sm" @click="picking = true"> <Library :size="10" />从素材库采用 </AppButton>
      <AppButton
        size="sm"
        :disabled="true"
        :title="
          comfyReady
            ? '生成场景参考图要接生成队列，本轮只支持上传'
            : 'ComfyUI 不在线，且生成队列尚未接上；本轮只支持上传参考图'
        "
      >
        <Sparkles :size="10" />生成参考图
      </AppButton>
      <AppButton size="sm" variant="ghost" class="ml-auto" :disabled="world.busy" @click="reload()">
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>

    <ErrorPanel
      v-if="world.lastError"
      class="mx-2 mt-2"
      :error="world.lastError"
      @dismiss="world.clearError()"
    />
    <ErrorPanel
      v-if="showAssetError"
      class="mx-2 mt-2"
      :error="assetError"
      @dismiss="assetError = null"
    />

    <div class="flex min-h-0 flex-1 gap-2 p-2">
      <!-- 左：地点 -->
      <AppPanel title="地点" class="w-56 shrink-0">
        <EmptyState
          v-if="world.locations.length === 0"
          title="还没有地点"
          body="地点是「哪儿」，变体是「什么时候的那儿」。Scene 引用的是变体，所以同一处的白天与雨夜各有一套参考图。"
        />
        <ul v-else class="divide-line-1 divide-y">
          <li v-for="l in world.locations" :key="l.id">
            <button
              class="hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left"
              :class="l.id === world.selectedLocationId ? 'bg-accent-dim/40' : ''"
              @click="world.selectLocation(pid, l.id)"
            >
              <span class="min-w-0 flex-1">
                <span class="text-fg-1 block truncate text-xs">{{ l.name }}</span>
                <span class="text-fg-4 block truncate text-2xs"
                  >{{ l.variants.length }} 个变体</span
                >
              </span>
              <AppBadge
                v-if="l.origin_library_id"
                tone="accent"
                title="当初从素材库采用而来。采用是单向复制：库改了不回流工程，工程改了也不影响库。"
              >
                库
              </AppBadge>
            </button>
          </li>
        </ul>
      </AppPanel>

      <!-- 中：变体 + 参考图 -->
      <div class="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
        <AppPanel title="变体" class="h-36 shrink-0">
          <template #actions>
            <input
              v-model="variantName"
              placeholder="变体名，例如 雨夜"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 h-5 w-32 border px-1.5 text-2xs outline-none focus:border-accent/60"
              @keyup.enter="createVariant()"
            />
            <AppButton
              size="sm"
              :disabled="!world.selectedLocationId || world.busy"
              @click="createVariant()"
            >
              <Plus :size="10" />新建变体
            </AppButton>
          </template>
          <EmptyState
            v-if="!world.selectedLocationId"
            title="先在左边选一个地点"
            body="选中之后可以给它建多个变体（白天 / 雨夜 / 火灾后），每个变体单独挂多机位参考图。"
          />
          <EmptyState
            v-else-if="world.variants.length === 0"
            title="这个地点还没有变体"
            body="至少建一个变体，Scene 才能引用它——Scene 指向变体而不是地点。"
          />
          <ul v-else class="divide-line-1 divide-y">
            <li v-for="v in world.variants" :key="v.id">
              <div
                class="hover:bg-base-2 flex items-center gap-1.5 px-2 py-1"
                :class="v.id === world.selectedVariantId ? 'bg-accent-dim/40' : ''"
              >
                <button
                  class="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                  @click="world.selectVariant(pid, v.id)"
                >
                  <span class="text-fg-1 min-w-0 truncate text-xs">{{ v.name }}</span>
                  <span class="text-fg-4 truncate text-2xs">
                    {{
                      [v.time_of_day, v.weather, v.lighting].filter(Boolean).join(' · ') ||
                      '尚未描述时间 / 天气 / 光线'
                    }}
                  </span>
                  <AppBadge :tone="v.scene_count > 0 ? 'accent' : 'neutral'" class="ml-auto">
                    {{ v.scene_count }} 个 Scene
                  </AppBadge>
                </button>
                <button
                  class="text-fg-4 hover:text-st-failed shrink-0"
                  title="删除这个变体。仍被 Scene 引用时后端会拒绝并列出是谁在用。"
                  @click="world.removeVariant(pid, v.id)"
                >
                  <Trash2 :size="11" />
                </button>
              </div>
            </li>
          </ul>
        </AppPanel>

        <AppPanel title="多机位参考图" class="min-h-0 flex-1">
          <template #actions>
            <input
              v-model="camera"
              placeholder="机位，例如 正面全景"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 h-5 w-32 border px-1.5 text-2xs outline-none focus:border-accent/60"
            />
            <AppButton
              size="sm"
              variant="primary"
              :disabled="!variant || uploading"
              @click="fileInput?.click()"
            >
              <Upload :size="10" />{{ uploading ? '上传中…' : '上传参考图' }}
            </AppButton>
            <input
              ref="fileInput"
              type="file"
              accept="image/*"
              class="hidden"
              @change="onPickFile"
            />
          </template>
          <EmptyState
            v-if="!variant"
            title="尚无选中变体"
            body="选一个变体后可以给它挂多机位参考图。机位名会跟着图一起记下来，镜头选图时才分得清正面与俯视。"
          />
          <EmptyState
            v-else-if="world.references.length === 0"
            title="这个变体还没有参考图"
            body="上传一张即可。没有参考图的变体在生成时上下文不完整，概览页的连续性检查会点出来。"
          />
          <div v-else class="grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-2 p-2">
            <figure
              v-for="r in world.references"
              :key="r.id"
              class="border bg-base-2 flex flex-col overflow-hidden"
              :class="r.is_current ? 'border-accent/60' : 'border-line-1'"
            >
              <span class="bg-base-3 flex h-24 items-center justify-center overflow-hidden">
                <img
                  v-if="thumb(r.asset_id)"
                  :src="thumb(r.asset_id)"
                  alt=""
                  class="h-full w-full object-cover"
                />
                <span v-else class="text-fg-4 text-2xs">文件不在了</span>
              </span>
              <figcaption class="flex items-center gap-1 px-1.5 py-1">
                <span class="text-fg-2 truncate text-2xs">{{ r.camera || '未标机位' }}</span>
                <AppBadge v-if="r.is_current" tone="ok" class="ml-auto">当前</AppBadge>
              </figcaption>
            </figure>
          </div>
        </AppPanel>
      </div>

      <!-- 右：属性 + 被谁引用 -->
      <AppPanel title="属性" class="w-72 shrink-0">
        <EmptyState
          v-if="!world.selectedLocation"
          title="尚无选中项"
          body="选中地点后可以改它的说明；选中变体后可以描述时间 / 天气 / 光线——这些会进生成上下文。"
        />
        <div v-else class="space-y-3 p-2">
          <section>
            <p class="text-fg-3 text-2xs tracking-wide uppercase">地点</p>
            <div class="mt-1 space-y-1">
              <label
                v-for="f in [
                  { key: 'name', label: '名称' },
                  { key: 'description', label: '描述' },
                  { key: 'notes', label: '备注' },
                ] as const"
                :key="f.key"
                class="block"
              >
                <span class="text-fg-4 text-2xs">{{ f.label }}</span>
                <input
                  :value="world.selectedLocation[f.key] ?? ''"
                  class="border-line-1 bg-base-2 text-fg-1 mt-px h-5 w-full border px-1.5 text-2xs outline-none focus:border-accent/60"
                  @change="saveLocationField(f.key, ($event.target as HTMLInputElement).value)"
                />
              </label>
            </div>
            <AppButton
              size="sm"
              variant="danger"
              class="mt-1.5"
              :disabled="world.busy"
              @click="world.removeLocation(pid, world.selectedLocationId)"
            >
              <Trash2 :size="10" />删除地点
            </AppButton>
          </section>

          <section v-if="variant" class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">变体 · {{ variant.name }}</p>
            <div class="mt-1 space-y-1">
              <label v-for="f in VARIANT_TEXT_FIELDS" :key="String(f.key)" class="block">
                <span class="text-fg-4 text-2xs">{{ f.label }}</span>
                <input
                  :value="
                    (variant as unknown as Record<string, string | null>)[String(f.key)] ?? ''
                  "
                  :placeholder="f.hint"
                  class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 mt-px h-5 w-full border px-1.5 text-2xs outline-none focus:border-accent/60"
                  @change="saveVariantField(f.key, ($event.target as HTMLInputElement).value)"
                />
              </label>
            </div>
          </section>

          <section v-if="variant" class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">被哪些 Scene 引用</p>
            <p v-if="world.usage.length === 0" class="text-fg-4 mt-1 text-2xs">
              还没有 Scene 用它，现在删掉不会破坏任何东西。
            </p>
            <ul v-else class="mt-1 space-y-0.5">
              <li v-for="s in world.usage" :key="s.id" class="text-fg-2 flex gap-1.5 text-2xs">
                <span class="text-fg-4 tnum">#{{ s.index_no }}</span>
                <span class="truncate">{{ s.title }}</span>
              </li>
            </ul>
          </section>
        </div>
      </AppPanel>
    </div>

    <LibraryPickDialog v-model:open="picking" :pid="pid" kind="location" @adopted="reload()" />
    <input ref="createFileInput" type="file" accept="image/*" class="hidden" @change="onCreateFile" />
    <AppDialog
      :open="createOpen"
      title="新建地点"
      subtitle="必须绑定一张默认场景参考图"
      size="sm"
      @update:open="createOpen = $event"
    >
      <form id="create-location" class="space-y-3 p-3" @submit.prevent="createLocation()">
        <label class="block">
          <span class="text-fg-3 text-2xs">地点名称</span>
          <input v-model="newName" autofocus placeholder="城南旧宅" class="border-line-1 bg-base-2 text-fg-1 mt-0.5 h-row w-full border px-2 text-xs outline-none" />
        </label>
        <section>
          <div class="flex items-center justify-between">
            <span class="text-fg-3 text-2xs">默认场景参考图（必选）</span>
            <AppButton size="sm" variant="ghost" :disabled="uploading" @click="createFileInput?.click()">
              <Upload :size="10" />{{ uploading ? '上传中' : '上传图片' }}
            </AppButton>
          </div>
          <div v-if="createAssetId && thumb(createAssetId)" class="border-line-1 bg-base-2 mt-1.5 flex h-32 items-center justify-center overflow-hidden border">
            <img :src="thumb(createAssetId)" alt="默认场景参考图" class="size-full object-contain" />
          </div>
          <p v-else class="text-fg-4 mt-1.5 text-2xs">请上传一张场景参考图后再创建。</p>
          <div v-if="imageAssets.length" class="mt-2 grid max-h-40 grid-cols-4 gap-1.5 overflow-auto">
            <button v-for="asset in imageAssets" :key="asset.id" type="button" class="bg-base-2 aspect-square overflow-hidden border" :class="createAssetId === asset.id ? 'border-accent/70 ring-1 ring-accent/30' : 'border-line-1'" title="使用项目中的这张图片" @click="createAssetId = asset.id">
              <img :src="fileUrl(pid, asset.path)" alt="" class="size-full object-cover" />
            </button>
          </div>
        </section>
      </form>
      <template #footer>
        <span class="flex-1" />
        <AppButton variant="ghost" @click="createOpen = false">取消</AppButton>
        <AppButton type="submit" form="create-location" variant="primary" :disabled="world.busy || uploading || !newName.trim() || !createAssetId">
          <Plus :size="11" />新建地点
        </AppButton>
      </template>
    </AppDialog>
  </div>
</template>
