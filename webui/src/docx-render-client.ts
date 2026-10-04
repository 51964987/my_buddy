/**
 * docx 渲染 worker 客户端（§6 v0.39）：主线程侧的 worker 生命周期管理与进度路由。
 * - jobId 路由：单个持久 worker 串行处理任务（渲染本就是 CPU 密集，并发无收益），
 *   逐页转换（全文打包 Word）复用同一 worker，避免 314 次重启实例。
 * - 取消：AbortSignal 触发即 terminate 整个 worker——docx 产物可驻留 worker 内存
 *   数百 MB，terminate 是唯一可靠的即时释放手段；无在途任务时才终止。
 */
import type { RenderProgress } from './docx-render'

type Pending = {
  resolve: (blob: Blob) => void
  reject: (e: Error) => void
  onProgress?: (p: RenderProgress) => void
}

let worker: Worker | null = null
let nextJobId = 1
const pending = new Map<number, Pending>()

function abortError(): Error {
  return new DOMException('导出已取消', 'AbortError')
}

function terminateIfIdle(): void {
  if (worker && pending.size === 0) {
    worker.terminate()
    worker = null
  }
}

function ensureWorker(): Worker {
  if (worker) return worker
  worker = new Worker(new URL('./docx-render.worker.ts', import.meta.url), { type: 'module' })
  worker.onmessage = (ev: MessageEvent) => {
    const { type, jobId, blob, message, progress } = ev.data ?? {}
    const job = pending.get(jobId)
    if (!job) return
    if (type === 'progress') {
      job.onProgress?.(progress as RenderProgress)
    } else if (type === 'done') {
      pending.delete(jobId)
      job.resolve(blob as Blob)
      terminateIfIdle()
    } else if (type === 'error') {
      pending.delete(jobId)
      job.reject(new Error(String(message)))
      terminateIfIdle()
    }
  }
  worker.onerror = (ev) => {
    // worker 级错误（脚本加载失败等）：广播给所有在途任务
    const err = new Error(ev.message || 'docx 渲染 worker 异常')
    for (const [, job] of pending) job.reject(err)
    pending.clear()
    terminateIfIdle()
  }
  return worker
}

/**
 * 在 worker 内渲染 docx（同步阻塞全部发生在 worker 线程）。
 * token：image API 鉴权（worker 内无 localStorage）；onProgress：三阶段进度；
 * signal：取消（terminate worker + reject AbortError）。
 */
export function renderDocxBlobInWorker(
  merged: string,
  header: { title?: string; note?: string } | undefined,
  token: string,
  onProgress?: (p: RenderProgress) => void,
  signal?: AbortSignal,
): Promise<Blob> {
  if (signal?.aborted) return Promise.reject(abortError())
  const w = ensureWorker()
  const jobId = nextJobId++
  return new Promise<Blob>((resolve, reject) => {
    const onAbort = () => {
      if (!pending.delete(jobId)) return
      reject(abortError())
      terminateIfIdle()
    }
    signal?.addEventListener('abort', onAbort, { once: true })
    pending.set(jobId, {
      resolve: (blob) => {
        signal?.removeEventListener('abort', onAbort)
        resolve(blob)
      },
      reject: (e) => {
        signal?.removeEventListener('abort', onAbort)
        reject(e)
      },
      onProgress,
    })
    w.postMessage({ jobId, merged, header, token })
  })
}
