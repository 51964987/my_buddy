<script setup lang="ts">
/** 工作台布局：左侧主导航（折叠/展开，偏好存浏览器本地，§11.7）+ 内容区。 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from './api'
import { subscribeEvents } from './sse'

const collapsed = ref(localStorage.getItem('kb_sidebar_collapsed') === '1')
const curationTodo = ref(0) // 审核台待办：待审单元数（draft 摘要卡，1 条目 1 单元；v0.55 口径）
const route = useRoute()

// 导航按管道依赖序（v0.59，§11.8）：总览（管道地图）→ 文档树（内容入口）
// → 审核台（AI 产出把关）→ 图谱（结构化知识）→ 检索/时间流（消费）→ 设置殿后；
// 页面之内仍维持 attention-first（v0.47：异常置顶、高频近手），两层口径互不冲突
const navs = [
  { name: 'overview', title: '总览', icon: 'grid' },
  { name: 'docs', title: '文档树', icon: 'folder' },
  { name: 'curation', title: '审核台', icon: 'shield' },
  { name: 'graph', title: '图谱', icon: 'graph' },
  { name: 'search', title: '检索', icon: 'search' },
  { name: 'timeline', title: '时间流', icon: 'clock' },
  { name: 'settings', title: '参数设置', icon: 'gear' },
]

function toggleCollapse() {
  collapsed.value = !collapsed.value
  // 折叠属 UI 偏好：只存浏览器本地，不进配置文件（§11.7 纪律）
  localStorage.setItem('kb_sidebar_collapsed', collapsed.value ? '1' : '0')
}

async function refreshBadge() {
  try {
    const data = await api.get<{ total: number }>('/api/wiki?status=draft&type=summary')
    curationTodo.value = data.total
  } catch {
    curationTodo.value = 0 // 未配置 token 等场景不渲染角标
  }
}

// SSE 实时角标（v0.56，修 v0.53 漏接）：写盘事件 → debounce 重拉角标（§11.8 ⑥
// 推送触发刷新同口径，业界 badge 通行做法）；重连（_reconnected）直接重拉兜底。
// 路由切换与页面内操作 emit（refresh-badge）刷新保留——三通道互补。
let badgeTimer: number | null = null
let unsubSse: (() => void) | null = null

function scheduleBadgeRefresh() {
  if (badgeTimer !== null) window.clearTimeout(badgeTimer)
  badgeTimer = window.setTimeout(() => {
    badgeTimer = null
    void refreshBadge()
  }, 500)
}

onMounted(() => {
  void refreshBadge()
  unsubSse = subscribeEvents(['kb.changed', '_reconnected'], (ev) => {
    if (ev.type === '_reconnected') void refreshBadge()
    else scheduleBadgeRefresh()
  })
})

onBeforeUnmount(() => {
  unsubSse?.()
  unsubSse = null
  if (badgeTimer !== null) window.clearTimeout(badgeTimer)
})

watch(() => route.path, refreshBadge)

const pageTitle = computed(() => (route.meta.title as string) ?? '')
</script>

<template>
  <nav class="sidebar" :class="{ collapsed }">
    <div class="brand">kb buddy</div>
    <router-link
      v-for="n in navs"
      :key="n.name"
      :to="{ name: n.name }"
      class="nav-item"
      :data-title="n.title"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <template v-if="n.icon === 'grid'">
          <rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" />
          <rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" />
        </template>
        <template v-else-if="n.icon === 'search'">
          <circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.5" y2="16.5" />
        </template>
        <template v-else-if="n.icon === 'clock'">
          <circle cx="12" cy="12" r="9" /><polyline points="12 7 12 12 15.5 14" />
        </template>
        <template v-else-if="n.icon === 'folder'">
          <path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
        </template>
        <template v-else-if="n.icon === 'graph'">
          <circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle cx="12" cy="18" r="2.5" />
          <line x1="8" y1="7.5" x2="11" y2="15.8" /><line x1="16" y1="7.5" x2="13" y2="15.8" /><line x1="8.5" y1="6" x2="15.5" y2="6" />
        </template>
        <template v-else-if="n.icon === 'shield'">
          <path d="M12 3l8 3v6c0 4.5-3.2 7.8-8 9-4.8-1.2-8-4.5-8-9V6z" />
        </template>
        <template v-else>
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h0a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55h0a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v0a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1z" />
        </template>
      </svg>
      <span class="nav-label">{{ n.title }}</span>
      <span v-if="n.name === 'curation' && curationTodo > 0" class="nav-badge">{{ curationTodo }}</span>
    </router-link>
  </nav>

  <main class="main">
    <!-- 顶栏（业界通行做法，参考豆包/Notion/GitHub）：导航切换 + 当前页标题常驻吸附 -->
    <header class="topbar">
      <button
        class="topbar-toggle"
        :data-title="collapsed ? '展开导航' : '折叠导航'"
        @click="toggleCollapse"
      >
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="3" y1="6" x2="21" y2="6" />
          <line x1="3" y1="12" x2="21" y2="12" />
          <line x1="3" y1="18" x2="21" y2="18" />
        </svg>
      </button>
      <h1>{{ pageTitle }}</h1>
    </header>
    <div class="main-body">
      <router-view @refresh-badge="refreshBadge" />
    </div>
  </main>
</template>
