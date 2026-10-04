/**
 * Markdown → HTML 的**唯一渲染出口**（§4.5 v0.35）。
 *
 * 为什么必须净化：wiki 卡正文与摘要卡内容均来自 LLM 输出（不可信输入），
 * marked v18 已移除 `sanitize` 选项，直接 `v-html` 等于开 XSS 口子。
 * 统一走这里，保证"一处配置、全站一致"，也便于将来收紧白名单。
 *
 * 口径：
 * - 禁原生 HTML（DOMPurify 剔除 script/事件属性/iframe 等）；
 * - 外链一律 `target="_blank" rel="noopener noreferrer"`（知识库正文常带原站链接）；
 * - 图片不特殊处理：卡正文里出现图片时按普通 img 渲染（相对路径 404 属预期）。
 */
import { marked } from 'marked'
import DOMPurify from 'dompurify'

marked.setOptions({ gfm: true, breaks: true })

/** 渲染 Markdown 为已净化的 HTML 字符串。 */
export function renderMarkdown(src: string | undefined | null): string {
  if (!src) return ''
  const raw = marked.parse(src ?? '', { async: false }) as string
  return DOMPurify.sanitize(raw, {
    ADD_ATTR: ['target', 'rel'],
    FORBID_TAGS: ['style', 'form', 'input', 'iframe'],
  })
}

/** 给渲染结果里的外链补 target/rel（DOMPurify 钩子，保留原 HTML 不重排）。 */
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.getAttribute('href')?.startsWith('http')) {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})
