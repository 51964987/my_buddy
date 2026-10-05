/**
 * 总览流程图（§11.8 ⑥ v0.53，B2 拍板）：ECharts 按需引入封装。
 *
 * - 形态：管道五站散点节点（计数 + 短标签 + 状态色）+ lines 系列流动粒子（管道边）
 *   + error 旁路虚线；本模块由 Overview 动态 import，ECharts 进独立 chunk 不涨主包；
 * - 状态色沿漏斗三色口径（ok 灰 / todo 蓝 / danger 红）；tooltip 保留状态词枚举
 *   原文（状态词对齐纪律）；「建议先处理」= 节点强调圈；
 * - 节点点击只回传 key，跳转纪律（§11.8 ①）由调用方决定。
 */

import * as echarts from 'echarts/core'
import { LinesChart, ScatterChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { ComposeOption } from 'echarts/core'
import type { LinesSeriesOption, ScatterSeriesOption } from 'echarts/charts'
import type { GridComponentOption, TooltipComponentOption } from 'echarts/components'

echarts.use([ScatterChart, LinesChart, GridComponent, TooltipComponent, CanvasRenderer])

type Option = ComposeOption<ScatterSeriesOption | LinesSeriesOption | GridComponentOption | TooltipComponentOption>

export interface FlowStation {
  key: string
  /** 完整标签（tooltip 用，保留状态词枚举原文） */
  label: string
  /** 节点内短标签 */
  short: string
  count: number | null
  tone: 'ok' | 'todo' | 'danger'
  suggested: boolean
}

export interface FlowChartHandle {
  update(stations: FlowStation[]): void
  destroy(): void
}

// 节点布局（隐藏直角坐标系 0-100）：管道主线一行，error 旁路居中下位
const POS: Record<string, [number, number]> = {
  inbox: [8, 66],
  normalized: [32, 66],
  draft: [56, 66],
  promoted: [80, 66],
  error: [44, 18],
}
const SHORT: Record<string, string> = {
  inbox: '投递',
  normalized: '待整理',
  draft: '待审核',
  promoted: '已晋升',
  error: 'error',
}
const TONE_COLOR: Record<FlowStation['tone'], string> = {
  ok: '#9aa0a6',
  todo: '#2563eb',
  danger: '#dc2626',
}
// 管道主线边 + error 旁路虚线（旁路从 normalized/draft 均可达——失败可发生在两阶段）
const EDGES: { from: string; to: string; dashed?: boolean }[] = [
  { from: 'inbox', to: 'normalized' },
  { from: 'normalized', to: 'draft' },
  { from: 'draft', to: 'promoted' },
  { from: 'normalized', to: 'error', dashed: true },
  { from: 'draft', to: 'error', dashed: true },
]

export function createFlowChart(el: HTMLElement, onNodeClick: (key: string) => void): FlowChartHandle {
  const chart = echarts.init(el)
  chart.on('click', (params) => {
    if (params.seriesType === 'scatter' && params.data && typeof (params.data as { key?: string }).key === 'string') {
      onNodeClick((params.data as { key: string }).key)
    }
  })

  const baseOption: Option = {
    animationDurationUpdate: 300,
    grid: { left: 12, right: 12, top: 14, bottom: 12 },
    xAxis: { type: 'value', min: 0, max: 100, show: false },
    yAxis: { type: 'value', min: 0, max: 100, show: false },
    tooltip: {
      confine: true,
      // 完整标签（枚举原文）+ 计数；参数类型经 unknown 收窄（echarts 联合类型限制）
      formatter: (p: unknown) => {
        const d = p as { data?: { fullLabel?: string; count?: number | null } }
        if (!d?.data?.fullLabel) return ''
        return `${d.data.fullLabel}：${d.data.count ?? '—'}`
      },
    },
    series: [
      {
        type: 'lines',
        coordinateSystem: 'cartesian2d',
        // 流动粒子：管道有数据在流的可视化（业界 workflow 图通行效果）
        effect: { show: true, period: 4, trailLength: 0.2, symbol: 'arrow', symbolSize: 7, color: '#60a5fa' },
        lineStyle: { color: '#cbd5e1', width: 1.5, curveness: 0 },
        data: EDGES.map((e) => ({
          coords: [POS[e.from], POS[e.to]],
          lineStyle: e.dashed ? { type: 'dashed', color: '#fca5a5' } : undefined,
          effect: e.dashed ? { color: '#f87171' } : undefined,
        })) as LinesSeriesOption['data'],
      },
      {
        type: 'scatter',
        symbolSize: 58,
        label: {
          show: true,
          position: 'inside',
          color: '#fff',
          fontSize: 12,
          fontWeight: 600,
          lineHeight: 16,
          formatter: (p: unknown) => {
            const d = p as { data?: { short?: string; count?: number | null } }
            return `${d?.data?.short ?? ''}\n${d?.data?.count ?? '—'}`
          },
        },
        data: [] as ScatterSeriesOption['data'],
      },
    ],
  }
  chart.setOption(baseOption)

  const observer = new ResizeObserver(() => chart.resize())
  observer.observe(el)

  return {
    update(stations: FlowStation[]) {
      const scatterData = stations
        .filter((s) => POS[s.key])
        .map((s) => ({
          key: s.key,
          short: SHORT[s.key] ?? s.key,
          fullLabel: s.label,
          count: s.count,
          value: POS[s.key],
          itemStyle: {
            color: TONE_COLOR[s.tone],
            borderWidth: s.suggested ? 3 : 0,
            borderColor: s.suggested ? '#1d4ed8' : 'transparent',
            shadowBlur: s.suggested ? 12 : 0,
            shadowColor: s.suggested ? 'rgba(59, 130, 246, 0.55)' : 'transparent',
          },
        }))
      chart.setOption({ series: [{}, { data: scatterData }] } as Option)
    },
    destroy() {
      observer.disconnect()
      chart.dispose()
    },
  }
}
