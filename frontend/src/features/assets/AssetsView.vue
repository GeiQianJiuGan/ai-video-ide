<script setup lang="ts">
/**
 * 资产库（Step 3 的前端）。
 *
 * 这一页是所有落盘文件的总账，回答三个问题：哪来的、被谁用了、哪些没人要。
 *
 * 三个刻意的设计：
 *   1. **孤儿是叠在同一张表上的一层标记**，不是另开一个列表——「有哪些文件」和
 *      「哪些能删」必须能对着看，分两页只会让人对不上账。
 *   2. **删之前先说清会破坏什么**。仍被引用时后端拒绝并列出是谁在用；右栏的反查面板
 *      就是那份清单，强删按钮旁边永远写着破坏几处。
 *   3. **文件丢了不等于登记错了**。`missing` 的资产照常列出来并标红：它是需要处理的
 *      事实，藏起来只会让导出在半路失败。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Image as ImageIcon, Library, RefreshCw, Search, Trash2 } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import LibraryPickDialog from '@/features/library/LibraryPickDialog.vue'
import { fileUrl } from '@/shared/api/files'
import {
  ASSET_KIND_LABEL,
  OWNER_KIND_LABEL,
  humanBytes,
  type Asset,
  type AssetKind,
} from '@/shared/api/assets'
import { useAssetsStore } from '@/stores/assets'

const route = useRoute()
const router = useRouter()
const store = useAssetsStore()

const pid = computed(() => String(route.params.pid ?? ''))
const KINDS = Object.keys(ASSET_KIND_LABEL) as AssetKind[]

/** 每种类型有多少个——筛选侧栏上直接写数字，省得一个个点进去看。 */
const countOf = computed(() => {
  const m: Record<string, number> = {}
  for (const a of store.assets) m[a.kind] = (m[a.kind] ?? 0) + 1
  return m
})

const isOrphan = (a: Asset): boolean => store.orphanIds?.has(a.id) ?? false

const IMAGE_EXT = /\.(png|jpe?g|webp|gif|bmp|avif)$/i
/**
 * 只显示图片缩略图：视频与音频没有可直接 `<img>` 的帧。
 *
 * mime 允许为空（后端只在拿得到时才填），所以扩展名要能兜住——否则一张
 * mime 缺失的 PNG 会被当成非图片，明明能显示却只画一个占位图标。
 */
const isImage = (a: Asset): boolean =>
  (a.mime ?? '').startsWith('image/') || (!a.mime && IMAGE_EXT.test(a.path))

function thumb(a: Asset): string {
  return a.missing ? '' : fileUrl(pid.value, a.path)
}

/** 删除结果拼成一句话：磁盘文件删掉没有、强删破了几处，都要说出来。 */
const deleteSummary = computed(() => {
  const r = store.lastDelete
  if (!r) return ''
  const head = r.file_removed
    ? '已删除登记，磁盘上的文件也删掉了'
    : '已删除登记，但磁盘文件没能删掉（可能被别的程序占用）'
  return r.broken_refs ? `${head}；破坏了 ${r.broken_refs} 处引用，这些地方现在会缺图` : head
})

async function reload(): Promise<void> {
  if (!pid.value) return
  await store.load(pid.value).catch(() => {})
}

onMounted(reload)
watch(pid, reload)

const confirmForce = ref('')

/**
 * 「从素材库采用」原来长在素材库页上（选中一条 → 采用到当前项目）。素材库属于应用级
 * 导航，打开工程后左栏里没有它，所以那个动作搬到这儿来——工程内取库里的文件，
 * 入口就在这份总账上，不需要先掉出工程再回来。
 */
const pickLibrary = ref(false)

async function onAdopted(): Promise<void> {
  await reload()
}

async function doRemove(assetId: string, force: boolean): Promise<void> {
  const ok = await store.remove(pid.value, assetId, force)
  if (ok) confirmForce.value = ''
}

