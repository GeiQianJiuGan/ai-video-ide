<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { fileUrl } from '@/shared/api/files'
import type { Asset } from '@/shared/api/assets'
import type { ContextBill, ContextItem, GenerationVersion } from '@/shared/api/generation'
import type { Shot } from '@/shared/api/story'
import SegmentPlayer from '@/shared/ui/SegmentPlayer.vue'
import { inputMode, referencesOf, type PromptReference } from '../shotPrompt'

export interface GraphFrame {
  role: 'first_frame' | 'last_frame'
  path: string | null
  assetId: string | null
  locked: boolean
  hint: string
}
const props = defineProps<{
  pid: string
  shot: Shot
  bill: ContextBill | null
  assets: Asset[]
  versions: GenerationVersion[]
  frames: GraphFrame[]
  busy: boolean
  prompt: string
  jobStatus?: string
}>()
const emit = defineEmits<{
  add: [assetId: string]
  remove: [key: string]
  frame: [role: 'first_frame' | 'last_frame', assetId: string]
  upload: []
  generate: []
  adopt: [versionId: string]
  reference: [reference: PromptReference]
  highlight: [key: string]
  preview: [version: GenerationVersion | null]
}>()
const search = ref('')
const libraryOpen = ref(true)
const inspectorOpen = ref(true)
const selected = ref('shot')
const highlighted = ref('')
const previewId = ref('')
const showEvidence = ref(false)
const viewport = ref<HTMLDivElement | null>(null)
const zoom = ref(0.7)
const offset = ref({ x: 0, y: 0 })
const positions = ref<Record<string, { x: number; y: number }>>({})
const references = computed(() => referencesOf(props.bill?.items ?? [], props.prompt))
const materials = computed(() =>
  (props.bill?.items ?? []).filter((i) => i.included && i.role === 'reference'),
)
const omitted = computed(() => (props.bill?.items ?? []).filter((i) => !i.included))
const version = computed(
  () =>
    props.versions.find((v) => v.id === previewId.value) ??
    props.versions.find((v) => v.is_current) ??
    null,
)
const selectedItem = computed(() => props.bill?.items.find((i) => i.key === selected.value))
const selectedRef = computed(() =>
  references.value.find((r) => r.key === selected.value || r.target === `frame:${selected.value}`),
)
const selectedFrame = computed(() => props.frames.find((f) => f.role === selected.value))
const library = computed(() =>
  props.assets.filter(
    (a) =>
      !a.missing &&
      /\.(png|jpe?g|webp|bmp|gif|mp4|webm|mov|mp3|wav|ogg|m4a|flac)$/i.test(a.path) &&
      `${a.path} ${a.description ?? ''}`.toLowerCase().includes(search.value.toLowerCase()),
  ),
)
const canvasHeight = computed(() =>
  Math.max(730, 550 + Math.ceil(Math.max(0, materials.value.length - 4) / 4) * 165),
)
const mode = computed(() =>
  inputMode(
    Boolean(props.frames[0]?.assetId),
    Boolean(props.frames[1]?.assetId),
    materials.value.filter((item) => !props.frames.some((frame) => frame.assetId === item.asset_id))
      .length,
  ),
)
const draftChanged = computed(() => {
  const v = props.versions.find((row) => row.is_current)
  if (!v || v.source !== 'generated') return false
  if (typeof v.params.prompt === 'string' && v.params.prompt !== props.prompt) return true
  const context = v.context as { included?: ContextItem[] } | null
  if (!context?.included) return false
  const signature = (items: ContextItem[]) =>
    items
      .filter((i) => i.included !== false)
      .map((i) => `${i.asset_id}:${i.role}`)
      .sort()
      .join('|')
  return (
    signature(context.included) !== signature((props.bill?.items ?? []).filter((i) => i.included))
  )
})
function pos(item: ContextItem, index: number) {
  const rowStart = Math.floor(index / 4) * 4
  const count = Math.min(4, materials.value.length - rowStart)
  return (
    positions.value[item.key] ?? {
      x: (1100 - count * 220 - (count - 1) * 45) / 2 + (index % 4) * 265,
      y: index < 4 ? 28 : 550 + Math.floor((index - 4) / 4) * 165,
    }
  )
}
function curve(x: number, y: number, tx = 550, ty = 365) {
  return `M ${x} ${y} C ${x} ${(y + ty) / 2}, ${tx} ${(y + ty) / 2}, ${tx} ${ty}`
}
function mediaUrl(path: string | null) {
  return path ? fileUrl(props.pid, path) : ''
}
function isImage(asset: Asset) {
  return /\.(png|jpe?g|webp|bmp|gif)$/i.test(asset.path)
}
function assetName(asset: Asset) {
  return (
    props.bill?.items.find((item) => item.asset_id === asset.id)?.label ??
    asset.path.split('/').pop() ??
    asset.path
  )
}
function usage(item: ContextItem) {
  const frame = props.frames.find((frame) => frame.assetId === item.asset_id)
  if (frame) return frame.role === 'first_frame' ? '同首帧素材' : '同尾帧素材'
  return (
    { character_sheet: '人物外观', location_reference: '场景与光线', prop_reference: '道具' }[
      item.kind
    ] ??
    { video: '动作 / 运镜', audio: '声音参考' }[item.media ?? ''] ??
    '画面参考'
  )
}
function choose(key: string) {
  selected.value = key
  inspectorOpen.value = true
  emit('highlight', key)
}
function insert() {
  if (selectedRef.value?.valid) emit('reference', selectedRef.value)
}
function drop(event: DragEvent, role?: GraphFrame['role']) {
  const id = event.dataTransfer?.getData('application/x-shot-asset')
  if (!id || props.busy) return
  const asset = props.assets.find((a) => a.id === id)
  if (role) {
    if (props.frames.find((f) => f.role === role)?.locked || !asset || !isImage(asset)) return
    emit('frame', role, id)
  } else if (props.shot.kind !== 'transition') emit('add', id)
}
function startAsset(event: DragEvent, id: string) {
  event.dataTransfer?.setData('application/x-shot-asset', id)
}
function persist() {
  try {
    localStorage.setItem(
      `shot-graph:${props.pid}:${props.shot.id}`,
      JSON.stringify(positions.value),
    )
  } catch {
    /* 布局持久化不影响工程数据。 */
  }
}
function resetLayout() {
  positions.value = {}
  persist()
  fit()
}
function disconnectSelected() {
  if (!selectedItem.value) return
  emit('remove', selectedItem.value.key)
  choose('shot')
}
let drag: { key: string | null; x: number; y: number; originX: number; originY: number } | null =
  null
