<script setup lang="ts">
/**
 * 长视频导入切段弹窗 (IngestVideoModal)。
 *
 * 流程：
 *   1. 选片登记 (Register)：选择本机视频文件，支持复制进工程（推荐）或原地引用；
 *   2. 预览裁切 (Trim)：先看一遍，把片头片尾框出来——几乎每段成片前面都有一截台标、
 *      后面都有一截字幕，它们不该先被自动切成两个镜头再让用户一个个删掉。裁出来的只是
 *      一对数字（`range_in` / `range_out`），**源文件一帧都不动**；
 *   3. 切段账单 (Plan)：选择切段方式（自动画面切换/静音停顿/固定步长），只读地出账单；
 *   4. 幕参数与落库 (Run)：设置幕标题、Prompt 继承模式，一键落库创建分镜与零复制区间版本。
 */

import { computed, ref, watch } from 'vue'
import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  Film,
  Scissors,
  UploadCloud,
} from '@lucide/vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import VideoTrimmer from './VideoTrimmer.vue'
import { ApiError } from '@/shared/api/client'
import { assetsApi } from '@/shared/api/assets'
import { fileUrl } from '@/shared/api/files'
import {
  ingestApi,
  type IngestMethod,
  type IngestPlanResult,
  type IngestRegisterResult,
} from '@/shared/api/ingest'

const props = defineProps<{
  open: boolean
  pid: string
}>()

const emit = defineEmits<{
  'update:open': [boolean]
  done: []
}>()

const step = ref<1 | 2 | 3 | 4>(1)
const busy = ref(false)
const error = ref<ApiError | null>(null)

// Step 1 状态
const selectMode = ref<'upload' | 'path'>('upload')
const filePath = ref('')
const uploadFile = ref<File | null>(null)
const uploading = ref(false)
const videoFileInput = ref<HTMLInputElement | null>(null)
const copyIntoProject = ref(true)
const registeredAsset = ref<IngestRegisterResult | null>(null)
const methodsList = ref<IngestMethod[]>([])

// Step 2 状态（预览裁切：片头片尾）
const rangeIn = ref(0)
const rangeOut = ref<number | null>(null)

/**
 * 裁切那一屏要播的地址。资产 `path` 相对工程目录存，所以走 `fileUrl`；
 * **原地引用（工程外的绝对路径）拿不到这个地址**——文件服务只服务工程目录里的东西，
 * 此时不画一个必然加载失败的播放器，改用填秒数那一路（裁切条自己会说明原因）。
 */
const trimSrc = computed(() => {
  const path = registeredAsset.value?.path
  if (!path || /^([a-zA-Z]:[\\/]|\/|\\\\)/.test(path)) return ''
  return fileUrl(props.pid, path)
})

// Step 3 状态
const selectedMethod = ref('auto')
const threshold = ref(0.3)
const minSegment = ref(1.0)
const maxSegment = ref<number | undefined>(10.0)
const chunkSeconds = ref(4.0)
const planResult = ref<IngestPlanResult | null>(null)

// Step 4 状态
const sceneTitle = ref('')
const paramMode = ref<'shared' | 'per_shot'>('shared')
const scenePrompt = ref('')

/** 保留区间总长——账单那条时间条量的是它，不是整个文件（不然裁掉的那截会占着宽度）。 */
const keptSpan = computed(() => {
  const bill = planResult.value
  if (!bill) return 0
  const end = bill.range_out ?? bill.duration ?? 0
  return Math.max(0, end - bill.range_in)
})

watch(
  () => props.open,
  async (opened) => {
    if (opened) {
      step.value = 1
      error.value = null
      selectMode.value = 'upload'
      filePath.value = ''
      uploadFile.value = null
      uploading.value = false
      registeredAsset.value = null
      planResult.value = null
      rangeIn.value = 0
      rangeOut.value = null
      sceneTitle.value = ''
      scenePrompt.value = ''
      maxSegment.value = 10.0
      try {
        methodsList.value = await ingestApi.methods()
      } catch {
        methodsList.value = [
          { method: 'auto', label: '自动（画面切换 → 对白停顿 → 固定长度）' },
          { method: 'scene', label: '按画面切换（scene detect）' },
          { method: 'silence', label: '按对白停顿（silence detect）' },
          { method: 'fixed', label: '按固定长度铺满' },
        ]
      }
    }
  },
)

