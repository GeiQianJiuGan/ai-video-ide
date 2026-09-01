<script setup lang="ts">
/**
 * 第五步：功能巡览。
 *
 * 文案**一个字都不在这里写**：分组、标题、作用、产出、能力要求全部来自
 * `app/features.ts`（Activity Bar、入口页、命令面板、功能页共用的那一份），
 * 所以以后加一个功能会自动出现在巡览里，也永远不会和导航里的说法不一致。
 *
 * 只列 `advanced: false` 的：Workflow 管理与队列页是兼容 / 细看路径，
 * 新手在这里看见它们只会分不清主路。
 *
 * 能力要求满不满足照 `shared/ui/FeatureHeader.vue` 那一套判断（`stores/system`），
 * 不在这一页算第二遍。`scope === 'project'` 的卡片没有工程时 disabled 并写清原因——
 * 不画点了会 404 的假入口（后端重启后进程里没有已打开的工程，这是设计而不是 bug）。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight } from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import { FEATURES, GROUPS, REQUIREMENT_LABEL, type Feature, type Requirement } from '@/app/features'
import { useOnboardingStore } from '@/stores/onboarding'
import { useProjectStore } from '@/stores/project'
import { useSystemStore } from '@/stores/system'

const wiz = useOnboardingStore()
const proj = useProjectStore()
const sys = useSystemStore()
const router = useRouter()

const pid = computed(() => proj.current?.id ?? null)

/** 巡览只讲主路：advanced 的兼容 / 细看页面不在这里露面。 */
const visible = computed(() => FEATURES.filter((f) => !f.advanced))
const groups = computed(() =>
  GROUPS.map((g) => ({ ...g, items: visible.value.filter((f) => f.group === g.id) })).filter(
    (g) => g.items.length,
  ),
)

/** 与 FeatureHeader 同一套判断：backend 看 health，其余看 deps 里那一项。 */
function satisfied(req: Requirement): boolean {
  if (req === 'backend') return sys.health !== null
  return sys.deps.find((d) => d.name === req)?.ok ?? false
}

function locked(f: Feature): boolean {
  return f.scope === 'project' && !pid.value
}

function why(f: Feature): string {
  if (locked(f)) return '这一页要先有打开的工程——回第二步建一份演示工程即可'
  const missing = f.requires.filter((r) => !satisfied(r)).map((r) => REQUIREMENT_LABEL[r])
  return missing.length
    ? `现在还缺：${missing.join(' / ')}。页面照旧能打开，缺的那几个按钮会是灰的并写明原因`
    : `去 ${f.title} 看看`
}

/** 跳过去 = 收起向导（进度已经记住了），项目页要带上 pid。 */
function visit(f: Feature): void {
  if (locked(f)) return
  wiz.open = false
  void router.push(
    f.scope === 'project' ? { name: f.route, params: { pid: pid.value } } : { name: f.route },
  )
}
</script>

<template>
  <div class="space-y-2">
    <p class="text-fg-3 border-line-1 bg-base-2 border px-3 py-2 text-2xs leading-relaxed">
      一共 {{ visible.length }} 个功能，按「这一层回答什么问题」分五组。
      点「去看看」会收起向导直接跳到那一页，进度已经记住了，随时可以从设置页或命令面板回来。
      <span v-if="!pid" class="text-st-review">
        现在没有打开的工程，所以叙事 / 生成 / 成片那几层的卡片是灰的。
      </span>
    </p>

    <AppPanel v-for="g in groups" :key="g.id" :title="`${g.title} · ${g.question}`">
      <ul class="divide-line-1 divide-y">
        <li v-for="f in g.items" :key="f.route" class="px-3 py-2">
          <div class="flex items-start gap-2">
            <component
              :is="f.icon"
              :size="14"
              :stroke-width="1.6"
              class="mt-0.5 shrink-0"
              :class="locked(f) ? 'text-fg-4' : 'text-accent'"
            />
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-1.5">
                <span class="text-xs" :class="locked(f) ? 'text-fg-3' : 'text-fg-1'">
                  {{ f.title }}
                </span>
                <AppBadge tone="neutral">{{ f.milestone }}</AppBadge>
                <AppBadge v-for="r in f.requires" :key="r" :tone="satisfied(r) ? 'ok' : 'warn'">
                  {{ REQUIREMENT_LABEL[r] }}
                </AppBadge>
              </div>
              <p class="text-fg-3 mt-0.5 text-2xs leading-relaxed">{{ f.purpose }}</p>
              <ul class="mt-1 space-y-0.5">
                <li
                  v-for="line in f.outcome"
                  :key="line"
                  class="text-fg-4 text-2xs leading-relaxed"
                >
                  · {{ line }}
                </li>
              </ul>
            </div>
            <AppButton size="sm" :disabled="locked(f)" :title="why(f)" @click="visit(f)">
              去看看<ArrowRight :size="10" />
            </AppButton>
          </div>
        </li>
      </ul>
    </AppPanel>

    <p class="text-fg-4 text-2xs leading-relaxed">
      还有两个页面没列在上面：Workflow 管理与生成队列。它们是兼容与细看路径，
      平时看任务用底部控制台（Ctrl + `），需要看失败现场时从命令面板进。
      点右下角「完成」就走完了引导——设置页与命令面板里都能重新走一遍。
    </p>
  </div>
</template>
