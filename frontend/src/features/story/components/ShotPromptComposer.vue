<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { fileUrl } from '@/shared/api/files'
import type { GenerationVersion } from '@/shared/api/generation'
import {
  lookupReference,
  mentionPattern,
  mentionText,
  promptStages,
  unresolvedMentions,
  type PromptReference,
} from '../shotPrompt'

const props = defineProps<{
  modelValue: string
  inherited: string
  references: PromptReference[]
  pid: string
  duration: number
  mode: string
  saved: boolean
  saving: boolean
  version: GenerationVersion | null
}>()
const emit = defineEmits<{
  'update:modelValue': [value: string]
  save: []
  locate: [key: string]
  hover: [key: string]
}>()
const editor = ref<HTMLDivElement | null>(null)
const stage = ref(0)
const view = ref<'write' | 'submitted'>('write')
const expanded = ref(true)
const menuOpen = ref(false)
const query = ref('')
const menuIndex = ref(0)
const hovered = ref<PromptReference | null>(null)
let caret: Range | null = null
let mentionRange: Range | null = null
const effective = computed(() => props.modelValue || props.inherited)
const invalid = computed(() => unresolvedMentions(effective.value, props.references))
const candidates = computed(() =>
  props.references.filter((r) =>
    `${r.alias} ${r.label}`.toLowerCase().includes(query.value.toLowerCase()),
  ),
)
const checks = computed(() => [
  {
    label: '引导占位内容',
    detail: /……|「引用素材」/.test(effective.value)
      ? '还有占位内容需要替换为具体描述'
      : '未发现引导占位内容',
    ok: !/……|「引用素材」/.test(effective.value),
  },
  {
    label: '创作内容',
    detail: effective.value.trim() ? '已填写' : '请先描述这个镜头',
    ok: Boolean(effective.value.trim()),
  },
  {
    label: '素材引用',
    detail: invalid.value.length ? `失效：${invalid.value.join('、')}` : '引用有效',
    ok: !invalid.value.length,
  },
  {
    label: '镜头时长',
    detail: `${props.duration}s${props.duration < 4 || props.duration > 15 ? '，超出 H3 指南建议的 4–15 秒，请确认工作流能力' : ''}`,
    ok: props.duration >= 4 && props.duration <= 15,
  },
  {
    label: '首尾变化',
    detail: props.mode.startsWith('FL2VA')
      ? '请确认动作连续到达尾帧，避免只描述两张静态画面'
      : '根据当前输入形态描写画面发展',
    ok: true,
  },
])

