/**
 * 知识图谱渲染（v0.57 第③批，浏览型；v0.68 三视图）：echarts Graph 力导布局。
 * 动态 import 独立分包（与 flowchart.ts 同手法，避免撑大主 bundle）；
 * 节点按 entity_type 分类着色（概念节点为独立分类「概念」+ 菱形），边分两类——
 * 实体 relations 实线带类型标签、概念—实体共现边虚线按共享条目数定线宽（无标签防糊）。
 */
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([GraphChart, LegendComponent, TooltipComponent, CanvasRenderer])

export interface GraphNode {
  id: string
  name: string
  entity_type: string
  sources: number
  /** v0.68：节点层——概念卡（aggregate 聚合产物）与实体卡同图时形状/尺寸区分 */
  kind: 'entity' | 'concept'
}
export interface GraphEdge {
  source: string
  target: string
  type: string
  /** v0.68：relation = 实体卡 relations 事实源；cooccurrence = 概念—实体共享来源派生 */
  kind: 'relation' | 'cooccurrence'
  /** 共现边的共享来源条目数（relation 边恒为 1） */
  weight: number
}

export interface GraphHandle {
  dispose: () => void
}

// 分类色板：类型不可枚举（ai.kg 白名单可改），未知类型循环取色
const PALETTE = ['#2563eb', '#059669', '#d97706', '#dc2626', '#7c3aed', '#0891b2', '#be185d', '#65a30d']

export function createGraph(
  el: HTMLElement,
  nodes: GraphNode[],
  edges: GraphEdge[],
  onNodeClick: (node: GraphNode) => void,
): GraphHandle {
  const chart = echarts.init(el)

  // 分类 = entity_type 出现顺序（稳定），色板循环；概念节点归入「概念」分类（§5.1 ④ v0.68）
  const types = [...new Set(nodes.map((n) => n.entity_type || '未分类'))]
  const categories = types.map((name, i) => ({ name, itemStyle: { color: PALETTE[i % PALETTE.length] } }))

  chart.setOption({
    tooltip: {
      formatter: (p: {
        dataType: string
        data: {
          name: string
          value?: string
          nodeKind?: string
          nodeType?: string
          sources?: number
          edgeKind?: string
          weight?: number
        }
      }) => {
        const d = p.data
        if (p.dataType === 'edge') {
          return d.edgeKind === 'cooccurrence'
            ? `${d.name}<br/>共现：共享来源条目 ${d.weight ?? 1} 条`
            : `${d.name}<br/>关系：${d.value ?? ''}`
        }
        const kindText = d.nodeKind === 'concept' ? '概念' : d.nodeType || '未分类'
        return `${d.name}<br/>${kindText} · 来源 ${d.sources ?? 0}`
      },
    },
    legend: [{ data: types.map((t) => t), top: 8, textStyle: { fontSize: 11 } }],
    series: [
      {
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        force: { repulsion: 320, edgeLength: 110, gravity: 0.12 },
        categories,
        data: nodes.map((n) => ({
          id: n.id,
          name: n.name,
          category: Math.max(0, types.indexOf(n.entity_type || '未分类')),
          // 概念用菱形且更大（层级更高），实体按来源数定尺寸（溯源性可视化）
          symbol: n.kind === 'concept' ? 'diamond' : 'circle',
          symbolSize: n.kind === 'concept' ? 28 : 18 + Math.min(14, n.sources * 3),
          nodeKind: n.kind,
          nodeType: n.entity_type,
          sources: n.sources,
        })),
        links: edges.map((e) => ({
          source: e.source,
          target: e.target,
          value: e.type,
          edgeKind: e.kind,
          weight: e.weight,
          // 共现边：虚线 + 线宽随共享条目数（不显标签，量大防糊）；关系边：实线带类型标签
          lineStyle:
            e.kind === 'cooccurrence'
              ? { color: '#cbd5e1', width: 1 + Math.min(3, e.weight ?? 1), type: 'dashed', curveness: 0.1 }
              : { color: '#94a3b8', width: 1.5, curveness: 0.1 },
          label: { show: e.kind !== 'cooccurrence', formatter: e.type, fontSize: 10, color: '#64748b' },
        })),
        label: { show: true, position: 'bottom', fontSize: 11, color: '#334155' },
        emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
      },
    ],
  })

  chart.on('click', (params) => {
    if (params.dataType !== 'node') return
    const d = params.data as { id?: string; name: string; nodeType?: string }
    const node = nodes.find((n) => n.id === d.id)
    if (node) onNodeClick(node)
  })

  const ro = new ResizeObserver(() => chart.resize())
  ro.observe(el)
  return {
    dispose() {
      ro.disconnect()
      chart.dispose()
    },
  }
}
