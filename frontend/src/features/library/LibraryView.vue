<script setup lang="ts">
/**
 * 素材库页（Phase 4）。
 *
 * 素材库是应用级的：一个目录 + 一份 library.db，与任何工程都无关。三条要求：
 *   1. 没配置不是错误——画引导让用户选目录（DirPicker，与新建工程共用一套）；
 *   2. 采用必须先出账单再动手（AdoptDialog 负责），这里只管选中哪一条；
 *   3. 「采用是单向复制」写在页面上，别让用户以为库和工程会互相同步。
 *
 * 版式：一次性动作（建预设 / 建标签 / 建变体 / 挂图）全部收进弹窗，页面只留
 * 「库里有什么」。原来这些输入框和下拉常驻在每一行上，一个五个角色的库要显示
 * 十几个空控件，真正要看的名字和缩略图反而被挤没了。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { FolderSearch, ImagePlus, Plus, RefreshCw, Tag, Trash2, Upload } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import DirPicker from '@/shared/ui/DirPicker.vue'
import AdoptDialog from './AdoptDialog.vue'
import { libraryFileUrl } from '@/shared/api/files'
import {
  humanBytes,
  LIBRARY_KIND_LABEL,
  type AdoptKind,
  type AdoptResult,
  type LibraryKind,
} from '@/shared/api/library'
import { useLibraryStore } from '@/stores/library'
import { useProjectStore } from '@/stores/project'

const lib = useLibraryStore()
const proj = useProjectStore()
const router = useRouter()

type Tab = 'assets' | 'characters' | 'locations' | 'props'
type PresetKind = 'character' | 'location' | 'prop'
type AttachTarget = 'appearance' | 'variant' | 'prop'

const TABS: { key: Tab; label: string }[] = [
  { key: 'assets', label: '素材' },
  { key: 'characters', label: '角色' },
  { key: 'locations', label: '地点' },
  { key: 'props', label: '道具' },
]
const KINDS = Object.keys(LIBRARY_KIND_LABEL) as LibraryKind[]
const PRESET_LABEL: Record<PresetKind, string> = {
  character: '角色',
  location: '地点',
  prop: '道具',
}
/** 挂图时该从库里的哪一类素材里挑。 */
const ATTACH_KIND: Record<AttachTarget, LibraryKind> = {
  appearance: 'character_sheet',
  variant: 'location_reference',
  prop: 'prop_reference',
}
const ATTACH_LABEL: Record<AttachTarget, string> = {
  appearance: '定妆图',
  variant: '参考图',
  prop: '参考图',
}

const tab = ref<Tab>('assets')
const picking = ref(false)
const kindFilter = ref<'' | LibraryKind>('')
const tagFilter = ref('')
const uploadKind = ref<LibraryKind>('upload')
const fileInput = ref<HTMLInputElement | null>(null)
const presetFileInput = ref<HTMLInputElement | null>(null)
const adopting = ref<{ kind: AdoptKind; id: string } | null>(null)
const adopted = ref<AdoptResult | null>(null)
/** 删素材被引用时后端会拒；记下这一条，错误面板才能给「仍然删除」。 */
const deleting = ref('')

/** 弹窗：新建预设 / 新建标签 / 新建变体 / 挂图。同时只会开一个。 */
const presetKind = ref<'' | PresetKind>('')
const presetName = ref('')
const presetDefaultAssetId = ref('')
const tagging = ref(false)
const newTag = ref('')
const variantFor = ref<{ id: string; name: string } | null>(null)
const variantName = ref('')
const attaching = ref<{ target: AttachTarget; id: string; title: string } | null>(null)
const attachPick = ref('')

const dialogOpen = computed(
  () =>
    presetKind.value !== '' ||
    tagging.value ||
    variantFor.value !== null ||
    attaching.value !== null,
)
/** 弹窗开着时错误显示在弹窗里（就地重试），否则显示在页面上。同一条只画一次。 */
const pageError = computed(() => (dialogOpen.value ? null : lib.lastError))

const pid = computed(() => proj.current?.id ?? '')
/**
 * 采用按钮的说明。素材库是**应用级**页面，左栏里只有没打开工程时才有它；
 * 这里还能采用，是因为「工程还开着，只是人跑到库里来了」。
 * 真没打开工程时别只说「先打开一个工程」——要说清去哪儿打开，
 * 以及工程内本来就有更近的入口（各页的「从素材库采用」）。
 */
