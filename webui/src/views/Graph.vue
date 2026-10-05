<script setup lang="ts">
/**
 * 知识图谱页（§5.1 ④ v0.57，浏览型起步）：promoted 实体节点 + 关系边。
 * 数据复用 /api/graph（kg_edges 派生缓存）；draft 不入图——图谱只呈现人工确认过的知识。
 * 渲染经 kg-graph.ts（echarts Graph，动态 import 独立分包）；节点点击展示实体详情。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api'

interface GraphNode {
  id: string
  name: string
  entity_type: string
  sources: number
}
interface GraphEdge {
  source: string
  target: string
  type: string
}

const error = ref('')
const loading = ref(true)
const nodes = ref<GraphNode[]>([])
const edges = ref<GraphEdge[]>([])
const totalNodes = ref(0)
const totalEdges = ref(0)
const selected = ref<GraphNode | null>(null)
const selectedDegree = ref(0)
const selectedRels = ref<{ type: string; other: string; dir: 'out' | 'in' }[]>([])

const container = ref<HTMLElement>()
let handle: { dispose: () => void } | null = null

const nameById = computed(() => new Map(nodes.value.map((n) => [n.id, n.name])))

function selectNode(n: GraphNode) {
  selected.value = n
  selectedRels.value = edges.value
    .filter((e) => e.source === n.id || e.target === n.id)
    .map((e) =>
      e.source === n.id
        ? { type: e.type, other: nameById.value.get(e.target) ?? e.target, dir: 'out' as const }
        : { type: e.type, other: nameById.value.get(e.source) ?? e.source, dir: 'in' as const },
    )
  selectedDegree.value = selectedRels.value.length
}

onMounted(async () => {
  try {
    const data = await api.get<{ nodes: GraphNode[]; edges: GraphEdge[]; total_nodes: number; total_edges: number }>(
      '/api/graph',
    )
    nodes.value = data.nodes
    edges.value = data.edges
    totalNodes.value = data.total_nodes
    totalEdges.value = data.total_edges
    if (container.value && data.nodes.length) {
      const mod = await import('../kg-graph')
      handle = mod.createGraph(container.value, data.nodes, data.edges, selectNode)
    }
    error.value = ''
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  } finally {
    loading.value = false
  }
})

onBeforeUnmount(() => {
  handle?.dispose()
  handle = null
})
</script>

<template>
  <div v-if="error" class="alert error">{{ error }}</div>

  <div class="panel">
    <h2>知识图谱（promoted 实体卡，浏览型）</h2>
    <p class="muted" style="margin-top: -4px">
      只有晋升（promoted）的实体卡才入图——draft 是 AI 草稿，未人工确认不入图谱；
      节点大小 = 来源条目数，点节点看实体详情，滚轮缩放、拖拽平移。
    </p>
    <p v-if="loading" class="muted">加载中…</p>
    <p v-else-if="!totalNodes" class="muted">
      暂无图谱数据。到<router-link to="/curation">审核台</router-link>晋升实体卡后，这里会自动呈现实体与关系（关系来自实体卡的 relations，孤立实体也会显示）。
    </p>
    <template v-else>
      <p class="muted" style="font-size: 12px">实体 {{ totalNodes }} 个 · 关系 {{ totalEdges }} 条</p>
      <div ref="container" style="height: 520px; border: 1px solid var(--border, #e2e8f0); border-radius: 8px"></div>

      <div v-if="selected" class="card-detail" style="margin-top: 12px">
        <p class="card-meta">
          <b>{{ selected.name }}</b>
          <span class="badge" style="margin-left: 8px">{{ selected.entity_type || '未分类' }}</span>
          <span class="muted mono" style="margin-left: 8px; font-size: 11px">{{ selected.id }}</span>
        </p>
        <p class="card-meta muted" style="font-size: 12px">
          来源条目 {{ selected.sources }} 条 · 关系 {{ selectedDegree }} 条 ·
          <router-link to="/curation">在审核台查看该卡</router-link>
        </p>
        <p v-if="selectedRels.length" class="card-meta" style="font-size: 13px">
          <span v-for="(r, i) in selectedRels" :key="i" class="badge" style="margin: 2px 6px 2px 0">
            {{ r.dir === 'out' ? `${r.type} → ${r.other}` : `← ${r.type} — ${r.other}` }}
          </span>
        </p>
      </div>
    </template>
  </div>
</template>
