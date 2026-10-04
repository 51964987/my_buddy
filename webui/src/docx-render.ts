/**
 * merged Markdown → docx 渲染器（§6 v0.31 全文导出嵌图版）。
 * 独立模块：docx/marked 体积大，经 docx-render.worker.ts 在 worker 线程加载（主 bundle 不背），
 * 主线程侧入口为 docx-render-client.ts（v0.39 起渲染/序列化不进主线程）。
 * 中保真口径：标题层级/段落/列表/表格/代码块 + 图片嵌入。
 * 图片：merged 取 images=api 变体（图片链接为后端 image 端点 URL），逐图拉取（并发限流+缓存）后
 * 以 ImageRun 嵌入；仅支持 Word 原生格式 jpg/png/gif/bmp（webp 等 docx 库不支持），尺寸自文件头
 * 解析（PNG/GIF/BMP/JPEG SOF），未知格式或拉取失败降级占位文本，不整体失败。
 */
import {
  AlignmentType,
  BorderStyle,
  Document,
  ExternalHyperlink,
  HeadingLevel,
  ImageRun,
  Packer,
  Paragraph,
  ShadingType,
  Table,
  TableCell,
  TableRow,
  TextRun,
  WidthType,
} from 'docx'
import { marked } from 'marked'
import type { Tokens } from 'marked'

const MAX_BULLET_LEVEL = 8 // Word 项目符号级别上限 9 级

/** 渲染进度（v0.39）：images 阶段带 done/total，render/pack 为阶段开始通知 */
export interface RenderProgress {
  stage: 'images' | 'render' | 'pack'
  done?: number
  total?: number
}

/** 渲染选项：token 供 worker 内直连 image API（worker 无 localStorage）；onProgress 阶段进度 */
export interface RenderOpts {
  token?: string
  onProgress?: (p: RenderProgress) => void
}

/* 文档默认样式（v0.37，对齐参考脚本 crawl_volc_docs.py 的 Word 产物排版基线）：
 * 微软雅黑（ascii/hAnsi/eastAsia 三槽全设，Word 中英文统一字体）、11pt、1.5 倍行距、段后 6pt。
 * 声明在 Normal（document default）上，标题/列表/表格单元格经样式继承，无需逐段设置。 */
const BODY_FONT = { ascii: '微软雅黑', hAnsi: '微软雅黑', eastAsia: '微软雅黑' }
const DEFAULT_STYLES = {
  default: {
    document: {
      run: { font: BODY_FONT, size: 22 }, // size 半磅：22 = 11pt
      paragraph: { spacing: { line: 360, after: 120 } }, // 1.5 倍行距（240=单倍）+ 段后 6pt（120 twip）
    },
  },
}

/* 表格单线边框（同 Word 内置 Table Grid 样式：0.5pt 单线，size 单位 1/8 磅）；
 * docx 库不指定 borders 时表格边框样式不确定，显式声明保证与脚本产物一致 */
const TABLE_GRID_BORDERS = {
  top: { style: BorderStyle.SINGLE, size: 4, color: 'auto' },
  bottom: { style: BorderStyle.SINGLE, size: 4, color: 'auto' },
  left: { style: BorderStyle.SINGLE, size: 4, color: 'auto' },
  right: { style: BorderStyle.SINGLE, size: 4, color: 'auto' },
  insideHorizontal: { style: BorderStyle.SINGLE, size: 4, color: 'auto' },
  insideVertical: { style: BorderStyle.SINGLE, size: 4, color: 'auto' },
}

// 有序列表编号定义：9 级缩进（docx 要求数字编号在 Document.numbering 声明）
const NUMBERING_LEVELS = Array.from({ length: MAX_BULLET_LEVEL + 1 }, (_, i) => ({
  level: i,
  format: 'decimal' as const,
  text: `%${i + 1}.`,
  alignment: AlignmentType.START,
  style: { paragraph: { indent: { left: 720 * (i + 1), hanging: 360 } } },
}))

/* ---------------- 图片拉取与尺寸解析（v0.31 嵌图） ---------------- */

const IMAGE_FETCH_CONCURRENCY = 6 // 并发限流：避免数百图同时拉取压垮本地服务
const FALLBACK_WIDTH = 550 // 尺寸解析失败时的回退显示尺寸
const FALLBACK_HEIGHT = 350
// 嵌入尺寸框（v0.37，对齐参考脚本 add_image_to_doc 的 max_w=6.3in / max_h=7.0in，96dpi 换算）：
// 等比缩入框内、不放大原尺寸；此前仅限宽，竖长截图会溢出页面
const MAX_DISPLAY_WIDTH = 605 // 6.3in × 96dpi
const MAX_DISPLAY_HEIGHT = 672 // 7.0in × 96dpi