async function onPickUploadFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || !props.pid) return
  uploadFile.value = file
  uploading.value = true
  busy.value = true
  error.value = null
  try {
    const asset = await assetsApi.upload(props.pid, file, 'upload')
    const res = await ingestApi.register(props.pid, asset.id, true)
    registeredAsset.value = res
    sceneTitle.value = `成片：${file.name.replace(/\.[^.]+$/, '')}`
    //: 登记完先去裁切那一屏，**不急着出账单**：片头片尾没框出来的话，账单里前后两段
    //: 一定是台标和字幕，用户只能一个个删掉。
    step.value = 2
  } catch (err) {
    error.value = err instanceof ApiError ? err : null
  } finally {
    uploading.value = false
    busy.value = false
  }
}

async function handleRegisterPath() {
  if (!filePath.value.trim()) return
  busy.value = true
  error.value = null
  try {
    const res = await ingestApi.register(props.pid, filePath.value.trim(), copyIntoProject.value)
    registeredAsset.value = res
    sceneTitle.value = `成片：${res.path.split(/[/\\]/).pop()?.replace(/\.[^.]+$/, '') || '导入片段'}`
    step.value = 2
  } catch (err) {
    error.value = err instanceof ApiError ? err : null
  } finally {
    busy.value = false
  }
}

/** 裁切确认：进账单那一屏，顺手出第一份账单。 */
async function goToPlan() {
  step.value = 3
  await fetchPlan()
}

async function fetchPlan() {
  if (!registeredAsset.value) return
  busy.value = true
  error.value = null
  try {
    planResult.value = await ingestApi.plan(props.pid, {
      asset_id: registeredAsset.value.id,
      method: selectedMethod.value,
      threshold: threshold.value,
      min_segment: minSegment.value,
      max_segment: maxSegment.value && maxSegment.value > 0 ? maxSegment.value : undefined,
      chunk_seconds: chunkSeconds.value,
      //: 片头片尾一路带下去：账单、落库两处必须是同一对数字，不然「切出来跟预览不一样」。
      range_in: rangeIn.value > 0 ? rangeIn.value : undefined,
      range_out: rangeOut.value ?? undefined,
    })
  } catch (err) {
    error.value = err instanceof ApiError ? err : null
    planResult.value = null
  } finally {
    busy.value = false
  }
}

async function handleRun() {
  if (!registeredAsset.value || !planResult.value) return
  busy.value = true
  error.value = null
  try {
    await ingestApi.run(props.pid, {
      asset_id: registeredAsset.value.id,
      title: sceneTitle.value.trim() || undefined,
      prompt: scenePrompt.value.trim() || undefined,
      param_mode: paramMode.value,
      method: selectedMethod.value,
      threshold: threshold.value,
      min_segment: minSegment.value,
      max_segment: maxSegment.value && maxSegment.value > 0 ? maxSegment.value : undefined,
      chunk_seconds: chunkSeconds.value,
      cuts: planResult.value.cuts,
      range_in: rangeIn.value > 0 ? rangeIn.value : undefined,
      range_out: rangeOut.value ?? undefined,
    })
    emit('done')
    emit('update:open', false)
  } catch (err) {
    error.value = err instanceof ApiError ? err : null
  } finally {
    busy.value = false
  }
}

function fmtSec(sec: number | null | undefined): string {
  if (sec == null) return '未知'
  const m = Math.floor(sec / 60)
  const s = (sec % 60).toFixed(1)
  return m > 0 ? `${m}分${s}秒` : `${s}秒`
}
</script>

