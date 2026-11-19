import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../endpoint', () => ({
  endpoint: { baseUrl: '/api/v1', wsUrl: 'ws://localhost/api/v1/ws', token: 'test-token' },
}))

import { ApiError, api, isProjectNotOpen, type ErrorPayload } from '../client'

afterEach(() => vi.unstubAllGlobals())

describe('API 错误契约', () => {
  it('超限文件在发送上传请求前被拦截', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ upload_limit_mb: 1 })))
    vi.stubGlobal('fetch', fetcher)
    const file = new File([new Uint8Array(1024 * 1024 + 1)], 'large.mp4')
    await expect(api.upload('/projects/prj_test/assets/upload', file)).rejects.toMatchObject({
      code: 'VALIDATION_ERROR',
      status: 413,
      relatedIds: { upload_limit_mb: 1 },
    })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0]?.[0]).toBe('/api/v1/integration/capabilities')
  })

  it('允许超过旧 500 MB 限制的文件并保持表单字段', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ upload_limit_mb: 10240 })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'ast_test' }), { status: 201 }))
    vi.stubGlobal('fetch', fetcher)
    const file = new File(['video'], 'long.mp4')
    Object.defineProperty(file, 'size', { value: 600 * 1024 * 1024 })
    await expect(
      api.upload('/projects/prj_test/assets/upload', file, { kind: 'upload' }),
    ).resolves.toEqual({ id: 'ast_test' })
    const form = fetcher.mock.calls[1]?.[1]?.body as FormData
    expect(form.get('kind')).toBe('upload')
    expect((form.get('file') as File).name).toBe('long.mp4')
  })

  it('保留后端的四要素与工程标识', async () => {
    const payload: ErrorPayload = {
      code: 'NOT_FOUND',
      title: '项目未打开',
      detail: '后端已经重启',
      suggestions: ['回到起始页重新打开'],
      related_ids: { project_id: 'prj_test' },
    }
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: payload }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetcher)

    await expect(api.get('/projects/prj_test')).rejects.toMatchObject({
      title: payload.title,
      detail: payload.detail,
      suggestions: payload.suggestions,
    })
    expect(fetcher).toHaveBeenCalledWith(
      '/api/v1/projects/prj_test',
      expect.objectContaining({
        headers: expect.objectContaining({ 'X-AIVS-Token': 'test-token' }),
      }),
    )
    expect(isProjectNotOpen(new ApiError(payload, 404))).toBe(true)
  })

  it('代理返回 HTML 时给出可执行的连接错误', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('<html>Bad Gateway</html>', { status: 502 })),
    )
    await expect(api.get('/projects')).rejects.toMatchObject({
      code: 'NETWORK_ERROR',
      status: 502,
      suggestions: expect.arrayContaining([expect.stringContaining('后端')]),
    })
  })
})
