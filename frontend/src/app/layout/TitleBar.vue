<script setup lang="ts">
/**
 * 顶部标题栏：应用身份 + 当前位置面包屑 + 全局动作（AI 导演 / 导出工程包 / 命令面板）。
 *
 * 这是「应用感」的锚点：任何时候都能看出自己在哪个项目、哪个功能里。
 *
 * 右边那三颗按钮是**跨页的动作**，所以只能长在这里：
 *   - AI 导演是右侧停靠栏（`DirectorDock`），跟着人从剧本走到流程图都不消失；
 *   - 导出工程包以前只长在项目管理页上，而打开工程之后应用级导航整排消失（两级互斥），
 *     于是「打开了工程却导不出这个工程」——它必须在工程内也有入口。
 * 两个开合状态都在 `stores/shell.ts` 里，弹窗与停靠栏本体挂在 `WorkbenchLayout` 上。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { Bot, ChevronRight, Command, Film, PackagePlus } from '@lucide/vue'
import { featureByRoute } from '@/app/features'
import { useProjectStore } from '@/stores/project'
import { useShellStore } from '@/stores/shell'

const props = defineProps<{ projectId: string | null }>()
const emit = defineEmits<{ openPalette: [] }>()

const route = useRoute()
const proj = useProjectStore()
const shell = useShellStore()
const feature = computed(() => featureByRoute(route.name as string | undefined))

const place = computed(() => {
  if (feature.value) return feature.value.title
  if (route.name === 'settings') return '设置'
  return '工作台'
})

/**
 * 项目段只在项目内页面显示。
 * 站在项目管理页 / 素材库 / 设置里说「未打开项目 ›」是噪音——
 * 那些页面本来就不需要工程。
 */
const showProject = computed(() => feature.value?.scope !== 'app' && route.name !== 'settings')

/** 面包屑上显示的是人看得懂的项目名；工程还没加载出来时退回 pid。 */
const projectLabel = computed(() => {
  if (!props.projectId) return '未打开项目'
  if (proj.current?.id === props.projectId) return proj.current.name
  return props.projectId
})

/** 悬停能看到工程在磁盘上的位置——「这是哪个项目」不能只靠名字猜。 */
const projectTitle = computed(() =>
  proj.current?.id === props.projectId ? proj.current.dir : '未打开任何工程',
)
</script>

<template>
  <header
    class="bg-base-2 border-line-1 flex h-row shrink-0 items-center gap-2 border-b px-2 text-xs"
  >
    <span class="text-accent flex shrink-0 items-center gap-1.5 font-medium">
      <Film :size="13" :stroke-width="1.8" />AI Video Studio
    </span>

    <span class="text-fg-4">|</span>

    <nav class="flex min-w-0 items-center gap-1">
      <template v-if="showProject">
        <span class="text-fg-3 truncate" :class="!projectId && 'text-fg-4'" :title="projectTitle">
          {{ projectLabel }}
        </span>
        <ChevronRight :size="12" class="text-fg-4 shrink-0" />
      </template>
      <span class="text-fg-1 truncate">{{ place }}</span>
    </nav>

    <div class="ml-auto flex shrink-0 items-center gap-1.5">
      <!-- 工程内才有对象可谈 / 可导：没打开工程时这两颗按钮不画，而不是画成灰的 -->
      <template v-if="projectId">
        <button
          type="button"
          class="border-line-1 hover:border-line-2 flex h-5 items-center gap-1.5 rounded-sm border px-2 text-2xs"
          :class="shell.directorOpen ? 'text-accent border-accent/50' : 'text-fg-3 hover:text-fg-1'"
          title="右侧 AI 导演：跟它说一句话，它提一份可逐条审阅的提案（按下采用之前库里什么都不会变）。这一栏跟着你换页，不会关掉"
          @click="shell.toggleDirector()"
        >
          <Bot :size="10" />AI 导演
          <kbd class="text-fg-4 font-mono">Ctrl I</kbd>
        </button>
        <button
          type="button"
          class="text-fg-3 hover:text-fg-1 border-line-1 hover:border-line-2 flex h-5 items-center gap-1.5 rounded-sm border px-2 text-2xs"
          title="把当前工程导出成一个 .aivspkg 包（先出账单再动手）。密钥与服务地址一律不进包"
          @click="shell.openExport()"
        >
          <PackagePlus :size="10" />导出工程
        </button>
      </template>
      <button
        type="button"
        class="text-fg-3 hover:text-fg-1 border-line-1 hover:border-line-2 flex h-5 items-center gap-1.5 rounded-sm border px-2 text-2xs"
        title="打开命令面板，搜索并跳转到任意功能"
        @click="emit('openPalette')"
      >
        <Command :size="10" />搜索功能
        <kbd class="text-fg-4 font-mono">Ctrl K</kbd>
      </button>
    </div>
  </header>
</template>
