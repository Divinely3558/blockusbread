/* 存储视图：分组表格 + 状态矩阵 + 行内解锁面板 + SMART + KPI 条 */
"use strict";

import { $, esc, humanSize, api, toast, copyText } from "./core.js";
import {
  state, on, findVolume, sftpUrlFor, loadSmart, allVolumes, refreshVolumes, patchLive,
} from "./store.js";
import { openBrowser } from "./browser.js";

const COLS = 7;
const TRANSIT = ["unlocking", "mounting", "unmounting"];

const openPanels = new Set();     // 展开解锁面板的卷
const draftPanels = new Map();    // key -> 重绘前捕获的面板表单状态
const rowRefs = new Map();        // key -> {main, panel, err}
const rowSigs = new Map();        // key -> 签名
let layoutSig = null;

export function init() {
  on("volumes", render);
  on("smart", paintSmart);
  on("metrics", paintLive);
  document.addEventListener("click", onClick);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeMenus();
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const tr = e.target.closest?.("tr.row[data-key]");
    if (!tr) return;
    if (ROVE.includes(e.key)) {
      e.preventDefault();
      moveRowFocus(tr, e.key);
      return;
    }
    const item = findVolume(tr.dataset.key);
    if (!item) return;
    if (e.key === "Enter" && e.target.tagName !== "BUTTON") {
      e.preventDefault();
      itemMounted(item) ? openBrowser(tr.dataset.key) : togglePanel(tr.dataset.key);
      return;
    }
    if (e.key.toLowerCase() === "e" && itemMounted(item)) {
      e.preventDefault();
      eject(tr.dataset.key, null);
    }
  });
}

const ROVE = ["ArrowUp", "ArrowDown", "Home", "End"];

function moveRowFocus(tr, key) {
  const rows = [...document.querySelectorAll("#storage-body tr.row[data-key]")]
    .filter((r) => r.offsetParent !== null);
  const i = rows.indexOf(tr);
  if (i === -1) return;
  const next = key === "ArrowDown" ? rows[i + 1]
    : key === "ArrowUp" ? rows[i - 1]
    : key === "Home" ? rows[0] : rows[rows.length - 1];
  if (next) next.focus();
}

function itemMounted(found) {
  if (found.part) return found.part.state === "mounted";
  return found.vol.state === "mounted";
}

/* ============================================================ 渲染入口 */

export function render() {
  const body = $("#storage-body");
  const plan = buildPlan();
  if (!plan.length) {
    body.innerHTML = "";
    rowRefs.clear();
    rowSigs.clear();
    layoutSig = null;
    $("#storage-empty").hidden = false;
    $("#kpi-strip").hidden = true;
    return;
  }
  $("#storage-empty").hidden = true;
  $("#kpi-strip").hidden = false;

  const sig = plan.map((b) => b.id).join("|");
  if (sig !== layoutSig) {
    layoutSig = sig;
    for (const key of openPanels) captureDraft(key);
    rowRefs.clear();
    rowSigs.clear();
    body.innerHTML = `<div class="storage-wrap"><table class="table storage">${HEAD}<tbody>${
      plan.map(cellHtml).join("")}</tbody></table></div>`;
    for (const el of body.querySelectorAll("tr[data-key]")) rowRefs.set(el.dataset.key, { main: el });
    for (const el of body.querySelectorAll("tr.sub-panel[data-panel]")) {
      rowRefs.get(el.dataset.panel).panel = el;
    }
    for (const el of body.querySelectorAll("tr.error-row[data-error]")) {
      rowRefs.get(el.dataset.error).err = el;
    }
    for (const b of plan) if (b.kind === "row") rowSigs.set(b.key, sigOf(b));
    applyDrafts();
    paintSmart();
    patchLive();
    renderKpis();
    return;
  }

  // 布局未变：仅重绘签名变化的行，其余保持焦点与输入
  for (const b of plan) {
    if (b.kind !== "row") continue;
    const refs = rowRefs.get(b.key);
    if (!refs || rowSigs.get(b.key) === sigOf(b)) continue;
    rowSigs.set(b.key, sigOf(b));
    captureDraft(b.key);
    // 附属行是主行的兄弟节点，重建前必须先摘掉，否则会留下孤儿面板 / 错误行
    if (refs.panel) refs.panel.remove();
    if (refs.err) refs.err.remove();
    refs.main.outerHTML = cellHtml(b);
    refs.main = $(`#storage-body tr[data-key="${cssq(b.key)}"]`);
    refs.panel = $(`#storage-body tr.sub-panel[data-panel="${cssq(b.key)}"]`);
    refs.err = $(`#storage-body tr.error-row[data-error="${cssq(b.key)}"]`);
  }
  applyDrafts();
  paintSmart();
  patchLive();
  renderKpis();
}

