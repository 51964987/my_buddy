<script setup lang="ts">
/**
 * AI 审核台（整理监工姿态，§4.5 v0.35）：draft 卡处置（修订/晋升/打回/删除）+ enrich 操作日志。
 *
 * 详情区按业界人工复核的通行口径组织（先看清"AI 产出了什么"，而不是只看一坨产物）：
 * AI 元信息（模型/时间/置信度/是否人工修订）+ AI 摘要与 AI 标签（存在**源条目** frontmatter，
 * 须按 sources 联查——这是"AI 到底说了什么"的直接答案）+ 渲染后的卡正文 + 原文入口。
 */
import { onMounted, ref } from 'vue'
import { api, type WikiCard } from '../api'
import { renderMarkdown } from '../markdown'

const emit = defineEmits<{ (e: 'refresh-badge'): void }>()

const cards = ref<WikiCard[]>([])
const logs = ref<Record<string, unknown>[]>([])
const error = ref('')
const notice = ref('')
const busy = ref('')
const expanded = ref<string>('')

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

async function load() {
  try {
    const [c, l] = await Promise.all([
      api.get<{ cards: WikiCard[] }>('/api/wiki'),
      api.get<{ logs: Record<string, unknown>[] }>('/api/enrich/logs?limit=50'),
    ])
    cards.value = c.cards
    logs.value = l.logs
    error.value = ''
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
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
    const entryId = d.sources?.[0]
    let source: Detail['source']
    if (entryId) {
      try {
        // 卡正文不带 AI 摘要/标签（它们在源条目 frontmatter），须联查条目详情
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
        source = undefined // 源条目已被人工删除：只显示卡本身，不阻塞审核
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
      点标题展开：AI 摘要与 AI 标签（AI 到底写了什么）、模型/时间/置信度、渲染后的正文、原文入口；draft 可修订后再晋升。
    </p>
    <p v-if="!cards.length" class="muted">暂无卡片。开启 AI 整理（ai.enabled）后由 enrich 流水线产出。</p>
    <div v-else class="table-scroll">
    <table class="list">
      <thead>
        <tr><th>标题</th><th>状态</th><th>置信度</th><th>来源</th><th>生成信息</th><th style="width: 180px">处置</th></tr>
      </thead>
      <tbody>
        <template v-for="card in cards" :key="card.id">
          <tr>
            <td style="cursor: pointer" @click="toggleBody(card)">
              <b>{{ card.title || card.id }}</b>
              <span class="badge ai-mark" style="margin-left: 6px">AI 生成</span>
              <span v-if="card.edited_at" class="badge human-mark" style="margin-left: 6px">已人工修订</span>
              <div class="mono muted" style="font-size: 11px">{{ card.id }} · {{ card.type }}</div>
            </td>
            <td><span class="badge" :class="`st-${card.status}`">{{ card.status }}</span></td>
            <td class="mono">{{ card.confidence ?? '—' }}</td>
            <td class="mono muted">{{ card.sources.join('、') }}</td>
            <td class="muted" style="font-size: 12px">
              {{ card.model }}<br />{{ card.created_at }}
              <div v-if="card.edited_at" style="font-size: 11px">修订于 {{ card.edited_at }}</div>
            </td>
            <td>
              <div class="row-actions">
                <button
                  v-if="card.status === 'draft'"
                  class="primary"
                  :disabled="busy === `${card.id}:promote`"
                  @click="act(card, 'promote')"
                >晋升</button>
                <button
                  title="删除该卡片并重置源条目为 normalized，AI 将重新生成"
                  :disabled="busy === `${card.id}:regenerate`"
                  @click="act(card, 'regenerate')"
                >打回</button>
                <button class="danger" :disabled="busy === `${card.id}:delete`" @click="act(card, 'delete')">删除</button>
              </div>
            </td>
          </tr>
          <tr v-if="expanded === card.id">
            <td colspan="6">
              <div class="card-detail">
                <p class="card-meta">
                  <span class="muted">
                    模型 <span class="mono">{{ card.model || '—' }}</span> ·
                    生成 {{ card.created_at || '—' }} ·
                    置信度 <span class="mono">{{ card.confidence ?? '—' }}</span>
                    <span v-if="card.edited_at"> · 人工修订 {{ card.edited_at }}</span>
                  </span>
                </p>
                <p class="card-meta">
                  <b>AI 摘要</b>：{{ detail[card.id]?.source?.aiSummary || '（源条目无 ai.summary）' }}
                </p>
                <p class="card-meta">
                  <b>AI 标签</b>：
                  <span v-if="detail[card.id]?.source?.tags?.length" class="mono">
                    {{ detail[card.id]?.source?.tags.join('、') }}
                  </span>
                  <span v-else class="muted">（无）</span>
                </p>
                <p class="card-meta">
                  <span v-if="detail[card.id]?.source?.url">
                    原文：<a :href="detail[card.id]!.source!.url!" target="_blank" rel="noopener noreferrer">{{ detail[card.id]?.source?.url }}</a>
                  </span>
                  <span v-else class="muted">原文：无 URL 记录</span>
                  <span class="muted mono"> · 源条目 <span class="mono">{{ detail[card.id]?.source?.id || card.sources[0] }}</span></span>
                </p>

                <div v-if="editingId === card.id" class="card-edit">
                  <textarea v-model="editBody" rows="14" class="mono"></textarea>
                  <div class="row-actions" style="margin-top: 8px">
                    <button class="primary" :disabled="busy === `${card.id}:edit`" @click="saveEdit(card)">保存修订</button>
                    <button @click="cancelEdit">取消</button>
                  </div>
                </div>
                <template v-else>
                  <div class="row-actions" style="margin: 8px 0">
                    <button v-if="card.status === 'draft'" @click="startEdit(card)">编辑草稿</button>
                    <span v-else class="muted" style="font-size: 12px">promoted 卡不可修订（已是索引引用源）</span>
                  </div>
                  <!-- 卡正文来自 LLM（不可信输入）：必须经 markdown.ts（marked+DOMPurify）净化后渲染 -->
                  <div class="md-body" v-html="renderMarkdown(detail[card.id]?.body)"></div>
                </template>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
    </div>
  </div>

  <div class="panel">
    <h2>enrich 操作日志（meta.json enrich.log 聚合，最近 50 条）</h2>
    <p v-if="!logs.length" class="muted">暂无流水。enrich 每次尝试（成功/重试/失败/复活）都会记录。</p>
    <div v-else class="table-scroll">
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
  </div>
</template>