function serialize(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? ''
  if (node instanceof HTMLElement && node.dataset.target)
    return `@[${node.dataset.alias}](${node.dataset.target})`
  if (node.nodeName === 'BR') return '\n'
  return Array.from(node.childNodes)
    .map((child, index) => {
      const separator =
        index > 0 && (child.nodeName === 'DIV' || child.nodeName === 'P') ? '\n' : ''
      return separator + serialize(child)
    })
    .join('')
}
function chip(target: string, alias: string) {
  const r = lookupReference(props.references, target)
  const element = document.createElement('span')
  element.contentEditable = 'false'
  element.dataset.target = target
  element.dataset.alias = alias
  element.className = r?.valid ? 'prompt-chip' : 'prompt-chip broken'
  element.textContent = `@${alias}`
  element.title = r
    ? `${r.label}${r.valid ? ' · 点击定位素材' : ` · ${r.reason}`}`
    : '引用已失效，请删除或重新连接素材'
  element.setAttribute('role', 'button')
  element.setAttribute('tabindex', '0')
  return element
}
function fragment(text: string) {
  const out = document.createDocumentFragment()
  let from = 0
  for (const match of text.matchAll(mentionPattern())) {
    out.append(document.createTextNode(text.slice(from, match.index)))
    out.append(chip(`${match[2]}:${match[3]}`, match[1]!))
    from = match.index! + match[0].length
  }
  out.append(document.createTextNode(text.slice(from)))
  return out
}
function render() {
  if (!editor.value) return
  if (serialize(editor.value) === props.modelValue) {
    // 素材被移除或容量变化时只刷新标签状态，不移动光标。
    editor.value.querySelectorAll<HTMLElement>('[data-target]').forEach((el) => {
      const r = lookupReference(props.references, el.dataset.target ?? '')
      el.className = r?.valid ? 'prompt-chip' : 'prompt-chip broken'
      el.title = r ? `${r.label} · ${r.reason}` : '引用已失效'
    })
    return
  }
  editor.value.replaceChildren(fragment(props.modelValue))
  // DOM 替换会把旧 Range 自动挪到开头，不能让下次快捷引用误用这个位置。
  caret = null
  mentionRange = null
}
function focusEnd() {
  if (!editor.value) return
  editor.value.focus()
  const range = document.createRange()
  range.selectNodeContents(editor.value)
  range.collapse(false)
  const selection = window.getSelection()
  selection?.removeAllRanges()
  selection?.addRange(range)
  caret = range.cloneRange()
}
function rememberCaret() {
  const selection = window.getSelection()
  if (selection?.rangeCount && editor.value?.contains(selection.anchorNode))
    caret = selection.getRangeAt(0).cloneRange()
}
function input() {
  if (!editor.value) return
  emit('update:modelValue', serialize(editor.value))
  rememberCaret()
  const selection = window.getSelection()
  const node = selection?.anchorNode
  if (node?.nodeType === Node.TEXT_NODE && selection?.isCollapsed) {
    const before = (node.textContent ?? '').slice(0, selection.anchorOffset)
    const match = /@([^@\s\n]{0,32})$/.exec(before)
    if (match) {
      query.value = match[1] ?? ''
      menuIndex.value = 0
      menuOpen.value = true
      mentionRange = document.createRange()
      mentionRange.setStart(node, before.length - match[0].length)
      mentionRange.setEnd(node, selection.anchorOffset)
      return
    }
  }
  menuOpen.value = false
  mentionRange = null
}
async function insertReference(r: PromptReference) {
  if (!r.valid) return
  view.value = 'write'
  expanded.value = true
  await nextTick()
  if (!editor.value) return
  editor.value.focus()
  const selection = window.getSelection()
  let range = mentionRange ?? caret
  if (!range || !editor.value.contains(range.startContainer)) {
    range = document.createRange()
    range.selectNodeContents(editor.value)
    range.collapse(false)
  }
  range.deleteContents()
  const element = chip(r.target, r.alias)
  range.insertNode(element)
  const space = document.createTextNode(' ')
  element.after(space)
  range.setStartAfter(space)
  range.collapse(true)
  selection?.removeAllRanges()
  selection?.addRange(range)
  caret = range.cloneRange()
  mentionRange = null
  menuOpen.value = false
  emit('update:modelValue', serialize(editor.value))
}
function insertText(text: string) {
  if (!editor.value) return
  const before = props.modelValue || props.inherited
  emit('update:modelValue', `${before}${before ? '\n' : ''}${text}`)
  void nextTick(() => {
    render()
    focusEnd()
  })
}
function template() {
  let text: string = promptStages[stage.value]!.template
  const first = props.references.find((r) => r.target === 'frame:first_frame' && r.valid)
  const last = props.references.find((r) => r.target === 'frame:last_frame' && r.valid)
  if (stage.value === 0) {
    const r = props.references.find((r) => r.valid && r.target.startsWith('asset:'))
    if (r) text = `保留 ${mentionText(r)} 中的……，用于本镜头的……。`
  }
  if (stage.value === 1 && first) text = `从 ${mentionText(first)} 的构图和主体状态开始，随后……。`
  if (stage.value === 3 && last)
    text = `主体通过……连续变化，镜头……，在 ${props.duration.toFixed(2)} 秒到达 ${mentionText(last)} 的姿态与构图。`
  insertText(text)
}
function skeleton() {
  // 仅插入结构骨架；中文内容的翻译和创作判断需要用户或 AI 完成。
  const referenced = props.mode.startsWith('Ref2VA')
  const fields = referenced
    ? [
        'subject_definitions',
        'summary',
        'retention_analysis',
        'detailed_description',
        'overall_soundscape',
        'non_diegetic_music',
      ]
    : ['integrated_multimodal_description', 'overall_soundscape', 'non_diegetic_music']
  const body = effective.value
  const main = referenced ? 'detailed_description' : 'integrated_multimodal_description'
  if (fields.some((key) => body.includes(`${key}:`))) return
  const first = props.references.find((r) => r.target === 'frame:first_frame' && r.valid)
  const last = props.references.find((r) => r.target === 'frame:last_frame' && r.valid)
  let alignment = ''
  if (!referenced) {
    if (first && last)
      alignment = `How the reference pictures align with the target video — ${mentionText(first)} (from Shot 1) aligns with the 0.00-second mark of the target video; ${mentionText(last)} (from Shot 1) aligns with the ${props.duration.toFixed(2)}-second mark of the target video.`
    else if (first)
      alignment = `For the target video, at 0.00 seconds into the target video, ${mentionText(first)} (from [Shot 1]) is fully referenced.`
    else if (last)
      alignment = `How the reference pictures align with the target video — ${mentionText(last)} (from [Shot 1]) aligns with the ${props.duration.toFixed(2)}-second mark of the target video.`
  }
  emit(
    'update:modelValue',
    (alignment ? alignment + '\n\n' : '') +
      fields
        .map(
          (key) => `${key}:\n${key === main ? body : key === 'non_diegetic_music' ? 'none' : '……'}`,
        )
        .join('\n\n'),
  )
  void nextTick(() => {
    render()
    focusEnd()
  })
}
function previewReference(r: PromptReference | null) {
  hovered.value = r
  emit('hover', r?.key ?? '')
}
function hoverChip(event: MouseEvent) {
  const el = (event.target as HTMLElement).closest<HTMLElement>('[data-target]')
  previewReference(el ? (lookupReference(props.references, el.dataset.target ?? '') ?? null) : null)
}
function keydown(event: KeyboardEvent) {
  if (event.isComposing) return
  if (event.key === 'Enter' && (event.target as HTMLElement).dataset.target) {
    event.preventDefault()
    clickChip(event)
    return
  }
  if (menuOpen.value) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      menuIndex.value =
        (menuIndex.value +
          (event.key === 'ArrowDown' ? 1 : -1) +
          Math.max(1, candidates.value.length)) %
        Math.max(1, candidates.value.length)
    } else if (event.key === 'Enter' && candidates.value[menuIndex.value]) {
      event.preventDefault()
      void insertReference(candidates.value[menuIndex.value]!)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      menuOpen.value = false
      mentionRange = null
    }
  }
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault()
    emit('save')
  }
}
function paste(event: ClipboardEvent) {
  event.preventDefault()
  // 只接受纯文本，避免外部 HTML 带入不可见节点或脚本。
  const text = event.clipboardData?.getData('text/plain') ?? ''
  const selection = window.getSelection()
  if (!selection?.rangeCount) return
  const range = selection.getRangeAt(0)
  if (!editor.value?.contains(range.startContainer)) return
  range.deleteContents()
  const nodes = fragment(text)
  const anchor = document.createTextNode('')
  nodes.append(anchor)
  range.insertNode(nodes)
  range.setStartAfter(anchor)
  range.collapse(true)
  selection.removeAllRanges()
  selection.addRange(range)
  input()
}
function copy(event: ClipboardEvent, cut = false) {
  const selection = window.getSelection()
  if (
    !selection?.rangeCount ||
    !editor.value?.contains(selection.anchorNode) ||
    !editor.value.contains(selection.focusNode)
  )
    return
  event.preventDefault()
  event.clipboardData?.setData('text/plain', serialize(selection.getRangeAt(0).cloneContents()))
  if (cut) {
    selection.getRangeAt(0).deleteContents()
    input()
  }
}
function clickChip(event: Event) {
  const el = (event.target as HTMLElement).closest<HTMLElement>('[data-target]')
  if (!el) return
  const r = lookupReference(props.references, el.dataset.target ?? '')
  if (r) emit('locate', r.key)
}
function blur() {
  rememberCaret()
  emit('save')
}
function switchView(value: 'write' | 'submitted') {
  view.value = value
  expanded.value = true
}
defineExpose({
  insertReference,
  getValue: () => (editor.value ? serialize(editor.value) : props.modelValue),
})
watch(
  () => props.modelValue,
  () => void nextTick(render),
)
watch(
  () => props.references,
  () => void nextTick(render),
  { deep: true },
)
watch(view, () => void nextTick(render))
watch(expanded, () => void nextTick(render))
onMounted(render)
</script>

