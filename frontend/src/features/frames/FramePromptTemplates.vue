<script setup lang="ts">
import { computed } from 'vue'
import AppButton from '@/shared/ui/AppButton.vue'
import {
  framePromptTemplates,
  frameStylePrompt,
  frameVideoPromptTemplates,
} from './frameStylePresets'
const props = withDefaults(defineProps<{ disabled?: boolean; kind?: 'image' | 'video' }>(), {
  kind: 'image',
})
const emit = defineEmits<{ apply: [prompt: string] }>()
const templates = computed(() =>
  props.kind === 'video'
    ? frameVideoPromptTemplates
    : framePromptTemplates.map((item) => ({
        ...item,
        prompt: 'prompt' in item ? item.prompt : frameStylePrompt(item.style),
      })),
)
</script>
<template>
  <div class="mt-1 space-y-1">
    <div
      role="group"
      :aria-label="kind === 'image' ? '共用图片提示词模板' : '共用分镜视频提示词模板'"
      class="flex flex-wrap gap-1"
    >
      <AppButton
        v-for="item in templates"
        :key="item.id"
        type="button"
        :disabled="disabled"
        :title="`填入${item.name}模板，替换当前草稿后可修改`"
        @click="emit('apply', item.prompt)"
        >{{ item.name }}</AppButton
      >
    </div>
    <p class="text-fg-4">点击按钮替换下方提示词草稿，可继续编辑；【】内请填写具体内容。</p>
    <p v-if="kind === 'video'" class="text-fg-4">
      复刻原片需实际提供视频参考，或填写原片的动作与运镜描述。当前首尾帧流程不会自动附带原视频或全部中间图片。
    </p>
  </div>
</template>