function cssq(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&");
}

const HEAD = `
  <colgroup>
    <col class="c-drive"><col class="c-name"><col class="c-fs"><col class="c-cap">
    <col class="c-state"><col class="c-speed"><col class="c-act">
  </colgroup>
  <thead><tr>
    <th scope="col" class="c-drive">盘符</th>
    <th scope="col" class="c-name">卷</th>
    <th scope="col" class="c-fs">文件系统</th>
    <th scope="col" class="c-cap">容量</th>
    <th scope="col" class="c-state">状态</th>
    <th scope="col" class="c-speed">实时速率</th>
    <th scope="col" class="c-act">操作</th>
  </tr></thead>`;

/* ============================================================ 计划 */

function buildPlan() {
  const out = [];
  if (state.local.length) {
    out.push({
      kind: "group", id: "g:local", icon: "ic-local", title: "本地存储",
      count: `${state.local.length} 个位置 · 始终可读写`,
    });
    for (const v of state.local) out.push({ kind: "row", id: `r:${v.key}`, key: v.key, type: "local", item: v });
  }
  if (state.disks.length) {
    const parts = state.disks.reduce((n, d) => n + (d.partitions || []).length, 0);
    out.push({
      kind: "group", id: "g:external", icon: "ic-external", title: "外接存储",
      count: `${state.disks.length} 台设备 / ${parts} 分区`,
    });
    for (const d of state.disks) {
      out.push({ kind: "sub", id: `d:${d.id}`, disk: d });
      if (!(d.partitions || []).length) {
        out.push({ kind: "note", id: `n:${d.id}`, text: "该磁盘没有可识别的分区" });
      }
      for (const p of d.partitions) out.push({ kind: "row", id: `r:${p.key}`, key: p.key, type: "external", item: p, disk: d });
    }
  }
  if (state.remote.length) {
    out.push({
      kind: "group", id: "g:remote", icon: "ic-remote", title: "远程存储",
      count: `${state.remote.length} 个挂载`,
    });
    for (const v of state.remote) out.push({ kind: "row", id: `r:${v.key}`, key: v.key, type: "remote", item: v });
  }
  return out;
}

function cellHtml(b) {
  if (b.kind === "group") {
    return `<tr class="group-row kind-${b.id.slice(2)}"><th colspan="${COLS}" scope="colgroup">
      <div class="group-inner"><i class="ic ${b.icon}"></i>${esc(b.title)}
      <span class="count">${esc(b.count)}</span><span class="spacer"></span></div></th></tr>`;
  }
  if (b.kind === "sub") return diskHeadHtml(b.disk);
  if (b.kind === "note") {
    return `<tr class="placeholder"><td colspan="${COLS}">${esc(b.text)}</td></tr>`;
  }
  return rowHtml(b) + panelHtml(b) + errorHtml(b);
}

/* ============================================================ 磁盘行 */

function diskHeadHtml(disk) {
  const mounted = (disk.partitions || []).filter((p) => p.state === "mounted").length;
  const meta = [
    disk.serial ? `SN ${disk.serial}` : "",
    disk.tran ? disk.tran.toUpperCase() : "",
    humanSize(disk.size),
    disk.name,
  ].filter(Boolean).join(" · ");
  return `<tr class="sub-row" data-disk="${esc(disk.id)}"><th colspan="${COLS}">
    <div class="subhead">
      <span class="subhead-title">${esc(diskTitle(disk))}</span>
      <span class="subhead-meta">${esc(meta)}</span>
      <span class="smart" data-smart="${esc(disk.id)}"></span>
      <span class="spacer"></span>
      <span class="subhead-actions">
        ${mounted ? `<button class="btn s danger" data-action="eject-disk" data-id="${esc(disk.id)}">
          <i class="ic ic-eject"></i>弹出整块硬盘</button>` : ""}
      </span>
    </div></th></tr>`;
}

