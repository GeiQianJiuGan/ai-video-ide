<script setup lang="ts">
/**
 * 「把这一份图设成某种默认」——两处共用的那几颗按钮（最外层的预设页 + 设置页那份清单）。
 *
 * 三个取舍：
 *
 *   1. **哪几颗、能不能按、为什么不能，全部来自 `shared/lib/presets.ts` 那张表**，
 *      这里一条判断都不写。以前两处各写一份按钮，应用级默认从一格变成三格之后必然漂移。
 *   2. **不能按的按钮保持 disabled 并把原因写进 tooltip**，不隐藏——「为什么这份图不能当
 *      首尾帧默认」正是用户要的答案（硬约束 4）。
 *   3. **已经是这项默认的那颗变成「取消」**：留空是有意义的状态（按角色那两项留空 = 退回
 *      共用那份），而这一页上没有别的地方能把它清掉。取消那颗**即使这份图已经不可用也照旧
 *      能按**——一个指着坏图的默认必须能撤下来。
 */
import { computed } from 'vue'
import AppButton from '@/shared/ui/AppButton.vue'
import { presetDefaultRoles, type PresetDefaultRole } from '@/shared/lib/presets'
import type { PresetRow } from '@/shared/api/settings'
import { useSettingsStore } from '@/stores/settings'

const props = defineProps<{ row: PresetRow; column?: string }>()
const cfg = useSettingsStore()

const roles = computed(() => presetDefaultRoles(props.column))
/** 与某一份图无关的那几件事实（出图协议认不认预设）。 */
const ctx = computed(() => ({ imageWantsPreset: Boolean(cfg.draftImageProtocol?.wants_preset) }))

function isMine(role: PresetDefaultRole): boolean {
  return cfg.byKey[role.key]?.value === props.row.name
}

function label(role: PresetDefaultRole): string {
  return isMine(role) ? role.action.replace('设为', '取消') : role.action
}

function title(role: PresetDefaultRole): string {
  if (isMine(role)) return `现在就是这一份。${role.cleared}`
  return role.title(props.row, ctx.value)
}

/** 改完就该生效，所以走 `setOne` / `clear`（立刻 PATCH），不进「保存」那一批草稿。 */
async function toggle(role: PresetDefaultRole): Promise<void> {
  if (isMine(role)) await cfg.clear(role.key).catch(() => {})
  else await cfg.setOne(role.key, props.row.name).catch(() => {})
}
</script>

<template>
  <AppButton
    v-for="role in roles"
    :key="role.key"
    size="sm"
    variant="ghost"
    :disabled="cfg.busy || (!isMine(role) && !role.ready(row))"
    :title="title(role)"
    @click="toggle(role)"
  >
    {{ label(role) }}
  </AppButton>
</template>
