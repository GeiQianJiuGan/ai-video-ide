import { computed } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { PROJECT_NAV, PROJECT_NAV_FEATURES } from '../features'
import {
  loadProductionMode,
  saveProductionMode,
  showProductionFeature,
} from '../productionNavigation'

describe('制作方式与叙事入口', () => {
  it('制作图片紧接剧本，属于叙事规划；制作详情不再重复列为菜单', () => {
    expect(PROJECT_NAV.indexOf('frames')).toBe(PROJECT_NAV.indexOf('story') + 1)
    expect(PROJECT_NAV_FEATURES.find((f) => f.id === 'frames')?.group).toBe('story')
    expect(PROJECT_NAV_FEATURES.some((f) => f.group === 'generate')).toBe(false)
    expect(showProductionFeature('frames', 'frames')).toBe(true)
    expect(showProductionFeature('frames', 'direct')).toBe(false)
  })

  it('同页切换制作方式时，分镜与创建任务能同步读取新模式，且按项目隔离', () => {
    vi.stubGlobal('window', { localStorage: { getItem: () => null, setItem: vi.fn() } })
    const boardMode = computed(() => loadProductionMode('prj_mode_test'))
    expect(boardMode.value).toBe('direct')
    saveProductionMode('prj_mode_test', 'frames')
    expect(boardMode.value).toBe('frames')
    expect(loadProductionMode('prj_other_mode')).toBe('direct')
    saveProductionMode('prj_mode_test', 'direct')
    expect(boardMode.value).toBe('direct')
    vi.unstubAllGlobals()
  })
})
