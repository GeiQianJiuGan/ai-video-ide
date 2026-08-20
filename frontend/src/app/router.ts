import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/**
 * 路由与功能注册表一一对应：`@/app/features` 里登记的每个 route name
 * 都必须在这里出现，否则导航会指向不存在的页面。
 *
 * 所有项目内页面统一渲染 FeatureView —— 它按注册表把工作区骨架、
 * 工具栏动作、能力锁画出来。功能真正实现时，把对应条目换成实页面即可。
 */
const FeatureView = () => import('@/shared/ui/FeatureView.vue')

const projectRoutes: RouteRecordRaw[] = [
  { path: '', name: 'dashboard', component: FeatureView },
  { path: 'characters', name: 'characters', component: FeatureView },
  { path: 'locations', name: 'locations', component: FeatureView },
  { path: 'props', name: 'props', component: FeatureView },
  { path: 'story', name: 'story', component: FeatureView },
  { path: 'storyboard', name: 'storyboard', component: FeatureView },
  { path: 'shot/:sid?', name: 'shot', component: FeatureView },
  { path: 'workflows', name: 'workflows', component: FeatureView },
  { path: 'queue', name: 'queue', component: FeatureView },
  { path: 'timeline', name: 'timeline', component: FeatureView },
  { path: 'assets', name: 'assets', component: FeatureView },
]

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'home', component: () => import('@/features/home/HomeView.vue') },
  {
    path: '/projects',
    name: 'projects',
    component: () => import('@/features/project/ProjectsView.vue'),
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
