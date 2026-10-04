<script setup lang="ts">
/**
 * 参数设置页（§11.5）：配置中心的人工界面，唯一配置入口。
 * 参数逐项即改即存（§11.5 v0.19）：编辑触发即下发 PUT /api/config 部分补丁（后端深合并）；
 * 敏感项掩码（token 回传 ****** = 保持原值；api_key env-only 不落盘）；
 * 危险项二次确认；热生效/重启标注（§11.5 四条纪律）。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api } from '../api'

const cfg = reactive<Record<string, any>>({})
const parsers = ref<{ name: string; description: string }[]>([])
const error = ref('')
const notice = ref('')
const loaded = ref(false)
const showToken = ref(false)
const newProvider = ref('')

const TASKS = [
  { key: 'tags', label: '标签 tags' },
  { key: 'summary_card', label: '摘要卡 summary_card' },
  { key: 'entity_extraction', label: '实体抽取 entity_extraction（知识图谱）' },
  { key: 'concept_card', label: '概念聚合 concept_card（知识图谱，二期接入）' },
  { key: 'embedding', label: '向量 embedding' },
]

const providerNames = () => Object.keys(cfg.ai?.providers ?? {})

// 任务实际生效的后端提示：model 空 = 用 provider 默认（§9 决策 4 分级解析规则）
function taskBackendHint(task: string): string {
  const t = cfg.ai?.tasks?.[task]
  if (!t?.provider) return '未配置（该任务不执行；concept_card 为聚合任务槽，二期接入）'
  const p = cfg.ai.providers[t.provider] ?? {}
  const model = t.model || p.model || '(空!)'
  const key = p.api_key_env
    ? `key ← 环境变量 ${p.api_key_env}`
    : '无需 key'
  return `${t.provider} → ${p.base_url ?? ''} · ${model} · ${key}`
}

async function load() {
  try {
    const data = await api.get<{ config: Record<string, any> }>('/api/config')
    Object.keys(cfg).forEach((k) => delete cfg[k])
    Object.assign(cfg, data.config)
    parsers.value = (await api.get<{ parsers: { name: string; description: string }[] }>('/api/parsers')).parsers
    loaded.value = true
    error.value = ''
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
}

// ---------- 逐项即改即存（§11.5 v0.19）：编辑触发即下发部分补丁，无全局保存按钮 ----------

const saveState = ref<'idle' | 'saving' | 'ok' | 'error'>('idle')
const saveMsg = ref('')
let okTimer: ReturnType<typeof setTimeout> | undefined

// 下发部分补丁；opts.restart = 该字段属"重启生效"项时前端直接给出提示（后端只探测 host/port）
async function patch(partial: Record<string, unknown>, opts?: { restart?: boolean }): Promise<boolean> {
  saveState.value = 'saving'
  error.value = ''
  try {
    const r = await api.put<{ config: Record<string, any>; restart_required: boolean }>('/api/config', {
      config: partial,
    })
    saveState.value = 'ok'
    saveMsg.value = opts?.restart || r.restart_required ? '已保存，重启服务后生效' : '已保存，热生效'
    clearTimeout(okTimer)
    okTimer = setTimeout(() => {
      if (saveState.value === 'ok') saveState.value = 'idle'
    }, 2500)
    // token 变更成功后回填服务端掩码，避免明文滞留输入框
    if ('token' in partial) cfg.token = r.config.token
    return true
  } catch (e) {
    saveState.value = 'error'
    saveMsg.value = '保存失败：' + String(e instanceof Error ? e.message : e)
    return false
  }
}

// 嵌套路径字段统一入口：path = ['ai','timeout'] → 补丁 {ai:{timeout: v}}
function savePath(path: string[], value: unknown, opts?: { restart?: boolean }) {
  const partial: Record<string, any> = {}
  let node = partial
  for (let i = 0; i < path.length - 1; i++) node = node[path[i]] = {}
  node[path[path.length - 1]] = value
  return patch(partial, opts)
}

function saveProvider(name: string, field: string, value: unknown) {
  return patch({ ai: { providers: { [name]: { [field]: value } } } })
}

function saveTask(task: string, field: string, value: unknown) {
  return patch({ ai: { tasks: { [task]: { [field]: value } } } })
}

// 图谱 schema 白名单（ai.kg，§9 决策 6）：顿号/逗号分隔输入 → 数组；
// 清空 entity_types = 不做实体抽取（后端口径：空白名单跳过任务）
function saveKg(field: 'entity_types' | 'relation_types', text: string) {
  const list = text.split(/[、,，]/).map((s) => s.trim()).filter(Boolean)
  return patch({ ai: { kg: { [field]: list } } })
}

function saveToken() {
  // 掩码值 = 保持原值（未改动），不回传
  if (cfg.token === '******') return
  patch({ token: cfg.token })
}

function changeKbRoot() {
  // 危险项二次确认：库目录变更（取消则回退原值）
  if (cfg.kb_root === cfg.__orig_kb_root) return
  if (
    !window.confirm(
      '库目录 kb_root 是危险项：服务不会自动迁移旧 kb/ 内容，需自行迁移并重启。\n确认改为：' + cfg.kb_root + ' ？',
    )
  ) {
    cfg.kb_root = cfg.__orig_kb_root
    return
  }
  patch({ kb_root: cfg.kb_root }, { restart: true }).then((ok) => {
    if (ok) cfg.__orig_kb_root = cfg.kb_root
  })
}

async function rebuildIndex() {
  if (!window.confirm('确认全量重建索引？向量增强开启时将全量重嵌入（产生外部 API 费用）。')) return
  try {
    const r = await api.post<{ docs: number }>('/api/index/rebuild')
    notice.value = `索引已重建（${r.docs} 篇文档）。`
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
}

// ---------- 熔断运行态（§5.1 v0.29：程序写运行态，页面只读 + 显式恢复） ----------

const breakerOpen = computed(() => cfg.ai?.breaker?.state === 'open')

async function resetBreaker() {
  try {
    await api.post('/api/enrich/breaker/reset')
    notice.value = '已恢复自动整理（熔断计数已清零）。'
    await load() // 重新拉配置，刷新熔断运行态展示
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
}

function addProvider() {
  const name = newProvider.value.trim().toLowerCase()
  if (!name) return
  if (cfg.ai.providers[name]) {
    error.value = `provider '${name}' 已存在`
    return
  }
  const entry = { base_url: 'http://127.0.0.1:11434/v1', model: '', api_key_env: '', api: 'openai' }
  cfg.ai.providers[name] = entry
  newProvider.value = ''
  patch({ ai: { providers: { [name]: entry } } }) // 深合并新增条目
}

async function removeProvider(name: string) {
  // 危险项二次确认（UI 纪律 3）
  if (!window.confirm(`确认删除 provider '${name}'？引用它的任务需改指向。`)) return
  delete cfg.ai.providers[name]
  // null 值 = 删除该键（§11.5 v0.19 补丁语义；深合并无法删键，此前删除不持久化）
  await patch({ ai: { providers: { [name]: null } } })
  for (const t of Object.keys(cfg.ai.tasks)) {
    if (cfg.ai.tasks[t].provider === name) {
      cfg.ai.tasks[t].provider = ''
      await patch({ ai: { tasks: { [t]: { provider: '' } } } })
    }
  }
}

// ---------- 左侧锚点导航（分区目录 + 滚动高亮当前分区） ----------

const SECTIONS = [
  { id: 'sec-service', title: '服务' },
  { id: 'sec-pipeline', title: '管道' },
  { id: 'sec-normalize', title: '归一化' },
  { id: 'sec-ai', title: 'AI 模型' },
  { id: 'sec-index', title: '索引' },
  { id: 'sec-parsers', title: '解析器注册表' },
  { id: 'sec-sync', title: 'D 类同步' },
]
const activeSection = ref('')

// 滚动高亮：取"顶部阈值（顶栏高度 + 余量）以上最后一个分区"为当前分区
function onScroll() {
  let current = ''
  for (const s of SECTIONS) {
    const el = document.getElementById(s.id)
    if (el && el.getBoundingClientRect().top <= 140) current = s.id
  }
  activeSection.value = current
}

function scrollToSection(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  activeSection.value = id
}

// 分区面板在配置加载完成后才渲染，届时再挂滚动监听
watch(loaded, (v) => {
  if (v) {
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
  } else {
    window.removeEventListener('scroll', onScroll)
  }
})
onUnmounted(() => window.removeEventListener('scroll', onScroll))

onMounted(load)
</script>

<template>
  <div v-if="error" class="alert error">{{ error }}</div>
  <div v-if="notice" class="alert ok">{{ notice }}</div>
  <div v-if="!loaded && !error" class="muted">加载配置中…</div>

  <template v-if="loaded && cfg.ai && cfg.index">
  <div class="settings-layout">
  <aside class="settings-nav">
    <a
      v-for="s in SECTIONS"
      :key="s.id"
      :href="'#' + s.id"
      :class="{ active: activeSection === s.id }"
      @click.prevent="scrollToSection(s.id)"
    >{{ s.title }}</a>
  </aside>

  <div class="settings-content">
  <div id="sec-service" class="panel">
    <h2>服务</h2>
    <div class="form-row">
      <label>监听地址 host</label>
      <input v-model="cfg.host" type="text" @change="savePath(['host'], cfg.host, { restart: true })" />
      <span class="hint">局域网监听（如 0.0.0.0）必须配置 token</span>
      <span class="restart-tag">重启生效</span>
    </div>
    <div class="form-row">
      <label>端口 port</label>
      <input v-model="cfg.port" type="number" style="width: 110px" @change="savePath(['port'], cfg.port, { restart: true })" />
      <span class="restart-tag">重启生效</span>
    </div>
    <div class="form-row">
      <label>API token</label>
      <input
        v-model="cfg.token"
        :type="showToken ? 'text' : 'password'"
        placeholder="留空 = 无鉴权（仅环回）"
        style="width: 220px"
        onmouseover="this.placeholder='回传 ****** = 保持原值'"
        @change="saveToken"
      />
      <button style="padding: 4px 10px" @click="showToken = !showToken">{{ showToken ? '隐藏' : '显示' }}</button>
      <span class="hint">掩码显示不回显明文；修改即时生效</span>
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>库目录 kb_root（危险）</label>
      <input
        v-model="cfg.kb_root"
        type="text"
        style="width: 320px"
        @focus="cfg.__orig_kb_root ??= cfg.kb_root"
        @change="changeKbRoot"
      />
      <span class="hint">变更需二次确认 + 自行迁移旧 kb/ 内容</span>
      <span class="restart-tag">重启生效</span>
    </div>
  </div>

  <div id="sec-pipeline" class="panel">
    <h2>管道</h2>
    <div class="form-row">
      <label>后台管道线程 worker_enabled</label>
      <input v-model="cfg.pipeline.worker_enabled" type="checkbox" @change="savePath(['pipeline', 'worker_enabled'], cfg.pipeline.worker_enabled, { restart: true })" />
      <span class="restart-tag">重启生效</span>
    </div>
    <div class="form-row">
      <label>轮询间隔 poll_interval（秒）</label>
      <input v-model="cfg.pipeline.poll_interval" type="number" step="0.5" style="width: 110px" @change="savePath(['pipeline', 'poll_interval'], cfg.pipeline.poll_interval, { restart: true })" />
      <span class="restart-tag">重启生效</span>
    </div>
    <div class="form-row">
      <label>失败重试 max_attempts</label>
      <input v-model="cfg.pipeline.max_attempts" type="number" style="width: 110px" @change="savePath(['pipeline', 'max_attempts'], cfg.pipeline.max_attempts)" />
      <span class="hint">超过转 error 态（可重跑）</span>
      <span class="hot-tag">热生效</span>
    </div>
  </div>

  <div id="sec-normalize" class="panel">
    <h2>归一化</h2>
    <div class="form-row">
      <label>图片本地化 image_localization</label>
      <input v-model="cfg.normalize.image_localization" type="checkbox" @change="savePath(['normalize', 'image_localization'], cfg.normalize.image_localization)" />
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>单条目图片上限 max_images</label>
      <input v-model="cfg.normalize.max_images" type="number" style="width: 110px" @change="savePath(['normalize', 'max_images'], cfg.normalize.max_images)" />
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>抓取超时 fetch_timeout（秒）</label>
      <input v-model="cfg.normalize.fetch_timeout" type="number" step="0.5" style="width: 110px" @change="savePath(['normalize', 'fetch_timeout'], cfg.normalize.fetch_timeout)" />
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>动态页兜底 playwright_fallback</label>
      <input v-model="cfg.normalize.playwright_fallback" type="checkbox" @change="savePath(['normalize', 'playwright_fallback'], cfg.normalize.playwright_fallback)" />
      <span class="hint">无选中文本且抓取/提取失败时经无头浏览器渲染重试</span>
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>兜底渲染超时 playwright_timeout（秒）</label>
      <input v-model="cfg.normalize.playwright_timeout" type="number" step="0.5" style="width: 110px" @change="savePath(['normalize', 'playwright_timeout'], cfg.normalize.playwright_timeout)" />
      <span class="hot-tag">热生效</span>
    </div>
  </div>

  <div id="sec-ai" class="panel">
    <h2>AI 模型（provider 表 + 任务分级；api_key 仅存环境变量，永不落盘）</h2>
    <div class="form-row">
      <label>AI 整理总开关 ai.enabled</label>
      <input v-model="cfg.ai.enabled" type="checkbox" @change="savePath(['ai', 'enabled'], cfg.ai.enabled)" />
      <span class="hint">总闸：允许调用 LLM（含试跑）；关掉即全面止损</span>
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>自动整理触发 trigger_mode</label>
      <select v-model="cfg.ai.trigger_mode" style="width: 170px" @change="savePath(['ai', 'trigger_mode'], cfg.ai.trigger_mode)">
        <option value="manual">手动（默认）</option>
        <option value="auto">自动（周期扫库）</option>
      </select>
      <span class="hint">手动=只响应页面上的试跑/单条/批量；自动=每 poll_interval 秒扫 batch_size 条</span>
      <span class="hot-tag">热生效</span>
    </div>
    <div v-if="breakerOpen" class="form-row">
      <label style="color: var(--danger)">熔断已触发</label>
      <span class="hint"
        >连续 {{ cfg.ai.breaker.consecutive_failures }} 轮整库无成功，自动整理已暂停（{{ cfg.ai.breaker.opened_at }}）。
        原因：{{ cfg.ai.breaker.last_error }} —— 修正模型配置后点右侧恢复。</span
      >
      <button class="primary" style="white-space: nowrap" @click="resetBreaker">恢复自动整理</button>
    </div>
    <div class="form-row">
      <label>熔断阈值 breaker_threshold</label>
      <input
        v-model.number="cfg.ai.breaker_threshold"
        type="number"
        min="1"
        style="width: 110px"
        @change="savePath(['ai', 'breaker_threshold'], cfg.ai.breaker_threshold)"
      />
      <span class="hint">仅 auto 模式生效：整轮无成功累计到该值即暂停，避免配置错误刷爆全库</span>
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>LLM 请求超时 timeout（秒）</label>
      <input v-model="cfg.ai.timeout" type="number" step="1" style="width: 110px" @change="savePath(['ai', 'timeout'], cfg.ai.timeout)" />
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>enrich 重试 max_attempts</label>
      <input v-model="cfg.ai.max_attempts" type="number" style="width: 110px" @change="savePath(['ai', 'max_attempts'], cfg.ai.max_attempts)" />
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>每轮批量 batch_size</label>
      <input v-model="cfg.ai.batch_size" type="number" style="width: 110px" @change="savePath(['ai', 'batch_size'], cfg.ai.batch_size)" />
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>批内并发度 concurrency</label>
      <input v-model="cfg.ai.concurrency" type="number" min="1" style="width: 110px" @change="savePath(['ai', 'concurrency'], cfg.ai.concurrency)" />
      <span class="hint">enrich worker 并发上限</span>
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>enrich 扫描间隔 poll_interval（秒）</label>
      <input v-model="cfg.ai.poll_interval" type="number" step="0.5" style="width: 110px" @change="savePath(['ai', 'poll_interval'], cfg.ai.poll_interval)" />
      <span class="hint">auto 模式每轮醒来前实时读（改完即生效）</span>
      <span class="hot-tag">热生效</span>
    </div>

    <h2 style="margin-top: 18px">provider 表</h2>
    <div class="table-scroll">
    <table class="list">
      <thead><tr><th>名称</th><th>base_url</th><th>默认 model</th><th>协议</th><th>key 环境变量名</th><th></th></tr></thead>
      <tbody>
        <tr v-for="(p, name) in cfg.ai.providers" :key="name">
          <td><b style="white-space: nowrap">{{ name }}</b></td>
          <td><input v-model="p.base_url" type="text" style="width: 100%; min-width: 200px" @change="saveProvider(String(name), 'base_url', p.base_url)" /></td>
          <td><input v-model="p.model" type="text" style="width: 100%; min-width: 110px" @change="saveProvider(String(name), 'model', p.model)" /></td>
          <td style="min-width: 110px">
            <select
              :value="p.api || 'openai'"
              title="本地 Ollama 选原生（自动关思维链）"
              @change="p.api = ($event.target as HTMLSelectElement).value; saveProvider(String(name), 'api', p.api)"
              style="width: 100%; min-width: 100px"
            >
              <option value="openai">openai 兼容</option>
              <option value="ollama">ollama 原生</option>
            </select>
          </td>
          <td>
            <input
              v-model="p.api_key_env"
              type="text"
              style="width: 100%; min-width: 130px"
              placeholder="（Ollama 留空）"
              title="key 只读自该环境变量，不回显"
              @change="saveProvider(String(name), 'api_key_env', p.api_key_env)"
            />
          </td>
          <td><button class="danger" style="white-space: nowrap" @click="removeProvider(String(name))">删除</button></td>
        </tr>
      </tbody>
    </table>
    </div>
    <div style="margin-top: 8px; display: flex; gap: 8px">
      <input v-model="newProvider" type="text" placeholder="新 provider 名称（如 ollama）" style="width: 220px" />
      <button @click="addProvider">新增 provider</button>
    </div>

    <h2 style="margin-top: 18px">任务分级（每个任务独立选择 LLM 后端与模型）</h2>
    <div v-for="t in TASKS" :key="t.key" class="form-row">
      <label>{{ t.label }}</label>
      <select v-model="cfg.ai.tasks[t.key].provider" style="width: 150px" @change="saveTask(t.key, 'provider', cfg.ai.tasks[t.key].provider)">
        <option value="">（未配置）</option>
        <option v-for="n in providerNames()" :key="n" :value="n">{{ n }}</option>
      </select>
      <input v-model="cfg.ai.tasks[t.key].model" type="text" placeholder="model（空 = provider 默认）" style="width: 180px" @change="saveTask(t.key, 'model', cfg.ai.tasks[t.key].model)" />
      <input
        v-if="t.key === 'embedding'"
        v-model="cfg.ai.tasks[t.key].dimensions"
        type="number"
        placeholder="dimensions"
        style="width: 110px"
        @change="saveTask('embedding', 'dimensions', cfg.ai.tasks.embedding.dimensions)"
      />
      <span class="hint">{{ taskBackendHint(t.key) }}</span>
      <span class="hot-tag">热生效</span>
    </div>

    <h2 style="margin-top: 18px">图谱 schema 白名单（ai.kg，全局一份）</h2>
    <div class="form-row">
      <label>实体类型 entity_types</label>
      <input
        :value="(cfg.ai?.kg?.entity_types ?? []).join('、')"
        type="text"
        style="flex: 1; min-width: 260px"
        placeholder="如：产品、技术、概念、组织、人物"
        @change="saveKg('entity_types', ($event.target as HTMLInputElement).value)"
      />
      <span class="hint">顿号/逗号分隔；清空 = 不做实体抽取</span>
      <span class="hot-tag">热生效</span>
    </div>
    <div class="form-row">
      <label>关系类型 relation_types</label>
      <input
        :value="(cfg.ai?.kg?.relation_types ?? []).join('、')"
        type="text"
        style="flex: 1; min-width: 260px"
        placeholder="如：属于、包含、依赖、相关"
        @change="saveKg('relation_types', ($event.target as HTMLInputElement).value)"
      />
      <span class="hint">顿号/逗号分隔</span>
      <span class="hot-tag">热生效</span>
    </div>
  </div>

  <div id="sec-index" class="panel">
    <h2>索引</h2>
    <div class="form-row">
      <label>向量增强 index.vector_enabled</label>
      <input v-model="cfg.index.vector_enabled" type="checkbox" @change="savePath(['index', 'vector_enabled'], cfg.index.vector_enabled, { restart: true })" />
      <span class="hint">开启后语义检索可用；embedding 调用产生外部 API 费用</span>
      <span class="restart-tag">重启生效</span>
    </div>
    <div class="form-row">
      <label>全量重建（危险）</label>
      <button class="danger" @click="rebuildIndex">POST /api/index/rebuild</button>
      <span class="hint">删库重扫，index.db 可随时重建</span>
    </div>
  </div>

  <div id="sec-parsers" class="panel">
    <h2>解析器注册表（只读）</h2>
    <p class="muted" style="margin-top: 0">
      注册在 sync 引擎代码内（新增站点 = 新增 adapter）；per-collection 覆盖走 collection.json。
    </p>
    <div class="table-scroll">
    <table class="list">
      <thead><tr><th>名称</th><th>说明</th></tr></thead>
      <tbody>
        <tr v-for="p in parsers" :key="p.name">
          <td class="mono">{{ p.name }}</td>
          <td class="muted">{{ p.description }}</td>
        </tr>
      </tbody>
    </table>
    </div>
  </div>

  <div id="sec-sync" class="panel">
    <h2>D 类同步</h2>
    <p class="muted" style="margin-top: 0">
      D 类文档站（collection）在「<router-link to="/docs">文档树</router-link>」页注册与同步：
      注册即后台全量首抓，增量同步在该页手动下发，无全局周期参数；
      per-collection 覆盖项在 collections/&lt;id&gt;/collection.json（§4.3）。
    </p>
  </div>

  </div>
  </div>
  </template>

  <!-- 逐项保存状态浮标（§11.5 v0.19）：无全局保存按钮，编辑即存 -->
  <div v-if="saveState !== 'idle'" class="save-chip" :class="saveState">{{ saveMsg }}</div>
</template>

<style scoped>
/* 左侧分区锚点导航（sticky 跟随滚动，业界设置页通行做法） */
.settings-layout {
  display: flex;
  align-items: flex-start;
  gap: 20px;
}

