<script setup lang="ts">
/**
 * 流程图上的一个节点 = 一幕，本身就是一张小图表。
 *
 * 四个刻意的设计：
 *   1. **没出片就明说「暂无已生成视频」**，出片了就给一个真能播的 `<video controls>`。
 *      两个字段不能混：`video_path` 才是视频，`thumbnail_path` 只会是图片——
 *      把 `.mp4` 喂给 `<img>` 只会得到一个坏图标。
 *   2. **小节点画成挂在节点下面的一串**：prompt（必填）、人物、地点。
 *      prompt 没写就标黄——它是唯一必填的那个，缺了这一幕根本生不出东西。
 *   3. **单击选中、双击进第二级**。视频上的双击留给播放器（原生是全屏），
 *      所以那一块显式 `@dblclick.stop`，不然拖进度条会莫名跳页。
 *   4. **计数一律写成 `N/上限`**：上限是后端给的（`node_limit`，运行期可配），
 *      前端不写死 9，也不自己判断超没超——真正的守卫在后端。
 *   5. **人物 / 地点小节点带图**：前两个是「图 + 名字」，再往后只画头像（卡片只有 224px 宽，
 *      名字排不下），鼠标停住能看到是谁。图从 `thumbnail_path` 来，后端保证只会是图片。
 */
import { computed } from 'vue'
import { Film, MapPin, Trash2, Type, Users } from '@lucide/vue'
import AppBadge from '@/shared/ui/AppBadge.vue'
import AppThumb from '@/shared/ui/AppThumb.vue'
import { fileUrl } from '@/shared/api/files'
import type { FlowNode } from '@/shared/api/sequence'

const props = defineProps<{
  pid: string
  node: FlowNode
  selected: boolean
}>()

const emit = defineEmits<{
  select: []
  open: []
  remove: []
}>()

const videoSrc = computed(() =>
  props.node.video_path ? fileUrl(props.pid, props.node.video_path) : '',
)
/** 视频的封面图只能是图片资产；没有就不给 poster，让播放器自己抽第一帧。 */
const poster = computed(() =>
  props.node.thumbnail_path ? fileUrl(props.pid, props.node.thumbnail_path) : undefined,
)
const promptText = computed(() => (props.node.prompt ?? '').trim())
</script>

<template>
  <div
    class="w-56 shrink-0 border p-1.5"
    :class="selected ? 'border-accent/60 bg-accent-dim/30' : 'border-line-1 bg-base-2'"
    :title="selected ? '双击进这一幕的工作台' : '单击选中，双击进这一幕的工作台'"
    @click="emit('select')"
    @dblclick="emit('open')"
  >
    <div class="flex items-center gap-1">
      <span class="text-fg-4 tnum text-2xs">{{ node.index_no }}</span>
      <span class="text-fg-1 min-w-0 flex-1 truncate text-2xs" :title="node.title">
        {{ node.title }}
      </span>
      <button
        class="text-fg-4 hover:text-st-failed"
        title="删掉这一幕（它的镜头与版本会一起没）"
        @click.stop="emit('remove')"
      >
        <Trash2 :size="10" />
      </button>
    </div>

    <!-- 成片：能播的那一段，或者一句明确的「暂无」 -->
    <div
      class="bg-base-3 mt-1 flex h-28 items-center justify-center overflow-hidden"
      @dblclick.stop
    >
      <video
        v-if="node.has_video && videoSrc"
        :key="node.video_version_id ?? ''"
        :src="videoSrc"
        :poster="poster"
        controls
        preload="metadata"
        class="max-h-full max-w-full"
      />
      <div v-else class="px-2 text-center">
        <Film :size="14" class="text-fg-4 mx-auto" />
        <p class="text-fg-4 mt-0.5 text-2xs">暂无已生成视频</p>
        <p class="text-fg-4 text-2xs">选中它，在右边挑一段或去工作台生成</p>
      </div>
    </div>

    <div class="mt-1 flex flex-wrap items-center gap-1">
      <AppBadge tone="neutral">{{ node.generated_count }}/{{ node.shot_count }} 镜头</AppBadge>
      <AppBadge
        v-if="node.video_count"
        :tone="node.video_adopted ? 'ok' : 'accent'"
        :title="
          node.video_adopted
            ? '这一段是你采用的主视频'
            : '这一幕有可播的视频，但还没采用哪一段当主视频——节点上播的是自动挑的那一段'
        "
      >
        {{ node.video_adopted ? '已采用主视频' : `${node.video_count} 段可选` }}
      </AppBadge>
      <AppBadge v-if="node.transition_count" tone="accent">
        转场 {{ node.transition_count }}
      </AppBadge>
      <AppBadge v-if="node.issues.length" tone="warn" :title="node.issues.join('；')">
        {{ node.issues.length }} 个问题
      </AppBadge>
    </div>

    <!-- 小节点：prompt 必填，人物 / 地点可多选也可以一个都不选 -->
    <div class="mt-1.5 flex flex-col items-center">
      <div class="border-line-2 h-2 border-l"></div>
      <div class="border-line-2 w-full border-t"></div>
    </div>
    <div class="mt-1 space-y-1">
      <AppBadge
        :tone="node.prompt_ok ? 'ok' : 'warn'"
        class="max-w-full"
        :title="node.prompt_ok ? promptText : 'prompt 是这一幕唯一必填的小节点'"
      >
        <Type :size="9" />
        <span class="min-w-0 truncate">{{ node.prompt_ok ? promptText : 'prompt 还没写' }}</span>
      </AppBadge>
      <div class="flex flex-wrap items-center gap-1">
        <AppBadge tone="neutral" title="这一幕出场的人物小节点">
          <Users :size="9" />{{ node.cast.length }}/{{ node.node_limit }}
        </AppBadge>
        <AppBadge
          v-for="c in node.cast.slice(0, 2)"
          :key="c.id"
          tone="neutral"
          class="max-w-24"
          :title="c.label"
        >
          <AppThumb :pid="pid" :path="c.thumbnail_path" size="xs" />
          <span class="min-w-0 truncate">{{ c.label }}</span>
        </AppBadge>
        <AppThumb
          v-for="c in node.cast.slice(2, 5)"
          :key="`t-${c.id}`"
          :pid="pid"
          :path="c.thumbnail_path"
          :label="c.label"
          size="xs"
        />
        <span v-if="node.cast.length > 5" class="text-fg-4 text-2xs">
          +{{ node.cast.length - 5 }}
        </span>
      </div>
      <div class="flex flex-wrap items-center gap-1">
        <AppBadge tone="neutral" title="这一幕的地点小节点；第一条同时是主地点">
          <MapPin :size="9" />{{ node.locations.length }}/{{ node.node_limit }}
        </AppBadge>
        <AppBadge
          v-for="l in node.locations.slice(0, 2)"
          :key="l.id"
          :tone="l.is_primary ? 'accent' : 'neutral'"
          class="max-w-24"
          :title="l.is_primary ? `${l.label}（主地点）` : l.label"
        >
          <AppThumb :pid="pid" :path="l.thumbnail_path" size="xs" />
          <span class="min-w-0 truncate">{{ l.label }}</span>
        </AppBadge>
        <AppThumb
          v-for="l in node.locations.slice(2, 5)"
          :key="`t-${l.id}`"
          :pid="pid"
          :path="l.thumbnail_path"
          :label="l.label"
          size="xs"
        />
        <span v-if="node.locations.length > 5" class="text-fg-4 text-2xs">
          +{{ node.locations.length - 5 }}
        </span>
      </div>
    </div>
  </div>
</template>
