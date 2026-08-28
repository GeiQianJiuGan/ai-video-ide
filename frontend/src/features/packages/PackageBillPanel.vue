<script setup lang="ts">
/**
 * 包账单里那两张「必须原样显示」的清单：**带不走的东西** 与 **环境要求的比对结果**。
 *
 * 抽出来是因为它在四个地方一模一样地出现（导出工程 / 导出一幕 / 导入工程 / 导入一幕），
 * 而且这两张表的口径只该有一处：跳过不是失败，但不说出来就是静默失败；
 * 「本机缺这份预设」也必须在导入之前看见——等到入队才报「选中的预设不存在」，
 * 用户早就忘了自己导入过什么。
 */
import { AlertTriangle, PackageMinus } from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import type { EnvCheck, MissingAsset, OmittedItem } from '@/shared/api/packages'

withDefaults(
  defineProps<{
    omitted?: OmittedItem[]
    envCheck?: EnvCheck | null
    /** 库里有这一行、但文件已经不在磁盘上的资产。 */
    missing?: MissingAsset[]
  }>(),
  { omitted: () => [], envCheck: null, missing: () => [] },
)
</script>

<template>
  <div class="space-y-2">
    <!-- 带不走的东西：跳过不是失败，但必须说出来 -->
    <div v-if="omitted.length > 0" class="border-line-1 bg-base-2 border p-2">
      <p class="text-fg-2 flex items-center gap-1 text-2xs">
        <PackageMinus :size="11" />带不走的东西（{{ omitted.length }} 项）
      </p>
      <ul class="mt-1 space-y-1">
        <li v-for="row in omitted" :key="row.kind" class="text-2xs">
          <span class="text-fg-1">{{ row.label }}</span>
          <span v-if="row.count !== null" class="text-fg-3 tnum"> · {{ row.count }}</span>
          <p class="text-fg-4 leading-tight">{{ row.reason }}</p>
        </li>
      </ul>
    </div>

    <!-- 文件已经不在磁盘上的资产：包照旧带走那一行，但换机后仍然缺图 -->
    <div v-if="missing.length > 0" class="border-st-review/40 bg-st-review/5 border p-2">
      <p class="text-st-review flex items-center gap-1 text-2xs">
        <AlertTriangle :size="11" />有 {{ missing.length }} 条素材的文件已经不在磁盘上
      </p>
      <ul class="mt-1 space-y-0.5">
        <li v-for="row in missing.slice(0, 8)" :key="row.id" class="text-fg-4 truncate text-2xs">
          <span class="text-fg-3">{{ row.kind }}</span>
          <span class="ml-1 font-mono">{{ row.path }}</span>
        </li>
      </ul>
      <p v-if="missing.length > 8" class="text-fg-4 mt-0.5 text-2xs">
        …还有 {{ missing.length - 8 }} 条
      </p>
      <p class="text-fg-4 mt-1 text-2xs">
        库里那几行照旧带走，但这个包到了另一台机器上仍然会缺这几张图。
      </p>
    </div>

    <!-- 环境要求 vs 本机：只报告，不拦 -->
    <div v-if="envCheck" class="border-line-1 bg-base-2 border p-2">
      <p class="text-fg-2 text-2xs">这个包需要的环境（与本机比对）</p>
      <ul class="mt-1 space-y-1">
        <li v-for="row in envCheck.presets" :key="`${row.role}-${row.name}`" class="text-2xs">
          <span class="text-fg-3">{{ row.label }}</span>
          <span class="text-fg-1 ml-1 font-mono">{{ row.name }}</span>
          <AppBadge v-if="!row.present" tone="fail" class="ml-1">本机没有</AppBadge>
          <AppBadge v-else-if="!row.ready" tone="warn" class="ml-1">不能用于此角色</AppBadge>
          <AppBadge v-else tone="ok" class="ml-1">已就绪</AppBadge>
          <p v-if="row.impact" class="text-fg-4 leading-tight">{{ row.impact }}</p>
          <p v-if="row.markers.length > 0" class="text-fg-4 leading-tight">
            要一份标了这几个入口的图：
            <span class="font-mono">{{ row.markers.join(' · ') }}</span>
          </p>
        </li>
      </ul>
      <div class="text-2xs mt-1.5 space-y-0.5">
        <p class="text-fg-4">
          视频服务：包要
          <span class="text-fg-2">{{ envCheck.video_provider.wanted || '—' }}</span> ，本机是
          <span class="text-fg-2">{{ envCheck.video_provider.current }}</span>
          <AppBadge v-if="!envCheck.video_provider.matches" tone="warn" class="ml-1"
            >不一致</AppBadge
          >
        </p>
        <p class="text-fg-4">
          FFmpeg：
          <span :class="envCheck.ffmpeg.present ? 'text-st-done' : 'text-st-failed'">
            {{ envCheck.ffmpeg.present ? `可用（${envCheck.ffmpeg.source}）` : '未找到' }}
          </span>
        </p>
        <p class="text-fg-4">
          schema：包是 <span class="tnum text-fg-2">{{ envCheck.schema.wanted }}</span> ，本机支持到
          <span class="tnum text-fg-2">{{ envCheck.schema.current }}</span>
          <AppBadge v-if="!envCheck.schema.ok" tone="fail" class="ml-1">这个包比本机新</AppBadge>
        </p>
      </div>
      <p class="text-fg-4 mt-1 text-2xs">
        包里只带「要一份标了这几个入口的图」这份清单，不带预设图本身——那份图属于「我这台机器怎么调模型」。
      </p>
    </div>
  </div>
</template>
