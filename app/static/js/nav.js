/* 导航外壳：rail、hash 路由、徽标、数据新鲜度、全局快捷键 */
"use strict";

import { $, $$, api, toast, clock } from "./core.js";
import { state, on, allVolumes, refreshVolumes } from "./store.js";
import { openRemote } from "./remote.js";

const VIEWS = {
  storage: { sel: "#view-storage", title: "存储" },
  jobs: { sel: "#view-jobs", title: "后台传输" },
  sessions: { sel: "#view-sessions", title: "SFTP 会话" },
};

const hooks = new Map();   // route -> cb
let route = "storage";

export function onView(name, cb) {
  hooks.set(name, cb);
}

export function currentRoute() {
  return route;
}

export function init() {
  addEventListener("hashchange", applyRoute);
  $("#btn-collapse").addEventListener("click", toggleRail);
  if (localStorage.getItem("rail") === "open") $("#shell").classList.add("rail-open");
  $("#btn-remote").addEventListener("click", openRemote);
  $("#empty-remote").addEventListener("click", openRemote);
  $("#btn-refresh").addEventListener("click", rescan);
  $("#btn-logout").addEventListener("click", async () => {
    try { await api("POST", "/api/auth/logout"); } catch { /* 忽略 */ }
    document.dispatchEvent(new CustomEvent("app:logout"));
  });

  document.addEventListener("click", (e) => {
    if (e.target.closest('[data-action="rescan"]')) rescan();
  });

  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
    if (document.querySelector(".overlay:not([hidden])")) return;
    const k = e.key.toLowerCase();
    if (k === "r") { e.preventDefault(); rescan(); }
    else if (k === "d") location.hash = "#/storage";
    else if (k === "j") location.hash = "#/jobs";
    else if (k === "s") location.hash = "#/sessions";
  });

  on("volumes", () => {
    $("#btn-remote").hidden = !state.rememberEnabled;
    $("#empty-remote").hidden = !state.rememberEnabled;
    paintAlertBadge();
  });

  applyRoute();
}

function toggleRail() {
  const shell = $("#shell");
  const open = shell.classList.toggle("rail-open");
  localStorage.setItem("rail", open ? "open" : "closed");
  $("#btn-collapse").setAttribute("aria-expanded", String(open));
  $("#btn-collapse").title = open ? "折叠导航" : "展开导航";
}

export async function rescan() {
  const btn = $("#btn-refresh");
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  try {
    await api("POST", "/api/rescan");
    toast("正在刷新设备列表…", "busy");
    setTimeout(refreshVolumes, 800);
  } catch (err) {
    toast(err.message, "err");
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
    }, 900);
  }
}

/* ------------------------------------------------------------ 路由 */

function applyRoute() {
  const want = (location.hash || "").replace("#/", "");
  if (!VIEWS[want]) {
    if (location.hash && location.hash !== "#/storage") {
      location.replace("#/storage");
      return;
    }
    route = "storage";
  } else {
    route = want;
  }
  for (const [name, v] of Object.entries(VIEWS)) {
    $(v.sel).hidden = name !== route;
  }
  for (const a of $$(".rail-item[data-nav]")) {
    if (a.dataset.nav === route) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  $("#page-title").textContent = VIEWS[route].title;
  const cb = hooks.get(route);
  if (cb) cb();
}

/* ------------------------------------------------------------ 徽标 / 新鲜度 */

export function setBadge(name, value, tone) {
  const el = document.querySelector(`.rail-badge[data-badge="${name}"]`);
  const item = document.querySelector(`.rail-item[data-nav="${name}"]`);
  if (!el) return;
  const n = Number(value) || 0;
  el.hidden = n <= 0;
  el.textContent = n > 99 ? "99+" : n;
  if (tone) el.dataset.tone = tone;
  if (item) item.style.display = "";
}

function paintAlertBadge() {
  const bad = allVolumes().filter((r) => r.state === "error").length;
  setBadge("alert", bad, "danger");
}

export function markFresh() {
  setFreshness("", `最近更新 ${clock(Date.now())}`);
}

export function setFreshness(tone, text) {
  const el = $("#freshness");
  el.dataset.tone = tone;
  $("#freshness-text").textContent = text;
}
