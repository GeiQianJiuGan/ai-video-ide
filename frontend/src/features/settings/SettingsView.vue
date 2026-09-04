<script setup lang="ts">
/**
 * 设置页：可写配置 + 外部依赖状态。
 *
 * 四条约定写在这里，因为它们是「绝不静默失败」在配置页的具体样子：
 *
 *   1. 每一项都标出**值是从哪来的**（配置文件 / 环境变量 / 默认）——排查时唯一有用的信息；
 *   2. 「测试连接」失败显示后端给的四要素错误，**不是一个红叉**；
 *   3. API Key 输入框永远是空的（后端不回明文），敲了才提交；要清除有专门的按钮；
 *   4. **「自动获取」是按 `field.fetch` 画的**，不在这里硬编码「模型这一项特殊」；协议的
 *      默认地址 / 要不要密钥 / 支不支持工具也全部来自后端的协议表，前端不抄一份。
 *   5. **系统提示词（`kind === 'text'`）的内置文案只有后端一份**：灰字占位与「填入内置默认」
 *      都读 `field.builtin`，这一页绝不抄第二份文案。
 *
 * Workflow 绑定页是高级路径（工程的调用方式选了「ComfyUI 工作流绑定」才需要它），
 * 入口收在最后的折叠区里。
 *
 * 左栏那一级菜单见下面 `sections` 那段：设置面板已经十几块，靠滚轮找一组是在碰运气。
 */
import { computed, nextTick, onMounted, ref } from 'vue'
import {
  Check,
  ChevronRight,
  Download,
  FileText,
  GraduationCap,
  PlugZap,
  RefreshCw,
  RotateCcw,
  Save,
  Trash2,
  Upload,
} from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import StatusDot from '@/shared/ui/StatusDot.vue'
import PresetDefaultBadges from '@/shared/ui/PresetDefaultBadges.vue'
import PresetDefaultButtons from '@/shared/ui/PresetDefaultButtons.vue'
import { SOURCE_LABEL, type SettingField, type SettingGroup } from '@/shared/api/settings'
import { useSettingsStore } from '@/stores/settings'
import { useOnboardingStore } from '@/stores/onboarding'
import { useSystemStore } from '@/stores/system'

const cfg = useSettingsStore()
const wiz = useOnboardingStore()
const sys = useSystemStore()

const DEP_TITLE: Record<string, string> = {
  ffmpeg: 'FFmpeg — 抽帧 / 代理转码 / 导出',
  comfyui: 'ComfyUI — 视频与图像生成',
  llm: 'LLM — AI 协作（可选，非必需）',
}

const GROUP_HINT: Record<string, string> = {
  llm: '给「幕」页面的 AI 协作栏用。不配也行——手动编排能走完全程。',
  prompt:
    '「AI 拆出来的场景不够好」多半是这段话不够好，所以它可改。留空 = 用内置默认；JSON 输出形状由系统始终追加，改不坏。',
  video:
    'comfy_preset 直接连 ComfyUI 并按节点标题注参数，模型端的图由模型端维护；http_api 走通用合同。',
  image:
    '角色四视图 / 地点参考图 / 道具图 / 镜头首末帧候选走这一族。不配也行——素材图照旧可以手动上传，只是 AI 那条「顺带出一张图」会跳过并说明原因。',
  comfy: 'comfy_preset 方式下的目标地址，同时也是节点探测与状态栏用的那一个。',
  director:
    '这几项只改「谁按下那一下」：写工具照旧永不落库，落库照旧走同一份实现。免确认开着时协作栏产出的提案在同一个请求里直接落库（右栏不再有待审的卡），「一键全流程」也要它开着才能跑。',
  scene: '一幕里挂多少个人物 / 地点小节点的上限。prompt 是必填的那一个，不受它限制。',
  runtime: '并发数与 FFmpeg。FFmpeg 留空或裸名字表示用应用自带的那份。',
}

/** 哪一组下面挂「测试连接」。 */
const PROBE_OF: Record<string, 'llm' | 'video' | 'image'> = {
  llm: 'llm',
  video: 'video',
  image: 'image',
}

/**
 * 一级菜单里那几块**不是设置字段**的面板：id → 标题，键的顺序就是它们在模板里的顺序。
 * 面板标题也读这张表——两处各写一份的话，改了标题菜单里指的还是旧名字。
 */
