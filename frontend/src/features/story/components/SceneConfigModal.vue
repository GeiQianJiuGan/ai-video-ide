<script setup lang="ts">
/**
 * 幕设定与共用参数配置弹窗 (SceneConfigModal)。
 *
 * 让用户可以在分镜泳道板上一键直接配置：
 *   1. 幕标题与简介；
 *   2. 参数模式（共用参数 shared / 逐镜独立 per_shot）；
 *   3. 本幕共用 Prompt（同幕内未单独填写 Prompt 的镜头自动继承此句）；
 *   4. 本幕出场角色（同幕内镜头默认继承出场人物表）；
 *   5. 本幕地点变体（同幕内镜头默认继承地点设定）。
 */

import { ref, watch } from 'vue'
import { Check, Sparkles, User, MapPin, RefreshCw } from '@lucide/vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import { ApiError } from '@/shared/api/client'
import { storyApi, type StoryboardLane } from '@/shared/api/story'
import { castApi, type Character, type Appearance } from '@/shared/api/cast'
import { worldApi, type Location, type LocationVariant } from '@/shared/api/world'

const props = defineProps<{
  open: boolean
  pid: string
  lane: StoryboardLane | null
}>()

const emit = defineEmits<{
  'update:open': [boolean]
  done: []
}>()

const busy = ref(false)
const error = ref<ApiError | null>(null)

// 表单字段
const title = ref('')
const summary = ref('')
const prompt = ref('')
const paramMode = ref<'shared' | 'per_shot'>('shared')
const selectedLocationVariantId = ref<string | null>(null)
const selectedCastIds = ref<string[]>([])
const clearShotOverrides = ref(false)

// 项目级资源
interface AppearanceOption {
  appearance: Appearance
  character: Character
}
interface VariantOption extends LocationVariant {
  locationName: string
}

const appearances = ref<AppearanceOption[]>([])
const locations = ref<Location[]>([])
const variants = ref<VariantOption[]>([])

async function loadProjectContext(): Promise<void> {
  if (!props.pid) return
  try {
    const [chars, locs] = await Promise.all([
      castApi.characters(props.pid),
      worldApi.locations(props.pid),
    ])
    const nested = await Promise.all(
      chars.map(async (c: Character) => ({
        c,
        rows: await castApi.appearances(props.pid, c.id),
      })),
    )
    appearances.value = nested.flatMap(({ c, rows }: { c: Character; rows: Appearance[] }) =>
      rows.map((row: Appearance) => ({ appearance: row, character: c })),
    )
    locations.value = locs
    variants.value = locs.flatMap((loc) =>
      loc.variants.map((v) => ({ ...v, locationName: loc.name })),
    )
  } catch (err) {
    if (err instanceof ApiError) error.value = err
  }
}

watch(
  () => props.open,
  async (opened) => {
    if (opened && props.lane) {
      error.value = null
      title.value = props.lane.title || ''
      summary.value = props.lane.summary || ''
      prompt.value = props.lane.prompt || ''
      paramMode.value = props.lane.param_mode || 'shared'
      selectedLocationVariantId.value = props.lane.location_variant_id || null
      selectedCastIds.value = [...(props.lane.cast_appearance_ids || [])]
      clearShotOverrides.value = false
      await loadProjectContext()
    }
  },
  { immediate: true },
)

function toggleCast(appearanceId: string): void {
  const index = selectedCastIds.value.indexOf(appearanceId)
  if (index >= 0) {
    selectedCastIds.value.splice(index, 1)
  } else {
    selectedCastIds.value.push(appearanceId)
  }
}

const isCastSelected = (aid: string) => selectedCastIds.value.includes(aid)

