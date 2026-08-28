<script setup lang="ts">
/**
 * 剧本工作台（Step 5 的前端）。
 *
 * 四条与后端一致的规矩，UI 上必须看得出来：
 *   1. **AI 是右栏那个协作者，不是一个「一键拆解」按钮**。它自己用 `read_script` 一段一段
 *      地读左栏的原文，每次只就读到的那一段提案——这就是「一次性拆解会超时」的解药。
 *      提案一个字都不写库，逐条审阅、按「采用」才落。
 *      老的一次性拆解（`breakdown/propose`）后端原样保留当兼容路径，这一页不再有入口。
 *   2. **剧本页与幕流程图共用同一个会话**（同一份 `DirectorTurn`）：在这里聊到一半去流程图
 *      页，看到的是同一段对话。`scope` 只让后端多说一句「用户现在在剧本页」。
 *   3. **序号是时间顺序**——Scene / Shot 的 index_no 由后端重排，前端只提交顺序。
 *   4. Scene 挂的是**地点变体**（雨夜 / 白天），不是地点本身，所以下拉框按地点分组。
 *
 * 已落库的每个 Shot 都可以通过右侧编辑按钮在弹窗里查看剧情详情、AI 选中的角色和生成 Prompt；
 * 需要上下文账单、版本轨等高级操作时，再进入单 Shot 工作台。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChevronDown,
  ChevronUp,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Sparkles,
  Trash2,
} from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import DirectorPanel from '../director/DirectorPanel.vue'
import { castApi, type AppearanceRow, type Character } from '@/shared/api/cast'
import { useStoryStore } from '@/stores/story'
import { useWorldStore } from '@/stores/world'

const route = useRoute()
const router = useRouter()
const story = useStoryStore()
const world = useWorldStore()

const pid = computed(() => String(route.params.pid ?? ''))

/** 原文是本地草稿：改了不自动存，避免每敲一个字打一次后端。 */
const draftText = ref('')
const draftTitle = ref('')
const dirty = computed(
  () =>
    story.story !== null &&
    (draftText.value !== story.story.raw_text || draftTitle.value !== story.story.title),
)

const newSceneTitle = ref('')
const newShotTitle = ref('')

/** 右栏两个 Tab：跟 AI 聊 / 改这一场的属性。 */
const rightTab = ref<'ai' | 'scene'>('ai')
const RIGHT_TABS = [
  { key: 'ai', label: 'AI 编剧' },
  { key: 'scene', label: '场景属性' },
] as const

/**
 * 剧本页的快捷句。点一下只是填进输入框——发出去之前用户还能改。
 * 「继续」这一条是关键：agent 上一轮会说清原文读到第几个字，说「继续」它就接着往下拆。
 */
const QUICK_ACTIONS = [
  '从头开始拆这个剧本',
  '继续拆下一段',
  '给第 1 幕补齐镜头 prompt',
  '按首尾帧规范重写这一幕的 prompt',
]

type ShotDraft = {
  title: string
  description: string
  duration: number
  camera: string
  movement: string
  prompt: string
  negative_prompt: string
}

const shotDraft = ref<ShotDraft | null>(null)
const shotCastIds = ref<string[]>([])
const shotEditorBusy = ref(false)
const shotEditorOpen = ref(false)
const appearances = ref<{ row: AppearanceRow; character: Character }[]>([])

const shotDirty = computed(() => {
  const current = story.shot
  const draft = shotDraft.value
  if (!current || !draft) return false
  return (
    draft.title !== current.title ||
    draft.description !== (current.description ?? '') ||
    draft.duration !== current.duration ||
    draft.camera !== (current.camera ?? '') ||
    draft.movement !== (current.movement ?? '') ||
    draft.prompt !== (current.prompt ?? '') ||
    draft.negative_prompt !== (current.negative_prompt ?? '') ||
    shotCastIds.value.slice().sort().join(',') !==
      current.cast.map((row) => row.appearance_id).slice().sort().join(',')
  )
})

function syncDraft(): void {
  draftText.value = story.story?.raw_text ?? ''
  draftTitle.value = story.story?.title ?? ''
}

function syncShotDraft(): void {
  const current = story.shot
  if (!current) {
    shotDraft.value = null
    shotCastIds.value = []
    return
  }
  shotDraft.value = {
    title: current.title,
    description: current.description ?? '',
    duration: current.duration,
    camera: current.camera ?? '',
    movement: current.movement ?? '',
    prompt: current.prompt ?? '',
    negative_prompt: current.negative_prompt ?? '',
  }
  shotCastIds.value = current.cast.map((row) => row.appearance_id)
}