type ImageAsset = {
  bytes: Uint8Array
  type: 'jpg' | 'png' | 'gif' | 'bmp' // docx 库 ImageRun 支持的格式（webp 不支持 → 占位）
  width: number
  height: number
} | null

function u16be(b: Uint8Array, i: number): number {
  return (b[i] << 8) | b[i + 1]
}

function u32be(b: Uint8Array, i: number): number {
  return (((b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]) >>> 0)
}

function u16le(b: Uint8Array, i: number): number {
  return b[i] | (b[i + 1] << 8)
}

function imageType(b: Uint8Array): 'jpg' | 'png' | 'gif' | 'bmp' | null {
  if (b.length > 8 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return 'png'
  if (b.length > 3 && b[0] === 0xff && b[1] === 0xd8) return 'jpg'
  if (b.length > 6 && b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46) return 'gif'
  if (b.length > 2 && b[0] === 0x42 && b[1] === 0x4d) return 'bmp'
  return null // webp/其他：Word 原生不支持，降级占位
}

/** 从文件头解析像素尺寸（PNG/GIF/BMP/JPEG SOF 扫描），失败返回 null */
function parseImageSize(b: Uint8Array, type: string): { width: number; height: number } | null {
  try {
    if (type === 'png' && b.length >= 24) {
      return { width: u32be(b, 16), height: u32be(b, 20) }
    }
    if (type === 'gif' && b.length >= 10) {
      return { width: u16le(b, 6), height: u16le(b, 8) }
    }
    if (type === 'bmp' && b.length >= 26) {
      // BITMAPINFOHEADER：宽高为有符号 int32，高可能为负（顶底颠倒）
      const dv = new DataView(b.buffer, b.byteOffset, b.byteLength)
      return { width: Math.abs(dv.getInt32(18, true)), height: Math.abs(dv.getInt32(22, true)) }
    }
    if (type === 'jpg') {
      // JPEG：逐段扫描 SOF0~SOF15（跳过 DHT/DAC/JPG/RST 无尺寸段）
      let i = 2
      while (i + 9 < b.length) {
        if (b[i] !== 0xff) {
          i += 1
          continue
        }
        const marker = b[i + 1]
        if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
          return { height: u16be(b, i + 5), width: u16be(b, i + 7) }
        }
        i += 2 + u16be(b, i + 2)
      }
    }
  } catch {
    return null
  }
  return null
}

async function fetchImage(href: string, token?: string): Promise<ImageAsset> {
  try {
    // 直连 fetch（v0.39）：渲染可在 worker 内执行，worker 无 localStorage，
    // token 由主线程传入；401 属配置问题，抛出让上层报错而非整文档静默占位
    const headers: Record<string, string> = {}
    if (token) headers['X-KB-Token'] = token
    const resp = await fetch(href, { headers })
    if (resp.status === 401) throw new Error('需要 token：请在「参数设置 → 服务」填写与服务端一致的 token')
    if (!resp.ok) return null // 单图失败不整体失败，降级占位
    const bytes = new Uint8Array(await resp.arrayBuffer())
    const type = imageType(bytes)
    if (!type) return null
    const size = parseImageSize(bytes, type) ?? { width: FALLBACK_WIDTH, height: FALLBACK_HEIGHT }
    return { bytes, type, width: size.width, height: size.height }
  } catch (e) {
    if (e instanceof Error && e.message.startsWith('需要 token')) throw e // 鉴权问题不吞
    return null // 单图失败不整体失败，降级占位
  }
}

/** 并发限流拉取全部图片（去重 + 缓存），产出 href → asset 映射；逐图回报进度（v0.39） */
async function fetchAllImages(
  hrefs: Iterable<string>,
  opts: RenderOpts = {},
): Promise<Map<string, ImageAsset>> {
  const unique = [...new Set(hrefs)]
  const result = new Map<string, ImageAsset>()
  let idx = 0
  let done = 0
  const workers = Array.from({ length: Math.min(IMAGE_FETCH_CONCURRENCY, unique.length) }, async () => {
    while (idx < unique.length) {
      const href = unique[idx]
      idx += 1
      result.set(href, await fetchImage(href, opts.token))
      done += 1
      opts.onProgress?.({ stage: 'images', done, total: unique.length })
    }
  })
  await Promise.all(workers)
  return result
}