async function handleSave(): Promise<void> {
  if (!props.pid || !props.lane || busy.value) return
  busy.value = true
  error.value = null
  try {
    const sid = props.lane.id

    // 1. 更新 Scene 属性
    await storyApi.updateScene(props.pid, sid, {
      title: title.value.trim() || undefined,
      summary: summary.value.trim() || undefined,
      prompt: prompt.value.trim() || undefined,
      param_mode: paramMode.value,
      location_variant_id: selectedLocationVariantId.value || undefined,
    })

    // 2. 更新 Scene Cast
    await storyApi.setSceneCast(props.pid, sid, selectedCastIds.value)

    // 3. 更新 Scene Location
    if (selectedLocationVariantId.value) {
      await storyApi.setSceneLocations(props.pid, sid, [selectedLocationVariantId.value])
    }

    // 4. 如果勾选了清空 Shot 的独立覆盖，让所有 Shot 全部继承本幕 Prompt
    if (clearShotOverrides.value && props.lane.shots.length > 0) {
      await Promise.all(
        props.lane.shots.map((shot) =>
          storyApi.updateShot(props.pid, shot.id, { prompt: '' }).catch(() => {}),
        ),
      )
    }

    emit('done')
    emit('update:open', false)
  } catch (err) {
    if (err instanceof ApiError) error.value = err
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AppDialog
    :open="open"
    :title="`第 ${lane?.index_no ?? ''} 幕设定与共用参数`"
    subtitle="配置本幕共用 Prompt、出场人物与地点；未单独配置的 Shot 会自动继承此处设定"
    size="md"
    @update:open="emit('update:open', $event)"
  >
    <form class="space-y-4 p-4 text-2xs" @submit.prevent="handleSave">
      <ErrorPanel v-if="error" :error="error" class="mb-3" />

      <!-- 幕标题与参数模式 -->
      <div class="grid grid-cols-2 gap-3">
        <label class="block">
          <span class="text-fg-3 font-medium">幕标题</span>
          <input
            v-model="title"
            type="text"
            required
            placeholder="例如：赛博街头初遇"
            class="border-line-1 bg-base-2 text-fg-1 mt-1 h-7 w-full border px-2 text-2xs outline-none focus:border-accent"
          />
        </label>

        <label class="block">
          <span class="text-fg-3 font-medium">参数继承模式</span>
          <select
            v-model="paramMode"
            class="border-line-1 bg-base-2 text-fg-1 mt-1 h-7 w-full border px-2 text-2xs outline-none focus:border-accent"
          >
            <option value="shared">共用参数（推荐：所有 Shot 默认继承本幕设定）</option>
            <option value="per_shot">逐镜独立（新建 Shot 时独立配置）</option>
          </select>
        </label>
      </div>

      <!-- 本幕共用 Prompt -->
      <div class="border-line-1 bg-base-2/50 border p-2.5 rounded-none space-y-1.5">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-1.5">
            <Sparkles :size="12" class="text-accent" />
            <span class="text-fg-1 font-medium">本幕共用 Prompt</span>
            <AppBadge tone="accent">共用继承</AppBadge>
          </div>
          <span class="text-fg-4">{{ prompt.length }} 字</span>
        </div>
        <p class="text-fg-4 leading-relaxed">
          适用于整幕统一的画风、环境光影与镜头基调。本幕内<b>未单独填写 Prompt 的 Shot</b> 将直接以此句作为画面生成指令。
        </p>
        <textarea
          v-model="prompt"
          rows="3"
          placeholder="例如：赛博朋克都市夜晚，霓虹灯光倒映在湿漉漉的街道上，电影级画面质感，8k高画质，电影级调色..."
          class="border-line-1 bg-base-1 text-fg-1 w-full border p-2 text-2xs outline-none focus:border-accent resize-none font-mono"
        />

        <!-- 一键同步/清空选项 -->
        <label v-if="lane && lane.shots.length > 0" class="flex items-center gap-2 pt-1 cursor-pointer text-fg-3 hover:text-fg-1">
          <input v-model="clearShotOverrides" type="checkbox" class="accent-accent" />
          <span>将所有 {{ lane.shots.length }} 个 Shot 的独立 Prompt 清空，强制全部统一继承此共用 Prompt</span>
        </label>
      </div>

      <!-- 本幕出场人物 (Scene Cast) -->
      <div class="space-y-1.5">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-1.5">
            <User :size="12" class="text-accent" />
            <span class="text-fg-1 font-medium">本幕出场人物</span>
            <AppBadge v-if="selectedCastIds.length" tone="ok">已选 {{ selectedCastIds.length }} 个</AppBadge>
            <AppBadge v-else tone="neutral">未选择</AppBadge>
          </div>
        </div>
        <p class="text-fg-4">
          本幕镜头生成画面时，若未在单个 Shot 独立指定出场人物，将<b>自动继承</b>此处选中的角色表进行 Context 组装与特征保持。
        </p>

        <div v-if="appearances.length === 0" class="border-line-1 bg-base-2 border p-3 text-center text-fg-4">
          项目中尚未创建角色或形象，可在左侧导航「角色」中添加。
        </div>
        <div v-else class="grid grid-cols-3 gap-1.5 max-h-36 overflow-y-auto pr-1">
          <div
            v-for="item in appearances"
            :key="item.appearance.id"
            class="border p-1.5 flex items-center gap-2 cursor-pointer transition-colors"
            :class="
              isCastSelected(item.appearance.id)
                ? 'border-accent/80 bg-accent-dim/40 text-fg-1'
                : 'border-line-1 bg-base-2 hover:bg-base-3 text-fg-3'
            "
            @click="toggleCast(item.appearance.id)"
          >
            <div
              class="w-3.5 h-3.5 rounded-sm border flex items-center justify-center shrink-0"
              :class="
                isCastSelected(item.appearance.id)
                  ? 'border-accent bg-accent text-base-1'
                  : 'border-line-2 bg-base-1'
              "
            >
              <Check v-if="isCastSelected(item.appearance.id)" :size="9" class="stroke-[3]" />
            </div>
            <div class="min-w-0 flex-1 truncate">
              <span class="font-medium truncate block">{{ item.character.name }}</span>
              <span class="text-fg-4 text-3xs truncate block">{{ item.appearance.name }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 本幕地点变体 (Scene Location) -->
      <div class="space-y-1.5">
        <div class="flex items-center gap-1.5">
          <MapPin :size="12" class="text-accent" />
          <span class="text-fg-1 font-medium">本幕地点变体</span>
        </div>
        <p class="text-fg-4">
          本幕镜头未指定地点时，将自动继承此地点变体的参考图与环境设定。
        </p>

        <select
          v-model="selectedLocationVariantId"
          class="border-line-1 bg-base-2 text-fg-1 h-7 w-full border px-2 text-2xs outline-none focus:border-accent"
        >
          <option :value="null">（不指定地点）</option>
          <option v-for="v in variants" :key="v.id" :value="v.id">
            {{ v.name }}
          </option>
        </select>
      </div>

      <!-- 底部动作条 -->
      <div class="border-line-1 flex items-center justify-end gap-2 border-t pt-3">
        <AppButton size="sm" variant="ghost" :disabled="busy" @click="emit('update:open', false)">
          取消
        </AppButton>
        <AppButton size="sm" variant="primary" type="submit" :disabled="busy">
          <RefreshCw v-if="busy" :size="11" class="animate-spin" />
          <span>{{ busy ? '保存中...' : '保存幕设定' }}</span>
        </AppButton>
      </div>
    </form>
  </AppDialog>
</template>
