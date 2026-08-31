/**
 * 「照着这张素材写一句描述」接口。
 *
 * 字段与后端 app/api/describe.py + services/describe.py 一一对应。
 *
 * 三个形状上的要点：
 *   1. **两头都不落库**——`plan()` 是账单（用哪个端、能不能真看图、这几张现在有没有
 *      描述、缺什么），`suggest()` 回的也只是建议文字。落库只有一条路：用户按保存 →
 *      `assetsApi.update`。界面上这句话要写出来，别让人以为点一次就存了。
 *   2. **`desc_max` 由后端给**（截断规则只在 `providers/base.py::ref_hint` 那一处），
 *      前端的字数提示照它显示，不写死第二份。
 *   3. **`source` 要显示给用户看**：`vision` 是真看了图，`text` 只是按名字与实体设定
 *      编的——可信度不一样，用户得知道该不该信它。
 */

import { api } from './client'
import type { ErrorPayload } from './client'
import type { LlmStatus } from './settings'

/** 这一句是怎么来的。`skipped` = 压根没送出去（视频 / 音频 / 图太大 / 读不到）。 */
export type DescribeSource = 'vision' | 'text' | 'skipped'

export const DESCRIBE_SOURCE_LABEL: Record<DescribeSource, string> = {
  vision: '看图写的',
  text: '只按名字写的',
  skipped: '已跳过',
}

/** 账单里的一条素材。`mode` 说的是「点下去之后会不会真的送字节出去」。 */
export interface DescribePlanItem {
  asset_id: string
  /** 短标签（「阿岚（默认形象）」这种），不塞描述。 */
  label: string
  /** 相对工程目录的路径，交给 fileUrl(pid, path) 变成缩略图。 */
  path: string
  media: 'image' | 'video' | 'audio' | string
  mime: string | null
  /** 库里现在那一句（可能为空）。 */
  description: string | null
  has_description: boolean
  /** 它挂在谁身上（「角色 · 阿岚」这种），由后端说，前端不反查。 */
  owner_hint: string
  /** 这个实体自己的设定文字：端看不到图时模型只能靠它写。 */
  setting: string
  mode: 'vision' | 'text'
  /** 真的不做这一条（视频 / 音频 / 文件不在 / 太大）。原因在 `warnings` 里。 */
  skipped: boolean
  warnings: string[]
}

/** 账单。`can_run=false` 时把 `missing[]` 的四要素错误连 suggestions 一起摆出来。 */
export interface DescribePlan {
  items: DescribePlanItem[]
  count: number
  /** 真会送出去字节的有几张。0 而 count > 0 = 只按名字写，界面要说清。 */
  vision_count: number
  skipped_count: number
  llm: LlmStatus
  desc_max: number
  note: string
  missing: ErrorPayload[]
  can_run: boolean
}

/** 一条建议。**没有写进库**——界面只把它填进输入框。 */
export interface DescribeSuggestion {
  asset_id: string
  label: string
  path: string
  media: string
  /** 库里现在那一句，用来和建议对照着看。 */
  description: string | null
  suggestion: string
  source: DescribeSource
  warnings: string[]
  /** 这一条失败了（其余几条照旧）。照 suggestions 提示，不吞。 */
  error: ErrorPayload | null
}

export interface DescribeSuggestions {
  items: DescribeSuggestion[]
  count: number
  ok_count: number
  desc_max: number
  note: string
}

export const describeApi = {
  /** 先账单：端没配好、端不认图、图太大送不出去，点之前就知道。 */
  plan: (pid: string, assetIds: string[]) =>
    api.post<DescribePlan>(`/projects/${pid}/describe/plan`, { asset_ids: assetIds }),
  /** 出建议。**不落库**，回来的文字只填进输入框，保存仍然是用户的一下。 */
  suggest: (pid: string, assetIds: string[]) =>
    api.post<DescribeSuggestions>(`/projects/${pid}/describe/suggest`, { asset_ids: assetIds }),
}
