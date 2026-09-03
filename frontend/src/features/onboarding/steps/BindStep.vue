<script setup lang="ts">
/**
 * 第四步：绑定。两条路二选一——ComfyUI 预设，或通用 REST API。
 *
 * 这一步是新手最容易卡住的地方，所以把「本工具不维护模型端的图」这件事讲清楚：
 * 我们不改你的 lora 与加速节点，只按**节点标题**往入口注参数。标题约定与槽位徽标
 * 的口径都来自后端（`GET /settings/presets` 的 `how_to` / `ref_slots` / `impact`），
 * 这一页不抄第二份。
 *
 * 绑定落在**工程**上（`PUT /projects/{pid}/preset`，R2V 与首尾帧两个角色各一份）；
 * 没有工程时按钮 disabled 并写清「先回上一步建演示工程」——不画假入口。
 */
import { computed, onMounted, ref } from 'vue'
import { Link2, Upload } from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import StatusDot from '@/shared/ui/StatusDot.vue'
import { ApiError } from '@/shared/api/client'
import { projectsApi, type ProjectPreset } from '@/shared/api/projects'
import type { PresetRow } from '@/shared/api/settings'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'

const cfg = useSettingsStore()
const proj = useProjectStore()

const fileInput = ref<HTMLInputElement | null>(null)
const bound = ref<ProjectPreset | null>(null)
const busy = ref(false)
const error = ref<ApiError | null>(null)

const pid = computed(() => proj.current?.id ?? null)
const rows = computed<PresetRow[]>(() => cfg.presets?.items ?? [])
/** 现在走的是哪条路。候选与文案都来自 `GET /settings` 的 `providers`。 */
const provider = computed(() => String(cfg.draft['video.provider'] ?? ''))

/** 三族槽位拼一行——`<option>` 与徽标塞不下，理由同概览页。 */
function slots(row: PresetRow): string {
  const parts = [`参考图 ${row.ref_slots} 槽`]
  if (row.ref_video_slots) parts.push(`参考视频 ${row.ref_video_slots} 槽`)
  if (row.ref_audio_slots) parts.push(`参考音频 ${row.ref_audio_slots} 槽`)
  return parts.join(' · ')
}

/**
 * 绑不上的时候到底为什么——**「这份图是出图那一份」与「这份图缺入口标题」是两件事**。
 *
 * 标了 AIVS_IMAGE 的图入口标题一个不缺（提示词、负向、种子、参考图槽位用的就是同一批
 * 标题），照「先照约定改好节点标题」那句说下去，只会让人去改一个本来没问题的标题；
 * 真正的原因是它声明了自己出图，所以后端把它从 R2V / 首尾帧的候选里撤掉了。
 */
function why(row: PresetRow, role: 'r2v' | 'flf'): string {
  if (!pid.value) return '先回上一步建（或打开）一个工程，绑定是落在工程上的'
  if (row.declares_image) {
    return '这份图标了 AIVS_IMAGE，是出图那一份（角色四视图 / 地点参考图 / 道具图走它）——出画面请另选一份没标它的图'
  }
  if (role === 'r2v') {
    return (row.r2v_ready ?? row.ready)
      ? '用它做「参考图 + prompt 出片段」'
      : '这份图缺必需的入口标题，先照上面的约定改好节点标题'
  }
  return row.flf_ready
    ? '用它做「首帧 + 末帧」那种严格衔接'
    : '这份图没有首尾帧入口（AIVS_FIRST_FRAME / AIVS_LAST_FRAME）'
}

async function loadBound(): Promise<void> {
  if (!pid.value) return
  bound.value = await projectsApi.preset(pid.value).catch(() => null)
}

onMounted(() => {
  if (!cfg.snapshot) void cfg.load()
  else void cfg.loadPresets()
  void loadBound()
})

async function onUpload(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file) await cfg.uploadPreset(file).catch(() => {})
}

/**
 * 把这份图绑到当前工程的某个角色上。
 *
 * 另一个角色**原样带回去**：`PUT` 收的是两个名字，只传一个会把另一个清掉。
 */