// 已挂载分区标题用「盘符:卷标」，无卷标回退厂商型号 / 内核设备名
function diskTitle(disk) {
  const labels = [...new Set((disk.partitions || []).map((p) => {
    const label = (p.label || "").trim();
    if (!label) return "";
    return p.state === "mounted" && p.drive ? `${p.drive}:${label}` : label;
  }).filter(Boolean))];
  return labels.length ? labels.join(" / ") : (disk.displayName || disk.name);
}

function smartHtml(id) {
  const info = state.smart[id];
  if (info === undefined) return "";
  if (info === null) {
    return `<span class="smart-loading"><i class="ic ic-loader ic-spin"></i>SMART…</span>`;
  }
  if (info.status === "unavailable") return "";
  const parts = [];
  if (info.healthy !== null && info.healthy !== undefined) {
    parts.push(`<span class="stat" data-tone="${info.healthy ? "ok" : "bad"}">
      <i class="ic ${info.healthy ? "ic-check" : "ic-alert"}"></i>${info.healthy ? "健康" : "异常"}</span>`);
  }
  if (info.tempC !== null && info.tempC !== undefined) {
    parts.push(`<span class="stat" ${info.tempC >= 55 ? 'data-tone="bad"' : ""}>
      <i class="ic ic-thermometer"></i>${info.tempC}°C</span>`);
  }
  if (info.powerOnHours !== null && info.powerOnHours !== undefined) {
    parts.push(`<span class="stat"><i class="ic ic-clock"></i>${info.powerOnHours.toLocaleString()} h</span>`);
  }
  parts.push(`<button class="btn ghost s" data-action="smart-refresh" data-id="${esc(id)}" title="重新查询 SMART">
    <i class="ic ic-refresh"></i></button>`);
  return parts.join("");
}

function paintSmart() {
  for (const el of document.querySelectorAll("#storage-body .smart[data-smart]")) {
    const host = el.closest("tr[data-disk]");
    const id = el.dataset.smart;
    if (!(id in state.smart) && host) loadSmart(id);
    el.innerHTML = smartHtml(id);
  }
}

/* ============================================================ 卷行 */

function sigOf(b) {
  const v = b.item;
  return JSON.stringify([
    v.state, v.mode, v.drive, v.error, v.remembered, v.supported, v.label, v.size, v.fstype,
    b.disk ? b.disk.displayName : null, openPanels.has(b.key), state.rememberEnabled,
  ]);
}

function rowHtml(b) {
  const { type, key, item } = b;
  const cls = ["row", `state-${item.state}`];
  if (openPanels.has(key) && panelHtml(b)) cls.push("is-open");
  return `<tr class="${cls.join(" ")}" data-key="${esc(key)}" data-type="${type}" tabindex="0">
    ${driveCell(b)}${nameCell(b)}${fsCell(b)}${capCell(b)}${stateCell(b)}${speedCell(b)}${actCell(b)}
  </tr>`;
}

function driveCell(b) {
  const v = b.item;
  if (v.state === "mounted" && v.drive) {
    return `<td class="c-drive" data-label="盘符"><button class="drive" data-action="copy-path"
      data-key="${esc(b.key)}" title="复制该卷的 SFTP 路径">${esc(v.drive)}:</button></td>`;
  }
  return `<td class="c-drive" data-label="盘符"><span class="drive none">—</span></td>`;
}

function nameCell(b) {
  const v = b.item;
  if (b.type === "local") {
    return `<td class="c-name" data-label="卷"><div class="cell-name"><i class="ic ic-local"></i>
      <span class="txt">${esc(v.name)}</span></div></td>`;
  }
  if (b.type === "remote") {
    const shown = `${v.host}:${v.remotePath}`;
    return `<td class="c-name" data-label="卷"><div class="cell-name"><i class="ic ic-remote"></i>
      <span class="txt">${esc(v.name)}</span></div>
      <span class="cell-sub">${esc(shown)}</span></td>`;
  }
  const label = v.label || `分区 ${v.number}`;
  return `<td class="c-name" data-label="卷"><div class="cell-name">
      ${v.bitlocker ? '<i class="ic ic-lock" title="BitLocker 加密卷"></i>' : ""}
      <span class="txt">${esc(label)}</span></div>
    <span class="cell-sub">${esc(v.path)} · ${humanSize(v.size)}</span></td>`;
}

