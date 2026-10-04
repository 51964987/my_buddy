<script setup lang="ts">
/** 总览（运维者/整理监工姿态）：状态摘要、error 巡检重跑、索引与向量状态。 */
import { onMounted, ref } from 'vue'
import { api, type StatusInfo } from '../api'

const st = ref<StatusInfo | null>(null)
const error = ref('')
const rerunning = ref('')

async function load() {
  try {
    st.value = await api.get<StatusInfo>('/api/status')
    error.value = ''
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
}

async function rerun(id: string) {
  rerunning.value = id
  try {
    await api.post(`/api/entries/${id}/rerun`)
    await load()
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  } finally {
    rerunning.value = ''
  }
}

// 丢弃（§4.4 v0.21）：inbox 走 /api/inbox/{id}，sources 走 /api/entries/{id}；物理删除不可恢复
async function discard(id: string, isInbox: boolean) {
  if (!window.confirm(`确认丢弃条目 ${id}？\n将物理删除其全部文件（含原始数据），不可恢复。`)) return
  try {
    await api.delete(isInbox ? `/api/inbox/${id}` : `/api/entries/${id}`)
    await load()
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
}

// ---------- AI 整理：手动跑一批（§5.1 v0.29 触发门控） ----------
// 试跑（dry-run）在文档树页按页进行（那里能选具体页面）；总览只提供"跑一批"并展示逐条结果。
interface BatchResult {
  entry_id: string
  title: string
  outcome: string
  message?: string
}
const batching = ref(false)
const batchResults = ref<BatchResult[] | null>(null)

// outcome（enriched/retry/error/skipped）→ 状态徽标类；retry 无专属色，落在 normalized 灰
function outcomeBadge(o: string): string {
  return `st-${o === 'retry' ? 'normalized' : o}`
}

async function runBatch() {
  if (!window.confirm('确认跑一批 AI 整理？将按 batch_size 处理待整理条目（逐条调用模型，耗时较长）。')) return
  batching.value = true
  error.value = ''
  batchResults.value = null
  try {
    const r = await api.post<{
      batch: { scanned: number; enriched: number; retry: number; error: number; results: BatchResult[] }
    }>('/api/enrich/run')
    batchResults.value = r.batch.results
    await load()
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  } finally {
    batching.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-if="error" class="alert error">{{ error }}</div>

  <template v-if="st">
    <div class="grid-cards">
      <div class="stat-card"><div class="num">{{ st.sources_entries }}</div><div class="label">条目（sources）</div></div>
      <div class="stat-card"><div class="num">{{ st.enrich.pending }}</div><div class="label">待整理（normalized）</div></div>
      <div class="stat-card"><div class="num">{{ st.enrich.enriched }}</div><div class="label">已整理（enriched）</div></div>
      <div class="stat-card"><div class="num">{{ st.inbox.inbox }}</div><div class="label">投递待处理（inbox）</div></div>
      <div class="stat-card"><div class="num">{{ st.inbox.error + st.enrich.errors.length }}</div><div class="label">error 条目</div></div>
      <div class="stat-card"><div class="num">{{ st.archived }}</div><div class="label">去重归档</div></div>
      <div class="stat-card"><div class="num">{{ st.index.docs }}</div><div class="label">索引文档</div></div>
      <div class="stat-card">
        <div class="num">{{ st.ai_enabled ? '开' : '关' }}</div>
        <div class="label">AI 整理开关</div>
      </div>
    </div>

    <div class="panel">
      <h2>AI 整理（v0.29 触发门控：默认手动，试跑先行）</h2>
      <div class="form-row">
        <label>总闸 ai.enabled</label>
        <span>{{ st.ai_enabled ? '已开启' : '关闭' }}</span>
        <label style="margin-left: 20px">触发 trigger_mode</label>
        <span class="badge" :class="st.ai_trigger_mode === 'auto' ? 'st-error' : 'st-normalized'">
          {{ st.ai_trigger_mode === 'auto' ? '自动' : '手动' }}
        </span>
        <span class="hint">手动=只响应页面上的试跑/单条/批量；自动=每 poll_interval 秒扫 batch_size 条</span>
      </div>
      <div v-if="st.ai_breaker?.state === 'open'" class="alert warn" style="margin: 8px 0">
        <b>熔断中</b>：连续 {{ st.ai_breaker.consecutive_failures }} 轮整库无成功，自动整理已暂停（{{
          st.ai_breaker.opened_at
        }}）—— {{ st.ai_breaker.last_error }}。修正模型配置后到「参数设置 → AI 模型」点「恢复自动整理」。
      </div>
      <div class="form-row">
        <button class="primary" :disabled="!st.ai_enabled || batching || !st.enrich.pending" @click="runBatch">
          {{ batching ? '整理中…' : `跑一批（待整理 ${st.enrich.pending}）` }}
        </button>
        <span class="hint">单条与试跑在「文档树」页按页进行；要全量自动整理需在设置页把 trigger_mode 切 auto</span>
      </div>
      <div v-if="batchResults" class="table-scroll" style="margin-top: 10px">
        <table class="list">
          <thead>
            <tr><th>条目</th><th style="width: 110px">结果</th><th>原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in batchResults" :key="r.entry_id">
              <td>{{ r.title || r.entry_id }}</td>
              <td><span class="badge" :class="outcomeBadge(r.outcome)">{{ r.outcome }}</span></td>
              <td class="muted" style="font-size: 12px">{{ r.message ?? '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <h2>索引与向量</h2>
      <div class="form-row">
        <label>全文索引</label>
        <span>{{ st.index.docs }} 篇文档</span>
      </div>
      <div class="form-row">
        <label>向量增强</label>
        <span v-if="!st.index.vector.enabled" class="muted">未启用（index.vector_enabled）</span>
        <span v-else>
          状态 <b>{{ st.index.vector.state }}</b> · 已嵌入 {{ st.index.vector.embedded }} 篇
          <span v-if="st.index.vector.error" class="muted">（{{ st.index.vector.error }}）</span>
        </span>
      </div>
      <div class="form-row">
        <label>上次管道扫描</label>
        <span class="muted mono">{{ st.last_scan ?? '—' }}</span>
      </div>
    </div>

    <div class="panel">
      <h2>error 巡检（可观测 · 可重跑）</h2>
      <div v-if="st.errors.length || st.enrich.errors.length" class="table-scroll">
      <table class="list">
        <thead>
          <tr><th>阶段</th><th>条目</th><th>错误</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="e in st.errors" :key="`in-${e.id}`">
            <td><span class="badge st-error">normalize/{{ e.error_stage }}</span></td>
            <td class="mono">{{ e.id }}<div class="muted cell-ellipsis" :title="e.url">{{ e.url }}</div></td>
            <td class="muted">{{ e.error_message }}</td>
            <td>
              <div class="row-actions">
                <button :disabled="rerunning === e.id" @click="rerun(e.id)">重跑</button>
                <button class="danger" @click="discard(e.id, true)">丢弃</button>
              </div>
            </td>
          </tr>
          <tr v-for="e in st.enrich.errors" :key="`en-${e.id}`">
            <td><span class="badge st-error">enrich</span></td>
            <td>{{ e.title }}<div class="mono muted">{{ e.id }}</div></td>
            <td class="muted">{{ e.error_message }}（已试 {{ e.attempts }} 次）</td>
            <td>
              <div class="row-actions">
                <button :disabled="rerunning === e.id" @click="rerun(e.id)">复活</button>
                <button class="danger" @click="discard(e.id, false)">丢弃</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      </div>
      <p v-else class="muted">无 error 条目。</p>
    </div>

    <div class="panel">
      <h2>集合同步（D 类）</h2>
      <div v-if="st.collections.length" class="table-scroll">
      <table class="list">
        <thead><tr><th>id</th><th>名称</th><th>状态</th><th>页面</th><th>上次同步</th></tr></thead>
        <tbody>
          <tr v-for="c in st.collections" :key="c.id">
            <td class="mono">{{ c.id }}</td>
            <td>{{ c.name }}</td>
            <td><span class="badge" :class="c.state === 'ok' ? 'st-enriched' : 'st-error'">{{ c.state }}</span></td>
            <td>{{ c.pages }}</td>
            <td class="muted mono">{{ c.last_synced_at ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
      </div>
      <p v-else class="muted">未注册 collection（注册见 <router-link to="/docs">文档树</router-link> 页表单）。</p>
    </div>
  </template>
</template>