function startDrag(event: PointerEvent, key: string | null, x: number, y: number) {
  if (
    event.button !== 0 ||
    (key === null && (event.target as HTMLElement).closest('button,input,select,video,audio'))
  )
    return
  event.preventDefault()
  drag = { key, x: event.clientX, y: event.clientY, originX: x, originY: y }
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}
function move(event: PointerEvent) {
  if (!drag) return
  const factor = drag.key === null ? 1 : zoom.value
  const next = {
    x: drag.originX + (event.clientX - drag.x) / factor,
    y: drag.originY + (event.clientY - drag.y) / factor,
  }
  if (drag.key === null) offset.value = next
  else
    positions.value[drag.key] = {
      x: Math.max(0, Math.min(920, next.x)),
      y: Math.max(0, Math.min(canvasHeight.value - 145, next.y)),
    }
}
function endDrag() {
  if (drag?.key) persist()
  drag = null
}
function fit() {
  if (!viewport.value) return
  const bottom = Math.max(520, ...materials.value.map((item, index) => pos(item, index).y + 165))
  zoom.value = Math.min(
    1,
    Math.max(
      0.25,
      Math.min(
        (viewport.value.clientWidth - 36) / 1100,
        (viewport.value.clientHeight - 30) / bottom,
      ),
    ),
  )
  offset.value = { x: Math.max(12, (viewport.value.clientWidth - 1100 * zoom.value) / 2), y: 14 }
}
function focusReference(key: string) {
  const r = references.value.find((r) => r.key === key)
  choose(r?.target.startsWith('frame:') ? r.target.slice(6) : key)
  const index = materials.value.findIndex((item) => item.key === key)
  if (index >= 0 && viewport.value) {
    const p = pos(materials.value[index]!, index)
    offset.value = {
      x: viewport.value.clientWidth / 2 - (p.x + 110) * zoom.value,
      y: viewport.value.clientHeight / 2 - (p.y + 70) * zoom.value,
    }
  }
}
function highlightReference(key: string) {
  const r = references.value.find((r) => r.key === key)
  highlighted.value = r?.target.startsWith('frame:') ? r.target.slice(6) : key
}
defineExpose({ focusReference, highlightReference })
watch(
  () => props.shot.id,
  async () => {
    selected.value = 'shot'
    previewId.value = ''
    showEvidence.value = false
    try {
      const saved = JSON.parse(
        localStorage.getItem(`shot-graph:${props.pid}:${props.shot.id}`) ?? '{}',
      )
      positions.value = Object.fromEntries(
        Object.entries(saved).filter(
          ([, p]) =>
            p &&
            typeof p === 'object' &&
            'x' in p &&
            'y' in p &&
            Number.isFinite(p.x) &&
            Number.isFinite(p.y),
        ),
      ) as Record<string, { x: number; y: number }>
    } catch {
      positions.value = {}
    }
    await nextTick()
    fit()
  },
  { immediate: true },
)
watch(version, (v) => emit('preview', v), { immediate: true })
let resize: ResizeObserver | undefined
onMounted(() => {
  if (window.matchMedia('(max-width: 700px)').matches) {
    libraryOpen.value = false
    inspectorOpen.value = false
  }
  resize = new ResizeObserver(fit)
  if (viewport.value) resize.observe(viewport.value)
})
onBeforeUnmount(() => resize?.disconnect())
</script>