<template>
  <section class="prompt-builder">
    <header class="builder-header">
      <button class="builder-title" @click="expanded = !expanded">
        {{ expanded ? '⌄' : '⌃' }} 提示词构筑
      </button>
      <span class="source">{{
        modelValue ? 'Shot 专属' : inherited ? '继承本幕提示词' : '等待创作'
      }}</span>
      <span class="mode">{{ mode }}</span
      ><span class="spacer" />
      <button :class="{ active: view === 'write' }" @click="switchView('write')">创作内容</button>
      <button :class="{ active: view === 'submitted' }" @click="switchView('submitted')">
        版本提交记录
      </button>
      <button :disabled="saved || saving" @click="emit('save')">
        {{ saving ? '保存中…' : saved ? '已保存' : '保存 · Ctrl↵' }}
      </button>
    </header>
    <template v-if="expanded">
      <template v-if="view === 'write'">
        <nav class="stages" aria-label="提示词构筑阶段">
          <button
            v-for="(item, index) in promptStages"
            :key="item.title"
            :class="{ active: stage === index }"
            :aria-pressed="stage === index"
            @click="stage = index"
          >
            {{ index + 1 }} {{ item.title }}
          </button>
        </nav>
        <div class="writing-area">
          <div class="editor-column">
            <div class="guidance">
              <span>{{ promptStages[stage]!.hint }}</span
              ><button v-if="promptStages[stage]!.template" @click="template">插入引导</button
              ><button @click="skeleton">插入 H3 结构</button>
            </div>
            <div v-if="!modelValue && inherited" class="inherited">
              <p>当前继承：{{ inherited }}</p>
              <button @click="emit('update:modelValue', inherited)">复制到本镜头编辑</button>
            </div>
            <div class="editor-wrap">
              <div
                ref="editor"
                class="prompt-editor"
                contenteditable="true"
                role="textbox"
                aria-label="镜头提示词，输入 @ 引用素材"
                aria-multiline="true"
                :data-placeholder="
                  inherited
                    ? '留空继承本幕；也可以为这个镜头单独编写'
                    : '描述画面、动作与声音；输入 @ 选择素材，点击标签可定位画布。'
                "
                @input="input"
                @keyup="rememberCaret"
                @mouseup="rememberCaret"
                @keydown="keydown"
                @paste="paste"
                @copy="copy"
                @cut="copy($event, true)"
                @click="clickChip"
                @mouseover="hoverChip"
                @mouseleave="previewReference(null)"
                @blur="blur"
              />
              <div v-if="menuOpen" class="mention-menu" role="listbox" aria-label="可引用素材">
                <button
                  v-for="(r, index) in candidates"
                  :key="r.target"
                  role="option"
                  :aria-selected="index === menuIndex"
                  :class="{ highlighted: index === menuIndex }"
                  :disabled="!r.valid"
                  @mousedown.prevent
                  @click="insertReference(r)"
                >
                  <img
                    v-if="r.media === 'image' && r.path"
                    :src="fileUrl(pid, r.path)"
                    alt=""
                  /><span
                    ><strong>@{{ r.alias }}</strong> {{ r.label
                    }}<small v-if="!r.valid">{{ r.reason }}</small></span
                  >
                </button>
                <p v-if="!candidates.length">没有匹配的已连接素材，请先从画布添加。</p>
              </div>
            </div>
            <div class="reference-strip">
              <span>快捷引用</span
              ><button
                v-for="r in references"
                :key="r.target"
                :disabled="!r.valid"
                :class="{ broken: !r.valid }"
                :title="`${r.label} · ${r.reason}`"
                @mouseenter="previewReference(r)"
                @mouseleave="previewReference(null)"
                @mousedown.prevent
                @click="insertReference(r)"
              >
                @{{ r.alias }}</button
              ><span v-if="!references.length">连接素材后在这里引用</span>
            </div>
            <div v-if="hovered" class="reference-preview">
              <img
                v-if="hovered.media === 'image' && hovered.path"
                :src="fileUrl(pid, hovered.path)"
                alt="素材缩略图"
              /><span>{{ hovered.label }} · {{ hovered.reason }}</span>
            </div>
            <p v-if="invalid.length" class="invalid-warning">
              引用失效：{{ invalid.join('、') }}。请重新连接或替换标签后生成。
            </p>
          </div>
          <aside v-if="stage === 5" class="checks">
            <div v-for="check in checks" :key="check.label">
              <strong :class="check.ok ? 'ok' : 'warn'"
                >{{ check.ok ? '✓' : '!' }} {{ check.label }}</strong
              >
              <p>{{ check.detail }}</p>
            </div>
          </aside>
          <aside v-else class="builder-note">
            <strong>构筑中的提示词</strong>
            <p>阶段可自由切换，按需要插入引导。标签始终链接当前镜头素材。</p>
            <p>H3 结构按钮插入字段骨架；英文改写、占位内容和创作细节需继续完善。</p>
            <p>清空本镜头内容可恢复继承。</p>
          </aside>
        </div>
      </template>
      <div v-else class="submitted">
        <p v-if="version">
          v{{ version.version_no }} · {{ version.is_current ? '当前采用' : '历史预览' }} ·
          以下为此版冻结记录；新草稿的编号在提交时按实际输入解析。
        </p>
        <p v-else>还没有可查看的生成版本。生成后这里显示实际提交的提示词。</p>
        <pre v-if="version">{{
          version.params.prompt_sent ?? version.params.prompt ?? '手动导入版本没有生成提示词。'
        }}</pre>
        <pre v-if="version?.params.pictures">{{
          JSON.stringify(version.params.pictures, null, 2)
        }}</pre>
      </div>
    </template>
  </section>
