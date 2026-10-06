<script setup lang="ts">
/**
 * 检索主页（消费者姿态）：单一输入框 + 模式下拉（v0.63 单框合并）——
 * 全文检索 / 语义检索（§5.1 v0.17）+ 知识库问答（§5.1 ask 实施口径 v0.61）。
 * 问答为只读消费：SSE 流式（ask.meta/ask.delta/ask.done/ask.error），答案带 [n] 引用，
 * 命中 promoted 实体卡以「相关实体」块展示并可跳图谱页（?q= 过滤定位）。
 * 后端与端点零变更：检索模式走 GET /api/search，问答模式走 POST /api/ask。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api, getToken, type SearchHit } from '../api'
import { renderMarkdown } from '../markdown'

type Action = 'fulltext' | 'semantic' | 'ask'

// 模式记忆（浏览器本地偏好，不进配置文件；存储不可用静默回落默认）
const ACTION_KEY = 'kb_search_action'
function loadAction(): Action {
  try {
    const v = localStorage.getItem(ACTION_KEY)
    if (v === 'fulltext' || v === 'semantic' || v === 'ask') return v
  } catch {
    /* 存储不可用 */
  }
  return 'fulltext'
}

const q = ref('')
const action = ref<Action>(loadAction())
watch(action, (v) => {
  try {
    localStorage.setItem(ACTION_KEY, v)
  } catch {
    /* 存储不可用 */
  }
})

const placeholder = computed(() => {
  if (action.value === 'ask') return '用自然语言提问，基于库内条目与晋升卡片回答…'
  if (action.value === 'semantic') return '输入关键词，按向量近邻检索语义相关文档…'
  return '输入关键词检索全文（FTS5 + jieba 分词）…'
})

// ---------- 检索（既有口径不变） ----------
const hits = ref<SearchHit[]>([])
const searched = ref(false)
const error = ref('')
const loading = ref(false)

async function doSearch() {
  if (!q.value.trim()) return
  loading.value = true
  error.value = ''
  try {
    const data = await api.get<{ results: SearchHit[]; total: number }>(
      `/api/search?q=${encodeURIComponent(q.value)}&mode=${action.value}`,
    )
    hits.value = data.results
    searched.value = true
  } catch (e) {
    hits.value = []
    error.value = String(e instanceof Error ? e.message : e)
  } finally {
    loading.value = false
  }
}

// ---------- 问答（v0.61 逻辑原样保留） ----------
interface AskRef {
  n: number
  rel: string
  kind: string
  entry_id: string
  title: string
  url: string | null
  status: string
}
interface AskEntity {
  id: string
  name: string
  entity_type: string
  aliases: string[]
  matched: string[]
  relations: { type: string; target: string; name: string }[]
}

const asking = ref(false)
const askError = ref('')
const answer = ref('')
const refs = ref<AskRef[]>([])
const entities = ref<AskEntity[]>([])
const retrieval = ref('')
const askDone = ref<{ model: string; elapsed_ms: number } | null>(null)
let aborter: AbortController | null = null

async function doAsk() {
  if (!q.value.trim() || asking.value) return
  asking.value = true
  askError.value = ''
  answer.value = ''
  refs.value = []
  entities.value = []
  retrieval.value = ''
  askDone.value = null
  aborter = new AbortController()
  try {
    const token = getToken()
    const resp = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { 'X-KB-Token': token } : {}) },
      body: JSON.stringify({ q: q.value }),
      signal: aborter.signal,
    })
    if (!resp.ok || !resp.body) {
      let detail = `HTTP ${resp.status}`
      try {
        detail = (await resp.json()).detail || detail
      } catch {
        /* 非 JSON 错误体（如网关），保留状态码 */
      }
      throw new Error(detail)
    }
    // SSE 分帧解析（与 sse.ts 同款口径：event:/data: 行，空行分帧）
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    let streamError = false
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        if (!frame.trim()) continue
        let type = 'message'
        const dataLines: string[] = []
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) type = line.slice(6).trim()
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
        }
        if (!dataLines.length) continue
        let data: Record<string, unknown>
        try {
          data = JSON.parse(dataLines.join('\n'))
        } catch {
          continue
        }
        if (type === 'ask.meta') {
          refs.value = (data.refs as AskRef[]) || []
          entities.value = (data.entities as AskEntity[]) || []
          retrieval.value = String(data.retrieval || '')
        } else if (type === 'ask.delta') {
          answer.value += String(data.delta || '')
        } else if (type === 'ask.done') {
          askDone.value = { model: String(data.model || ''), elapsed_ms: Number(data.elapsed_ms || 0) }
        } else if (type === 'ask.error') {
          askError.value = String(data.error || '生成失败')
          streamError = true
        }
      }
      if (streamError) break
    }
  } catch (e) {
    if ((e as Error).name !== 'AbortError') {
      askError.value = String(e instanceof Error ? e.message : e)
    }
  } finally {
    asking.value = false
    aborter = null
  }
}

function stopAsk() {
  aborter?.abort()
}

function onSubmit() {
  if (action.value === 'ask') void doAsk()
  else void doSearch()
}

const submitLabel = computed(() => {
  if (action.value === 'ask') return asking.value ? '回答中…' : '提问'
  return loading.value ? '检索中…' : '检索'
})