/**
 * 选中场景里的镜头。
 *
 * 镜头列表只在分镜板接口里（`GET /storyboard` 的泳道），Scene 列表本身只带 shot_count，
 * 所以这一页也拉一次分镜板，取对应那条泳道——不在前端另攒一份镜头缓存。
 */
const sceneShots = computed(
  () => story.lanes.find((l) => l.id === story.selectedSceneId)?.shots ?? [],
)

async function reload(): Promise<void> {
  if (!pid.value) return
  await story.load(pid.value).catch(() => {})
  await story.loadBoard(pid.value).catch(() => {})
  await world.loadWorld(pid.value).catch(() => {})
  await loadAppearances()
  syncDraft()
}

onMounted(reload)
watch(pid, reload)
watch(
  () => story.shot,
  () => {
    if (!shotEditorBusy.value) syncShotDraft()
  },
  { immediate: true },
)

async function loadAppearances(): Promise<void> {
  if (!pid.value) return
  try {
    const chars = await castApi.characters(pid.value)
    const nested = await Promise.all(
      chars.map(async (character) => ({
        character,
        rows: await castApi.appearances(pid.value, character.id),
      })),
    )
    appearances.value = nested.flatMap(({ character, rows }) =>
      rows.map((row) => ({ row, character })),
    )
  } catch {
    appearances.value = []
  }
}

async function openShotEditor(shotId: string): Promise<void> {
  if (shotDirty.value) {
    await saveShotEditor()
    if (shotDirty.value) return
  }
  if (story.selectedShotId !== shotId || story.shot?.id !== shotId) {
    await story.selectShot(pid.value, shotId).catch(() => {})
  }
  if (story.shot?.id === shotId) shotEditorOpen.value = true
}

async function setShotEditorOpen(open: boolean): Promise<void> {
  if (open) {
    shotEditorOpen.value = true
    return
  }
  if (shotDirty.value) syncShotDraft()
  shotEditorOpen.value = false
}

function toggleShotAppearance(appearanceId: string): void {
  shotCastIds.value = shotCastIds.value.includes(appearanceId)
    ? shotCastIds.value.filter((id) => id !== appearanceId)
    : [...shotCastIds.value, appearanceId]
}

async function saveShotEditor(): Promise<void> {
  const current = story.shot
  const draft = shotDraft.value
  if (!current || !draft) return
  const castIds = [...shotCastIds.value]
  shotEditorBusy.value = true
  try {
    await story.updateShot(pid.value, current.id, {
      title: draft.title.trim() || current.title,
      description: draft.description || null,
      duration: Number.isFinite(Number(draft.duration))
        ? Math.max(0.1, Number(draft.duration))
        : current.duration,
      camera: draft.camera || null,
      movement: draft.movement || null,
      prompt: draft.prompt || null,
      negative_prompt: draft.negative_prompt || null,
    })
    await story.setCast(pid.value, current.id, castIds)
    syncShotDraft()
  } catch {
    // store 已经把可展示的 API 错误写进 lastError，保留草稿让用户修正后重试。
  } finally {
    shotEditorBusy.value = false
  }
}

async function saveText(): Promise<void> {
  await story
    .saveStory(pid.value, { title: draftTitle.value.trim(), raw_text: draftText.value })
    .catch(() => {})
  syncDraft()
}

/**
 * 新建一场。**名字空着不是「按了没反应」**——补一个「第 N 场」的默认名照常建，
 * 和幕流程图的「加一幕」同一个口径。按钮按下去什么都不发生，最像功能坏了。
 */
async function createScene(): Promise<void> {
  const title = newSceneTitle.value.trim() || `第 ${story.scenes.length + 1} 场`
  newSceneTitle.value = ''
  await story.createScene(pid.value, { title }).catch(() => {})
}

/** 同上；镜头必须挂在某一场下，所以没选中场景时按钮是禁用的（tooltip 里写了原因）。 */
async function createShot(): Promise<void> {
  const sid = story.selectedSceneId
  if (!sid) return
  const title = newShotTitle.value.trim() || `镜头 ${sceneShots.value.length + 1}`
  newShotTitle.value = ''
  await story.createShot(pid.value, sid, { title }).catch(() => {})
  // createShot 只回一条镜头，泳道要重新拉才能看到它
  await story.loadBoard(pid.value).catch(() => {})
}

