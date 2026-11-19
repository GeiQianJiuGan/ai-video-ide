import type { FrameCard, FrameVideo } from '@/shared/api/frames'

export interface FrameInputCheck {
  key: 'prompt' | 'first' | 'last' | 'duration'
  label: string
  ready: boolean
  detail: string
}

type FrameAsset = { id: string; missing: boolean }

function imageProgress(card: FrameCard): string {
  if (card.candidates.length)
    return '图片已生成或导入，但尚未采用；请到「制作图片」审阅并点击「采用此图」。'
  if (card.jobs.some((job) => ['queued', 'waiting', 'running'].includes(job.status)))
    return '图片正在生成或排队；完成后请到「制作图片」审阅并采用。'
  if (card.jobs.some((job) => job.status === 'failed'))
    return '图片生成失败；请在任务面板查看错误，重试后审阅并采用。'
  if (card.refs.length) return '当前只有制图参考；请先生成图片并采用，参考图不会自动成为视频首帧。'
  return '尚未制作并采用图片；请到「制作图片」生成或导入候选，再点击「采用此图」。'
}

export function frameVideoChecks(
  video: FrameVideo,
  cards: FrameCard[],
  assets?: FrameAsset[],
): FrameInputCheck[] {
  const ownPrompt = video.prompt.trim()
  const sharedPrompt = video.shared_prompt?.trim() || ''
  function checkImage(key: 'first' | 'last', cardId: string): FrameInputCheck {
    const label = key === 'first' ? '首帧图片' : '尾帧图片（可选）'
    const result = (ready: boolean, detail: string) => ({ key, label, ready, detail })
    if (!cardId) {
      if (key === 'last') return result(true, '仅使用首帧，无需尾帧。')
      if (!cards.length) return result(false, '本镜头还没有画面；请先到「制作图片」添加画面。')
      const adopted = cards.filter((card) => card.adopted_asset_id)
      if (adopted.length)
        return result(false, `已有 ${adopted.length} 张已采用图片，请在下方「首帧」选择一张。`)
      const pending =
        cards.find((card) => card.candidates.length) ||
        cards.find((card) =>
          card.jobs.some((job) => ['queued', 'waiting', 'running'].includes(job.status)),
        ) ||
        cards[0]!
      return result(false, imageProgress(pending))
    }
    const card = cards.find((item) => item.id === cardId)
    if (!card) return result(false, '所选画面已移除或不属于本镜头，请重新选择。')
    if (!card.adopted_asset_id) return result(false, `「${card.title}」：${imageProgress(card)}`)
    if (assets && !assets.some((asset) => asset.id === card.adopted_asset_id && !asset.missing))
      return result(false, `「${card.title}」已采用的图片文件不可用，请恢复文件或采用其他候选。`)
    return result(true, `已选择并采用「${card.title}」。`)
  }
  const durationOk = Number.isFinite(video.duration) && video.duration > 0 && video.duration <= 60
  return [
    {
      key: 'prompt',
      label: '视频提示词',
      ready: !!(ownPrompt || sharedPrompt),
      detail: ownPrompt
        ? '已填写本镜头的视频提示词。'
        : sharedPrompt
          ? '使用本幕共用视频提示词。'
          : '请填写本镜头的视频提示词，或设置本幕共用视频提示词；图片提示词用于制图。',
    },
    checkImage('first', video.first_card_id),
    checkImage('last', video.last_card_id),
    {
      key: 'duration',
      label: '视频时长',
      ready: durationOk,
      detail: durationOk ? `${video.duration} 秒。` : '视频时长须大于 0 且不超过 60 秒。',
    },
  ]
}

export function frameCardImageState(card: FrameCard): string {
  if (card.adopted_asset_id) return '已采用'
  if (card.candidates.length) return '有候选，未采用'
  if (card.jobs.some((job) => ['queued', 'waiting', 'running'].includes(job.status)))
    return '生成中'
  if (card.refs.length) return '仅有参考图，未制作并采用'
  return '未制作并采用'
}