function fsCell(b) {
  const v = b.item;
  if (b.type === "local") return `<td class="c-fs" data-label="文件系统"><span class="fs-text">bind</span></td>`;
  if (b.type === "remote") return `<td class="c-fs" data-label="文件系统"><span class="fs-text">SFTP</span></td>`;
  const fs = (v.fstype || "").toUpperCase();
  if (v.bitlocker) return `<td class="c-fs" data-label="文件系统"><span class="fs-text">BitLocker<span class="enc">${esc(fs)}</span></span></td>`;
  return `<td class="c-fs" data-label="文件系统"><span class="fs-text">${esc(fs || "—")}</span></td>`;
}

function capCell(b) {
  const v = b.item;
  if (v.state !== "mounted") {
    return `<td class="c-cap" data-label="容量"><span class="f-meta">${b.type === "external" ? humanSize(v.size) : "—"}</span></td>`;
  }
  return `<td class="c-cap" data-label="容量"><span class="meter" data-usage-key="${esc(b.key)}"
    title="卷容量与剩余空间">
    <span class="meter-track"><span class="meter-fill"></span></span>
    <span class="meter-label"><span class="meter-pct">—</span><span class="meter-sub">—</span></span>
  </span></td>`;
}

function stateCell(b) {
  return `<td class="c-state" data-label="状态">${chipFor(b)}</td>`;
}

function chipFor(b) {
  const v = b.item;
  const t = b.type;
  if (t === "local") return chip("accent", "读写", "ic-check");
  if (t === "remote") {
    if (v.state === "mounted") return chip("accent", "读写", "ic-check");
    if (v.state === "mounting") return chip("accent", "挂载中", "ic-loader ic-spin");
    return chip("danger", "连接失败", "ic-alert");
  }
  switch (v.state) {
    case "mounted":
      return v.mode === "rw" ? chip("accent", "读写", "ic-check") : chip("info", "只读", "ic-lock");
    case "unlocking": return chip("accent", "解锁中", "ic-loader ic-spin");
    case "mounting": return chip("accent", "挂载中", "ic-loader ic-spin");
    case "unmounting": return chip("info", "弹出中", "ic-loader ic-spin");
    case "error": return chip("danger", "失败", "ic-alert");
    default:
      if (!v.supported) return chip("off", v.fstype ? `不支持 ${v.fstype}` : "不支持");
      if (v.bitlocker) return chip("warning", v.remembered ? "待解锁" : "需凭据", "ic-lock");
      return chip("off", "未挂载");
  }
}

function chip(tone, text, icon) {
  return `<span class="chip" data-tone="${tone}" aria-label="状态：${esc(text)}">${
    icon ? `<i class="ic ${icon}"></i>` : ""}${esc(text)}</span>`;
}

function speedCell(b) {
  if (b.item.state !== "mounted") return `<td class="c-speed"></td>`;
  return `<td class="c-speed" data-label="速率"><span class="metrics idle" data-speed-key="${esc(b.key)}"
    title="底层硬盘实时吞吐：↓ 读取 / ↑ 写入">
    <span class="metric rx"><i class="ic ic-arrow-down"></i><b data-metric="rx">0/s</b></span>
    <span class="metric tx"><i class="ic ic-arrow-up"></i><b data-metric="tx">0/s</b></span>
  </span></td>`;
}

function actCell(b) {
  return `<td class="c-act" data-label="操作"><div class="row-actions">${actionsFor(b)}${menuFor(b)}</div></td>`;
}

