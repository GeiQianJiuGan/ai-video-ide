<script setup lang="ts">
import AppDialog from './AppDialog.vue'
import AppButton from './AppButton.vue'
defineProps<{ open: boolean; title: string }>()
defineEmits<{ 'update:open': [boolean]; choose: [scope: 'all' | 'unfinished'] }>()
</script>
<template>
  <AppDialog :open="open" :title="title" size="sm" @update:open="$emit('update:open', $event)">
    <div class="space-y-3 p-4 text-xs">
      <p>选择本次生成范围。两种方式都会跳过正在排队或运行的任务。</p>
      <div class="flex flex-wrap gap-2">
        <AppButton variant="primary" @click="$emit('choose', 'unfinished')">仅生成未完成</AppButton>
        <AppButton @click="$emit('choose', 'all')">全部生成</AppButton>
      </div>
      <p class="text-fg-4">
        未完成包含尚无生成结果、失败和已取消的项目。全部生成会为已有结果再生成新版本，历史结果保留。
      </p>
    </div>
  </AppDialog>
</template>