async function removeShot(shotId: string): Promise<void> {
  await story.removeShot(pid.value, shotId).catch(() => {})
}

async function removeScene(sid: string): Promise<void> {
  await story.removeScene(pid.value, sid).catch(() => {})
  await story.loadBoard(pid.value).catch(() => {})
}

async function moveScene(sid: string, delta: number): Promise<void> {
  await story.moveScene(pid.value, sid, delta).catch(() => {})
  await story.loadBoard(pid.value).catch(() => {})
}

async function saveScene(
  key: 'title' | 'summary' | 'time_of_day' | 'notes',
  value: string,
): Promise<void> {
  const sid = story.selectedSceneId
  if (!sid) return
  await story
    .updateScene(pid.value, sid, { [key]: key === 'title' ? value : value || null })
    .catch(() => {})
}

async function setVariant(value: string): Promise<void> {
  const sid = story.selectedSceneId
  if (!sid) return
  await story.updateScene(pid.value, sid, { location_variant_id: value || null }).catch(() => {})
}

/** 提案落库之后：幕、镜头、顺序都可能变了，整页重拉。 */
async function onDirectorApplied(): Promise<void> {
  await reload()
  await story.loadBoard(pid.value).catch(() => {})
}

function fmtDuration(n: number): string {
  return `${Math.round(n * 10) / 10}s`
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <FeatureHeader />

    <div class="border-line-1 bg-base-1 flex h-row shrink-0 items-center gap-1 border-b px-2">
      <span class="text-fg-4 text-2xs">
        AI 编剧在右栏：它自己一段一段读左边的原文，每次只就读到的那一段提案，按「采用」才落库。
        手动新建 Scene 与 Shot 能把同一件事做完，不依赖 LLM。
      </span>
      <AppButton size="sm" variant="ghost" class="ml-auto" :disabled="story.busy" @click="reload()">
        <RefreshCw :size="10" />刷新
      </AppButton>
    </div>

    <ErrorPanel
      v-if="story.lastError"
      class="mx-2 mt-2"
      :error="story.lastError"
      @dismiss="story.clearError()"
    />

    <div class="flex min-h-0 flex-1 gap-2 p-2">
      <!-- 左：剧本原文 -->
      <AppPanel title="剧本原文" class="w-80 shrink-0">
        <template #actions>
          <AppBadge v-if="story.story" :tone="story.story.mode === 'manual' ? 'neutral' : 'accent'">
            {{ story.story.mode }}
          </AppBadge>
          <AppButton
            size="sm"
            variant="primary"
            :disabled="!dirty || story.busy"
            @click="saveText()"
          >
            <Save :size="10" />{{ dirty ? '保存' : '已保存' }}
          </AppButton>
        </template>
        <div class="flex h-full min-h-0 flex-col gap-1 p-2">
          <input
            v-model="draftTitle"
            placeholder="片名"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-5 w-full shrink-0 border px-1.5 text-2xs outline-none"
          />
          <textarea
            v-model="draftText"
            placeholder="把剧本贴进来。也可以不写一个字——直接在中间手动建 Scene 与 Shot。"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 min-h-0 flex-1 resize-none border p-1.5 text-2xs leading-relaxed outline-none"
          />
          <p class="text-fg-4 shrink-0 text-2xs">
            右栏的 AI 编剧读的就是这里**保存后**的原文（它按 offset 一段一段取）。原文只是素材：
            真正的真源是中间那些 Scene / Shot（存在 project.db 里）。
          </p>
        </div>
      </AppPanel>

      <!-- 中：已落库的 Scene / Shot。AI 的提案不画在这里——它在右栏逐条审阅 -->
      <AppPanel title="Scene 与 Shot" class="min-h-0 flex-1">
        <template #actions>
          <input
            v-model="newSceneTitle"
            placeholder="新场景名，例如 城南旧宅 · 雨夜"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-5 w-52 border px-1.5 text-2xs outline-none"
            @keyup.enter="createScene()"
          />
          <AppButton
            size="sm"
            variant="primary"
            :disabled="story.busy"
            title="建一场；名字空着就叫「第 N 场」"
            @click="createScene()"
          >
            <Plus :size="10" />新建场景
          </AppButton>
        </template>

        <!-- 已落库 -->
        <EmptyState
          v-if="story.scenes.length === 0"
          title="还没有场景"
          body="从上面「新建场景」开始，或者把剧本贴进左栏保存，再让右栏的 AI 编剧一段一段拆。手动与 AI 出的东西在库里没有区别。"
        />
        <div v-else class="space-y-1.5 p-2">
          <section
            v-for="scene in story.scenes"
            :key="scene.id"
            class="border bg-base-2"
            :class="scene.id === story.selectedSceneId ? 'border-accent/60' : 'border-line-1'"
          >
            <header class="border-line-1 flex items-center gap-1.5 border-b px-2 py-1">
              <button class="min-w-0 flex-1 text-left" @click="story.selectScene(scene.id)">
                <span class="text-fg-1 flex items-center gap-1.5 text-xs">
                  <span class="text-fg-4 tnum">{{ scene.index_no }}</span>
                  <span class="truncate">{{ scene.title }}</span>
                </span>
                <span class="text-fg-4 block truncate text-2xs">
                  {{ scene.shot_count }} 镜 · {{ fmtDuration(scene.duration_total) }}
                  <template v-if="scene.location_variant_name">
                    · {{ scene.location_variant_name }}
                  </template>
                </span>
              </button>
              <AppButton size="sm" variant="ghost" title="上移" @click="moveScene(scene.id, -1)">
                <ChevronUp :size="10" />
              </AppButton>
              <AppButton size="sm" variant="ghost" title="下移" @click="moveScene(scene.id, 1)">
                <ChevronDown :size="10" />
              </AppButton>
              <AppButton
                size="sm"
                variant="ghost"
                title="删除这一场（连带它的镜头）"
                @click="removeScene(scene.id)"
              >
                <Trash2 :size="10" />
              </AppButton>
            </header>
            <ul v-if="scene.id === story.selectedSceneId" class="divide-line-1 divide-y">
              <li
                v-for="card in sceneShots"
                :key="card.id"
                class="text-2xs"
                :class="card.id === story.selectedShotId ? 'bg-accent/5' : ''"
              >
                <div class="flex items-center gap-1.5 px-2 py-1">
                  <div
                    class="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                    :title="`查看 ${card.title} 的摘要`"
                  >
                    <span class="text-fg-4 tnum shrink-0">{{ card.index_no }}</span>
                    <span class="text-fg-1 min-w-0 flex-1 truncate">{{ card.title }}</span>
                    <AppBadge
                      v-if="!card.context_ok"
                      tone="warn"
                      :title="card.context_issues.join('；')"
                    >
                      上下文缺 {{ card.context_issues.length }} 项
                    </AppBadge>
                    <span class="text-fg-3 tnum shrink-0">{{ fmtDuration(card.duration) }}</span>
                  </div>
                  <AppButton
                    size="sm"
                    variant="ghost"
                    title="编辑 Shot 剧情、角色和 Prompt"
                    @click="openShotEditor(card.id)"
                  >
                    <Pencil :size="10" />编辑
                  </AppButton>
                  <AppButton size="sm" variant="ghost" title="删除镜头" @click="removeShot(card.id)">
                    <Trash2 :size="10" />
                  </AppButton>
                </div>
              </li>
              <li class="flex items-center gap-1 px-2 py-1">
                <input
                  v-model="newShotTitle"
                  placeholder="新镜头名，例如 推近·雨中的手"
                  class="border-line-1 bg-base-1 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-5 min-w-0 flex-1 border px-1.5 text-2xs outline-none"
                  @keyup.enter="createShot()"
                />
                <AppButton
                  size="sm"
                  :disabled="story.busy || !story.selectedSceneId"
                  :title="
                    story.selectedSceneId
                      ? '在这一场末尾加一个镜头；名字空着就叫「镜头 N」'
                      : '先在左边选一场——镜头必须挂在某一场下面'
                  "
                  @click="createShot()"
                >
                  <Plus :size="10" />加镜头
                </AppButton>
              </li>
            </ul>
          </section>
        </div>
      </AppPanel>

      <!-- 右：两个 Tab。AI 编剧与场景属性都是「就这一场做事」，共用这一栏 -->
      <div class="flex w-80 shrink-0 flex-col gap-2">
        <div class="border-line-1 bg-base-1 flex shrink-0 border">
          <button
            v-for="t in RIGHT_TABS"
            :key="t.key"
            type="button"
            class="flex-1 px-2 py-1 text-2xs"
            :class="rightTab === t.key ? 'bg-accent/10 text-fg-1' : 'text-fg-4 hover:text-fg-2'"
            @click="rightTab = t.key"
          >
            {{ t.label }}
          </button>
        </div>
        <!-- 与幕流程图页共用同一个会话：在这里聊过的，那边也看得见 -->
        <DirectorPanel
          v-if="rightTab === 'ai'"
          class="min-h-0 flex-1"
          :pid="pid"
          scope="script"
          title="AI 编剧"
          empty-body="把剧本贴进左栏保存，再说一句「从头开始拆这个剧本」。它自己一段一段读原文，每次只就读到的那一段提案——按下采用之前，库里什么都不会变。"
          :quick-actions="QUICK_ACTIONS"
          @applied="onDirectorApplied()"
        />
        <AppPanel v-else title="场景属性" class="min-h-0 flex-1">
          <EmptyState
            v-if="!story.selectedScene"
            title="尚无选中场景"
            body="选一场之后可以改它的梗概与时间，并把它挂到某个地点变体上——镜头的上下文会顺着这条线取参考图。"
          />
          <div v-else class="space-y-3 p-2">
            <section class="space-y-1">
              <label class="block">
                <span class="text-fg-4 text-2xs">场景名</span>
                <input
                  :value="story.selectedScene.title"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveScene('title', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">梗概</span>
                <textarea
                  :value="story.selectedScene.summary ?? ''"
                  rows="3"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px w-full resize-none border px-1.5 py-1 text-2xs outline-none"
                  @change="saveScene('summary', ($event.target as HTMLTextAreaElement).value)"
                />
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">时间（白天 / 雨夜 / 黄昏）</span>
                <input
                  :value="story.selectedScene.time_of_day ?? ''"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveScene('time_of_day', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label class="block">
                <span class="text-fg-4 text-2xs">备注</span>
                <input
                  :value="story.selectedScene.notes ?? ''"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-px h-5 w-full border px-1.5 text-2xs outline-none"
                  @change="saveScene('notes', ($event.target as HTMLInputElement).value)"
                />
              </label>
            </section>
            <section class="border-line-1 border-t pt-2">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">地点变体</p>
              <select
                :value="story.selectedScene.location_variant_id ?? ''"
                class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-1 h-5 w-full border px-1 text-2xs outline-none"
                @change="setVariant(($event.target as HTMLSelectElement).value)"
              >
                <option value="">（未指定）</option>
                <optgroup v-for="loc in world.locations" :key="loc.id" :label="loc.name">
                  <option v-for="v in loc.variants" :key="v.id" :value="v.id">{{ v.name }}</option>
                </optgroup>
              </select>
              <p class="text-fg-4 mt-1 text-2xs">
                挂的是变体（雨夜 / 白天）而不是地点：同一个院子不同天气是两套参考图。
                <template v-if="world.locations.length === 0">
                  现在库里还没有地点，先去「场景与地点」页建一个。
                </template>
              </p>
            </section>

            <section class="border-line-1 border-t pt-2">
              <p class="text-fg-3 text-2xs tracking-wide uppercase">Shot 编辑</p>
              <p class="text-fg-4 mt-1 text-2xs">
                在中间的 Shot 列表右侧点击“编辑”，即可在弹窗中查看和修改剧情、角色、Prompt；这里保留场景级属性。
              </p>
              <AppButton
                size="sm"
                class="mt-1.5"
                :disabled="!story.shot"
                title="去镜头工作台查看上下文账单、版本和生成参数"
                @click="router.push({ name: 'shot', params: { pid, sid: story.shot?.id ?? '' } })"
              >
                <Sparkles :size="10" />打开完整 Shot 工作台
              </AppButton>
            </section>
          </div>
        </AppPanel>
      </div>
    </div>

    <AppDialog
      :open="shotEditorOpen"
      :title="story.shot ? `编辑 Shot · ${story.shot.title}` : '编辑 Shot'"
      subtitle="查看并修改剧情详情、AI 角色选择与生成 Prompt"
      size="lg"
      @update:open="setShotEditorOpen"
    >
      <div v-if="shotDraft && story.shot" class="space-y-3 p-3">
        <div class="grid grid-cols-[minmax(0,1fr)_6rem] gap-2">
          <label class="block">
            <span class="text-fg-4 text-2xs">Shot 标题</span>
            <input v-model="shotDraft.title" class="border-line-1 bg-base-2 text-fg-1 mt-px h-7 w-full border px-2 text-xs outline-none" />
          </label>
          <label class="block">
            <span class="text-fg-4 text-2xs">时长（秒）</span>
            <input v-model.number="shotDraft.duration" type="number" min="0.1" step="0.1" class="border-line-1 bg-base-2 text-fg-1 mt-px h-7 w-full border px-2 text-xs outline-none" />
          </label>
        </div>
        <label class="block">
          <span class="text-fg-4 text-2xs">剧情详情</span>
          <textarea v-model="shotDraft.description" rows="4" class="border-line-1 bg-base-2 text-fg-1 mt-px w-full resize-y border px-2 py-1.5 text-xs leading-relaxed outline-none" placeholder="动作、对白、情绪和剧情上下文" />
        </label>
        <div class="grid grid-cols-2 gap-2">
          <label class="block">
            <span class="text-fg-4 text-2xs">景别 / 镜头</span>
            <input v-model="shotDraft.camera" class="border-line-1 bg-base-2 text-fg-1 mt-px h-7 w-full border px-2 text-xs outline-none" placeholder="中近景" />
          </label>
          <label class="block">
            <span class="text-fg-4 text-2xs">镜头运动</span>
            <input v-model="shotDraft.movement" class="border-line-1 bg-base-2 text-fg-1 mt-px h-7 w-full border px-2 text-xs outline-none" placeholder="缓慢推近" />
          </label>
        </div>
        <section class="border-line-1 border-t pt-3">
          <div class="flex items-center justify-between">
            <span class="text-fg-3 text-2xs tracking-wide uppercase">AI 选用角色</span>
            <span class="text-fg-4 text-2xs">{{ shotCastIds.length }} 个形象</span>
          </div>
          <div v-if="appearances.length" class="mt-2 grid grid-cols-2 gap-x-3 gap-y-1.5">
            <label v-for="appearance in appearances" :key="appearance.row.id" class="text-fg-2 flex min-w-0 items-center gap-1.5 text-2xs">
              <input type="checkbox" :checked="shotCastIds.includes(appearance.row.id)" @change="toggleShotAppearance(appearance.row.id)" />
              <span class="truncate">{{ appearance.character.name }} · {{ appearance.row.name }}</span>
            </label>
          </div>
          <p v-else class="text-fg-4 mt-2 text-2xs">还没有可用的角色形象，请先在角色页建立。</p>
        </section>
        <label class="block">
          <span class="text-fg-4 text-2xs">生成 Prompt</span>
          <textarea v-model="shotDraft.prompt" rows="5" class="border-line-1 bg-base-2 text-fg-1 mt-px w-full resize-y border px-2 py-1.5 text-xs leading-relaxed outline-none" placeholder="AI 生成或人工修订的画面 Prompt" />
        </label>
        <label class="block">
          <span class="text-fg-4 text-2xs">负面 Prompt</span>
          <textarea v-model="shotDraft.negative_prompt" rows="3" class="border-line-1 bg-base-2 text-fg-1 mt-px w-full resize-y border px-2 py-1.5 text-xs leading-relaxed outline-none" placeholder="不希望出现的内容" />
        </label>
        <div class="text-fg-4 flex items-center justify-between text-2xs">
          <span>{{ story.shot.status }} · {{ story.shot.version_count }} 个版本</span>
          <AppButton size="sm" variant="ghost" title="打开单 Shot 工作台查看上下文账单、版本和生成参数" @click="router.push({ name: 'shot', params: { pid, sid: story.shot.id } })">
            <Sparkles :size="10" />打开完整工作台
          </AppButton>
        </div>
      </div>
      <template #footer>
        <span class="text-fg-4 mr-auto text-2xs">{{ shotDirty ? '有未保存修改' : '内容已保存' }}</span>
        <AppButton size="sm" variant="ghost" @click="setShotEditorOpen(false)">取消</AppButton>
        <AppButton size="sm" variant="primary" :disabled="shotEditorBusy || !shotDirty" @click="saveShotEditor()">
          <Save :size="10" />{{ shotDirty ? '保存 Shot' : '已保存' }}
        </AppButton>
      </template>
    </AppDialog>
  </div>
</template>
