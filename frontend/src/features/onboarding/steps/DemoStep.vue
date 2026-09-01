<script setup lang="ts">
/**
 * 第二步：演示工程。
 *
 * 照 `services/adopt.py` / `services/packages.py` 那条规矩：**先账单再动手**。进来先拉
 * `POST /onboarding/demo/plan`（一个字节都不写），把「落在哪、会建什么、多大、已经有了会怎样」
 * 摆出来，用户点了「创建并打开」才真的落地。
 *
 * 目录可改（`DirPicker.vue`，与新建工程走同一套后端目录树）。默认落在文档目录下——
 * 安装目录在 Windows 上常常只读。
 *
 * 演示工程里**没有任何已生成的版本**，账单的 `warnings` 会说这句话，这里原样显示：
 * 给一个看着能播其实是假的演示，比空版本轨更糟。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { FolderOpen, Play, RefreshCw } from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppPanel from '@/shared/ui/AppPanel.vue'
import DirPicker from '@/shared/ui/DirPicker.vue'
import { useOnboardingStore } from '@/stores/onboarding'
import { useProjectStore } from '@/stores/project'
import type { DemoSummary } from '@/shared/api/onboarding'

const wiz = useOnboardingStore()
const proj = useProjectStore()
const router = useRouter()

const picking = ref(false)
/** 用户改过的目录；空串表示用后端给的默认位置。 */
const dir = ref('')
const created = ref<DemoSummary | null>(null)

const target = computed(() => wiz.plan?.dir || dir.value || wiz.state?.default_demo_dir || '')
const opened = computed(() => proj.current)

const SUMMARY_LABEL: Record<keyof DemoSummary, string> = {
  characters: '角色',
  locations: '地点',
  props: '道具',
  scenes: '幕',
  shots: '镜头',
  links: '幕间衔接',
}

function mb(bytes: number): string {
  return bytes >= 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1024))} KB`
}

async function refresh(): Promise<void> {
  await wiz.loadPlan(dir.value || undefined).catch(() => {})
}

onMounted(() => void refresh())

function pick(path: string): void {
  dir.value = path
  void refresh()
}

/** 建（或打开）演示工程，并让它成为当前工程——后面两步要往它身上绑预设。 */
async function run(): Promise<void> {
  const result = await wiz.createDemo(dir.value || undefined).catch(() => null)
  if (!result) return
  created.value = result.summary
  await proj.open(result.project.dir).catch(() => {})
}

function visit(): void {
  const pid = proj.current?.id
  if (!pid) return
  wiz.open = false
  void router.push({ name: 'flow', params: { pid } })
}
</script>

<template>
  <div class="space-y-2">
    <AppPanel title="会做什么（账单）">
      <template #actions>
        <AppButton size="sm" variant="ghost" :disabled="wiz.busy" @click="refresh()">
          <RefreshCw :size="10" />重新算
        </AppButton>
        <AppButton size="sm" @click="picking = true"> <FolderOpen :size="10" />换个位置 </AppButton>
      </template>

      <div class="border-line-1 border-b px-3 py-2">
        <p class="text-fg-4 text-2xs">落在这里</p>
        <p class="text-fg-1 mt-0.5 font-mono text-2xs break-all">{{ target || '正在读取…' }}</p>
        <p v-if="wiz.plan" class="text-fg-4 mt-1 text-2xs">
          <template v-if="wiz.plan.action === 'open'">
            这个目录里已经有一个工程了 —— 点下面的按钮只会打开它，不重建、不覆盖。
          </template>
          <template v-else
            >约 {{ mb(wiz.plan.estimated_bytes) }}，其中大部分是几张占位素材图。</template
          >
        </p>
      </div>

      <ul v-if="wiz.plan" class="divide-line-1 divide-y">
        <li
          v-for="item in wiz.plan.items"
          :key="item.kind"
          class="flex items-center gap-2 px-3 py-1.5"
        >
          <span class="text-fg-2 min-w-0 flex-1 text-xs">{{ item.label }}</span>
          <AppBadge tone="neutral">{{ item.count }}</AppBadge>
        </li>
      </ul>

      <ul v-if="wiz.plan?.warnings.length" class="border-line-1 border-t px-3 py-2">
        <li v-for="w in wiz.plan.warnings" :key="w" class="text-st-review text-2xs leading-relaxed">
          · {{ w }}
        </li>
      </ul>
    </AppPanel>

    <div class="flex items-center gap-2">
      <AppButton variant="primary" :disabled="wiz.busy || !target" @click="run()">
        <Play :size="11" />
        {{ wiz.plan?.action === 'open' ? '打开这个工程' : '创建并打开演示工程' }}
      </AppButton>
      <AppButton v-if="opened" variant="ghost" @click="visit()">去幕流程图看看</AppButton>
      <span v-if="opened" class="text-fg-4 min-w-0 truncate text-2xs">
        当前工程：{{ opened.name }}
      </span>
    </div>

    <AppPanel v-if="created" title="已经长出来了">
      <ul class="divide-line-1 divide-y">
        <li
          v-for="(label, key) in SUMMARY_LABEL"
          :key="key"
          class="flex items-center gap-2 px-3 py-1.5"
        >
          <span class="text-fg-2 min-w-0 flex-1 text-xs">{{ label }}</span>
          <AppBadge tone="ok">{{ created[key] }}</AppBadge>
        </li>
      </ul>
      <p class="text-fg-4 border-line-1 border-t px-3 py-2 text-2xs leading-relaxed">
        幕之间那三条线分别是「直接切」「补一段转场」「续接上一幕末帧」——三种衔接各一条，
        进幕流程图就能看到。版本轨是空的：配好下一步的生成服务，才能从这里做出第一段画面。
      </p>
    </AppPanel>

    <DirPicker
      v-model:open="picking"
      :start="target"
      title="演示工程放在哪"
      confirm-label="就放这里"
      @pick="pick"
    />
  </div>
</template>
