<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { loadProductionMode } from '@/app/productionNavigation'
import StoryboardView from './StoryboardView.vue'

const route = useRoute()
const mode = computed(() => {
  const explicit = route.query.production
  return explicit === 'frames' || explicit === 'direct'
    ? explicit
    : loadProductionMode(String(route.params.pid || ''))
})
</script>

<template>
  <StoryboardView :mode="mode" />
</template>
