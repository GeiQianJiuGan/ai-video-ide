import type { CreationGoal } from '@/shared/api/creation'

export function inferCreationGoal(text: string): CreationGoal | 'chat' {
  // Local edits should keep using the existing collaboration/review tools.
  if (
    /修改|调整|删除|改成|检查|第[\d一二三四五六七八九十]+幕/.test(text) &&
    !/完整|全部|整部|整篇/.test(text)
  )
    return 'chat'
  if (/分镜|拆.*幕|全流程/.test(text)) return 'storyboard'
  if (/剧本|故事|短片|短剧|创作/.test(text)) return 'screenplay'
  return 'chat'
}

/** 统一发送入口：显式目标优先；空输入仅在显式完整创作时使用项目现有内容。 */
export type ComposerGoal = CreationGoal | 'chat' | 'auto'
export function resolveComposerGoal(selection: ComposerGoal, text: string): CreationGoal | 'chat' {
  return selection === 'auto' ? inferCreationGoal(text.trim()) : selection
}
export function canSubmitComposer(
  selection: ComposerGoal,
  text: string,
  attachments: number,
): boolean {
  return (
    Boolean(text.trim()) ||
    attachments > 0 ||
    selection === 'screenplay' ||
    selection === 'storyboard'
  )
}
