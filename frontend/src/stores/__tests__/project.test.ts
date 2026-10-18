import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const mocks = vi.hoisted(() => ({
  open: vi.fn(),
  recent: vi.fn(),
  get: vi.fn(),
}))

vi.mock('@/shared/api/endpoint', () => ({
  endpoint: { baseUrl: '/api/v1', wsUrl: 'ws://localhost/api/v1/ws', token: '' },
}))
vi.mock('@/shared/api/projects', () => ({
  projectsApi: mocks,
}))

import { ApiError } from '@/shared/api/client'
import { useProjectStore } from '../project'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  mocks.recent.mockResolvedValue([])
})

describe('工程状态', () => {
  it('打开升级后的工程会显示迁移提示，重启后找不到工程会清除旧状态', async () => {
    mocks.open.mockResolvedValue({
      id: 'prj_test',
      name: '测试片',
      schema_version: 23,
      migrated_from: 22,
    })
    const store = useProjectStore()
    await store.open('D:/film')
    expect(store.migration).toEqual({ projectName: '测试片', from: 22, to: 23 })

    mocks.get.mockRejectedValue(
      new ApiError(
        {
          code: 'NOT_FOUND',
          title: '项目未打开',
          detail: '后端已重启',
          suggestions: ['重新打开工程'],
          related_ids: { project_id: 'prj_other' },
        },
        404,
      ),
    )
    await store.ensure('prj_other')
    expect(store.current).toBeNull()
    expect(store.lastError?.code).toBe('NOT_FOUND')
  })
})
