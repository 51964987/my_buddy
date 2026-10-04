<script setup lang="ts">
/** 文档树（消费者姿态）：D 类 collection 目录树浏览，页面内容经 Query API 读取。 */
import { computed, onMounted, reactive, ref } from 'vue'
import { api, type TocDir, type TocPage } from '../api'
import { buildDocxFromMarkdown, buildMarkdown, downloadBlob, exportFilename, type ExportNode } from '../exporters'
import { renderMarkdown } from '../markdown'
import PopoverMenu from '../PopoverMenu.vue'

interface CollectionItem {
  id: string
  name: string | null
  state: string | null
  pages?: number
}

interface TreeNode {
  name: string
  path: string
  page: TocPage | null
  children: TreeNode[]
  order: number // 源站排序键（目录=dir.index，页面=page.index），兄弟节点按其升序
}

const collections = ref<CollectionItem[]>([])
const current = ref<string>('')
const tree = ref<TreeNode[]>([])
const error = ref('')
const syncing = ref(false)
const tocNotice = ref('')
const detail = ref<{ frontmatter: Record<string, unknown>; body: string } | null>(null)
const detailId = ref('')

// 注册 collection 表单（§11.5 v0.18：注册入口页面化，字段随解析器元数据动态渲染）
// v0.22 连接向导：入口 URL 失焦自动探测注册表 adapter 并预填；字段提示随元数据下发
interface ParserField {
  name: string
  hint: string
  placeholder: string
}
const parsers = ref<{ name: string; description: string; fields: ParserField[] }[]>([])
const showRegister = ref(false)
const registering = ref(false)
const regNotice = ref('')
const probeResult = ref<{ ok: boolean; text: string } | null>(null)
const probing = ref(false)
const reg = reactive<Record<string, string>>({
  id: '',
  name: '',
  entry_url: '',
  toc_parser: '',
  library_code: '',
  lang: 'zh',
})

const currentParser = computed(() => parsers.value.find((p) => p.name === reg.toc_parser))
const requiredFields = computed(() => currentParser.value?.fields ?? [])

async function loadParsers() {
  if (parsers.value.length) return
  try {
    parsers.value = (await api.get<{ parsers: typeof parsers.value }>('/api/parsers')).parsers
    if (!reg.toc_parser && parsers.value.length) reg.toc_parser = parsers.value[0].name
  } catch {
    /* 下拉留空即可，注册仍可用 API */
  }
}

// 连接向导（§11.5 v0.22）：入口 URL 失焦探测 → 预填解析器与站点参数 + 实测验证
async function probeUrl() {
  const url = reg.entry_url.trim()
  if (!url) return
  probing.value = true
  probeResult.value = null
  try {
    const r = await api.post<{ matched: boolean; parser: string | null; params?: Record<string, string>; toc_nodes?: number; toc_pages?: number; verify_error?: string }>(
      '/api/parsers/probe',
      { url },
    )
    if (!r.matched || !r.parser) {
      probeResult.value = { ok: false, text: '未识别到可用适配器——该站点需先开发 adapter（铁律 2：新增站点 = 新增 adapter），可在「参数设置 → 解析器注册表」查看现有适配器。' }
      return
    }
    reg.toc_parser = r.parser
    for (const [k, v] of Object.entries(r.params ?? {})) reg[k] = v
    if (r.verify_error) {
      probeResult.value = { ok: false, text: `已匹配适配器 ${r.parser}（参数已预填），但目录树实测失败：${r.verify_error}` }
    } else {
      probeResult.value = { ok: true, text: `已识别适配器 ${r.parser}，站点参数已预填并实测通过（目录树 ${r.toc_nodes} 节点 / ${r.toc_pages} 页）。` }
    }
  } catch (e) {
    probeResult.value = { ok: false, text: '探测失败：' + String(e instanceof Error ? e.message : e) }
  } finally {
    probing.value = false
  }
}

