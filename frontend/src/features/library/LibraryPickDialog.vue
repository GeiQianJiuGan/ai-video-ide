<script setup lang="ts">
/**
 * 「从素材库采用」选择器 —— 项目内取全局素材的入口。
 *
 * 原来采用只能在素材库页做（选中一条 → 采用到当前项目），于是在角色页想加一个
 * 库里已有的角色，得先跳出去、选中、采用、再跳回来。这个组件把那趟往返省掉。
 * 现在它还是**唯一**的往返：素材库属于应用级导航，打开工程后左栏里没有它，
 * 项目内要取库里的东西一律从这个框走（`kind: 'asset'` 就是原来素材库页上那个
 * 「采用到当前项目」，搬进了资产库页）。
 *
 * 它只负责「挑哪一条」，动手仍然交给 AdoptDialog（先出账单再复制，顺序不能颠倒）。
 * 三态：
 *   1. 库没配置 —— **不是错误**，画引导 + 跳素材库页的按钮（那会离开工程的工作区，
 *      但工程不关，左栏会出现「返回工程」）；
 *   2. 库里这一类是空的 —— 画空状态，说清去哪儿建；
 *   3. 有内容 —— 列出来选一条。
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { DialogContent, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui'
import { Library, RefreshCw } from '@lucide/vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import AdoptDialog from './AdoptDialog.vue'
import { libraryFileUrl } from '@/shared/api/files'
import { humanBytes, LIBRARY_KIND_LABEL, type AdoptResult } from '@/shared/api/library'
import { useLibraryStore } from '@/stores/library'

const props = defineProps<{
  open: boolean
  /** 采用到哪个工程。 */
  pid: string
  /** 挑哪一类。与项目页一一对应，不给「随便挑」。 */
  kind: 'asset' | 'character' | 'location' | 'prop'
}>()

const emit = defineEmits<{ 'update:open': [boolean]; adopted: [AdoptResult] }>()

const lib = useLibraryStore()
const router = useRouter()

/** 选中要采用的那一条；交给 AdoptDialog 后由它出账单。 */
const picked = ref('')

const KIND_LABEL = { asset: '素材文件', character: '角色', location: '地点', prop: '道具' } as const

interface Row {
  id: string
  name: string
  /** 副标题：形象 / 变体 / 参考图的数量，让人知道会带进来什么。 */
  detail: string
  thumb: string
  /** 库里的文件已经不在磁盘上：仍然列出来但不能采用，藏起来只会让人以为库空了。 */
  missing?: boolean
}

const assetById = computed(() => new Map(lib.assets.map((a) => [a.id, a])))

function thumbOf(assetId: string | null | undefined): string {
  if (!assetId) return ''
  const asset = assetById.value.get(assetId)
  if (!asset || asset.missing) return ''
  return libraryFileUrl(asset.path)
}

const rows = computed<Row[]>(() => {
  if (props.kind === 'asset') {
    return lib.assets.map((a) => ({
      id: a.id,
      name: a.title || a.path.split('/').pop() || a.id,
      detail: [
        LIBRARY_KIND_LABEL[a.kind as keyof typeof LIBRARY_KIND_LABEL] ?? a.kind,
        humanBytes(a.size_bytes),
        a.width && a.height ? `${a.width}×${a.height}` : '',
      ]
        .filter(Boolean)
        .join(' · '),
      thumb: a.missing ? '' : libraryFileUrl(a.path),
      missing: a.missing,
    }))
  }
  if (props.kind === 'character') {
    return lib.characters.map((c) => ({
      id: c.id,
      name: c.name,
      detail: `${c.appearances.length} 个形象 · ${c.appearances.reduce((n, a) => n + a.sheet_count, 0)} 张角色表`,
      thumb: thumbOf(c.appearances.find((a) => a.current_sheet)?.current_sheet?.asset_id),
    }))
  }
  if (props.kind === 'location') {
    return lib.locations.map((l) => ({
      id: l.id,
      name: l.name,
      detail: `${l.variants.length} 个变体 · ${l.variants.reduce((n, v) => n + v.reference_count, 0)} 张参考图`,
      thumb: '',
    }))
  }
  return lib.props.map((p) => ({
    id: p.id,
    name: p.name,
    detail: `${p.reference_count} 张参考图`,
    thumb: thumbOf(p.current_reference?.asset_id),
  }))
})

/** 每次打开都对齐一次库内容：库是应用级的，可能在别处改过。 */
watch(
  () => props.open,
  async (open) => {
    picked.value = ''
    if (open) await lib.refresh()
  },
  { immediate: true },
)