const EXTRA_TITLE = {
  presets: '生成预设（ComfyUI 图）',
  deps: '外部依赖',
  advanced: '高级路径',
  events: '实时事件（最近 200 条）',
} as const

const advanced = ref(false)
const presetName = ref('')
const presetText = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

/**
 * 左栏那一级菜单。设置已经十几块面板，靠滚轮找「出图那一族在哪」是在碰运气。
 *
 * 三个取舍：
 *
 *   1. **菜单项 = `cfg.groups` + `EXTRA_TITLE`**：分组与标题是后端给的，这里不抄第二份，
 *      后端加一组设置菜单里就自动多一项；
 *   2. **只滚动，不切换**：所有面板照旧全部渲染。做成 Tab 会把「保存」那一排也关进某一个
 *      分区里，而浏览器的 Ctrl+F 再也搜不到别的分区里那一项；
 *   3. **菜单上标出哪一组有没保存的改动**：这一页的保存是显式的，改完一项滚到别处，
 *      那个「未保存」徽标就跟着看不见了（硬约束 4：待办不藏起来）。
 *
 * 滚动只滚右栏那一层（`scrollIntoView` 会把外层一起滚），算法与分镜板的幕锚点同一套：
 * 滚动容器上加 `relative`，于是 `offsetTop` 就是相对它的偏移。
 */
const scroller = ref<HTMLElement | null>(null)
const sectionEls = ref<Record<string, HTMLElement | null>>({})
const active = ref('')

const sections = computed<SettingGroup[]>(() => [
  ...cfg.groups,
  ...Object.entries(EXTRA_TITLE).map(([id, title]) => ({ id, title })),
])

/** 每一组里有几项改了还没保存。键从字段自己的 `group` 来，这里不猜「哪个键属于哪一组」。 */
const dirtyOf = computed<Record<string, number>>(() => {
  const out: Record<string, number> = {}
  for (const key of cfg.dirtyKeys) {
    const group = cfg.byKey[key]?.group
    if (group) out[group] = (out[group] ?? 0) + 1
  }
  return out
})

/** 面板是组件，函数式 ref 收到的是实例而不是 DOM 节点，所以要取它的根节点。 */
function setSection(id: string, el: unknown): void {
  const node = el instanceof HTMLElement ? el : ((el as { $el?: unknown } | null)?.$el ?? null)
  sectionEls.value[id] = node instanceof HTMLElement ? node : null
}

function jump(id: string): void {
  const box = scroller.value
  const el = sectionEls.value[id]
  if (!box || !el) return
  box.scrollTo({ top: Math.max(0, el.offsetTop - 8), behavior: 'smooth' })
  active.value = id
}

/** 滚到哪一块了：菜单要跟着高亮，否则长页面里不知道自己在哪。 */
function onScroll(): void {
  const box = scroller.value
  if (!box) return
  const list = sections.value
  // 滚到底那一下要单独算：末尾那几块加起来还没一屏高（生成预设 / 外部依赖 / 高级路径 /
  // 实时事件），它们的顶边**永远滚不到视口顶上来**，按「顶边过没过」算的话点了「生成预设」
  // 会被随后的滚动事件弹到「运行」或「实时事件」上——看着就是点了 A 却选中 D。
  // 所以到底时先认已经选中的那一块：只要它还露在视口里就保持不动，都不露了才落到最后一块。
  // `scrollTop > 0` 那个条件是给「内容还不够一屏」兜底的（此时一开页就算「到底」），
  // 那种情况下第一块才是对的。
  if (box.scrollTop > 0 && box.scrollTop + box.clientHeight >= box.scrollHeight - 4) {
    const cur = sectionEls.value[active.value]
    if (cur && cur.offsetTop + cur.offsetHeight > box.scrollTop) return
    active.value = list[list.length - 1]?.id ?? ''
    return
  }
  let current = ''
  for (const sec of list) {
    const el = sectionEls.value[sec.id]
    if (!el) continue
    if (el.offsetTop - 12 <= box.scrollTop) current = sec.id
    else break
  }
  active.value = current || list[0]?.id || ''
}

onMounted(async () => {
  await cfg.load().catch(() => {})
  await nextTick()
  onScroll()
})