function actionsFor(b) {
  const v = b.item;
  if (b.type === "remote" && v.state === "mounting") return "";
  if (b.type === "external" && TRANSIT.includes(v.state)) return "";

  if (v.state === "mounted") {
    const eject = b.type === "external"
      ? `<button class="btn s danger" data-action="eject" data-key="${esc(b.key)}"
           title="卸载该分区"><i class="ic ic-eject"></i>弹出</button>` : "";
    return `<button class="btn s primary" data-action="browse" data-key="${esc(b.key)}">
      <i class="ic ic-folder-open"></i>浏览</button>${eject}`;
  }
  if (b.type === "remote") {
    return v.state === "error"
      ? `<button class="btn s primary" data-action="reconnect" data-key="${esc(b.key)}">
         <i class="ic ic-refresh"></i>重新连接</button>` : "";
  }
  if (!v.supported) return "";
  const label = v.bitlocker ? "解锁并挂载" : "挂载";
  return `<button class="btn s primary" data-action="panel" data-key="${esc(b.key)}"
    aria-expanded="${openPanels.has(b.key)}"><i class="ic ${v.bitlocker ? "ic-unlock" : "ic-disk"}"></i>${label}</button>`;
}

function menuFor(b) {
  const v = b.item;
  const items = [];
  if (v.state === "mounted") {
    items.push(`<button type="button" data-action="copy-path" data-key="${esc(b.key)}">
      <i class="ic ic-copy"></i>复制 SFTP 路径</button>`);
    if (v.remembered) {
      items.push(`<button type="button" data-action="forget" data-key="${esc(b.key)}">
        <i class="ic ic-edit"></i>忘记凭据</button>`);
    }
  }
  if (b.type === "remote") {
    items.push(`<hr><button type="button" class="danger" data-action="delete-remote" data-key="${esc(b.key)}">
      <i class="ic ic-trash"></i>删除挂载配置</button>`);
  }
  if (!items.length) return "";
  return `<span class="menu-host">
    <button class="btn icon s" type="button" data-action="menu" aria-haspopup="true" aria-expanded="false"
      title="更多操作"><i class="ic ic-more"></i></button>
    <span class="menu" role="group" hidden>${items.join("")}</span></span>`;
}

/* ============================================================ 行内面板 */

/* ---- 面板重绘前捕获表单：中途刷新不能清掉输入到一半的密码 ---- */

function captureDraft(key) {
  const p = panelOf(key);
  if (!p) return;
  const input = p.querySelector(".cred-input");
  draftPanels.set(key, {
    kind: p.querySelector(".segmented button.active")?.dataset.kind || "password",
    value: input ? input.value : "",
    caret: typeof input?.selectionStart === "number" ? input.selectionStart : -1,
    rw: !!p.querySelector(".rw-check")?.checked,
    remember: !!p.querySelector(".remember-check")?.checked,
    focus: focusSel(p, document.activeElement),
  });
}

function focusSel(p, el) {
  if (!el || !p.contains(el)) return "";
  if (el.classList.contains("cred-input")) return ".cred-input";
  if (el.classList.contains("rw-check")) return ".rw-check";
  if (el.classList.contains("remember-check")) return ".remember-check";
  return el.dataset.kind ? `button[data-kind="${cssq(el.dataset.kind)}"]` : "";
}

function restoreDraft(key, d) {
  const p = panelOf(key);
  if (!p) return false;
  setKind(p, d.kind);
  const input = p.querySelector(".cred-input");
  if (input && d.value) {
    input.value = d.value;
    if (d.caret >= 0) input.setSelectionRange(d.caret, d.caret);
  }
  const rw = p.querySelector(".rw-check");
  if (rw) rw.checked = d.rw;
  const rem = p.querySelector(".remember-check");
  if (rem) rem.checked = d.remember;
  const target = d.focus ? p.querySelector(d.focus) : null;
  if (target) target.focus();
  return true;
}

function applyDrafts() {
  for (const [key, d] of draftPanels) {
    if (!restoreDraft(key, d)) draftPanels.delete(key);   // 面板已收起：丢弃草稿
  }
  draftPanels.clear();
}

function panelOf(key) {
  const p = $(`#storage-body tr.sub-panel[data-panel="${cssq(key)}"]`);
  return p && !p.hidden ? p : null;
}

function setKind(p, kind) {
  const group = p.querySelector(".segmented");
  if (!group) return;
  for (const b of group.querySelectorAll("button")) b.classList.toggle("active", b.dataset.kind === kind);
  const input = p.querySelector(".cred-input");
  if (input) input.placeholder = kind === "recovery" ? "输入 48 位 BitLocker 恢复密钥" : "输入 BitLocker 密码";
}

