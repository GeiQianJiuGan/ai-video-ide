<script setup lang="ts">
/**
 * 第一步：这是什么。
 *
 * 只讲三句话 + 一条链路。链路条直接复用概览页那个 `ChainStrip.vue`（文案在
 * `app/features.ts::CHAIN`），所以「系统怎么组织一部片子」在两个地方永远说得一样。
 * 它在没有工程时会退化成不可点的节点——这一步通常还没有工程，正好。
 */
import { Bot, Cpu, UserRound } from '@lucide/vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import ChainStrip from '@/shared/ui/ChainStrip.vue'

const ROLES = [
  {
    icon: Bot,
    title: 'AI = 素材生产器',
    body: '出图、出片段、出配音。它只负责「这一格长什么样」，不负责整部片子怎么排。',
  },
  {
    icon: Cpu,
    title: 'System = 视频工程与编排器',
    body: '角色、场景、镜头、上下文、版本、时间线都落在工程库里。真源永远是 project.db，不是模型的记忆。',
  },
  {
    icon: UserRound,
    title: 'Human = 导演',
    body: '你决定谁出场、镜头怎么接、用哪一段。AI 不做决定，只把你的决定变成画面。',
  },
]

const RULES = [
  '生成版本永不覆盖：每次生成都是新的一版，换用哪一段只是换指针，旧的一条都不删。',
  'LLM 是可选的：不配大模型也能走完全流程，只是拆镜头和写 prompt 要自己动手。',
  '模型端的图由模型端维护：本工具只按节点标题往里注参数，不改你的 lora 与加速节点。',
]
</script>

<template>
  <div class="space-y-2">
    <AppPanel title="三个角色">
      <ul class="divide-line-1 divide-y">
        <li v-for="r in ROLES" :key="r.title" class="flex items-start gap-2 px-3 py-2">
          <component :is="r.icon" :size="14" :stroke-width="1.6" class="text-accent mt-0.5" />
          <div class="min-w-0">
            <p class="text-fg-1 text-xs">{{ r.title }}</p>
            <p class="text-fg-3 mt-0.5 text-2xs leading-relaxed">{{ r.body }}</p>
          </div>
        </li>
      </ul>
    </AppPanel>

    <ChainStrip />

    <AppPanel title="三条不会变的规矩">
      <ul class="divide-line-1 divide-y">
        <li v-for="line in RULES" :key="line" class="text-fg-3 px-3 py-2 text-2xs leading-relaxed">
          {{ line }}
        </li>
      </ul>
    </AppPanel>

    <p class="text-fg-4 text-2xs leading-relaxed">
      接下来四步：先长一份能点开看的演示工程，再配生成服务，然后把预设或 API
      绑到工程上，最后把每个功能过一遍。中途关掉也没关系——进度记在本机，下次接着走。
    </p>
  </div>
</template>