function tone(field: SettingField): 'accent' | 'neutral' {
  return field.source === 'file' ? 'accent' : 'neutral'
}

/**
 * 「自动获取」按的是哪一族的协议。
 *
 * `field.fetch` 同时就是设置里的键前缀（`llm` / `image`），所以这里不写第二张
 * 「哪一项属于哪一族」的表——后端加一族只多一个 `fetch` 值，这一页一行不用改。
 * 两族的协议行形状不一样（LLM 有 `supports_tools`，出图有 `supports_refs`），
 * 只取这里真正要用的那三个字段。
 */
function protoOf(field: SettingField): { name: string; label: string; models_hint: string } | null {
  return field.fetch === 'image' ? cfg.draftImageProtocol : cfg.draftProtocol
}

/** 没选协议时按不动这个按钮——理由写进 tooltip，不画一个点了没反应的按钮。 */
function canFetch(field: SettingField): boolean {
  const proto = protoOf(field)
  return Boolean(proto && proto.name !== 'none')
}

function fetchTitle(field: SettingField): string {
  const proto = protoOf(field)
  if (!proto || proto.name === 'none') return '先在上面选一个协议，再来自动获取模型列表'
  return `列出 ${proto.label} 上可用的模型（${proto.models_hint}）`
}

/**
 * 把内置默认那一段填进输入框——给「我想在它基础上改」用。
 *
 * 它和右边那个「恢复内置默认」**不是一回事**：填进来再保存会存成一份覆盖（来源变成配置文件），
 * 而「恢复内置默认」是清除覆盖、回到代码里那一份。文案只有后端一份（`field.builtin`）。
 */
function fillBuiltin(field: SettingField): void {
  cfg.draft[field.key] = field.builtin
}

async function onPickPreset(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file) await cfg.uploadPreset(file).catch(() => {})
}

