/**
 * 「这一份预设当哪一种默认」——**前端只有这一份口径**。
 *
 * 应用级默认有四项，光看设置键看不出它们的关系：
 *
 *     video.r2v_preset   普通镜头（图生视频）
 *     video.flf_preset   衔接与转场（首尾帧 / FL2VA）
 *     video.preset       上面两项留空时共用的那一份
 *     image.preset       出图（角色四视图 / 地点参考图 / 道具图 / 首末帧候选）
 *
 * 解析顺序在后端只有一份（`services/route.py::app_preset_of`：按角色那一项 → 共用那一项；
 * 工程那三列为空就跟着它，见 `preset_name_of`）。这里只负责说「这一份图能不能当这个角色」
 * 与「为什么不能」，**就绪判断一条都不自己算**：`r2v_ready` / `flf_ready` / `prompt_ok` /
 * `declares_image` 全部读后端给的（`providers/presets.py::inspect`）——前端另算一遍，
 * 必然和预设列表上那几个徽标说的不一致。
 *
 * 两处照它画：最外层的「预设 Workflow」页与设置页里那份同样的清单。以前两处各写一份按钮，
 * 于是应用级默认从一格变成三格之后必然漂移，所以两个共用件
 * （`shared/ui/PresetDefaultBadges.vue` / `PresetDefaultButtons.vue`）都只读这张表。
 */

import type { PresetRow } from '@/shared/api/settings'

/**
 * 出图那条链的调用方式**不认预设**时（`none` / 云端 API，协议表里 `wants_preset=false`）
 * 要说的那句话。
 *
 * 这时候按钮**照旧可用**：装出来默认就是 `image.provider=none`，一按 `wants_preset` 就把按钮
 * 藏起来的话，「设置默认出图预设」这件事在界面上根本没有入口——用户得先去设置页换协议再回来，
 * 而那正是这次要修的毛病。指一份暂时用不上的预设坏不了任何事，但必须说出来（硬约束 4）。
 */
export const IMAGE_PRESET_INERT =
  '出图那条链现在的调用方式不认预设，指了也暂时用不上——把设置页「图片生成 API」的调用方式改成本机 ComfyUI 预设后才生效。'

/** 画按钮时要知道的、与某一份图无关的事实。 */
export interface PresetDefaultContext {
  /** 出图协议认不认预设（协议表里的 `wants_preset`）。 */
  imageWantsPreset: boolean
}

/** 一种应用级默认。`column` 与「预设 Workflow」页那两栏同名，所以那一页不用另配一张表。 */
export interface PresetDefaultRole {
  /** 设置键（`GET /settings` 里的那个 key）。 */
  key: string
  column: 'video' | 'image'
  /** 这一份图正是这个默认时贴的徽标。 */
  badge: string
  /** 按钮文字。 */
  action: string
  /** 取消这项默认之后会发生什么。**取消得说清后果**，不然用户只是按掉了一个不认识的开关。 */
  cleared: string
  /** 这一份图能不能当这个角色。false 时按钮禁用，理由进 `title`。 */
  ready: (row: PresetRow) => boolean
  /** tooltip：能当时说它管什么，不能当时说为什么不能——不拿一个灰按钮了事。 */
  title: (row: PresetRow, ctx: PresetDefaultContext) => string
}

/** 「这份图是出图那一份」——三个视频角色共用同一句拒绝理由。 */
function declaresImage(which: string): string {
  return `这份图标了 AIVS_IMAGE，是出图那一份——${which}请另选一份没标它的图`
}

export const PRESET_DEFAULT_ROLES: PresetDefaultRole[] = [
  {
    key: 'video.r2v_preset',
    column: 'video',
    badge: 'R2V 默认',
    action: '设为 R2V 默认',
    cleared: '取消后普通镜头退回下面那份共用默认',
    ready: (row) => Boolean(row.r2v_ready),
    title: (row) =>
      row.declares_image
        ? declaresImage('普通镜头')
        : row.r2v_ready
          ? '工程没有单独绑预设时，普通镜头（图生视频）按这一份出'
          : '这份图里没有 AIVS_PROMPT，本工具没法告诉它要生成什么',
  },
  {
    key: 'video.flf_preset',
    column: 'video',
    badge: '首尾帧默认',
    action: '设为首尾帧默认',
    cleared: '取消后首尾帧 / 转场退回下面那份共用默认',
    ready: (row) => Boolean(row.flf_ready),
    title: (row) =>
      row.declares_image
        ? declaresImage('衔接与转场')
        : row.flf_ready
          ? '工程没有单独绑预设时，首尾帧 / 转场 / FL2VA 按这一份出'
          : '首尾帧那份图必须同时标出 AIVS_PROMPT、AIVS_FIRST_FRAME、AIVS_LAST_FRAME',
  },
  {
    key: 'video.preset',
    column: 'video',
    badge: '共用默认',
    action: '设为共用默认',
    cleared: '取消后没有共用默认，上面两项留空的角色按下生成会报「还没有选生成预设」',
    ready: (row) => Boolean(row.ready) && !row.declares_image,
    title: (row) =>
      row.declares_image
        ? declaresImage('出画面')
        : row.ready
          ? '上面两项留空的角色按这一份出（只有一份图的人只配它就够）'
          : (row.impact ?? '这份图里填不进任何东西'),
  },
  {
    key: 'image.preset',
    column: 'image',
    badge: '出图默认',
    action: '设为出图默认',
    cleared: '取消后没有默认出图预设，角色四视图 / 地点参考图只能手动上传',
    // **判据是 `prompt_ok` 而不是 `t2i_ready`**：没标 AIVS_IMAGE 的图照旧能当出图预设用
    // （否则每一台升级前就配好出图预设的机器当场坏掉），只是它同时还留在视频那两栏的
    // 候选里——这一句写进 tooltip，不拿禁用来说。
    ready: (row) => Boolean(row.prompt_ok),
    title: (row, ctx) => {
      const base = !row.prompt_ok
        ? '这份图里没有 AIVS_PROMPT，本工具没法告诉它要画什么'
        : row.declares_image
          ? '把这份图当出图用的那一份（角色四视图 / 地点参考图 / 道具图走它）'
          : '可以用，但这份图没标 AIVS_IMAGE，它同时还留在 R2V / 首尾帧的候选里——给它加上这个标题就只归「出图」那一栏'
      return ctx.imageWantsPreset ? base : `${base}。${IMAGE_PRESET_INERT}`
    },
  },
]

/** 某一栏要画哪几种默认。不给 `column` = 全部（设置页那份清单没有分栏）。 */
export function presetDefaultRoles(column?: string): PresetDefaultRole[] {
  if (!column) return PRESET_DEFAULT_ROLES
  return PRESET_DEFAULT_ROLES.filter((role) => role.column === column)
}
