<script setup lang="ts">
/**
 * 镜头编辑器（Step 6 / Step 7 的前端）。
 *
 * 这一页是「这条片段是怎么来的」的唯一答案页：左边是镜头本身与出场，
 * 中间是**上下文账单**——真正喂给模型的每一条参考，连没被采用的也列出来并写明理由，
 * 右边是版本轨（只增不改），底部是 prompt 与参数。
 *
 * 三个刻意的设计：
 *   1. **账单里没被采用的条目照样显示**。「为什么这张角色表没进去」比「进去了哪几张」
 *      更常是用户要问的问题，藏起来等于让人去猜。
 *   2. **入队被拒是正常结果**。上下文不完整时后端拒绝，页面把理由显示出来，
 *      同时留一颗「跳过检查强行入队」——它是显式选择，不是默认值。
 *   3. **手动导入的成片也走版本系统**。不接 AI 也能把工程做完（硬约束 2）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ListVideo, Play, RefreshCw, RotateCcw, Sparkles, Star, Upload, X } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import { fileUrl } from '@/shared/api/files'
import { ApiError } from '@/shared/api/client'
import { assetsApi, type Asset } from '@/shared/api/assets'
import { castApi, type AppearanceRow, type Character } from '@/shared/api/cast'
import { worldApi, type Prop } from '@/shared/api/world'
import {
  CONTEXT_KIND_LABEL,
  CONTEXT_ROLE_LABEL,
  type GenerationVersion,
} from '@/shared/api/generation'
import { SHOT_STATUS, SHOT_STATUS_LABEL } from '@/shared/api/story'
import { CAPABILITY_LABEL, type Capability } from '@/shared/api/workflows'
import { useConsoleStore } from '@/stores/console'
import { useShotStore } from '@/stores/shot'
import { useStoryStore } from '@/stores/story'
import { useWorkflowStore } from '@/stores/workflows'

const route = useRoute()
const router = useRouter()
const editor = useShotStore()
const story = useStoryStore()
const wf = useWorkflowStore()
const consolePanel = useConsoleStore()

const pid = computed(() => String(route.params.pid ?? ''))
const sid = computed(() => String(route.params.sid ?? ''))

/** 出场角色可选项：所有角色的所有形象拉平成一张表。 */
const appearances = ref<{ row: AppearanceRow; character: Character }[]>([])
const props_ = ref<Prop[]>([])
const assets = ref<Asset[]>([])
const sideError = ref<ApiError | null>(null)
const uploading = ref(false)
const versionInput = ref<HTMLInputElement | null>(null)
const contextInput = ref<HTMLInputElement | null>(null)

const shot = computed(() => editor.shot)
const bill = computed(() => editor.bill)
const assetById = computed(() => new Map(assets.value.map((a) => [a.id, a])))
const castIds = computed(() => new Set((shot.value?.cast ?? []).map((c) => c.appearance_id)))
const propState = computed(
  () => new Map((shot.value?.props ?? []).map((p) => [p.prop_id, p.state])),
)
/** 同一件事只报一次（后端重启后两边都会 404「项目未打开」）。 */
const showSideError = computed(
  () => sideError.value !== null && sideError.value.code !== editor.lastError?.code,
)

/** 分镜板拉平成一张镜头清单，供顶部下拉在镜头之间跳。 */
const allShots = computed(() =>
  story.lanes.flatMap((l) =>
    l.shots.map((s) => ({ id: s.id, label: `${s.index_no}. ${s.title} · ${l.title}` })),
  ),
)

const readyWorkflows = computed(() => wf.list.filter((w) => w.status === 'ready'))

function thumb(assetId: string | null): string {
  if (!assetId) return ''
  const asset = assetById.value.get(assetId)
  if (!asset || asset.missing) return ''
  return fileUrl(pid.value, asset.path)
}