/** 递归收集词法树中所有 image token 的 href（仅 images=api 产出的站内 API 链接） */
function collectImageHrefs(token: unknown, out: Set<string>): void {
  if (!token || typeof token !== 'object') return
  const t = token as Record<string, unknown>
  if (t.type === 'image' && typeof t.href === 'string' && t.href.startsWith('/api/')) {
    out.add(t.href)
  }
  for (const v of Object.values(t)) {
    if (Array.isArray(v)) v.forEach((item) => collectImageHrefs(item, out))
    else if (v && typeof v === 'object') collectImageHrefs(v, out)
  }
}

function scaledSize(asset: NonNullable<ImageAsset>): { width: number; height: number } {
  const scale = Math.min(1, MAX_DISPLAY_WIDTH / asset.width, MAX_DISPLAY_HEIGHT / asset.height)
  return { width: Math.max(1, Math.round(asset.width * scale)), height: Math.max(1, Math.round(asset.height * scale)) }
}

function imagePlaceholderRun(): TextRun {
  return new TextRun({ text: '（图片不可嵌入或拉取失败，见 zip 导出版）', italics: true, color: '888888' })
}

/** image token → ImageRun / 占位（按拉取结果） */
function imageChild(href: string, images: Map<string, ImageAsset>, base: { bold?: boolean; italics?: boolean; color?: string; style?: string } = {}): ImageRun | TextRun {
  const asset = images.get(href)
  if (!asset) return imagePlaceholderRun()
  return new ImageRun({ type: asset.type, data: asset.bytes, transformation: scaledSize(asset) })
}

/**
 * 解包纯包裹型 HTML 标签（span/div/p）：D 类正文普遍写成 `<span>![图片](url)</span>`，
 * marked 不解析标签内部的 markdown，不解包则图片整段以文本流失（v0.31 实测 34 处）。
 * `<br>` 不在此处理（表格单元格内换行不能换成空行——会破坏 markdown 表格结构），
 * 交由渲染层 inline html 分支映射为硬换行。
 */
function unwrapInlineHtml(md: string): string {
  return md.replace(/<\/?(?:span|div|p)(?:\s[^>]*)?>/gi, '')
}

/** 段内 html token：`<br>`/`<br/>` → 硬换行；其余 html 原样保留（表格/代码等固有损耗） */
function htmlTokenRun(raw: string, base: BlockStyle = {}): TextRun {
  if (/^<br\s*\/?>$/i.test(raw.trim())) return new TextRun({ text: '', break: 1, ...runOpts(base) })
  return new TextRun({ text: raw, ...runOpts(base) })
}

/* ---------------- Markdown 块级渲染（沿用 v0.30 口径） ---------------- */

function headingLevel(depth: number) {
  const levels = [
    HeadingLevel.HEADING_1,
    HeadingLevel.HEADING_2,
    HeadingLevel.HEADING_3,
    HeadingLevel.HEADING_4,
    HeadingLevel.HEADING_5,
    HeadingLevel.HEADING_6,
  ]
  return levels[Math.min(depth, levels.length) - 1]
}

/** 块级样式：随调用链传递（blockquote 内部按块递归时继承灰斜体 + 缩进，v0.31） */
interface BlockStyle {
  bold?: boolean
  italics?: boolean
  color?: string
  style?: string
  indentLeft?: number
}

/** 挑出 TextRun 可识别的样式字段（indentLeft 是段落属性，不能进 run） */
function runOpts(base: BlockStyle) {
  return { bold: base.bold, italics: base.italics, color: base.color, style: base.style }
}

