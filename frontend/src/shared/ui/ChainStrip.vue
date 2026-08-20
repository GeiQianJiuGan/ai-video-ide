<script setup lang="ts">
/**
 * 核心链路条：把「这个系统怎么工作」画成一条可点击的流水线。
 *
 * 它只出现在项目概览页——链路上的每一节点（Character / Scene / Shot / …）
 * 都是某个工程里的数据，没打开工程时画它没有意义，点了也无处可去。
 */
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { ChevronRight } from '@lucide/vue'
import AppPanel from './AppPanel.vue'
import { CHAIN } from '@/app/features'

const route = useRoute()
const pid = computed(() => (route.params.pid as string | undefined) ?? null)
</script>

<template>
  <AppPanel title="核心链路">
    <div class="flex flex-wrap items-stretch gap-1 p-3">
      <template v-for="(node, i) in CHAIN" :key="node.label">
        <ChevronRight v-if="i > 0" :size="12" class="text-fg-4 self-center" />
        <component
          :is="node.route && pid ? RouterLink : 'div'"
          :to="node.route && pid ? { name: node.route, params: { pid } } : undefined"
          class="border-line-1 bg-base-2 flex min-w-24 flex-col gap-0.5 rounded-sm border px-2 py-1.5"
          :class="node.route && pid ? 'hover:border-accent/50 hover:bg-base-3' : ''"
        >
          <span class="text-fg-1 font-mono text-2xs">{{ node.label }}</span>
          <span class="text-fg-4 text-2xs leading-tight">{{ node.desc }}</span>
        </component>
      </template>
    </div>
    <p class="text-fg-4 border-line-1 border-t px-3 py-2 text-2xs">
      业务层不绑定任何具体视频模型：差异全部下沉到 Workflow Adapter。生成版本永不覆盖。
    </p>
  </AppPanel>
</template>