/**
 * 版本轨上那一格：**视频走 `<video>`、图片走 `<img>`**。两个字段由后端分开给
 * （`generation._version_media`），因为版本的资产几乎总是一段 `.mp4`——
 * 把它塞进 `<img>` 只会得到一个坏图标。
 */
function versionVideo(v: GenerationVersion): string {
  return v.video_path ? fileUrl(pid.value, v.video_path) : ''
}

function versionPoster(v: GenerationVersion): string {
  return v.thumbnail_path ? fileUrl(pid.value, v.thumbnail_path) : ''
}

async function loadSide(): Promise<void> {
  if (!pid.value) return
  try {
    const [chars, propRows, assetRows] = await Promise.all([
      castApi.characters(pid.value),
      worldApi.props(pid.value),
      assetsApi.list(pid.value),
    ])
    const nested = await Promise.all(
      chars.map(async (c) => ({ c, rows: await castApi.appearances(pid.value, c.id) })),
    )
    appearances.value = nested.flatMap(({ c, rows }) => rows.map((row) => ({ row, character: c })))
    props_.value = propRows
    assets.value = assetRows
    sideError.value = null
  } catch (err) {
    sideError.value = err instanceof ApiError ? err : null
  }
}

async function reload(): Promise<void> {
  if (!pid.value) return
  await Promise.all([
    editor.load(pid.value, sid.value).catch(() => {}),
    story.loadBoard(pid.value).catch(() => {}),
    wf.load(pid.value).catch(() => {}),
    loadSide(),
  ])
}

onMounted(reload)
watch([pid, sid], () => {
  editor.load(pid.value, sid.value).catch(() => {})
})

/** 没带 sid 时落到第一个镜头，URL 也跟着变——刷新后还在同一个镜头上。 */
watch(allShots, (list) => {
  if (!sid.value && list.length) {
    void router.replace({ name: 'shot', params: { pid: pid.value, sid: list[0]?.id } })
  }
})

function goShot(shotId: string): void {
  void router.push({ name: 'shot', params: { pid: pid.value, sid: shotId } })
}

function fmt(n: number | null | undefined): string {
  return n === null || n === undefined ? '—' : `${Math.round(n * 10) / 10}s`
}

async function saveText(key: 'prompt' | 'negative_prompt' | 'description', value: string) {
  await editor.save(pid.value, { [key]: value || null }).catch(() => {})
}

async function saveNumber(key: 'seed' | 'steps' | 'duration', value: string) {
  const n = Number(value)
  if (value !== '' && !Number.isFinite(n)) return
  await editor.save(pid.value, { [key]: value === '' ? null : n }).catch(() => {})
}

async function saveField(
  key: 'title' | 'camera' | 'movement' | 'status' | 'workflow_id' | 'prev_shot_id',
  value: string,
) {
  const nullable = key !== 'title' && key !== 'status'
  await editor.save(pid.value, { [key]: nullable ? value || null : value }).catch(() => {})
}

async function toggleCast(appearanceId: string): Promise<void> {
  const next = new Set(castIds.value)
  if (next.has(appearanceId)) next.delete(appearanceId)
  else next.add(appearanceId)
  await editor.setCast(pid.value, [...next]).catch(() => {})
}

async function setPropState(propId: string, state: string): Promise<void> {
  const items = (shot.value?.props ?? [])
    .filter((p) => p.prop_id !== propId)
    .map((p) => ({ prop_id: p.prop_id, state: p.state }))
  if (state) items.push({ prop_id: propId, state })
  await editor.setProps(pid.value, items).catch(() => {})
}

