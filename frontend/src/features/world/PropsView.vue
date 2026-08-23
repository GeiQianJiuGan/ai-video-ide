<script setup lang="ts">
/**
 * 道具工作台（Step 3 的前端）。
 *
 * 比角色 / 地点简单一层：道具没有形象树、也没有变体，参考图直接挂在道具上，
 * 且**只增版本**——上传即成为新的当前版本，旧版本留在历史里，没有「改掉某版」的入口。
 *
 * 右栏摆 `shot_count`：删道具之前要先说清会影响多少个 Shot。
 * 后端在仍被引用时会拒绝并说明理由，前端不做二次判断。
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
const prop = computed(() => world.selectedProp)
/**
 * 两个面板都在说同一件事时只留一个。
 *
 * 典型是后端重启后的「项目未打开」：道具列表与资产总账会同时 404，
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
  await Promise.all([world.loadProps(pid.value).catch(() => {}), loadAssets()])
}

onMounted(reload)
watch(pid, reload)

async function createProp(): Promise<void> {
  const name = newName.value.trim()
  if (!name || !createAssetId.value) return
  newName.value = ''
  await world.createProp(pid.value, name, createAssetId.value).catch(() => {})
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
    const asset = await assetsApi.upload(pid.value, file, 'prop_reference')
    createAssetId.value = asset.id
    await loadAssets()
  } catch (err) {
    assetError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

async function onPickFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  const propId = prop.value?.id
  if (!file || !propId) return
  uploading.value = true
  try {
    const asset = await assetsApi.upload(pid.value, file, 'prop_reference')
    await world.addPropReference(pid.value, propId, asset.id)
    await loadAssets()
    // 版本列表是按需拉的，加完要重新对齐一次
    await world.selectProp(pid.value, propId)
  } catch (err) {
    assetError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

async function saveField(key: 'name' | 'description' | 'notes', value: string): Promise<void> {
  const propId = prop.value?.id
  if (!propId) return
  await world.updateProp(pid.value, propId, { [key]: value || null }).catch(() => {})
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />

    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1 border-b px-2">
      <AppButton size="sm" variant="primary" :disabled="world.busy" @click="openCreate()">
        <Plus :size="10" />新建道具
      </AppButton>
      <AppButton size="sm" @click="picking = true"> <Library :size="10" />从素材库采用 </AppButton>
      <AppButton
        size="sm"
        :disabled="true"
        :title="
          comfyReady
            ? '生成道具参考图要接生成队列，本轮只支持上传'
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
      <!-- 左：道具列表 -->
      <AppPanel title="道具" class="w-56 shrink-0">
        <EmptyState
          v-if="world.props.length === 0"
          title="还没有道具"
          body="需要在多个镜头里保持一致的东西才值得建成道具（怀表、佩剑、招牌）。一次性的摆设写在镜头描述里就够了。"
        />
        <ul v-else class="divide-line-1 divide-y">
          <li v-for="p in world.props" :key="p.id">
            <button
              class="hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left"
              :class="p.id === world.selectedPropId ? 'bg-accent-dim/40' : ''"
              @click="world.selectProp(pid, p.id)"
            >
              <span
                class="border-line-1 bg-base-2 flex h-7 w-7 shrink-0 items-center justify-center overflow-hidden border"
              >
                <img
                  v-if="thumb(p.current_reference?.asset_id)"
                  :src="thumb(p.current_reference?.asset_id)"
                  alt=""
                  class="h-full w-full object-cover"
                />
                <span v-else class="text-fg-4 text-2xs">无图</span>
              </span>
              <span class="min-w-0 flex-1">
                <span class="text-fg-1 block truncate text-xs">{{ p.name }}</span>
                <span class="text-fg-4 block truncate text-2xs">
                  {{ p.reference_count }} 张参考图 · {{ p.shot_count }} 个镜头
                </span>
              </span>
              <AppBadge
                v-if="p.origin_library_id"
                tone="accent"
                title="当初从素材库采用而来。采用是单向复制：库改了不回流工程，工程改了也不影响库。"
              >
                库
              </AppBadge>
            </button>
          </li>
        </ul>
      </AppPanel>

      <!-- 中：参考图版本 -->
      <AppPanel title="参考图版本" class="min-h-0 flex-1">
        <template #actions>
          <span class="text-fg-4 text-2xs">新版本自动成为当前，旧版本永不覆盖</span>
          <AppButton
            size="sm"
            variant="primary"
            :disabled="!prop || uploading"
            @click="fileInput?.click()"
          >
            <Upload :size="10" />{{ uploading ? '上传中…' : '上传参考图' }}
          </AppButton>
          <input ref="fileInput" type="file" accept="image/*" class="hidden" @change="onPickFile" />
        </template>
        <EmptyState
          v-if="!prop"
          title="尚无选中道具"
          body="选一个道具后可以上传它的参考图。镜头会引用当前版本，所以同一件道具在不同镜头里长得一样。"
        />
        <EmptyState
          v-else-if="world.propReferences.length === 0"
          title="这个道具还没有参考图"
          body="上传一张即可。没有参考图的道具在生成时上下文不完整，概览页的连续性检查会点出来。"
        />
        <div v-else class="grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-2 p-2">
          <figure
            v-for="r in world.propReferences"
            :key="r.id"
            class="border bg-base-2 flex flex-col overflow-hidden"
            :class="r.is_current ? 'border-accent/60' : 'border-line-1'"
          >
            <span class="bg-base-3 flex h-28 items-center justify-center overflow-hidden">
              <img
                v-if="thumb(r.asset_id)"
                :src="thumb(r.asset_id)"
                alt=""
                class="h-full w-full object-cover"
              />
              <span v-else class="text-fg-4 text-2xs">文件不在了</span>
            </span>
            <figcaption class="flex items-center gap-1 px-1.5 py-1">
              <span class="text-fg-2 tnum text-2xs">v{{ r.version_no }}</span>
              <AppBadge v-if="r.is_current" tone="ok">当前</AppBadge>
              <span v-if="r.note" class="text-fg-4 ml-auto truncate text-2xs">{{ r.note }}</span>
            </figcaption>
          </figure>
        </div>
      </AppPanel>

      <!-- 右：属性 + 影响范围 -->
      <AppPanel title="属性" class="w-72 shrink-0">
        <EmptyState
          v-if="!prop"
          title="尚无选中项"
          body="选中道具后可以改它的说明，并看清它出现在多少个镜头里——删之前得先知道会影响什么。"
        />
        <div v-else class="space-y-3 p-2">
          <section>
            <p class="text-fg-3 text-2xs tracking-wide uppercase">道具</p>
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
                  :value="prop[f.key] ?? ''"
                  class="border-line-1 bg-base-2 text-fg-1 mt-px h-5 w-full border px-1.5 text-2xs outline-none focus:border-accent/60"
                  @change="saveField(f.key, ($event.target as HTMLInputElement).value)"
                />
              </label>
            </div>
          </section>

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">影响范围</p>
            <p class="text-fg-2 mt-1 text-2xs">
              出现在 <span class="tnum text-fg-1">{{ prop.shot_count }}</span> 个镜头里，共
              <span class="tnum text-fg-1">{{ prop.reference_count }}</span> 张参考图。
            </p>
            <p class="text-fg-4 mt-0.5 text-2xs">
              {{
                prop.shot_count > 0
                  ? '删掉它，这些镜头的上下文会少一条参考——后端会先拒绝并列出是谁在用。'
                  : '还没有镜头用它，现在删掉不会破坏任何东西。'
              }}
            </p>
            <AppButton
              size="sm"
              variant="danger"
              class="mt-1.5"
              :disabled="world.busy"
              @click="world.removeProp(pid, prop.id)"
            >
              <Trash2 :size="10" />删除道具
            </AppButton>
          </section>
        </div>
      </AppPanel>
    </div>

    <LibraryPickDialog v-model:open="picking" :pid="pid" kind="prop" @adopted="reload()" />
    <input ref="createFileInput" type="file" accept="image/*" class="hidden" @change="onCreateFile" />
    <AppDialog
      :open="createOpen"
      title="新建道具"
      subtitle="必须绑定一张默认参考图"
      size="sm"
      @update:open="createOpen = $event"
    >
      <form id="create-prop" class="space-y-3 p-3" @submit.prevent="createProp()">
        <label class="block">
          <span class="text-fg-3 text-2xs">道具名称</span>
          <input v-model="newName" autofocus placeholder="铜制怀表" class="border-line-1 bg-base-2 text-fg-1 mt-0.5 h-row w-full border px-2 text-xs outline-none" />
        </label>
        <section>
          <div class="flex items-center justify-between">
            <span class="text-fg-3 text-2xs">默认参考图（必选）</span>
            <AppButton size="sm" variant="ghost" :disabled="uploading" @click="createFileInput?.click()">
              <Upload :size="10" />{{ uploading ? '上传中' : '上传图片' }}
            </AppButton>
          </div>
          <div v-if="createAssetId && thumb(createAssetId)" class="border-line-1 bg-base-2 mt-1.5 flex h-32 items-center justify-center overflow-hidden border">
            <img :src="thumb(createAssetId)" alt="默认道具参考图" class="size-full object-contain" />
          </div>
          <p v-else class="text-fg-4 mt-1.5 text-2xs">请上传一张道具参考图后再创建。</p>
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
        <AppButton type="submit" form="create-prop" variant="primary" :disabled="world.busy || uploading || !newName.trim() || !createAssetId">
          <Plus :size="11" />新建道具
        </AppButton>
      </template>
    </AppDialog>
  </div>
</template>
