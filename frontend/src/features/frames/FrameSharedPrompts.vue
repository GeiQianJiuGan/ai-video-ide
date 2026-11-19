<script setup lang="ts">
import { ref, watch } from 'vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import { framesApi, type SharedFramePrompts } from '@/shared/api/frames'
import FramePromptTemplates from './FramePromptTemplates.vue'

const props = defineProps<{ open: boolean; pid: string; sceneId: string }>()
const emit = defineEmits<{ 'update:open': [boolean]; done: [data: SharedFramePrompts] }>()
const imagePrompt = ref('')
const videoPrompt = ref('')
const clear = ref(false)
const busy = ref(false)
const error = ref('')
const loaded = ref(false)
watch(
  () => [props.open, props.pid, props.sceneId] as const,
  async () => {
    if (!props.open || !props.sceneId) return
    busy.value = true
    error.value = ''
    loaded.value = false
    clear.value = false
    try {
      const data = await framesApi.sharedPrompts(props.pid, props.sceneId)
      imagePrompt.value = data.image_prompt
      videoPrompt.value = data.video_prompt
      loaded.value = true
    } catch (e) {
      error.value = String(e)
    } finally {
      busy.value = false
    }
  },
  { immediate: true },
)
async function save() {
  if (!loaded.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const data = {
      image_prompt: imagePrompt.value,
      video_prompt: videoPrompt.value,
      clear_overrides: clear.value,
    }
    await framesApi.saveSharedPrompts(props.pid, props.sceneId, data)
    emit('done', data)
    emit('update:open', false)
  } catch (e) {
    error.value = String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AppDialog
    :open="open"
    title="本幕共用制帧提示词"
    size="md"
    @update:open="emit('update:open', $event)"
  >
    <form class="p-4 space-y-3 text-xs" @submit.prevent="save">
      <p class="text-fg-4">
        本幕内留空的图片、分镜提示词自动继承此处；填写独立提示词即可单独覆盖。修改仅影响之后提交的任务。
      </p>
      <p v-if="error" role="alert" class="text-st-failed">{{ error }}</p>
      <div>
        <label for="shared-image-prompt">共用图片提示词</label>
        <FramePromptTemplates :disabled="busy || !loaded" @apply="imagePrompt = $event" />
        <textarea
          id="shared-image-prompt"
          v-model="imagePrompt"
          :disabled="busy || !loaded"
          rows="4"
          class="border-line-1 bg-base-2 mt-1 w-full border p-2"
        />
      </div>
      <div>
        <label for="shared-video-prompt">共用分镜视频提示词</label>
        <FramePromptTemplates
          kind="video"
          :disabled="busy || !loaded"
          @apply="videoPrompt = $event"
        />
        <textarea
          id="shared-video-prompt"
          v-model="videoPrompt"
          :disabled="busy || !loaded"
          rows="4"
          class="border-line-1 bg-base-2 mt-1 w-full border p-2"
        />
      </div>
      <label class="flex gap-2"
        ><input
          v-model="clear"
          :disabled="busy"
          type="checkbox"
        />清空本幕所有图片和分镜的独立提示词，全部改用共用提示词</label
      >
      <AppButton type="submit" :disabled="busy || !loaded">{{
        busy ? '处理中…' : '保存共用提示词'
      }}</AppButton>
    </form>
  </AppDialog>
</template>
