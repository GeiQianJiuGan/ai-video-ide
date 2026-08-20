<script setup lang="ts">
/**
 * 素材库页（Phase 4）。
 *
 * 素材库是应用级的：一个目录 + 一份 library.db，与任何工程都无关。三条要求：
 *   1. 没配置不是错误——画引导让用户选目录（DirPicker，与新建工程共用一套）；
 *   2. 采用必须先出账单再动手（AdoptDialog 负责），这里只管选中哪一条；
 *   3. 「采用是单向复制」写在页面上，别让用户以为库和工程会互相同步。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { FolderSearch, Plus, RefreshCw, Trash2, Upload } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
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
const TABS: { key: Tab; label: string }[] = [
  { key: 'assets', label: '素材' },
  { key: 'characters', label: '角色' },
  { key: 'locations', label: '地点' },
  { key: 'props', label: '道具' },
]
const KINDS = Object.keys(LIBRARY_KIND_LABEL) as LibraryKind[]

const tab = ref<Tab>('assets')
const picking = ref(false)
const kindFilter = ref<'' | LibraryKind>('')
const tagFilter = ref('')
const uploadKind = ref<LibraryKind>('upload')
const fileInput = ref<HTMLInputElement | null>(null)
const newName = ref('')
const newTag = ref('')
/** 每个「挂参考图」的位置各自记住选了哪张图。 */
const pendingAsset = ref<Record<string, string>>({})
/** 每个地点各自记住正在输入的变体名。 */
const variantName = ref<Record<string, string>>({})
const adopting = ref<{ kind: AdoptKind; id: string } | null>(null)
const adopted = ref<AdoptResult | null>(null)
/** 删素材被引用时后端会拒；记下这一条，错误面板才能给「仍然删除」。 */
const deleting = ref('')

const pid = computed(() => proj.current?.id ?? '')
const assetById = computed(() => new Map(lib.assets.map((a) => [a.id, a])))

const visibleAssets = computed(() =>
  lib.assets.filter(
    (a) =>
      (kindFilter.value === '' || a.kind === kindFilter.value) &&
      (tagFilter.value === '' || a.tags.some((t) => t.name === tagFilter.value)),
  ),
)

function kindLabel(kind: string): string {
  return LIBRARY_KIND_LABEL[kind as LibraryKind] ?? kind
}

/** 挂参考图的候选：库里还在的图，本类型优先，「其它上传」也允许。 */
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

async function addPreset(kind: 'character' | 'location' | 'prop'): Promise<void> {
  const name = newName.value.trim()
  if (!name) return
  try {
    await lib.createPreset(kind, name)
    newName.value = ''
  } catch {
    /* 错误已由 store 记入 lastError，下方错误面板负责展示 */
  }
}

