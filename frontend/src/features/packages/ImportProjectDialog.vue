<script setup lang="ts">
/**
 * 导入工程包：还原成一个新工程目录并打开它。
 *
 * 两条不许绕的规矩：
 *   1. **账单没看过不给按导入**（照 adopt 的老规矩）——先把「这个包要什么环境、本机缺什么」
 *      摊开，缺哪份预设要在导入之前就看见；
 *   2. **绝不覆盖用户文件**——目标目录里已经有工程时后端报 `CONFLICT`，这里照原样显示。
 *
 * 导入的副本会拿到一个**新的工程 id**：注册表按 pid 索引，同机导入一份副本后
 * 两个目录同 id 会互相顶掉。
 *
 * **包从哪来：主路是「从我的电脑选一个文件」**（`POST /packages/upload`）。以前只有一条路
 * ——手输一个**后端机器上**的绝对路径；而后端的目录浏览器只列目录、不返回文件内容
 * （`/fs/dirs`），所以那颗「浏览…」只能把包所在的文件夹填进来、文件名还得自己补，包本来
 * 就在用户自己电脑上时那条路压根走不通。上传回来的账单**和 `inspect` 是同一份**
 * （只多 `staged` / `name`），`path` 指向暂存副本，所以下面导入那一步一行都没改。
 *
 * 暂存副本要收拾干净（几个 G 的东西）：导入成功后端自己删；用户取消、关窗、换一个包时
 * 这里显式 `discardStaged`；都漏了还有后端上传时的 TTL 兜底。
 *
 * **新工程落在哪仍然是后端机器上的目录**（`DirPicker`）：工程目录是后端要读写的东西，
 * 浏览器给不了也不该猜。
 */
import { computed, ref, watch } from 'vue'
import { FolderSearch, PackageOpen, ScanSearch, Upload } from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppButton from '@/shared/ui/AppButton.vue'
import AppDialog from '@/shared/ui/AppDialog.vue'
import DirPicker from '@/shared/ui/DirPicker.vue'
import ErrorPanel from '@/shared/ui/ErrorPanel.vue'
import PackageBillPanel from './PackageBillPanel.vue'
import { humanBytes } from '@/shared/api/library'
import { packagesApi, type PackageInfo } from '@/shared/api/packages'
import type { ApiError } from '@/shared/api/client'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ 'update:open': [boolean]; done: [string] }>()

/** 包从哪来。`upload` 是主路（用户自己的电脑），`server` 是后端机器上的一个路径。 */
type Source = 'upload' | 'server'

const source = ref<Source>('upload')
const path = ref('')
const dir = ref('')
const info = ref<PackageInfo | null>(null)
const busy = ref(false)
const error = ref<ApiError | null>(null)
const picking = ref<'package' | 'target' | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)

/** 上传上来的那份临时副本的路径（空 = 现在手上这个包不是上传来的，不该去删它）。 */
const staged = ref('')
/** 上传时那个文件叫什么，只用来给人看——`path` 是暂存区里那个带 id 的名字。 */
const pickedName = ref('')

/** 看过的是**这一个**包吗：换了路径就得重新看一遍，否则按下的是上一份账单。 */
const seen = ref('')
const billSeen = computed(() => info.value !== null && seen.value === path.value.trim())

/**
 * 丢掉暂存区里那份临时副本。
 *
 * **失败不打扰用户**：这是一次清理，后端上传时的 TTL 兜底还会再来一次，而此刻用户要的是
 * 「关掉这个框」或者「换一个包」。先清 `staged` 再发请求，免得连点两下删两遍。
 */
async function discard(): Promise<void> {
  const target = staged.value
  if (target === '') return
  staged.value = ''
  try {
    await packagesApi.discardStaged(target)
  } catch {
    /* 见 services/packages.py::_prune_uploads —— 过期的副本下一次上传时会被清掉 */
  }
}

/** 换包 / 换来源：先把账单作废，再把上一份临时副本收走。 */
function resetPick(): void {
  info.value = null
  seen.value = ''
  error.value = null
  path.value = ''
  pickedName.value = ''
  void discard()
}