function goLibrary(): void {
  emit('update:open', false)
  void router.push({ name: 'library' })
}

function onAdopted(result: AdoptResult): void {
  picked.value = ''
  emit('adopted', result)
  emit('update:open', false)
}
</script>

<template>
  <DialogRoot :open="open" @update:open="emit('update:open', $event)">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-40 bg-black/50" />
      <DialogContent
        class="border-line-2 bg-base-1 fixed top-[10vh] left-1/2 z-50 flex max-h-[76vh] w-[min(34rem,92vw)] -translate-x-1/2 flex-col overflow-hidden rounded-md border shadow-2xl"
      >
        <DialogTitle
          class="border-line-1 text-fg-1 flex h-9 shrink-0 items-center gap-2 border-b px-3 text-xs"
        >
          <Library :size="12" class="text-accent" />
          从素材库采用{{ KIND_LABEL[kind] }}
          <span v-if="lib.info" class="text-fg-4 text-2xs truncate">{{ lib.info.name }}</span>
          <AppButton
            v-if="lib.configured"
            size="sm"
            variant="ghost"
            class="ml-auto"
            :disabled="lib.busy"
            @click="lib.refresh()"
          >
            <RefreshCw :size="10" />刷新
          </AppButton>
        </DialogTitle>

        <div class="min-h-0 flex-1 overflow-auto">
          <!-- 态 1：没配置库。这不是错误，别画成红的 -->
          <EmptyState
            v-if="!lib.configured"
            title="还没有选择素材库目录"
            body="素材库是一个独立目录（library.aivs.json + library.db + assets/），位置由你决定。配好之后，里面的角色 / 地点 / 道具预设就能采用到任何工程里。"
          >
            <AppButton variant="primary" @click="goLibrary()">去素材库页选目录</AppButton>
          </EmptyState>

          <!-- 态 2：库里这一类还是空的 -->
          <EmptyState
            v-else-if="rows.length === 0"
            :title="`素材库里还没有${KIND_LABEL[kind]}`"
            :body="
              kind === 'asset'
                ? '先去素材库页上传几个文件，之后每一部片子都能直接采用它们，不用重复导入。'
                : '先去素材库页建一条并挂上参考图，之后每一部片子都能直接采用它，不用从零重建。'
            "
          >
            <AppButton variant="primary" @click="goLibrary()">
              {{ kind === 'asset' ? '去素材库页上传' : '去素材库页新建' }}
            </AppButton>
          </EmptyState>

          <!-- 态 3：挑一条 -->
          <ul v-else class="divide-line-1 divide-y">
            <li v-for="row in rows" :key="row.id">
              <button
                class="hover:bg-base-2 flex w-full items-center gap-2 px-2 py-1.5 text-left disabled:opacity-50"
                :disabled="row.missing"
                :title="row.missing ? '这个文件在库目录里找不到了，采用无法进行' : row.name"
                @click="picked = row.id"
              >
                <span
                  class="border-line-1 bg-base-2 flex h-8 w-8 shrink-0 items-center justify-center overflow-hidden border"
                >
                  <img
                    v-if="row.thumb"
                    :src="row.thumb"
                    alt=""
                    class="h-full w-full object-cover"
                  />
                  <span v-else class="text-fg-4 text-2xs">无图</span>
                </span>
                <span class="min-w-0 flex-1">
                  <span class="text-fg-1 block truncate text-xs">{{ row.name }}</span>
                  <span class="text-fg-4 block truncate text-2xs">{{ row.detail }}</span>
                </span>
                <AppBadge v-if="row.missing" tone="fail">文件不见了</AppBadge>
                <AppBadge v-else tone="accent">采用</AppBadge>
              </button>
            </li>
          </ul>
        </div>

        <ErrorPanel
          v-if="lib.lastError"
          class="shrink-0"
          :error="lib.lastError"
          @dismiss="lib.clearError()"
        />

        <div class="border-line-1 flex shrink-0 items-center gap-1.5 border-t p-2">
          <p class="text-fg-4 min-w-0 flex-1 text-2xs">
            采用是单向复制：文件会复制一份进工程，之后两边各改各的。
          </p>
          <AppButton variant="ghost" @click="emit('update:open', false)">关闭</AppButton>
        </div>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>

  <!-- 挑好之后才出账单：复制进用户的工程目录这一步不能跳 -->
  <AdoptDialog
    v-if="picked"
    :open="picked !== ''"
    :pid="pid"
    :kind="kind"
    :library-id="picked"
    @update:open="(v) => !v && (picked = '')"
    @adopted="onAdopted"
  />
</template>
