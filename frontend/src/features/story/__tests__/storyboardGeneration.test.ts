import { beforeEach, describe, expect, it, vi } from 'vitest'
import type * as FrameApi from '@/shared/api/frames'
vi.mock('@/shared/api/endpoint', () => ({
  endpoint: { baseUrl: '/api/v1', wsUrl: 'ws://localhost/api/v1/ws', token: '' },
}))
const mocked = vi.hoisted(() => ({ direct: vi.fn(), workspace: vi.fn(), frames: vi.fn() }))
vi.mock('@/shared/api/generation', () => ({ generationApi: { enqueueShot: mocked.direct } }))
vi.mock('@/shared/api/frames', async (importOriginal) => ({
  ...(await importOriginal<typeof FrameApi>()),
  framesApi: { get: mocked.workspace, video: mocked.frames },
}))
import { enqueueStoryboardVideo } from '../storyboardGeneration'
beforeEach(() => {
  vi.clearAllMocks()
})
describe('同一分镜页按制作方式取参', () => {
  it('直出使用角色场景上下文的生成入口，不读取制帧草稿', async () => {
    mocked.direct.mockResolvedValue({ id: 'direct-job' })
    await enqueueStoryboardVideo('project', { id: 'shot', duration: 6 }, 'direct', true)
    expect(mocked.direct).toHaveBeenCalledWith('project', 'shot', { allow_ref_drop: true })
    expect(mocked.workspace).not.toHaveBeenCalled()
    expect(mocked.frames).not.toHaveBeenCalled()
  })
  it('制帧读取保存的帧图与独立描述，使用分镜时长，不调用直出', async () => {
    mocked.workspace.mockResolvedValue({
      video: {
        prompt: 'A girl turns.',
        first_card_id: 'first',
        last_card_id: 'last',
        preset: 'frames-only',
        duration: 4,
      },
    })
    mocked.frames.mockResolvedValue({ id: 'frame-job' })
    await enqueueStoryboardVideo('project', { id: 'shot', duration: 6 }, 'frames')
    expect(mocked.frames).toHaveBeenCalledWith('project', 'shot', {
      prompt: 'A girl turns.',
      first_card_id: 'first',
      last_card_id: 'last',
      preset: 'frames-only',
      duration: 6,
    })
    expect(mocked.direct).not.toHaveBeenCalled()
  })
  it('制帧输入失败时保留错误，不回退到直出', async () => {
    mocked.workspace.mockResolvedValue({ video: {} })
    mocked.frames.mockRejectedValue(new Error('首帧尚未制作'))
    await expect(
      enqueueStoryboardVideo('project', { id: 'shot', duration: 6 }, 'frames'),
    ).rejects.toThrow('首帧尚未制作')
    expect(mocked.direct).not.toHaveBeenCalled()
  })
})
