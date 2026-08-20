<script setup lang="ts">
/**
 * 项目概览页。
 *
 * 真正的进度总览要等接后端（注册表里 dashboard 仍是 ready: false，
 * 骨架与那把能力锁照旧由 FeatureView 按注册表画——不伪造数据）。
 * 这里额外做一件现在就成立的事：把核心链路画在底部。
 * 它从原来的工作台首页搬到这里——链路上每个节点都是本工程里的数据，
 * 只有打开了工程才点得进去。
 */
import ChainStrip from '@/shared/ui/ChainStrip.vue'
import FeatureView from '@/shared/ui/FeatureView.vue'
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <div class="flex min-h-0 flex-1 flex-col overflow-hidden">
      <FeatureView />
    </div>
    <div class="border-line-1 shrink-0 border-t p-2">
      <ChainStrip />
    </div>
  </div>
</template>
