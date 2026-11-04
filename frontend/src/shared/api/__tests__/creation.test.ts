import { describe, expect, it } from 'vitest'
import {
  inferCreationGoal,
  resolveComposerGoal,
  canSubmitComposer,
} from '../../../app/creationGoal'

describe('creation goal routing', () => {
  it('finishes a requested script', () => {
    expect(inferCreationGoal('帮我制作一个雨夜来信的剧本')).toBe('screenplay')
  })
  it('plans the complete storyboard when requested', () => {
    expect(inferCreationGoal('把这个故事拆成幕和分镜')).toBe('storyboard')
  })
  it('keeps local edits in collaboration mode', () => {
    expect(inferCreationGoal('修改剧本第2幕的对白')).toBe('chat')
    expect(inferCreationGoal('检查现有剧本的连续性')).toBe('chat')
  })
  it('allows an explicitly complete rewrite', () => {
    expect(inferCreationGoal('修改并补全完整剧本')).toBe('screenplay')
  })
})

describe('unified composer', () => {
  it('uses explicit goals even when the text suggests another workflow', () => {
    expect(resolveComposerGoal('chat', '讨论完整分镜应该怎么写')).toBe('chat')
    expect(resolveComposerGoal('storyboard', '雨夜来信')).toBe('storyboard')
    expect(resolveComposerGoal('auto', '把这个故事拆成幕和分镜')).toBe('storyboard')
  })
  it('preserves creation from existing project content without allowing empty chat', () => {
    expect(canSubmitComposer('storyboard', '', 0)).toBe(true)
    expect(canSubmitComposer('screenplay', '  ', 0)).toBe(true)
    expect(canSubmitComposer('auto', '', 0)).toBe(false)
    expect(canSubmitComposer('chat', '  ', 0)).toBe(false)
  })
  it('allows attachment-only messages through the same entry', () => {
    expect(canSubmitComposer('auto', '', 1)).toBe(true)
    expect(resolveComposerGoal('screenplay', '')).toBe('screenplay')
  })
})
