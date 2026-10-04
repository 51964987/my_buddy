<script setup lang="ts">
/** 总览（运维者/整理监工姿态）：流程漏斗、首次向导、状态摘要、error 巡检重跑、索引与向量状态。 */
import { computed, onMounted, ref } from 'vue'
import { api, type StatusInfo, type WikiCard } from '../api'
import GuideWizard from '../components/GuideWizard.vue'

const st = ref<StatusInfo | null>(null)
const error = ref('')
const rerunning = ref('')

// ---------- 流程漏斗 + 首次向导（§11.8 v0.45，纯前端：数据复用 /api/status + /api/wiki，不加聚合端点） ----------
const GUIDE_KEY = 'kb_guide_done'
const guideShow = ref(false)

function closeGuide() {
  guideShow.value = false
  try {
    localStorage.setItem(GUIDE_KEY, '1')
  } catch {
    /* 隐私模式等存储不可用时静默：向导仅是偏好，不影响功能 */
  }
}
function replayGuide() {
  guideShow.value = true
}

// wiki 卡计数（v0.46 同时保留卡清单供卡类型细分）：draft/promoted 两态；取不到时显示 — 不阻塞页面
const wikiCards = ref<WikiCard[] | null>(null)
const wikiCounts = computed<{ draft: number | null; promoted: number | null }>(() => {
  if (!wikiCards.value) return { draft: null, promoted: null }
  return {
    draft: wikiCards.value.filter((c) => c.status === 'draft').length,
    promoted: wikiCards.value.filter((c) => c.status === 'promoted').length,
  }
})

async function loadFunnelDetails() {
  try {
    const r = await api.get<{ cards: WikiCard[] }>('/api/wiki')
    wikiCards.value = r.cards
  } catch {
    wikiCards.value = null
  }
}

