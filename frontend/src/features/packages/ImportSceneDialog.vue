<script setup lang="ts">
/**
 * 导入一幕的设定到**当前**工程。
 *
 * 与工程导入的差别只有一个：这里不新建工程，而是把包里那一幕落进已经打开的这个工程，
 * id 全部重映射、素材按 sha1 只有一份。所以账单里最重要的一列是
 * **每个人物 / 地点 / 道具是复用还是新建**——同一部片子里多幕引用同一个「林小雨」是常态，
 * 每导一幕多长出一个同名角色才是 bug（所以 `reuse_by_name` 默认开着）。
 *
 * 照旧**账单没看过不给按导入**。
 */
import { computed, ref, watch } from 'vue'
import { FolderSearch, PackageOpen, ScanSearch } from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import DirPicker from '@/shared/ui/DirPicker.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import PackageBillPanel from './PackageBillPanel.vue'
import { packagesApi, type SceneImportPlan, type SceneImportResult } from '@/shared/api/packages'
import type { ApiError } from '@/shared/api/client'

const props = defineProps<{ open: boolean; pid: string }>()
const emit = defineEmits<{ 'update:open': [boolean]; done: [SceneImportResult] }>()

const path = ref('')
const reuseByName = ref(true)
const plan = ref<SceneImportPlan | null>(null)
const result = ref<SceneImportResult | null>(null)
const busy = ref(false)
const error = ref<ApiError | null>(null)
const picking = ref(false)

const seen = ref('')
const billSeen = computed(() => plan.value !== null && seen.value === path.value.trim())

const KIND_LABEL: Record<string, string> = {
  character: '人物',
  location: '地点',
  prop: '道具',
}

async function loadPlan(): Promise<void> {
  busy.value = true
  error.value = null
  plan.value = null
  try {
    plan.value = await packagesApi.planSceneImport(props.pid, path.value.trim(), reuseByName.value)
    seen.value = path.value.trim()
  } catch (e) {
    error.value = e as ApiError
  } finally {
    busy.value = false
  }
}

async function run(): Promise<void> {
  busy.value = true
  error.value = null
  try {
    result.value = await packagesApi.importScene(props.pid, path.value.trim(), reuseByName.value)
    emit('done', result.value)
  } catch (e) {
    error.value = e as ApiError
  } finally {
    busy.value = false
  }
}

/** 换了「同名复用」这个勾，账单的每一行都会变——必须重新出一份。 */
watch(reuseByName, () => {
  if (props.open && billSeen.value) void loadPlan()
})

watch(
  () => props.open,
  (now) => {
    if (!now) return
    plan.value = null
    result.value = null
    seen.value = ''
    error.value = null
  },
)

const canImport = computed(() => billSeen.value && !busy.value && result.value === null)
</script>

<template>
  <AppDialog
    :open="open"
    title="导入一幕的设定"
    subtitle="落进当前这个工程 · id 全部重映射"
    size="lg"
    @update:open="emit('update:open', $event)"
  >
    <div class="space-y-2 p-3">
      <div v-if="result" class="border-st-done/40 bg-st-done/5 border p-2 text-2xs">
        <p class="text-st-done">
          已导入「{{ result.scene.title || '未命名' }}」：{{ result.shots }} 个镜头 ·
          {{ result.shot_links }} 条镜头衔接 · 素材新增 {{ result.assets.assets_new }} / 复用
          {{ result.assets.assets_reused }}
        </p>
        <p v-if="result.assets.assets_missing > 0" class="text-st-review mt-0.5">
          有 {{ result.assets.assets_missing }} 条素材在包里就已经缺文件了。
        </p>
      </div>

      <template v-else>
        <label class="block">
          <span class="text-fg-3 text-2xs">场景包的绝对路径（.aivspkg）</span>
          <div class="mt-0.5 flex items-center gap-1.5">
            <input
              v-model="path"
              type="text"
              placeholder="E:/包/雨夜旧宅.aivspkg"
              class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 font-mono text-xs outline-none"
            />
            <AppButton title="选到包所在的文件夹，文件名自己补" @click="picking = true">
              <FolderSearch :size="12" />浏览…
            </AppButton>
            <AppButton variant="primary" :disabled="busy || path.trim() === ''" @click="loadPlan()">
              <ScanSearch :size="12" />出账单
            </AppButton>
          </div>
        </label>

        <label class="flex cursor-pointer items-start gap-1.5 text-xs">
          <input v-model="reuseByName" type="checkbox" class="accent-accent mt-0.5" />
          <span>
            <span class="text-fg-2">同名的人物 / 地点 / 道具复用已有的</span>
            <span class="text-fg-4 block text-2xs">
              关掉就全部新建一份。同一部片子里多幕引用同一个角色是常态，所以默认开着。
            </span>
          </span>
        </label>

        <div v-if="plan" class="border-line-1 bg-base-2 border p-2 text-2xs">
          <p class="text-fg-1">
            要落进「{{ plan.target_project.name }}」的是：{{ plan.scene.title || '未命名' }}
          </p>
          <p class="text-fg-4 mt-0.5">
            素材共 {{ plan.assets.total }} 个 · 按 sha1 复用 {{ plan.assets.reuse }} · 复制
            {{ plan.assets.copy }}
          </p>
          <ul v-if="plan.entities.length > 0" class="mt-1 space-y-0.5">
            <li v-for="row in plan.entities" :key="`${row.kind}-${row.name}`">
              <span class="text-fg-3">{{ KIND_LABEL[row.kind] || row.kind }}</span>
              <span class="text-fg-1 ml-1">{{ row.name }}</span>
              <AppBadge :tone="row.action === 'reuse' ? 'ok' : 'accent'" class="ml-1">
                {{ row.action === 'reuse' ? '复用已有' : '新建' }}
              </AppBadge>
            </li>
          </ul>
        </div>

        <PackageBillPanel v-if="plan" :omitted="plan.omitted" :env-check="plan.env_check" />
      </template>
    </div>

    <ErrorPanel v-if="error" class="mx-3 mb-3" :error="error" @dismiss="error = null" />

    <template #footer>
      <p class="text-fg-4 min-w-0 flex-1 text-2xs">
        {{ billSeen ? '这一幕会插在最后，原有的幕一个都不动。' : '先出账单才能导入。' }}
      </p>
      <AppButton variant="ghost" @click="emit('update:open', false)">
        {{ result ? '关闭' : '取消' }}
      </AppButton>
      <AppButton v-if="!result" variant="primary" :disabled="!canImport" @click="run()">
        <PackageOpen :size="12" />{{ busy ? '导入中…' : '导入这一幕' }}
      </AppButton>
    </template>
  </AppDialog>

  <DirPicker
    :open="picking"
    :start="path"
    title="选到包所在的文件夹"
    confirm-label="就是这个文件夹"
    @update:open="picking = $event"
    @pick="
      (p) => {
        path = `${p}/`
        picking = false
      }
    "
  />
</template>