function panelHtml(b) {
  const v = b.item;
  if (b.type !== "external") return "";
  if (v.state === "mounted" || TRANSIT.includes(v.state) || !v.supported) return "";
  const open = openPanels.has(b.key);
  return `<tr class="sub-panel" data-panel="${esc(b.key)}"${open ? "" : " hidden"}><td colspan="${COLS}">
    <div class="panel">${panelInner(b)}</div></td></tr>`;
}

function panelInner(b) {
  const v = b.item;
  const key = esc(b.key);
  if (!v.bitlocker) {
    return `<div class="panel-form">
      ${rwLine(key, "以读写模式挂载（默认只读）")}
      <div class="panel-actions">
        <button class="btn primary s" data-action="mount" data-key="${key}"><i class="ic ic-disk"></i>挂载分区</button>
        <button class="btn ghost s" data-action="panel-close" data-key="${key}">取消</button>
      </div></div>`;
  }
  if (v.remembered) {
    return `<div class="panel-form">
      ${rwLine(key, "以读写模式解锁（默认只读）")}
      <p class="panel-note">该卷已保存凭据，解锁无需再输密码。</p>
      <div class="panel-actions">
        <button class="btn primary s" data-action="unlock" data-key="${key}"><i class="ic ic-unlock"></i>解锁并挂载</button>
        <button class="btn ghost s" data-action="forget" data-key="${key}"><i class="ic ic-edit"></i>忘记凭据，重新输入</button>
      </div></div>`;
  }
  return `<div class="panel-form">
      <div class="segmented" data-kind-group="${key}" role="group" aria-label="凭据类型">
        <button type="button" class="active" data-action="kind" data-key="${key}" data-kind="password">密码解锁</button>
        <button type="button" data-action="kind" data-key="${key}" data-kind="recovery">恢复密钥</button>
      </div>
      <input type="password" class="cred-input" data-key="${key}" autocomplete="off"
             placeholder="输入 BitLocker 密码">
      ${state.rememberEnabled
        ? `<label class="risk"><i class="ic ic-alert"></i>写入 BitLocker / NTFS 由第三方工具链完成，存在损坏风险</label>` : ""}
    </div>
    <div class="panel-side">
      ${rwLine(key, "以读写模式解锁")}
      <label class="checkline" title="${state.rememberEnabled ? "凭据加密保存，下次解锁免输密码" : "未设置 SECRET_KEY，功能不可用"}">
        <input type="checkbox" class="remember-check" data-key="${key}"${state.rememberEnabled ? "" : " disabled"}>
        <span>记住此卷${state.rememberEnabled ? "" : "（需设置 SECRET_KEY）"}</span>
      </label>
      <div class="panel-actions">
        <button class="btn primary s" data-action="unlock" data-key="${key}"><i class="ic ic-unlock"></i>解锁并挂载</button>
        <button class="btn ghost s" data-action="panel-close" data-key="${key}">取消</button>
      </div>
    </div>`;
}

function rwLine(key, text) {
  return `<label class="checkline"><input type="checkbox" class="rw-check" data-key="${key}">
    <span>${esc(text)}</span></label>`;
}

function errorHtml(b) {
  const v = b.item;
  if (!v.error || v.state !== "error") return "";
  const retry = b.type === "remote"
    ? `<button class="btn s" data-action="reconnect" data-key="${esc(b.key)}"><i class="ic ic-refresh"></i>重试</button>`
    : `<button class="btn s" data-action="panel" data-key="${esc(b.key)}"><i class="ic ic-unlock"></i>重试</button>`;
  return `<tr class="error-row" data-error="${esc(b.key)}"><td colspan="${COLS}">
    <div class="error-line"><i class="ic ic-alert"></i><span>${esc(v.error)}</span>
    <span class="actions">${retry}<button class="btn s ghost" data-action="show-sessions">查看占用</button></span>
    </div></td></tr>`;
}