function scrollToPanel(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

interface Station {
  key: string
  label: string
  count: number | null
  tone: 'ok' | 'todo' | 'danger'
  hint: string
  route?: string
  onClick?: () => void
}

const stations = computed<Station[]>(() => {
  if (!st.value) return []
  const s = st.value
  const errCount = s.inbox.error + s.enrich.errors.length
  return [
    {
      key: 'inbox',
      label: '投递待处理（inbox）',
      count: s.inbox.inbox,
      tone: s.inbox.inbox > 0 ? 'todo' : 'ok',
      hint: '归一化自动进行，无需人工操作；失败条目见 error 巡检',
    },
    {
      key: 'normalized',
      label: '待整理（normalized）',
      count: s.enrich.pending,
      tone: s.enrich.pending > 0 ? 'todo' : 'ok',
      hint: 'AI 整理默认手动：点这里到下方「AI 整理」跑一批，或去文档树按页试跑',
      onClick: () => scrollToPanel('panel-ai'),
    },
    {
      key: 'draft',
      label: '待审核（draft）',
      count: wikiCounts.value.draft,
      tone: (wikiCounts.value.draft ?? 0) > 0 ? 'todo' : 'ok',
      hint: 'AI 产出的草稿卡，须人工晋升后才可被检索/图谱引用',
      route: '/curation',
    },
    {
      key: 'promoted',
      label: '已晋升（promoted）',
      count: wikiCounts.value.promoted,
      tone: 'ok',
      hint: '已人工确认的卡片，可被检索与图谱引用',
      route: '/curation',
    },
    {
      key: 'error',
      label: 'error 巡检',
      count: errCount,
      tone: errCount > 0 ? 'danger' : 'ok',
      hint: '管道失败条目：可重跑/复活或丢弃',
      onClick: () => scrollToPanel('panel-error'),
    },
  ]
})

// 建议动作：沿管道方向第一处非零站（inbox 自动进行不算建议；error 属警示旁路单独醒目）
const suggestedKey = computed(() => {
  for (const k of ['normalized', 'draft'] as const) {
    const s = stations.value.find((x) => x.key === k)
    if (s && (s.count ?? 0) > 0) return k
  }
  return null
})

async function load() {
  try {
    st.value = await api.get<StatusInfo>('/api/status')
    error.value = ''
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
  void loadFunnelDetails()
  void loadRecentLogs()
}

// ---------- 站点细分（展开下钻，§11.8 ③ v0.46） ----------
// 跳转纪律：没有着陆页的 segment 只展示不硬造入口——首版全部只读。
interface Segment {
  label: string
  count: number
}

// 投递通道 / 卡类型的中文角色词标签；未知值原样展示（不丢计数）
const ENTRY_LABELS: Record<string, string> = {
  browser_ext: '浏览器扩展',
  mobile_share: '手机分享',
  crawler: '爬虫',
  cli: '命令行',
}
const CARD_TYPE_LABELS: Record<string, string> = {
  summary: '摘要卡',
  concept: '概念卡',
  entity: '实体卡',
}

function groupCount(values: readonly (string | null | undefined)[] | null | undefined, labels?: Record<string, string>): Segment[] {
  const raw: Record<string, number> = {}
  if (!values) return []
  for (const v of values) {
    const k = v || 'unknown'
    raw[k] = (raw[k] ?? 0) + 1
  }
  return Object.entries(raw).map(([k, count]) => ({ label: labels?.[k] ?? k, count }))
}

// 细分分组（v0.48 起支持多组）：region/platform 是「待整理」站的两个可行动维度
const REGION_LABELS: Record<string, string> = {
  collections: '集合镜像页',
  sources: '单条沉淀',
}

interface SegmentGroup {
  label: string
  items: Segment[]
}

function segmentsGroups(key: string): SegmentGroup[] {
  switch (key) {
    case 'inbox':
      return [{ label: '投递通道', items: groupCount(Object.entries(st.value?.inbox.by_entry ?? {}).flatMap(([k, n]) => Array<string>(n).fill(k)), ENTRY_LABELS) }]
    case 'normalized':
      // 与 pending 同源：status 的 enrich.pending_by_region / pending_by_platform（含 D 类集合页）
      return [
        {
          label: '来源区域',
          items: groupCount(Object.entries(st.value?.enrich.pending_by_region ?? {}).flatMap(([k, n]) => Array<string>(n).fill(k)), REGION_LABELS),
        },
        {
          label: '平台',
          items: groupCount(Object.entries(st.value?.enrich.pending_by_platform ?? {}).flatMap(([k, n]) => Array<string>(n).fill(k))),
        },
      ]
    case 'draft':
      return [{ label: '卡类型', items: groupCount(wikiCards.value?.filter((c) => c.status === 'draft').map((c) => c.type), CARD_TYPE_LABELS) }]
    case 'promoted':
      return [{ label: '卡类型', items: groupCount(wikiCards.value?.filter((c) => c.status === 'promoted').map((c) => c.type), CARD_TYPE_LABELS) }]
    case 'error':
      return [
        {
          label: '失败阶段',
          items: [
            ...groupCount(st.value?.errors.map((e) => e.error_stage ? `normalize/${e.error_stage}` : 'normalize/未知')),
            ...groupCount(st.value?.enrich.errors.map(() => 'enrich')),
          ],
        },
      ]
    default:
      return []
  }
}

// 手风琴式：同时只展开一站
const segOpen = ref<string | null>(null)
function toggleSeg(key: string) {
  segOpen.value = segOpen.value === key ? null : key
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

// ---------- 集合元数据编辑（v0.50 PATCH 通道） ----------
// 改入口 URL 只修正回查元数据，不触发重抓；换平台（detect_platform 不一致）后端 400
const editingId = ref<string | null>(null)
const editName = ref('')
const editUrl = ref('')
const savingColl = ref(false)
const collError = ref('')

function startEdit(c: { id: string; name: string | null; entry_url?: string | null }) {
  editingId.value = c.id
  editName.value = c.name ?? ''
  editUrl.value = c.entry_url ?? ''
  collError.value = ''
}
function cancelEdit() {
  editingId.value = null
  collError.value = ''
}
async function saveEdit() {
  if (!editingId.value) return
  savingColl.value = true
  collError.value = ''
  try {
    await api.patch(`/api/collections/${editingId.value}`, { name: editName.value, entry_url: editUrl.value })
    editingId.value = null
    await load()
  } catch (e) {
    collError.value = String(e instanceof Error ? e.message : e)
  } finally {
    savingColl.value = false
  }
}

// ---------- AI 整理：手动跑一批（§5.1 v0.29 触发门控） ----------
// 试跑（dry-run）在文档树页按页进行（那里能选具体页面）；总览只提供"跑一批"并展示逐条结果。

// 最近整理记录（v0.48 活动流）：回答"刚发生了什么"，与审核台同源（/api/enrich/logs）
interface EnrichLogEntry {
  at: string | null
  outcome: string
  message?: string | null
  attempts?: number | null
  entry_id: string
  title: string
}
const recentLogs = ref<EnrichLogEntry[] | null>(null)
async function loadRecentLogs() {
  try {
    const r = await api.get<{ logs: EnrichLogEntry[] }>('/api/enrich/logs?limit=5')
    recentLogs.value = r.logs
  } catch {
    recentLogs.value = null
  }
}

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

onMounted(() => {
  void load()
  // 首次向导：仅当 localStorage 无完成标记时弹出（§11.8）
  try {
    if (!localStorage.getItem(GUIDE_KEY)) guideShow.value = true
  } catch {
    /* 存储不可用则不自动弹，可经「重新查看指引」手动打开 */
  }
})
</script>

<template>
  <div v-if="error" class="alert error">{{ error }}</div>

  <template v-if="st">
    <!-- 流程漏斗（§11.8 v0.45）：管道状态机的常驻可视化，站点可跳转对应操作位 -->
    <div class="panel funnel-panel">
      <div class="funnel-head">
        <h2>流程漏斗</h2>
        <button class="funnel-replay" @click="replayGuide">重新查看指引</button>
      </div>
      <div class="funnel-row">
        <template v-for="(s, i) in stations" :key="s.key">
          <span v-if="i > 0" class="funnel-arrow" aria-hidden="true">→</span>
          <div class="funnel-col">
            <component
              :is="s.route ? 'router-link' : 'button'"
              v-bind="s.route ? { to: s.route } : { type: 'button' }"
              class="funnel-station"
              :class="[s.tone, { suggested: s.key === suggestedKey }]"
              :title="s.hint"
              @click="s.onClick?.()"
            >
              <div class="num">{{ s.count ?? '—' }}</div>
              <div class="label">{{ s.label }}</div>
              <div v-if="s.key === suggestedKey" class="suggest-tag">建议先处理</div>
            </component>
            <!-- 站点细分开关（§11.8 ③ v0.46）：仅计数 > 0 的站可展开下钻 -->
            <button
              v-if="(s.count ?? 0) > 0"
              class="seg-toggle"
              :aria-expanded="segOpen === s.key"
              @click.stop="toggleSeg(s.key)"
            >
              {{ segOpen === s.key ? '收起 ▴' : '细分 ▾' }}
            </button>
          </div>
        </template>
      </div>
      <!-- segment 计数条：手风琴式同时只展开一站；只读（无着陆页不硬造入口） -->
      <div v-if="segOpen" class="seg-panel">
        <template v-for="g in segmentsGroups(segOpen)" :key="g.label">
          <span class="seg-title">{{ g.label }}：</span>
          <span v-for="seg in g.items" :key="seg.label" class="seg-chip">
            {{ seg.label }} <b>{{ seg.count }}</b>
          </span>
        </template>
        <span v-if="!segmentsGroups(segOpen).some((g) => g.items.length)" class="muted">暂无数据</span>
      </div>
      <p class="funnel-note muted">
        {{ (st.inbox.error + st.enrich.errors.length) > 0 ? '存在 error 条目，建议先到 error 巡检面板处理（可重跑/丢弃）。' : suggestedKey ? '归一化自动进行；按「建议先处理」提示逐站消化即可。' : '管道畅通，无待办。' }}
      </p>
    </div>

    <div class="grid-cards">
      <!-- v0.47 统计卡瘦身：与流程漏斗重复的 待整理/投递待处理/error 三张已删（同一数字只出现一次） -->
      <div class="stat-card"><div class="num">{{ st.sources_entries }}</div><div class="label">条目（sources）</div></div>
      <div class="stat-card"><div class="num">{{ st.enrich.enriched }}</div><div class="label">已整理（enriched）</div></div>
      <div class="stat-card"><div class="num">{{ st.archived }}</div><div class="label">去重归档</div></div>
      <div class="stat-card"><div class="num">{{ st.index.docs }}</div><div class="label">索引文档</div></div>
      <div class="stat-card">
        <div class="num">{{ st.ai_enabled ? '开' : '关' }}</div>
        <div class="label">AI 整理开关</div>
      </div>
    </div>

    <div id="panel-ai" class="panel">
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

      <!-- 最近整理记录（v0.48 活动流）：试跑不落盘不记流水（v0.29 设计），故只出现真实整理动作 -->
      <div v-if="recentLogs?.length" class="table-scroll" style="margin-top: 10px">
        <table class="list">
          <thead>
            <tr><th>最近整理记录</th><th style="width: 90px">结果</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="l in recentLogs" :key="`${l.entry_id}-${l.at}`">
              <td>{{ l.title || l.entry_id }}<div class="muted mono" style="font-size: 12px">{{ l.at ?? '' }}</div></td>
              <td><span class="badge" :class="outcomeBadge(l.outcome)">{{ l.outcome }}</span></td>
              <td class="muted" style="font-size: 12px">{{ l.message ?? '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="muted" style="font-size: 12px; margin: 8px 0 0">
        <template v-if="!recentLogs?.length">暂无整理记录（试跑不落盘不记流水）。</template>
        完整日志见 <router-link to="/curation">审核台</router-link>。
      </p>
    </div>

    <!-- error 巡检（v0.47 上移）：异常面板紧跟操作面板，有 error 时第二屏即达 -->
    <div id="panel-error" class="panel">
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
      <h2>集合同步（D 类）</h2>
      <div v-if="st.collections.length" class="table-scroll">
      <table class="list">
        <thead><tr><th>id</th><th>名称</th><th>状态</th><th>页面</th><th>上次同步</th><th></th></tr></thead>
        <tbody>
          <template v-for="c in st.collections" :key="c.id">
            <tr>
              <td class="mono">{{ c.id }}</td>
              <td>
                {{ c.name }}
                <!-- v0.49：原站入口外链（collection.json entry_url），回查源站方便 -->
                <a v-if="c.entry_url" :href="c.entry_url" target="_blank" rel="noopener" class="muted" style="margin-left: 8px; font-size: 12px">原站 ↗</a>
              </td>
              <td><span class="badge" :class="c.state === 'ok' ? 'st-enriched' : 'st-error'">{{ c.state }}</span></td>
              <td>{{ c.pages }}</td>
              <td class="muted mono">{{ c.last_synced_at ?? '—' }}</td>
              <td><button v-if="editingId !== c.id" @click="startEdit(c)">编辑</button></td>
            </tr>
            <!-- v0.50 行内编辑：名称 + 入口 URL，保存走 PATCH 通道 -->
            <tr v-if="editingId === c.id">
              <td colspan="6">
                <div class="coll-edit">
                  <label>名称 <input v-model="editName" style="width: 200px" /></label>
                  <label>入口 URL <input v-model="editUrl" class="mono" style="width: 400px" /></label>
                  <button class="primary" :disabled="savingColl" @click="saveEdit">{{ savingColl ? '保存中…' : '保存' }}</button>
                  <button :disabled="savingColl" @click="cancelEdit">取消</button>
                </div>
                <div v-if="collError" class="alert error" style="margin: 6px 0 0">{{ collError }}</div>
                <p class="muted" style="font-size: 12px; margin: 6px 0 0">
                  改入口 URL 只修正回查元数据，不会触发重新抓取；换平台（不同站点）须重新注册 collection。
                </p>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
      </div>
      <p v-else class="muted">未注册 collection（注册见 <router-link to="/docs">文档树</router-link> 页表单）。</p>
    </div>
  </template>

  <GuideWizard v-if="guideShow" @done="closeGuide" />
</template>

<style scoped>
/* 流程漏斗（§11.8 v0.45） */
.funnel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.funnel-head h2 {
  margin: 0;
}
.funnel-row {
  display: flex;
  align-items: stretch;
  gap: 8px;
  flex-wrap: wrap;
}
.funnel-arrow {
  align-self: center;
  color: #b0b0b0;
  font-size: 16px;
}
.funnel-station {
  position: relative;
  flex: 1;
  min-width: 130px;
  text-align: center;
  border: 1px solid #e0e0e0;
  border-radius: 8px;
  padding: 10px 8px 12px;
  background: #fafafa;
  cursor: pointer;
  color: inherit;
  font: inherit;
  text-decoration: none;
  display: block;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.funnel-station:hover {
  border-color: #3b82f6;
  box-shadow: 0 1px 6px rgba(59, 130, 246, 0.25);
}
.funnel-station .num {
  font-size: 22px;
  font-weight: 600;
  line-height: 1.2;
}
.funnel-station .label {
  font-size: 12px;
  color: #666;
  margin-top: 2px;
}
.funnel-station.ok .num {
  color: #9aa0a6;
}
.funnel-station.todo .num {
  color: #2563eb;
}
.funnel-station.todo {
  border-color: #bfdbfe;
  background: #eff6ff;
}
.funnel-station.danger .num {
  color: #dc2626;
}
.funnel-station.danger {
  border-color: #fecaca;
  background: #fef2f2;
}
.funnel-station.suggested {
  box-shadow: 0 0 0 2px #3b82f6 inset;
}
.suggest-tag {
  position: absolute;
  top: -9px;
  left: 50%;
  transform: translateX(-50%);
  background: #3b82f6;
  color: #fff;
  font-size: 11px;
  border-radius: 8px;
  padding: 1px 8px;
  white-space: nowrap;
}
.funnel-note {
  margin: 10px 0 0;
  font-size: 12px;
}
/* 站点细分（v0.46 展开下钻） */
.funnel-col {
  flex: 1;
  min-width: 130px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.seg-toggle {
  align-self: center;
  border: none;
  background: none;
  color: #3b82f6;
  font-size: 12px;
  cursor: pointer;
  padding: 2px 6px;
}
.seg-toggle:hover {
  text-decoration: underline;
}
.seg-panel {
  margin-top: 10px;
  padding: 8px 12px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12.5px;
}
.seg-title {
  color: #666;
}
.seg-chip {
  background: #fff;
  border: 1px solid #dbeafe;
  border-radius: 12px;
  padding: 2px 10px;
}
.seg-chip b {
  color: #2563eb;
}
/* 集合行内编辑（v0.50） */
.coll-edit {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 13px;
}
.coll-edit input {
  margin-left: 4px;
}
.funnel-replay {
  font-size: 12.5px;
}
</style>