async function submitRegister() {
  registering.value = true
  error.value = ''
  regNotice.value = ''
  try {
    await api.post('/api/collections', { ...reg })
    regNotice.value = `已注册 ${reg.id}，正在后台全量首抓（页面越多耗时越久）；可稍后刷新查看进度。`
    showRegister.value = false
    await loadCollections()
    await select(reg.id)
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  } finally {
    registering.value = false
  }
}

async function loadCollections() {
  try {
    const data = await api.get<{ collections: CollectionItem[] }>('/api/collections')
    collections.value = data.collections
    if (data.collections.length && !current.value) await select(data.collections[0].id)
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
}

async function select(id: string) {
  current.value = id
  detail.value = null
  tocNotice.value = ''
  try {
    const toc = await api.get<{ pages: TocPage[]; dirs?: TocDir[] }>(`/api/collections/${id}/toc`)
    const dirMap: Record<string, { title: string; index: number }> = {}
    for (const d of toc.dirs ?? []) dirMap[d.path] = { title: d.title, index: d.index }
    tree.value = buildTree(toc.pages ?? [], dirMap)
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    // 409 = 首抓进行中（目录树尚未生成）：提示而非报错（v0.23 三态口径）
    if (msg.includes('首抓')) {
      tocNotice.value = msg
      tree.value = []
    } else {
      error.value = msg
    }
  }
}

function buildTree(pages: TocPage[], dirs: Record<string, { title: string; index: number }>): TreeNode[] {
  // 建树后兄弟节点按源站排序键（Index）升序排列（v0.25）——页面驱动插入的节点创建
  // 顺序 ≠ 源站文档序，必须显式排序；目录段名用 dirs 中文标题，缺失回退英文 code
  const root: TreeNode = { name: '', path: '', page: null, children: [], order: 0 }
  for (const p of pages) {
    const segs = p.path.split('/').filter(Boolean)
    let node = root
    segs.forEach((seg, i) => {
      const path = segs.slice(0, i + 1).join('/')
      let child = node.children.find((c) => c.path === path)
      if (!child) {
        const isLeaf = i === segs.length - 1
        const d = dirs[path]
        child = {
          name: d?.title || seg,
          path,
          page: null,
          children: [],
          order: isLeaf ? (p.index || 0) : d?.index ?? 0,
        }
        node.children.push(child)
      }
      node = child
    })
    node.page = p
    node.order = p.index || 0
  }
  const sortRec = (n: TreeNode) => {
    n.children.sort((a, b) => a.order - b.order)
    n.children.forEach(sortRec)
  }
  sortRec(root)
  return root.children
}

// D 类页面 id 口径（§9 决策 1）：SHA-1(collection-id + 章节路径) 前 12 位，前端复算
async function pageId(cid: string, path: string): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(`${cid}:${path}`))
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 12)
}

async function openPage(node: TreeNode) {
  if (!node.page) return
  try {
    const id = await pageId(current.value, node.path)
    detailId.value = id
    detail.value = await api.get(`/api/entries/${id}`)
    preview.value = null // 换页即丢弃上一份试跑结果，避免张冠李戴
    aiNotice.value = ''
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  }
}

// ---------- AI 整理：试跑（零落盘）与单条整理（§5.1 v0.29 触发门控） ----------

interface PreviewResult {
  entry_id: string
  title: string
  provider: string
  model: string
  base_url: string
  tags: string[]
  summary: string
  card_body: string
  confidence: number | null
  elapsed_ms: number
}
const aiEnabled = ref(false)
const triggerMode = ref('manual')
const preview = ref<PreviewResult | null>(null)
const aiBusy = ref<'' | 'preview' | 'run'>('')
const aiNotice = ref('')

async function loadAiState() {
  try {
    const s = await api.get<{ ai_enabled: boolean; ai_trigger_mode: string }>('/api/status')
    aiEnabled.value = s.ai_enabled
    triggerMode.value = s.ai_trigger_mode
  } catch {
    /* 状态读不到不阻塞浏览，按钮按未启用处理 */
  }
}

const canEnrich = computed(() => {
  const st = detail.value?.frontmatter?.status
  return st === 'normalized' || st === 'error'
})

