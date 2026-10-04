<script setup lang="ts">
/** 检索主页（消费者姿态）：全文 / 语义两种模式（§5.1 v0.17）。 */
import { onMounted, ref } from 'vue'
import { api, type SearchHit } from '../api'

const q = ref('')
const mode = ref<'fulltext' | 'semantic'>('fulltext')
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
      `/api/search?q=${encodeURIComponent(q.value)}&mode=${mode.value}`,
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

onMounted(() => {
  // 支持 /app?/?q= 直达（可选）；默认空态
})
</script>

<template>
  <div class="panel">
    <form style="display: flex; gap: 10px" @submit.prevent="doSearch">
      <input
        v-model="q"
        type="text"
        placeholder="输入关键词检索全文 / 语义…"
        style="flex: 1"
        autofocus
      />
      <select v-model="mode">
        <option value="fulltext">全文（FTS5 + jieba）</option>
        <option value="semantic">语义（向量）</option>
      </select>
      <button class="primary" type="submit" :disabled="loading">检索</button>
    </form>
  </div>

  <div v-if="error" class="alert error">{{ error }}</div>
  <div v-if="loading" class="muted">检索中…</div>

  <div v-if="searched && !loading" class="panel">
    <h2>结果（{{ hits.length }}）</h2>
    <p v-if="!hits.length" class="muted">无命中。</p>
    <!-- 宽表规范（04-ui-constraints）：容器内滚动兜底；v-else 必须上移到包裹元素，留在 table 上会断链 -->
    <div v-else class="table-scroll">
      <table class="list">
        <thead>
          <tr><th>类型</th><th>标题</th><th>摘要</th><th>标签</th><th>状态</th><th v-if="mode === 'semantic'">距离</th></tr>
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
            <td v-if="mode === 'semantic'" class="mono">{{ h.distance }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
