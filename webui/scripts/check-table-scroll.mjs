/**
 * 宽表规范机械防线（2026-10-04 沉淀，检索页第三次复发后加）：
 * 校验所有视图里 `table.list` 的紧邻上一非空行是 `.table-scroll` 容器开标签。
 *
 * 背景：04-ui-constraints「宽表格必须容器内滚动」已人工沉淀两次仍复发（检索页漏包），
 * 长 URL/rel 不折行把表格撑出面板。靠人记住不可靠，改为构建期拦截：
 * 本脚本接入 `npm run build`（build: "node scripts/check-table-scroll.mjs && vue-tsc -b && vite build"），
 * 新增含表格的页面漏包时构建直接失败，并报文件与行号。
 *
 * 口径：`<table` 标签的 class 含 `list` 即视为宽表；其上一非空行必须含 `table-scroll`
 * （兼容 `class="table-scroll"`、`v-if/v-else` 与额外 style 属性的写法，与存量 9 处一致）。
 */
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const viewsDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views')
const failures = []

for (const file of readdirSync(viewsDir).filter((f) => f.endsWith('.vue'))) {
  const lines = readFileSync(join(viewsDir, file), 'utf8').split(/\r?\n/)
  lines.forEach((line, i) => {
    if (!/<table[^>]*class="[^"]*\blist\b/.test(line)) return
    // 向上找最近的非空行（允许 table 标签与包裹 div 之间隔注释行）
    let prev = i - 1
    while (prev >= 0 && !lines[prev].trim()) prev--
    while (prev >= 0 && lines[prev].trim().startsWith('<!--')) prev--
    while (prev >= 0 && !lines[prev].trim()) prev--
    const ok = prev >= 0 && lines[prev].includes('table-scroll')
    if (!ok) {
      failures.push(`${file}:${i + 1}  <table class="list"> 上一非空行不是 .table-scroll 容器（宽表规范见 .codebuddy/rules/04-ui-constraints）`)
    }
  })
}

if (failures.length) {
  console.error('[check-table-scroll] 发现裸放的宽表：')
  for (const f of failures) console.error('  ' + f)
  process.exit(1)
}
console.log('[check-table-scroll] OK：所有 table.list 均在 .table-scroll 容器内')
