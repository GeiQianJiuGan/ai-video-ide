<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import {
  framesApi,
  frameVideoDraft,
  FRAME_ROLE_LABEL,
  type FrameCard,
  type FramePlan,
  type FrameRole,
  type FrameNarrativePlan,
  type FrameVideo,
} from '@/shared/api/frames'
import { assetsApi, type Asset } from '@/shared/api/assets'
import { storyApi, type StoryboardLane } from '@/shared/api/story'
import { generationApi, JOB_STATUS_LABEL, type GenerationVersion } from '@/shared/api/generation'
import { fileUrl } from '@/shared/api/files'
import { useShellStore } from '@/stores/shell'
import { useStoryStore } from '@/stores/story'
import FrameReferencePicker from './FrameReferencePicker.vue'

const props = defineProps<{ planning?: boolean }>()
const route = useRoute()
const shell = useShellStore()
const storyState = useStoryStore()
const pid = computed(() => String(route.params.pid || ''))
const lanes = ref<StoryboardLane[]>([])
const sid = ref('')
const cards = ref<FrameCard[]>([])
const cid = ref('')
const assets = ref<Asset[]>([])
const versions = ref<GenerationVersion[]>([])
const video = ref<FrameVideo>({ prompt: '', duration: 4, first_card_id: '', last_card_id: '' })
const title = ref('')
const prompt = ref('')
const frameStory = ref('')
const frameRole = ref<FrameRole>('key')
const refs = ref<string[]>([])
const imported = ref('')
const preview = ref('')
const compare = ref(false)
const error = ref('')
const notice = ref('')
const busy = ref(false)
const dirty = ref(false)
const plan = ref<FramePlan | null>(null)
const narrativePlan = ref<FrameNarrativePlan | null>(null)
const config = ref<{
  provider: string
  presets: { name: string; first_frame_ok: boolean; flf_ready: boolean }[]
}>({ provider: '', presets: [] })
const mode = computed(() =>
  props.planning ? 'planning' : route.name === 'frame-video' ? 'video' : 'frames',
)
const selected = computed(() => cards.value.find((c) => c.id === cid.value))
const shots = computed(() => lanes.value.flatMap((l) => l.shots))
const shot = computed(() => shots.value.find((s) => s.id === sid.value))
const images = computed(() =>
  assets.value.filter((a) => !a.missing && /\.(png|jpe?g|webp|bmp)$/i.test(a.path)),
)
const prepared = computed(() => cards.value.filter((c) => c.adopted_asset_id))
const canPlan = computed(
  () =>
    cards.value.length <= 8 &&
    !video.value.prompt.trim() &&
    !video.value.first_card_id &&
    !video.value.last_card_id &&
    cards.value.every(
      (c) =>
        !c.prompt.trim() &&
        !c.story.trim() &&
        !c.refs.length &&
        !c.candidates.length &&
        !c.jobs.length &&
        !c.adopted_asset_id,
    ),
)
const canGenerateVideo = computed(
  () =>
    !!video.value.prompt.trim() &&
    prepared.value.some((c) => c.id === video.value.first_card_id) &&
    (!video.value.last_card_id || prepared.value.some((c) => c.id === video.value.last_card_id)),
)
const frameVersions = computed(() =>
  versions.value.filter((v) => v.params.creation_pipeline === 'frames'),
)
const asset = (aid: string | null) => assets.value.find((a) => a.id === aid)
const url = (aid: string | null) => {
  const a = asset(aid)
  return a ? fileUrl(pid.value, a.path) : ''
}
const showError = (e: unknown) => {
  error.value = e instanceof Error ? e.message + ('detail' in e ? `：${e.detail}` : '') : String(e)
}
function edit() {
  dirty.value = true
  plan.value = null
}
async function act(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await fn()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
async function refresh() {
  if (!sid.value) return
  const target = sid.value
  const [ws, all, vs] = await Promise.all([
    framesApi.get(pid.value, target),
    assetsApi.list(pid.value),
    generationApi.versions(pid.value, target),
  ])
  if (target !== sid.value) return
  cards.value = ws.cards
  assets.value = all
  versions.value = vs
  if (!dirty.value) video.value = frameVideoDraft(ws.video, shot.value?.duration || 4)
}
function loadCard() {
  const c = selected.value
  title.value = c?.title || ''
  prompt.value = c?.prompt || ''
  frameStory.value = c?.story || ''
  frameRole.value = c?.role || 'key'
  refs.value = [...(c?.refs || [])]
  preview.value = c?.adopted_asset_id || c?.candidates.at(-1)?.asset_id || ''
}
async function save() {
  if (!dirty.value || !sid.value) return
  if (cid.value)
    await framesApi.update(pid.value, cid.value, {
      title: title.value,
      prompt: prompt.value,
      story: frameStory.value,
      role: frameRole.value,
      refs: refs.value,
    })
  await framesApi.saveVideo(pid.value, sid.value, video.value)
  dirty.value = false
}
async function selectCard(id: string) {
  await save()
  cid.value = id
  loadCard()
  imported.value = ''
}
async function selectShot(id: string) {
  await save()
  sid.value = id
  await storyState.selectShot(pid.value, id)
  plan.value = null
  narrativePlan.value = null
  imported.value = ''
  const ws = await framesApi.get(pid.value, id)
  video.value = {
    prompt: '',
    duration: shot.value?.duration || 4,
    first_card_id: '',
    last_card_id: '',
    ...ws.video,
  }
  await refresh()
  cid.value = cards.value[0]?.id || ''
  loadCard()
}
async function add() {
  await save()
  await framesApi.create(pid.value, sid.value, `画面 ${cards.value.length + 1}`)
  await refresh()
  cid.value = cards.value.at(-1)?.id || ''
  loadCard()
}
async function proposeNarrative() {
  await save()
  notice.value = '正在按镜头剧情规划画面；完成后请审阅采用。'
  narrativePlan.value = await framesApi.proposePlan(pid.value, sid.value)
  notice.value = '制帧草稿已就绪，尚未写入镜头，也未生成图片。'
}
async function applyNarrative() {
  if (!narrativePlan.value) return
  await framesApi.applyPlan(pid.value, sid.value, narrativePlan.value)
  await selectShot(sid.value)
  notice.value = '已采用逐帧计划，可以制作图片并生成视频。'
}
async function adoptVideo(versionId: string) {
  await save()
  await generationApi.setCurrent(pid.value, versionId)
  await storyState.selectShot(pid.value, sid.value)
  await refresh()
  notice.value = '已采用此视频版本。'
}
function derive() {
  refs.value.push(preview.value)
  edit()
}
async function generateMissing() {
  await save()
  let queued = 0
  const failures: string[] = []
  const warnings: string[] = []
  for (const c of cards.value.filter(
    (c) =>
      !c.adopted_asset_id &&
      c.prompt.trim() &&
      !c.jobs.some((j) => ['queued', 'waiting', 'running'].includes(j.status)),
  )) {
    try {
      const job = await framesApi.generate(pid.value, c.id)
      warnings.push(...job.plan.warnings.map((warning) => `${c.title}：${warning}`))
      queued++
    } catch (e) {
      failures.push(`${c.title}: ${e instanceof Error ? e.message : String(e)}`)
    }
  }
  await refresh()
  notice.value = `已入队 ${queued} 个画面。${[...warnings, ...failures].join('；')}`
}
async function generate() {
  await save()
  const job = await framesApi.generate(pid.value, cid.value)
  await refresh()
  notice.value = '图片任务已入队，完成后会出现在候选中。' + job.plan.warnings.join('；')
}
async function adopt(candidateId: string) {
  await save()
  await framesApi.adopt(pid.value, cid.value, candidateId)
  await refresh()
  loadCard()
  plan.value = null
}
async function removeCard() {
  await save()
  await framesApi.remove(pid.value, cid.value)
  if (video.value.first_card_id === cid.value) video.value.first_card_id = ''
  if (video.value.last_card_id === cid.value) video.value.last_card_id = ''
  await refresh()
  cid.value = cards.value[0]?.id || ''
  loadCard()
  plan.value = null
}
async function move(delta: number) {
  await save()
  const i = cards.value.findIndex((c) => c.id === cid.value),
    other = cards.value[i + delta],
    c = selected.value
  if (!c || !other) return
  await framesApi.update(pid.value, c.id, { position: other.position })
  await framesApi.update(pid.value, other.id, { position: c.position })
  await refresh()
}
async function attach() {
  if (!imported.value) return
  await save()
  await framesApi.candidate(pid.value, cid.value, imported.value)
  await refresh()
  preview.value = imported.value
  imported.value = ''
}
async function upload(e: Event) {
  const input = e.target as HTMLInputElement,
    file = input.files?.[0]
  if (!file) return
  await act(async () => {
    await save()
    const a = await assetsApi.upload(pid.value, file, 'upload')
    await framesApi.candidate(pid.value, cid.value, a.id)
    await refresh()
    preview.value = a.id
  })
  input.value = ''
}
async function videoPlan() {
  await save()
  plan.value = await framesApi.plan(pid.value, sid.value, video.value)
}
async function generateVideo() {
  await videoPlan()
  await framesApi.video(pid.value, sid.value, video.value)
  notice.value = '视频已入队，完成后可在本页预览并采用。'
  await refresh()
}
async function beforeLeaving() {
  if (busy.value) return false
  try {
    await save()
  } catch (e) {
    showError(e)
    return false
  }
}
onBeforeRouteLeave(beforeLeaving)
onBeforeRouteUpdate(beforeLeaving)
watch(
  pid,
  () =>
    act(async () => {
      lanes.value = await storyApi.storyboard(pid.value)
      config.value = await framesApi.capabilities(pid.value)
      const requested = String(route.query.shot || storyState.selectedShotId || '')
      const first =
        shots.value.find((s) => s.id === requested) ||
        shots.value.find((s) => s.kind !== 'transition')
      if (first) await selectShot(first.id)
    }),
  { immediate: true },
)
watch(
  () => shell.appliedTick,
  () => {
    void act(async () => {
      await save()
      lanes.value = await storyApi.storyboard(pid.value)
      if (!sid.value || !shots.value.some((s) => s.id === sid.value)) {
        const first = shots.value.find((s) => s.kind !== 'transition')
        if (first) await selectShot(first.id)
      } else {
        await refresh()
        if (!dirty.value) loadCard()
      }
    })
  },
)
const timer = window.setInterval(() => {
  if (!busy.value && sid.value) void refresh().catch(showError)
}, 5000)
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<template>
  <div class="frame-workspace">
    <header>
      <div>
        <h1>
          {{ mode === 'planning' ? '制帧分镜' : mode === 'frames' ? '制作图片' : '视频生成' }}
        </h1>
        <small>{{
          mode === 'planning'
            ? '镜头剧情 → 逐帧故事与图片提示词 → 视频动作与声音'
            : '按分镜计划制作画面、比较与采用候选'
        }}</small>
      </div>
      <nav>
        <RouterLink
          v-if="mode !== 'planning'"
          :to="{ name: 'storyboard', params: { pid }, query: { production: 'frames', shot: sid } }"
          >制帧分镜 / 视频</RouterLink
        >
        <button :disabled="busy || !dirty" @click="act(save)">
          {{ dirty ? '保存草稿' : '已保存' }}
        </button>
      </nav>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status" class="notice">{{ notice }}</p>
    <div class="workspace-body">
      <aside class="shot-list">
        <p v-if="!shots.length">先在剧本或分镜中创建镜头。</p>
        <section v-for="lane in lanes" :key="lane.id">
          <h3>{{ lane.title }}</h3>
          <button
            v-for="s in lane.shots.filter((s) => s.kind !== 'transition')"
            :key="s.id"
            :class="{ active: sid === s.id }"
            :disabled="busy"
            @click="act(() => selectShot(s.id))"
          >
            {{ s.index_no }} · {{ s.title || '未命名镜头' }}
          </button>
        </section>
        <RouterLink :to="{ name: 'story', params: { pid } }">查看剧本</RouterLink>
      </aside>
      <main v-if="sid">
        <div class="shot-heading">
          <strong>{{ shot?.title }}</strong
          ><span>{{ cards.length }} 个画面 · {{ prepared.length }} 个已选图</span>
        </div>
        <p v-if="shot?.description" class="shot-story">镜头剧情：{{ shot.description }}</p>
        <template v-if="mode !== 'video'">
          <section class="narrative-section">
            <h2>逐帧故事与图片提示词</h2>
            <p v-if="!cards.length">
              本镜头尚无制帧计划。可按现有剧情规划画面，或手动添加每张图片。
            </p>
            <button
              :disabled="busy || !canPlan"
              :title="
                canPlan
                  ? '按当前镜头与空白画面位置生成待审阅的计划'
                  : '已有制作内容，请直接编辑对应剧情与提示词'
              "
              @click="act(proposeNarrative)"
            >
              AI 规划本镜画面
            </button>
            <section v-if="narrativePlan" class="plan-review">
              <h3>待采用的制帧计划</h3>
              <article v-for="(frame, index) in narrativePlan.frames" :key="index">
                <strong
                  >{{ index + 1 }} · {{ frame.title }} · {{ FRAME_ROLE_LABEL[frame.role] }}</strong
                >
                <p>对应剧情：{{ frame.story }}</p>
                <p>图片提示词：{{ frame.prompt }}</p>
              </article>
              <p>视频动作与声音：{{ narrativePlan.video_prompt }}</p>
              <div class="row">
                <button :disabled="busy" @click="act(applyNarrative)">采用制帧计划</button>
                <button :disabled="busy" @click="narrativePlan = null">丢弃草稿</button>
              </div>
            </section>
          </section>
          <div class="row">
            <RouterLink
              v-if="mode === 'planning'"
              :to="{ name: 'frames', params: { pid }, query: { shot: sid } }"
              >制作本镜图片</RouterLink
            >
            <button
              v-if="mode === 'frames'"
              :disabled="busy || !cards.length"
              @click="act(generateMissing)"
            >
              批量生成缺失画面</button
            ><label v-if="mode === 'frames'"
              ><input v-model="compare" type="checkbox" /> 对比采用图与候选</label
            >
          </div>
          <div class="filmstrip">
            <button
              v-for="c in cards"
              :key="c.id"
              :class="{ active: cid === c.id }"
              :disabled="busy"
              @click="act(() => selectCard(c.id))"
            >
              <img v-if="url(c.adopted_asset_id)" :src="url(c.adopted_asset_id)" alt="采用的画面" />
              <span>{{ c.title }} · {{ FRAME_ROLE_LABEL[c.role] }}</span>
              <span v-if="mode === 'planning'" class="frame-story">
                {{ c.story || '待填写对应剧情' }}
              </span>
              <small v-if="mode === 'planning'" class="frame-prompt"
                >图片提示词：{{ c.prompt || '待填写' }}</small
              ><small
                >{{ c.adopted_asset_id ? '已选图' : '待选图' }} ·
                {{ c.candidates.length }} 个候选</small
              >
            </button>
            <button :disabled="busy" @click="act(add)">＋ 添加画面</button>
          </div>
          <div
            v-if="selected"
            class="frame-editor"
            :class="{ 'planning-editor': mode === 'planning' }"
          >
            <div v-if="mode === 'frames'" class="picture-area">
              <div class="large-preview" :class="{ comparing: compare }">
                <figure v-if="compare && selected.adopted_asset_id">
                  <img :src="url(selected.adopted_asset_id)" alt="已采用画面" />
                  <figcaption>已采用</figcaption>
                </figure>
                <img v-if="url(preview)" :src="url(preview)" alt="当前画面预览" />
                <p v-else>生成图片或从项目素材中添加候选</p>
              </div>
              <div class="candidates">
                <article
                  v-for="(v, i) in selected.candidates"
                  :key="v.id"
                  :class="{ active: preview === v.asset_id }"
                >
                  <button @click="preview = v.asset_id">
                    <img :src="url(v.asset_id)" :alt="`候选 ${i + 1}`" />候选 {{ i + 1 }}
                  </button>
                  <button
                    :disabled="busy || selected.adopted_asset_id === v.asset_id"
                    @click="act(() => adopt(v.id))"
                  >
                    {{ selected.adopted_asset_id === v.asset_id ? '已采用' : '采用此图' }}
                  </button>
                </article>
              </div>
            </div>
            <aside class="inspector">
              <label>画面名称<input v-model="title" maxlength="200" @input="edit" /></label>
              <label
                >叙事位置<select v-model="frameRole" @change="edit">
                  <option v-for="(label, role) in FRAME_ROLE_LABEL" :key="role" :value="role">
                    {{ label }}
                  </option>
                </select></label
              >
              <label
                >对应剧情<textarea
                  v-model="frameStory"
                  rows="3"
                  placeholder="这张图讲到哪一步？谁处于什么状态，如何承接前后画面？"
                  @input="edit"
                />
              </label>
              <label
                >图片提示词<textarea
                  v-model="prompt"
                  rows="7"
                  placeholder="描述这一时刻的人物、姿态、景别、机位、场景、道具与光线。"
                  @input="edit"
                />
              </label>
              <template v-if="mode === 'frames'">
                <FrameReferencePicker
                  v-model="refs"
                  :pid="pid"
                  :assets="assets"
                  :disabled="busy"
                  @change="edit"
                />
                <button :disabled="busy || !prompt.trim()" @click="act(generate)">
                  生成新候选
                </button>
                <button v-if="preview && !refs.includes(preview)" :disabled="busy" @click="derive">
                  以预览图为参考继续修改
                </button>
                <div class="row">
                  <button :disabled="busy" @click="act(() => move(-1))">前移</button
                  ><button :disabled="busy" @click="act(() => move(1))">后移</button
                  ><button :disabled="busy" @click="act(removeCard)">移除画面（保留素材）</button>
                </div>
                <label
                  >从项目素材添加<select v-model="imported">
                    <option value="">选择图片</option>
                    <option v-for="a in images" :key="a.id" :value="a.id">
                      {{ a.description || a.path }}
                    </option>
                  </select></label
                >
                <button :disabled="busy || !imported" @click="act(attach)">添加为候选</button>
                <label
                  >上传画面<input
                    type="file"
                    accept="image/png,image/jpeg,image/webp,image/bmp"
                    :disabled="busy"
                    @change="upload"
                /></label>
                <p v-for="j in selected.jobs" :key="j.id">
                  {{ JOB_STATUS_LABEL[j.status] || j.status }}
                  <small v-if="j.error_json">{{ j.error_json }}</small>
                </p>
              </template>
              <div v-else class="row">
                <button :disabled="busy" @click="act(() => move(-1))">前移</button>
                <button :disabled="busy" @click="act(() => move(1))">后移</button>
                <button :disabled="busy" @click="act(removeCard)">移除画面（保留素材）</button>
              </div>
            </aside>
          </div>
          <p v-else class="empty">
            为当前镜头添加开场、关键动作或收尾画面。每个画面可保存多个候选版本。
          </p>
          <p>画面按故事顺序排列。视频使用选定首帧及可选尾帧，关键状态仅作叙事参考。</p>
        </template>
        <div v-if="mode !== 'frames'" class="video-editor">
          <h2>视频动作与声音</h2>
          <p>为本镜头单独描述动作推进、运镜与声音，采用图片后即可生成视频。</p>
          <label v-if="config.provider === 'comfy_preset'"
            >本镜头制帧流程的视频预设<select v-model="video.preset" @change="edit">
              <option value="">跟随项目默认</option>
              <option
                v-for="p in config.presets.filter(
                  (p) => p.first_frame_ok && (!video.last_card_id || p.flf_ready),
                )"
                :key="p.name"
                :value="p.name"
              >
                {{ p.name }}
              </option></select
            ><small>单独保存到制帧草稿，不改变原版制作的预设。</small></label
          >
          <label
            >首帧<select v-model="video.first_card_id" @change="edit">
              <option value="">选择开场画面</option>
              <option v-for="c in cards" :key="c.id" :value="c.id">
                {{ c.title }}{{ c.adopted_asset_id ? '' : '（待选图）' }}
              </option>
            </select></label
          >
          <label
            >尾帧（可选）<select v-model="video.last_card_id" @change="edit">
              <option value="">仅使用首帧</option>
              <option v-for="c in cards" :key="c.id" :value="c.id">
                {{ c.title }}{{ c.adopted_asset_id ? '' : '（待选图）' }}
              </option>
            </select></label
          >
          <label
            >动作、运镜、对白与声音<textarea
              v-model="video.prompt"
              rows="7"
              placeholder="从首帧的当前状态开始，描述接下来发生的动作与镜头变化。"
              @input="edit"
            />
          </label>
          <label
            >时长（秒）<input
              v-model.number="video.duration"
              type="number"
              min="1"
              max="60"
              step="0.1"
              @input="edit"
          /></label>
          <div class="row">
            <button :disabled="busy" @click="act(videoPlan)">检查实际输入</button
            ><button :disabled="busy || !canGenerateVideo" @click="act(generateVideo)">
              生成视频候选
            </button>
          </div>
          <section v-if="plan" class="submission">
            <strong>本次提交的画面</strong>
            <div class="row">
              <figure v-for="c in plan.inputs.cards" :key="c.role">
                <img :src="url(c.asset_id)" :alt="c.title" />
                <figcaption>
                  {{ c.role === 'first_frame' ? '首帧' : '尾帧' }} · {{ c.title }}
                </figcaption>
              </figure>
            </div>
            <p>其余 {{ plan.unused_cards.length }} 个画面不提交；制帧设定图不进入视频请求。</p>
          </section>
          <section v-if="frameVersions.length">
            <h3>画面驱动的视频候选</h3>
            <article v-for="v in frameVersions" :key="v.id">
              <strong>版本 {{ v.version_no }}</strong
              ><video
                v-if="url(v.asset_id)"
                :src="url(v.asset_id)"
                controls
                preload="metadata"
              /><button
                :disabled="busy || !v.asset_id || storyState.shot?.current_version_id === v.id"
                @click="act(() => adoptVideo(v.id))"
              >
                {{ storyState.shot?.current_version_id === v.id ? '已采用' : '采用此视频版本' }}
              </button>
            </article>
          </section>
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.frame-workspace {
  height: 100%;
  display: flex;
  flex-direction: column;
  color: var(--color-fg-1);
  font-size: 12px;
  overflow: auto;
}
header {
  padding: 16px;
  border-bottom: 1px solid var(--color-line-1);
  display: flex;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
header h1 {
  font-size: 18px;
}
small {
  display: block;
  opacity: 0.65;
  margin-top: 4px;
}
nav,
.row {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
a {
  color: var(--color-accent);
  text-decoration: underline;
}
button,
input,
textarea,
select {
  border: 1px solid var(--color-line-1);
  background: var(--color-base-2);
  border-radius: 5px;
  padding: 8px;
  color: inherit;
}
button {
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.active {
  outline: 1px solid var(--color-accent);
}
.workspace-body {
  display: flex;
  flex: 1;
  min-height: 0;
}
.shot-list {
  width: 175px;
  flex-shrink: 0;
  border-right: 1px solid var(--color-line-1);
  padding: 12px;
  overflow: auto;
}
.shot-list button {
  display: block;
  width: 100%;
  text-align: left;
  margin: 7px 0;
}
.shot-list h3 {
  opacity: 0.65;
  margin-top: 15px;
}
main {
  flex: 1;
  min-width: 0;
  padding: 18px;
  overflow: auto;
}
.shot-heading {
  display: flex;
  gap: 16px;
  align-items: center;
  margin-bottom: 18px;
}
.filmstrip {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  padding: 3px 3px 15px;
}
.filmstrip button {
  width: 150px;
  flex-shrink: 0;
}
.filmstrip img {
  height: 70px;
  width: 100%;
  object-fit: contain;
}
.filmstrip span {
  display: block;
}
.frame-story,
.frame-prompt {
  text-align: left;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.frame-prompt {
  max-height: 100px;
  overflow: auto;
}
.narrative-section,
.video-editor {
  margin: 18px 0;
}
.narrative-section h2,
.video-editor h2 {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 10px;
}
.plan-review {
  border: 1px solid var(--color-accent);
  padding: 14px;
  margin-top: 12px;
}
.plan-review article {
  margin: 12px 0;
  white-space: pre-wrap;
}
.frame-editor {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 20px;
  margin-top: 15px;
}
.planning-editor {
  display: block;
  max-width: 1100px;
}
.planning-editor .inspector {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.planning-editor .inspector > .row {
  grid-column: 1 / -1;
}
.large-preview {
  min-height: 300px;
  background: #101216;
  display: flex;
  justify-content: center;
  align-items: center;
  border-radius: 8px;
}
.large-preview img {
  width: 100%;
  max-height: 60vh;
  object-fit: contain;
}
.candidates {
  display: flex;
  gap: 10px;
  overflow: auto;
  padding: 12px 2px;
}
.candidates article {
  width: 145px;
  flex-shrink: 0;
}
.candidates img {
  width: 125px;
  height: 85px;
  object-fit: contain;
}
.candidates button {
  width: 100%;
}
.inspector {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 7px;
}
textarea {
  resize: vertical;
}
.ref-thumbs {
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
}
.ref-thumbs img {
  width: 50px;
  height: 50px;
  object-fit: cover;
}
.video-editor {
  max-width: 900px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.video-editor video {
  display: block;
  width: 100%;
  max-height: 350px;
  margin: 10px 0;
}
.submission img {
  width: 190px;
  height: 110px;
  object-fit: contain;
}
.error,
.notice {
  padding: 12px;
  margin: 10px 16px;
  border: 1px solid #ad6262;
}
.notice {
  border-color: #548d78;
}
.empty {
  padding: 60px;
  text-align: center;
  opacity: 0.65;
}
@media (max-width: 1000px) {
  .frame-editor {
    grid-template-columns: 1fr;
  }
  .shot-list {
    width: 140px;
  }
}
</style>

<style scoped>
.large-preview.comparing {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding: 12px;
}
.large-preview.comparing img {
  max-height: 45vh;
}
.large-preview figure {
  text-align: center;
}
.filmstrip {
  margin-top: 12px;
}
</style>