<template>
  <section class="shot-graph">
    <header class="graph-tools">
      <button @click="libraryOpen = !libraryOpen">{{ libraryOpen ? '收起素材' : '素材库' }}</button>
      <button @click="inspectorOpen = !inspectorOpen">
        {{ inspectorOpen ? '收起属性' : '属性 / 版本' }}
      </button>
      <span class="mode">{{ mode }}</span
      ><span class="subtle">拖入素材连接镜头 · 拖动卡片标题整理布局</span>
      <span class="spacer" />
      <button aria-label="缩小画布" @click="zoom = Math.max(0.25, zoom - 0.1)">−</button>
      <span>{{ Math.round(zoom * 100) }}%</span>
      <button aria-label="放大画布" @click="zoom = Math.min(1.5, zoom + 0.1)">＋</button>
      <button @click="fit">适应画布</button>
      <button @click="resetLayout">整理</button>
    </header>
    <div class="graph-body">
      <aside v-if="libraryOpen" class="asset-drawer">
        <div class="drawer-title">
          工程素材
          <button :disabled="busy || shot.kind === 'transition'" @click="emit('upload')">
            上传
          </button>
        </div>
        <input v-model="search" placeholder="搜索文件名或描述" aria-label="搜索工程素材" />
        <p class="subtle">拖到画布添加参考，拖到首尾帧设置画面。</p>
        <div class="asset-list">
          <div
            v-for="asset in library"
            :key="asset.id"
            class="library-card"
            :draggable="!busy"
            @dragstart="startAsset($event, asset.id)"
          >
            <img v-if="isImage(asset)" :src="mediaUrl(asset.path)" alt="" loading="lazy" />
            <span v-else class="media-icon">{{ asset.mime?.startsWith('audio') ? '♫' : '▶' }}</span>
            <div>
              <span :title="asset.path">{{ assetName(asset) }}</span
              ><button
                :disabled="busy || shot.kind === 'transition'"
                @click="emit('add', asset.id)"
              >
                ＋参考
              </button>
            </div>
          </div>
          <p v-if="!library.length" class="subtle">没有匹配的素材，可上传图片、视频或音频。</p>
        </div>
      </aside>
      <div
        ref="viewport"
        class="graph-viewport"
        @dragover.prevent
        @drop.prevent="drop($event)"
        @pointerdown.self="startDrag($event, null, offset.x, offset.y)"
        @pointermove="move"
        @pointerup="endDrag"
        @pointercancel="endDrag"
        @lostpointercapture="endDrag"
      >
        <div
          class="graph-canvas"
          :style="{
            height: `${canvasHeight}px`,
            transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})`,
          }"
          @pointerdown.self="startDrag($event, null, offset.x, offset.y)"
        >
          <svg
            class="connections"
            width="1100"
            :height="canvasHeight"
            aria-label="素材与镜头的连线"
          >
            <defs>
              <marker
                id="shot-arrow"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="5"
                markerHeight="5"
                orient="auto-start-reverse"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
              </marker>
            </defs>
            <path
              d="M 258 340 C 295 340, 310 340, 350 340"
              class="first-wire"
              marker-end="url(#shot-arrow)"
            />
            <text x="282" y="326" class="wire-label">起始画面</text>
            <path
              d="M 842 340 C 805 340, 790 340, 750 340"
              class="last-wire"
              marker-end="url(#shot-arrow)"
            />
            <text x="749" y="326" class="wire-label">结束画面</text>
            <template v-for="(item, index) in materials" :key="item.key">
              <path
                :d="
                  curve(
                    pos(item, index).x + 110,
                    pos(item, index).y + (index < 4 ? 140 : 0),
                    550,
                    index < 4 ? 190 : 490,
                  )
                "
                :class="{
                  selected: selected === item.key || highlighted === item.key,
                  invalid: item.over_capacity || item.missing_file,
                }"
                marker-end="url(#shot-arrow)"
              />
            </template>
          </svg>
          <article
            v-for="(frame, index) in frames"
            :key="frame.role"
            class="graph-node frame-node"
            :class="[
              {
                active: selected === frame.role || highlighted === frame.role,
                locked: frame.locked,
              },
              frame.role,
            ]"
            :style="{ left: `${index ? 842 : 38}px`, top: '230px' }"
            @click="choose(frame.role)"
            @dragover.prevent.stop
            @drop.prevent.stop="drop($event, frame.role)"
          >
            <header>
              {{ index ? '尾帧' : '首帧' }}
              <span>{{ index ? `${shot.duration.toFixed(2)}s` : '0.00s' }}</span>
            </header>
            <div class="frame-image">
              <img
                v-if="frame.path"
                :src="mediaUrl(frame.path)"
                :alt="index ? '尾帧' : '首帧'"
              /><span v-else>{{ frame.locked ? '等待上下游画面' : '拖入图片设置' }}</span>
            </div>
            <footer>
              {{ frame.locked ? '🔒 自动继承' : frame.assetId ? '已指定画面' : '未指定' }}
            </footer>
          </article>
          <article
            class="graph-node shot-node"
            :class="{ active: selected === 'shot' }"
            style="left: 350px; top: 190px"
            @click="choose('shot')"
          >
            <header>
              <strong>SHOT {{ String(shot.index_no).padStart(2, '0') }} · {{ shot.title }}</strong
              ><span>{{ shot.duration }}s</span>
            </header>
            <div class="shot-screen">
              <SegmentPlayer
                v-if="version?.video_path"
                :key="version.id"
                :src="mediaUrl(version.video_path)"
                :poster="mediaUrl(version.thumbnail_path ?? null)"
                :in-point="version.in_point"
                :out-point="version.out_point"
              />
              <img
                v-else-if="version?.thumbnail_path || frames[0]?.path"
                :src="mediaUrl(version?.thumbnail_path ?? frames[0]?.path ?? null)"
                alt="镜头画面"
              />
              <div v-else class="empty-shot">
                <span>▶</span>
                <p>从素材与提示词构筑这个镜头</p>
              </div>
            </div>
            <p v-if="jobStatus" class="job-status">最近任务 · {{ jobStatus }}</p>
            <p v-if="draftChanged" class="draft-warning">素材或提示词已修改，当前视频尚未更新</p>
            <footer>
              <span>{{
                version
                  ? `v${version.version_no} · ${version.is_current ? '当前采用' : '历史预览'}`
                  : '尚未生成'
              }}</span
              ><button :disabled="busy" @click.stop="emit('generate')">生成新版本 ↗</button>
            </footer>
          </article>
          <article
            v-for="(item, index) in materials"
            :key="item.key"
            class="graph-node material-node"
            :class="{
              active: selected === item.key || highlighted === item.key,
              invalid: item.over_capacity || item.missing_file,
            }"
            :style="{ left: `${pos(item, index).x}px`, top: `${pos(item, index).y}px` }"
            @click="choose(item.key)"
          >
            <header
              @pointerdown.stop="
                startDrag($event, item.key, pos(item, index).x, pos(item, index).y)
              "
            >
              <strong>{{ references.find((r) => r.key === item.key)?.alias ?? '素材' }}</strong
              ><span>{{ usage(item) }}</span>
            </header>
            <div class="material-image">
              <img
                v-if="(item.media ?? 'image') === 'image' && item.asset_path && !item.missing_file"
                :src="mediaUrl(item.asset_path)"
                :alt="item.label"
                loading="lazy"
              /><span v-else>{{
                item.missing_file
                  ? '文件缺失'
                  : item.media === 'audio'
                    ? '♫ 音频参考'
                    : '▶ 视频参考'
              }}</span>
            </div>
            <footer>
              <span :title="item.label">{{ item.label }}</span
              ><button
                :disabled="item.over_capacity || item.missing_file"
                @click.stop="
                  emit(
                    'reference',
                    references.find((r) => r.key === item.key)!,
                  )
                "
              >
                @ 引用
              </button>
            </footer>
            <div v-if="item.over_capacity" class="node-warning">超出容量 · 不会提交</div>
          </article>
        </div>
        <div v-if="bill && !bill.complete" class="graph-problems">
          <strong>生成前需要处理</strong>
          <p v-for="problem in bill.problems" :key="problem">{{ problem }}</p>
        </div>
      </div>
      <aside v-if="inspectorOpen" class="graph-inspector">
        <template v-if="selectedItem || selectedFrame">
          <div class="drawer-title">
            {{
              selectedFrame ? (selected === 'first_frame' ? '首帧' : '尾帧') : selectedItem?.label
            }}
          </div>
          <img
            v-if="selectedRef?.media === 'image' && selectedRef.path"
            class="inspector-image"
            :src="mediaUrl(selectedRef.path)"
            alt="素材预览"
          />
          <video
            v-if="selectedRef?.media === 'video' && selectedRef.path"
            :src="mediaUrl(selectedRef.path)"
            controls
            preload="metadata"
          />
          <audio
            v-if="selectedRef?.media === 'audio' && selectedRef.path"
            :src="mediaUrl(selectedRef.path)"
            controls
            preload="metadata"
          />
          <p>
            {{
              selectedFrame?.hint ??
              selectedItem?.desc ??
              '可在素材库中补充描述，帮助模型理解参考内容。'
            }}
          </p>
          <p class="subtle">{{ selectedRef?.reason }}</p>
          <button :disabled="!selectedRef?.valid" @click="insert">@ 插入提示词</button>
          <template v-if="selectedFrame">
            <label
              >选择图片<select
                :disabled="busy || selectedFrame.locked"
                :value="selectedFrame.assetId ?? ''"
                @change="
                  emit('frame', selectedFrame.role, ($event.target as HTMLSelectElement).value)
                "
              >
                <option value="">不指定</option>
                <option
                  v-for="asset in assets.filter((a) => !a.missing && isImage(a))"
                  :key="asset.id"
                  :value="asset.id"
                >
                  {{ assetName(asset) }}
                </option>
              </select></label
            >
          </template>
          <template v-else-if="selectedItem">
            <template v-if="(selectedItem.media ?? 'image') === 'image' && selectedItem.asset_id">
              <button
                :disabled="busy || frames[0]?.locked"
                @click="emit('frame', 'first_frame', selectedItem.asset_id)"
              >
                同时设为首帧
              </button>
              <button
                :disabled="busy || frames[1]?.locked"
                @click="emit('frame', 'last_frame', selectedItem.asset_id)"
              >
                同时设为尾帧
              </button>
            </template>
            <button class="danger" :disabled="busy" @click="disconnectSelected">
              断开参考连接
            </button>
          </template>
        </template>
        <template v-else>
          <div class="drawer-title">镜头与版本</div>
          <p v-if="version && !version.is_current" class="subtle">
            中心正在预览历史版本，周围连线与底部提示词仍为当前编辑草稿。此版输入请查看生成依据。
          </p>
          <label
            >预览版本<select v-model="previewId" aria-label="预览版本">
              <option value="">当前采用版本</option>
              <option v-for="v in versions" :key="v.id" :value="v.id">
                v{{ v.version_no }} · {{ v.status }}{{ v.is_current ? ' · 当前采用' : '' }}
              </option>
            </select></label
          >
          <button
            v-if="version && !version.is_current && version.asset_id"
            :disabled="busy"
            @click="emit('adopt', version.id)"
          >
            采用 v{{ version.version_no }}
          </button>
          <button v-if="version" @click="showEvidence = !showEvidence">
            {{ showEvidence ? '收起' : '查看' }}此版生成依据
          </button>
          <div v-if="showEvidence && version" class="evidence">
            <p>v{{ version.version_no }} · 冻结记录</p>
            <pre>{{
              version.params.prompt_sent ?? version.params.prompt ?? '手动导入版本没有生成提示词。'
            }}</pre>
            <pre v-if="version.params.pictures">{{
              JSON.stringify(version.params.pictures, null, 2)
            }}</pre>
          </div>
          <p class="subtle">{{ mode }} 是素材输入形态，实际可用能力由所选工作流决定。</p>
          <p class="subtle">点击素材查看用途，点击 @ 将它链接到提示词。</p>
        </template>
        <details v-if="omitted.length" class="omitted">
          <summary>未采用素材 · {{ omitted.length }}</summary>
          <p v-for="item in omitted" :key="item.key">
            <strong>{{ item.label }}</strong
            ><br />{{ item.reason }}
          </p>
        </details>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.shot-graph {
  display: flex;
  flex: 1;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  background: var(--color-base-0);
  border: 1px solid var(--color-line-1);
  font-size: 12px;
  color: var(--color-fg-2);
}
.graph-tools {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 36px;
  padding: 4px 10px;
  border-bottom: 1px solid var(--color-line-1);
  background: var(--color-base-1);
  flex-wrap: wrap;
}
.mode {
  color: var(--color-accent);
  font-size: 11px;
}
.spacer {
  flex: 1;
}
.subtle {
  color: var(--color-fg-3);
  font-size: 11px;
  line-height: 1.6;
}
button {
  cursor: pointer;
  border: 1px solid var(--color-line-2);
  border-radius: 4px;
  padding: 3px 8px;
  background: var(--color-base-2);
  color: var(--color-fg-2);
  font-size: 11px;
}
button:hover {
  border-color: var(--color-accent);
  color: var(--color-fg-1);
}
button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.graph-body {
  position: relative;
  display: flex;
  flex: 1;
  min-height: 0;
}
.asset-drawer {
  width: 176px;
  flex-shrink: 0;
  border-right: 1px solid var(--color-line-1);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow: hidden;
  background: var(--color-base-1);
}
.drawer-title {
  display: flex;
  justify-content: space-between;
  color: var(--color-fg-1);
  font-weight: 600;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
input,
select {
  width: 100%;
  padding: 5px;
  background: var(--color-base-0);
  border: 1px solid var(--color-line-2);
  border-radius: 4px;
  color: var(--color-fg-1);
  font-size: 11px;
}
.asset-list {
  overflow: auto;
  min-height: 0;
}
.library-card {
  border: 1px solid var(--color-line-1);
  border-radius: 5px;
  margin-bottom: 8px;
  overflow: hidden;
  cursor: grab;
}
.library-card img {
  width: 100%;
  height: 72px;
  object-fit: cover;
}
.media-icon {
  display: grid;
  place-items: center;
  height: 48px;
  font-size: 24px;
  background: var(--color-base-2);
}
.library-card > div {
  padding: 5px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.library-card span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10px;
}
.graph-viewport {
  position: relative;
  flex: 1;
  overflow: hidden;
  min-width: 0;
  min-height: 0;
  background-image: radial-gradient(var(--color-line-2) 1px, transparent 1px);
  background-size: 20px 20px;
  touch-action: none;
  cursor: grab;
}
.graph-canvas {
  position: absolute;
  width: 1100px;
  transform-origin: 0 0;
  pointer-events: none;
}
.connections {
  position: absolute;
  pointer-events: none;
  overflow: visible;
}
.connections path:not(defs path) {
  fill: none;
  stroke: #4a5967;
  stroke-width: 2;
}
.connections path.selected {
  stroke: var(--color-accent);
  stroke-width: 3;
}
.connections path.invalid {
  stroke: var(--color-st-review);
  stroke-dasharray: 6 5;
}
.connections .first-wire {
  stroke: #60a5fa !important;
}
.connections .last-wire {
  stroke: #c4a1f5 !important;
}
.wire-label {
  fill: var(--color-fg-3);
  font-size: 11px;
}
.graph-node {
  position: absolute;
  border: 1px solid var(--color-line-2);
  border-radius: 9px;
  overflow: hidden;
  background: var(--color-base-1);
  box-shadow: 0 6px 20px #0004;
  pointer-events: auto;
  cursor: default;
}
.graph-node.active {
  border-color: var(--color-accent);
  box-shadow: 0 0 0 2px #2dd4bf24;
}
.graph-node header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 10px;
  background: var(--color-base-2);
  font-size: 12px;
  color: var(--color-fg-1);
}
.graph-node header strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.graph-node header span {
  margin-left: auto;
  white-space: nowrap;
  font-size: 11px;
  color: var(--color-fg-3);
}
.graph-node footer {
  padding: 6px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
}
.graph-node footer > span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.frame-node,
.material-node {
  width: 220px;
}
.frame-node.first_frame {
  border-top: 3px solid #60a5fa;
}
.frame-node.last_frame {
  border-top: 3px solid #c4a1f5;
}
.frame-image {
  height: 126px;
  display: grid;
  place-items: center;
  background: #0c0e11;
  color: var(--color-fg-3);
}
.frame-image img {
  width: 100%;
  height: 126px;
  object-fit: contain;
}
.shot-node {
  width: 400px;
  border-top: 3px solid var(--color-accent);
}
.shot-screen {
  height: 225px;
  background: #080a0d;
  display: flex;
  align-items: center;
  justify-content: center;
}
.shot-screen > img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.shot-screen > :deep(div) {
  width: 100%;
}
.empty-shot {
  text-align: center;
  color: var(--color-fg-3);
  font-size: 12px;
}
.empty-shot > span {
  font-size: 32px;
}
.empty-shot p {
  margin-top: 12px;
}
.shot-node footer button {
  color: var(--color-accent);
}
.draft-warning,
.job-status {
  font-size: 10px;
  padding: 4px 9px;
  background: var(--color-base-2);
  color: var(--color-st-review);
}
.job-status {
  color: var(--color-st-queued);
}
.material-node header {
  cursor: grab;
  touch-action: none;
  padding: 6px 9px;
}
.material-image {
  height: 77px;
  display: grid;
  place-items: center;
  background: #0c0e11;
}
.material-image img {
  height: 77px;
  width: 100%;
  object-fit: cover;
}
.material-node footer {
  padding: 5px 8px;
}
.material-node footer button {
  padding: 2px 5px;
  color: var(--color-accent);
  white-space: nowrap;
}
.node-warning {
  color: var(--color-st-review);
  font-size: 10px;
  padding: 3px 8px;
}
.invalid {
  border-style: dashed;
}
.graph-inspector {
  width: 204px;
  padding: 12px;
  border-left: 1px solid var(--color-line-1);
  background: var(--color-base-1);
  overflow: auto;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  line-height: 1.6;
}
.graph-inspector label {
  display: block;
  font-size: 11px;
}
.graph-inspector select {
  margin-top: 5px;
}
.graph-inspector video,
.graph-inspector audio {
  width: 100%;
}
.inspector-image {
  width: 100%;
  max-height: 150px;
  object-fit: contain;
}
.danger {
  color: var(--color-st-failed);
}
.omitted {
  border-top: 1px solid var(--color-line-1);
  padding-top: 8px;
  font-size: 11px;
}
.omitted p {
  margin-top: 10px;
  color: var(--color-fg-3);
}
.evidence pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 10px;
  max-height: 240px;
  overflow: auto;
  background: var(--color-base-0);
  padding: 8px;
  margin-top: 6px;
}
.graph-problems {
  position: absolute;
  left: 10px;
  bottom: 10px;
  max-width: 420px;
  max-height: 100px;
  overflow: auto;
  background: var(--color-base-1);
  border: 1px solid var(--color-st-review);
  border-radius: 5px;
  padding: 8px;
  font-size: 10px;
  color: var(--color-st-review);
}
@media (max-width: 1150px) {
  .graph-inspector {
    width: 170px;
  }
  .asset-drawer {
    width: 145px;
  }
  .graph-tools > .subtle {
    display: none;
  }
}
@media (max-width: 850px) {
  .graph-inspector {
    width: 150px;
  }
  .asset-drawer {
    width: 125px;
  }
}
@media (max-width: 700px) {
  .asset-drawer,
  .graph-inspector {
    position: absolute;
    top: 0;
    bottom: 0;
    z-index: 20;
    width: 190px;
    box-shadow: 0 0 24px #0008;
  }
  .asset-drawer {
    left: 0;
  }
  .graph-inspector {
    right: 0;
  }
  .graph-tools {
    gap: 5px;
  }
  .graph-tools .mode {
    display: none;
  }
  .graph-problems {
    max-width: calc(100% - 20px);
    max-height: 62px;
  }
}
</style>
