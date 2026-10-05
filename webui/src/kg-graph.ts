/**
 * 知识图谱渲染（v0.57 第③批，浏览型）：echarts Graph 力导布局。
 * 动态 import 独立分包（与 flowchart.ts 同手法，避免撑大主 bundle）；
 * 节点按 entity_type 分类着色，边展示关系类型（hover 提示，不做常显标签防糊）。
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
}
export interface GraphEdge {
  source: string
  target: string
  type: string
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

  // 分类 = entity_type 出现顺序（稳定），色板循环
  const types = [...new Set(nodes.map((n) => n.entity_type || '未分类'))]
  const categories = types.map((name, i) => ({ name, itemStyle: { color: PALETTE[i % PALETTE.length] } }))

  chart.setOption({
    tooltip: {
      formatter: (p: { dataType: string; data: { name: string; type?: string } }) =>
        p.dataType === 'edge' ? `${p.data.name}` : `${p.data.name}`,
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
          symbolSize: 18 + Math.min(14, n.sources * 3), // 来源越多节点越大（溯源性可视化）
          nodeType: n.entity_type,
        })),
        links: edges.map((e) => ({
          source: e.source,
          target: e.target,
          value: e.type,
          lineStyle: { color: '#94a3b8', width: 1.5, curveness: 0.1 },
          label: { show: true, formatter: e.type, fontSize: 10, color: '#64748b' },
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