function togglePanel(key) {
  if (openPanels.has(key)) openPanels.delete(key);
  else openPanels.add(key);
  const refs = rowRefs.get(key);
  const b = planRow(key);
  if (!refs || !b) { render(); return; }
  rowSigs.set(key, sigOf(b));
  if (refs.panel) refs.panel.remove();
  if (refs.err) refs.err.remove();
  refs.main.outerHTML = cellHtml(b);
  refs.main = $(`#storage-body tr[data-key="${cssq(key)}"]`);
  const p = $(`#storage-body tr.sub-panel[data-panel="${cssq(key)}"]`);
  refs.panel = p;
  if (p && !p.hidden) p.querySelector(".cred-input")?.focus();
}

function planRow(key) {
  return buildPlan().find((b) => b.kind === "row" && b.key === key);
}

/* ============================================================ KPI */

function paintLive() {
  renderKpis();
}

export function renderKpis() {
  const rows = allVolumes();
  if (!rows.length) return;
  const strip = $("#kpi-strip");
  if (!strip) return;
  const set = (name, val) => {
    const el = strip.querySelector(`[data-kpi="${name}"]`);
    if (el && el.textContent !== String(val)) el.textContent = val;
  };
  const mounted = rows.filter((r) => r.state === "mounted");
  const count = (kind) => rows.filter((r) => r.kind === kind).length;
  const mountedCount = (kind) => mounted.filter((r) => r.kind === kind).length;

  let rx = 0, tx = 0;
  for (const s of Object.values(state.rates)) { rx += s.rx || 0; tx += s.tx || 0; }

  set("mounted", `${mounted.length} / ${rows.length}`);
  set("mountedSub", `本地 ${count("local")} · 外接 ${mountedCount("external")} · 远程 ${mountedCount("remote")}`);
  set("read", rateText(rx));
  set("write", rateText(tx));

  const s = state.sessions;
  const open = s ? (s.openFiles || []).length : 0;
  set("conns", s ? s.connections : 0);
  set("openFiles", open ? `${open} 个文件打开中` : "无打开文件");
  strip.dataset.tone = rows.some((r) => r.state === "error") ? "danger" : "";
}

