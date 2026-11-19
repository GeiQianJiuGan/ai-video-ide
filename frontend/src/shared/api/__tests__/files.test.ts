import { describe, expect, it, vi } from 'vitest'

vi.mock('../endpoint', () => ({
  endpoint: { baseUrl: '/api/v1', wsUrl: 'ws://localhost/api/v1/ws', token: 'tok en' },
}))

import { fileUrl } from '../files'

describe('fileUrl', () => {
  it('逐段编码路径并带上 token', () => {
    expect(fileUrl('prj_1', 'assets/角色 表.png')).toBe(
      '/api/v1/projects/prj_1/files/assets/%E8%A7%92%E8%89%B2%20%E8%A1%A8.png?token=tok+en',
    )
  })

  it('图片要缩略图时带 w，token 照样在', () => {
    expect(fileUrl('prj_1', 'a/b.PNG', 256)).toBe(
      '/api/v1/projects/prj_1/files/a/b.PNG?w=256&token=tok+en',
    )
  })

  it('视频忽略缩略图参数，走原文件', () => {
    expect(fileUrl('prj_1', 'v/c.mp4', 256)).toBe(
      '/api/v1/projects/prj_1/files/v/c.mp4?token=tok+en',
    )
  })

  it('缺 pid 或路径给空串', () => {
    expect(fileUrl('', 'a.png')).toBe('')
    expect(fileUrl('prj_1', '')).toBe('')
  })
})
