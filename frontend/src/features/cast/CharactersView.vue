<script setup lang="ts">
/**
 * 角色工作台（Step 2 的前端）。
 *
 * 三件事必须由后端说、前端只管画：
 *   1. **继承**——`Appearance.fields[字段]` 已经带了 value / source / from_name，
 *      前端绝不重算继承链，只按 source 决定正常色 / 浅色 + 来源 / 空占位；
 *   2. **版本只增不改**——Character Sheet 上传即成为新的当前版本，旧版本留在历史里，
 *      所以这里没有任何「改掉某个版本」的入口；
 *   3. **出处不是同步关系**——`origin_library_id` 只说明「当初从素材库采用而来」，
 *      采用之后两边各改各的，标记上的 title 把这句话写出来。
 *
 * 「生成角色表」走第三条生成链（`features/images/GenerateImageDialog.vue`）：结构由内置的
 * 「角色四视图」SKILL 补，用户那段话只写「长什么样」。出图服务没配置时**不在这里判断**——
 * 弹窗里那份只读账单会原样摆出四要素错误与去设置页的路，比一句 tooltip 说得清。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  GitBranch,
  Library,
  Plus,
  RefreshCw,
  Sparkles,
  Star,
  Trash2,
  Undo2,
  Upload,
} from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import LibraryPickDialog from '@/features/library/LibraryPickDialog.vue'
import GenerateImageDialog from '@/features/images/GenerateImageDialog.vue'
import AssetDescription from '@/shared/ui/AssetDescription.vue'
import { fileUrl } from '@/shared/api/files'
import { assetsApi, type Asset } from '@/shared/api/assets'
import {
  CHARACTER_TEXT_FIELDS,
  FIELD_LABEL,
  INHERITABLE,
  type AppearanceRow,
  type InheritableField,
} from '@/shared/api/cast'
import { ApiError } from '@/shared/api/client'
import { useCastStore } from '@/stores/cast'

const route = useRoute()
const cast = useCastStore()

const pid = computed(() => String(route.params.pid ?? ''))

const genOpen = ref(false)

const newName = ref('')
const createOpen = ref(false)
const createAssetId = ref('')
const deriveName = ref('')
const picking = ref(false)
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const createFileInput = ref<HTMLInputElement | null>(null)
/**
 * 工程内资产总账：sheet 行上只有 asset_id，缩略图要靠 path。
 *
 * 刻意**不按 kind 过滤**：资产登记走 sha1 去重，同一个文件先以别的 kind 登记过时
 * 复用原记录（kind 保持首次登记的值），按 character_sheet 过滤会把它漏掉，
 * 于是明明有图却显示「无图」。
 */
const assets = ref<Asset[]>([])
const assetError = ref<ApiError | null>(null)

const assetById = computed(() => new Map(assets.value.map((a) => [a.id, a])))
const imageAssets = computed(() =>
  assets.value.filter(
    (asset) =>
      !asset.missing &&
      (asset.mime?.startsWith('image/') || /\.(png|jpe?g|webp|gif|bmp)$/i.test(asset.path)) &&
      asset.kind !== 'audio',
  ),
)
/**
 * 两个面板都在说同一件事时只留一个。
 *
 * 典型是后端重启后的「项目未打开」：角色列表与资产总账会同时 404，
 * 叠两块一模一样的错误面板只会让人以为出了两个问题。
 */
const showAssetError = computed(
  () => assetError.value !== null && assetError.value.code !== cast.lastError?.code,
)

/** 形象树：父在前、子缩进。后端按 created_at 排，这里只补层级。 */
interface TreeNode {
  row: AppearanceRow
  depth: number
}

const tree = computed<TreeNode[]>(() => {
  const rows = cast.appearances
  const out: TreeNode[] = []
  const walk = (parentId: string | null, depth: number): void => {
    for (const row of rows.filter((r) => r.parent_id === parentId)) {
      out.push({ row, depth })
      walk(row.id, depth + 1)
    }
  }
  walk(null, 0)
  // 父节点被删过的孤儿也要露出来，否则用户会以为形象丢了
  for (const row of rows) if (!out.some((n) => n.row.id === row.id)) out.push({ row, depth: 0 })
  return out
})

const current = computed(() => cast.selectedAppearance)

/**
 * 描述框对着哪一版角色表。默认跟着当前版本走——那一版才是镜头真正引用的那张图。
 *
 * 刻意让**每一版都能选**（而不是只给当前版）：版本只增不改，用户回头把旧版换回当前
 * 之前，通常先想看清「那一版长什么样」。切形象时清空，否则描述框会对着上一个形象的图。
 */
