<script setup lang="ts">
/**
 * 新手引导向导（Step 10）：五步走完「这是什么 → 有东西可看 → 连上服务 → 绑定 → 都有哪些功能」。
 *
 * 三处设计上的取舍：
 *
 *   1. **不用 `AppDialog`**：五步的内容撑不进那个尺寸，所以照 `CommandPalette.vue` 的做法
 *      自己起一层 reka-ui Dialog（焦点陷阱 + ARIA 照旧由它管）。
 *   2. **步骤记在后端**（`PATCH /onboarding`）：中途关掉再打开接着走，而不是从头再来。
 *   3. **它不进功能注册表**：向导是覆盖层不是页面，登记进去会让它出现在导航与它自己的
 *      巡览列表里（自我指涉）。
 *
 * 关掉 ≠ 走完：右上角那个 X 只收起这一层，`completed` 只有点「完成」才写。
 */
import { computed } from 'vue'
import { DialogContent, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui'
import { Check, ChevronLeft, ChevronRight, X } from '@lucide/vue'
import AppButton from '@/shared/ui/AppButton.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import WelcomeStep from './steps/WelcomeStep.vue'
import DemoStep from './steps/DemoStep.vue'
import ServiceStep from './steps/ServiceStep.vue'
import BindStep from './steps/BindStep.vue'
import TourStep from './steps/TourStep.vue'
import { STEP_LABEL, useOnboardingStore } from '@/stores/onboarding'
import type { OnboardingStep } from '@/shared/api/onboarding'

const wiz = useOnboardingStore()

/** 每一步一句「这一步要干什么」。步骤顺序仍然由后端给，这里只是文案表。 */
const STEP_HINT: Record<OnboardingStep, string> = {
  welcome: 'AI 出素材，系统做编排，你当导演',
  demo: '先有一份能点开看的工程，再谈配置',
  service: '视频 / 图片 / LLM 三条链分开配，各自能单独测',
  bind: 'ComfyUI 预设或通用 REST API，二选一',
  tour: '每个功能一张卡，点进去就是那一页',
}

/** 走过了几步——步骤条上打勾的那些。 */
const done = computed(() => wiz.stepIndex)

function go(step: OnboardingStep): void {
  void wiz.setStep(step)
}
</script>

<template>
  <DialogRoot :open="wiz.open" @update:open="wiz.open = $event">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-40 bg-black/70" />
      <DialogContent
        class="border-line-2 bg-base-1 fixed top-[6vh] left-1/2 z-50 flex h-[88vh] w-[min(64rem,94vw)] -translate-x-1/2 overflow-hidden rounded-md border shadow-2xl"
      >
        <!-- 左：步骤条。走过的打勾，当前高亮，后面的也能点——引导不是考试 -->
        <nav class="border-line-1 bg-base-2 flex w-52 shrink-0 flex-col border-r">
          <DialogTitle
            class="text-fg-1 border-line-1 h-row flex shrink-0 items-center border-b px-3 text-xs font-medium"
          >
            新手引导
          </DialogTitle>
          <ul class="min-h-0 flex-1 overflow-auto py-1">
            <li v-for="(s, i) in wiz.steps" :key="s">
              <button
                class="flex w-full items-start gap-2 px-3 py-2 text-left"
                :class="s === wiz.step ? 'bg-base-3' : 'hover:bg-base-2'"
                @click="go(s)"
              >
                <span
                  class="mt-px flex size-4 shrink-0 items-center justify-center rounded-full border text-2xs"
                  :class="
                    i < done
                      ? 'border-st-done/40 text-st-done bg-st-done/10'
                      : s === wiz.step
                        ? 'border-accent/60 text-accent bg-accent-dim'
                        : 'border-line-2 text-fg-4'
                  "
                >
                  <Check v-if="i < done" :size="9" />
                  <template v-else>{{ i + 1 }}</template>
                </span>
                <span class="min-w-0 flex-1">
                  <span
                    class="block truncate text-xs"
                    :class="s === wiz.step ? 'text-fg-1' : 'text-fg-3'"
                  >
                    {{ STEP_LABEL[s] }}
                  </span>
                  <span class="text-fg-4 block text-2xs leading-tight">{{ STEP_HINT[s] }}</span>
                </span>
              </button>
            </li>
          </ul>
          <p class="text-fg-4 border-line-1 border-t px-3 py-2 text-2xs leading-relaxed">
            随时可以关掉。设置页与命令面板里都有「重新打开新手引导」。
          </p>
        </nav>

        <!-- 右：当前那一步 -->
        <div class="flex min-w-0 flex-1 flex-col">
          <header class="border-line-1 h-row flex shrink-0 items-center gap-2 border-b px-3">
            <span class="text-fg-1 text-xs font-medium">
              第 {{ wiz.stepIndex + 1 }} / {{ wiz.steps.length }} 步 · {{ STEP_LABEL[wiz.step] }}
            </span>
            <span class="text-fg-4 min-w-0 flex-1 truncate text-2xs">
              {{ STEP_HINT[wiz.step] }}
            </span>
            <button
              class="text-fg-4 hover:text-fg-1 shrink-0"
              title="收起（进度已经记住了，下次接着走）"
              @click="wiz.open = false"
            >
              <X :size="13" />
            </button>
          </header>

          <div class="min-h-0 flex-1 overflow-auto p-3">
            <ErrorPanel :error="wiz.lastError" class="mb-2" @dismiss="wiz.clearError()" />
            <WelcomeStep v-if="wiz.step === 'welcome'" />
            <DemoStep v-else-if="wiz.step === 'demo'" />
            <ServiceStep v-else-if="wiz.step === 'service'" />
            <BindStep v-else-if="wiz.step === 'bind'" />
            <TourStep v-else-if="wiz.step === 'tour'" />
          </div>

          <footer class="border-line-1 flex shrink-0 items-center gap-2 border-t px-3 py-2">
            <AppButton variant="ghost" :disabled="wiz.stepIndex <= 0" @click="wiz.prev()">
              <ChevronLeft :size="11" />上一步
            </AppButton>
            <span class="flex-1"></span>
            <AppButton
              variant="ghost"
              title="跳过引导。以后可以从设置页或命令面板重新走一遍"
              @click="wiz.skip()"
            >
              跳过引导
            </AppButton>
            <AppButton variant="primary" @click="wiz.next()">
              <template v-if="wiz.isLast"><Check :size="11" />完成</template>
              <template v-else>下一步<ChevronRight :size="11" /></template>
            </AppButton>
          </footer>
        </div>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
