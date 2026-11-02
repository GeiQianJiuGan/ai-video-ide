/** Menu preference only: switching never modifies a project's generation settings. */
export type ProductionMode = 'direct' | 'frames'

export const PRODUCTION_MODES = [
  { id: 'direct', label: '直接制作', short: '直出', description: '剧情与设定素材直接生成视频' },
  { id: 'frames', label: '制帧制作', short: '制帧', description: '先制作镜头画面，再生成视频' },
] as const

const directRoutes = new Set(['flow', 'scene', 'shot'])
const frameRoutes = new Set(['frames', 'frame-video'])

export function productionModeForRoute(name: string): ProductionMode | null {
  if (directRoutes.has(name)) return 'direct'
  if (frameRoutes.has(name)) return 'frames'
  return null
}

export function showProductionFeature(id: string, mode: ProductionMode): boolean {
  // Queue details remain available from the persistent task console.
  if (id === 'queue') return false
  const owner = productionModeForRoute(id)
  return owner === null || owner === mode
}

const key = (pid: string) => `aivs-production-menu:${pid}`

export function loadProductionMode(pid: string): ProductionMode {
  try {
    return window.localStorage.getItem(key(pid)) === 'frames' ? 'frames' : 'direct'
  } catch {
    return 'direct'
  }
}

export function saveProductionMode(pid: string, mode: ProductionMode): void {
  try {
    window.localStorage.setItem(key(pid), mode)
  } catch {
    // Storage is optional; the current session still works.
  }
}
