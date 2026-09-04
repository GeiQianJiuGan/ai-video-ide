<script setup lang="ts">
/**
 * 「这一份图现在是哪几种默认」——只画徽标。
 *
 * 与 `PresetDefaultButtons.vue` 读同一张表（`shared/lib/presets.ts`），于是「默认」这件事
 * 在最外层的预设页与设置页里说的是同一句话。分成两个件是因为两处的行布局都是
 * 「名字旁边贴徽标、行尾放按钮」，中间还隔着槽位徽标与那句 `ref_hint`。
 */
import { computed } from 'vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import { presetDefaultRoles } from '@/shared/lib/presets'
import type { PresetRow } from '@/shared/api/settings'
import { useSettingsStore } from '@/stores/settings'

const props = defineProps<{ row: PresetRow; column?: string }>()
const cfg = useSettingsStore()

/** 指向这一份图的那几种默认。读的是**已保存**的值而不是草稿——徽标说的是事实。 */
const mine = computed(() =>
  presetDefaultRoles(props.column).filter((role) => cfg.byKey[role.key]?.value === props.row.name),
)
</script>

<template>
  <AppBadge v-for="role in mine" :key="role.key" tone="accent">{{ role.badge }}</AppBadge>
</template>
