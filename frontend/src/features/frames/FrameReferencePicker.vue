<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  framesApi,
  type FrameReferenceMaterial,
  type FrameReferenceMaterials,
} from '@/shared/api/frames'
import type { Asset } from '@/shared/api/assets'
import { fileUrl } from '@/shared/api/files'

const props = defineProps<{ pid: string; assets: Asset[]; disabled?: boolean }>()
const selected = defineModel<string[]>({ required: true })
const emit = defineEmits<{ change: [] }>()
const kinds = { character: '角色', location: '场景', prop: '道具' } as const
const kind = ref<FrameReferenceMaterial['kind']>('character')
const entityId = ref('')
const materials = ref<FrameReferenceMaterials | null>(null)
const error = ref('')
const loading = ref(false)
let request = 0
watch(
  () => props.pid,
  async (pid) => {
    const ticket = ++request
    loading.value = true
    error.value = ''
    try {
      const data = await framesApi.referenceMaterials(pid)
      if (ticket === request) materials.value = data
    } catch (e) {
      if (ticket === request) error.value = e instanceof Error ? e.message : String(e)
    } finally {
      if (ticket === request) loading.value = false
    }
  },
  { immediate: true },
)
const options = computed(() => materials.value?.groups[kind.value] || [])
const current = computed(() => options.value.find((row) => row.id === entityId.value))
function changeKind(value: FrameReferenceMaterial['kind']) {
  kind.value = value
  entityId.value = ''
}
const limit = computed(() => materials.value?.provider.max_refs ?? null)
const included = (index: number) => limit.value === null || index < limit.value
function update(values: string[]) {
  selected.value = values
  emit('change')
}
function add() {
  const row = current.value
  if (row?.asset_id && row.available && !selected.value.includes(row.asset_id))
    update([...selected.value, row.asset_id])
  entityId.value = ''
}
function move(index: number, delta: number) {
  const values = [...selected.value],
    target = index + delta
  if (target < 0 || target >= values.length) return
  ;[values[index], values[target]] = [values[target]!, values[index]!]
  update(values)
}
function label(id: string) {
  const row =
    materials.value &&
    Object.values(materials.value.groups)
      .flat()
      .find((row) => row.asset_id === id)
  return row
    ? `${kinds[row.kind]} · ${row.label}`
    : props.assets.find((a) => a.id === id)?.description || '画面候选 / 已选图片'
}
function url(id: string) {
  const asset = props.assets.find((a) => a.id === id)
  return asset ? fileUrl(props.pid, asset.path) : ''
}
</script>

<template>
  <section class="reference-picker" aria-label="制帧设定素材">
    <strong>引用设定素材</strong>
    <div class="reference-kinds" role="group" aria-label="设定图类别">
      <button
        v-for="(name, id) in kinds"
        :key="id"
        type="button"
        :aria-pressed="kind === id"
        :disabled="disabled"
        @click="changeKind(id)"
      >
        {{ name }}
      </button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <label
      >添加{{ kinds[kind] }}素材<select v-model="entityId" :disabled="disabled || loading">
        <option value="">选择{{ kinds[kind] }}设定</option>
        <option
          v-for="row in options"
          :key="row.id"
          :value="row.id"
          :disabled="!row.available || !row.asset_id || selected.includes(row.asset_id)"
        >
          {{ row.label }}{{ row.available ? '' : '（暂无可用设定图）' }}
        </option>
      </select></label
    >
    <button
      type="button"
      :disabled="disabled || !current?.available || !current.asset_id"
      @click="add"
    >
      添加参考图
    </button>
    <p v-if="materials" class="reference-capacity">
      {{ materials.provider.label }} ·
      {{
        !materials.provider.configured
          ? '请先配置图片生成服务'
          : limit === null
            ? '接口未声明数量上限'
            : `最多采用 ${limit} 张`
      }}
    </p>
    <p>按下方顺序采用参考图；超出容量时舍弃后面的图片。</p>
    <ol>
      <li v-for="(id, index) in selected" :key="id" :class="{ dropped: !included(index) }">
        <img v-if="url(id)" :src="url(id)" :alt="label(id)" />
        <div>
          <span>{{ index + 1 }}. {{ label(id) }}</span
          ><small>{{ included(index) ? '本次采用' : '超出容量，本次舍弃' }}</small>
        </div>
        <div class="reference-order">
          <button
            type="button"
            :disabled="disabled || index === 0"
            aria-label="参考图前移"
            @click="move(index, -1)"
          >
            ↑
          </button>
          <button
            type="button"
            :disabled="disabled || index === selected.length - 1"
            aria-label="参考图后移"
            @click="move(index, 1)"
          >
            ↓
          </button>
          <button
            type="button"
            :disabled="disabled"
            aria-label="移除参考图"
            @click="update(selected.filter((value) => value !== id))"
          >
            移除
          </button>
        </div>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.reference-picker {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.reference-kinds,
.reference-order {
  display: flex;
  gap: 5px;
}
button,
select {
  border: 1px solid var(--color-line-1);
  background: var(--color-base-2);
  color: inherit;
  padding: 5px;
  border-radius: 4px;
}
button[aria-pressed='true'] {
  border-color: var(--color-accent);
  color: var(--color-accent);
}
button:disabled {
  opacity: 0.5;
}
label {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
ol {
  list-style: none;
  padding: 0;
  margin: 0;
}
li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 6px 0;
  border-bottom: 1px solid var(--color-line-1);
}
li > div {
  flex: 1;
  min-width: 0;
}
li .reference-order {
  flex: none;
}
img {
  width: 42px;
  height: 42px;
  object-fit: contain;
}
small {
  display: block;
  opacity: 0.7;
}
.dropped {
  opacity: 0.5;
}
.reference-capacity {
  color: var(--color-accent);
}
</style>
