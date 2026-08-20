import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const WipView = () => import('@/shared/ui/WipView.vue')

/** 项目内页面。M0 阶段除设置外均为占位，meta 描述其归属里程碑与计划内容。 */
const projectRoutes: RouteRecordRaw[] = [
  {
    path: '',
    name: 'dashboard',
    component: WipView,
    meta: {
      title: '项目概览',
      milestone: 'M1',
      items: [
        'Scene / Shot 数量、已生成与待生成统计',
        '总视频时长（各 Shot 当前版本求和）',
        '最近 10 条生成任务与 GPU / VRAM 状态',
        'Generation Queue 摘要',
      ],
    },
  },
  {
    path: 'characters',
    name: 'characters',
    component: WipView,
    meta: {
      title: 'Character Studio',
      milestone: 'M1 / M2',
      items: [
        'Character → Appearance → Character Sheet → Version 四层结构',
        'Appearance 继承（face/hair/body/traits 继承，age/costume/state 覆写）',
        '多视角 Character Sheet 与 view_regions 框选',
        '从已有形象派生新形象并生成 N 个候选',
      ],
    },
  },
  {
    path: 'locations',
    name: 'locations',
    component: WipView,
    meta: {
      title: 'Location Studio',
      milestone: 'M1',
      items: ['Location / Variant（日夜雨雪）/ Scene Reference 版本', '多机位参考生成'],
    },
  },
  {
    path: 'props',
    name: 'props',
    component: WipView,
    meta: {
      title: 'Prop Library',
      milestone: 'M1（最小实现）',
      items: ['道具与参考图版本', '被引用位置反查'],
    },
  },
  {
    path: 'story',
    name: 'story',
    component: WipView,
    meta: {
      title: 'Story / AI Director',
      milestone: 'M3',
      items: [
        '剧本编辑 + 三种模式（AI Auto / AI Assisted / Manual）',
        'LLM 拆解结果逐项 Diff 审阅后落库',
        '角色映射到已有 Character，避免重复建人',
        '连续性检查',
      ],
    },
  },
  {
    path: 'storyboard',
    name: 'storyboard',
    component: WipView,
    meta: {
      title: 'Storyboard',
      milestone: 'M3',
      items: [
        'Scene 泳道 + Shot 卡片（缩略图 / 时长 / Cast / 状态 / Context 完备度）',
        '批量生成：Scene 级、选中 Shot、N 个候选',
        '拖拽排序与跨 Scene 移动',
      ],
    },
  },
  {
    path: 'shot/:sid',
    name: 'shot',
    component: WipView,
    meta: {
      title: 'Shot Editor',
      milestone: 'M4',
      items: [
        'Basic / Cast & State / Prompt / Workflow / Dependencies 五个 Tab',
        'Context Inspector：查看、移除、添加、替换、恢复自动',
        'VersionRail：预览、A-B 对比、设为当前、加入时间线',
      ],
    },
  },
  {
    path: 'timeline',
    name: 'timeline',
    component: WipView,
    meta: {
      title: 'Timeline',
      milestone: 'M5',
      items: [
        'Canvas 2D 渲染：多轨、缩放、缩略图条、音频波形',
        '编辑命令集 + 撤销栈 + 吸附',
        'Auto-Assemble 与 Replace Version',
        '基础转场与效果，导出走 FFmpeg filter_complex',
      ],
    },
  },
  {
    path: 'assets',
    name: 'assets',
    component: WipView,
    meta: {
      title: 'Asset Library',
      milestone: 'M1',
      items: ['统一资产登记与反向引用', '孤儿资产检测'],
    },
  },
  {
    path: 'workflows',
    name: 'workflows',
    component: WipView,
    meta: {
      title: 'Workflow Manager',
      milestone: 'M2',
      items: [
        'Capability 声明与 Capability Matrix',
        '导入 workflow_api.json 并做 Node 绑定',
        '绑定校验 + ComfyUI 自定义节点探测',
      ],
    },
  },
  {
    path: 'queue',
    name: 'queue',
    component: WipView,
    meta: {
      title: 'Generation Queue',
      milestone: 'M4',
      items: [
        'DAG 依赖调度与并发',
        'Pause / Resume / Cancel / Retry / 优先级',
        '失败现场：graph、stderr、结构化错误',
      ],
    },
  },
]

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'projects', component: () => import('@/features/project/ProjectsView.vue') },
  { path: '/p/:pid', children: projectRoutes },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/features/settings/SettingsView.vue'),
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export const router = createRouter({ history: createWebHistory(), routes })