function inlineRuns(
  tokens: Tokens.Generic[],
  images: Map<string, ImageAsset>,
  base: BlockStyle = {},
): (TextRun | ExternalHyperlink | ImageRun)[] {
  const out: (TextRun | ExternalHyperlink | ImageRun)[] = []
  for (const t of tokens) {
    switch (t.type) {
      case 'text':
        if (t.tokens?.length) out.push(...inlineRuns(t.tokens, images, base))
        else out.push(new TextRun({ text: t.text, ...runOpts(base) }))
        break
      case 'strong':
        out.push(...inlineRuns(t.tokens ?? [], images, { ...base, bold: true }))
        break
      case 'em':
      case 'del':
        out.push(...inlineRuns(t.tokens ?? [], images, { ...base, italics: true }))
        break
      case 'codespan':
        out.push(new TextRun({ text: t.text, ...runOpts(base), font: 'Consolas', color: base.color ?? 'C7254E' }))
        break
      case 'link':
        // ExternalHyperlink = OOXML w:hyperlink，Word 中真正可点击（Hyperlink 字符样式仅负责蓝色下划线外观）
        out.push(
          new ExternalHyperlink({
            children: inlineRuns(t.tokens ?? [], images, { ...base, style: 'Hyperlink' }) as TextRun[],
            link: t.href,
          }),
        )
        break
      case 'br':
        out.push(new TextRun({ text: '', break: 1, ...runOpts(base) }))
        break
      case 'image':
        out.push(imageChild(t.href, images, base))
        break
      case 'html':
        out.push(htmlTokenRun(t.raw, base))
        break
      case 'escape':
        out.push(new TextRun({ text: t.text, ...runOpts(base) }))
        break
      default:
        // 块级 token 误入行内（blockquote 等容器直接调用 inlineRuns 时）：按 raw 兜底
        if (t.raw) out.push(new TextRun({ text: t.raw, ...runOpts(base) }))
    }
  }
  return out
}

function codeParagraphs(code: string, base: BlockStyle = {}): Paragraph[] {
  // 代码块：等宽 + 浅灰底，逐行成段保留换行
  return code
    .replace(/\n$/, '')
    .split('\n')
    .map(
      (line) =>
        new Paragraph({
          shading: { type: ShadingType.CLEAR, fill: 'F5F5F5' },
          spacing: { after: 0 },
          ...(base.indentLeft ? { indent: { left: base.indentLeft } } : {}),
          children: [new TextRun({ text: line || ' ', font: 'Consolas', size: 18, ...runOpts(base) })],
        }),
    )
}

function listParagraphs(token: Tokens.List, depth: number, images: Map<string, ImageAsset>, base: BlockStyle = {}): (Paragraph | Table)[] {
  // 列表项 tokens 是混装容器（行内 text/paragraph/image + 块级 list/code/table）：
  // 块级 token 必须显式分派，落到 else if (t.raw) 分支会被静默丢弃
  // （table token 无 raw → 整表连图片丢失，v0.31 实测 34 张）。
  const out: (Paragraph | Table)[] = []
  for (const item of token.items) {
    const runs: (TextRun | ExternalHyperlink | ImageRun)[] = []
    for (const t of item.tokens) {
      if (t.type === 'text' && 'tokens' in t && t.tokens?.length) {
        runs.push(...inlineRuns(t.tokens as Tokens.Generic[], images, base))
      } else if (t.type === 'paragraph') {
        runs.push(...inlineRuns((t as Tokens.Paragraph).tokens ?? [], images, base))
      } else if (t.type === 'list') {
        out.push(...listParagraphs(t as Tokens.List, depth + 1, images, base)) // 嵌套列表随后输出
      } else if (t.type === 'code') {
        out.push(...codeParagraphs((t as Tokens.Code).text, base))
      } else if (t.type === 'table') {
        // 列表项内嵌表格（v0.31 修：此前落入 else if (t.raw) 分支，table token 无 raw，
        // 整表连同其中图片被静默丢弃——实测 314 页文档丢 34 张）
        out.push(buildTable(t as Tokens.Table, images, base))
      } else if (t.type === 'image') {
        runs.push(imageChild((t as Tokens.Image).href, images, base))
      } else if (t.type === 'space') {
        continue
      } else if ('tokens' in (t as Tokens.Generic)) {
        runs.push(...inlineRuns((t as Tokens.Generic).tokens ?? [], images, base))
      } else if (t.raw) {
        runs.push(new TextRun({ text: t.raw, ...runOpts(base) }))
      }
    }
    const level = Math.min(depth, MAX_BULLET_LEVEL)
    out.push(
      new Paragraph({
        spacing: { after: 40 },
        ...(base.indentLeft ? { indent: { left: base.indentLeft } } : {}),
        children: runs,
        ...(token.ordered ? { numbering: { reference: 'kb-num', level } } : { bullet: { level } }),
      }),
    )
  }
  return out
}