const adoptHint = computed(() =>
  pid.value
    ? `采用到《${proj.current?.name ?? pid.value}》· 单向复制，之后两边各改各的`
    : '先回「项目」页打开一个工程。打开之后，工程内各页的「从素材库采用」也能直接取库里的东西，不必来这儿',
)
const assetById = computed(() => new Map(lib.assets.map((a) => [a.id, a])))

const visibleAssets = computed(() =>
  lib.assets.filter(
    (a) =>
      (kindFilter.value === '' || a.kind === kindFilter.value) &&
      (tagFilter.value === '' || a.tags.some((t) => t.name === tagFilter.value)),
  ),
)

/** 当前 tab 对应的预设类型；素材 tab 没有预设可建。 */
const tabPreset = computed<PresetKind | ''>(() =>
  tab.value === 'characters'
    ? 'character'
    : tab.value === 'locations'
      ? 'location'
      : tab.value === 'props'
        ? 'prop'
        : '',
)

const presetAssetKind = computed<LibraryKind>(() =>
  presetKind.value === 'character'
    ? 'character_sheet'
    : presetKind.value === 'location'
      ? 'location_reference'
      : 'prop_reference',
)

function kindLabel(kind: string): string {
  return LIBRARY_KIND_LABEL[kind as LibraryKind] ?? kind
}

/** 挂图的候选：库里还在的图，本类型优先，「其它上传」也允许。 */
function pickable(kind: LibraryKind) {
  return lib.assets.filter((a) => !a.missing && (a.kind === kind || a.kind === 'upload'))
}

function thumbOf(assetId: string | null | undefined): string {
  const row = assetId ? assetById.value.get(assetId) : undefined
  return row && !row.missing ? libraryFileUrl(row.path) : ''
}

function askAdopt(kind: AdoptKind, id: string): void {
  adopted.value = null
  adopting.value = { kind, id }
}

function onAdopted(out: AdoptResult): void {
  adopted.value = out
  adopting.value = null
}

async function onFiles(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  for (const f of files) {
    try {
      await lib.upload(f, uploadKind.value)
    } catch {
      break // 错误已进 lastError，别让剩下的文件再撞一次同一面墙
    }
  }
}

async function onPresetFile(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || !presetKind.value) return
  try {
    const asset = await lib.upload(file, presetAssetKind.value, file.name)
    presetDefaultAssetId.value = asset.id
  } catch {
    /* 错误已由 store 记入 lastError，弹窗内错误面板负责展示。 */
  }
}

/** 下面四个开窗函数都先清错：一进来就看到上一次的红字会让人以为这次就失败了。 */
function startPreset(kind: PresetKind): void {
  lib.clearError()
  presetName.value = ''
  presetDefaultAssetId.value = ''
  presetKind.value = kind
}

function startTag(): void {
  lib.clearError()
  newTag.value = ''
  tagging.value = true
}

function startVariant(id: string, name: string): void {
  lib.clearError()
  variantName.value = ''
  variantFor.value = { id, name }
}

function startAttach(target: AttachTarget, id: string, title: string): void {
  lib.clearError()
  attachPick.value = ''
  attaching.value = { target, id, title }
}

async function addPreset(): Promise<void> {
  const kind = presetKind.value
  const name = presetName.value.trim()
  if (!kind || !name || !presetDefaultAssetId.value) return
  try {
    await lib.createPreset(kind, name, presetDefaultAssetId.value)
    presetKind.value = ''
  } catch {
    /* 错误已由 store 记入 lastError，弹窗内的错误面板负责展示 */
  }
}

async function addVariant(): Promise<void> {
  const target = variantFor.value
  const name = variantName.value.trim()
  if (!target || !name) return
  try {
    await lib.createVariant(target.id, name)
    variantFor.value = null
  } catch {
    /* 同上 */
  }
}

async function addTag(): Promise<void> {
  const name = newTag.value.trim()
  if (!name) return
  try {
    await lib.createTag(name)
    newTag.value = ''
  } catch {
    /* 同上；标签框不关，通常要连着建好几个 */
  }
}

async function tagAsset(aid: string, ev: Event): Promise<void> {
  const select = ev.target as HTMLSelectElement
  const tid = select.value
  select.value = ''
  if (!tid) return
  try {
    await lib.tagAsset(tid, aid)
  } catch {
    /* 同上 */
  }
}

