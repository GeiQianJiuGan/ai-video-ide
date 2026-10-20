import type { ContextItem } from '@/shared/api/generation'

export interface PromptReference {
  target: string
  alias: string
  label: string
  path: string | null
  media: string
  valid: boolean
  reason: string
  key: string
  assetId: string | null
}

// 文本存稳定 ID，显示名只是别名；模型编号由后端实际图册决定。
export const mentionPattern = () => /@\[([^\]\n]+)\]\((asset|frame):([^)\s]+)\)/g
export const mentionText = (r: PromptReference) => `@[${r.alias}](${r.target})`

export function referencesOf(items: ContextItem[], prompt = ''): PromptReference[] {
  const counts: Record<string, number> = {}
  const seen = new Set<string>()
  const aliases = new Map(
    [...prompt.matchAll(mentionPattern())].map((m) => [`${m[2]}:${m[3]}`, m[1]!]),
  )
  const usedAliases = new Set(aliases.values())
  return items
    .filter((i) => i.included)
    .flatMap((item) => {
      const frame = item.role === 'first_frame' || item.role === 'last_frame'
      if (!frame && !item.asset_id) return []
      const target = frame ? `frame:${item.role}` : `asset:${item.asset_id}`
      if (seen.has(target)) return []
      seen.add(target)
      const media = item.media ?? 'image'
      const prefix = { image: '图片', video: '视频', audio: '音频' }[media] ?? '素材'
      let alias = aliases.get(target)
      if (!alias) {
        if (frame) alias = item.role === 'first_frame' ? '首帧' : '尾帧'
        else {
          do {
            counts[media] = (counts[media] ?? 0) + 1
            alias = `${prefix}${counts[media]}`
          } while (usedAliases.has(alias))
        }
      }
      usedAliases.add(alias)
      return [
        {
          target,
          alias,
          label: item.label,
          path: item.asset_path,
          media,
          valid: !item.missing_file && !item.over_capacity && Boolean(item.asset_id),
          reason: item.over_capacity
            ? '超出工作流容量'
            : item.missing_file
              ? '文件缺失'
              : item.reason,
          key: item.key,
          assetId: item.asset_id,
        },
      ]
    })
}

export function unresolvedMentions(text: string, references: PromptReference[]): string[] {
  return [...text.matchAll(mentionPattern())]
    .filter((m) => !lookupReference(references, `${m[2]}:${m[3]}`)?.valid)
    .map((m) => m[1]!)
}

export function lookupReference(
  references: PromptReference[],
  target: string,
): PromptReference | undefined {
  const exact = references.find((r) => r.target === target)
  if (exact?.valid) return exact
  return (
    references.find(
      (r) => r.valid && target.startsWith('asset:') && r.assetId === target.slice(6),
    ) ?? exact
  )
}

export function inputMode(first: boolean, last: boolean, references: number): string {
  if (references) return 'Ref2VA · 全参考'
  if (first && last) return 'FL2VA · 首尾帧'
  if (first) return 'I2VA · 首帧'
  if (last) return 'L2VA · 尾帧'
  return 'T2VA · 文字'
}

export const promptStages = [
  {
    title: '素材与主体',
    hint: '引用谁？保留外观、服装、场景还是动作？输入 @ 选择已连接素材。',
    template: '主体采用「引用素材」中的外观与服装，保留……。',
  },
  {
    title: '起始画面',
    hint: '开场时人物在哪里？景别、光线和空间关系是什么？',
    template: '开场为……景别，主体位于画面……，环境与光线为……。',
  },
  {
    title: '动作发展',
    hint: '按发生顺序写出可观察的动作、反应和状态变化。',
    template: '0–2 秒，主体……；随后……；最后……。',
  },
  {
    title: '运镜与收尾',
    hint: '镜头如何移动？有尾帧时，描述动作如何连续到达它的姿态与构图。',
    template: '镜头从……缓慢……，主体逐渐……，最终停在……。',
  },
  {
    title: '声音',
    hint: '填写原文对白、环境声和动作音效；当前工程不添加背景配乐。',
    template: '环境声：……。动作音效：……。对白原文：……。无背景配乐。',
  },
  {
    title: '检查',
    hint: '检查失效引用、时长和首尾帧过渡；下方可查看当前预览版本的实际提交记录。',
    template: '',
  },
] as const
