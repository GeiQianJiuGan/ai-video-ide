<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RefreshCw, Trash2, Upload } from '@lucide/vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import StatusDot from '@/shared/ui/StatusDot.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import FeatureHeader from '@/shared/ui/FeatureHeader.vue'
import PresetDefaultBadges from '@/shared/ui/PresetDefaultBadges.vue'
import PresetDefaultButtons from '@/shared/ui/PresetDefaultButtons.vue'
import { IMAGE_PRESET_INERT } from '@/shared/lib/presets'
import { useSettingsStore } from '@/stores/settings'

const cfg = useSettingsStore()
const fileInput = ref<HTMLInputElement | null>(null)
const name = ref('')
const text = ref('')

onMounted(() => void cfg.load())

/**
 * **出图那份图与出画面那份图分两栏列。**
 *
 * 这一页以前把两者混在一张表里，而它们的入口标题几乎完全一样（AIVS_PROMPT /
 * AIVS_NEGATIVE / AIVS_SEED / AIVS_REF_*），于是「哪一份是出图用的」只能靠预设名字自己
 * 记——挑错一次就是一次白跑（把 T2I 图提交给 R2V，只会得到一张图或一个报错）。
 *
 * 分栏依据是后端那个**声明**（`declares_image`，来自图里那个 AIVS_IMAGE 标题），
 * **前端不照节点标题再算一遍**：标题分不出 T2I 与 R2V，这件事只有声明说得清。
 * 同一个原因，`t2i_ready` / `r2v_ready` / `flf_ready` 三个徽标可以无条件画——
 * 声明过的图在后两个上必为假，没声明的图在第一个上必为假。
 *
 * **应用级默认也在这一页设**（`column` 直接就是分栏的 key，两颗共用件照它挑角色）：
 * 出画面那一栏有 R2V / 首尾帧 / 共用三种，出图那一栏有出图默认。以前这几颗按钮只在设置页
 * 有，而且只有共用那一格与出图那一格——于是「工程没绑就跟随设置页」在首尾帧上必然落到一份
 * 不能用的图上，用户只能回到每个工程里各绑一次。
 */
const groups = computed(() => {
  const items = cfg.presets?.items ?? []
  // 出图那条链的调用方式不认预设时，这一栏的「设为出图默认」照旧能按（藏起来就等于没有入口），
  // 但「暂时用不上」这句话要摆在明面上，而不是只藏在 tooltip 里。
  const inert = cfg.draftImageProtocol?.wants_preset ? '' : IMAGE_PRESET_INERT
  return [
    {
      key: 'video',
      title: '出画面（R2V / 补转场）',
      note: '没标 AIVS_IMAGE 的图都在这一栏：镜头生成、补转场、二次处理、出声音从这里选。这里设的三种默认就是「工程没有单独绑预设时按哪一份出」——按角色那两项留空则退回共用那份。',
      caveat: '',
      empty: '这一栏还没有图。出画面那份图不要标 AIVS_IMAGE。',
      rows: items.filter((row) => !row.declares_image),
    },
    {
      key: 'image',
      title: '出图（T2I / 图生图）',
      note: '标了 AIVS_IMAGE 的图在这一栏：角色四视图 / 地点参考图 / 道具图 / 首末帧候选走它。',
      caveat: inert,
      empty:
        '还没有出图那份图，所以角色四视图 / 地点参考图只能手动上传。把出图那份 T2I 图里任意一个节点（例如 SaveImage）的标题改成 AIVS_IMAGE，用「Save (API Format)」导出后上传到这里。',
      rows: items.filter((row) => row.declares_image),
    },
  ]
})

/** 一份预设都没有时只说一句话，两栏各说一遍「这一栏是空的」只是噪音。 */
const empty = computed(() => !(cfg.presets?.items ?? []).length)

async function pick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  await cfg.uploadPreset(file).catch(() => {})
}

async function save(): Promise<void> {
  if (!name.value.trim() || !text.value.trim()) return
  await cfg.savePreset(name.value.trim(), text.value).catch(() => {})
  if (!cfg.lastError) {
    name.value = ''
    text.value = ''
  }
}
</script>

