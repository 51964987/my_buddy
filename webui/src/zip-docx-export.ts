/**
 * 全文打包 Word 版（§6 v0.32）：逐页转 docx 后前端打包 zip。
 *
 * 口径：`format=pages` 一次取回全量页面 JSON（zip_path 由后端统一计算，命名单一
 * 事实源；含 error 页占位与 toc_md），逐页复用 docx-render 嵌图渲染器（md→docx
 * 转换实现前端唯一，不在 Python 侧重复），图片经 image API 端点拉取后 ImageRun
 * 嵌入；docx 本身是 zip 容器，入包用 STORE（level 0）不再二次压缩。
 * v0.39：逐页渲染走持久 worker（主线程零阻塞，保序串行复用同一实例），页级进度
 * 带页内拉图明细；signal 取消（当前页 terminate、循环即时中断）。
 */
import { zip } from 'fflate'
import { api, getToken } from './api'
import { renderDocxBlobInWorker } from './docx-render-client'
import type { RenderProgress } from './docx-render'

export interface PagesPayload {
  name: string
  pages: {
    kind: 'page' | 'error' | string
    path: string
    title: string
    zip_path: string
    url: string
    body?: string | null
    error_message?: string
  }[]
  toc_md: string
}

// 与后端 exporter._IMAGE_REF_RE 同形：raw/img-<sha1>.<ext> 相对引用
const IMG_REF_RE = /(!\[[^\]]*\]\()(raw\/img-[^)\s]+)((?:\s+"[^"]*")?\))/g

/** 正文 raw/img-* 引用改写为 image API URL（围栏代码块感知，与后端 _rewrite_image_links 同口径） */
function rewriteImageRefs(body: string, urlFor: (rel: string) => string): string {
  let fence = false
  return body
    .split('\n')
    .map((line) => {
      const stripped = line.trimStart()
      if (stripped.startsWith('```')) {
        fence = !fence
        return line
      }
      if (fence) return line
      return line.replace(IMG_REF_RE, (_m, pre: string, rel: string, post: string) => pre + urlFor(rel) + post)
    })
    .join('\n')
}

/** 扁平文件表 → fflate 嵌套目录结构（按 zip_path 段构建） */
function nestFiles(files: Record<string, [Uint8Array, { level: 0 }]>): Record<string, unknown> {
  const root: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(files)) {
    const segs = key.split('/')
    let cur = root
    for (let i = 0; i < segs.length - 1; i += 1) {
      const seg = segs[i]
      if (typeof cur[seg] !== 'object' || cur[seg] === null) cur[seg] = {}
      cur = cur[seg] as Record<string, unknown>
    }
    cur[segs[segs.length - 1]] = value
  }
  return root
}

export type ZipDocxProgress = (done: number, total: number, detail?: string) => void

export async function exportZipDocx(collectionId: string, onProgress: ZipDocxProgress, signal?: AbortSignal): Promise<Blob> {
  const payload = await api.get<PagesPayload>(`/api/collections/${collectionId}/export?format=pages`)
  if (signal?.aborted) throw new DOMException('导出已取消', 'AbortError')
  const token = getToken()
  const total = payload.pages.length
  const files: Record<string, [Uint8Array, { level: 0 }]> = {
    'toc.md': [new TextEncoder().encode(payload.toc_md), { level: 0 }],
  }
  let done = 0
  for (const p of payload.pages) {
    let md: string
    if (p.kind === 'error') {
      md = `# ${p.title}\n\n该页抓取失败：${p.error_message ?? ''}\n`
    } else if (!p.body) {
      // toc 在册但正文缺失：与 error 页同口径占位（后端 pages 载荷 body=null）
      md = `# ${p.title}\n\n该页抓取失败：正文文件缺失。\n`
    } else {
      const urlFor = (rel: string) => {
        const name = rel.split('/').pop() ?? rel
        return `/api/collections/${collectionId}/image?path=${encodeURIComponent(p.path)}&name=${encodeURIComponent(name)}`
      }
      const lines = [`# ${p.title}`, '']
      if (p.url) lines.push(`[原文](${p.url})`, '')
      lines.push(rewriteImageRefs(p.body, urlFor), '')
      md = lines.join('\n')
    }
    // 渲染进持久 worker（主线程零阻塞）；页内进度映射为明细文案
    const pageNo = done + 1
    const pageProgress = (pr: RenderProgress) => {
      if (pr.stage === 'images' && pr.total) {
        onProgress(done, total, `第 ${pageNo}/${total} 页：拉图 ${pr.done}/${pr.total}`)
      } else if (pr.stage === 'pack') {
        onProgress(done, total, `第 ${pageNo}/${total} 页：打包 docx`)
      }
    }
    const blob = await renderDocxBlobInWorker(md, undefined, token, pageProgress, signal)
    files[`${p.zip_path}.docx`] = [new Uint8Array(await blob.arrayBuffer()), { level: 0 }]
    onProgress(++done, total, `已完成 ${done}/${total} 页`)
  }
  return await new Promise<Blob>((resolve, reject) => {
    zip(nestFiles(files) as Parameters<typeof zip>[0], { level: 0 }, (err, data) =>
      err ? reject(err) : resolve(new Blob([data], { type: 'application/zip' })),
    )
  })
}