function buildTable(token: Tokens.Table, images: Map<string, ImageAsset>, base: BlockStyle = {}): Table {
  const cellPara = (cell: { tokens?: Tokens.Generic[] }) =>
    new Paragraph({
      children: inlineRuns(cell.tokens ?? [], images, base),
      ...(base.indentLeft ? { indent: { left: base.indentLeft } } : {}),
    })
  const header = new TableRow({
    tableHeader: true,
    children: token.header.map(
      (cell) =>
        new TableCell({
          children: [cellPara(cell)],
          shading: { type: ShadingType.CLEAR, fill: 'F0F3F8' },
        }),
    ),
  })
  const rows = token.rows.map(
    (row) => new TableRow({ children: row.map((cell) => new TableCell({ children: [cellPara(cell)] })) }),
  )
  return new Table({ width: { size: 100, type: WidthType.PERCENTAGE }, borders: TABLE_GRID_BORDERS, rows: [header, ...rows] })
}

function blockToParagraphs(token: Tokens.Generic, images: Map<string, ImageAsset>, base: BlockStyle = {}): (Paragraph | Table)[] {
  switch (token.type) {
    case 'heading': {
      if (token.depth > 6) {
        return [new Paragraph({ children: [new TextRun({ text: token.text, ...runOpts(base), bold: true })] })]
      }
      // 走 inlineRuns 而非 plainText：标题内的图片/链接不应被拍平丢弃（v0.31 举一反三）
      return [
        new Paragraph({
          children: inlineRuns(token.tokens ?? [], images, base),
          heading: headingLevel(token.depth),
          ...(base.indentLeft ? { indent: { left: base.indentLeft } } : {}),
        }),
      ]
    }
    case 'paragraph':
      return [
        new Paragraph({
          children: inlineRuns(token.tokens ?? [], images, base),
          spacing: { after: 120 },
          ...(base.indentLeft ? { indent: { left: base.indentLeft } } : {}),
        }),
      ]
    case 'code':
      return codeParagraphs(token.text, base)
    case 'blockquote': {
      // 引用块：内部按块递归渲染（引用内可能是段落/列表/代码/图片——v0.31 修：原先拍平丢内容），继承灰斜体 + 左缩进
      const inner: BlockStyle = { ...base, italics: true, color: '666666', indentLeft: (base.indentLeft ?? 0) + 480 }
      const out: (Paragraph | Table)[] = []
      for (const child of (token.tokens ?? []) as Tokens.Generic[]) {
        out.push(...blockToParagraphs(child, images, inner))
      }
      return out
    }
    case 'list':
      return listParagraphs(token as Tokens.List, 0, images, base)
    case 'table':
      return [buildTable(token as Tokens.Table, images, base)]
    case 'hr':
    case 'space':
      return []
    default:
      return token.raw?.trim() ? [new Paragraph({ text: token.raw, ...runOpts(base) })] : []
  }
}

export async function renderDocxBlob(
  merged: string,
  header?: { title?: string; note?: string }, // 可选文档头（v0.32：zip 分页 docx 自带 # 标题，不传头避免双标题）
  opts: RenderOpts = {},
): Promise<Blob> {
  // 先解包 span/div/p 包裹（否则标签内 markdown 图片整段流失），再词法分析
  const tokens = marked.lexer(unwrapInlineHtml(merged))
  // 先并发拉取全部图片再渲染（渲染阶段同步查表；单图失败 → 占位文本）
  const hrefs = new Set<string>()
  tokens.forEach((t) => collectImageHrefs(t, hrefs))
  const images = await fetchAllImages(hrefs, opts)

  const children: (Paragraph | Table)[] = []
  opts.onProgress?.({ stage: 'render' }) // 块级渲染（同步，worker 内执行）
  if (header?.title) {
    children.push(new Paragraph({ text: header.title, heading: HeadingLevel.HEADING_1 }))
  }
  if (header?.note) {
    children.push(
      new Paragraph({
        children: [new TextRun({ text: header.note, italics: true, color: '888888' })],
      }),
    )
  }
  for (const t of tokens) {
    children.push(...blockToParagraphs(t as Tokens.Generic, images))
  }
  opts.onProgress?.({ stage: 'pack' }) // 序列化是最大同步长任务，worker 化后主线程仍可见阶段
  const doc = new Document({
    styles: DEFAULT_STYLES,
    numbering: { config: [{ reference: 'kb-num', levels: NUMBERING_LEVELS }] },
    sections: [{ children }],
  })
  return Packer.toBlob(doc)
}
