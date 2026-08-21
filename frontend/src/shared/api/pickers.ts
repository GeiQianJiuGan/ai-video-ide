/**
 * 「可挑的形象 / 地点变体」两张清单。
 *
 * 挂人物、挑地点这件事在两级页面上都要做（幕流程图的小节点、场景工作台的首帧），
 * 名字怎么拼（`角色 · 形象`、`地点 · 变体`）**和图从哪来**都只该有一处口径——
 * 所以整张清单由后端一次给全（`GET /projects/{pid}/scene-node-options`），
 * 前端不再按角色一个个拉形象、再按变体一个个拉参考图（那是两轮 N+1）。
 *
 * 它**吞掉失败**返回空清单：这是选项清单，拉不到不该让整页报错——
 * 真正会被拒绝的是保存那一步（超上限 / 形象不存在），那里的四要素错误照常显示。
 */

import { api } from './client'

/** 可挑的形象：角色名 + 形象名拼好，页面不再自己查两张表。 */
export interface CastOption {
  appearance_id: string
  character_id: string | null
  character_name: string | null
  appearance_name: string
  label: string
  /** 这个角色的默认形象——镜头不指定形象时用的就是它。 */
  is_default: boolean
  /** 没有角色表图的形象也能挂，只是生成时喂不出参考图——界面上要标出来。 */
  has_sheet: boolean
  /** 当前角色表那张图，相对工程目录；`fileUrl(pid, path)` 才是能给 `<img>` 的 URL。 */
  thumbnail_path: string | null
}

/** 可挑的地点变体：「城南旧宅 · 雨夜」。 */
export interface VariantOption {
  id: string
  location_id: string
  variant_name: string
  label: string
  /** 变体的参考图，同 `CastOption.thumbnail_path` 的口径。 */
  thumbnail_path: string | null
}

export interface NodeOptions {
  cast: CastOption[]
  locations: VariantOption[]
  /** 人物 / 地点各自的上限，运行期可配（设置页 `scene.node_limit`）。 */
  node_limit: number
  /** 上限怎么改；和后端拒绝时给的建议是同一句话。 */
  limit_hint: string
}

const EMPTY: NodeOptions = { cast: [], locations: [], node_limit: 9, limit_hint: '' }

export async function loadNodeOptions(pid: string): Promise<NodeOptions> {
  return api.get<NodeOptions>(`/projects/${pid}/scene-node-options`).catch(() => EMPTY)
}
