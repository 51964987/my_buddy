<script setup lang="ts">
/**
 * AI 审核台（整理监工姿态，§4.5 v0.35；v0.55 按条目聚合）：
 * 审核单元 = 一次 AI 整理的源条目——摘要卡为主行，关联实体/概念卡折叠为组内明细
 * （归属按实体卡 sources[0]，即首次抽出条目；同名跨条目合并的实体卡只出现在首源组）。
 * 组级「整条晋升 / 整条打回」+ 跨组勾选批量处置（POST /api/wiki/batch 逐条收口）。
 * 卡详情口径不变（v0.35）：AI 元信息 + 源条目 AI 摘要与标签（摘要卡按 sources 联查）+ 渲染正文 + 原文入口。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { api, type WikiCard } from '../api'
import { renderMarkdown } from '../markdown'

const emit = defineEmits<{ (e: 'refresh-badge'): void }>()

const cards = ref<WikiCard[]>([])
const logs = ref<Record<string, unknown>[]>([])
const logPage = ref(1) // v0.60 分页：默认每页 5 条（业界分页器口径，参考 AntD/Element）
const logPageSize = ref(5)
const logTotal = ref(0)
const error = ref('')
const notice = ref('')
const busy = ref('')
const expanded = ref<string>('')
const openedGroup = ref<string>('')
const checked = ref<Set<string>>(new Set()) // 勾选的审核单元（组键 = 摘要卡 sources[0]）

const TYPE_LABELS: Record<string, string> = { summary: '摘要卡', entity: '实体卡', concept: '概念卡' }

/** 审核单元：一条源条目 = 摘要卡主行 + 折叠的实体/概念明细 */
interface EntryGroup {
  key: string
  summary: WikiCard
  related: WikiCard[]
}

const groups = computed<EntryGroup[]>(() => {
  const byKey = new Map<string, EntryGroup>()
  for (const s of cards.value.filter((c) => c.type === 'summary')) {
    const key = s.sources[0] ?? s.id
    byKey.set(key, { key, summary: s, related: [] })
  }
  for (const c of cards.value) {
    if (c.type === 'summary') continue
    byKey.get(c.sources[0] ?? '')?.related.push(c)
  }
  return [...byKey.values()].sort((a, b) => (b.summary.created_at ?? '').localeCompare(a.summary.created_at ?? ''))
})

/** 无主明细卡：首源条目已无摘要卡（如摘要卡被单独删除），单列处置，正常流程不产生 */
const orphans = computed(() => {
  const keys = new Set(cards.value.filter((c) => c.type === 'summary').map((c) => c.sources[0] ?? c.id))
  return cards.value.filter((c) => c.type !== 'summary' && !keys.has(c.sources[0] ?? ''))
})

const relatedTotal = computed(() => groups.value.reduce((n, g) => n + g.related.length, 0))

// v0.64 分页（与 v0.60 日志分页同口径）：默认每页 5 个审核单元，客户端切页——
// /api/wiki 全量返回且本地库规模小，暂不需服务端分页（业界：本地工具列表客户端切页即可）
const unitPage = ref(1)
const unitPageSize = ref(5)
const unitTotal = computed(() => groups.value.length)
const unitPageCount = computed(() => Math.max(1, Math.ceil(unitTotal.value / unitPageSize.value)))

// 数据收缩（删除/晋升后）当前页越界：回退到最后一页（业界分页器口径）
watch(unitTotal, () => {
  if (unitPage.value > unitPageCount.value) unitPage.value = unitPageCount.value
})

const pagedGroups = computed(() =>
  groups.value.slice((unitPage.value - 1) * unitPageSize.value, unitPage.value * unitPageSize.value),
)

function gotoUnitPage(n: number) {
  if (n >= 1 && n <= unitPageCount.value && n !== unitPage.value) unitPage.value = n
}

function changeUnitPageSize() {
  unitPage.value = 1
}

/** 页码条（与 v0.60 日志分页同口径：首页/末页/当前页±1，间隔以省略号折叠） */
const unitPageItems = computed<(number | '…')[]>(() => {
  const cur = unitPage.value
  const last = unitPageCount.value
  const pages = [...new Set([1, last, cur - 1, cur, cur + 1])]
    .filter((n) => n >= 1 && n <= last)
    .sort((a, b) => a - b)
  const out: (number | '…')[] = []
  let prev = 0
  for (const n of pages) {
    if (n - prev > 1) out.push('…')
    out.push(n)
    prev = n
  }
  return out
})