async function addVariant(lid: string): Promise<void> {
  const name = (variantName.value[lid] ?? '').trim()
  if (!name) return
  try {
    await lib.createVariant(lid, name)
    delete variantName.value[lid]
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
    /* 同上 */
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

async function attach(kind: 'appearance' | 'variant' | 'prop', id: string): Promise<void> {
  const assetId = pendingAsset.value[id]
  if (!assetId) return
  try {
    await lib.attachReference({ kind, id }, assetId)
    delete pendingAsset.value[id]
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
      <h1 class="text-fg-1 text-base font-medium">素材库</h1>
      <p class="text-fg-2 mt-1 text-xs">
        库是应用级的一个目录，跨项目复用：素材文件 + 角色 / 地点 / 道具预设。
      </p>
      <p class="text-fg-4 mt-1 text-2xs">
        采用是单向复制：文件会进工程目录，之后库里改了不回流工程，工程里改了也不影响库。
      </p>
    </section>

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
          <AppButton size="sm" variant="ghost" @click="lib.refresh()">
            <RefreshCw :size="10" />刷新
          </AppButton>
          <AppButton size="sm" variant="ghost" @click="picking = true">
            <FolderSearch :size="10" />换个目录
          </AppButton>
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
                  :title="pid ? '采用到当前项目' : '先打开一个工程'"
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
                <AppButton
                  size="sm"
                  variant="ghost"
                  title="从库里删除"
                  @click="removeAsset(a.id)"
                >
                  <Trash2 :size="10" />
                </AppButton>
              </div>
            </figcaption>
          </figure>
        </div>

        <div class="border-line-1 flex items-center gap-1.5 border-t p-2">
          <input
            v-model="newTag"
            placeholder="新建标签"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row w-40 rounded-sm border px-2 text-xs outline-none"
            @keydown.enter.prevent="addTag()"
          />
          <AppButton size="sm" :disabled="newTag.trim() === ''" @click="addTag()">
            <Plus :size="10" />标签
          </AppButton>
          <p class="text-fg-4 text-2xs">库会越攒越大，标签是之后找回素材的主要手段。</p>
        </div>
      </AppPanel>

      <AppPanel v-else-if="tab === 'characters'" title="角色预设" class="mt-2">
        <template #actions>
          <input
            v-model="newName"
            placeholder="角色名"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 h-5 w-32 rounded-sm border px-1.5 text-2xs outline-none"
            @keydown.enter.prevent="addPreset('character')"
          />
          <AppButton size="sm" :disabled="newName.trim() === ''" @click="addPreset('character')">
            <Plus :size="10" />新建
          </AppButton>
        </template>
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
              <AppButton size="sm" :disabled="!pid" @click="askAdopt('character', c.id)">
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
                <select
                  v-model="pendingAsset[a.id]"
                  class="border-line-1 bg-base-1 text-fg-3 h-5 w-28 rounded-sm border px-1 text-2xs outline-none"
                >
                  <option value="">选一张图</option>
                  <option v-for="o in pickable('character_sheet')" :key="o.id" :value="o.id">
                    {{ o.title || o.path }}
                  </option>
                </select>
                <AppButton
                  size="sm"
                  :disabled="!pendingAsset[a.id]"
                  @click="attach('appearance', a.id)"
                >
                  挂定妆图
                </AppButton>
              </li>
            </ul>
          </li>
        </ul>
      </AppPanel>

      <AppPanel v-else-if="tab === 'locations'" title="地点预设" class="mt-2">
        <template #actions>
          <input
            v-model="newName"
            placeholder="地点名"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 h-5 w-32 rounded-sm border px-1.5 text-2xs outline-none"
            @keydown.enter.prevent="addPreset('location')"
          />
          <AppButton size="sm" :disabled="newName.trim() === ''" @click="addPreset('location')">
            <Plus :size="10" />新建
          </AppButton>
        </template>
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
              <AppButton size="sm" :disabled="!pid" @click="askAdopt('location', l.id)">
                采用
              </AppButton>
              <AppButton size="sm" variant="ghost" @click="lib.deletePreset('location', l.id)">
                <Trash2 :size="10" />
              </AppButton>
            </div>
            <p v-if="l.description" class="text-fg-4 mt-0.5 text-2xs">{{ l.description }}</p>
            <ul class="mt-1.5 space-y-1">
              <li
                v-for="v in l.variants"
                :key="v.id"
                class="border-line-1 bg-base-2 flex items-center gap-1.5 border px-2 py-1"
              >
                <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">{{ v.name }}</span>
                <AppBadge v-if="v.weather">{{ v.weather }}</AppBadge>
                <AppBadge v-if="v.time_of_day">{{ v.time_of_day }}</AppBadge>
                <span class="text-fg-4 text-2xs">参考图 {{ v.reference_count }}</span>
                <select
                  v-model="pendingAsset[v.id]"
                  class="border-line-1 bg-base-1 text-fg-3 h-5 w-28 rounded-sm border px-1 text-2xs outline-none"
                >
                  <option value="">选一张图</option>
                  <option v-for="o in pickable('location_reference')" :key="o.id" :value="o.id">
                    {{ o.title || o.path }}
                  </option>
                </select>
                <AppButton size="sm" :disabled="!pendingAsset[v.id]" @click="attach('variant', v.id)">
                  挂参考图
                </AppButton>
              </li>
            </ul>
            <div class="mt-1 flex items-center gap-1.5">
              <input
                v-model="variantName[l.id]"
                placeholder="新变体（雨夜 / 白天…）"
                class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 h-5 w-40 rounded-sm border px-1.5 text-2xs outline-none"
                @keydown.enter.prevent="addVariant(l.id)"
              />
              <AppButton
                size="sm"
                variant="ghost"
                :disabled="(variantName[l.id] ?? '').trim() === ''"
                @click="addVariant(l.id)"
              >
                <Plus :size="10" />变体
              </AppButton>
            </div>
          </li>
        </ul>
      </AppPanel>

      <AppPanel v-else title="道具预设" class="mt-2">
        <template #actions>
          <input
            v-model="newName"
            placeholder="道具名"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 h-5 w-32 rounded-sm border px-1.5 text-2xs outline-none"
            @keydown.enter.prevent="addPreset('prop')"
          />
          <AppButton size="sm" :disabled="newName.trim() === ''" @click="addPreset('prop')">
            <Plus :size="10" />新建
          </AppButton>
        </template>
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
            <select
              v-model="pendingAsset[p.id]"
              class="border-line-1 bg-base-1 text-fg-3 h-5 w-28 rounded-sm border px-1 text-2xs outline-none"
            >
              <option value="">选一张图</option>
              <option v-for="o in pickable('prop_reference')" :key="o.id" :value="o.id">
                {{ o.title || o.path }}
              </option>
            </select>
            <AppButton size="sm" :disabled="!pendingAsset[p.id]" @click="attach('prop', p.id)">
              挂参考图
            </AppButton>
            <AppButton size="sm" :disabled="!pid" @click="askAdopt('prop', p.id)">采用</AppButton>
            <AppButton size="sm" variant="ghost" @click="lib.deletePreset('prop', p.id)">
              <Trash2 :size="10" />
            </AppButton>
          </li>
        </ul>
      </AppPanel>
    </template>

    <AppPanel v-if="lib.lastError" title="上一步失败了" class="mt-2">
      <template #actions>
        <AppButton size="sm" variant="ghost" @click="lib.clearError()">关闭</AppButton>
      </template>
      <div class="p-3 text-xs">
        <p class="text-st-failed">{{ lib.lastError.title }}</p>
        <p class="text-fg-3 mt-0.5 text-2xs">{{ lib.lastError.detail }}</p>
        <ul class="text-fg-4 mt-1 space-y-0.5 text-2xs">
          <li v-for="s in lib.lastError.suggestions" :key="s">· {{ s }}</li>
        </ul>
        <p class="text-fg-4 mt-1 font-mono text-2xs">{{ lib.lastError.code }}</p>
        <!-- 被预设占用的素材：说清破坏什么之后，才给硬删这条路 -->
        <AppButton
          v-if="lib.lastError.code === 'CONFLICT' && deleting"
          size="sm"
          variant="danger"
          class="mt-1.5"
          @click="removeAsset(deleting, true)"
        >
          仍然删除，并解除这些引用
        </AppButton>
      </div>
    </AppPanel>

    <input ref="fileInput" type="file" multiple class="hidden" @change="onFiles" />

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
  </div>
</template>