</template>

<style scoped>
.prompt-builder {
  flex-shrink: 0;
  background: var(--color-base-1);
  border-top: 1px solid var(--color-line-2);
  font-size: 12px;
  color: var(--color-fg-2);
  max-height: 48vh;
  overflow: auto;
}
.builder-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  flex-wrap: wrap;
}
.builder-title {
  font-weight: 600;
  color: var(--color-fg-1);
}
.source {
  font-size: 10px;
  color: var(--color-fg-3);
}
.mode {
  font-size: 10px;
  color: var(--color-accent);
}
.spacer {
  flex: 1;
}
button {
  font-size: 11px;
  padding: 4px 8px;
  background: var(--color-base-2);
  border: 1px solid var(--color-line-1);
  border-radius: 4px;
  cursor: pointer;
}
button:hover,
button.active {
  color: var(--color-accent);
  border-color: var(--color-accent);
}
button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.stages {
  display: flex;
  gap: 5px;
  padding: 0 12px 8px;
  flex-wrap: wrap;
}
.stages button {
  border-radius: 20px;
  padding: 4px 12px;
}
.writing-area {
  display: flex;
  gap: 16px;
  padding: 0 12px 10px;
}
.editor-column {
  position: relative;
  flex: 1;
  min-width: 0;
}
.guidance {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 7px;
  font-size: 11px;
  color: var(--color-fg-3);
}
.guidance > span {
  flex: 1;
}
.guidance button {
  white-space: nowrap;
}
.editor-wrap {
  position: relative;
}
.prompt-editor {
  min-height: 85px;
  max-height: 190px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  padding: 10px 12px;
  border: 1px solid var(--color-line-2);
  background: var(--color-base-0);
  border-radius: 6px;
  color: var(--color-fg-1);
  font-size: 12px;
  line-height: 1.9;
  outline: none;
}
.prompt-editor:focus {
  border-color: var(--color-accent);
}
.prompt-editor:empty:before {
  content: attr(data-placeholder);
  color: var(--color-fg-3);
  pointer-events: none;
}
.prompt-editor :deep(.prompt-chip) {
  display: inline-block;
  color: var(--color-accent-hi);
  background: var(--color-accent-dim);
  border: 1px solid #2dd4bf45;
  border-radius: 4px;
  padding: 0 5px;
  margin: 0 2px;
  font-size: 11px;
  cursor: pointer;
  user-select: all;
}
.prompt-editor :deep(.prompt-chip.broken) {
  background: #613a27;
  color: var(--color-st-review);
  border-color: var(--color-st-review);
}
.mention-menu {
  position: absolute;
  bottom: 100%;
  left: 0;
  width: min(440px, 100%);
  max-height: 220px;
  overflow: auto;
  border: 1px solid var(--color-line-2);
  border-radius: 6px;
  background: var(--color-base-2);
  box-shadow: 0 6px 30px #0008;
  z-index: 50;
  padding: 5px;
}
.mention-menu button {
  display: flex;
  text-align: left;
  gap: 8px;
  width: 100%;
  align-items: center;
  border: 0;
  padding: 7px;
}
.mention-menu button.highlighted {
  background: var(--color-base-3);
}
.mention-menu img {
  width: 42px;
  height: 32px;
  object-fit: cover;
  border-radius: 3px;
}
.mention-menu small {
  display: block;
  color: var(--color-st-review);
}
.mention-menu p {
  padding: 8px;
  color: var(--color-fg-3);
}
.reference-strip {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
  margin-top: 7px;
  font-size: 10px;
  color: var(--color-fg-3);
}
.reference-strip button {
  color: var(--color-accent);
  padding: 2px 7px;
}
.reference-strip button.broken {
  color: var(--color-st-review);
}
.reference-preview {
  position: absolute;
  bottom: 32px;
  left: 8px;
  right: 8px;
  z-index: 25;
  pointer-events: none;
  padding: 8px;
  border: 1px solid var(--color-line-2);
  border-radius: 5px;
  background: var(--color-base-2);
  box-shadow: 0 6px 20px #0008;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  margin-top: 5px;
}
.reference-preview img {
  width: 48px;
  height: 32px;
  object-fit: cover;
}
.builder-note,
.checks {
  width: 210px;
  flex-shrink: 0;
  border-left: 1px solid var(--color-line-1);
  padding-left: 12px;
  font-size: 11px;
  line-height: 1.7;
  color: var(--color-fg-3);
}
.builder-note strong,
.checks strong {
  color: var(--color-fg-2);
}
.builder-note p {
  margin-top: 5px;
}
.checks > div {
  margin-bottom: 5px;
}
.checks .ok {
  color: var(--color-st-done);
}
.checks .warn,
.invalid-warning {
  color: var(--color-st-review);
}
.invalid-warning {
  font-size: 11px;
  margin-top: 6px;
}
.inherited {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  margin: 4px 0 8px;
  color: var(--color-fg-3);
}
.inherited p {
  flex: 1;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.inherited button {
  white-space: nowrap;
}
.submitted {
  padding: 0 12px 12px;
  font-size: 11px;
  color: var(--color-fg-3);
}
.submitted pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 200px;
  overflow: auto;
  padding: 10px;
  margin-top: 8px;
  background: var(--color-base-0);
  color: var(--color-fg-2);
  border: 1px solid var(--color-line-1);
  border-radius: 5px;
}
@media (max-width: 1000px) {
  .builder-note {
    display: none;
  }
  .checks {
    width: 180px;
  }
}
@media (max-height: 750px) {
  .prompt-editor {
    min-height: 65px;
    max-height: 110px;
  }
  .builder-note {
    font-size: 10px;
  }
}
</style>
