import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/**
 * 路由与功能注册表一一对应：`@/app/features` 里登记的每个 route name
 * 都必须在这里出现，否则导航会指向不存在的页面。
 *
 * 入口是项目管理页（scope: 'app'）——没打开工程时项目内页面根本不出现，
 * 而不是画一排灰锁。
 *
 * 现在每条路由都指向真页面：注册表里 13 个功能全部 `ready: true`。
 * `shared/ui/FeatureView.vue`（按注册表画工作区骨架与能力锁）暂时没有人用，
 * 留着给下一个「登记了但还没接后端」的功能——那种情况先挂它，绝不给假界面。
 */

const projectRoutes: RouteRecordRaw[] = [
  {
    path: '',
    name: 'dashboard',
    component: () => import('@/features/project/OverviewView.vue'),
  },
  {
    path: 'characters',
    name: 'characters',
    component: () => import('@/features/cast/CharactersView.vue'),
  },
  {
    path: 'locations',
    name: 'locations',
    component: () => import('@/features/world/LocationsView.vue'),
  },
  { path: 'props', name: 'props', component: () => import('@/features/world/PropsView.vue') },
  { path: 'story', name: 'story', component: () => import('@/features/story/StoryView.vue') },
  {
    path: 'storyboard',
    name: 'storyboard',
    component: () => import('@/features/story/StoryboardView.vue'),
  },
  {
    path: 'shot/:sid?',
    name: 'shot',
    component: () => import('@/features/story/ShotView.vue'),
  },
  // 两级场景系统：flow 是第一级（整片一张图），scene 是第二级（一幕的工作台）。
  // scene 不进导航——它只从流程图上点节点进来，所以 URL 里必须带 sid。
  {
    path: 'flow',
    name: 'flow',
    component: () => import('@/features/flow/FlowView.vue'),
  },
  {
    path: 'scene/:sid',
    name: 'scene',
    component: () => import('@/features/flow/SceneWorkbench.vue'),
  },
  {
    path: 'workflows',
    name: 'workflows',
    redirect: { name: 'dashboard' },
  },
  {
    path: 'queue',
    name: 'queue',
    component: () => import('@/features/generation/QueueView.vue'),
  },
  {
    path: 'timeline',
    name: 'timeline',
    component: () => import('@/features/timeline/TimelineView.vue'),
  },
  {
    path: 'assets',
    name: 'assets',
    component: () => import('@/features/assets/AssetsView.vue'),
  },
]

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'projects', component: () => import('@/features/project/ProjectsView.vue') },
  // 旧链接与书签还指向 /projects，保留一条重定向而不是给 404
  { path: '/projects', redirect: '/' },
  // 素材库是应用级的（不在任何工程里），所以挂在顶层而不是 /p/:pid 下
  {
    path: '/library',
    name: 'library',
    component: () => import('@/features/library/LibraryView.vue'),
  },
  {
    path: '/presets',
    name: 'presets',
    component: () => import('@/features/preset/PresetsView.vue'),
  },
  {
    path: '/workflows',
    name: 'global-workflows',
    redirect: { name: 'presets' },
  },
  { path: '/p/:pid', children: projectRoutes },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/features/settings/SettingsView.vue'),
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export const router = createRouter({ history: createWebHistory(), routes })
