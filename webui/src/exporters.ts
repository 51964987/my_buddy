/**
 * 目录树导出（方案文档 §6 v0.28/v0.30/v0.31）。
 *
 * 目录索引（v0.28，树+链接；v0.31 下线 CSV，仅 Markdown 大纲）：buildMarkdown——前端本地生成、纯只读。
 * 全文导出（v0.30，"目录树=大纲、正文=内联"）：zip / merged 由后端
 * `GET /api/collections/{id}/export` 产出；docx 为 merged（images=api 变体）的嵌图转换，
 * 渲染器在 docx-render.ts（docx/marked 体积大，动态 import 分包、仅导出时加载）。
 */
import type { TocPage } from './api'
import { getToken } from './api'
import type { RenderProgress } from './docx-render'

export interface ExportNode {
  name: string
  path: string
  page: TocPage | null
  children: ExportNode[]
}

/** 触发浏览器下载（Blob + a[download]，业界前端导出通行做法） */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

/** 导出文件名：toc-<collection-id>-<yyyymmdd>.<ext> */
export function exportFilename(collectionId: string, ext: string, now = new Date()): string {
  const d = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}`
  return `toc-${collectionId}-${d}.${ext}`
}

/* ---------- 目录索引：Markdown（v0.28） ---------- */

function countPages(nodes: ExportNode[]): number {
  let n = 0
  for (const node of nodes) {
    if (node.page) n += 1
    n += countPages(node.children)
  }
  return n
}

function walk(nodes: ExportNode[], level: number, fn: (node: ExportNode, level: number) => void): void {
  for (const node of nodes) {
    fn(node, level)
    walk(node.children, level + 1, fn)
  }
}

export function buildMarkdown(tree: ExportNode[], collectionName: string, now = new Date()): string {
  const lines: string[] = [
    `# ${collectionName} 目录树`,
    '',
    `> 导出于 ${now.toLocaleDateString('zh-CN')}，共 ${countPages(tree)} 页。`,
  ]
  walk(tree, 0, (node, level) => {
    const indent = '  '.repeat(level)
    if (node.page) {
      const title = node.page.title || node.name
      lines.push(`${indent}- [${title}](${node.page.url})`)
    } else {
      lines.push(`${indent}- **${node.name}**`)
    }
  })
  return lines.join('\n') + '\n'
}

/* ---------- 全文 docx（v0.31 嵌图版）：转发至按需加载的 worker 渲染链 ---------- */

/**
 * v0.39：渲染/序列化进 worker（主线程零阻塞），进度三阶段（拉图 n/m → 渲染 → 打包），
 * signal 取消（terminate worker）。token 由主线程读取传入（worker 内无 localStorage）。
 */
export async function buildDocxFromMarkdown(
  merged: string,
  collectionName: string,
  opts: { onProgress?: (p: RenderProgress) => void; signal?: AbortSignal } = {},
): Promise<Blob> {
  const { renderDocxBlobInWorker } = await import('./docx-render-client')
  return renderDocxBlobInWorker(
    merged,
    {
      title: `${collectionName}（全文导出）`,
      note: '由 kb buddy 全文导出生成；个别图片因格式限制（webp 等）或拉取失败以占位标注，完整图片见 zip 导出版本。',
    },
    getToken(),
    opts.onProgress,
    opts.signal,
  )
}