async function attach(): Promise<void> {
  const target = attaching.value
  if (!target || !attachPick.value) return
  try {
    await lib.attachReference({ kind: target.target, id: target.id }, attachPick.value)
    attaching.value = null
  } catch {
    /* 同上 */
  }
}

/** 先按「不强制」删：被预设占用时后端会拒并说清破坏什么，再由用户决定要不要硬删。 */
async function removeAsset(aid: string, force = false): Promise<void> {
  deleting.value = aid
  try {
    await lib.deleteAsset(aid, force)
    deleting.value = ''
  } catch {
    /* 错误面板负责展示，并在 CONFLICT 时给出「仍然删除」 */
  }
}

async function configure(dir: string): Promise<void> {
  try {
    await lib.configure(dir)
  } catch {
    /* 同上 */
  }
}

onMounted(() => void lib.refresh())
</script>

<template>
  <div class="min-h-0 flex-1 overflow-auto p-2">
    <section class="border-line-1 bg-base-1 border p-4">
      <div class="flex items-start gap-2">
        <div class="min-w-0 flex-1">
          <h1 class="text-fg-1 text-base font-medium">素材库</h1>
          <p class="text-fg-2 mt-1 text-xs">
            库是应用级的一个目录，跨项目复用：素材文件 + 角色 / 地点 / 道具预设。
          </p>
          <p class="text-fg-4 mt-1 text-2xs">
            采用是单向复制：文件会进工程目录，之后库里改了不回流工程，工程里改了也不影响库。
          </p>
        </div>
        <AppButton v-if="lib.configured" variant="ghost" @click="lib.refresh()">
          <RefreshCw :size="12" />刷新
        </AppButton>
        <AppButton :variant="lib.configured ? 'default' : 'primary'" @click="picking = true">
          <FolderSearch :size="12" />{{ lib.configured ? '换个目录' : '选择素材库目录' }}
        </AppButton>
      </div>
    </section>

    <ErrorPanel v-if="pageError" class="mt-2" :error="pageError" @dismiss="lib.clearError()">
      <!-- 被预设占用的素材：说清破坏什么之后，才给硬删这条路 -->
      <template #actions>
        <AppButton
          v-if="pageError.code === 'CONFLICT' && deleting && pageError.relatedIds.protected_default !== true"
          size="sm"
          variant="danger"
          @click="removeAsset(deleting, true)"
        >
          仍然删除，并解除这些引用
        </AppButton>
      </template>
    </ErrorPanel>

    <!-- 没配置不是错误：画引导，别自动瞎建目录 -->
    <AppPanel v-if="!lib.configured" title="还没有素材库" class="mt-2">
      <EmptyState
        title="选一个文件夹当素材库"
        body="库目录里会生成 library.aivs.json 与 library.db，素材文件按类型放进 assets/。目录里若已有别人的库会被拒绝，一个字节都不会被覆盖。"
      >
        <AppButton variant="primary" @click="picking = true">
          <FolderSearch :size="12" />选择素材库目录
        </AppButton>
      </EmptyState>
      <p v-if="lib.status?.remembered_dir" class="text-fg-4 px-3 pb-3 font-mono text-2xs">
        上次用的是 {{ lib.status.remembered_dir }}
      </p>
    </AppPanel>

    <template v-else>
      <AppPanel :title="lib.info?.name ?? '当前素材库'" class="mt-2">
        <template #actions>
          <AppButton
            size="sm"
            variant="ghost"
            title="只忘掉位置，库文件与内容都还在，重新选回来就恢复"
            @click="lib.close()"
          >
            不再使用
          </AppButton>
        </template>
        <div class="space-y-1 p-3 text-xs">
          <p class="text-fg-2 truncate font-mono text-2xs">{{ lib.info?.dir }}</p>
          <div class="flex flex-wrap items-center gap-1">
            <AppBadge>素材 {{ lib.info?.counts.assets ?? 0 }}</AppBadge>
            <AppBadge>角色 {{ lib.info?.counts.characters ?? 0 }}</AppBadge>
            <AppBadge>地点 {{ lib.info?.counts.locations ?? 0 }}</AppBadge>
            <AppBadge>道具 {{ lib.info?.counts.props ?? 0 }}</AppBadge>
            <AppBadge tone="accent">schema {{ lib.info?.schema_version ?? 0 }}</AppBadge>
          </div>
          <p v-if="pid" class="text-fg-3 text-2xs">
            采用目标：{{ proj.current?.name }} — {{ proj.current?.dir }}
          </p>
          <p v-else class="text-st-review flex items-center gap-1.5 text-2xs">
            没有打开的工程，「采用」不可用。
            <AppButton size="sm" variant="ghost" @click="router.push('/')">去项目管理</AppButton>
          </p>
        </div>
      </AppPanel>

      <p v-if="adopted" class="border-line-1 bg-base-1 text-fg-2 mt-2 border px-3 py-2 text-2xs">
        已采用「{{ adopted.name }}」：复制 {{ adopted.copied }} 个文件，复用
        {{ adopted.reused }} 个。{{ adopted.one_way }}
      </p>

      <div class="mt-2 flex items-center gap-1">
        <button
          v-for="t in TABS"
          :key="t.key"
          type="button"
          class="border-line-1 h-row border px-3 text-xs"
          :class="tab === t.key ? 'bg-base-3 text-fg-1' : 'bg-base-1 text-fg-3 hover:text-fg-1'"
          @click="tab = t.key"
        >
          {{ t.label }}
        </button>
        <AppButton
          v-if="tabPreset"
          size="sm"
          variant="primary"
          class="ml-1"
          @click="startPreset(tabPreset)"
        >
          <Plus :size="10" />新建{{ PRESET_LABEL[tabPreset] }}
        </AppButton>
      </div>
      <AppPanel v-if="tab === 'assets'" title="素材" class="mt-2">
        <template #actions>
          <select
            v-model="kindFilter"
            class="border-line-1 bg-base-2 text-fg-2 h-5 rounded-sm border px-1 text-2xs outline-none"
          >
            <option value="">全部类型</option>
            <option v-for="k in KINDS" :key="k" :value="k">{{ LIBRARY_KIND_LABEL[k] }}</option>
          </select>
          <select
            v-model="tagFilter"
            class="border-line-1 bg-base-2 text-fg-2 h-5 rounded-sm border px-1 text-2xs outline-none"
          >
            <option value="">全部标签</option>
            <option v-for="t in lib.tags" :key="t.id" :value="t.name">{{ t.name }}</option>
          </select>
          <AppButton size="sm" variant="ghost" title="管理标签" @click="startTag()">
            <Tag :size="10" />标签
          </AppButton>
          <select
            v-model="uploadKind"
            title="上传为哪一类素材"
            class="border-line-1 bg-base-2 text-fg-2 h-5 rounded-sm border px-1 text-2xs outline-none"
          >
            <option v-for="k in KINDS" :key="k" :value="k">{{ LIBRARY_KIND_LABEL[k] }}</option>
          </select>
          <AppButton size="sm" :disabled="lib.busy" @click="fileInput?.click()">
            <Upload :size="10" />上传
          </AppButton>
        </template>

        <EmptyState
          v-if="visibleAssets.length === 0"
          title="库里还没有素材"
          body="上传角色表、场景参考、道具图或音频。同一份文件传两次只会留一份，靠内容去重。"
        />
        <div
          v-else
          class="grid gap-2 p-3 [grid-template-columns:repeat(auto-fill,minmax(9rem,1fr))]"
        >
          <figure v-for="a in visibleAssets" :key="a.id" class="border-line-1 bg-base-2 border">
            <div class="bg-base-3 flex aspect-square items-center justify-center overflow-hidden">
              <img
                v-if="!a.missing && a.kind !== 'audio'"
                :src="libraryFileUrl(a.path)"
                :alt="a.title ?? a.path"
                loading="lazy"
                class="size-full object-cover"
              />
              <span v-else class="text-fg-4 px-1 text-center text-2xs">
                {{ a.missing ? '文件不见了' : '音频' }}
              </span>
            </div>
            <figcaption class="space-y-1 p-1.5">
              <p class="text-fg-1 truncate text-2xs" :title="a.path">{{ a.title || a.path }}</p>
              <p class="text-fg-4 text-2xs">
                <span class="tnum">{{ humanBytes(a.size_bytes) }}</span> · {{ kindLabel(a.kind) }}
              </p>
              <div class="flex flex-wrap items-center gap-1">
                <AppBadge v-for="t in a.tags" :key="t.id" tone="accent">{{ t.name }}</AppBadge>
                <AppBadge v-if="a.ref_count > 0" tone="ok">被 {{ a.ref_count }} 处使用</AppBadge>
              </div>
              <div class="flex items-center gap-1">
                <AppButton
                  size="sm"
                  :disabled="!pid || a.missing"
                  :title="a.missing ? '这个文件在库目录里找不到了' : adoptHint"
                  @click="askAdopt('asset', a.id)"
                >
                  采用
                </AppButton>
                <select
                  v-if="lib.tags.length"
                  title="挂一个标签"
                  class="border-line-1 bg-base-1 text-fg-3 h-5 rounded-sm border px-1 text-2xs outline-none"
                  @change="tagAsset(a.id, $event)"
                >
                  <option value="">＋标签</option>
                  <option v-for="t in lib.tags" :key="t.id" :value="t.id">{{ t.name }}</option>
                </select>
                <AppButton size="sm" variant="ghost" title="从库里删除" @click="removeAsset(a.id)">
                  <Trash2 :size="10" />
                </AppButton>
              </div>
            </figcaption>
          </figure>
        </div>
      </AppPanel>

      <AppPanel v-else-if="tab === 'characters'" title="角色预设" class="mt-2">
        <EmptyState
          v-if="lib.characters.length === 0"
          title="库里还没有角色"
          body="角色预设连形象链与定妆图一起被采用；进了工程就是可再改的副本，改它不影响库。"
        />
        <ul v-else class="divide-line-1 divide-y">
          <li v-for="c in lib.characters" :key="c.id" class="p-3 text-xs">
            <div class="flex items-center gap-1.5">
              <span class="text-fg-1 min-w-0 flex-1 truncate">{{ c.name }}</span>
              <AppBadge v-for="t in c.tags" :key="t.id" tone="accent">{{ t.name }}</AppBadge>
              <AppBadge>{{ c.appearances.length }} 个形象</AppBadge>
              <AppButton
                size="sm"
                :disabled="!pid"
                :title="adoptHint"
                @click="askAdopt('character', c.id)"
              >
                采用
              </AppButton>
              <AppButton size="sm" variant="ghost" @click="lib.deletePreset('character', c.id)">
                <Trash2 :size="10" />
              </AppButton>
            </div>
            <ul class="mt-1.5 space-y-1">
              <li
                v-for="a in c.appearances"
                :key="a.id"
                class="border-line-1 bg-base-2 flex items-center gap-1.5 border px-2 py-1"
              >
                <img
                  v-if="thumbOf(a.current_sheet?.asset_id)"
                  :src="thumbOf(a.current_sheet?.asset_id)"
                  alt=""
                  class="border-line-1 size-8 shrink-0 border object-cover"
                />
                <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">{{ a.name }}</span>
                <AppBadge v-if="a.is_default">默认</AppBadge>
                <AppBadge v-if="a.overrides.length" tone="warn">
                  覆写 {{ a.overrides.join(' / ') }}
                </AppBadge>
                <span class="text-fg-4 text-2xs">定妆图 {{ a.sheet_count }}</span>
                <AppButton
                  size="sm"
                  @click="startAttach('appearance', a.id, `${c.name} · ${a.name}`)"
                >
                  <ImagePlus :size="10" />修改定妆图
                </AppButton>
                <div v-if="a.sheets.length > 1" class="flex items-center gap-1">
                  <button v-for="sheet in a.sheets" :key="sheet.id" type="button" class="relative" :title="sheet.is_current ? '当前定妆图' : '历史定妆图'">
                    <img v-if="thumbOf(sheet.asset_id)" :src="thumbOf(sheet.asset_id)" alt="" class="size-6 border border-line-1 object-cover" />
                    <span v-if="sheet.is_current" class="absolute right-0 bottom-0 size-1.5 bg-accent" />
                  </button>
                </div>
                <AppButton v-for="sheet in a.sheets.filter((item) => !item.is_current)" :key="`delete-${sheet.id}`" size="sm" variant="ghost" title="删除历史定妆图" @click="lib.deleteReference({ kind: 'sheet', id: sheet.id })">
                  <Trash2 :size="10" />
                </AppButton>
              </li>
            </ul>
          </li>
        </ul>
      </AppPanel>

      <AppPanel v-else-if="tab === 'locations'" title="地点预设" class="mt-2">
        <EmptyState
          v-if="lib.locations.length === 0"
          title="库里还没有地点"
          body="参考图挂在变体上（雨夜 / 白天各一套），采用时变体与参考图一起进工程。"
        />
        <ul v-else class="divide-line-1 divide-y">
          <li v-for="l in lib.locations" :key="l.id" class="p-3 text-xs">
            <div class="flex items-center gap-1.5">
              <span class="text-fg-1 min-w-0 flex-1 truncate">{{ l.name }}</span>
              <AppBadge v-for="t in l.tags" :key="t.id" tone="accent">{{ t.name }}</AppBadge>
              <AppBadge>{{ l.variants.length }} 个变体</AppBadge>
              <AppButton size="sm" @click="startVariant(l.id, l.name)">
                <Plus :size="10" />变体
              </AppButton>
              <AppButton
                size="sm"
                :disabled="!pid"
                :title="adoptHint"
                @click="askAdopt('location', l.id)"
              >
                采用
              </AppButton>
              <AppButton size="sm" variant="ghost" @click="lib.deletePreset('location', l.id)">
                <Trash2 :size="10" />
              </AppButton>
            </div>
            <p v-if="l.description" class="text-fg-4 mt-0.5 text-2xs">{{ l.description }}</p>
            <p v-if="l.variants.length === 0" class="text-fg-4 mt-1 text-2xs">
              还没有变体。参考图挂在变体上，所以至少要有一个（雨夜 / 白天…）。
            </p>
            <ul v-else class="mt-1.5 space-y-1">
              <li
                v-for="v in l.variants"
                :key="v.id"
                class="border-line-1 bg-base-2 flex items-center gap-1.5 border px-2 py-1"
              >
                <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">{{ v.name }}</span>
                <AppBadge v-if="v.weather">{{ v.weather }}</AppBadge>
                <AppBadge v-if="v.time_of_day">{{ v.time_of_day }}</AppBadge>
                <span class="text-fg-4 text-2xs">参考图 {{ v.reference_count }}</span>
                <AppBadge v-if="v.name === '默认场景'" tone="ok">默认</AppBadge>
                <AppButton size="sm" @click="startAttach('variant', v.id, `${l.name} · ${v.name}`)">
                  <ImagePlus :size="10" />修改参考图
                </AppButton>
                <AppButton v-for="reference in v.references.filter((item) => !item.is_current)" :key="`delete-${reference.id}`" size="sm" variant="ghost" title="删除历史参考图" @click="lib.deleteReference({ kind: 'variant', id: reference.id })">
                  <Trash2 :size="10" />
                </AppButton>
              </li>
            </ul>
          </li>
        </ul>
      </AppPanel>

      <AppPanel v-else title="道具预设" class="mt-2">
        <EmptyState
          v-if="lib.props.length === 0"
          title="库里还没有道具"
          body="道具图跨片子最容易复用：一把油纸伞在哪部戏里都是那把伞。"
        />
        <ul v-else class="divide-line-1 divide-y">
          <li v-for="p in lib.props" :key="p.id" class="flex items-center gap-1.5 p-3 text-xs">
            <img
              v-if="thumbOf(p.current_reference?.asset_id)"
              :src="thumbOf(p.current_reference?.asset_id)"
              alt=""
              class="border-line-1 size-8 shrink-0 border object-cover"
            />
            <span class="text-fg-1 min-w-0 flex-1 truncate">{{ p.name }}</span>
            <AppBadge v-for="t in p.tags" :key="t.id" tone="accent">{{ t.name }}</AppBadge>
            <span class="text-fg-4 text-2xs">参考图 {{ p.reference_count }}</span>
            <AppButton size="sm" @click="startAttach('prop', p.id, p.name)">
              <ImagePlus :size="10" />修改参考图
            </AppButton>
            <AppButton v-for="reference in p.references.filter((item) => !item.is_current)" :key="`delete-${reference.id}`" size="sm" variant="ghost" title="删除历史参考图" @click="lib.deleteReference({ kind: 'prop', id: reference.id })">
              <Trash2 :size="10" />
            </AppButton>
            <AppButton
              size="sm"
              :disabled="!pid"
              :title="adoptHint"
              @click="askAdopt('prop', p.id)"
            >
              采用
            </AppButton>
            <AppButton size="sm" variant="ghost" @click="lib.deletePreset('prop', p.id)">
              <Trash2 :size="10" />
            </AppButton>
          </li>
        </ul>
      </AppPanel>
    </template>

    <input ref="fileInput" type="file" multiple class="hidden" @change="onFiles" />
    <input
      ref="presetFileInput"
      type="file"
      accept="image/*"
      class="hidden"
      @change="onPresetFile"
    />

    <DirPicker
      :open="picking"
      :start="lib.info?.dir ?? lib.status?.remembered_dir ?? ''"
      title="选择素材库目录"
      confirm-label="用这个目录当素材库"
      @update:open="picking = $event"
      @pick="configure"
    />

    <AdoptDialog
      v-if="adopting"
      :open="true"
      :pid="pid"
      :kind="adopting.kind"
      :library-id="adopting.id"
      @update:open="adopting = $event ? adopting : null"
      @adopted="onAdopted"
    />
    <!-- 新建预设：名称与默认定妆图 / 参考图缺一不可 -->
    <AppDialog
      :open="presetKind !== ''"
      :title="presetKind ? `新建${PRESET_LABEL[presetKind]}预设` : ''"
      subtitle="库里的预设是模板，采用进工程后是可再改的副本"
      size="md"
      @update:open="presetKind = $event ? presetKind : ''"
    >
      <form id="new-preset" class="space-y-3 p-3" @submit.prevent="addPreset()">
        <label class="block">
          <span class="text-fg-3 text-2xs">名称</span>
          <input
            v-model="presetName"
            type="text"
            autofocus
            :placeholder="
              presetKind === 'character'
                ? '林昭'
                : presetKind === 'location'
                  ? '城南旧宅'
                  : '铜制怀表'
            "
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-0.5 h-row w-full rounded-sm border px-2 text-xs outline-none"
          />
        </label>
        <section>
          <div class="flex items-center justify-between gap-2">
            <span class="text-fg-3 text-2xs">默认{{ presetKind === 'character' ? '定妆图' : '参考图' }}（必选）</span>
            <div class="flex items-center gap-1.5">
              <span class="text-fg-4 text-2xs">创建后不可删除，可通过挂新图修改</span>
              <AppButton
                size="sm"
                variant="ghost"
                :disabled="lib.busy"
                title="上传图片并设为默认参考图"
                @click="presetFileInput?.click()"
              >
                <Upload :size="10" />上传
              </AppButton>
            </div>
          </div>
          <EmptyState
            v-if="pickable(presetAssetKind).length === 0"
            class="mt-1.5"
            title="还没有可选图片"
            body="可直接点击上方上传；没有默认图时不允许创建。"
          />
          <div v-else class="mt-1.5 grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(7rem,1fr))]">
            <button
              v-for="asset in pickable(presetAssetKind)"
              :key="asset.id"
              type="button"
              class="bg-base-2 overflow-hidden border text-left"
              :class="presetDefaultAssetId === asset.id ? 'border-accent/60 ring-1 ring-accent/30' : 'border-line-1'"
              @click="presetDefaultAssetId = asset.id"
            >
              <span class="bg-base-3 flex h-20 items-center justify-center overflow-hidden">
                <img :src="libraryFileUrl(asset.path)" alt="" loading="lazy" class="size-full object-cover" />
              </span>
              <span class="text-fg-2 block truncate px-1.5 py-1 text-2xs">{{ asset.title || asset.path }}</span>
            </button>
          </div>
        </section>
        <p v-if="presetKind === 'location'" class="text-fg-4 text-2xs">
          系统会同时建立“默认场景”，之后仍可新增雨夜、白天等其他变体。
        </p>
      </form>

      <ErrorPanel
        v-if="lib.lastError"
        class="mx-3 mb-3"
        :error="lib.lastError"
        @dismiss="lib.clearError()"
      />

      <template #footer>
        <span class="flex-1" />
        <AppButton variant="ghost" @click="presetKind = ''">取消</AppButton>
        <AppButton
          type="submit"
          form="new-preset"
          variant="primary"
          :disabled="lib.busy || presetName.trim() === '' || presetDefaultAssetId === ''"
        >
          <Plus :size="11" />新建
        </AppButton>
      </template>
    </AppDialog>

    <!-- 标签：库会越攒越大，这是之后找回素材的主要手段 -->
    <AppDialog
      v-model:open="tagging"
      title="标签"
      subtitle="库会越攒越大，标签是之后找回素材的主要手段"
      size="sm"
    >
      <form
        id="new-tag"
        class="border-line-1 flex items-center gap-1.5 border-b p-3"
        @submit.prevent="addTag()"
      >
        <input
          v-model="newTag"
          placeholder="新标签名，例如 民国"
          class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 text-xs outline-none"
        />
        <AppButton type="submit" :disabled="lib.busy || newTag.trim() === ''">
          <Plus :size="11" />新建
        </AppButton>
      </form>
      <p v-if="lib.tags.length === 0" class="text-fg-4 p-3 text-2xs">还没有标签。</p>
      <div v-else class="flex flex-wrap gap-1 p-3">
        <AppBadge v-for="t in lib.tags" :key="t.id" tone="accent">{{ t.name }}</AppBadge>
      </div>

      <ErrorPanel
        v-if="lib.lastError"
        class="mx-3 mb-3"
        :error="lib.lastError"
        @dismiss="lib.clearError()"
      />

      <template #footer>
        <p class="text-fg-4 min-w-0 flex-1 text-2xs">标签建好后在素材卡片上的「＋标签」里挂。</p>
        <AppButton variant="ghost" @click="tagging = false">关闭</AppButton>
      </template>
    </AppDialog>

    <!-- 新建变体：参考图挂在变体上，所以地点必须先有变体 -->
    <AppDialog
      :open="variantFor !== null"
      title="新建变体"
      :subtitle="variantFor?.name ?? ''"
      size="sm"
      @update:open="variantFor = $event ? variantFor : null"
    >
      <form id="new-variant" class="p-3" @submit.prevent="addVariant()">
        <label class="block">
          <span class="text-fg-3 text-2xs">变体名</span>
          <input
            v-model="variantName"
            type="text"
            autofocus
            placeholder="雨夜 / 白天 / 火灾后"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-0.5 h-row w-full rounded-sm border px-2 text-xs outline-none"
          />
        </label>
        <p class="text-fg-4 mt-1.5 text-2xs">
          同一处地点的不同时间 / 天气各是一个变体，各自挂一套参考图。
        </p>
      </form>

      <ErrorPanel
        v-if="lib.lastError"
        class="mx-3 mb-3"
        :error="lib.lastError"
        @dismiss="lib.clearError()"
      />

      <template #footer>
        <span class="flex-1" />
        <AppButton variant="ghost" @click="variantFor = null">取消</AppButton>
        <AppButton
          type="submit"
          form="new-variant"
          variant="primary"
          :disabled="lib.busy || variantName.trim() === ''"
        >
          <Plus :size="11" />新建
        </AppButton>
      </template>
    </AppDialog>

    <!-- 挂图：从库里已上传的图里挑一张，看图选比看文件名选靠得住 -->
    <AppDialog
      :open="attaching !== null"
      :title="attaching ? `挂${ATTACH_LABEL[attaching.target]}` : ''"
      :subtitle="attaching?.title ?? ''"
      @update:open="attaching = $event ? attaching : null"
    >
      <template v-if="attaching">
        <EmptyState
          v-if="pickable(ATTACH_KIND[attaching.target]).length === 0"
          title="库里还没有可挂的图"
          body="先回素材 tab 上传一张（类型选对应的参考图，或用「其它上传」），再回来挂。"
        />
        <div
          v-else
          class="grid gap-2 p-3 [grid-template-columns:repeat(auto-fill,minmax(7rem,1fr))]"
        >
          <button
            v-for="o in pickable(ATTACH_KIND[attaching.target])"
            :key="o.id"
            type="button"
            class="bg-base-2 flex flex-col overflow-hidden border text-left"
            :class="attachPick === o.id ? 'border-accent/60' : 'border-line-1'"
            @click="attachPick = o.id"
          >
            <span class="bg-base-3 flex h-20 items-center justify-center overflow-hidden">
              <img
                :src="libraryFileUrl(o.path)"
                alt=""
                loading="lazy"
                class="size-full object-cover"
              />
            </span>
            <span class="text-fg-2 truncate px-1.5 py-1 text-2xs">{{ o.title || o.path }}</span>
          </button>
        </div>
      </template>

      <ErrorPanel
        v-if="lib.lastError"
        class="mx-3 mb-3"
        :error="lib.lastError"
        @dismiss="lib.clearError()"
      />

      <template #footer>
        <p class="text-fg-4 min-w-0 flex-1 text-2xs">新挂的自动成为当前版本，旧的留在历史里。</p>
        <AppButton variant="ghost" @click="attaching = null">取消</AppButton>
        <AppButton variant="primary" :disabled="lib.busy || attachPick === ''" @click="attach()">
          <ImagePlus :size="11" />挂上
        </AppButton>
      </template>
    </AppDialog>
  </div>
</template>
