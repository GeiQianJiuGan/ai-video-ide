import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/**
 * 路由与功能注册表一一对应：`@/app/features` 里登记的每个 route name
 * 都必须在这里出现，否则导航会指向不存在的页面。
 *
 * 入口是项目管理页（scope: 'app'）——没打开工程时项目内页面根本不出现，
 * 而不是画一排灰锁。项目内页面统一渲染 FeatureView：它按注册表把工作区骨架、
 * 工具栏动作、能力锁画出来。功能真正实现时，把对应条目换成实页面即可。
 */
const FeatureView = () => import('@/shared/ui/FeatureView.vue')

const projectRoutes: RouteRecordRaw[] = [
  // 概览与素材层三页已接后端；其余仍走注册表骨架（FeatureView）
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
  { path: 'story', name: 'story', component: FeatureView },
  { path: 'storyboard', name: 'storyboard', component: FeatureView },
  { path: 'shot/:sid?', name: 'shot', component: FeatureView },
  { path: 'workflows', name: 'workflows', component: FeatureView },
  { path: 'queue', name: 'queue', component: FeatureView },
  { path: 'timeline', name: 'timeline', component: FeatureView },
  { path: 'assets', name: 'assets', component: FeatureView },
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
  { path: '/p/:pid', children: projectRoutes },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/features/settings/SettingsView.vue'),
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export const router = createRouter({ history: createWebHistory(), routes })