async function pastePreset(): Promise<void> {
  if (!presetName.value.trim() || !presetText.value.trim()) return
  await cfg.savePreset(presetName.value.trim(), presetText.value).catch(() => {})
  if (!cfg.lastError) {
    presetName.value = ''
    presetText.value = ''
  }
}
</script>
<template>
  <div class="flex min-h-0 flex-1">
    <!--
      一级菜单：**只跳转，不切换视图**（面板照旧全部渲染，理由见 script 里 `sections` 那段）。
      项与标题全部来自 `sections`，这一栏里一个分组名都不写死。
    -->
    <nav class="border-line-1 bg-base-1 flex w-44 shrink-0 flex-col overflow-y-auto border-r">
      <span class="text-fg-4 border-line-1 shrink-0 border-b px-2 py-1 text-2xs">设置分区</span>
      <button
        v-for="sec in sections"
        :key="sec.id"
        type="button"
        class="flex shrink-0 items-center gap-1 border-l-2 px-2 py-1 text-left text-2xs"
        :class="
          active === sec.id
            ? 'border-accent bg-base-2 text-fg-1'
            : 'text-fg-3 hover:text-fg-1 hover:bg-base-2 border-transparent'
        "
        :title="`滚动到「${sec.title}」`"
        @click="jump(sec.id)"
      >
        <span class="min-w-0 flex-1 truncate">{{ sec.title }}</span>
        <!-- 这一组有几项还没保存：改完滚到别处，那个「未保存」徽标就看不见了 -->
        <span v-if="dirtyOf[sec.id]" class="text-st-review shrink-0">
          未保存 <span class="tnum">{{ dirtyOf[sec.id] }}</span>
        </span>
      </button>
    </nav>

    <div
      ref="scroller"
      class="relative min-h-0 min-w-0 flex-1 overflow-auto p-2"
      @scroll="onScroll()"
    >
      <ErrorPanel :error="cfg.lastError" class="mb-2" @dismiss="cfg.clearError()" />

      <!-- 引导的第二个入口（第一个是命令面板）：这一页就是它第三步教人填的地方 -->
      <div class="border-line-1 bg-base-1 mb-2 flex items-center gap-2 border px-3 py-2">
        <span class="text-fg-3 min-w-0 flex-1 text-2xs leading-relaxed">
          不确定这些字段该怎么填？新手引导会带你走一遍：演示工程 → 连上生成服务 → 绑定预设或 API →
          每个功能干什么。
        </span>
        <AppButton size="sm" @click="wiz.reopen()">
          <GraduationCap :size="10" />重新打开新手引导
        </AppButton>
      </div>

      <!-- 可写配置：一组一块 -->
      <AppPanel
        v-for="group in cfg.groups"
        :key="group.id"
        :ref="(el) => setSection(group.id, el)"
        :title="group.title"
        class="mb-2"
      >
        <template #actions>
          <AppButton
            v-if="PROBE_OF[group.id]"
            size="sm"
            :disabled="cfg.probes[PROBE_OF[group.id]!].busy"
            @click="cfg.probe(PROBE_OF[group.id]!)"
          >
            <PlugZap :size="10" />{{
              cfg.probes[PROBE_OF[group.id]!].busy ? '探测中…' : '测试连接'
            }}
          </AppButton>
        </template>

        <p
          v-if="GROUP_HINT[group.id]"
          class="text-fg-4 border-line-1 border-b px-3 py-1.5 text-2xs"
        >
          {{ GROUP_HINT[group.id] }}
        </p>
        <!-- 协议的能力说明来自后端的协议表：加一个协议不用改这一页 -->
        <p
          v-if="group.id === 'llm' && cfg.draftProtocol && cfg.draftProtocol.name !== 'none'"
          class="text-fg-3 border-line-1 border-b px-3 py-1.5 text-2xs"
        >
          {{ cfg.draftProtocol.label }} ·
          <span class="font-mono">{{ cfg.draftProtocol.default_base_url || '无默认地址' }}</span>
          （地址留空即用它）·
          {{ cfg.draftProtocol.needs_key ? '需要 API Key' : '不需要 API Key（本机端）' }} ·
          {{
            cfg.draftProtocol.supports_tools
              ? '支持多轮工具调用'
              : '不支持工具调用 —— AI 协作会退化成一次性产出提案，提案形状完全一样'
          }}
        </p>
        <!--
        出图那一族的同一句话，读的是同一张协议表的另一半。
        `wants_preset` / `supports_refs` 是这一族才有的两件事：前者说「还得指一份 T2I 图」，
        后者说「这个端收不了参考图，带了只会降级并写进账单的 warnings」。
      -->
        <p
          v-if="
            group.id === 'image' && cfg.draftImageProtocol && cfg.draftImageProtocol.name !== 'none'
          "
          class="text-fg-3 border-line-1 border-b px-3 py-1.5 text-2xs"
        >
          {{ cfg.draftImageProtocol.label }} ·
          <span class="font-mono">
            {{ cfg.draftImageProtocol.default_base_url || '无默认地址' }}
          </span>
          （地址留空即用它）·
          {{ cfg.draftImageProtocol.needs_key ? '需要 API Key' : '不需要 API Key（本机端）' }} ·
          {{
            cfg.draftImageProtocol.supports_refs
              ? '能收参考图'
              : '收不了参考图 —— 带了也只会降级，账单里会把跳过哪几张写出来'
          }}
          {{
            cfg.draftImageProtocol.wants_preset
              ? ' · 还要在下面的预设列表里给出图指一份 T2I 图'
              : ''
          }}
        </p>

        <ul class="divide-line-1 divide-y">
          <li v-for="field in cfg.fieldsOf(group.id)" :key="field.key" class="px-3 py-1.5">
            <!-- 长文本（系统提示词）：整行一个 textarea，标签与按钮摆在上面那一行 -->
            <template v-if="field.kind === 'text'">
              <div class="flex items-center gap-2">
                <span class="text-fg-2 text-xs">{{ field.label }}</span>
                <AppBadge :tone="tone(field)">{{ SOURCE_LABEL[field.source] }}</AppBadge>
                <AppBadge v-if="cfg.isDirty(field.key)" tone="warn">未保存</AppBadge>
                <span class="flex-1"></span>
                <AppButton
                  size="sm"
                  variant="ghost"
                  title="把内置那段文案填进下面的框，好在它基础上改（改完记得点保存）"
                  @click="fillBuiltin(field)"
                >
                  <FileText :size="10" />填入内置默认
                </AppButton>
                <AppButton
                  size="sm"
                  variant="ghost"
                  :disabled="field.source !== 'file' || cfg.busy"
                  title="清除这项覆盖，回到代码里那段内置提示词"
                  @click="cfg.clear(field.key)"
                >
                  <RotateCcw :size="10" />恢复内置默认
                </AppButton>
              </div>
              <p v-if="field.impact" class="text-fg-4 mt-0.5 text-2xs">{{ field.impact }}</p>
              <textarea
                :value="String(cfg.draft[field.key] ?? '')"
                rows="10"
                :placeholder="field.builtin"
                class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 mt-1 w-full border px-1.5 py-1 text-2xs leading-relaxed outline-none"
                @input="cfg.draft[field.key] = ($event.target as HTMLTextAreaElement).value"
              ></textarea>
              <p class="text-fg-4 mt-0.5 text-2xs">
                框里是空的就是在用内置默认（上面那段灰字就是它，一字不差）。清空并保存 =
                恢复内置默认。
              </p>
            </template>

            <template v-else>
              <div class="flex items-center gap-2">
                <span class="text-fg-2 w-20 shrink-0 text-xs">{{ field.label }}</span>

                <select
                  v-if="field.kind === 'enum'"
                  :value="cfg.draft[field.key]"
                  class="border-line-1 bg-base-2 text-fg-1 focus:border-accent/60 h-5 min-w-40 border px-1 text-2xs outline-none"
                  @change="cfg.setOne(field.key, ($event.target as HTMLSelectElement).value)"
                >
                  <option v-for="(c, i) in field.choices" :key="c" :value="c">
                    {{ field.choice_labels[i] || c }}
                  </option>
                </select>

                <!-- 开关：勾上就是开。不给「留空 = 默认」那套语义——bool 没有第三种状态 -->
                <label
                  v-else-if="field.kind === 'bool'"
                  class="text-fg-2 flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 text-2xs"
                >
                  <input
                    type="checkbox"
                    :checked="Boolean(cfg.draft[field.key])"
                    class="accent-accent"
                    @change="cfg.setOne(field.key, ($event.target as HTMLInputElement).checked)"
                  />
                  {{ Boolean(cfg.draft[field.key]) ? '开启' : '关闭' }}
                </label>

                <input
                  v-else
                  v-model="cfg.draft[field.key]"
                  :type="
                    field.kind === 'secret' ? 'password' : field.kind === 'int' ? 'number' : 'text'
                  "
                  :placeholder="
                    field.kind === 'secret'
                      ? field.has_value
                        ? `已保存 ${field.masked}（留空表示不改）`
                        : '未设置'
                      : '留空表示用环境变量 / 默认值'
                  "
                  class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-5 min-w-0 flex-1 border px-1.5 text-2xs outline-none"
                  @keyup.enter="cfg.save()"
                />

                <AppButton
                  v-if="field.fetch"
                  size="sm"
                  :disabled="!canFetch(field) || cfg.fetched.busy || cfg.busy"
                  :title="fetchTitle(field)"
                  @click="cfg.fetchOptions(field)"
                >
                  <Download :size="10" />
                  {{ cfg.fetched.busy && cfg.fetched.key === field.key ? '获取中…' : '自动获取' }}
                </AppButton>

                <AppBadge :tone="tone(field)">{{ SOURCE_LABEL[field.source] }}</AppBadge>
                <AppBadge v-if="cfg.isDirty(field.key)" tone="warn">未保存</AppBadge>
                <AppButton
                  size="sm"
                  variant="ghost"
                  :disabled="field.source !== 'file' || cfg.busy"
                  title="清除这项覆盖，回到环境变量或默认值"
                  @click="cfg.clear(field.key)"
                >
                  <RotateCcw :size="10" />
                </AppButton>
              </div>
              <p v-if="field.impact" class="text-fg-4 mt-0.5 pl-22 text-2xs">{{ field.impact }}</p>

              <!-- 自动获取的结果：挑一个只是填进输入框，手打照旧可用 -->
              <div v-if="cfg.fetched.key === field.key" class="mt-1 pl-22">
                <ErrorPanel :error="cfg.fetched.error" @dismiss="cfg.clearFetched()" />
                <div v-if="cfg.fetched.listing" class="border-line-1 bg-base-2 border">
                  <p class="text-fg-4 border-line-1 flex gap-2 border-b px-1.5 py-1 text-2xs">
                    <span class="text-fg-2">
                      {{ cfg.fetched.listing.label }} · {{ cfg.fetched.listing.count }} 个模型
                    </span>
                    <span class="min-w-0 flex-1 truncate font-mono">
                      {{ cfg.fetched.listing.target }}
                    </span>
                  </p>
                  <p
                    v-if="cfg.fetched.listing.current_present === false"
                    class="text-st-failed border-line-1 border-b px-1.5 py-1 text-2xs"
                  >
                    连得上，但这个端上没有
                    <span class="font-mono">{{ cfg.fetched.listing.current }}</span>
                    —— 现在这样调用时才会失败，从下面挑一个。
                  </p>
                  <ul class="max-h-40 overflow-auto">
                    <li v-for="m in cfg.fetched.listing.items" :key="m.id">
                      <button
                        class="hover:bg-base-3 flex w-full items-center gap-1.5 px-1.5 py-1 text-left text-2xs"
                        @click="cfg.pickOption(field.key, m.id)"
                      >
                        <Check
                          :size="10"
                          :class="
                            String(cfg.draft[field.key] ?? '') === m.id
                              ? 'text-accent'
                              : 'opacity-0'
                          "
                        />
                        <span class="text-fg-1">{{ m.label }}</span>
                        <span v-if="m.label !== m.id" class="text-fg-4 truncate font-mono">
                          {{ m.id }}
                        </span>
                      </button>
                    </li>
                    <li
                      v-if="!cfg.fetched.listing.items.length"
                      class="text-fg-4 px-1.5 py-1 text-2xs"
                    >
                      这个端一个模型都没列出来。自建端有时不提供列表——直接把模型名填进上面的输入框。
                    </li>
                  </ul>
                  <p class="text-fg-4 border-line-1 border-t px-1.5 py-1 text-2xs">
                    挑一个只是填进输入框，记得点下面的「保存」。列不出来的模型直接手打也一样能用。
                  </p>
                </div>
              </div>
            </template>
          </li>
        </ul>

        <div
          v-if="PROBE_OF[group.id] && cfg.probes[PROBE_OF[group.id]!].result"
          class="border-line-1 flex items-center gap-2 border-t px-3 py-1.5"
        >
          <StatusDot status="completed" />
          <span class="text-fg-2 text-2xs">
            {{ cfg.probes[PROBE_OF[group.id]!].result?.detail }}
          </span>
          <span class="text-fg-4 font-mono text-2xs">
            {{ cfg.probes[PROBE_OF[group.id]!].result?.target }}
          </span>
        </div>
        <div v-if="PROBE_OF[group.id] && cfg.probes[PROBE_OF[group.id]!].error" class="p-2">
          <ErrorPanel
            :error="cfg.probes[PROBE_OF[group.id]!].error"
            @dismiss="cfg.probes[PROBE_OF[group.id]!].error = null"
          />
        </div>
      </AppPanel>

      <div class="mb-2 flex items-center gap-2">
        <AppButton variant="primary" :disabled="!cfg.dirty || cfg.busy" @click="cfg.save()">
          <Save :size="11" />保存{{ cfg.dirty ? `（${cfg.dirtyKeys.length} 项）` : '' }}
        </AppButton>
        <AppButton variant="ghost" :disabled="!cfg.dirty" @click="cfg.resetDraft()">
          撤销未保存的改动
        </AppButton>
        <span class="text-fg-4 truncate font-mono text-2xs">{{ cfg.path }}</span>
      </div>
      <!-- ComfyUI 预设：模型端那份图的本地副本 -->
      <AppPanel :ref="(el) => setSection('presets', el)" :title="EXTRA_TITLE.presets" class="mb-2">
        <template #actions>
          <AppButton size="sm" variant="ghost" @click="cfg.loadPresets()">
            <RefreshCw :size="10" />刷新
          </AppButton>
          <AppButton size="sm" variant="primary" :disabled="cfg.busy" @click="fileInput?.click()">
            <Upload :size="10" />上传 API 格式 json
          </AppButton>
          <input ref="fileInput" type="file" accept=".json" class="hidden" @change="onPickPreset" />
        </template>

        <ul v-if="cfg.presets" class="text-fg-4 border-line-1 border-b px-3 py-1.5 text-2xs">
          <li v-for="line in cfg.presets.how_to" :key="line">· {{ line }}</li>
        </ul>

        <ul class="divide-line-1 divide-y">
          <li v-for="row in cfg.presets?.items ?? []" :key="row.name" class="px-3 py-1.5">
            <div class="flex flex-wrap items-center gap-2">
              <StatusDot :status="row.ready ? 'completed' : 'failed'" />
              <span class="text-fg-1 text-xs">{{ row.name }}</span>
              <!--
              「这一份现在是哪几种默认」：四种角色（R2V / 首尾帧 / 共用 / 出图）都画——这一页
              没有分栏，所以不传 `column`。徽标与按钮都读 `shared/lib/presets.ts` 那张表，
              与最外层的「预设 Workflow」页说的是同一句话。
            -->
              <PresetDefaultBadges :row="row" />
              <!-- 参考图槽位数：0 个也能生成，但角色表喂不进去，所以标出来而不是藏起来 -->
              <AppBadge v-if="row.ready" :tone="row.ref_slots ? 'neutral' : 'warn'">
                参考图 {{ row.ref_slots }} 槽
              </AppBadge>
              <!--
              参考视频 / 参考音频反过来：**0 是常态**，只在真标了槽位时画。
              给每份预设都挂一个「参考音频 0 槽」会把「参考图 0 槽」那个真问题埋掉。
            -->
              <AppBadge v-if="row.ready && row.ref_video_slots" tone="neutral">
                参考视频 {{ row.ref_video_slots }} 槽
              </AppBadge>
              <AppBadge v-if="row.ready && row.ref_audio_slots" tone="neutral">
                参考音频 {{ row.ref_audio_slots }} 槽
              </AppBadge>
              <!--
              **这份图是出图那一份**（图里标了 AIVS_IMAGE）。从入口标题分不出 T2I 与 R2V
              （两边都是 AIVS_PROMPT / AIVS_NEGATIVE / AIVS_SEED），所以只有这个声明说得清；
              标了它之后后端已经把它从 R2V / 首尾帧的候选里撤掉，这里只把结论标出来。
            -->
              <AppBadge v-if="row.t2i_ready" tone="ok">T2I 出图</AppBadge>
              <AppBadge v-if="row.declares_image && !row.prompt_ok" tone="fail">
                缺 AIVS_PROMPT
              </AppBadge>
              <span class="text-fg-4 min-w-0 flex-1 truncate text-2xs">
                {{ row.ready ? (row.found ?? []).join(' · ') : row.impact }}
              </span>
              <!--
              四颗「设为…默认 / 取消…默认」全部来自共用件，这一页一条判断都不写。以前这里是两颗
              内联按钮（「设为默认」＋一颗被 `wants_preset` 用 `v-if` 挡着的「设为出图默认」），
              于是：应用级默认从一格变成三格之后这里画不出按角色那两项；而默认协议是 `none`
              （`wants_preset=false`），出图那颗在全新装的机器上根本不出现——「不能设置默认出图
              预设」就是这么来的。共用件照旧显示它、把「暂时用不上」写进 tooltip（硬约束 4）。
            -->
              <PresetDefaultButtons :row="row" />
              <AppButton
                size="sm"
                variant="danger"
                :disabled="cfg.busy"
                @click="cfg.removePreset(row.name)"
              >
                <Trash2 :size="10" />
              </AppButton>
            </div>
            <!-- 「人物形象跑偏」的原因常常就在这一句里，文案由后端给 -->
            <p
              v-if="row.ready && row.ref_hint"
              class="mt-0.5 pl-4 text-2xs"
              :class="row.ref_slots ? 'text-fg-4' : 'text-st-failed'"
            >
              {{ row.ref_hint }}
            </p>
          </li>
          <li v-if="!(cfg.presets?.items ?? []).length" class="text-fg-4 px-3 py-2 text-2xs">
            还没有预设。从 ComfyUI 里用「Save (API
            Format)」导出一份，入口节点的标题按上面的约定改好再上传。
          </li>
        </ul>

        <div class="border-line-1 space-y-1 border-t p-2">
          <input
            v-model="presetName"
            placeholder="预设名，例如 wan-i2v-快速"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-5 w-full border px-1.5 text-2xs outline-none"
          />
          <textarea
            v-model="presetText"
            rows="3"
            placeholder="也可以直接粘贴 API 格式 json"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 w-full border px-1.5 py-1 font-mono text-2xs outline-none"
          ></textarea>
          <AppButton
            size="sm"
            :disabled="!presetName.trim() || !presetText.trim() || cfg.busy"
            :title="
              !presetName.trim()
                ? '先给这份图起个名字——之后在 provider 设置里按名字选它'
                : !presetText.trim()
                  ? '把 ComfyUI 导出的 API 格式 json 粘进下面那个框'
                  : '把这份图存进应用级预设目录'
            "
            @click="pastePreset()"
          >
            保存这份图
          </AppButton>
        </div>
      </AppPanel>

      <AppPanel :ref="(el) => setSection('deps', el)" :title="EXTRA_TITLE.deps">
        <template #actions>
          <AppButton size="sm" variant="ghost" @click="sys.refresh()">
            <RefreshCw :size="11" />重新探测
          </AppButton>
        </template>
        <ul class="divide-line-1 divide-y">
          <li v-for="dep in sys.deps" :key="dep.name" class="px-3 py-2">
            <div class="flex items-center gap-2">
              <StatusDot :status="dep.ok ? 'completed' : 'failed'" />
              <span class="text-fg-1 text-xs">{{ DEP_TITLE[dep.name] ?? dep.name }}</span>
            </div>
            <p class="text-fg-2 mt-1 pl-4 text-xs">{{ dep.detail }}</p>
            <p v-if="dep.hint" class="text-fg-4 mt-0.5 pl-4 text-xs">{{ dep.hint }}</p>
          </li>
          <li v-if="!sys.deps.length" class="text-fg-4 px-3 py-2 text-xs">尚未获取到依赖状态。</li>
        </ul>
      </AppPanel>

      <!-- 高级：ComfyUI 工作流绑定那条路 -->
      <div :ref="(el) => setSection('advanced', el)" class="border-line-1 bg-base-1 mt-2 border">
        <button
          class="text-fg-2 hover:text-fg-1 flex w-full items-center gap-1.5 px-3 py-1.5 text-xs"
          @click="advanced = !advanced"
        >
          <ChevronRight :size="11" :class="advanced ? 'rotate-90' : ''" />{{ EXTRA_TITLE.advanced }}
        </button>
        <div v-if="advanced" class="border-line-1 border-t px-3 py-2">
          <p class="text-fg-3 text-2xs">
            「Workflow 管理」把 prompt / 参考图 / 时长逐个绑到 ComfyUI 节点字段上。默认路径改成了按
            节点标题注参数——模型端的图由模型端维护，本工具不再跟着改。绑定那条路仍然是一条正经路：
            把这里的「调用方式」改成 <span class="font-mono">comfy_workflow</span> 就会走它，
            <strong class="text-fg-2">单个工程也能在概览页自己选</strong>（那一处优先于这里）。
          </p>
          <p class="text-fg-4 mt-1 text-2xs">
            绑定页在工程里：打开一个工程后按 Ctrl+K，搜「Workflow」就能进去。它不在左栏导航里——
            默认路径不需要它。
          </p>
        </div>
      </div>

      <AppPanel :ref="(el) => setSection('events', el)" :title="EXTRA_TITLE.events" class="mt-2">
        <ul class="divide-line-1 divide-y font-mono text-2xs">
          <li v-for="(ev, i) in [...sys.events].reverse()" :key="i" class="flex gap-2 px-3 py-1">
            <span class="text-fg-4 tnum shrink-0">{{ ev.ts.slice(11, 19) }}</span>
            <span class="text-accent shrink-0">{{ ev.channel }}</span>
            <span class="text-fg-2 shrink-0">{{ ev.event }}</span>
            <span class="text-fg-4 truncate">{{ JSON.stringify(ev.payload) }}</span>
          </li>
          <li v-if="!sys.events.length" class="text-fg-4 px-3 py-2 text-xs">
            暂无事件。生成任务开始后，进度与状态会实时出现在这里。
          </li>
        </ul>
      </AppPanel>
    </div>
  </div>
</template>