function rateText(bps) {
  if (!bps || bps < 1) return "0 B/s";
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let v = bps, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/* ============================================================ 操作 */

function closeMenus(except) {
  for (const m of document.querySelectorAll("#storage-body .menu")) {
    if (m === except) continue;
    m.hidden = true;
    const btn = m.closest(".menu-host")?.querySelector("[data-action=menu]");
    if (btn) btn.setAttribute("aria-expanded", "false");
  }
}

async function onClick(e) {
  const host = e.target.closest("#storage-body");
  const menuBtn = e.target.closest('[data-action="menu"]');
  if (!host) { closeMenus(); return; }

  if (menuBtn) {
    e.preventDefault();
    const menu = menuBtn.nextElementSibling;
    const wasOpen = !menu.hidden;
    closeMenus();
    menu.hidden = wasOpen;
    menuBtn.setAttribute("aria-expanded", String(!wasOpen));
    return;
  }
  const inMenu = e.target.closest(".menu");
  if (!inMenu) {
    if (!e.target.closest(".menu-host")) closeMenus();
  }

  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const { action, key, id } = btn.dataset;

  if (action === "browse") { openBrowser(key); return; }
  if (action === "copy-path") {
    const found = findVolume(key);
    const sub = found?.part?.sftpPath ?? found?.vol?.sftpPath ?? "";
    await copyText(sftpUrlFor(sub), "SFTP 路径");
    return;
  }
  if (action === "panel") { togglePanel(key); return; }
  if (action === "panel-close") { openPanels.delete(key); render(); return; }
  if (action === "kind") {
    const p = btn.closest("tr.sub-panel");
    if (p) {
      setKind(p, btn.dataset.kind);
      p.querySelector(".cred-input")?.focus();
    }
    return;
  }
  if (action === "show-sessions") { location.hash = "#/sessions"; return; }
  if (action === "smart-refresh") {
    btn.setAttribute("aria-busy", "true");
    await loadSmart(id, true);
    btn.removeAttribute("aria-busy");
    return;
  }
  if (action === "unlock") { unlock(key, btn); return; }
  if (action === "mount") { mount(key, btn); return; }
  if (action === "eject") { eject(key, btn); return; }
  if (action === "eject-disk") { ejectDisk(id, btn); return; }
  if (action === "forget") { forget(key); return; }
  if (action === "reconnect") { reconnect(key, btn); return; }
  if (action === "delete-remote") { deleteRemote(key); return; }
}

async function busy(btn, on) {
  if (!btn) return;
  btn.disabled = on;
  if (on) btn.setAttribute("aria-busy", "true");
  else btn.removeAttribute("aria-busy");
}

async function unlock(key, btn) {
  const panel = document.querySelector(`#storage-body tr.sub-panel[data-panel="${cssq(key)}"]`);
  const found = findVolume(key);
  const remembered = !!(found && found.part && found.part.remembered);
  const writable = panel?.querySelector(".rw-check")?.checked || false;
  let kind = "password", secret = "", remember = false;
  if (!remembered && panel) {
    const active = panel.querySelector(".segmented button.active");
    kind = active ? active.dataset.kind : "password";
    secret = (panel.querySelector(".cred-input")?.value || "").trim();
    remember = panel.querySelector(".remember-check")?.checked || false;
    if (!secret) { toast("请先输入密码或恢复密钥", "err"); panel.querySelector(".cred-input")?.focus(); return; }
  }
  if (writable && !window.confirm(
    "读写模式通过第三方工具链（dislocker + NTFS-3G）写入 BitLocker 卷，\n存在数据损坏风险。确认以读写模式解锁吗？"
  )) return;

  busy(btn, true);
  try {
    await api("POST", `/api/volumes/${encodeURIComponent(key)}/unlock`, { kind, secret, writable, remember });
    toast("正在解锁挂载…", "busy");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    busy(btn, false);
  }
  openPanels.delete(key);
  await refreshVolumes();
}

async function mount(key, btn) {
  const panel = document.querySelector(`#storage-body tr.sub-panel[data-panel="${cssq(key)}"]`);
  const writable = panel?.querySelector(".rw-check")?.checked || false;
  if (writable && !window.confirm("确认以读写模式挂载该分区吗？")) return;
  busy(btn, true);
  try {
    await api("POST", `/api/volumes/${encodeURIComponent(key)}/mount`, { writable });
    toast("正在挂载…", "busy");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    busy(btn, false);
  }
  openPanels.delete(key);
  await refreshVolumes();
}

async function eject(key, btn) {
  if (!window.confirm("安全弹出该分区？将先卸载文件系统并断开解密连接。")) return;
  busy(btn, true);
  try {
    const r = await api("POST", `/api/volumes/${encodeURIComponent(key)}/eject`);
    if (r.safeToRemove) toast("已卸载", "ok");
  } catch (err) {
    toast(err.message, "err");
    await refreshVolumes();
  } finally {
    busy(btn, false);
  }
}

async function ejectDisk(id, btn) {
  if (!window.confirm("将卸载该硬盘上所有已挂载的分区，确认安全弹出整块硬盘？")) return;
  busy(btn, true);
  try {
    const r = await api("POST", `/api/disks/${encodeURIComponent(id)}/eject`);
    if (r.safeToRemove) toast("整块硬盘已弹出，可以安全拔除", "ok");
  } catch (err) {
    toast(err.message, "err");
    await refreshVolumes();
  } finally {
    busy(btn, false);
  }
}

async function forget(key) {
  if (!window.confirm("删除已保存的解锁凭据？之后解锁需重新输入密码。")) return;
  try {
    await api("DELETE", `/api/volumes/${encodeURIComponent(key)}/credential`);
    toast("已忘记该卷的凭据");
    await refreshVolumes();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function reconnect(key, btn) {
  busy(btn, true);
  try {
    await api("POST", `/api/remote-stores/${encodeURIComponent(key)}/reconnect`);
    toast("正在重新连接…", "busy");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    busy(btn, false);
  }
  await refreshVolumes();
}

async function deleteRemote(key) {
  if (!window.confirm("确认删除该远程存储？仅删除挂载配置，不会删除远端文件。")) return;
  try {
    await api("DELETE", `/api/remote-stores/${encodeURIComponent(key)}`);
    toast("已删除远程存储", "ok");
    await refreshVolumes();
  } catch (err) {
    toast(err.message, "err");
  }
}
