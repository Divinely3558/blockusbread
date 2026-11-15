/* 层管理：单一 Esc 归属、焦点归还、Tab 圈定 */
"use strict";

const layers = new Map();   // id -> {el, hooks, prevFocus}
const stack = [];

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function register(id, hooks = {}) {
  const el = document.getElementById(id);
  if (!el) return;
  layers.set(id, { el, hooks, prevFocus: null });
}

export function isOpen(id) {
  return stack.includes(id);
}

export function open(id, focusSel) {
  const layer = layers.get(id);
  if (!layer || stack.includes(id)) return;
  layer.prevFocus = document.activeElement;
  layer.el.hidden = false;
  stack.push(id);
  if (layer.hooks.onOpen) layer.hooks.onOpen();
  syncScrim();
  const target = focusSel ? layer.el.querySelector(focusSel) : firstFocusable(layer.el);
  if (target) target.focus();
}

export function close(id) {
  const i = stack.indexOf(id);
  if (i === -1) return;
  const layer = layers.get(id);
  stack.splice(i, 1);
  layer.el.hidden = true;
  if (layer.hooks.onClose) layer.hooks.onClose();
  const prev = layer.prevFocus;
  layer.prevFocus = null;
  // 归还给打开它的元素；若它自己也在被关掉的层里（已不可见）则交给浏览器回落到背景
  // 顺序要紧：先解除背景 inert，再还焦点，否则 inert 元素聚焦会被忽略
  syncScrim();
  if (prev && document.contains(prev) && prev.getClientRects().length) prev.focus();
}

export function closeTop() {
  if (stack.length) close(stack[stack.length - 1]);
  return stack.length > 0;
}

export function closeAll() {
  while (stack.length) close(stack[stack.length - 1]);
}

function firstFocusable(el) {
  return [...el.querySelectorAll(FOCUSABLE)].find((n) => n.offsetParent !== null);
}

// 只保留最上层可交互：被覆盖的层与层外背景一起 inert，Tab 才走不出去
function syncScrim() {
  const top = stack[stack.length - 1];
  for (const [id, layer] of layers) {
    if (!layer.el.hidden) layer.el.inert = id !== top;
  }
  const bg = [document.getElementById("shell"), document.getElementById("login")];
  for (const el of bg) if (el) el.inert = stack.length > 0;   // 隐藏时也写回，避免留个 inert 给下次登录
  // 手机上拖动遮罩不该让底下的列表跟着滚：层开着就锁住文档滚动
  document.documentElement.classList.toggle("overlay-open", stack.length > 0);
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && stack.length) {
    e.preventDefault();
    closeTop();
    return;
  }
  if (e.key !== "Tab" || !stack.length) return;
  const layer = layers.get(stack[stack.length - 1]);
  const items = [...layer.el.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
});

document.addEventListener("click", (e) => {
  const dismiss = e.target.closest("[data-dismiss]");
  if (dismiss) {
    close(dismiss.dataset.dismiss);
    return;
  }
  const closer = e.target.closest("[data-close]");
  if (closer) {
    const layer = closer.closest(".overlay");
    if (layer) close(layer.id);
  }
});
