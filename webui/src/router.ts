import { createRouter, createWebHistory } from 'vue-router'
import Overview from './views/Overview.vue'
import SearchView from './views/SearchView.vue'
import Timeline from './views/Timeline.vue'
import DocTree from './views/DocTree.vue'
import Graph from './views/Graph.vue'
import Curation from './views/Curation.vue'
import Settings from './views/Settings.vue'

// 导航七项（§11.7 布局约束，v0.57 增图谱）：总览 / 检索 / 时间流 / 文档树 / 图谱 / 审核台 / 参数设置
export const router = createRouter({
  history: createWebHistory('/app/'),
  routes: [
    { path: '/', name: 'overview', component: Overview, meta: { title: '总览' } },
    { path: '/search', name: 'search', component: SearchView, meta: { title: '检索' } },
    { path: '/timeline', name: 'timeline', component: Timeline, meta: { title: '时间流' } },
    { path: '/docs', name: 'docs', component: DocTree, meta: { title: '文档树' } },
    { path: '/graph', name: 'graph', component: Graph, meta: { title: '图谱' } },
    { path: '/curation', name: 'curation', component: Curation, meta: { title: '审核台' } },
    { path: '/settings', name: 'settings', component: Settings, meta: { title: '参数设置' } },
  ],
})
