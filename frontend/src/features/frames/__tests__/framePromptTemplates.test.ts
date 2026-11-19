import { describe, expect, it } from 'vitest'
import { framePromptTemplates, frameStylePrompt } from '../frameStylePresets'

describe('抽帧转绘模板', () => {
  it('提供不同风格，并明确保留源镜头和人物信息', () => {
    expect(framePromptTemplates.length).toBeGreaterThanOrEqual(6)
    for (const template of framePromptTemplates.filter(
      (item) => item.id !== 'custom' && !('prompt' in item),
    )) {
      const prompt = frameStylePrompt(template.style)
      expect(prompt).toContain(template.style)
      expect(prompt).toContain('人物身份')
      expect(prompt).toContain('构图与画幅比例')
    }
  })
  it('动漫化使用独立文案，允许人物外观大致相近', () => {
    const template = framePromptTemplates.find((item) => item.id === 'anime-film')!
    expect(template.prompt).not.toContain('日系')
    expect(template.prompt).toContain('相近即可')
    expect(template.prompt).toContain('无需严格复刻')
    expect(template.prompt).not.toContain('只改变视觉表现')
  })
  it('可填入任意自定义风格，生成普通可编辑文本', () => {
    expect(frameStylePrompt('  复古漫画  ')).toContain('转换为复古漫画。')
  })
})
