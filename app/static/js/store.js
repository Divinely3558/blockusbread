/* 单一数据源：快照、实时指标、任务、卷名与查找 */
"use strict";

import { api, humanSize } from "./core.js";

export const state = {
  username: null,
  disks: [],
  local: [],
  remote: [],
  rememberEnabled: false,
  share: null,
  jobs: [],
  sessions: null,
  rates: {},
  usage: {},
  smart: {},
  lastVolSig: null,
};

/* ------------------------------------------------------------ 事件 */

const subs = new Map();

export function on(evt, cb) {
  if (!subs.has(evt)) subs.set(evt, new Set());
  subs.get(evt).add(cb);
  return () => subs.get(evt).delete(cb);
}

export function emit(evt, payload) {
  const set = subs.get(evt);
  if (set) for (const cb of set) cb(payload);
}

/* ------------------------------------------------------------ 拉取 */

export async function refreshVolumes() {
  const snap = await api("GET", "/api/volumes");
  // 数据未变化不重绘：既避免闪烁，也避免清空正在输入的解锁密码
  const sig = JSON.stringify(snap);
  if (sig === state.lastVolSig) return false;
  state.lastVolSig = sig;
  state.disks = snap.disks || [];
  state.local = snap.local || [];
  state.remote = snap.remote || [];
  state.rememberEnabled = !!snap.rememberEnabled;
  emit("volumes");
  return true;
}

export async function refreshJobs() {
  try {
    const data = await api("GET", "/api/jobs");
    applyJobs(data.jobs || []);
  } catch { /* 未登录等场景忽略 */ }
}

export function applyJobs(jobs) {
  state.jobs = jobs;
  emit("jobs");
}

export async function loadSmart(diskId, force = false) {
  if (!(diskId in state.smart)) state.smart[diskId] = null;
  try {
    const info = await api(
      "GET",
      `/api/disks/${encodeURIComponent(diskId)}/smart${force ? "?force=1" : ""}`
    );
    state.smart[diskId] = info;
  } catch {
    delete state.smart[diskId];   // 失败清除占位，下次重绘自动重试
    return;
  }
  emit("smart", diskId);
}

/** 实时指标：速率与容量每周期取，会话频率低一档（interval 参数区分） */
export async function liveTick(withSessions) {
  let rates = null, usage = null, sessions = null;
  try {
    [rates, usage] = await Promise.all([api("GET", "/api/speeds"), api("GET", "/api/usage")]);
    state.rates = rates || {};
    state.usage = usage || {};
  } catch {
    throw new Error("speeds");
  }
  if (withSessions) {
    try {
      sessions = await api("GET", "/api/sessions");
      state.sessions = sessions;
    } catch { /* 会话取不到不影响主视图 */ }
  }
  emit("live", { rates: state.rates, usage: state.usage, sessions: state.sessions });
  patchLive();
}

/* ------------------------------------------------------------ 高频数字定点更新 */

/** 只改文本与宽度，不重建子树：速率/容量跳动的唯一来源 */
export function patchLive() {
  const rates = state.rates, usage = state.usage;
  let sumRx = 0, sumTx = 0;
  for (const el of document.querySelectorAll("[data-speed-key]")) {
    const s = rates[el.dataset.speedKey] || {};
    const rx = s.rx || 0, tx = s.tx || 0;
    const r = el.querySelector("[data-metric=rx]");
    const t = el.querySelector("[data-metric=tx]");
    if (r) r.textContent = humanRateNum(rx);
    if (t) t.textContent = humanRateNum(tx);
    el.classList.toggle("idle", rx < 1 && tx < 1);
    el.dataset.live = rx >= 1 && tx >= 1 ? "both" : rx >= 1 ? "rx" : tx >= 1 ? "tx" : "";
    sumRx += rx; sumTx += tx;
  }
  for (const el of document.querySelectorAll("[data-usage-key]")) {
    const u = usage[el.dataset.usageKey];
    if (!u || !u.total) continue;
    const pct = Math.min(100, Math.round((u.used / u.total) * 100));
    const fill = el.querySelector(".meter-fill");
    const pctEl = el.querySelector(".meter-pct");
    const subEl = el.querySelector(".meter-sub");
    if (fill) fill.style.width = `${pct}%`;
    if (pctEl) pctEl.textContent = `${pct}%`;
    if (subEl) subEl.textContent = `${humanSize(u.used)} / ${humanSize(u.total)}`;
    el.dataset.level = pct >= 97 ? "crit" : pct >= 90 ? "warn" : "ok";
  }
  emit("metrics", { sumRx, sumTx });
}