async function runPreview() {
  if (!detailId.value) return
  aiBusy.value = 'preview'
  aiNotice.value = ''
  error.value = ''
  try {
    preview.value = await api.post<PreviewResult>(`/api/enrich/preview?entry_id=${detailId.value}`)
  } catch (e) {
    aiNotice.value = String(e instanceof Error ? e.message : e)
  } finally {
    aiBusy.value = ''
  }
}

async function enrichThisOne() {
  if (!detailId.value) return
  aiBusy.value = 'run'
  aiNotice.value = ''
  error.value = ''
  try {
    const r = await api.post<{ outcome: string; detail?: { message?: string } }>(
      `/api/enrich/run?entry_id=${detailId.value}`,
    )
    if (r.outcome === 'enriched') {
      aiNotice.value = '已整理：标签已回写条目，摘要卡进审核台（draft，需人工晋升）。'
      detail.value = await api.get(`/api/entries/${detailId.value}`)
    } else {
      aiNotice.value = `未成功（${r.outcome}）：${r.detail?.message ?? '见总览/审核台 enrich 流水'}`
    }
  } catch (e) {
    aiNotice.value = String(e instanceof Error ? e.message : e)
  } finally {
    aiBusy.value = ''
  }
}

async function syncNow() {
  syncing.value = true
  error.value = ''
  try {
    await api.post(`/api/collections/${current.value}/sync`)
    await select(current.value)
    await loadCollections()
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e)
  } finally {
    syncing.value = false
  }
}

const currentName = computed(() => collections.value.find((c) => c.id === current.value)?.name ?? current.value)

// 目录树导出（方案文档 §6 v0.28/v0.30/v0.31/v0.32）：
// 目录索引（md 大纲）前端本地生成；全文（zip/merged/docx）经后端 export 端点；
// zip 的 Word 版为前端逐页渲染（format=pages 数据源 + docx-render + fflate 打包）
type ExportFormat = 'md' | 'zip' | 'merged' | 'docx' | 'zipdocx'
const exporting = ref('')
// v0.39 导出进度：网络段流式百分比（getBlobWithProgress）+ CPU 段 worker 三阶段；AbortController 可取消
const exportProgress = ref<{ label: string; percent: number | null; detail: string } | null>(null)
let exportAbort: AbortController | null = null

function setExportProgress(label: string, percent: number | null, detail = ''): void {
  exportProgress.value = { label, percent, detail }
}