// 全选/全不选按当前页（业界口径：分页下 select-all 只作用于可见行，AntD/Gmail 同款）
const allChecked = computed(
  () => pagedGroups.value.length > 0 && pagedGroups.value.every((g) => checked.value.has(g.key)),
)

function toggleGroup(key: string) {
  const next = new Set(checked.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  checked.value = next
}

function toggleAll() {
  checked.value = allChecked.value ? new Set() : new Set(pagedGroups.value.map((g) => g.key))
}

function toggleGroupOpen(key: string) {
  openedGroup.value = openedGroup.value === key ? '' : key
}

/** 明细构成标签按组内实际类型动态生成（如「实体卡 9 张」/「实体卡 8 张 + 概念卡 1 张」）——概念卡仅在总览聚合后产出，文案不预设类型全集 */
function relatedLabel(g: EntryGroup): string {
  const counts = new Map<string, number>()
  for (const c of g.related) {
    const t = TYPE_LABELS[c.type] ?? c.type
    counts.set(t, (counts.get(t) ?? 0) + 1)
  }
  return [...counts.entries()].map(([t, n]) => `${t} ${n} 张`).join(' + ')
}

/** 表格扁平行模型：组主行（摘要卡）/ 明细开关行 / 明细行（实体卡），单一 v-for 渲染保证详情行紧跟其卡行 */
type Row =
  | { kind: 'card'; card: WikiCard; group: EntryGroup; role: 'summary' | 'related' }
  | { kind: 'toggle'; group: EntryGroup }

const rows = computed<Row[]>(() => {
  const out: Row[] = []
  for (const g of pagedGroups.value) {
    out.push({ kind: 'card', card: g.summary, group: g, role: 'summary' })
    if (g.related.length) out.push({ kind: 'toggle', group: g })
    if (openedGroup.value === g.key) {
      for (const c of g.related) out.push({ kind: 'card', card: c, group: g, role: 'related' })
    }
  }
  return out
})

const draftRelated = (g: EntryGroup) => g.related.filter((c) => c.status === 'draft').length

/** 组内各卡的处置清单：整条晋升=全部 draft 卡晋升；整条打回=摘要卡打回（源条目复位重生成）+ draft 实体卡删除（重生成时重建）。promoted 实体卡保留——人工已接受过，不因整条打回降级 */
function groupItems(g: EntryGroup, kind: 'promote-all' | 'redo'): { id: string; action: string }[] {
  if (kind === 'promote-all') {
    return [g.summary, ...g.related].filter((c) => c.status === 'draft').map((c) => ({ id: c.id, action: 'promote' }))
  }
  const items = [{ id: g.summary.id, action: 'regenerate' }]
  for (const c of g.related) if (c.status === 'draft') items.push({ id: c.id, action: 'delete' })
  return items
}

async function batchAction(items: { id: string; action: string }[], what: string) {
  busy.value = 'batch'
  error.value = ''
  notice.value = ''
  try {
    const r = await api.post<{ results: { id: string; ok: boolean }[] }>('/api/wiki/batch', { items })
    const fails = r.results.filter((x) => !x.ok).length
    notice.value = fails ? `${what}：${r.results.length - fails} 成功 / ${fails} 失败（明细见下方刷新后的状态列）` : `${what}：共 ${r.results.length} 项`
    checked.value = new Set()
    await load()
    emit('refresh-badge')
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  } finally {
    busy.value = ''
  }
}

function groupPromote(g: EntryGroup) {
  const items = groupItems(g, 'promote-all')
  if (!items.length) return
  batchAction(items, '整条晋升')
}

function groupRedo(g: EntryGroup) {
  if (!window.confirm(`确认整条打回？\n摘要卡删除 + 源条目复位 normalized（AI 重新生成），组内 draft 实体卡一并删除。\n条目：${g.summary.title || g.key}`)) return
  batchAction(groupItems(g, 'redo'), '整条打回')
}

function batchChecked(kind: 'promote-all' | 'redo') {
  const sel = groups.value.filter((g) => checked.value.has(g.key))
  if (!sel.length) return
  if (kind === 'redo' && !window.confirm(`确认批量整条打回 ${sel.length} 条？各条目的 AI 产出将删除并重新生成。`)) return
  batchAction(sel.flatMap((g) => groupItems(g, kind)), kind === 'promote-all' ? `批量整条晋升 ${sel.length} 条` : `批量整条打回 ${sel.length} 条`)
}

/** 展开的卡详情：卡正文 + 源条目 AI 产出（摘要/标签/原文 URL） */
interface Detail {
  body: string
  source?: {
    id: string
    title: string
    url: string | null
    status: string
    aiSummary: string
    tags: string[]
  }
}
const detail = ref<Record<string, Detail>>({})

const editingId = ref('')
const editBody = ref('')

async function loadLogs() {
  try {
    const l = await api.get<{ logs: Record<string, unknown>[]; total: number }>(
      `/api/enrich/logs?page=${logPage.value}&page_size=${logPageSize.value}`,
    )
    // 数据收缩后当前页可能越界：按业界分页器口径回退到最后一页再取一次
    const last = Math.max(1, Math.ceil(l.total / logPageSize.value))
    if (l.total > 0 && logPage.value > last) {
      logPage.value = last
      return loadLogs()
    }
    logs.value = l.logs
    logTotal.value = l.total
    error.value = ''
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
}

async function load() {
  try {
    const [c] = await Promise.all([
      api.get<{ cards: WikiCard[] }>('/api/wiki'),
      loadLogs(),
    ])
    cards.value = c.cards
    error.value = ''
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
}

const logPageCount = computed(() => Math.max(1, Math.ceil(logTotal.value / logPageSize.value)))

/** 页码条（业界口径：首页/末页/当前页±1，间隔以省略号折叠） */
const logPageItems = computed<(number | '…')[]>(() => {
  const cur = logPage.value
  const last = logPageCount.value
  const pages = [...new Set([1, last, cur - 1, cur, cur + 1])]
    .filter((n) => n >= 1 && n <= last)
    .sort((a, b) => a - b)
  const out: (number | '…')[] = []
  let prev = 0
  for (const n of pages) {
    if (n - prev > 1) out.push('…')
    out.push(n)
    prev = n
  }
  return out
})

function gotoLogPage(n: number) {
  if (n < 1 || n > logPageCount.value || n === logPage.value) return
  logPage.value = n
  loadLogs()
}

function changeLogPageSize() {
  logPage.value = 1
  loadLogs()
}

async function act(card: WikiCard, action: 'promote' | 'regenerate' | 'delete') {
  // 危险项二次确认（UI 纪律 3）：打回与删除不可逆
  if (action !== 'promote') {
    const what = action === 'delete' ? '永久删除该卡片' : '删除该卡片并重置源条目为 normalized（AI 将重新生成）'
    if (!window.confirm(`确认${what}？\n卡片：${card.title || card.id}`)) return
  }
  busy.value = `${card.id}:${action}`
  error.value = ''
  notice.value = ''
  try {
    await api.post(`/api/wiki/${card.id}/${action}`)
    notice.value =
      action === 'promote' ? '已晋升为 promoted' : action === 'regenerate' ? '已打回，源条目等待 AI 重新加工' : '已删除'
    await load()
    emit('refresh-badge')
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  } finally {
    busy.value = ''
  }
}

async function toggleBody(card: WikiCard) {
  if (expanded.value === card.id) {
    expanded.value = ''
    editingId.value = ''
    return
  }
  busy.value = `${card.id}:load`
  try {
    const d = await api.get<WikiCard>(`/api/wiki/${card.id}`)
    let source: Detail['source']
    // 源条目 AI 摘要/标签联查只对摘要卡有意义（实体卡正文自带结构化信息）；
    // 源条目已被人工删除时只显示卡本身，不阻塞审核
    if (d.type === 'summary' && d.sources?.[0]) {
      const entryId = d.sources[0]
      try {
        const e = await api.get<{ frontmatter: Record<string, unknown> }>(`/api/entries/${entryId}`)
        const fm = e.frontmatter
        const ai = (fm.ai ?? {}) as Record<string, unknown>
        source = {
          id: entryId,
          title: String(fm.title ?? ''),
          url: (fm.url as string) ?? null,
          status: String(fm.status ?? ''),
          aiSummary: String(ai.summary ?? ''),
          tags: Array.isArray(fm.tags) ? (fm.tags as string[]) : [],
        }
      } catch {
        source = undefined
      }
    }
    detail.value = { ...detail.value, [card.id]: { body: d.body ?? '', source } }
    expanded.value = card.id
    error.value = ''
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  } finally {
    busy.value = ''
  }
}

function startEdit(card: WikiCard) {
  editingId.value = card.id
  editBody.value = detail.value[card.id]?.body ?? ''
}

function cancelEdit() {
  editingId.value = ''
  editBody.value = ''
}

async function saveEdit(card: WikiCard) {
  busy.value = `${card.id}:edit`
  error.value = ''
  notice.value = ''
  try {
    await api.put(`/api/wiki/${card.id}`, { body: editBody.value })
    editingId.value = ''
    notice.value = '已保存修订（卡片标记为「AI 生成 · 已人工修订」）；晋升后才对索引与消费者可见'
    await load()
    emit('refresh-badge')
    await toggleBody(card) // 重新拉详情，让正文与「已人工修订」标记归位
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  } finally {
    busy.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div v-if="error" class="alert error">{{ error }}</div>
  <div v-if="notice" class="alert ok">{{ notice }}</div>

  <div class="panel">
    <h2>wiki 卡审核（AI 产出一律 draft，晋升仅人工触发）</h2>
    <p class="muted" style="margin-top: -4px">
      按条目聚合（v0.55）：每行是一次 AI 整理（审核单元 = 源条目），实体/概念卡折叠在组内明细；
      点标题展开 AI 摘要/标签、渲染正文与原文入口，单卡处置在详情里，组级可整条晋升/打回，勾选多行批量操作。
      分页展示（v0.64）：默认每页 5 个审核单元，全选仅作用于当前页。
    </p>
    <p v-if="!groups.length" class="muted">暂无卡片。开启 AI 整理（ai.enabled）后由 enrich 流水线产出。</p>
    <template v-else>
      <div v-if="checked.size" class="row-actions" style="margin-bottom: 8px">
        <b>已勾选 {{ checked.size }} 条</b>
        <button class="primary" :disabled="busy === 'batch'" @click="batchChecked('promote-all')">批量整条晋升</button>
        <button :disabled="busy === 'batch'" @click="batchChecked('redo')">批量整条打回</button>
        <button @click="checked = new Set()">清除选择</button>
      </div>
      <div class="table-scroll">
      <table class="list">
        <thead>
          <tr>
            <th style="width: 32px"><input type="checkbox" :checked="allChecked" title="全选本页" @change="toggleAll" /></th>
            <th>条目（摘要卡）</th><th>状态</th><th>置信度</th><th>生成信息</th><th style="width: 240px">处置</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in rows" :key="row.kind === 'card' ? row.card.id : `toggle-${row.group.key}`">
            <tr v-if="row.kind === 'card'">
              <td>
                <input
                  v-if="row.role === 'summary'"
                  type="checkbox"
                  :checked="checked.has(row.group.key)"
                  :title="`勾选条目：${row.group.summary.title || row.group.key}`"
                  @change="toggleGroup(row.group.key)"
                />
              </td>
              <td style="cursor: pointer" @click="toggleBody(row.card)">
                <b>{{ row.card.title || row.card.id }}</b>
                <span class="badge ai-mark" style="margin-left: 6px">AI 生成</span>
                <span v-if="row.role === 'related'" class="badge" style="margin-left: 6px">{{ TYPE_LABELS[row.card.type] ?? row.card.type }}</span>
                <span v-if="row.card.edited_at" class="badge human-mark" style="margin-left: 6px">已人工修订</span>
                <div class="mono muted" style="font-size: 11px">
                  {{ row.role === 'summary' ? `源条目 ${row.group.key}` : row.card.id }}
                </div>
              </td>
              <td><span class="badge" :class="`st-${row.card.status}`">{{ row.card.status }}</span></td>
              <td class="mono">{{ row.card.confidence ?? '—' }}</td>
              <td class="muted" style="font-size: 12px">
                {{ row.card.model }}<br />{{ row.card.created_at }}
                <div v-if="row.card.edited_at" style="font-size: 11px">修订于 {{ row.card.edited_at }}</div>
              </td>
              <td>
                <!-- 组级操作只挂摘要卡主行；单卡处置在展开详情里，明细行保留行内单卡处置 -->
                <div v-if="row.role === 'summary'" class="row-actions">
                  <button
                    v-if="row.card.status === 'draft' || draftRelated(row.group)"
                    class="primary"
                    :disabled="busy === 'batch'"
                    @click="groupPromote(row.group)"
                  >整条晋升</button>
                  <button :disabled="busy === 'batch'" @click="groupRedo(row.group)">整条打回</button>
                  <button v-if="row.group.related.length" @click="toggleGroupOpen(row.group.key)">
                    {{ openedGroup === row.group.key ? '收起明细' : `明细 ${row.group.related.length}` }}
                  </button>
                </div>
                <div v-else class="row-actions">
                  <button
                    v-if="row.card.status === 'draft'"
                    class="primary"
                    :disabled="busy === `${row.card.id}:promote`"
                    @click="act(row.card, 'promote')"
                  >晋升</button>
                  <button
                    title="删除该卡片并重置其全部来源条目为 normalized"
                    :disabled="busy === `${row.card.id}:regenerate`"
                    @click="act(row.card, 'regenerate')"
                  >打回</button>
                  <button class="danger" :disabled="busy === `${row.card.id}:delete`" @click="act(row.card, 'delete')">删除</button>
                </div>
              </td>
            </tr>
            <tr v-else>
              <td></td>
              <td colspan="5">
                <button class="link-toggle" style="background: none; border: none; cursor: pointer; color: var(--accent, #2563eb); padding: 0" @click="toggleGroupOpen(row.group.key)">
                  {{ openedGroup === row.group.key ? '▾' : '▸' }}
                  {{ relatedLabel(row.group) }}（{{ draftRelated(row.group) }} 张待审）
                </button>
              </td>
            </tr>
            <tr v-if="row.kind === 'card' && expanded === row.card.id">
              <td colspan="6">
                <div class="card-detail">
                  <p class="card-meta">
                    <span class="muted">
                      模型 <span class="mono">{{ row.card.model || '—' }}</span> ·
                      生成 {{ row.card.created_at || '—' }} ·
                      置信度 <span class="mono">{{ row.card.confidence ?? '—' }}</span>
                      <span v-if="row.card.edited_at"> · 人工修订 {{ row.card.edited_at }}</span>
                    </span>
                  </p>
                  <template v-if="detail[row.card.id]?.source">
                    <p class="card-meta">
                      <b>AI 摘要</b>：{{ detail[row.card.id]!.source!.aiSummary || '（源条目无 ai.summary）' }}
                    </p>
                    <p class="card-meta">
                      <b>AI 标签</b>：
                      <span v-if="detail[row.card.id]!.source!.tags?.length" class="mono">
                        {{ detail[row.card.id]!.source!.tags.join('、') }}
                      </span>
                      <span v-else class="muted">（无）</span>
                    </p>
                    <p class="card-meta">
                      <span v-if="detail[row.card.id]?.source?.url">
                        原文：<a :href="detail[row.card.id]!.source!.url!" target="_blank" rel="noopener noreferrer">{{ detail[row.card.id]?.source?.url }}</a>
                      </span>
                      <span v-else class="muted">原文：无 URL 记录</span>
                      <span class="muted mono"> · 源条目 <span class="mono">{{ detail[row.card.id]!.source!.id }}</span></span>
                    </p>
                  </template>
                  <div v-if="editingId === row.card.id" class="card-edit">
                    <textarea v-model="editBody" rows="14" class="mono"></textarea>
                    <div class="row-actions" style="margin-top: 8px">
                      <button class="primary" :disabled="busy === `${row.card.id}:edit`" @click="saveEdit(row.card)">保存修订</button>
                      <button @click="cancelEdit">取消</button>
                    </div>
                  </div>
                  <template v-else>
                    <div class="row-actions" style="margin: 8px 0">
                      <button v-if="row.card.status === 'draft'" class="primary" @click="act(row.card, 'promote')">晋升</button>
                      <button title="删除该卡片并重置其全部来源条目为 normalized" @click="act(row.card, 'regenerate')">打回</button>
                      <button class="danger" @click="act(row.card, 'delete')">删除</button>
                      <button v-if="row.card.status === 'draft'" @click="startEdit(row.card)">编辑草稿</button>
                      <span v-else class="muted" style="font-size: 12px">promoted 卡不可修订（已是索引引用源）</span>
                    </div>
                    <!-- 卡正文来自 LLM（不可信输入）：必须经 markdown.ts（marked+DOMPurify）净化后渲染 -->
                    <div class="md-body" v-html="renderMarkdown(detail[row.card.id]?.body)"></div>
                  </template>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
      </div>
      <!-- v0.64 分页条（与 v0.60 日志分页同款样式与口径） -->
      <div class="pager" style="margin-top: 8px">
        <span class="muted">
          第 {{ (unitPage - 1) * unitPageSize + 1 }}–{{ Math.min(unitPage * unitPageSize, unitTotal) }} 条 /
          共 {{ unitTotal }} 个审核单元
        </span>
        <label class="muted">每页
          <select v-model.number="unitPageSize" @change="changeUnitPageSize">
            <option :value="5">5</option>
            <option :value="10">10</option>
            <option :value="20">20</option>
            <option :value="50">50</option>
          </select>
        </label>
        <button :disabled="unitPage <= 1" @click="gotoUnitPage(unitPage - 1)">‹ 上一页</button>
        <template v-for="(item, i) in unitPageItems" :key="i">
          <span v-if="item === '…'" class="muted">…</span>
          <button v-else class="pager-page" :class="{ active: item === unitPage }" @click="gotoUnitPage(item)">{{ item }}</button>
        </template>
        <button :disabled="unitPage >= unitPageCount" @click="gotoUnitPage(unitPage + 1)">下一页 ›</button>
      </div>
      <div v-if="orphans.length" style="margin-top: 12px">
        <h3>无主明细卡（{{ orphans.length }} 张，首次来源条目已无摘要卡）</h3>
        <p class="muted" style="font-size: 12px">正常流程不产生；可直接晋升或删除。</p>
        <div class="row-actions">
          <button v-for="c in orphans" :key="c.id" :disabled="busy === `${c.id}:promote`" @click="act(c, 'promote')">
            晋升 {{ c.title || c.id }}
          </button>
        </div>
      </div>
    </template>
  </div>

  <div class="panel">
    <h2>enrich 操作日志（meta.json enrich.log 聚合，共 {{ logTotal }} 条）</h2>
    <p v-if="!logs.length" class="muted">暂无流水。enrich 每次尝试（成功/重试/失败/复活）都会记录。</p>
    <template v-else>
    <div class="table-scroll">
    <table class="list">
      <thead><tr><th>时间</th><th>结果</th><th>条目</th><th>说明</th><th>尝试</th></tr></thead>
      <tbody>
        <tr v-for="(l, i) in logs" :key="i">
          <td class="mono muted" style="white-space: nowrap">{{ String(l.at) }}</td>
          <td>
            <span class="badge" :class="l.outcome === 'enriched' ? 'st-enriched' : l.outcome === 'error' ? 'st-error' : 'st-normalized'">
              {{ l.outcome }}
            </span>
          </td>
          <td>{{ l.title }}<div class="mono muted" style="font-size: 11px">{{ l.entry_id }}</div></td>
          <td class="muted">{{ l.message ?? '—' }}</td>
          <td class="mono">{{ l.attempts }}</td>
        </tr>
      </tbody>
    </table>
    </div>
    <div class="pager" style="margin-top: 8px">
      <span class="muted">第 {{ (logPage - 1) * logPageSize + 1 }}–{{ Math.min(logPage * logPageSize, logTotal) }} 条</span>
      <label class="muted">每页
        <select v-model.number="logPageSize" @change="changeLogPageSize">
          <option :value="5">5</option>
          <option :value="10">10</option>
          <option :value="20">20</option>
          <option :value="50">50</option>
        </select>
      </label>
      <button :disabled="logPage <= 1" @click="gotoLogPage(logPage - 1)">‹ 上一页</button>
      <template v-for="(item, i) in logPageItems" :key="i">
        <span v-if="item === '…'" class="muted">…</span>
        <button v-else class="pager-page" :class="{ active: item === logPage }" @click="gotoLogPage(item)">{{ item }}</button>
      </template>
      <button :disabled="logPage >= logPageCount" @click="gotoLogPage(logPage + 1)">下一页 ›</button>
    </div>
    </template>
  </div>
</template>
