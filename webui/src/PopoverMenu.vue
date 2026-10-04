<script setup lang="ts">
/**
 * 受控下拉浮层（业界 dropdown/popover pattern，行为对标 Ant Design Dropdown、
 * Radix Popover、Headless UI Dismiss 三家通行契约）：
 * 原生 <details> 只有 UA 的 toggle 语义，缺三项浮层基本契约——「点击外部关闭」
 * 「Escape 关闭」「选中后收起」，故把开关状态提升为受控 ref；外部 dismiss 由
 * document 级 pointerdown 在捕获阶段判定（早于目标元素自身处理，与 Radix 一致）。
 * 无障碍按 WAI-ARIA Menu Button 模式：aria-haspopup/aria-expanded、浮层内
 * roving tabindex（打开时项 tabindex=-1，键盘上下遍历，关闭后焦点回触发器）。
 */
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

const open = ref(false)
const root = ref<HTMLElement | null>(null) // 触发器 + 浮层的共同祖先：判定点击是否落在浮层外
const trigger = ref<HTMLButtonElement | null>(null)
const menu = ref<HTMLElement | null>(null)

/** 可聚焦菜单项（DOM 顺序即键盘遍历顺序），禁用项跳过 */
function menuItems(): HTMLButtonElement[] {
  return menu.value ? Array.from(menu.value.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')) : []
}

/** roving focus：浮层打开期间项不参与 Tab（Tab 直接关闭浮层，焦点交给触发器之后的下一个控件） */
function lockTabOrder() {
  for (const b of menu.value?.querySelectorAll<HTMLButtonElement>('button') ?? []) b.tabIndex = -1
}

/** 相对当前焦点项移动；to='first'/'last' 为首尾（键盘打开时直接落到首/末项） */
function moveFocus(delta: number | 'first' | 'last') {
  const items = menuItems()
  if (!items.length) return
  const cur = items.indexOf(document.activeElement as HTMLButtonElement)
  let next: number
  if (delta === 'first') next = 0
  else if (delta === 'last') next = items.length - 1
  else next = cur < 0 ? 0 : (cur + delta + items.length) % items.length
  items[next].focus()
}

async function openMenu(to: number | 'first' | 'last' | 'none' = 'none') {
  if (open.value) {
    // 已打开时按 ↑/↓：把焦点移入菜单（否则键盘用户无法进入浮层）
    if (to !== 'none') moveFocus(to)
    return
  }
  open.value = true
  await nextTick()
  lockTabOrder()
  if (to !== 'none') moveFocus(to)
}

function close(restoreFocus = false) {
  if (!open.value) return
  open.value = false // 浮层 DOM 随之卸载，tabindex 无需复原（重建即默认 0）
  if (restoreFocus) nextTick(() => trigger.value?.focus())
}

/** 触发器 click：键盘 Enter/Space 产生的 click detail=0（据此判定键盘触发并聚焦首项，AntD/Radix 同款区分） */
function toggle(e: MouseEvent) {
  if (open.value) close()
  else openMenu(e.detail === 0 ? 'first' : 'none')
}

function onMenuKeydown(e: KeyboardEvent) {
  switch (e.key) {
    case 'ArrowDown':
      e.preventDefault()
      moveFocus(1)
      break
    case 'ArrowUp':
      e.preventDefault()
      moveFocus(-1)
      break
    case 'Home':
      e.preventDefault()
      moveFocus('first')
      break
    case 'End':
      e.preventDefault()
      moveFocus('last')
      break
    case 'Escape':
      e.preventDefault()
      close(true)
      break
    case 'Tab':
      close() // 不拦默认，焦点按文档顺序自然后移
      break
  }
}

/** 激活菜单项后收起（原生 details 不会自动收起，v0.28 起一直手动绕过） */
function onMenuClick(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('button')) close(true)
}

function onDocPointerDown(e: PointerEvent) {
  if (open.value && root.value && !root.value.contains(e.target as Node)) close()
}

function onDocKeydown(e: KeyboardEvent) {
  if (open.value && e.key === 'Escape') close(true)
}

onMounted(() => {
  document.addEventListener('pointerdown', onDocPointerDown, true)
  document.addEventListener('keydown', onDocKeydown)
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocPointerDown, true)
  document.removeEventListener('keydown', onDocKeydown)
})

defineExpose({ close })
</script>

<template>
  <div ref="root" class="popover-menu">
    <button
      ref="trigger"
      type="button"
      class="popover-trigger"
      aria-haspopup="menu"
      :aria-expanded="open"
      @click="toggle"
      @keydown.down.prevent="openMenu('first')"
      @keydown.up.prevent="openMenu('last')"
    >
      <slot name="trigger" />
    </button>
    <div v-if="open" ref="menu" class="popover-menu-items" role="menu" @keydown="onMenuKeydown" @click="onMenuClick">
      <slot />
    </div>
  </div>
</template>