async function bind(role: 'r2v' | 'flf', name: string): Promise<void> {
  if (!pid.value) return
  busy.value = true
  error.value = null
  try {
    const keep = bound.value
    bound.value = await projectsApi.setVideoPresets(
      pid.value,
      role === 'r2v' ? name : (keep?.r2v_name ?? null),
      role === 'flf' ? name : (keep?.flf_name ?? null),
    )
  } catch (err) {
    error.value = err instanceof ApiError ? err : null
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="space-y-2">
    <ErrorPanel :error="error" @dismiss="error = null" />
    <ErrorPanel :error="cfg.lastError" @dismiss="cfg.clearError()" />

    <p class="text-fg-3 border-line-1 bg-base-2 border px-3 py-2 text-2xs leading-relaxed">
      现在的调用方式是
      <span class="text-fg-1 font-mono">{{ provider || '未选择' }}</span>
      （在上一步的「视频生成」里改）。选 comfy_preset 就走下面的预设那条路；选 http_api
      就不需要预设，只要地址与密钥对上通用合同即可。
    </p>

    <!-- 路一：ComfyUI 预设 -->
    <AppPanel title="路一 · ComfyUI 预设（本机那台 ComfyUI）">
      <template #actions>
        <AppButton size="sm" variant="primary" :disabled="cfg.busy" @click="fileInput?.click()">
          <Upload :size="10" />上传 API 格式 json
        </AppButton>
        <input
          ref="fileInput"
          type="file"
          accept=".json,application/json"
          class="hidden"
          @change="onUpload"
        />
      </template>

      <ul v-if="cfg.presets" class="text-fg-4 border-line-1 border-b px-3 py-1.5 text-2xs">
        <li v-for="line in cfg.presets.how_to" :key="line">· {{ line }}</li>
      </ul>

      <ul class="divide-line-1 divide-y">
        <li v-for="row in rows" :key="row.name" class="px-3 py-1.5">
          <div class="flex items-center gap-2">
            <StatusDot :status="row.ready ? 'completed' : 'failed'" />
            <span class="text-fg-1 min-w-0 flex-1 truncate text-xs">{{ row.name }}</span>
            <AppBadge v-if="bound?.r2v_name === row.name" tone="accent">本工程 · R2V</AppBadge>
            <AppBadge v-if="bound?.flf_name === row.name" tone="accent">本工程 · 首尾帧</AppBadge>
            <AppBadge v-if="row.ready" :tone="row.ref_slots ? 'neutral' : 'warn'">
              {{ slots(row) }}
            </AppBadge>
            <!--
              标了 AIVS_IMAGE 的图在这一页是**不能绑的**（它出的是素材图，不是画面）：
              下面两颗按钮此时都禁用，所以先把「它是哪一栏的」标出来，用户才不会以为坏了。
            -->
            <AppBadge v-if="row.declares_image" tone="ok">T2I 出图</AppBadge>
            <AppButton
              size="sm"
              :disabled="!pid || busy || !(row.r2v_ready ?? row.ready)"
              :title="why(row, 'r2v')"
              @click="bind('r2v', row.name)"
            >
              <Link2 :size="10" />绑为 R2V
            </AppButton>
            <AppButton
              size="sm"
              :disabled="!pid || busy || !row.flf_ready"
              :title="why(row, 'flf')"
              @click="bind('flf', row.name)"
            >
              <Link2 :size="10" />绑为首尾帧
            </AppButton>
          </div>
          <p
            v-if="!row.ready && row.impact"
            class="text-st-failed mt-0.5 pl-4 text-2xs leading-relaxed"
          >
            {{ row.impact }}
          </p>
          <p
            v-else-if="row.ref_hint"
            class="mt-0.5 pl-4 text-2xs leading-relaxed"
            :class="row.ref_slots ? 'text-fg-4' : 'text-st-failed'"
          >
            {{ row.ref_hint }}
          </p>
        </li>
        <li v-if="!rows.length" class="text-fg-4 px-3 py-2 text-2xs leading-relaxed">
          还没有预设。在 ComfyUI 里把能出片的那张图用「Save (API Format)」导出，
          按上面的标题约定改好入口节点的标题，再点右上角上传。
        </li>
      </ul>

      <p class="text-fg-4 border-line-1 border-t px-3 py-2 text-2xs leading-relaxed">
        我们只按节点标题注入入口参数：AIVS_PROMPT / AIVS_NEGATIVE / AIVS_FIRST_FRAME /
        AIVS_LAST_FRAME / AIVS_DURATION / AIVS_SEED，参考素材是 AIVS_REF_1…9（图）、
        AIVS_REF_VIDEO_1…4、AIVS_REF_AUDIO_1…4。图里的 lora、加速节点、采样器怎么摆
        是模型端自己的事，本工具不解析也不改写。
      </p>
    </AppPanel>

    <!-- 路二：通用 REST API -->
    <AppPanel title="路二 · 通用 REST API（云端或自建服务）">
      <p class="text-fg-3 px-3 py-2 text-2xs leading-relaxed">
        把上一步的「调用方式」改成 <span class="font-mono">http_api</span>，填好地址与密钥就行 ——
        这条路不需要预设。服务端只要满足 docs/05 里那份合同：收 prompt、首尾帧、 refs（每条带 desc
        与 media），回一个可轮询的任务 id 与最终文件。
      </p>
      <ul class="text-fg-4 border-line-1 border-t px-3 py-2 text-2xs leading-relaxed">
        <li>· 描述走 refs[].desc 那一项，不重复塞进 prompt——描述属于素材本身。</li>
        <li>· 密钥只走请求头，永远不进 URL，也不会进工程包。</li>
      </ul>
    </AppPanel>

    <p v-if="!pid" class="text-st-review text-2xs leading-relaxed">
      还没有打开的工程，所以上面的绑定按钮是灰的：绑定落在工程上而不是应用上。
      回上一步建一份演示工程即可。
    </p>
  </div>
</template>