<template>
  <div class="min-h-0 flex-1 overflow-auto p-2">
    <FeatureHeader />
    <ErrorPanel
      v-if="cfg.lastError"
      :error="cfg.lastError"
      class="mb-2"
      @dismiss="cfg.clearError()"
    />
    <AppPanel title="预设 Workflow（ComfyUI API 图）">
      <template #actions>
        <AppButton size="sm" variant="ghost" :disabled="cfg.busy" @click="cfg.loadPresets()">
          <RefreshCw :size="10" />刷新
        </AppButton>
        <AppButton size="sm" variant="primary" :disabled="cfg.busy" @click="fileInput?.click()">
          <Upload :size="10" />上传 API json
        </AppButton>
        <input
          ref="fileInput"
          type="file"
          accept=".json,application/json"
          class="hidden"
          @change="pick"
        />
      </template>
      <!--
        「怎么把图改成本工具认的样子」由后端给（`presets.HOW_TO`）：这一页以前把那一串
        AIVS_* 抄了一份在这儿，于是加了 AIVS_IMAGE 这类新约定之后，两处说的不是一件事。
      -->
      <ul v-if="cfg.presets" class="text-fg-4 border-line-1 border-b px-3 py-1.5 text-2xs">
        <li>预设目录：{{ cfg.presets.dir }}</li>
        <li v-for="line in cfg.presets.how_to" :key="line">· {{ line }}</li>
      </ul>
      <template v-if="!empty">
        <section v-for="group in groups" :key="group.key">
          <div class="border-line-1 bg-base-2 border-b px-3 py-1">
            <div class="text-fg-2 text-2xs">
              {{ group.title }}
              <span class="text-fg-4">· {{ group.rows.length }} 份</span>
            </div>
            <p class="text-fg-4 text-2xs">{{ group.note }}</p>
            <!--
              出图协议不认预设时那句话：藏在 tooltip 里等于没说（硬约束 4）。
              用告警色而不是失败色——**指了也没坏**，只是这条链现在换了调用方式所以暂时用不上。
            -->
            <p v-if="group.caveat" class="text-st-review text-2xs">{{ group.caveat }}</p>
          </div>
          <ul class="divide-line-1 divide-y">
            <li v-for="row in group.rows" :key="row.name" class="px-3 py-2">
              <div class="flex flex-wrap items-center gap-2">
                <StatusDot :status="row.ready ? 'completed' : 'failed'" />
                <span class="text-fg-1 text-xs">{{ row.name }}</span>
                <!-- 「这一份现在是哪几种默认」：只画这一栏的那几种（video 三种 / image 一种）。 -->
                <PresetDefaultBadges :row="row" :column="group.key" />
                <AppBadge v-if="row.ready" :tone="row.ref_slots ? 'neutral' : 'warn'">
                  参考图 {{ row.ref_slots }} 槽
                </AppBadge>
                <!--
                  参考视频 / 参考音频只在真标了槽位时才画：**0 是常态**（绝大多数图只收图片），
                  给每一份预设都挂一个「参考音频 0 槽」只会把「参考图 0 槽」那个真问题埋掉。
                -->
                <AppBadge v-if="row.ready && row.ref_video_slots" tone="neutral">
                  参考视频 {{ row.ref_video_slots }} 槽
                </AppBadge>
                <AppBadge v-if="row.ready && row.ref_audio_slots" tone="neutral">
                  参考音频 {{ row.ref_audio_slots }} 槽
                </AppBadge>
                <AppBadge v-if="row.t2i_ready" tone="ok">T2I</AppBadge>
                <!--
                  声明了出图却没有 AIVS_PROMPT：这份图在出图那一栏里也是不能用的
                  （本工具没法告诉它要画什么），后端把原因写在 `impact` 里。
                -->
                <AppBadge v-if="row.declares_image && !row.prompt_ok" tone="fail">
                  缺 AIVS_PROMPT
                </AppBadge>
                <AppBadge v-if="row.r2v_ready" tone="ok">R2V</AppBadge>
                <AppBadge v-if="row.flf_ready" tone="accent">FL2VA</AppBadge>
                <span class="text-fg-4 min-w-0 flex-1 truncate text-2xs">
                  {{ row.ready ? row.ref_hint : row.impact }}
                </span>
                <!--
                  「设为某种默认」就在这一行末尾：以前只在设置页有，于是这一页认得出哪份是出图那份，
                  却没有任何入口把它设成默认（用户报的就是这件事）。`column` 决定画哪几颗。
                -->
                <PresetDefaultButtons :row="row" :column="group.key" />
                <AppButton
                  size="sm"
                  variant="danger"
                  :disabled="cfg.busy"
                  @click="cfg.removePreset(row.name)"
                >
                  <Trash2 :size="10" />
                </AppButton>
              </div>
            </li>
            <li v-if="!group.rows.length" class="text-fg-4 px-3 py-2 text-2xs">
              {{ group.empty }}
            </li>
          </ul>
        </section>
      </template>
      <p v-else class="text-fg-4 px-3 py-3 text-2xs">
        还没有预设 Workflow。请从 ComfyUI 导出 API 格式 json 后导入——出画面那份图与出图那份 T2I
        图各存一份，后者要给图里任意一个节点加 AIVS_IMAGE 标题。
      </p>
      <div class="border-line-1 space-y-1 border-t p-2">
        <input
          v-model="name"
          placeholder="预设名，例如 minimax-h3-fast"
          class="border-line-1 bg-base-2 text-fg-1 h-6 w-full border px-1.5 text-2xs outline-none"
        />
        <textarea
          v-model="text"
          rows="5"
          placeholder="也可以直接粘贴 API 格式 json"
          class="border-line-1 bg-base-2 text-fg-1 w-full border px-1.5 py-1 font-mono text-2xs outline-none"
        />
        <AppButton
          size="sm"
          variant="primary"
          :disabled="cfg.busy || !name.trim() || !text.trim()"
          @click="save()"
          >保存预设 Workflow</AppButton
        >
      </div>
    </AppPanel>
  </div>
</template>