.settings-nav {
  position: sticky;
  top: 62px; /* 顶栏吸附高度（~46px）+ 余量 */
  width: 140px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
}

.settings-nav a {
  display: block;
  padding: 5px 12px;
  font-size: 13px;
  color: var(--muted);
  text-decoration: none;
  border-left: 2px solid var(--line);
  white-space: nowrap;
}

.settings-nav a:hover {
  color: var(--text);
}

.settings-nav a.active {
  color: var(--accent);
  border-left-color: var(--accent);
  font-weight: 500;
}

.settings-content {
  flex: 1;
  min-width: 0;
}

/* 锚点跳转时给分区标题留出顶栏 + 余量 */
.settings-content .panel {
  scroll-margin-top: 64px;
}

/* 窄屏隐藏锚点导航，回到纵排 */
@media (max-width: 900px) {
  .settings-nav {
    display: none;
  }
}

/* 逐项保存状态浮标 */
.save-chip {
  position: fixed;
  right: 20px;
  bottom: 16px;
  z-index: 50;
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 12px;
  box-shadow: 0 2px 8px #0002;
  background: var(--panel);
  border: 1px solid var(--line);
  color: var(--muted);
}

.save-chip.ok {
  background: #e5f5ea;
  border-color: #bfe6cb;
  color: var(--ok);
}

.save-chip.error {
  background: #fdeaea;
  border-color: #efc4c4;
  color: var(--danger);
}
</style>
