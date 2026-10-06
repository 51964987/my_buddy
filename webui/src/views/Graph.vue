<script setup lang="ts">
/**
 * 知识图谱页（§5.1 ④ v0.57 浏览型起步，v0.68 三视图）：promoted 实体/概念节点 + 关系边。
 * 数据复用 /api/graph?kind=entity|concept|mixed（kg_edges 派生缓存 + 概念—实体共现派生）；
 * draft 不入图——图谱只呈现人工确认过的知识。
 * 渲染经 kg-graph.ts（echarts Graph，动态 import 独立分包）；节点点击展示实体详情。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'

interface GraphNode {
  id: string
  name: string
  aliases?: string[]
  entity_type: string
  sources: number
  kind: 'entity' | 'concept'
}
interface GraphEdge {
  source: string
  target: string
  type: string
  kind: 'relation' | 'cooccurrence'
  weight: number
}

type GraphKind = 'entity' | 'concept' | 'mixed'
const KINDS: { value: GraphKind; label: string; hint: string }[] = [
  { value: 'entity', label: '实体图', hint: 'promoted 实体卡 + 实体间关系（relations 事实源）' },
  { value: 'concept', label: '概念图', hint: '概念卡统领实体：共享来源条目 ≥ 2 即共现边' },
  { value: 'mixed', label: '双层混合', hint: '概念—实体共现边 + 实体间关系，两层全貌' },
]

const route = useRoute()
const router = useRouter()
const error = ref('')
const loading = ref(true)
const kind = ref<GraphKind>('entity')
const nodes = ref<GraphNode[]>([])
const edges = ref<GraphEdge[]>([])
const selected = ref<GraphNode | null>(null)
const selectedDegree = ref(0)
const selectedRels = ref<{ type: string; other: string; dir: 'out' | 'in'; kind: string; weight: number }[]>(
  [],
)

// 节点过滤（v0.61）：?q= 初始定位（检索页/问答「相关实体」跳转入口）+ 页内输入即时过滤。
// 节点量级小（promoted 卡），纯客户端过滤，无新增检索端点。
const filterQ = ref('')
const filteredNodes = computed(() => {
  const kw = filterQ.value.trim().toLowerCase()
  if (!kw) return nodes.value
  return nodes.value.filter(
    (n) =>
      n.name.toLowerCase().includes(kw) ||
      n.entity_type.toLowerCase().includes(kw) ||
      (n.aliases ?? []).some((a) => a.toLowerCase().includes(kw)),
  )
})
const filteredEdges = computed(() => {
  if (!filterQ.value.trim()) return edges.value
  const ids = new Set(filteredNodes.value.map((n) => n.id))
  return edges.value.filter((e) => ids.has(e.source) && ids.has(e.target))
})

const entityCount = computed(() => nodes.value.filter((n) => n.kind === 'entity').length)
const conceptCount = computed(() => nodes.value.filter((n) => n.kind === 'concept').length)
const kindHint = computed(() => KINDS.find((k) => k.value === kind.value)?.hint ?? '')

const container = ref<HTMLElement>()
let handle: { dispose: () => void } | null = null

const nameById = computed(() => new Map(nodes.value.map((n) => [n.id, n.name])))

function selectNode(n: GraphNode) {
  selected.value = n
  selectedRels.value = edges.value
    .filter((e) => e.source === n.id || e.target === n.id)
    .map((e) =>
      e.source === n.id
        ? {
            type: e.type,
            other: nameById.value.get(e.target) ?? e.target,
            dir: 'out' as const,
            kind: e.kind,
            weight: e.weight,
          }
        : {
            type: e.type,
            other: nameById.value.get(e.source) ?? e.source,
            dir: 'in' as const,
            kind: e.kind,
            weight: e.weight,
          },
    )
  selectedDegree.value = selectedRels.value.length
}

async function load(k: GraphKind) {
  const data = await api.get<{ nodes: GraphNode[]; edges: GraphEdge[]; total_nodes: number; total_edges: number }>(
    `/api/graph?kind=${k}`,
  )
  nodes.value = data.nodes
  edges.value = data.edges
}

onMounted(async () => {
  // ?q= 初始定位（v0.61）/ ?kind= 图谱类型（v0.68）：前者来自检索页、问答「相关实体」跳转
  const q0 = route.query.q
  if (typeof q0 === 'string' && q0.trim()) filterQ.value = q0.trim()
  const k0 = route.query.kind
  if (typeof k0 === 'string' && KINDS.some((k) => k.value === k0)) kind.value = k0 as GraphKind
  try {
    await load(kind.value)
    loading.value = false
    // 容器 div 在 v-else 分支内（须 loading=false 才渲染）：先置 loading 再 nextTick 等渲染完成，
    // 否则 container.value 为 undefined，图表初始化被跳过（实测空 canvas、只见计数）
    await nextTick()
    await renderGraph()
    error.value = ''
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
    loading.value = false
  }
})

async function renderGraph() {
  handle?.dispose()
  handle = null
  if (!container.value || !filteredNodes.value.length) return
  const mod = await import('../kg-graph')
  handle = mod.createGraph(container.value, filteredNodes.value, filteredEdges.value, selectNode)
  // 过滤定位：唯一或首个命中自动选中，省一次点击（检索页/问答跳转的主场景）
  if (filterQ.value.trim() && filteredNodes.value.length) selectNode(filteredNodes.value[0])
}

// 切换图谱类型：重新拉该视图的数据（节点集与边来源都不同，非同一份数据的子集过滤）
async function switchKind(k: GraphKind) {
  if (k === kind.value) return
  kind.value = k
  selected.value = null
  error.value = ''
  void router.replace({ query: { ...route.query, kind: k } })
  await load(k)
  await renderGraph()
}

// 过滤输入即时重建图（节点量级小，dispose+重建成本可忽略；模块 import 有缓存）
watch(filterQ, () => {
  void renderGraph()
})

onBeforeUnmount(() => {
  handle?.dispose()
  handle = null
})
</script>

<template>
  <div v-if="error" class="alert error">{{ error }}</div>

  <div class="panel">
    <h2>知识图谱（promoted 卡，浏览型）</h2>
    <p class="muted" style="margin-top: -4px">
      只有晋升（promoted）的卡才入图——draft 是 AI 草稿，未人工确认不入图谱；
      节点大小 = 来源条目数，点节点看详情，滚轮缩放、拖拽平移。
    </p>
    <p v-if="loading" class="muted">加载中…</p>
    <p v-else-if="!nodes.length" class="muted">
      暂无图谱数据。到<router-link to="/curation">审核台</router-link>晋升实体卡后，这里会自动呈现实体与关系（关系来自实体卡的 relations，孤立实体也会显示）。
    </p>
    <template v-else>
      <div class="graph-kinds">
        <button
          v-for="k in KINDS"
          :key="k.value"
          type="button"
          :class="{ active: k.value === kind }"
          @click="switchKind(k.value)"
        >
          {{ k.label }}
        </button>
      </div>
      <p class="muted" style="font-size: 12px; margin-top: -2px">
        {{ kindHint }}
      </p>
      <p class="muted" style="font-size: 12px">
        实体 {{ entityCount }} 个<template v-if="kind !== 'entity'"> · 概念 {{ conceptCount }} 个</template> ·
        边 {{ edges.length }} 条<template v-if="filterQ.trim()"> · 命中 {{ filteredNodes.length }} 个（过滤中）</template>
      </p>
      <p v-if="kind !== 'entity' && !conceptCount" class="muted" style="font-size: 12px">
        当前库没有已晋升的概念卡——概念卡由 AI 聚合产出（总览页「聚合概念卡」），晋升后这里会出现概念节点与共现边。
      </p>
      <div style="display: flex; gap: 10px; margin-bottom: 8px">
        <input
          v-model="filterQ"
          type="text"
          placeholder="按名称 / 别名 / 类型过滤节点…"
          style="flex: 0 1 320px"
        />
        <button v-if="filterQ" type="button" @click="filterQ = ''">清除</button>
      </div>
      <p v-if="!filteredNodes.length" class="muted">没有匹配「{{ filterQ }}」的节点，清除过滤查看全部。</p>
      <div ref="container" style="height: 520px; border: 1px solid var(--border, #e2e8f0); border-radius: 8px"></div>

      <div v-if="selected" class="card-detail" style="margin-top: 12px">
        <p class="card-meta">
          <b>{{ selected.name }}</b>
          <span class="badge" style="margin-left: 8px">{{ selected.kind === 'concept' ? '概念' : selected.entity_type || '未分类' }}</span>
          <span class="muted mono" style="margin-left: 8px; font-size: 11px">{{ selected.id }}</span>
        </p>
        <p class="card-meta muted" style="font-size: 12px">
          来源条目 {{ selected.sources }} 条 · 关系 {{ selectedDegree }} 条 ·
          <router-link to="/curation">在审核台查看该卡</router-link>
        </p>
        <p v-if="selectedRels.length" class="card-meta" style="font-size: 13px">
          <span v-for="(r, i) in selectedRels" :key="i" class="badge" style="margin: 2px 6px 2px 0">
            <template v-if="r.kind === 'cooccurrence'">
              {{ r.dir === 'out' ? `共现(${r.weight}) → ${r.other}` : `← 共现(${r.weight}) — ${r.other}` }}
            </template>
            <template v-else>
              {{ r.dir === 'out' ? `${r.type} → ${r.other}` : `← ${r.type} — ${r.other}` }}
            </template>
          </span>
        </p>
      </div>
    </template>
  </div>
</template>
