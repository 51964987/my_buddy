/** kbserver API 客户端：统一 token 头与错误处理（写操作一律走 API，前端禁直写 kb/）。 */

const TOKEN_KEY = 'kb_token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t)
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers['X-KB-Token'] = token
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const resp = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (resp.status === 401) {
    throw new ApiError(401, '需要 token：请在「参数设置 → 服务」填写与服务端一致的 token')
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const data = await resp.json()
      if (data?.detail) detail = String(data.detail)
    } catch {
      /* 保留状态码信息 */
    }
    throw new ApiError(resp.status, detail)
  }
  return (await resp.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body ?? {}),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body ?? {}),
  delete: <T>(path: string) => request<T>('DELETE', path),
  /** 二进制下载（全文导出 zip / merged）：带 token，错误走同一套 401/409 提示 */
  getBlob: async (path: string): Promise<Blob> => {
    const headers: Record<string, string> = {}
    const token = getToken()
    if (token) headers['X-KB-Token'] = token
    const resp = await fetch(path, { headers })
    if (resp.status === 401) throw new ApiError(401, '需要 token：请在「参数设置 → 服务」填写与服务端一致的 token')
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`
      try {
        const data = await resp.json()
        if (data?.detail) detail = String(data.detail)
      } catch {
        /* 非_json 响应体（zip 等），保留状态码信息 */
      }
      throw new ApiError(resp.status, detail)
    }
    return resp.blob()
  },
  /**
   * 流式下载并回报进度（v0.39）：`resp.blob()` 没有进度语义，大文件（143MB zip）传输期间
   * 前端无任何反馈。业界通行做法（axios onDownloadProgress 同款）：`resp.body.getReader()`
   * 逐 chunk 累加 + Content-Length 换算百分比；无 Content-Length 时按已接收字节。
   * signal 贯穿 fetch 与读取循环，取消即时生效。
   */
  getBlobWithProgress: async (
    path: string,
    onProgress?: (receivedBytes: number, totalBytes: number | null) => void,
    signal?: AbortSignal,
  ): Promise<Blob> => {
    const headers: Record<string, string> = {}
    const token = getToken()
    if (token) headers['X-KB-Token'] = token
    const resp = await fetch(path, { headers, signal })
    if (resp.status === 401) throw new ApiError(401, '需要 token：请在「参数设置 → 服务」填写与服务端一致的 token')
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`
      try {
        const data = await resp.json()
        if (data?.detail) detail = String(data.detail)
      } catch {
        /* 非_json 响应体（zip 等），保留状态码信息 */
      }
      throw new ApiError(resp.status, detail)
    }
    if (!resp.body) return resp.blob() // 无流式响应体（极端环境）退回一次性缓冲
    const total = Number(resp.headers.get('Content-Length')) || null
    const reader = resp.body.getReader()
    const chunks: Uint8Array[] = []
    let received = 0
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      chunks.push(value)
      received += value.length
      onProgress?.(received, total)
    }
    return new Blob(chunks as BlobPart[])
  },
}

// ---------------- 视图模型类型（对齐后端返回） ----------------

export type EntryStatus = 'inbox' | 'normalized' | 'enriched' | 'error'

export interface EntryItem {
  id: string
  title: string
  url: string | null
  platform: string
  source_type: string
  status: EntryStatus
  captured_at: string
  tags: string[]
  path: string
}

export interface SearchHit {
  rel: string
  kind: 'entry' | 'wiki'
  entry_id: string
  title: string
  url: string
  date: string
  tags: string[]
  status: string
  snippet: string
  score?: number
  distance?: number
}

export interface WikiCard {
  id: string
  rel: string
  type: string
  ai_generated: boolean
  status: 'draft' | 'promoted' | string
  sources: string[]
  confidence: number | null
  created_at: string | null
  /** v0.35：人工修订草稿的时间；非空即表示"AI 生成 · 已人工修订" */
  edited_at?: string | null
  model: string | null
  title: string
  body?: string
}

export interface StatusInfo {
  // by_entry（v0.46）：按投递通道计数（含 error 项），总览页漏斗细分数据源
  inbox: { inbox: number; error: number; by_entry?: Record<string, number> }
  archived: number
  errors: { id: string; entry?: string; url?: string; error_stage?: string; error_message?: string }[]
  sources_entries: number
  collections: {
    id: string
    name: string | null
    /** collection.json 的 entry_url（§4.3）：站点入口，总览集合同步面板外链用（v0.49） */
    entry_url?: string | null
    state: string | null
    pages: number
    last_synced_at: string | null
    last_error: string | null
  }[]
  enrich: {
    pending: number
    enriched: number
    errors: { id: string; title: string; attempts: number; error_message: string }[]
    last_scan: string | null
    /** v0.46：与 pending 同源按 platform 分组（含 D 类集合页），总览页漏斗细分数据源 */
    pending_by_platform?: Record<string, number>
    /** v0.48：与 pending 同源按来源区域分组（collections=集合镜像页 / sources=单条沉淀） */
    pending_by_region?: Record<string, number>
  }
  ai_enabled: boolean
  // v0.29 触发门控：manual（默认，只响应手动指令）/ auto（周期扫库）+ 熔断运行态
  ai_trigger_mode: 'manual' | 'auto' | string
  ai_breaker: {
    state: 'closed' | 'open' | string
    consecutive_failures: number
    opened_at: string | null
    last_error: string | null
  }
  guard_violations: Record<string, number>
  last_scan: string | null
  version: string
  index: {
    docs: number
    vector: { enabled: boolean; state: string; embedded: number; error: string | null }
  }
}

export interface TocPage {
  path: string
  title: string
  url: string
  document_id: number
  content_hash: string
  index: number // 源站排序键（v0.25）
}

// 目录节点标题（v0.24：目录显示源站中文名，路径段为清洗后英文 code）
export interface TocDir {
  path: string
  title: string
  index: number // 源站排序键（v0.25）
}

export interface Facets {
  platforms: string[]
  source_types: string[]
}
