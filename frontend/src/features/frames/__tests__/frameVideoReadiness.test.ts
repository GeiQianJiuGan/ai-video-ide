import { describe, expect, it } from 'vitest'
import type { FrameCard, FrameVideo } from '@/shared/api/frames'
import { frameCardImageState, frameVideoChecks } from '../frameVideoReadiness'

const card = (patch: Partial<FrameCard> = {}): FrameCard => ({
  id: 'first',
  title: '开场',
  prompt: '',
  story: '',
  role: 'first',
  position: 0,
  refs: [],
  adopted_asset_id: 'image',
  candidates: [],
  jobs: [],
  ...patch,
})
const video = (patch: Partial<FrameVideo> = {}): FrameVideo => ({
  prompt: 'Camera moves forward.',
  duration: 5,
  first_card_id: 'first',
  last_card_id: '',
  ...patch,
})
const first = (checks: ReturnType<typeof frameVideoChecks>) =>
  checks.find((item) => item.key === 'first')!

describe('制帧生成条件及缺项说明', () => {
  it('仅首帧即可生成，尾帧是可选项', () => {
    expect(
      frameVideoChecks(video(), [card()], [{ id: 'image', missing: false }]).every(
        (item) => item.ready,
      ),
    ).toBe(true)
  })
  it('视频描述可继承幕级提示词，纯空白提示词仍然缺失', () => {
    const shared = frameVideoChecks(video({ prompt: '', shared_prompt: 'Move slowly.' }), [card()])
    expect(shared[0]).toMatchObject({ ready: true, detail: '使用本幕共用视频提示词。' })
    expect(frameVideoChecks(video({ prompt: ' ', shared_prompt: '  ' }), [card()])[0]?.ready).toBe(
      false,
    )
  })
  it('源视频抽帧作为参考并不意味着已制作和采用图片', () => {
    const reference = card({ adopted_asset_id: null, refs: ['source-frame'] })
    const state = first(frameVideoChecks(video({ first_card_id: '' }), [reference]))
    expect(state.ready).toBe(false)
    expect(state.detail).toContain('当前只有制图参考')
    expect(frameCardImageState(reference)).toContain('仅有参考图')
  })
  it('生成或导入候选后必须明确采用', () => {
    const candidate = card({
      adopted_asset_id: null,
      candidates: [{ id: 'candidate', asset_id: 'image', job_id: null, snapshot: {} }],
    })
    expect(first(frameVideoChecks(video(), [candidate]))).toMatchObject({ ready: false })
    expect(first(frameVideoChecks(video(), [candidate])).detail).toContain('采用此图')
    expect(frameCardImageState(candidate)).toBe('有候选，未采用')
  })
  it('有采用图片但未选择首帧时，明确要求选择', () => {
    expect(first(frameVideoChecks(video({ first_card_id: '' }), [card()])).detail).toContain(
      '下方「首帧」选择',
    )
  })
  it('说明尚未完成的图片任务，避免误认为已出图', () => {
    const pending = card({
      adopted_asset_id: null,
      jobs: [{ id: 'job', status: 'running', error_json: null }],
    })
    expect(first(frameVideoChecks(video(), [pending])).detail).toContain('图片正在生成或排队')
  })
  it('已采用图片丢失和选定画面被移除也会阻止提交', () => {
    expect(
      first(frameVideoChecks(video(), [card()], [{ id: 'image', missing: true }])).detail,
    ).toContain('文件不可用')
    expect(first(frameVideoChecks(video({ first_card_id: 'removed' }), [card()])).detail).toContain(
      '已移除',
    )
  })
  it('选用了尾帧时，尾帧也必须有采用的图片', () => {
    const checks = frameVideoChecks(video({ last_card_id: 'last' }), [
      card(),
      card({ id: 'last', adopted_asset_id: null }),
    ])
    expect(first(checks).ready).toBe(true)
    expect(checks.find((item) => item.key === 'last')?.ready).toBe(false)
  })
  it.each([0, 61, Number.NaN])('无效时长 %s 会显示修复范围', (duration) => {
    expect(
      frameVideoChecks(video({ duration }), [card()]).find((item) => item.key === 'duration'),
    ).toMatchObject({ ready: false })
  })
})
