<script setup lang="ts">
/**
 * 剧本工作台（Step 5 的前端）。
 *
 * 三条与后端一致的规矩，UI 上必须看得出来：
 *   1. **AI 只出提案**——`breakdown/propose` 一个字都不写库。提案画在主区，逐条可标
 *      「不要」，按「落库」才真的写进工程。所以 LLM 没配置也不挡路：右边那排手动
 *      入口能把同一件事做完，只是慢。
 *   2. **序号是时间顺序**——Scene / Shot 的 index_no 由后端重排，前端只提交顺序。
 *   3. Scene 挂的是**地点变体**（雨夜 / 白天），不是地点本身，所以下拉框按地点分组。
 *
 * 镜头的提示词、Workflow、上下文不在这一页——那是镜头编辑器的活。这里只管
 * 「有哪些场、每场有哪些镜头、各多长」。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChevronDown,
  ChevronUp,
  Plus,
  RefreshCw,
  Save,
  Sparkles,
  Trash2,
  Wand2,
  X,
} from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import type { ProposedScene, ProposedShot } from '@/shared/api/story'
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

const proposal = computed(() => story.proposal)
const llmReady = computed(() => story.llm?.configured ?? false)

/** 提案里被接受的条数——落库按钮上写清「要写几条」，别让人蒙着点。 */
const accepted = computed(() => {
  const list = proposal.value?.scenes ?? []
  const scenes = list.filter((s) => s.op !== 'reject')
  return {
    scenes: scenes.length,
    shots: scenes.reduce((n, s) => n + s.shots.filter((c) => c.op !== 'reject').length, 0),
  }
})

function syncDraft(): void {
  draftText.value = story.story?.raw_text ?? ''
  draftTitle.value = story.story?.title ?? ''
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
  syncDraft()
}

onMounted(reload)
watch(pid, reload)

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

/** 逐条审阅：标 reject 就是「不要它」，后端 apply 时跳过。 */
function toggleScene(scene: ProposedScene): void {
  scene.op = scene.op === 'reject' ? 'create' : 'reject'
}

function toggleShot(shot: ProposedShot): void {
  shot.op = shot.op === 'reject' ? 'create' : 'reject'
}

async function propose(): Promise<void> {
  await story.propose(pid.value, draftText.value.trim() || undefined).catch(() => {})
}