<template>
  <AppDialog
    :open="open"
    title="导入长视频加工与切段"
    subtitle="零文件复制：自动切分段落为独立镜头并保留区间"
    size="lg"
    @update:open="emit('update:open', $event)"
  >
    <template #icon>
      <Scissors :size="14" class="text-accent" />
    </template>

    <div class="space-y-3 p-3">
      <!-- 步骤指示条 -->
      <div class="border-line-1 bg-base-2 flex items-center justify-between border px-3 py-1.5 text-2xs">
        <span :class="step === 1 ? 'text-accent font-medium' : 'text-fg-3'">1. 选片登记</span>
        <span class="text-fg-4">→</span>
        <span :class="step === 2 ? 'text-accent font-medium' : 'text-fg-3'">2. 预览裁片头片尾</span>
        <span class="text-fg-4">→</span>
        <span :class="step === 3 ? 'text-accent font-medium' : 'text-fg-3'">3. 智能切段账单</span>
        <span class="text-fg-4">→</span>
        <span :class="step === 4 ? 'text-accent font-medium' : 'text-fg-3'">4. 幕参数与生成</span>
      </div>

      <ErrorPanel v-if="error" :error="error" @dismiss="error = null" />

      <!-- Step 1: 登记视频 -->
      <div v-if="step === 1" class="space-y-3">
        <!-- 方式切换 -->
        <div class="flex items-center border-line-1 border-b bg-base-2 text-2xs">
          <button
            type="button"
            class="flex-1 py-1.5 font-medium text-center transition-colors border-r border-line-1"
            :class="selectMode === 'upload' ? 'bg-base-1 text-accent' : 'text-fg-4 hover:text-fg-2'"
            @click="selectMode = 'upload'"
          >
            选择本地视频（推荐）
          </button>
          <button
            type="button"
            class="flex-1 py-1.5 font-medium text-center transition-colors"
            :class="selectMode === 'path' ? 'bg-base-1 text-accent' : 'text-fg-4 hover:text-fg-2'"
            @click="selectMode = 'path'"
          >
            填写本机绝对路径（超大文件/原地引用）
          </button>
        </div>

        <!-- 模式 A: 浏览器直接选择上传 -->
        <div v-if="selectMode === 'upload'" class="space-y-2">
          <div
            class="border-dashed border-2 border-line-1 bg-base-2 hover:bg-base-3 p-6 text-center rounded cursor-pointer transition-colors"
            :class="{ 'opacity-60 pointer-events-none': uploading }"
            @click="videoFileInput?.click()"
          >
            <UploadCloud :size="28" class="mx-auto text-accent mb-2" />
            <p class="text-xs text-fg-1 font-medium">
              {{ uploadFile ? uploadFile.name : '点击选择本地成片视频文件' }}
            </p>
            <p v-if="uploading" class="text-accent text-2xs mt-2 animate-pulse font-medium">
              正在上传并登记视频，请稍候...
            </p>
            <p v-else class="text-fg-4 text-2xs mt-1">
              支持 MP4、MOV、MKV、WebM、AVI 格式，系统将自动安全导入工程资产
            </p>
            <input
              ref="videoFileInput"
              type="file"
              accept="video/*"
              class="hidden"
              @change="onPickUploadFile"
            />
          </div>
        </div>

        <!-- 模式 B: 绝对路径手动登记 -->
        <div v-else class="space-y-3">
          <label class="block">
            <span class="text-fg-3 text-2xs font-medium">成片文件本机绝对路径</span>
            <input
              v-model="filePath"
              type="text"
              placeholder="例如: D:\videos\my_render.mp4"
              class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-1 h-7 w-full border px-2 text-xs outline-none"
            />
          </label>

          <div class="border-line-1 bg-base-2 space-y-2 border p-2.5">
            <label class="flex cursor-pointer items-center gap-2 text-xs">
              <input v-model="copyIntoProject" type="checkbox" class="accent-accent" />
              <span class="text-fg-2 font-medium">复制进工程目录（推荐）</span>
            </label>
            <p class="text-fg-4 text-2xs leading-relaxed">
              复制进工程可保证整个项目目录拷走依然有效；若取消勾选（原地引用），虽然节省磁盘空间，但源文件一旦移动或重命名，所有镜头都将无法读取。
            </p>
          </div>
        </div>
      </div>

      <!-- Step 2: 预览并裁掉片头片尾（切段之前的那一步） -->
      <div v-else-if="step === 2" class="space-y-2">
        <div
          v-if="registeredAsset"
          class="border-line-1 bg-base-2 flex items-center justify-between border p-2 text-2xs"
        >
          <div class="flex items-center gap-1.5">
            <Film :size="12" class="text-accent" />
            <span class="text-fg-1 max-w-xs truncate font-medium">{{ registeredAsset.path }}</span>
          </div>
          <div class="text-fg-3 flex items-center gap-3">
            <span>
              时长: <strong class="text-fg-1">{{ fmtSec(registeredAsset.duration) }}</strong>
            </span>
            <span>
              音频轨: <strong class="text-fg-1">{{ registeredAsset.has_audio ? '有' : '无' }}</strong>
            </span>
          </div>
        </div>

        <ul
          v-if="registeredAsset?.warnings.length"
          class="border-st-review/40 bg-st-review/5 text-st-review space-y-0.5 border p-2 text-2xs"
        >
          <li v-for="(w, i) in registeredAsset.warnings" :key="i">· {{ w }}</li>
        </ul>

        <VideoTrimmer
          v-if="trimSrc"
          :key="trimSrc"
          :src="trimSrc"
          :duration="registeredAsset?.duration ?? null"
          :range-in="rangeIn"
          :range-out="rangeOut"
          @update:range-in="rangeIn = $event"
          @update:range-out="rangeOut = $event"
        />
        <!--
          原地引用的文件在工程目录之外，文件服务不给它地址（越界会被拒），所以放不出画面。
          这里不画一个必然加载失败的播放器，只留填秒数那一路——切段与导出走 FFmpeg，不受影响。
        -->
        <div v-else class="border-line-1 bg-base-2 space-y-2 border p-2.5">
          <p class="text-st-review text-2xs leading-relaxed">
            这段视频在工程目录之外（原地引用），浏览器里放不出来，所以没法拖着裁。
            片头片尾可以直接填秒数；要能预览就回上一步改成「复制进工程」。
          </p>
          <div class="grid grid-cols-2 gap-2">
            <label class="block">
              <span class="text-fg-4 text-2xs">片头结束（秒）</span>
              <input
                v-model.number="rangeIn"
                type="number"
                min="0"
                step="0.1"
                class="border-line-1 bg-base-2 text-fg-1 mt-1 h-6 w-full border px-1.5 text-2xs outline-none"
              />
            </label>
            <label class="block">
              <div class="flex items-center justify-between">
                <span class="text-fg-4 text-2xs">片尾开始（秒）</span>
                <span class="text-fg-4 text-2xs">空 = 到结尾</span>
              </div>
              <input
                :value="rangeOut ?? ''"
                type="number"
                min="0"
                step="0.1"
                placeholder="到文件结尾"
                class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 mt-1 h-6 w-full border px-1.5 text-2xs outline-none"
                @change="
                  rangeOut =
                    ($event.target as HTMLInputElement).value.trim() === ''
                      ? null
                      : Number(($event.target as HTMLInputElement).value)
                "
              />
            </label>
          </div>
        </div>
      </div>

      <!-- Step 3: 切段配置与账单 -->
      <div v-else-if="step === 3" class="space-y-3">
        <!-- 视频探测摘要 -->
        <div v-if="registeredAsset" class="border-line-1 bg-base-2 flex items-center justify-between border p-2 text-2xs">
          <div class="flex items-center gap-1.5">
            <Film :size="12" class="text-accent" />
            <span class="text-fg-1 truncate font-medium max-w-xs">{{ registeredAsset.path }}</span>
          </div>
          <div class="text-fg-3 flex items-center gap-3">
            <span>时长: <strong class="text-fg-1">{{ fmtSec(registeredAsset.duration) }}</strong></span>
            <span>音频轨: <strong class="text-fg-1">{{ registeredAsset.has_audio ? '有' : '无' }}</strong></span>
          </div>
        </div>

        <!-- 切段算法选择 -->
        <div class="grid grid-cols-2 gap-2">
          <label class="block">
            <span class="text-fg-4 text-2xs">切段算法</span>
            <select
              v-model="selectedMethod"
              class="border-line-1 bg-base-2 text-fg-1 mt-1 h-6 w-full border px-1.5 text-2xs outline-none"
              @change="fetchPlan"
            >
              <option v-for="m in methodsList" :key="m.method" :value="m.method">
                {{ m.label }}
              </option>
            </select>
          </label>
          <label v-if="selectedMethod === 'scene' || selectedMethod === 'auto'" class="block">
            <span class="text-fg-4 text-2xs">画面切换灵敏度阈值 ({{ threshold }})</span>
            <input
              v-model.number="threshold"
              type="range"
              min="0.1"
              max="0.9"
              step="0.05"
              class="accent-accent mt-2 w-full"
              @change="fetchPlan"
            />
          </label>
        </div>

        <div class="grid grid-cols-3 gap-2">
          <label class="block">
            <span class="text-fg-4 text-2xs">单段最小时长 (秒)</span>
            <input
              v-model.number="minSegment"
              type="number"
              min="0.5"
              step="0.5"
              class="border-line-1 bg-base-2 text-fg-1 mt-1 h-6 w-full border px-1.5 text-2xs outline-none"
              @change="fetchPlan"
            />
          </label>
          <label class="block">
            <div class="flex items-center justify-between">
              <span class="text-fg-4 text-2xs">单段最大限制 (秒)</span>
              <span class="text-fg-4 text-2xs">空=不限</span>
            </div>
            <input
              v-model.number="maxSegment"
              type="number"
              min="1.0"
              step="1.0"
              placeholder="例如 10.0"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 mt-1 h-6 w-full border px-1.5 text-2xs outline-none"
              @change="fetchPlan"
            />
          </label>
          <label v-if="selectedMethod === 'fixed' || selectedMethod === 'auto'" class="block">
            <span class="text-fg-4 text-2xs">兜底固定窗口 (秒)</span>
            <input
              v-model.number="chunkSeconds"
              type="number"
              min="1.0"
              step="0.5"
              class="border-line-1 bg-base-2 text-fg-1 mt-1 h-6 w-full border px-1.5 text-2xs outline-none"
              @change="fetchPlan"
            />
          </label>
        </div>

        <!-- 账单结果列表 -->
        <div v-if="planResult" class="space-y-1.5 border-line-1 border-t pt-2">
          <div class="flex items-center justify-between text-2xs">
            <span class="text-fg-2 font-medium">切段预览（共 {{ planResult.total }} 段 · {{ planResult.method_label }}）</span>
            <AppButton size="sm" variant="ghost" :disabled="busy" @click="fetchPlan">重新计算</AppButton>
          </div>

          <!--
            「少了一截」必须是账单上看得见的一句话：片头片尾去掉了多少秒写在这里，
            否则过两天回来看只会觉得第一段莫名不是从 0 秒开始。
          -->
          <p
            v-if="planResult.trimmed_head || planResult.trimmed_tail"
            class="text-fg-3 text-2xs"
          >
            只切 {{ planResult.range_in.toFixed(2) }}s ~
            {{ planResult.range_out == null ? '结尾' : `${planResult.range_out.toFixed(2)}s` }}：
            已去掉片头 {{ planResult.trimmed_head.toFixed(2) }} 秒 / 片尾
            {{ planResult.trimmed_tail.toFixed(2) }} 秒（源文件没有被裁）。
          </p>

          <!-- 可视化时间条：量的是保留区间，不是整个文件 -->
          <div class="bg-base-3 flex h-3 w-full overflow-hidden rounded-xs">
            <div
              v-for="seg in planResult.segments"
              :key="seg.index_no"
              class="border-base-1 border-r bg-accent/70 transition-all hover:bg-accent"
              :style="{ width: `${Math.max(2, (seg.duration / (keptSpan || 1)) * 100)}%` }"
              :title="`第 ${seg.index_no} 段: ${seg.in_point}s ~ ${seg.out_point}s (${seg.duration}s)`"
            />
          </div>

          <!-- 段落详细列表 -->
          <div class="border-line-1 max-h-40 overflow-y-auto border p-1 space-y-1 bg-base-2">
            <div
              v-for="seg in planResult.segments"
              :key="seg.index_no"
              class="flex items-center justify-between px-2 py-1 bg-base-1 text-2xs rounded-xs"
            >
              <span class="text-fg-1 font-medium">第 {{ seg.index_no }} 段</span>
              <span class="text-fg-3 tnum">{{ seg.in_point.toFixed(1) }}s ~ {{ seg.out_point.toFixed(1) }}s</span>
              <AppBadge tone="neutral">{{ seg.duration.toFixed(1) }}s</AppBadge>
            </div>
          </div>

          <!-- 拿不准的事情一律显示出来（合并掉的切点不是错误，但必须说） -->
          <p v-if="planResult.merged_away.length" class="text-fg-4 text-2xs">
            有 {{ planResult.merged_away.length }} 个切点挨得太近被合并了（比「单段最小时长」还短）：
            {{ planResult.merged_away.map((c) => c.toFixed(2)).join('、') }}s
          </p>
          <ul
            v-if="planResult.warnings.length"
            class="border-st-review/40 bg-st-review/5 text-st-review space-y-0.5 border p-2 text-2xs"
          >
            <li v-for="(w, i) in planResult.warnings" :key="i">· {{ w }}</li>
          </ul>
        </div>
      </div>

      <!-- Step 4: 幕参数设置 -->
      <div v-else-if="step === 4" class="space-y-3">
        <label class="block">
          <span class="text-fg-3 text-2xs font-medium">生成的幕标题</span>
          <input
            v-model="sceneTitle"
            type="text"
            class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-1 h-7 w-full border px-2 text-xs outline-none"
          />
        </label>

        <div class="border-line-1 bg-base-2 p-2.5 border space-y-2">
          <span class="text-fg-3 text-2xs font-medium block">参数模式 (Param Mode)</span>
          <div class="flex gap-4 text-xs">
            <label class="flex items-center gap-1.5 cursor-pointer">
              <input v-model="paramMode" type="radio" value="shared" class="accent-accent" />
              <span class="text-fg-1 font-medium">共用参数（默认）</span>
            </label>
            <label class="flex items-center gap-1.5 cursor-pointer">
              <input v-model="paramMode" type="radio" value="per_shot" class="accent-accent" />
              <span class="text-fg-1 font-medium">每段独立</span>
            </label>
          </div>
          <p class="text-fg-4 text-2xs">
            共用参数模式下，镜头留空即继承幕级 Prompt 与设置，修改幕级一处，全部 {{ planResult?.total || 0 }} 段自动跟随生效。
          </p>
        </div>

        <label class="block">
          <span class="text-fg-3 text-2xs font-medium">整幕共用 Prompt（可选）</span>
          <textarea
            v-model="scenePrompt"
            rows="3"
            placeholder="为导入的切段镜头提供统一的画风/角色/场景描述..."
            class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 mt-1 w-full resize-none border p-2 text-xs outline-none"
          />
        </label>
      </div>
    </div>

    <template #footer>
      <AppButton v-if="step > 1" size="sm" variant="ghost" :disabled="busy" @click="step--">
        <ArrowLeft :size="12" />上一步
      </AppButton>
      <div class="ml-auto flex items-center gap-2">
        <AppButton size="sm" variant="ghost" @click="emit('update:open', false)">取消</AppButton>
        <AppButton
          v-if="step === 1 && selectMode === 'path'"
          size="sm"
          variant="primary"
          :disabled="busy || !filePath.trim()"
          @click="handleRegisterPath"
        >
          登记并预览<ArrowRight :size="12" />
        </AppButton>
        <AppButton
          v-else-if="step === 2"
          size="sm"
          variant="primary"
          :disabled="busy || !registeredAsset"
          title="按框出来的区间出切段账单（源文件不动）"
          @click="goToPlan"
        >
          裁好了，出切段账单<ArrowRight :size="12" />
        </AppButton>
        <AppButton
          v-else-if="step === 3"
          size="sm"
          variant="primary"
          :disabled="busy || !planResult?.segments.length"
          @click="step = 4"
        >
          配置幕参数<ArrowRight :size="12" />
        </AppButton>
        <AppButton
          v-else-if="step === 4"
          size="sm"
          variant="primary"
          :disabled="busy"
          @click="handleRun"
        >
          <CheckCircle2 :size="12" />确认导入分镜 ({{ planResult?.total || 0 }} 镜)
        </AppButton>
      </div>
    </template>
  </AppDialog>
</template>