/** 上传一张图并直接挂进上下文（`manual` 优先级最高）。 */
async function onPickContextFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  uploading.value = true
  try {
    const asset = await assetsApi.upload(pid.value, file, 'upload')
    await editor.override(pid.value, { action: 'add', asset_id: asset.id, label: file.name })
    await loadSide()
  } catch (err) {
    sideError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

/** 手动导入一个成片版本：不生成也能把工程做完。 */
async function onPickVersionFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  uploading.value = true
  try {
    const video = file.type.startsWith('video')
    // 手动导入的不是生成物，落 assets/uploads 而不是 generations/
    const asset = await assetsApi.upload(pid.value, file, 'upload')
    await editor.addVersion(pid.value, asset.id, video ? 'video' : 'image')
    await loadSide()
  } catch (err) {
    sideError.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
  }
}

async function generate(skipContext: boolean): Promise<void> {
  const job = await editor.enqueue(pid.value, {
    workflowId: shot.value?.workflow_id ?? null,
    checkContext: !skipContext,
  })
  if (job) await story.loadBoard(pid.value).catch(() => {})
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />
    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1.5 border-b px-2">
      <span class="text-fg-4 text-2xs">镜头</span>
      <select
        :value="sid"
        class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 w-60 border px-1 text-2xs outline-none"
        @change="goShot(($event.target as HTMLSelectElement).value)"
      >
        <option value="">未选择</option>
        <option v-for="s in allShots" :key="s.id" :value="s.id">{{ s.label }}</option>
      </select>
      <AppButton
        size="sm"
        variant="primary"
        :disabled="!shot || editor.busy"
        title="按当前上下文与 Workflow 入队生成一个新版本；旧版本一条都不会被覆盖"
        @click="generate(false)"
      >
        <Sparkles :size="10" />生成
      </AppButton>
      <AppButton
        size="sm"
        :disabled="!shot || editor.busy || (bill?.complete ?? false)"
        title="上下文不完整时仍然入队。这是显式选择：出来的东西可能缺参考，但有时你就是想先看一眼"
        @click="generate(true)"
      >
        <Play :size="10" />跳过检查入队
      </AppButton>
      <AppButton
        size="sm"
        variant="ghost"
        title="在底部控制台的任务框里看它跑到哪了（不用离开这一页）"
        @click="consolePanel.openWith('jobs')"
      >
        <ListVideo :size="10" />任务
      </AppButton>
      <span v-if="editor.lastJob" class="text-fg-4 text-2xs">
        最近入队 {{ editor.lastJob.kind }} · {{ editor.lastJob.status }}
      </span>
      <AppButton
        size="sm"
        variant="ghost"
        class="ml-auto"
        :disabled="editor.busy"
        @click="reload()"
      >
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>

    <ErrorPanel
      v-if="editor.lastError"
      class="mx-2 mt-2"
      :error="editor.lastError"
      @dismiss="editor.clearError()"
    />
    <ErrorPanel
      v-if="showSideError"
      class="mx-2 mt-2"
      :error="sideError"
      @dismiss="sideError = null"
    />
    <EmptyState
      v-if="!shot"
      class="flex-1"
      title="尚无选中镜头"
      body="上面的下拉里选一个镜头，或先去剧本 / 分镜页建几个。这一页回答的是「这条片段是怎么来的」。"
    />
    <template v-else>
      <div class="flex min-h-0 flex-1 gap-2 p-2">
        <!-- 左：镜头信息 + 出场 -->
        <AppPanel title="镜头信息" class="w-64 shrink-0">
          <div class="space-y-3 p-2">
            <section>
              <p class="text-fg-3 text-2xs tracking-wide uppercase">
                第 {{ shot.scene_index_no }} 场 · {{ shot.scene_title }}
              </p>
              <div class="mt-1 space-y-1">
                <label class="block">
                  <span class="text-fg-4 text-2xs">标题</span>
                  <input
                    :value="shot.title"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                    @change="saveField('title', ($event.target as HTMLInputElement).value)"
                  />
                </label>
                <div class="grid grid-cols-2 gap-1">
                  <label class="block">
                    <span class="text-fg-4 text-2xs">时长（秒）</span>
                    <input
                      :value="shot.duration"
                      type="number"
                      min="0.1"
                      step="0.1"
                      class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                      @change="saveNumber('duration', ($event.target as HTMLInputElement).value)"
                    />
                  </label>
                  <label class="block">
                    <span class="text-fg-4 text-2xs">状态</span>
                    <select
                      :value="shot.status"
                      class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                      @change="saveField('status', ($event.target as HTMLSelectElement).value)"
                    >
                      <option v-for="s in SHOT_STATUS" :key="s" :value="s">
                        {{ SHOT_STATUS_LABEL[s] }}
                      </option>
                    </select>
                  </label>
                  <label class="block">
                    <span class="text-fg-4 text-2xs">机位</span>
                    <input
                      :value="shot.camera ?? ''"
                      placeholder="中景 / 特写"
                      class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                      @change="saveField('camera', ($event.target as HTMLInputElement).value)"
                    />
                  </label>
                  <label class="block">
                    <span class="text-fg-4 text-2xs">运镜</span>
                    <input
                      :value="shot.movement ?? ''"
                      placeholder="推 / 摇 / 固定"
                      class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                      @change="saveField('movement', ($event.target as HTMLInputElement).value)"
                    />
                  </label>
                </div>
                <label class="block">
                  <span class="text-fg-4 text-2xs">上游镜头（首尾帧连续性）</span>
                  <select
                    :value="shot.prev_shot_id ?? ''"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                    @change="saveField('prev_shot_id', ($event.target as HTMLSelectElement).value)"
                  >
                    <option value="">不接上游</option>
                    <option
                      v-for="s in allShots.filter((x) => x.id !== shot!.id)"
                      :key="s.id"
                      :value="s.id"
                    >
                      {{ s.label }}
                    </option>
                  </select>
                </label>
                <p class="text-fg-4 text-2xs">
                  接了上游，本镜头会等它出当前版本再跑——队列里那条等待会写明原因，不是卡住。
                </p>
              </div>
            </section>

            <section class="border-line-1 border-t pt-2">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">出场角色形象</p>
              <p v-if="appearances.length === 0" class="text-fg-4 mt-1 text-2xs">
                还没有角色形象。先去角色页建一个——没有角色的镜头上下文不完整，入队会被拒。
              </p>
              <ul v-else class="mt-1 space-y-px">
                <li v-for="a in appearances" :key="a.row.id">
                  <label class="hover:bg-base-2 flex items-center gap-1 px-0.5 py-0.5">
                    <input
                      type="checkbox"
                      :checked="castIds.has(a.row.id)"
                      class="accent-accent"
                      @change="toggleCast(a.row.id)"
                    />
                    <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">
                      {{ a.character.name }} · {{ a.row.name }}
                    </span>
                    <AppBadge
                      v-if="!a.row.current_sheet"
                      tone="warn"
                      title="这个形象还没有角色表，进不了上下文"
                    >
                      无角色表
                    </AppBadge>
                  </label>
                </li>
              </ul>
            </section>

            <section class="border-line-1 border-t pt-2">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">道具</p>
              <p v-if="props_.length === 0" class="text-fg-4 mt-1 text-2xs">还没有道具。</p>
              <ul v-else class="mt-1 space-y-px">
                <li v-for="p in props_" :key="p.id" class="flex items-center gap-1">
                  <span class="text-fg-2 min-w-0 flex-1 truncate text-2xs">{{ p.name }}</span>
                  <select
                    :value="propState.get(p.id) ?? ''"
                    class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 w-20 border px-1 text-2xs outline-none"
                    @change="setPropState(p.id, ($event.target as HTMLSelectElement).value)"
                  >
                    <option value="">不出场</option>
                    <option value="present">在场</option>
                    <option value="discarded">已丢弃</option>
                  </select>
                </li>
              </ul>
              <p class="text-fg-4 mt-1 text-2xs">
                标成「已丢弃」的道具不会进上下文——连续性检查靠它判断伞是什么时候没的。
              </p>
            </section>
          </div>
        </AppPanel>

        <!-- 中：上下文账单。没被采用的照样列出来，理由写在旁边 -->
        <AppPanel title="上下文检查器" class="min-w-0 flex-1">
          <template #actions>
            <span v-if="bill" class="text-fg-4 tnum text-2xs">
              {{ bill.included_count }} / {{ bill.limit }} 张参考
            </span>
            <AppButton
              size="sm"
              variant="ghost"
              :disabled="uploading || editor.busy"
              title="上传一张图直接挂进上下文，优先级最高（手动添加）"
              @click="contextInput?.click()"
            >
              <Upload :size="10" />加图
            </AppButton>
            <AppButton
              size="sm"
              variant="ghost"
              :disabled="editor.busy"
              title="丢掉所有人工干预，回到自动解析的结果"
              @click="editor.override(pid, { action: 'reset' }).catch(() => {})"
            >
              <RotateCcw :size="10" />恢复自动
            </AppButton>
            <input
              ref="contextInput"
              type="file"
              accept="image/*"
              class="hidden"
              @change="onPickContextFile"
            />
          </template>
          <div class="min-h-0 flex-1 overflow-auto p-2">
            <div
              v-if="bill && !bill.complete"
              class="border-st-failed/40 bg-base-2 mb-2 border p-1.5"
            >
              <p class="text-st-review text-2xs">上下文不完整，直接「生成」会被后端拒掉：</p>
              <ul class="text-fg-2 mt-0.5 space-y-px text-2xs">
                <li v-for="p in bill.problems" :key="p">· {{ p }}</li>
              </ul>
            </div>
            <p v-else-if="bill" class="text-st-done mb-2 text-2xs">
              上下文完整{{ bill.at_limit ? '（已到参考图上限，多出来的会被省略）' : '' }}。
            </p>

            <p class="text-fg-3 text-2xs tracking-wide uppercase">
              已采用（{{ editor.included.length }}）
            </p>
            <EmptyState
              v-if="editor.included.length === 0"
              title="一条参考都没有"
              body="给镜头挂上出场角色、给所在场景选一个地点变体，或者直接「加图」——账单会立刻重算。"
            />
            <ul v-else class="mt-1 grid grid-cols-3 gap-1.5">
              <li
                v-for="item in editor.included"
                :key="item.key"
                class="border-line-1 bg-base-2 border"
              >
                <div class="bg-base-3 flex h-20 items-center justify-center overflow-hidden">
                  <img
                    v-if="thumb(item.asset_id)"
                    :src="thumb(item.asset_id)"
                    class="max-h-full max-w-full object-contain"
                    :alt="item.label"
                  />
                  <span v-else class="text-fg-4 text-2xs">
                    {{ item.missing_file ? '文件不在磁盘上' : '无图' }}
                  </span>
                </div>
                <div class="p-1">
                  <div class="flex items-center gap-1">
                    <!-- 当首帧还是当参考图：规则只在后端（context.py::_assign_roles），这里只标 -->
                    <AppBadge :tone="item.role === 'first_frame' ? 'ok' : 'accent'">
                      {{ CONTEXT_ROLE_LABEL[item.role ?? ''] ?? '参考图' }}
                    </AppBadge>
                    <AppBadge tone="neutral">
                      {{ CONTEXT_KIND_LABEL[item.kind] ?? item.kind }}
                    </AppBadge>
                    <AppBadge v-if="item.manual" tone="warn">人工</AppBadge>
                    <span class="text-fg-4 tnum ml-auto text-2xs">P{{ item.priority }}</span>
                    <button
                      class="text-fg-4 hover:text-st-failed"
                      title="从这次上下文里移除（记成人工覆写，可「恢复自动」撤销）"
                      @click="
                        editor.override(pid, { action: 'remove', key: item.key }).catch(() => {})
                      "
                    >
                      <X :size="10" />
                    </button>
                  </div>
                  <p class="text-fg-2 mt-0.5 truncate text-2xs" :title="item.label">
                    {{ item.label }}
                  </p>
                  <p class="text-fg-4 truncate text-2xs" :title="item.reason">{{ item.reason }}</p>
                </div>
              </li>
            </ul>

            <p class="text-fg-3 mt-3 text-2xs tracking-wide uppercase">
              未采用（{{ editor.omitted.length }}）
            </p>
            <p v-if="editor.omitted.length === 0" class="text-fg-4 mt-1 text-2xs">
              没有被省略的条目。
            </p>
            <ul v-else class="mt-1 space-y-px">
              <li
                v-for="item in editor.omitted"
                :key="item.key"
                class="border-line-1 hover:bg-base-2 flex items-center gap-1.5 border px-1 py-0.5 opacity-70"
              >
                <AppBadge tone="neutral">
                  {{ CONTEXT_KIND_LABEL[item.kind] ?? item.kind }}
                </AppBadge>
                <span class="text-fg-2 min-w-0 shrink-0 truncate text-2xs">{{ item.label }}</span>
                <span class="text-fg-4 min-w-0 flex-1 truncate text-2xs" :title="item.reason">
                  {{ item.reason }}
                </span>
                <AppBadge v-if="item.missing_file" tone="fail">文件丢失</AppBadge>
              </li>
            </ul>
            <p class="text-fg-4 mt-2 text-2xs">
              没被采用的也列在这儿：「为什么这张角色表没进去」比「进去了哪几张」更常是要问的问题。
            </p>
          </div>
        </AppPanel>
        <!-- 右：版本轨。只增不改，换当前版本是唯一的「修改」 -->
        <AppPanel title="版本轨" class="w-56 shrink-0">
          <template #actions>
            <AppButton
              size="sm"
              variant="ghost"
              :disabled="uploading || editor.busy"
              title="手动导入一个成片版本：不接 AI 也能把工程做完"
              @click="versionInput?.click()"
            >
              <Upload :size="10" />导入
            </AppButton>
            <input
              ref="versionInput"
              type="file"
              accept="image/*,video/*"
              class="hidden"
              @change="onPickVersionFile"
            />
          </template>
          <div class="p-2">
            <EmptyState
              v-if="editor.versions.length === 0"
              title="还没有任何版本"
              body="「生成」入队一个，或者「导入」一个已有的成片。版本只增不改，旧的一条都不会被覆盖。"
            />
            <ul v-else class="space-y-1">
              <li
                v-for="v in editor.versions"
                :key="v.id"
                class="border p-1"
                :class="
                  v.is_current
                    ? 'border-accent/60 bg-accent-dim/40'
                    : 'border-line-1 bg-base-2 hover:bg-base-3'
                "
              >
                <div class="flex items-center gap-1">
                  <span class="text-fg-1 tnum text-2xs">v{{ v.version_no }}</span>
                  <AppBadge :tone="v.source === 'manual' ? 'neutral' : 'accent'">
                    {{ v.source === 'manual' ? '手动' : '生成' }}
                  </AppBadge>
                  <AppBadge v-if="v.status !== 'done'" tone="warn">{{ v.status }}</AppBadge>
                  <button
                    v-if="!v.is_current"
                    class="text-fg-4 hover:text-accent ml-auto"
                    title="设为当前版本（下游镜头取末帧、时间线取片段都用它）"
                    @click="editor.setCurrent(pid, v.id).catch(() => {})"
                  >
                    <Star :size="10" />
                  </button>
                  <Star v-else :size="10" class="text-accent ml-auto" />
                </div>
                <!-- 视频给播放器，图片才走 <img>：两个字段绝不混用 -->
                <div
                  v-if="versionVideo(v) || versionPoster(v)"
                  class="bg-base-3 mt-1 flex h-16 items-center justify-center overflow-hidden"
                >
                  <video
                    v-if="versionVideo(v)"
                    :src="versionVideo(v)"
                    :poster="versionPoster(v) || undefined"
                    controls
                    preload="metadata"
                    class="max-h-full max-w-full"
                  />
                  <img
                    v-else
                    :src="versionPoster(v)"
                    class="max-h-full max-w-full object-contain"
                    :alt="`v${v.version_no}`"
                  />
                </div>
                <p class="text-fg-4 mt-0.5 text-2xs">
                  {{ v.kind }} · {{ fmt(v.duration) }} · {{ v.created_at.slice(0, 16) }}
                </p>
                <p v-if="v.error" class="text-st-review mt-0.5 text-2xs">这个版本是失败现场</p>
              </li>
            </ul>
          </div>
        </AppPanel>
      </div>
      <!-- 底：prompt 与参数。这些值会在入队那一刻被冻结进版本里 -->
      <div class="border-line-1 bg-base-1 shrink-0 border-t p-2">
        <div class="flex gap-2">
          <label class="min-w-0 flex-1">
            <span class="text-fg-4 text-2xs">Prompt</span>
            <textarea
              :value="shot.prompt ?? ''"
              rows="3"
              placeholder="这条镜头要画什么。上下文里的参考图会和它一起喂给模型。"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px w-full resize-none border px-1.5 py-1 text-2xs outline-none"
              @change="saveText('prompt', ($event.target as HTMLTextAreaElement).value)"
            />
          </label>
          <label class="min-w-0 flex-1">
            <span class="text-fg-4 text-2xs">Negative Prompt</span>
            <textarea
              :value="shot.negative_prompt ?? ''"
              rows="3"
              placeholder="不要出现的东西"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-px w-full resize-none border px-1.5 py-1 text-2xs outline-none"
              @change="saveText('negative_prompt', ($event.target as HTMLTextAreaElement).value)"
            />
          </label>
          <div class="w-64 shrink-0 space-y-1">
            <label class="block">
              <span class="text-fg-4 text-2xs">Workflow（决定用哪套图与参数）</span>
              <select
                :value="shot.workflow_id ?? ''"
                class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1 text-2xs outline-none"
                @change="saveField('workflow_id', ($event.target as HTMLSelectElement).value)"
              >
                <option value="">用该能力的默认 Workflow</option>
                <option v-for="w in readyWorkflows" :key="w.id" :value="w.id">
                  {{ w.name }} · {{ CAPABILITY_LABEL[w.capability as Capability] ?? w.capability }}
                </option>
              </select>
            </label>
            <div class="grid grid-cols-2 gap-1">
              <label class="block">
                <span class="text-fg-4 text-2xs">Seed（空 = 随机）</span>
                <input
                  :value="shot.seed ?? ''"
                  type="number"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveNumber('seed', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">Steps</span>
                <input
                  :value="shot.steps ?? ''"
                  type="number"
                  min="1"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 tnum mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveNumber('steps', ($event.target as HTMLInputElement).value)"
                />
              </label>
            </div>
            <p v-if="readyWorkflows.length === 0" class="text-fg-4 text-2xs">
              还没有校验通过的 Workflow。去 Workflow 页导入并绑定一个——不然入队会说不出该用哪套图。
            </p>
            <p v-else class="text-fg-4 text-2xs">
              入队那一刻这些值会被冻结进版本里，之后再改不影响已经出过的结果。
            </p>
          </div>
          <label class="w-56 shrink-0">
            <span class="text-fg-4 text-2xs">镜头描述（给人看的，不进 prompt）</span>
            <textarea
              :value="shot.description ?? ''"
              rows="3"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px w-full resize-none border px-1.5 py-1 text-2xs outline-none"
              @change="saveText('description', ($event.target as HTMLTextAreaElement).value)"
            />
          </label>
        </div>
      </div>
    </template>
  </div>
</template>
