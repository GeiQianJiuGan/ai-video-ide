import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../endpoint', () => ({
  endpoint: { baseUrl: '/api/v1', wsUrl: 'ws://localhost/api/v1/ws', token: 'test-token' },
}))

import { ApiError, api, isProjectNotOpen, type ErrorPayload } from '../client'

afterEach(() => vi.unstubAllGlobals())

describe('API 错误契约', () => {
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
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>Bad Gateway</html>', { status: 502 })))
    await expect(api.get('/projects')).rejects.toMatchObject({
      code: 'NETWORK_ERROR',
      status: 502,
      suggestions: expect.arrayContaining([expect.stringContaining('后端')]),
    })
  })
})
