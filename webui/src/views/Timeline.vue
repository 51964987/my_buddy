<script setup lang="ts">
/** 时间流（收集者/消费者姿态）：按时间倒序浏览单条沉淀，过滤选项来自 /api/facets。 */
import { computed, onMounted, ref } from 'vue'
import { api, type EntryItem, type Facets } from '../api'

const entries = ref<EntryItem[]>([])
const facets = ref<Facets>({ platforms: [], source_types: [] })
const fPlatform = ref('')
const fType = ref('')
const fStatus = ref('')
const detail = ref<{ frontmatter: Record<string, unknown>; body: string; meta: Record<string, unknown>; raw_files: string[] } | null>(null)
const error = ref('')

const filtered = computed(() =>
  [...entries.value]
    .filter(
      (e) =>
        (!fPlatform.value || e.platform === fPlatform.value) &&
        (!fType.value || e.source_type === fType.value) &&
        (!fStatus.value || e.status === fStatus.value),
    )
    .sort((a, b) => (a.captured_at < b.captured_at ? 1 : -1)),
)

async function load() {
  try {
    const [list, f] = await Promise.all([
      api.get<{ entries: EntryItem[] }>('/api/entries'),
      api.get<Facets>('/api/facets'),
    ])
    entries.value = list.entries
    facets.value = f
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
}

async function openDetail(id: string) {
  try {
    detail.value = await api.get(`/api/entries/${id}`)
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
}

async function deleteEntry(id: string, title: string) {
  // 危险项二次确认（§4.4 v0.20）：物理删除条目目录含 raw/，不可恢复
  if (!window.confirm(`确认删除条目「${title || id}」？\n将物理删除该条目全部文件（含 raw 原件），不可恢复。`)) return
  try {
    await api.delete(`/api/entries/${id}`)
    if (detail.value && detail.value.frontmatter.id === id) detail.value = null
    await load()
  } catch (err) {
    error.value = String(err instanceof Error ? err.message : err)
  }
}

onMounted(load)
</script>

<template>
  <div v-if="error" class="alert error">{{ error }}</div>

  <div class="panel" style="display: flex; gap: 10px; align-items: center">
    <select v-model="fPlatform">
      <option value="">全部平台</option>
      <option v-for="p in facets.platforms" :key="p" :value="p">{{ p }}</option>
    </select>
    <select v-model="fType">
      <option value="">全部来源类型</option>
      <option v-for="t in facets.source_types" :key="t" :value="t">{{ t }}</option>
    </select>
    <select v-model="fStatus">
      <option value="">全部状态</option>
      <option value="normalized">normalized</option>
      <option value="enriched">enriched</option>
      <option value="error">error</option>
    </select>
    <span class="muted">{{ filtered.length }} 条</span>
  </div>

  <div class="panel">
    <div class="table-scroll">
    <table class="list">
      <thead><tr><th>时间</th><th>标题</th><th>平台</th><th>状态</th><th>标签</th><th></th></tr></thead>
      <tbody>
        <tr v-for="e in filtered" :key="e.id" style="cursor: pointer" @click="openDetail(e.id)">
          <td class="mono muted" style="white-space: nowrap">{{ e.captured_at?.replace('T', ' ') }}</td>
          <td class="cell-main">
            <b>{{ e.title || e.id }}</b>
            <div v-if="e.url" class="muted mono cell-ellipsis" style="font-size: 11px" :title="e.url">{{ e.url }}</div>
          </td>
          <td><span class="tag">{{ e.platform }}</span><span class="tag">{{ e.source_type }}</span></td>
          <td><span class="badge" :class="`st-${e.status}`">{{ e.status }}</span></td>
          <td style="min-width: 160px"><span v-for="t in e.tags" :key="t" class="tag">{{ t }}</span></td>
          <td>
            <div class="row-actions" @click.stop>
              <button class="danger" @click="deleteEntry(e.id, e.title)">删除</button>
            </div>
          </td>
        </tr>
        <tr v-if="!filtered.length">
          <td colspan="6" class="muted">暂无条目。</td>
        </tr>
      </tbody>
    </table>
    </div>
  </div>

  <div v-if="detail" class="panel">
    <h2>
      {{ detail.frontmatter.title }}
      <button style="float: right" @click="detail = null">关闭</button>
      <button
        class="danger"
        style="float: right; margin-right: 8px"
        @click="deleteEntry(String(detail.frontmatter.id), String(detail.frontmatter.title ?? ''))"
      >删除</button>
    </h2>
    <p class="muted mono">{{ detail.frontmatter.url || '（无 URL 文本条目）' }}</p>
    <p>
      <span class="badge" :class="`st-${detail.frontmatter.status}`">{{ detail.frontmatter.status }}</span>
      <span v-if="detail.frontmatter.ai" class="badge ai-mark">AI 生成内容</span>
      <span v-for="t in (detail.frontmatter.tags as string[])" :key="t" class="tag">{{ t }}</span>
    </p>
    <div class="detail-body">{{ detail.body }}</div>
    <p class="muted" style="font-size: 12px">raw 原件：{{ detail.raw_files.join('、') || '无' }}</p>
  </div>
</template>