const sheetPick = ref('')
watch(
  () => [current.value?.id, cast.sheets.map((s) => s.id).join(',')] as const,
  () => {
    if (!cast.sheets.some((s) => s.id === sheetPick.value)) sheetPick.value = ''
  },
)
const sheet = computed(
  () =>
    cast.sheets.find((s) => s.id === sheetPick.value) ??
    cast.sheets.find((s) => s.is_current) ??
    cast.sheets[0] ??
    null,
)
/** 这一版挂的那个资产（可能已经不在磁盘上了，那时只有描述可改）。 */
const sheetAsset = computed(() =>
  sheet.value?.asset_id ? (assetById.value.get(sheet.value.asset_id) ?? null) : null,
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
  await Promise.all([cast.load(pid.value).catch(() => {}), loadAssets()])
}

onMounted(reload)
watch(pid, reload)

async function createCharacter(): Promise<void> {
  const name = newName.value.trim()
  if (!name || !createAssetId.value) return
  newName.value = ''
  await cast.create(pid.value, name, createAssetId.value).catch(() => {})
  if (!cast.lastError) {
    createAssetId.value = ''
    createOpen.value = false
  }
}

function openCreate(): void {
  cast.clearError()
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
    const asset = await assetsApi.upload(pid.value, file, 'character_sheet')
    createAssetId.value = asset.id
    await loadAssets()
  } catch (err) {
    assetError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

async function derive(): Promise<void> {
  const parent = current.value
  if (!parent) return
  const name = deriveName.value.trim() || `${parent.name} 派生`
  deriveName.value = ''
  await cast.addAppearance(pid.value, name, parent.id).catch(() => {})
}

async function addRoot(): Promise<void> {
  await cast.addAppearance(pid.value, '新形象', null).catch(() => {})
}

/** 上传一张图 → 登记成 character_sheet 资产 → 挂成新的当前版本。 */
async function onPickFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  const aid = current.value?.id
  if (!file || !aid) return
  uploading.value = true
  try {
    const asset = await assetsApi.upload(pid.value, file, 'character_sheet')
    await cast.addSheet(pid.value, aid, asset.id)
    await loadAssets()
  } catch (err) {
    assetError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

/** 就地改一个可继承字段：写进去即视为「覆写」，恢复继承走 revert。 */
async function saveField(field: InheritableField, value: string): Promise<void> {
  const aid = current.value?.id
  if (!aid) return
  await cast.updateAppearance(pid.value, aid, { [field]: value || null }).catch(() => {})
}

async function saveCharacterField(key: string, value: string): Promise<void> {
  const cid = cast.selectedId
  if (!cid) return
  await cast.update(pid.value, cid, { [key]: value || null }).catch(() => {})
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />

    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1 border-b px-2">
      <AppButton
        size="sm"
        variant="primary"
        :disabled="cast.busy"
        title="新建角色并绑定默认定妆图"
        @click="openCreate()"
      >
        <Plus :size="10" />新建角色
      </AppButton>
      <AppButton size="sm" @click="picking = true"> <Library :size="10" />从素材库采用 </AppButton>
      <!--
        出图服务没配置时不在这里判断：弹窗里那份只读账单会原样摆出四要素错误与
        「去设置页配一个」，比一句 tooltip 说得清。这里只拦「还没选形象」。
      -->
      <AppButton
        size="sm"
        :disabled="!current"
        :title="current ? '照内置的「角色四视图」结构出一张角色表' : '先选一个形象'"
        @click="genOpen = true"
      >
        <Sparkles :size="10" />生成角色表
      </AppButton>
      <AppButton size="sm" variant="ghost" class="ml-auto" :disabled="cast.busy" @click="reload()">
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>

    <ErrorPanel
      v-if="cast.lastError"
      class="mx-2 mt-2"
      :error="cast.lastError"
      @dismiss="cast.clearError()"
    />
    <ErrorPanel
      v-if="showAssetError"
      class="mx-2 mt-2"
      :error="assetError"
      @dismiss="assetError = null"
    />

    <div class="flex min-h-0 flex-1 gap-2 p-2">
      <!-- 左：角色列表 -->
      <AppPanel title="角色" class="w-56 shrink-0">
        <EmptyState
          v-if="cast.characters.length === 0"
          title="还没有角色"
          body="只需一个名字就能建；后端会顺手给它一个「默认形象」，因为没有形象的角色在镜头里无法被引用。"
        />
        <ul v-else class="divide-line-1 divide-y">
          <li v-for="c in cast.characters" :key="c.id">
            <button
              class="hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left"
              :class="c.id === cast.selectedId ? 'bg-accent-dim/40' : ''"
              @click="cast.select(pid, c.id)"
            >
              <span class="min-w-0 flex-1">
                <span class="text-fg-1 block truncate text-xs">{{ c.name }}</span>
                <span class="text-fg-4 block truncate text-2xs">
                  {{ c.appearance_count }} 个形象{{ c.alias ? ` · ${c.alias}` : '' }}
                </span>
              </span>
              <AppBadge
                v-if="c.origin_library_id"
                tone="accent"
                title="当初从素材库采用而来。采用是单向复制：库改了不回流工程，工程改了也不影响库。"
              >
                库
              </AppBadge>
            </button>
          </li>
        </ul>
      </AppPanel>

      <!-- 中：形象树 + 角色表 -->
      <div class="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
        <AppPanel title="形象" class="h-44 shrink-0">
          <template #actions>
            <input
              v-model="deriveName"
              placeholder="派生形象名，例如 战损"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 h-5 w-36 border px-1.5 text-2xs outline-none focus:border-accent/60"
              @keyup.enter="derive()"
            />
            <AppButton
              size="sm"
              :disabled="!current || cast.busy"
              title="基于选中形象派生：未填的字段自动继承，改哪个字段哪个才算覆写"
              @click="derive()"
            >
              <GitBranch :size="10" />派生
            </AppButton>
            <AppButton size="sm" variant="ghost" :disabled="!cast.selectedId" @click="addRoot()">
              <Plus :size="10" />根形象
            </AppButton>
          </template>
          <EmptyState
            v-if="!cast.selectedId"
            title="先在左边选一个角色"
            body="一个角色可以有多个形象（少年 / 成年 / 战损），派生出来的形象默认继承父形象的特征。"
          />
          <ul v-else class="divide-line-1 divide-y">
            <li v-for="node in tree" :key="node.row.id">
              <div
                class="hover:bg-base-2 flex items-center gap-1.5 px-2 py-1"
                :class="node.row.id === cast.selectedAppearanceId ? 'bg-accent-dim/40' : ''"
              >
                <button
                  class="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                  :style="{ paddingLeft: `${node.depth * 12}px` }"
                  @click="cast.selectAppearance(pid, node.row.id)"
                >
                  <span
                    class="border-line-1 bg-base-2 flex h-7 w-7 shrink-0 items-center justify-center overflow-hidden border"
                  >
                    <img
                      v-if="thumb(node.row.current_sheet?.asset_id)"
                      :src="thumb(node.row.current_sheet?.asset_id)"
                      alt=""
                      class="h-full w-full object-cover"
                    />
                    <span v-else class="text-fg-4 text-2xs">无图</span>
                  </span>
                  <span class="text-fg-1 min-w-0 truncate text-xs">{{ node.row.name }}</span>
                  <AppBadge v-if="node.row.is_default" tone="ok">默认</AppBadge>
                  <AppBadge v-if="node.row.parent_id" tone="neutral">
                    覆写 {{ node.row.overrides.length }}
                  </AppBadge>
                  <span class="text-fg-4 text-2xs">{{ node.row.sheet_count }} 版</span>
                </button>
                <button
                  v-if="!node.row.is_default"
                  class="text-fg-4 hover:text-st-done shrink-0"
                  title="设为默认形象（镜头里不指定时用它）"
                  @click="cast.setDefaultAppearance(pid, node.row.id)"
                >
                  <Star :size="11" />
                </button>
                <button
                  class="text-fg-4 hover:text-st-failed shrink-0"
                  title="删除这个形象"
                  @click="cast.removeAppearance(pid, node.row.id)"
                >
                  <Trash2 :size="11" />
                </button>
              </div>
            </li>
          </ul>
        </AppPanel>

        <AppPanel title="Character Sheet 版本" class="min-h-0 flex-1" :scroll="false">
          <template #actions>
            <span class="text-fg-4 text-2xs">新版本自动成为当前，旧版本永不覆盖</span>
            <AppButton
              size="sm"
              variant="primary"
              :disabled="!current || uploading"
              @click="fileInput?.click()"
            >
              <Upload :size="10" />{{ uploading ? '上传中…' : '上传角色表' }}
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
            v-if="!current"
            title="尚无选中形象"
            body="选一个形象后可以上传它的多视角角色表。之后镜头会自动引用当前版本，人物不会走形。"
          />
          <EmptyState
            v-else-if="cast.sheets.length === 0"
            title="这个形象还没有角色表"
            body="上传一张多视角参考图即可。没有角色表的形象在生成时上下文不完整，概览页的连续性检查会点出来。"
          />
          <div v-else class="flex h-full min-h-0 flex-col">
            <div
              class="grid min-h-0 flex-1 grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] content-start gap-2 overflow-auto p-2"
            >
              <figure
                v-for="s in cast.sheets"
                :key="s.id"
                class="border bg-base-2 flex cursor-pointer flex-col overflow-hidden"
                :class="
                  s.id === sheet?.id
                    ? 'border-accent'
                    : s.is_current
                      ? 'border-accent/60'
                      : 'border-line-1'
                "
                :title="'点一下：在下面给这一版写描述'"
                @click="sheetPick = s.id"
              >
                <span class="bg-base-3 flex h-24 items-center justify-center overflow-hidden">
                  <img
                    v-if="thumb(s.asset_id)"
                    :src="thumb(s.asset_id)"
                    alt=""
                    class="h-full w-full object-cover"
                  />
                  <span v-else class="text-fg-4 text-2xs">占位版本（还没有图）</span>
                </span>
                <figcaption class="flex items-center gap-1 px-1.5 py-1">
                  <span class="text-fg-2 tnum text-2xs">v{{ s.version_no }}</span>
                  <AppBadge v-if="s.is_current" tone="ok">当前</AppBadge>
                  <!-- 描述空着 = 模型引用这张图时只看到一个文件名，所以标出来 -->
                  <AppBadge
                    v-if="s.asset_id && !assetById.get(s.asset_id)?.description"
                    tone="warn"
                    title="这一版还没有描述：模型引用它时只看到一个文件名"
                  >
                    缺描述
                  </AppBadge>
                  <span class="text-fg-4 ml-auto truncate text-2xs">{{ s.source }}</span>
                </figcaption>
              </figure>
            </div>
            <!-- 描述固定在底部：它属于「上面选中的那一版」，跟着网格滚走会看不见 -->
            <div v-if="sheet" class="border-line-1 shrink-0 border-t p-2">
              <p class="text-fg-3 mb-1 text-2xs tracking-wide uppercase">
                v{{ sheet.version_no }} 这张图的描述
              </p>
              <AssetDescription
                v-if="sheet.asset_id"
                :key="sheet.asset_id"
                :pid="pid"
                :asset-id="sheet.asset_id"
                :description="sheetAsset?.description ?? null"
                @saved="loadAssets()"
              />
              <p v-else class="text-fg-4 text-2xs">
                这一版没有挂图（占位版本），没有可写描述的对象。
              </p>
            </div>
          </div>
        </AppPanel>
      </div>

      <!-- 右：属性检查器 -->
      <AppPanel title="属性" class="w-72 shrink-0">
        <EmptyState
          v-if="!cast.selected"
          title="尚无选中项"
          body="选中角色后可以改它的基础设定；选中形象后可以改外观特征，并看清每个值是自己填的还是继承来的。"
        />
        <div v-else class="space-y-3 p-2">
          <section>
            <p class="text-fg-3 text-2xs tracking-wide uppercase">角色</p>
            <div class="mt-1 space-y-1">
              <label v-for="f in CHARACTER_TEXT_FIELDS" :key="String(f.key)" class="block">
                <span class="text-fg-4 text-2xs">{{ f.label }}</span>
                <input
                  :value="
                    (cast.selected as unknown as Record<string, string | null>)[String(f.key)] ?? ''
                  "
                  class="border-line-1 bg-base-2 text-fg-1 mt-px h-5 w-full border px-1.5 text-2xs outline-none focus:border-accent/60"
                  @change="
                    saveCharacterField(String(f.key), ($event.target as HTMLInputElement).value)
                  "
                />
              </label>
            </div>
            <AppButton
              size="sm"
              variant="danger"
              class="mt-1.5"
              :disabled="cast.busy"
              @click="cast.remove(pid, cast.selectedId)"
            >
              <Trash2 :size="10" />删除角色
            </AppButton>
          </section>

          <section v-if="current" class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">形象 · {{ current.name }}</p>
            <p v-if="current.parent_id" class="text-fg-4 mt-0.5 text-2xs">
              派生形象：留空的字段继续继承父形象，填了就变成覆写。
            </p>
            <div class="mt-1 space-y-1.5">
              <div v-for="f in INHERITABLE" :key="f">
                <div class="flex items-center gap-1">
                  <span class="text-fg-4 text-2xs">{{ FIELD_LABEL[f] }}</span>
                  <AppBadge
                    v-if="current.fields[f].source === 'inherited'"
                    tone="neutral"
                    :title="`继承自「${current.fields[f].from_name}」`"
                  >
                    继承
                  </AppBadge>
                  <AppBadge v-else-if="current.fields[f].overridden" tone="warn">已覆写</AppBadge>
                  <button
                    v-if="current.fields[f].overridden"
                    class="text-fg-4 hover:text-accent ml-auto"
                    title="恢复继承：清掉本地值，重新由父形象决定"
                    @click="cast.revertField(pid, current.id, f)"
                  >
                    <Undo2 :size="11" />
                  </button>
                </div>
                <input
                  :value="current.fields[f].value ?? ''"
                  :placeholder="current.fields[f].source === 'empty' ? '整条链上都还没人填' : ''"
                  class="border-line-1 bg-base-2 mt-px h-5 w-full border px-1.5 text-2xs outline-none focus:border-accent/60"
                  :class="current.fields[f].source === 'inherited' ? 'text-fg-4' : 'text-fg-1'"
                  @change="saveField(f, ($event.target as HTMLInputElement).value)"
                />
              </div>
            </div>
          </section>
        </div>
      </AppPanel>
    </div>

    <LibraryPickDialog v-model:open="picking" :pid="pid" kind="character" @adopted="reload()" />
    <!--
      入队之后图要等队列跑完才落进来，所以这里只重拉一次列表（「多了一个任务」交给底部
      控制台说），不假装角色表已经有了。
    -->
    <GenerateImageDialog
      v-if="current"
      v-model:open="genOpen"
      :pid="pid"
      target-kind="appearance"
      :target-id="current.id"
      :what="`角色形象 · ${current.name}`"
      @queued="reload()"
    />

    <input
      ref="createFileInput"
      type="file"
      accept="image/*"
      class="hidden"
      @change="onCreateFile"
    />
    <AppDialog
      :open="createOpen"
      title="新建角色"
      subtitle="必须绑定一张默认定妆图"
      size="sm"
      @update:open="createOpen = $event"
    >
      <form id="create-character" class="space-y-3 p-3" @submit.prevent="createCharacter()">
        <label class="block">
          <span class="text-fg-3 text-2xs">角色名称</span>
          <input
            v-model="newName"
            autofocus
            placeholder="林昭"
            class="border-line-1 bg-base-2 text-fg-1 mt-0.5 h-row w-full border px-2 text-xs outline-none"
          />
        </label>
        <section>
          <div class="flex items-center justify-between">
            <span class="text-fg-3 text-2xs">默认定妆图（必选）</span>
            <AppButton
              size="sm"
              variant="ghost"
              :disabled="uploading"
              @click="createFileInput?.click()"
            >
              <Upload :size="10" />{{ uploading ? '上传中' : '上传图片' }}
            </AppButton>
          </div>
          <div
            v-if="createAssetId && thumb(createAssetId)"
            class="border-line-1 bg-base-2 mt-1.5 flex h-32 items-center justify-center overflow-hidden border"
          >
            <img :src="thumb(createAssetId)" alt="默认定妆图" class="size-full object-contain" />
          </div>
          <p v-else class="text-fg-4 mt-1.5 text-2xs">请上传一张角色定妆图后再创建。</p>
          <div
            v-if="imageAssets.length"
            class="mt-2 grid max-h-40 grid-cols-4 gap-1.5 overflow-auto"
          >
            <button
              v-for="asset in imageAssets"
              :key="asset.id"
              type="button"
              class="bg-base-2 aspect-square overflow-hidden border"
              :class="
                createAssetId === asset.id
                  ? 'border-accent/70 ring-1 ring-accent/30'
                  : 'border-line-1'
              "
              title="使用项目中的这张图片"
              @click="createAssetId = asset.id"
            >
              <img :src="fileUrl(pid, asset.path)" alt="" class="size-full object-cover" />
            </button>
          </div>
        </section>
      </form>
      <template #footer>
        <span class="flex-1" />
        <AppButton variant="ghost" @click="createOpen = false">取消</AppButton>
        <AppButton
          type="submit"
          form="create-character"
          variant="primary"
          :disabled="cast.busy || uploading || !newName.trim() || !createAssetId"
        >
          <Plus :size="11" />新建角色
        </AppButton>
      </template>
    </AppDialog>
  </div>
</template>