async function applyProposal(): Promise<void> {
  await story.applyProposal(pid.value).catch(() => {})
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
      <AppButton
        size="sm"
        variant="primary"
        :disabled="story.busy || !llmReady"
        :title="
          llmReady
            ? '把左边的原文交给 LLM 拆成 Scene / Shot；结果只是提案，要你审阅后才落库'
            : '还没配置 LLM（AIVS_LLM_PROVIDER）。手动新建 Scene 与 Shot 能把同一件事做完，只是慢'
        "
        @click="propose()"
      >
        <Wand2 :size="10" />AI 自动拆解
      </AppButton>
      <span class="text-fg-4 text-2xs">
        {{ llmReady ? story.llm?.hint : 'LLM 未配置 —— 右边手动新建同样能走完整条链路' }}
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
            placeholder="把剧本贴进来。也可以不写一个字——直接在右边手动建 Scene 与 Shot。"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 min-h-0 flex-1 resize-none border p-1.5 text-2xs leading-relaxed outline-none"
          />
          <p class="text-fg-4 shrink-0 text-2xs">
            原文只是素材：真正的真源是下面拆出来的 Scene / Shot（存在 project.db 里）。
          </p>
        </div>
      </AppPanel>

      <!-- 中：提案审阅（有提案时）或已落库的 Scene / Shot -->
      <AppPanel :title="proposal ? 'AI 提案（尚未落库）' : 'Scene 与 Shot'" class="min-h-0 flex-1">
        <template #actions>
          <template v-if="proposal">
            <span class="text-fg-4 text-2xs">
              打叉的条目不会写进工程；提案不落库，刷新页面就没了
            </span>
            <AppButton size="sm" variant="ghost" @click="story.discardProposal()">
              <X :size="10" />丢弃
            </AppButton>
            <AppButton
              size="sm"
              variant="primary"
              :disabled="story.busy || accepted.scenes === 0"
              @click="applyProposal()"
            >
              <Plus :size="10" />落库 {{ accepted.scenes }} 场 / {{ accepted.shots }} 镜
            </AppButton>
          </template>
          <template v-else>
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
        </template>

        <!-- 提案：逐条 Diff -->
        <div v-if="proposal" class="space-y-2 p-2">
          <p class="text-fg-4 text-2xs">{{ proposal.note }}</p>
          <section
            v-for="scene in proposal.scenes"
            :key="scene.temp_id"
            class="border bg-base-2"
            :class="scene.op === 'reject' ? 'border-line-1 opacity-50' : 'border-accent/40'"
          >
            <header class="border-line-1 flex items-center gap-1.5 border-b px-2 py-1">
              <span class="text-fg-1 min-w-0 flex-1 truncate text-xs">{{ scene.title }}</span>
              <span v-if="scene.time_of_day" class="text-fg-4 text-2xs">{{
                scene.time_of_day
              }}</span>
              <AppBadge>{{ scene.shots.length }} 镜</AppBadge>
              <AppButton size="sm" variant="ghost" @click="toggleScene(scene)">
                {{ scene.op === 'reject' ? '要它' : '不要' }}
              </AppButton>
            </header>
            <p v-if="scene.summary" class="text-fg-3 px-2 py-1 text-2xs">{{ scene.summary }}</p>
            <ul class="divide-line-1 divide-y">
              <li
                v-for="shot in scene.shots"
                :key="shot.temp_id"
                class="flex items-center gap-1.5 px-2 py-1 text-2xs"
                :class="shot.op === 'reject' ? 'opacity-50' : ''"
              >
                <span class="text-fg-1 min-w-0 flex-1 truncate">{{ shot.title }}</span>
                <span v-if="shot.camera" class="text-fg-4 shrink-0">{{ shot.camera }}</span>
                <span class="text-fg-3 tnum shrink-0">{{ fmtDuration(shot.duration) }}</span>
                <span v-if="shot.characters.length" class="text-fg-4 shrink-0 truncate">
                  {{ shot.characters.join(' / ') }}
                </span>
                <AppButton size="sm" variant="ghost" @click="toggleShot(shot)">
                  {{ shot.op === 'reject' ? '要它' : '不要' }}
                </AppButton>
              </li>
            </ul>
          </section>
        </div>

        <!-- 已落库 -->
        <EmptyState
          v-else-if="story.scenes.length === 0"
          title="还没有场景"
          body="从上面「新建场景」开始，或者把剧本贴进左栏交给 AI 拆一版提案。手动与 AI 出的东西在库里没有区别。"
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
                class="flex items-center gap-1.5 px-2 py-1 text-2xs"
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
                <AppButton size="sm" variant="ghost" title="删除镜头" @click="removeShot(card.id)">
                  <Trash2 :size="10" />
                </AppButton>
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

      <!-- 右：提案时看角色映射，平时改场景属性 -->
      <AppPanel :title="proposal ? '角色映射' : '场景属性'" class="w-72 shrink-0">
        <template v-if="proposal">
          <EmptyState
            v-if="proposal.character_mapping.length === 0"
            title="文本里没认出人名"
            body="没有映射不影响落库：场景与镜头照样写进去，出场角色可以之后在镜头编辑器里挂。"
          />
          <ul v-else class="divide-line-1 divide-y">
            <li
              v-for="m in proposal.character_mapping"
              :key="m.name"
              class="flex items-center gap-1.5 px-2 py-1 text-2xs"
            >
              <span class="text-fg-1 min-w-0 flex-1 truncate">{{ m.name }}</span>
              <AppBadge
                :tone="
                  m.confidence === 'exact' ? 'ok' : m.confidence === 'fuzzy' ? 'warn' : 'neutral'
                "
              >
                {{
                  m.confidence === 'exact' ? '同名' : m.confidence === 'fuzzy' ? '相近' : '库里没有'
                }}
              </AppBadge>
              <span class="text-fg-4 shrink-0 truncate">{{ m.match_name ?? '待新建' }}</span>
            </li>
          </ul>
          <p class="text-fg-4 border-line-1 border-t p-2 text-2xs">
            映射只是提示：落库不会自动建角色，也不会自动改已有角色。缺的人去角色页建或从素材库采用。
          </p>
        </template>
        <template v-else>
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
              <p class="text-fg-3 text-2xs tracking-wide uppercase">镜头细节</p>
              <p class="text-fg-4 mt-1 text-2xs">
                提示词、Workflow、出场角色与上下文账单在镜头编辑器里改。这一页只管有哪些场、哪些镜。
              </p>
              <AppButton
                size="sm"
                class="mt-1.5"
                :disabled="!story.shot"
                title="去镜头编辑器改提示词、挂角色、看上下文账单"
                @click="router.push({ name: 'shot', params: { pid, sid: story.shot?.id ?? '' } })"
              >
                <Sparkles :size="10" />打开镜头编辑器
              </AppButton>
            </section>
          </div>
        </template>
      </AppPanel>
    </div>
  </div>
</template>
