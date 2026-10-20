import { describe, expect, it } from 'vitest'
import type { ContextItem } from '@/shared/api/generation'
import { inputMode, referencesOf, unresolvedMentions } from '../shotPrompt'

function item(id: string, patch: Partial<ContextItem> = {}): ContextItem {
  return {
    key: id,
    kind: 'manual',
    asset_id: id,
    label: id,
    source_id: null,
    priority: 100,
    reason: '手动连接',
    included: true,
    role: 'reference',
    media: 'image',
    manual: true,
    asset_path: `${id}.png`,
    missing_file: false,
    ...patch,
  }
}

describe('镜头素材链接', () => {
  it('重新排序和删除素材后仍保留已引用素材的别名，且不复用失效别名', () => {
    const prompt = '@[图片1](asset:a) 与 @[图片2](asset:b)'
    const refs = referencesOf([item('c'), item('b')], prompt)
    expect(refs.find((r) => r.target === 'asset:b')?.alias).toBe('图片2')
    expect(refs.find((r) => r.target === 'asset:c')?.alias).toBe('图片3')
    expect(unresolvedMentions(prompt, refs)).toEqual(['图片1'])
  })
  it('首尾帧不占编辑器参考素材别名，并且图片、音视频分别编号', () => {
    const refs = referencesOf([
      item('first', { role: 'first_frame' }),
      item('a'),
      item('audio', { media: 'audio' }),
    ])
    expect(refs.map((r) => r.alias)).toEqual(['首帧', '图片1', '音频1'])
  })
  it('素材移到帧槽位后，原有资产链接仍然有效', () => {
    expect(
      unresolvedMentions('@[图片1](asset:a)', referencesOf([item('a', { role: 'first_frame' })])),
    ).toEqual([])
  })
  it('未采用、文件缺失和超容量不能作为有效引用', () => {
    const refs = referencesOf([
      item('a', { included: false }),
      item('b', { missing_file: true }),
      item('c', { over_capacity: true }),
    ])
    expect(
      unresolvedMentions('@[图片1](asset:a) @[图片2](asset:b) @[图片3](asset:c)', refs),
    ).toEqual(['图片1', '图片2', '图片3'])
  })
  it('根据素材形态提供相应构筑引导', () => {
    expect(inputMode(true, true, 0)).toContain('FL2VA')
    expect(inputMode(false, true, 0)).toContain('L2VA')
    expect(inputMode(true, true, 2)).toContain('Ref2VA')
  })
})
