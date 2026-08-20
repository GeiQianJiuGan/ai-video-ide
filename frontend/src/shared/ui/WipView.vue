<script setup lang="ts">
/** 未实现页面的统一占位：说明该页归属哪个里程碑、将包含哪些内容。 */
import { useRoute } from 'vue-router'
import { computed } from 'vue'

const route = useRoute()
const title = computed(() => (route.meta.title as string | undefined) ?? '未命名')
const milestone = computed(() => (route.meta.milestone as string | undefined) ?? '')
const items = computed(() => (route.meta.items as string[] | undefined) ?? [])
</script>

<template>
  <div class="flex min-h-0 flex-1 items-center justify-center p-8">
    <div class="border-line-1 bg-base-1 w-full max-w-xl border p-5">
      <div class="flex items-baseline gap-2">
        <h1 class="text-fg-1 text-base font-medium">{{ title }}</h1>
        <span
          v-if="milestone"
          class="border-accent/40 text-accent bg-accent-dim rounded-xs border px-1.5 py-0.5 text-2xs"
        >
          {{ milestone }}
        </span>
      </div>
      <p class="text-fg-3 mt-1 text-xs">此页面尚未实现。计划包含：</p>
      <ul class="text-fg-2 mt-3 space-y-1 text-xs">
        <li v-for="item in items" :key="item" class="flex gap-2">
          <span class="text-fg-4">·</span><span>{{ item }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>