/**
 * 主路：把用户电脑上那个文件传上去，回来的就是账单。
 *
 * 上传完账单就已经看过了（同一份数据），所以这条路上没有「看一眼」那一步。
 */
async function onPickFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  resetPick()
  busy.value = true
  try {
    const st = await packagesApi.upload(file)
    info.value = st
    path.value = st.path
    seen.value = st.path
    staged.value = st.path
    pickedName.value = st.name
  } catch (e) {
    error.value = e as ApiError
  } finally {
    busy.value = false
  }
}

/** 第二条路：读后端机器上那个路径（桌面版里就是本机，几个 G 不必自己传给自己）。 */
async function inspect(): Promise<void> {
  busy.value = true
  error.value = null
  info.value = null
  try {
    info.value = await packagesApi.inspect(path.value.trim())
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
    const res = await packagesApi.importProject(path.value.trim(), dir.value.trim())
    // 还原完了，暂存副本已经由后端删掉（`packages.import_project`），这里别再删第二遍
    staged.value = ''
    emit('done', res.project.dir)
  } catch (e) {
    error.value = e as ApiError
  } finally {
    busy.value = false
  }
}

function pickedDir(p: string): void {
  if (picking.value === 'package') path.value = `${p}/`
  else if (picking.value === 'target') dir.value = p
  picking.value = null
}

/** 换来源等于换包：上一条路上填的路径在另一条路上没有意义。 */
watch(source, () => resetPick())

watch(
  () => props.open,
  (now) => {
    if (now) {
      info.value = null
      seen.value = ''
      error.value = null
      return
    }
    // 关窗 = 放弃这次导入：上传上来的那份副本没人再要了
    resetPick()
  },
)

const wrongScope = computed(() => info.value !== null && info.value.scope !== 'project')
const canImport = computed(
  () => billSeen.value && !wrongScope.value && !busy.value && dir.value.trim() !== '',
)
</script>