/** 反查结果里能跳过去的只有镜头——别的 owner 没有独立页面，给出地址就够了。 */
function goOwner(ownerKind: string, ownerId: string): void {
  if (ownerKind !== 'shot') return
  void router.push({ name: 'shot', params: { pid: pid.value, sid: ownerId } })
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />
    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1.5 border-b px-2">
      <AppButton
        size="sm"
        variant="primary"
        :disabled="store.busy"
        title="找出没有任何引用的文件。只报事实，不会自动删任何东西"
        @click="store.scanOrphans(pid).catch(() => {})"
      >
        <Search :size="10" />扫描孤儿资产
      </AppButton>
      <AppButton
        size="sm"
        :disabled="store.busy"
        title="从应用级素材库复制一个文件进这个工程。先出账单再复制，之后两边各改各的"
        @click="pickLibrary = true"
      >
        <Library :size="10" />从素材库采用
      </AppButton>
      <span class="text-fg-3 tnum text-2xs">
        {{ store.assets.length }} 个文件 · {{ humanBytes(store.totalBytes) }}
        <template v-if="store.orphanIds !== null">
          · 孤儿 {{ store.orphanIds.size }} 个（{{ humanBytes(store.orphanBytes) }} 可回收）
        </template>
        <span v-if="store.missing.length" class="text-st-review">
          · {{ store.missing.length }} 个文件已丢失
        </span>
      </span>
      <AppButton size="sm" variant="ghost" class="ml-auto" :disabled="store.busy" @click="reload()">
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>

    <ErrorPanel
      v-if="store.lastError"
      class="mx-2 mt-2"
      :error="store.lastError"
      @dismiss="store.clearError()"
    />
    <div
      v-if="store.lastDelete"
      class="border-line-1 bg-base-2 mx-2 mt-2 flex items-center gap-2 border p-1.5"
    >
      <p class="text-fg-2 min-w-0 flex-1 text-2xs">{{ deleteSummary }}</p>
      <button class="text-fg-4 hover:text-fg-1 text-2xs" @click="store.lastDelete = null">
        关闭
      </button>
    </div>

    <div class="flex min-h-0 flex-1 gap-2 p-2">
      <!-- 左：按类型筛选 -->
      <AppPanel title="按类型筛选" class="w-44 shrink-0">
        <ul class="divide-line-1 divide-y">
          <li>
            <button
              class="hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left text-2xs"
              :class="store.kind === '' ? 'bg-accent-dim/40 text-fg-1' : 'text-fg-2'"
              @click="store.setKind(pid, '')"
            >
              <span class="min-w-0 flex-1 truncate">全部</span>
              <span class="text-fg-4 tnum">{{ store.assets.length }}</span>
            </button>
          </li>
          <li v-for="k in KINDS" :key="k">
            <button
              class="hover:bg-base-2 flex w-full items-center gap-1.5 px-2 py-1 text-left text-2xs"
              :class="store.kind === k ? 'bg-accent-dim/40 text-fg-1' : 'text-fg-2'"
              @click="store.setKind(pid, k)"
            >
              <span class="min-w-0 flex-1 truncate">{{ ASSET_KIND_LABEL[k] }}</span>
              <span v-if="store.kind === ''" class="text-fg-4 tnum">{{ countOf[k] ?? 0 }}</span>
            </button>
          </li>
        </ul>
        <p class="text-fg-4 border-line-1 border-t p-2 text-2xs">
          generations/ 只放生成物，手动素材一律进 assets/——路径由后端的类型映射决定，不是随手放的。
        </p>
      </AppPanel>

      <!-- 中：资产网格 -->
      <AppPanel title="资产网格" class="min-h-0 min-w-0 flex-1">
        <template #actions>
          <span class="text-fg-4 text-2xs">
            {{
              store.orphanIds === null
                ? '点上面「扫描孤儿资产」把没人用的标出来'
                : '标「孤儿」的没有任何引用，可以安全回收'
            }}
          </span>
        </template>
        <EmptyState
          v-if="store.assets.length === 0"
          :title="store.kind ? '这个类型下还没有文件' : '工程里还没有落盘文件'"
          :body="
            store.kind
              ? '换一个类型看看，或者点「全部」。'
              : '上传角色表、场景参考图，或者跑一次生成——所有落盘文件都会登记到这里，包括导出的成片。'
          "
        />
        <div v-else class="grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-2 p-2">
          <article
            v-for="a in store.assets"
            :key="a.id"
            class="border bg-base-2"
            :class="
              a.id === store.selectedId
                ? 'border-accent/60'
                : a.missing
                  ? 'border-st-failed/60'
                  : 'border-line-1'
            "
          >
            <button class="block w-full text-left" @click="store.loadRefs(pid, a.id)">
              <span class="bg-base-3 flex h-20 items-center justify-center overflow-hidden">
                <img
                  v-if="isImage(a) && thumb(a)"
                  :src="thumb(a)"
                  alt=""
                  class="h-full w-full object-cover"
                />
                <ImageIcon v-else :size="14" class="text-fg-4" />
              </span>
              <span class="block truncate px-1.5 pt-1">
                <span class="text-fg-1 text-2xs">{{ ASSET_KIND_LABEL[a.kind] ?? a.kind }}</span>
              </span>
              <span class="text-fg-4 block truncate px-1.5 text-2xs">{{ a.path }}</span>
              <span class="flex flex-wrap items-center gap-1 px-1.5 pt-1 pb-1.5">
                <AppBadge v-if="a.missing" tone="fail">文件丢失</AppBadge>
                <AppBadge v-else-if="isOrphan(a)" tone="warn">孤儿</AppBadge>
                <AppBadge v-else-if="a.ref_count > 0" tone="ok">{{ a.ref_count }} 处引用</AppBadge>
                <span class="text-fg-4 tnum text-2xs">{{ humanBytes(a.size_bytes) }}</span>
                <span v-if="a.width && a.height" class="text-fg-4 tnum text-2xs">
                  {{ a.width }}×{{ a.height }}
                </span>
              </span>
            </button>
          </article>
        </div>
      </AppPanel>

      <!-- 右：引用关系与删除 -->
      <AppPanel title="引用关系" class="w-72 shrink-0">
        <EmptyState
          v-if="!store.selected"
          title="选一个文件"
          body="点一张缩略图，这里反查它被哪些角色形象、地点变体、镜头或版本引用——删之前先看清会破坏什么。"
        />
        <div v-else class="space-y-3 p-2">
          <section>
            <p class="text-fg-1 text-2xs">
              {{ ASSET_KIND_LABEL[store.selected.kind] ?? store.selected.kind }}
            </p>
            <p class="text-fg-4 mt-0.5 text-2xs break-all">{{ store.selected.path }}</p>
            <p class="text-fg-4 mt-0.5 text-2xs">
              {{ humanBytes(store.selected.size_bytes) }}
              <template v-if="store.selected.width && store.selected.height">
                · {{ store.selected.width }}×{{ store.selected.height }}
              </template>
              <template v-if="store.selected.duration">
                · {{ Math.round(store.selected.duration * 10) / 10 }}s
              </template>
              · 来源 {{ store.selected.source }} · {{ store.selected.created_at.slice(0, 16) }}
            </p>
            <p v-if="store.selected.missing" class="text-st-review mt-1 text-2xs">
              登记还在，但文件已经不在磁盘上了：可能被工程外的程序删掉或移走了。
            </p>
          </section>

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">
              被谁用了（{{ store.refs.length }} 处）
            </p>
            <p v-if="store.refs.length === 0" class="text-fg-4 mt-1 text-2xs">
              没有任何引用。这是一个孤儿文件，删掉不会破坏任何地方。
            </p>
            <ul v-else class="mt-1 space-y-0.5">
              <li v-for="r in store.refs" :key="r.id" class="text-2xs">
                <button
                  class="text-fg-2"
                  :class="r.owner_kind === 'shot' ? 'hover:text-accent' : 'cursor-default'"
                  :title="r.owner_kind === 'shot' ? '打开这个镜头' : r.owner_id"
                  @click="goOwner(r.owner_kind, r.owner_id)"
                >
                  {{ OWNER_KIND_LABEL[r.owner_kind] ?? r.owner_kind }}
                </button>
                <span v-if="r.role" class="text-fg-4"> · {{ r.role }}</span>
                <span class="text-fg-4"> · {{ r.owner_id }}</span>
              </li>
            </ul>
          </section>

          <section class="border-line-1 border-t pt-2">
            <p class="text-fg-3 text-2xs tracking-wide uppercase">删除</p>
            <AppButton
              size="sm"
              class="mt-1"
              :disabled="store.busy"
              title="删除登记与磁盘文件。仍被引用时后端会拒绝，并告诉你是谁在用"
              @click="doRemove(store.selected.id, false)"
            >
              <Trash2 :size="10" />删除这个文件
            </AppButton>
            <template v-if="store.refs.length > 0">
              <p class="text-fg-4 mt-1.5 text-2xs">
                它还有
                {{ store.refs.length }} 处引用，普通删除会被拒。确认这些地方可以缺图，再走强删：
              </p>
              <div class="mt-1 flex items-center gap-1">
                <input
                  v-model="confirmForce"
                  :placeholder="`输入 强删 确认`"
                  class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-5 min-w-0 flex-1 border px-1.5 text-2xs outline-none"
                />
                <AppButton
                  size="sm"
                  :disabled="store.busy || confirmForce !== '强删'"
                  title="强制删除：这些引用会变成缺图，不可撤销"
                  @click="doRemove(store.selected.id, true)"
                >
                  强制删除
                </AppButton>
              </div>
            </template>
            <p class="text-fg-4 mt-1.5 text-2xs">
              删除只删这一个文件，不动引用它的角色 / 镜头本身——它们会变成「缺图」，而不是消失。
            </p>
          </section>
        </div>
      </AppPanel>
    </div>

    <LibraryPickDialog v-model:open="pickLibrary" :pid="pid" kind="asset" @adopted="onAdopted()" />
  </div>
</template>
