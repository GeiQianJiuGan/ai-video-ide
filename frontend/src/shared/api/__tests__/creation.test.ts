import { describe, expect, it } from 'vitest'
import { inferCreationGoal } from '../../../app/creationGoal'

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