function humanRateNum(bps) {
  if (!bps || bps < 1) return "0";
  const units = ["B", "K", "M", "G"];
  let v = bps, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)}${units[i]}/s`;
}

/* ------------------------------------------------------------ 卷查找与命名 */

export function findVolume(key) {
  if (!key) return null;
  if (key.startsWith("local:")) {
    const v = state.local.find((x) => x.key === key);
    return v ? { kind: "local", name: v.name, drive: null, vol: v } : null;
  }
  if (key.startsWith("remote:")) {
    const v = state.remote.find((x) => x.key === key);
    if (!v) return null;
    return {
      kind: "remote",
      name: `${v.drive ? `${v.drive}:` : ""}${v.remotePath}`,
      drive: v.drive || null,
      vol: v,
    };
  }
  for (const d of state.disks) {
    const p = d.partitions.find((x) => x.key === key);
    if (p) return { kind: "external", disk: d, part: p };
  }
  return null;
}

export function volumeLabel(part, disk) {
  const label = part.label || `${disk.displayName || disk.id} · 分区 ${part.number}`;
  return part.drive ? `${part.drive}:${label}` : label;
}

export function volumeDisplayName(key) {
  const found = findVolume(key);
  if (!found) return key;
  if (found.part) return volumeLabel(found.part, found.disk);
  return found.name || key;
}

export function driveOf(key) {
  const found = findVolume(key);
  if (!found) return null;
  return found.part ? found.part.drive || null : found.drive || null;
}

export function shortKey(key) {
  if (findVolume(key)) return volumeDisplayName(key);
  const m = key.match(/^(local|remote):(.+)$/);
  if (m) return m[2];
  return key.length > 14 ? `${key.slice(0, 6)}…${key.slice(-4)}` : key;
}

export function routeLabel(key, path) {
  const clean = (path || "").replace(/\/+$/, "");
  if (!clean) return shortKey(key);
  const i = clean.lastIndexOf("/");
  const name = i === -1 ? clean : clean.slice(i + 1);
  const drive = driveOf(key);
  return drive ? `${drive}:${name}` : name;
}

/** 可作为移动 / 复制目标的卷：本地、远程恒可写；外接需读写模式挂载 */
export function writableTargets() {
  const out = [];
  for (const v of state.local) out.push({ key: v.key, label: v.name, group: "本地存储" });
  for (const d of state.disks) {
    for (const p of d.partitions || []) {
      if (p.state === "mounted" && p.mode === "rw") {
        out.push({ key: p.key, label: volumeLabel(p, d), group: "外接存储" });
      }
    }
  }
  for (const v of state.remote) {
    if (v.state === "mounted") {
      out.push({
        key: v.key,
        label: v.drive ? `${v.drive}:${v.remotePath}` : `${v.name}:${v.remotePath}`,
        group: "远程存储",
      });
    }
  }
  return out;
}

export function sftpUrlFor(sub) {
  const s = state.share;
  const host = s ? s.host : location.hostname;
  const port = s ? s.port : 2222;
  const auth = s && s.username ? `${s.username}@` : "";
  return `sftp://${auth}${host}:${port}/${sub || ""}`;
}

/** 全部卷的扁平视图，供 KPI 与查找使用 */
export function allVolumes() {
  const rows = [];
  for (const v of state.local) rows.push({ kind: "local", key: v.key, state: "mounted", mode: "rw", size: 0, vol: v });
  for (const d of state.disks) {
    for (const p of d.partitions || []) {
      rows.push({ kind: "external", key: p.key, state: p.state, mode: p.mode, size: p.size, supported: p.supported, vol: p });
    }
  }
  for (const v of state.remote) {
    rows.push({ kind: "remote", key: v.key, state: v.state, mode: v.mode, size: 0, vol: v });
  }
  return rows;
}
