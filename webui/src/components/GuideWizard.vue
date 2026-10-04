<script setup lang="ts">
/**
 * 首次使用分步向导（§11.8 v0.45）。
 * 纯展示组件：父组件用 v-if 控制显隐，关闭时 emit('done')，localStorage 持久化由父组件负责。
 * 五步内容对齐方案文档口径：状态词用 §4.2/§4.5 枚举原文，不出现自造状态词。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

interface GuideStep {
  title: string
  points: string[]
  /** 可选跳转目标：{ 文案, 路由路径 } */
  action?: { label: string; to: string }
}

const STEPS: GuideStep[] = [
  {
    title: '欢迎 · 这个工作台怎么运转',
    points: [
      '内容流水线：投递 → 归一化 → AI 整理 → 人工审核，每一步的待办数量都常驻在总览页顶部的漏斗里。',
      'kb/ 目录是唯一事实源，所有卡片和原文都以文件形式保存，随时可备份、可迁移。',
      '跟着本向导走一遍，之后漏斗会提示你当前该做的事。',
    ],
  },
  {
    title: '① 投递与归一化（自动，无需人工）',
    points: [
      '浏览器扩展右键、手机分享、命令行投递的内容先落入 inbox/ 暂存。',
      '服务会自动抓取正文并转成 Markdown（note.md），状态变为 normalized；无需你操作。',
      '失败的条目不会消失，会出现在总览页的 error 巡检面板，可重跑或丢弃。',
    ],
  },
  {
    title: '② AI 整理（默认手动，试跑先行）',
    points: [
      '默认手动触发：总览页「跑一批」，或在文档树页对单页先「试跑（不落盘）」预览效果。',
      '全量自动整理需在参数设置页把触发模式切为 auto；连续失败会熔断暂停，恢复需人工确认。',
      'AI 整理会打标签、写摘要，并生成摘要卡/实体卡。',
    ],
    action: { label: '去参数设置看 AI 配置', to: '/settings' },
  },
  {
    title: '③ 审核：AI 写的不算数，你说了算',
    points: [
      'AI 产出的卡片一律是 draft（草稿），不会进入检索和图谱。',
      '在审核台逐张把关：满意点「晋升」（promoted）才对外可见；不满意可「打回重生成」「修订」或「删除」。',
      '这个设计保证 AI 的错误总结永远污染不了你的知识库。',
    ],
    action: { label: '去审核台', to: '/curation' },
  },
  {
    title: '完成 · 接下来交给漏斗',
    points: [
      '顶部漏斗会随数据变化提示当前建议动作，点对应站点即可跳到操作位置。',
      '想再看本向导，随时点漏斗旁的「重新查看指引」。',
    ],
  },
]

const emit = defineEmits<{ done: [] }>()

const idx = ref(0)
const step = computed(() => STEPS[idx.value]!)
const isLast = computed(() => idx.value === STEPS.length - 1)

function next() {
  if (isLast.value) emit('done')
  else idx.value++
}
function prev() {
  if (idx.value > 0) idx.value--
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('done')
  else if (e.key === 'ArrowRight') next()
  else if (e.key === 'ArrowLeft') prev()
}
onMounted(() => document.addEventListener('keydown', onKey))
onBeforeUnmount(() => document.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="gw-overlay" role="dialog" aria-modal="true" aria-label="新手引导">
    <div class="gw-card">
      <div class="gw-head">
        <span class="gw-progress">第 {{ idx + 1 }} / {{ STEPS.length }} 步</span>
        <button class="gw-close" aria-label="关闭引导" @click="emit('done')">×</button>
      </div>

      <div class="gw-dots" aria-hidden="true">
        <span v-for="(s, i) in STEPS" :key="i" class="gw-dot" :class="{ on: i === idx }" />
      </div>

      <h3>{{ step.title }}</h3>
      <ul>
        <li v-for="(p, i) in step.points" :key="i">{{ p }}</li>
      </ul>

      <div v-if="step.action" class="gw-action">
        <router-link :to="step.action.to" class="gw-link" @click="emit('done')">
          {{ step.action.label }} →
        </router-link>
      </div>

      <div class="gw-foot">
        <button :disabled="idx === 0" @click="prev">上一步</button>
        <span class="gw-hint">Esc 关闭 · ← → 切换</span>
        <button class="primary" @click="next">{{ isLast ? '完成' : '下一步' }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.gw-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.gw-card {
  background: var(--bg-panel, #fff);
  border-radius: 10px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
  width: min(560px, calc(100vw - 40px));
  max-height: calc(100vh - 80px);
  overflow: auto;
  padding: 20px 24px;
}
.gw-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.gw-progress {
  font-size: 12px;
  color: #888;
}
.gw-close {
  border: none;
  background: none;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  color: #888;
  padding: 2px 8px;
}
.gw-close:hover {
  color: #333;
}
.gw-dots {
  display: flex;
  gap: 6px;
  margin: 10px 0 4px;
}
.gw-dot {
  width: 24px;
  height: 4px;
  border-radius: 2px;
  background: #e0e0e0;
}
.gw-dot.on {
  background: #3b82f6;
}
.gw-card h3 {
  margin: 12px 0 8px;
  font-size: 16px;
}
.gw-card ul {
  margin: 0;
  padding-left: 18px;
  color: #444;
  font-size: 13.5px;
  line-height: 1.8;
}
.gw-action {
  margin-top: 12px;
}
.gw-link {
  font-size: 13.5px;
  color: #3b82f6;
  text-decoration: none;
}
.gw-link:hover {
  text-decoration: underline;
}
.gw-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 20px;
}
.gw-hint {
  flex: 1;
  text-align: center;
  font-size: 12px;
  color: #aaa;
}
</style>