const router = useRouter()
function goGraph(name: string) {
  void router.push({ path: '/graph', query: { q: name } })
}

onBeforeUnmount(() => aborter?.abort())
</script>

<template>
  <!-- 单一输入框 + 模式下拉（v0.63 单框合并） -->
  <div class="panel">
    <h2>检索与问答</h2>
    <form style="display: flex; gap: 10px" @submit.prevent="onSubmit">
      <input v-model="q" type="text" :placeholder="placeholder" style="flex: 1" />
      <select v-model="action" aria-label="检索模式">
        <option value="fulltext">全文检索（FTS5 + jieba）</option>
        <option value="semantic">语义检索（向量）</option>
        <option value="ask">知识库问答（AI 生成）</option>
      </select>
      <button class="primary" type="submit" :disabled="loading || asking">{{ submitLabel }}</button>
      <button v-if="asking" type="button" @click="stopAsk">停止</button>
    </form>
    <p v-if="action === 'ask'" class="muted" style="font-size: 12px; margin-top: 6px">
      答案由 AI 依据库内内容生成，[n] 编号对应下方引用；AI 产出可能不准确，请以引用原文为准。
    </p>

    <div v-if="error" class="alert error" style="margin-top: 8px">{{ error }}</div>
    <div v-if="askError" class="alert error" style="margin-top: 8px">{{ askError }}</div>

    <!-- 问答输出块（仅问答模式渲染） -->
    <template v-if="action === 'ask'">
      <!-- 相关实体（ask.meta 先行到达；点击跳图谱页过滤定位） -->
      <div v-if="entities.length" style="margin-top: 10px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap">
        <span class="muted" style="font-size: 12px">相关实体：</span>
        <button
          v-for="e in entities"
          :key="e.id"
          class="tag"
          type="button"
          style="cursor: pointer"
          :title="e.aliases.length ? `别名：${e.aliases.join('、')}` : e.entity_type"
          @click="goGraph(e.name)"
        >
          {{ e.name }}<template v-if="e.entity_type">（{{ e.entity_type }}）</template>
        </button>
      </div>

      <!-- 流式答案 -->
      <div v-if="answer" class="md-body" style="margin-top: 10px" v-html="renderMarkdown(answer)"></div>
      <div v-else-if="asking && !refs.length" class="muted" style="margin-top: 10px">检索并生成中…</div>

      <!-- 引用清单（编号对应答案 [n]） -->
      <div v-if="refs.length" style="margin-top: 10px">
        <h3 style="font-size: 13px">引用（{{ refs.length }}）</h3>
        <ol style="margin: 4px 0 0 20px; padding: 0">
          <li v-for="r in refs" :key="r.n" style="font-size: 13px; line-height: 1.8">
            <b>{{ r.title || r.entry_id }}</b>
            <span class="badge" :class="r.kind === 'wiki' ? 'ai-mark' : 'st-normalized'" style="margin-left: 6px">
              {{ r.kind === 'wiki' ? 'wiki 卡' : r.status }}
            </span>
            <a v-if="r.url" :href="r.url" target="_blank" rel="noopener noreferrer" style="margin-left: 6px">原文</a>
          </li>
        </ol>
      </div>
      <p v-if="askDone" class="muted" style="font-size: 12px; margin-top: 6px">
        模型 {{ askDone.model }} · 耗时 {{ askDone.elapsed_ms }}ms ·
        检索模式 {{ retrieval === 'hybrid' ? '全文 + 向量混合' : '全文（向量未参与，已自动降级）' }}
      </p>
    </template>
  </div>

  <!-- 命中列表（仅检索模式渲染） -->
  <template v-if="action !== 'ask'">
    <div v-if="loading" class="muted">检索中…</div>
    <div v-if="searched && !loading" class="panel">
      <h2>结果（{{ hits.length }}）</h2>
      <p v-if="!hits.length" class="muted">无命中。</p>
      <!-- 宽表规范（04-ui-constraints）：容器内滚动兜底；v-else 必须上移到包裹元素，留在 table 上会断链 -->
      <div v-else class="table-scroll">
        <table class="list">
          <thead>
            <tr><th>类型</th><th>标题</th><th>摘要</th><th>标签</th><th>状态</th><th v-if="action === 'semantic'">距离</th></tr>
          </thead>
          <tbody>
            <tr v-for="h in hits" :key="h.rel">
              <td><span class="badge" :class="h.kind === 'wiki' ? 'ai-mark' : 'st-normalized'">{{ h.kind === 'wiki' ? 'wiki 卡' : '条目' }}</span></td>
              <td class="cell-main">
                <b>{{ h.title || h.entry_id }}</b>
                <!-- url/rel 为长 ASCII 串不折行，是此前撑破面板的元凶：单行省略 + 悬停看全文 -->
                <div v-if="h.url" class="muted mono cell-ellipsis" :title="h.url">{{ h.url }}</div>
                <div class="muted mono cell-ellipsis" :title="h.rel">{{ h.rel }}</div>
              </td>
              <td class="muted">{{ h.snippet }}</td>
              <td style="min-width: 160px"><span v-for="t in h.tags" :key="t" class="tag">{{ t }}</span></td>
              <td><span class="badge" :class="`st-${h.status}`">{{ h.status }}</span></td>
              <td v-if="action === 'semantic'" class="mono">{{ h.distance }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </template>
</template>
