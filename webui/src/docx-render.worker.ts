/**
 * docx 渲染 worker 入口（§6 v0.39）：渲染/序列化（marked 词法分析、块级构建、Packer.toBlob
 * OOXML 序列化+压缩）是秒级同步长任务，占死主线程会导致 Vue 无法重绘——业界口径
 * ">50ms 任务不进主线程"。本 worker 收渲染任务，进度经 postMessage 回传，Blob 回传主线程。
 * 注意：worker 内无 localStorage，image API 鉴权 token 由主线程随任务传入。
 */
import { renderDocxBlob, type RenderOpts, type RenderProgress } from './docx-render'

interface JobRequest {
  jobId: number
  merged: string
  header?: { title?: string; note?: string }
  token?: string
}

// tsconfig 全局含 DOM lib，self 会被推成 Window；收窄成最小 postMessage 形状避免类型冲突
const ctx = self as unknown as { postMessage: (msg: unknown) => void }
const post = (msg: unknown) => ctx.postMessage(msg)

self.onmessage = async (ev: MessageEvent<JobRequest>) => {
  const { jobId, merged, header, token } = ev.data
  const opts: RenderOpts = {
    token,
    onProgress: (p: RenderProgress) => post({ type: 'progress', jobId, progress: p }),
  }
  try {
    const blob = await renderDocxBlob(merged, header, opts)
    post({ type: 'done', jobId, blob })
  } catch (e) {
    post({ type: 'error', jobId, message: String(e instanceof Error ? e.message : e) })
  }
}