function fmtBytes(n: number): string {
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`
}

function downloadProgress(label: string) {
  return (received: number, total: number | null) =>
    setExportProgress(label, total ? Math.round((received / total) * 100) : null, total ? `${fmtBytes(received)} / ${fmtBytes(total)}` : fmtBytes(received))
}

function cancelExport(): void {
  exportAbort?.abort()
}

async function exportTree(format: ExportFormat) {
  if (!current.value) return
  exporting.value = format
  error.value = ''
  exportAbort = new AbortController()
  const signal = exportAbort.signal
  try {
    const nodes = tree.value as ExportNode[]
    const name = currentName.value
    if (format === 'md') {
      downloadBlob(new Blob([buildMarkdown(nodes, name)], { type: 'text/markdown;charset=utf-8' }), exportFilename(current.value, 'md'))
    } else if (format === 'zip') {
      // v0.32：中文标题布局（标题命名 md + 目录 raw 图片 + toc.md），后端打包；v0.39 流式进度
      setExportProgress('全文打包', null, '等待服务端…')
      const blob = await api.getBlobWithProgress(`/api/collections/${current.value}/export?format=zip`, downloadProgress('全文打包'), signal)
      downloadBlob(blob, `export-${current.value}-${dateTag()}.zip`)
    } else if (format === 'merged') {
      // v0.32：默认 images=original——图片按 meta.json 映射改写回原站 URL（无映射回落 image API）
      setExportProgress('全文合并', null, '等待服务端…')
      const blob = await api.getBlobWithProgress(`/api/collections/${current.value}/export?format=merged`, downloadProgress('全文合并'), signal)
      downloadBlob(blob, `full-${current.value}-${dateTag()}.md`)
    } else if (format === 'zipdocx') {
      // 全文打包 Word 版：逐页 worker 渲染（嵌图）→ 前端 zip 打包；模块动态加载（docx/fflate 分包）
      const { exportZipDocx } = await import('../zip-docx-export')
      const blob = await exportZipDocx(
        current.value,
        (done, total, detail) => setExportProgress('全文打包 Word', total ? Math.round((done / total) * 100) : null, detail || `${done}/${total} 页`),
        signal,
      )
      downloadBlob(blob, `export-${current.value}-${dateTag()}.zip`)
    } else {
      // 全文 docx：images=api 变体（图片链接为 image 端点 URL）→ worker 嵌图转换（三阶段进度，主线程零阻塞）
      setExportProgress('Word 全文', null, '读取正文…')
      const merged = await (
        await api.getBlobWithProgress(`/api/collections/${current.value}/export?format=merged&images=api`, downloadProgress('Word 全文'), signal)
      ).text()
      const blob = await buildDocxFromMarkdown(merged, name, {
        signal,
        onProgress: (p) => {
          if (p.stage === 'images') {
            setExportProgress('Word 全文', p.total ? Math.round(((p.done ?? 0) / p.total) * 100) : null, `拉取图片 ${p.done ?? 0}/${p.total ?? '?'}`)
          } else if (p.stage === 'render') {
            setExportProgress('Word 全文', null, '渲染文档结构…')
          } else {
            setExportProgress('Word 全文', null, '打包 docx…')
          }
        },
      })
      downloadBlob(blob, `full-${current.value}-${dateTag()}.docx`)
    }
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') return // 取消不是错误，finally 统一收尾
    error.value = '导出失败：' + String(e instanceof Error ? e.message : e)
  } finally {
    exporting.value = ''
    exportProgress.value = null
    exportAbort = null
  }
}

function dateTag(now = new Date()): string {
  return `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}`
}

onMounted(() => {
  loadCollections()
  loadParsers()
  loadAiState()
})
</script>

<template>
  <div v-if="error" class="alert error">{{ error }}</div>

  <div v-if="tocNotice" class="alert warn">{{ tocNotice }}</div>

  <div v-if="regNotice" class="alert ok">{{ regNotice }}</div>

  <div class="panel" style="display: flex; gap: 10px; align-items: center">
    <select :value="current" @change="select(($event.target as HTMLSelectElement).value)">
      <option v-if="!collections.length" value="">（未注册 collection）</option>
      <option v-for="c in collections" :key="c.id" :value="c.id">{{ c.name || c.id }}</option>
    </select>
    <button :disabled="!current || syncing" @click="syncNow">
      {{ syncing ? '同步中…' : '手动增量同步' }}
    </button>
    <!-- v0.28/v0.31 目录树导出：目录索引（md 大纲）+ 全文（zip/merged/docx 嵌图）
         浮层行为（点外部/Escape 关闭、选中收起、键盘遍历）由 PopoverMenu 承担 -->
    <PopoverMenu v-if="current">
      <template #trigger>导出</template>
      <div class="popover-menu-group">目录索引</div>
      <button role="menuitem" :disabled="!!exporting" @click="exportTree('md')">
        {{ exporting === 'md' ? '生成中…' : 'Markdown 大纲（.md）' }}
      </button>
      <div class="popover-menu-group">全文（正文内联）</div>
      <button role="menuitem" :disabled="!!exporting" @click="exportTree('zip')">
        {{ exporting === 'zip' ? '打包中…' : '全文打包（.zip，md 分文件 + 图片）' }}
      </button>
      <button role="menuitem" :disabled="!!exporting || exporting === 'zipdocx'" @click="exportTree('zipdocx')">
        {{ exporting === 'zipdocx' ? '转换中…（进度见下方面板）' : '全文打包 Word（.zip，docx 分文件嵌图）' }}
      </button>
      <button role="menuitem" :disabled="!!exporting" @click="exportTree('merged')">
        {{ exporting === 'merged' ? '生成中…' : '全文合并（.md，原站图片链接）' }}
      </button>
      <button role="menuitem" :disabled="!!exporting" @click="exportTree('docx')">
        {{ exporting === 'docx' ? '转换中…' : 'Word 全文（.docx，含图片）' }}
      </button>
    </PopoverMenu>
    <button @click="showRegister = !showRegister">注册 collection</button>
  </div>

  <!-- v0.39 导出进度面板：确定百分比/不确定态流纹 + 阶段文案 + 取消（渲染在 worker，UI 可实时刷新） -->
  <div v-if="exportProgress" class="panel export-progress">
    <div class="export-progress-bar">
      <div
        class="export-progress-fill"
        :class="{ indeterminate: exportProgress.percent == null }"
        :style="exportProgress.percent == null ? undefined : { width: exportProgress.percent + '%' }"
      ></div>
    </div>
    <div class="export-progress-text">
      <span>{{ exportProgress.label }}{{ exportProgress.percent != null ? ` ${exportProgress.percent}%` : '' }}</span>
      <span v-if="exportProgress.detail" class="export-progress-detail">{{ exportProgress.detail }}</span>
    </div>
    <button @click="cancelExport">取消</button>
  </div>

  <div v-if="showRegister" class="panel">
    <h2>注册 collection（D 类文档站）</h2>
    <div class="form-row">
      <label>id</label>
      <input v-model="reg.id" type="text" placeholder="小写字母数字与连字符，如 volcengine-bytehouse" style="width: 320px" />
      <span class="hint">唯一标识，注册后不可改</span>
    </div>
    <div class="form-row">
      <label>名称 name</label>
      <input v-model="reg.name" type="text" placeholder="显示名（可留空用 id）" style="width: 320px" />
    </div>
    <div class="form-row">
      <label>入口 URL</label>
      <input v-model="reg.entry_url" type="text" placeholder="https://docs.volcengine.com/docs/…" style="width: 420px" @change="probeUrl" />
      <span class="hint">文档库入口地址；失焦自动探测适配器并预填参数</span>
    </div>
    <div v-if="probeResult" class="alert" :class="probeResult.ok ? 'ok' : 'warn'" style="margin: 8px 0">
      {{ probeResult.text }}
    </div>
    <div class="form-row">
      <label>目录树解析器 toc_parser</label>
      <select v-model="reg.toc_parser" style="width: 200px">
        <option v-for="p in parsers" :key="p.name" :value="p.name">{{ p.name }}</option>
      </select>
      <span class="hint">{{ currentParser?.description || '站点适配器：决定目录树与正文的读取方式' }}</span>
    </div>
    <div v-for="f in requiredFields" :key="f.name" class="form-row">
      <label>{{ f.name }}（必填）</label>
      <input v-model="reg[f.name]" type="text" :placeholder="f.placeholder" style="width: 280px" />
      <span class="hint">{{ f.hint }}</span>
    </div>
    <div class="form-row">
      <label>语言 lang</label>
      <input v-model="reg.lang" type="text" style="width: 80px" />
    </div>
    <div class="alert warn" style="margin: 10px 0">
      注册即后台全量首抓，页面越多耗时越久。落盘页面若为 normalized，AI 整理<b>默认手动触发</b>：在目录树里点开某页
      「试跑（不落盘）」验效果，满意再「整理这一条」或到总览页「跑一批」；要在参数设置里把
      trigger_mode 切成 auto 才会按 batch_size 周期自动整理。每页产出 tags + 一张 draft 摘要卡（进审核台待人工晋升）。
      费用取决于整理后端：GLM 等云端产生外部 API 费用，本地 Ollama 零外部费用但耗本地算力。随时可关闭 ai.enabled 止损（热生效）。
    </div>
    <button class="primary" :disabled="registering || !reg.id || !reg.entry_url" @click="submitRegister">
      {{ registering ? '注册中…' : '注册并开始首抓' }}
    </button>
  </div>

  <div style="display: flex; gap: 16px; align-items: flex-start">
    <div class="panel" style="width: max-content; max-width: 60%; min-width: 280px">
      <!-- v0.28：目录树面板宽度随内容自适应（width: max-content），不再固定比例留白 -->
      <h2>{{ currentName }} 目录树</h2>
      <p v-if="!current" class="muted">先注册 collection（POST /api/collections）。</p>
      <div class="tree-node" v-for="n in tree" :key="n.path">
        <template v-if="n.page">
          <div class="tree-page" @click="openPage(n)">
            <span :class="{ mono: detailId }">{{ n.page.title || n.name }}</span>
          </div>
        </template>
        <template v-else>
          <details>
            <summary class="tree-dir">{{ n.name }}</summary>
            <div class="tree-children" v-for="c in n.children" :key="c.path">
              <div class="tree-node">
                <template v-if="c.page">
                  <div class="tree-page" @click="openPage(c)">{{ c.page.title || c.name }}</div>
                </template>
                <template v-else>
                  <details>
                    <summary class="tree-dir">{{ c.name }}</summary>
                    <div class="tree-children">
                      <div class="tree-page" v-for="g in c.children" :key="g.path" @click="openPage(g)">
                        {{ g.page?.title || g.name }}
                      </div>
                    </div>
                  </details>
                </template>
              </div>
            </div>
          </details>
        </template>
      </div>
    </div>

    <div class="panel" style="flex: 1; min-width: 0" v-if="detail">
      <h2>{{ detail.frontmatter.title }}</h2>
      <p>
        <span class="badge st-enriched">{{ detail.frontmatter.status }}</span>
        <span v-if="detail.frontmatter.ai" class="badge ai-mark">AI 生成内容</span>
        <a v-if="detail.frontmatter.url" :href="detail.frontmatter.url as string" target="_blank" class="muted" style="margin-left: 8px">原文</a>
      </p>
      <p v-if="(detail.frontmatter.tags as string[])?.length" class="muted">
        tags：<span class="mono">{{ (detail.frontmatter.tags as string[]).join('、') }}</span>
      </p>

      <!-- v0.29 触发门控：试跑零落盘先验效果，满意再落盘这一条；全量自动需在设置页显式切 auto -->
      <div class="form-row">
        <button :disabled="!aiEnabled || !canEnrich || aiBusy !== ''" @click="runPreview">
          {{ aiBusy === 'preview' ? '试跑中…' : '试跑（不落盘）' }}
        </button>
        <button class="primary" :disabled="!aiEnabled || !canEnrich || aiBusy !== ''" @click="enrichThisOne">
          {{ aiBusy === 'run' ? '整理中…' : '整理这一条' }}
        </button>
        <span class="hint">
          <template v-if="!aiEnabled">未开启 AI 整理总闸（参数设置 → AI 模型 → ai.enabled）</template>
          <template v-else-if="!canEnrich">当前状态无需整理（仅 normalized / error 可加工）</template>
          <template v-else>当前触发：{{ triggerMode === 'auto' ? '自动' : '手动' }}——试跑只调模型看结果，不写库</template>
        </span>
      </div>
      <div v-if="aiNotice" class="alert" style="margin: 8px 0">{{ aiNotice }}</div>
      <div v-if="preview" class="alert" style="margin: 8px 0">
        <p style="margin: 0 0 6px">
          <b>试跑结果（未落盘）</b>
          <span class="muted mono">{{ preview.provider }} → {{ preview.base_url }} · {{ preview.model }} · 耗时 {{ preview.elapsed_ms }}ms</span>
        </p>
        <p style="margin: 0 0 6px">
          tags：<span class="mono">{{ preview.tags.join('、') || '（空）' }}</span>
          <span v-if="preview.confidence !== null" class="muted"> · 置信度 {{ preview.confidence }}</span>
        </p>
        <p style="margin: 0 0 4px" class="muted">摘要：{{ preview.summary }}</p>
        <div class="md-body" style="max-height: 260px; overflow: auto" v-html="renderMarkdown(preview.card_body)"></div>
        <p style="margin: 6px 0 0"><button class="primary" @click="enrichThisOne">满意，落盘这一条</button></p>
      </div>

      <div class="md-body" v-html="renderMarkdown(detail.body)"></div>
    </div>
  </div>
</template>