<template>
  <AppDialog
    :open="open"
    title="导入工程包"
    subtitle="先看清单，再还原成一个新工程"
    size="lg"
    @update:open="emit('update:open', $event)"
  >
    <div class="space-y-2 p-3">
      <!-- 包从哪来：默认从用户自己的电脑选一个文件；读后端机器上的路径是第二条路 -->
      <div class="border-line-1 bg-base-2 border p-2">
        <span class="text-fg-3 block text-2xs">包在哪</span>
        <label class="mt-1 flex cursor-pointer items-start gap-1.5 text-xs">
          <input v-model="source" type="radio" value="upload" class="accent-accent mt-0.5" />
          <span>
            <span class="text-fg-2">从我的电脑选一个文件（默认）</span>
            <span class="text-fg-4 block text-2xs">
              传上去落进暂存区，取消或关窗时就删掉，不动你磁盘上那份包。
            </span>
          </span>
        </label>
        <label class="mt-1.5 flex cursor-pointer items-start gap-1.5 text-xs">
          <input v-model="source" type="radio" value="server" class="accent-accent mt-0.5" />
          <span>
            <span class="text-fg-2">读后端机器上的一个路径</span>
            <span class="text-fg-4 block text-2xs">
              桌面版里那就是本机，几个 G 的包不必先传一遍。
            </span>
          </span>
        </label>

        <div v-if="source === 'upload'" class="mt-2 flex items-center gap-1.5">
          <AppButton variant="primary" :disabled="busy" @click="fileInput?.click()">
            <Upload :size="12" />{{ busy ? '上传中…' : '选择 .aivspkg 文件' }}
          </AppButton>
          <p class="text-fg-4 min-w-0 flex-1 truncate text-2xs">
            <span v-if="pickedName" class="text-fg-2 font-mono">{{ pickedName }}</span>
            <span v-else>还没选文件</span>
          </p>
          <input
            ref="fileInput"
            type="file"
            accept=".aivspkg,application/zip"
            class="hidden"
            @change="onPickFile"
          />
        </div>

        <label v-else class="mt-2 block">
          <span class="text-fg-3 text-2xs">包文件在后端机器上的绝对路径（.aivspkg）</span>
          <div class="mt-0.5 flex items-center gap-1.5">
            <input
              v-model="path"
              type="text"
              placeholder="E:/包/我的片子.aivspkg"
              class="border-line-1 bg-base-1 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 font-mono text-xs outline-none"
            />
            <AppButton title="选到包所在的文件夹，文件名自己补" @click="picking = 'package'">
              <FolderSearch :size="12" />浏览…
            </AppButton>
            <AppButton variant="primary" :disabled="busy || path.trim() === ''" @click="inspect()">
              <ScanSearch :size="12" />看一眼
            </AppButton>
          </div>
        </label>
      </div>

      <div v-if="info" class="border-line-1 bg-base-2 border p-2 text-2xs">
        <p class="text-fg-1 flex items-center gap-1.5">
          {{ info.project.name || '未命名' }}
          <AppBadge :tone="info.scope === 'project' ? 'accent' : 'warn'">
            {{ info.scope === 'project' ? '工程包' : `${info.scope} 包` }}
          </AppBadge>
          <AppBadge v-if="info.include_generated" tone="ok">带成片</AppBadge>
        </p>
        <p class="text-fg-4 mt-0.5">
          {{ humanBytes(info.bytes) }} ·
          <span v-for="(value, key) in info.counts" :key="key" class="mr-2">
            {{ key }} <span class="tnum text-fg-2">{{ value }}</span>
          </span>
        </p>
        <p class="text-fg-4">
          由 {{ info.app || '未知版本' }} 于 {{ info.created_at || '未知时间' }} 导出
        </p>
      </div>

      <p v-if="wrongScope" class="text-st-failed text-2xs">
        这是一个「一幕的设定」包，不能还原成工程——请先打开一个工程，再用流程图上的「导入一幕」。
      </p>

      <label v-if="billSeen && !wrongScope" class="block">
        <span class="text-fg-3 text-2xs">新工程放在哪个目录（空目录；已有工程会被拒绝）</span>
        <div class="mt-0.5 flex items-center gap-1.5">
          <input
            v-model="dir"
            type="text"
            placeholder="E:/aivs/还原的片子"
            class="border-line-1 bg-base-2 text-fg-1 placeholder:text-fg-4 focus:border-accent/60 h-row min-w-0 flex-1 rounded-sm border px-2 font-mono text-xs outline-none"
          />
          <AppButton title="浏览后端机器上的文件夹" @click="picking = 'target'">
            <FolderSearch :size="12" />浏览…
          </AppButton>
        </div>
      </label>

      <PackageBillPanel v-if="info" :omitted="info.omitted" :env-check="info.env_check" />
    </div>

    <ErrorPanel v-if="error" class="mx-3 mb-3" :error="error" @dismiss="error = null" />

    <template #footer>
      <p class="text-fg-4 min-w-0 flex-1 text-2xs">
        {{
          billSeen
            ? '导入的副本会拿到一个新的工程 id，原工程不受影响。'
            : source === 'upload'
              ? '选一个包才能看到清单。'
              : '先「看一眼」才能导入。'
        }}
      </p>
      <AppButton variant="ghost" @click="emit('update:open', false)">取消</AppButton>
      <AppButton variant="primary" :disabled="!canImport" @click="run()">
        <PackageOpen :size="12" />{{ busy ? '还原中…' : '导入并打开' }}
      </AppButton>
    </template>
  </AppDialog>

  <DirPicker
    :open="picking !== null"
    :start="picking === 'package' ? path : dir"
    :title="picking === 'package' ? '选到包所在的文件夹' : '选择新工程放在哪个文件夹'"
    :confirm-label="picking === 'package' ? '就是这个文件夹' : '还原到这里'"
    @update:open="picking = $event ? picking : null"
    @pick="pickedDir"
  />
</template>
