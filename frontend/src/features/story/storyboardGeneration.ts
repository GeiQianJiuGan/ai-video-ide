import type { ProductionMode } from '@/app/productionNavigation'
import { framesApi, frameVideoDraft } from '@/shared/api/frames'
import { generationApi } from '@/shared/api/generation'

/** 两种分镜共用入口，仅按制作方式读取对应的输入；制帧失败直接返回错误。 */
export async function enqueueStoryboardVideo(
  pid: string,
  shot: { id: string; duration: number },
  mode: ProductionMode,
  allowRefDrop = false,
) {
  const { id, duration } = shot
  if (mode === 'frames') {
    const workspace = await framesApi.get(pid, id)
    return framesApi.video(pid, id, {
      ...frameVideoDraft(workspace.video, duration),
      duration,
    })
  }
  return generationApi.enqueueShot(pid, id, { allow_ref_drop: allowRefDrop })
}
