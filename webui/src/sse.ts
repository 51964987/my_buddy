/**
 * SSE 事件流客户端（§11.8 ⑥ v0.53）：全局单例连接。
 *
 * - 用 fetch 流式读取而非原生 EventSource：后者不能带 X-KB-Token 自定义头；
 * - 自动重连（指数退避，1s 起步上限 30s）；重连成功（hello 帧）向订阅者发
 *   `_reconnected` 事件，由订阅方整体重拉兜底（后端不做断线期间事件补发）；
 * - 按事件类型分发；'*' 订阅所有类型；
 * - 首个订阅者出现时才建立连接（lazy start），全部退订后保持连接（本地单用户
 *   常驻，避免频繁重建；服务不可达时退避循环静默重试）。
 */

import { getToken } from './api'

export interface KbEvent {
  type: string
  [key: string]: unknown
}

type Handler = (ev: KbEvent) => void

const handlers = new Map<string, Set<Handler>>()
let started = false
let backoffMs = 1000
let buffer = ''

function dispatch(ev: KbEvent) {
  handlers.get(ev.type)?.forEach((h) => {
    try {
      h(ev)
    } catch {
      /* 单个订阅者异常不影响其他订阅者 */
    }
  })
  handlers.get('*')?.forEach((h) => {
    try {
      h(ev)
    } catch {
      /* 同上 */
    }
  })
}

/** 解析一帧 SSE（以空行分隔）：取 event: 与 data: 行。 */
function handleFrame(frame: string) {
  let type = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) type = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!dataLines.length) return // 心跳注释帧（": ping"）无 data
  if (type === 'hello') {
    // 连接（重）建立：通知订阅方整体重拉，弥补断线期间错过的事件
    dispatch({ type: '_reconnected' })
    return
  }
  let data: unknown = undefined
  try {
    data = JSON.parse(dataLines.join('\n'))
  } catch {
    return
  }
  dispatch(data as KbEvent)
}

async function connectLoop() {
  for (;;) {
    try {
      const headers: Record<string, string> = {}
      const token = getToken()
      if (token) headers['X-KB-Token'] = token
      const resp = await fetch('/api/events', { headers })
      if (!resp.ok || !resp.body) throw new Error(`SSE HTTP ${resp.status}`)
      backoffMs = 1000 // 连接成功即重置退避
      buffer = ''
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          if (frame.trim()) handleFrame(frame)
        }
      }
    } catch {
      /* 网络/服务不可达：走退避重连 */
    }
    await new Promise((r) => setTimeout(r, backoffMs))
    backoffMs = Math.min(backoffMs * 2, 30000)
  }
}

function ensureStarted() {
  if (!started) {
    started = true
    void connectLoop()
  }
}

/** 订阅事件（types 数组；含 '_reconnected' 与 '*'）。返回退订函数。 */
export function subscribeEvents(types: string[], handler: Handler): () => void {
  ensureStarted()
  for (const t of types) {
    let set = handlers.get(t)
    if (!set) {
      set = new Set()
      handlers.set(t, set)
    }
    set.add(handler)
  }
  return () => {
    for (const t of types) {
      const set = handlers.get(t)
      set?.delete(handler)
      if (set && set.size === 0) handlers.delete(t)
    }
  }
}
