/* 文件浏览抽屉 + 媒体/文本预览 + 上传/删除/重命名 + 移动复制对话框 */
"use strict";

import { $, esc, api, toast, fileKind, humanSize, humanDate, encPath, rawUrl } from "./core.js";
import { state, findVolume, volumeDisplayName, writableTargets } from "./store.js";
import * as overlays from "./overlays.js";
import { reloadJobs } from "./jobs.js";

const fsState = {
  key: null, path: "", writable: false, entries: [], crumbs: [],
  selected: new Set(), lastCheckIndex: null,
};
const moveState = {
  srcKey: null, srcItems: [], destKey: "", destPath: "",
  srcWritable: false, op: "move", entries: [], crumbs: [], busy: false,
};
const renameState = { path: "", name: "" };

export function init() {
  overlays.register("fs-layer", {
    onClose: () => {
      fsState.key = null;
      fsState.entries = [];
      clearSelection();
    },
  });
  overlays.register("player-modal", { onClose: stopMedia });
  overlays.register("text-modal", { onClose: () => { $("#text-body").textContent = ""; } });
  overlays.register("move-modal");
  overlays.register("rename-modal");

  $("#fs-reload").addEventListener("click", () => loadFsDir(fsState.path));
  $("#fs-error-retry").addEventListener("click", () => loadFsDir(fsState.path));
  $("#fs-upload").addEventListener("click", () => $("#fs-file-input").click());
  $("#fs-file-input").addEventListener("change", async function pick() {
    const file = this.files && this.files[0];
    this.value = "";
    if (file) await uploadFile(file, 0);
  });
  $("#fs-crumbs").addEventListener("click", (e) => {
    const btn = e.target.closest(".crumb");
    if (btn && !btn.classList.contains("cur")) loadFsDir(btn.dataset.path || "");
  });
  $("#fs-list").addEventListener("click", (e) => onFsClick(e));
  $("#fs-list").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.target.closest("input.fcheck")) return;
    const row = e.target.closest("tr.frow");
    if (row) activateRow(row);
  });
  $("#fs-select-all").addEventListener("change", (e) => selectAll(e.target.checked));
  $("#fs-select-all-bar").addEventListener("change", (e) => selectAll(e.target.checked));
  $("#fs-copy-sel").addEventListener("click", () => openSelected("copy"));
  $("#fs-move-sel").addEventListener("click", () => openSelected("move"));
  $("#fs-clear-sel").addEventListener("click", clearSelection);
  $("#player-close").addEventListener("click", stopMedia);
  $("#move-keep").addEventListener("change", (e) => {
    moveState.op = e.target.checked ? "copy" : "move";
    paintMoveMode();
  });
  $("#move-disk-select").addEventListener("change", (e) => {
    moveState.destKey = e.target.value;
    loadMoveDir("");
  });
  $("#move-crumbs").addEventListener("click", (e) => {
    const btn = e.target.closest(".crumb");
    if (btn && !btn.classList.contains("cur")) loadMoveDir(btn.dataset.path || "");
  });
  $("#move-dir-list").addEventListener("click", (e) => {
    const row = e.target.closest(".drow");
    if (row && !row.disabled) loadMoveDir(row.dataset.path || "");
  });
  $("#move-confirm").addEventListener("click", submitMove);
  $("#rename-ok").addEventListener("click", submitRename);
  $("#rename-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitRename();
  });
}

/* ============================================================ 打开卷 */

export async function openBrowser(key) {
  const found = findVolume(key);
  if (!found) { toast("卷不可用", "err"); return; }
  fsState.key = key;
  $("#fs-title").textContent = volumeDisplayName(key);
  overlays.open("fs-layer", "#fs-close");
  await loadFsDir("");
}

function closeBrowser() { overlays.close("fs-layer"); }

async function loadFsDir(path) {
  if (!fsState.key) return;
  clearSelection();
  const list = $("#fs-list");
  // 失败时保留上一份可用列表：正在卸载导致的读取失败不该把用户踢出抽屉
  const prev = { path: fsState.path, writable: fsState.writable, entries: fsState.entries, crumbs: fsState.crumbs };
  $("#fs-error").hidden = true;
  list.innerHTML = `<tr class="placeholder loading"><td colspan="4">
    <i class="ic ic-loader ic-spin"></i>读取中…</td></tr>`;
  try {
    const data = await api("GET",
      `/api/volumes/${encodeURIComponent(fsState.key)}/browse?path=${encPath(path)}`);
    fsState.path = data.path || "";
    fsState.writable = !!data.writable;
    fsState.entries = data.entries || [];
    fsState.crumbs = data.crumbs || [];
    renderFs();
  } catch (err) {
    Object.assign(fsState, prev);
    if (fsState.entries.length) renderFs();
    else list.innerHTML = `<tr class="placeholder"><td colspan="4">${esc(err.message)}</td></tr>`;
    showFsError(err.message);
  }
}

function showFsError(msg) {
  $("#fs-error-msg").textContent = msg;
  $("#fs-error").hidden = false;
}

function renderFs() {
  $("#fs-upload").hidden = !fsState.writable;
  paintCrumbs("#fs-crumbs", fsState.crumbs);

  const list = $("#fs-list");
  const bar = $("#fs-select-bar");
  if (!fsState.entries.length) {
    list.innerHTML = `<tr class="placeholder"><td colspan="4">空文件夹</td></tr>`;
    bar.hidden = true;
    paintSelection();
    return;
  }
  bar.hidden = false;
  const targets = writableTargets().length;
  list.innerHTML = fsState.entries.map((e) => {
    const full = entryPath(e);
    const checked = fsState.selected.has(full);
    const icon = e.isDir ? "folder" : ({ video: "film", audio: "music", image: "image" }[fileKind(e.name)] || "file");
    const meta = e.isDir ? "文件夹" : `${humanSize(e.size)} · ${humanDate(e.mtime)}`;
    let actions = "";
    if (!e.isDir) {
      actions += `<button class="btn icon s" type="button" title="下载" data-act="download"
        data-path="${esc(full)}"><i class="ic ic-download"></i></button>`;
    }
    if (targets) actions += transferBtn(full, e.name);
    if (fsState.writable) {
      actions += `<button class="btn icon s" type="button" title="重命名" data-act="rename"
        data-path="${esc(full)}" data-name="${esc(e.name)}"><i class="ic ic-edit"></i></button>
        <button class="btn icon s danger" type="button" title="删除" data-act="delete"
        data-path="${esc(full)}" data-dir="${e.isDir ? 1 : 0}"><i class="ic ic-trash"></i></button>`;
    }
    return `<tr class="frow${e.isDir ? " dir" : ""}${checked ? " selected" : ""}"
        data-path="${esc(full)}" data-dir="${e.isDir ? 1 : 0}"
        tabindex="0" title="${esc(e.name)}">
      <td class="c-check"><input type="checkbox" class="fs-check fcheck"
        data-path="${esc(full)}" aria-label="选择 ${esc(e.name)}"${checked ? " checked" : ""}></td>
      <td class="c-name"><div class="cell-name">
        <i class="ic ic-${icon} fic fic-${icon}"></i><span class="txt">${esc(e.name)}</span></div></td>
      <td class="c-meta"><span class="f-meta">${esc(meta)}</span></td>
      <td class="c-act"><div class="row-actions">${actions}</div></td>
    </tr>`;
  }).join("");
  paintSelection();
}

/* ============================================================ 多选 */

function entryPath(entry) {
  return fsState.path ? `${fsState.path}/${entry.name}` : entry.name;
}

function selectedItems() {
  return fsState.entries
    .filter((e) => fsState.selected.has(entryPath(e)))
    .map((e) => ({ path: entryPath(e), name: e.name }));
}

function paintSelection() {
  const total = fsState.entries.length;
  let count = 0;
  for (const cb of $("#fs-list").querySelectorAll("input.fcheck")) {
    const on = fsState.selected.has(cb.dataset.path);
    cb.checked = on;
    if (on) count += 1;
    const row = cb.closest("tr.frow");
    if (row) row.classList.toggle("selected", on);
  }

  const master = $("#fs-select-all");
  const barMaster = $("#fs-select-all-bar");
  const all = total > 0 && count === total;
  const partial = count > 0 && count < total;
  for (const box of [master, barMaster]) {
    box.checked = all;
    box.indeterminate = partial;
    box.disabled = total === 0;
  }

  $("#fs-select-count").textContent = count;
  const hasTargets = writableTargets().length > 0;
  $("#fs-copy-sel").disabled = count === 0 || !hasTargets;
  const moveBtn = $("#fs-move-sel");
  moveBtn.hidden = !fsState.writable;
  moveBtn.disabled = count === 0 || !hasTargets;
  $("#fs-clear-sel").disabled = count === 0;
}

function selectAll(on) {
  for (const entry of fsState.entries) {
    const path = entryPath(entry);
    if (on) fsState.selected.add(path);
    else fsState.selected.delete(path);
  }
  fsState.lastCheckIndex = null;
  paintSelection();
}

function clearSelection() {
  fsState.selected.clear();
  fsState.lastCheckIndex = null;
  paintSelection();
}

function toggleCheck(cb, event) {
  const idx = fsState.entries.findIndex((e) => entryPath(e) === cb.dataset.path);
  if (idx < 0) return;
  const on = cb.checked;
  const last = fsState.lastCheckIndex;
  if (event.shiftKey && last !== null && last !== idx) {
    const [from, to] = last < idx ? [last, idx] : [idx, last];
    for (let i = from; i <= to; i += 1) {
      const path = entryPath(fsState.entries[i]);
      if (on) fsState.selected.add(path);
      else fsState.selected.delete(path);
    }
  } else if (on) {
    fsState.selected.add(cb.dataset.path);
  } else {
    fsState.selected.delete(cb.dataset.path);
  }
  fsState.lastCheckIndex = idx;
  paintSelection();
}

function openSelected(wantOp) {
  const items = selectedItems();
  if (!items.length) return;
  openMove(items, wantOp);
}

// 只读源只能复制出去；读写源默认移动（对话框内可勾选改为复制）
function transferBtn(full, name) {
  const op = fsState.writable ? "move" : "copy";
  return `<button class="btn icon s" type="button"
    title="${op === "move" ? "移动到其他位置（可在对话框内改为复制）" : "复制到其他硬盘（只读盘不能移动）"}"
    data-act="move" data-op="${op}" data-path="${esc(full)}" data-name="${esc(name)}">
    <i class="ic ic-${op === "move" ? "move" : "copy"}"></i></button>`;
}

function paintCrumbs(sel, crumbs) {
  const host = $(sel);
  if (!crumbs.length) {
    host.innerHTML = `<span class="crumb cur">根目录</span>`;
    return;
  }
  host.innerHTML = crumbs.map((c, i) => {
    const last = i === crumbs.length - 1;
    return last
      ? `<span class="crumb cur">${esc(c.name)}</span>`
      : `<button class="crumb" type="button" data-path="${esc(c.path)}">${esc(c.name)}</button><span class="crumb-sep">/</span>`;
  }).join("");
}

function onFsClick(e) {
  const check = e.target.closest("input.fcheck");
  const btn = e.target.closest("[data-act]");
  const row = e.target.closest("tr.frow");
  if (check && row) {
    e.stopPropagation();
    toggleCheck(check, e);
    return;
  }
  if (btn && row) {
    e.stopPropagation();
    const { act, path, name, op } = btn.dataset;
    if (act === "download") downloadFile(path);
    else if (act === "move") openMove([{ path, name }], op || "move");
    else if (act === "rename") openRename(path, name);
    else if (act === "delete") deleteEntry(path, btn.dataset.dir === "1");
    return;
  }
  if (row) activateRow(row);
}

function activateRow(row) {
  if (row.dataset.dir === "1") loadFsDir(row.dataset.path);
  else openEntry(row.dataset.path);
}

/* ============================================================ 预览 */

function openEntry(path) {
  const name = path.split("/").pop() || path;
  const kind = fileKind(name);
  if (kind === "other") { downloadFile(path); return; }
  if (kind === "text") { openTextPreview(path, name); return; }
  const url = rawUrl(fsState.key, path, false);
  $("#player-title").textContent = name;
  const body = $("#player-body");
  body.innerHTML = "";
  if (kind === "video") {
    const el = document.createElement("video");
    el.src = url;
    ["controls", "autoplay", "playsinline"].forEach((a) => el.setAttribute(a, ""));
    el.preload = "metadata";
    body.appendChild(el);
  } else if (kind === "audio") {
    const wrap = document.createElement("div");
    wrap.className = "audio-wrap";
    const art = document.createElement("i");
    art.className = "ic ic-music audio-art";
    const el = document.createElement("audio");
    el.src = url;
    ["controls", "autoplay"].forEach((a) => el.setAttribute(a, ""));
    el.preload = "metadata";
    wrap.append(art, el);
    body.appendChild(wrap);
  } else {
    const el = document.createElement("img");
    el.src = url;
    el.alt = name;
    body.appendChild(el);
  }
  overlays.open("player-modal", "#player-close");
}

function stopMedia() {
  const body = $("#player-body");
  for (const m of body.querySelectorAll("video, audio")) {
    m.pause();
    m.removeAttribute("src");
    m.load?.();
  }
  body.innerHTML = "";
}

async function openTextPreview(path, name) {
  const pre = $("#text-body");
  $("#text-title").textContent = name;
  $("#text-meta").textContent = "读取中…";
  // textContent 赋值：盘内 HTML / 脚本只作纯文本显示，不会执行
  pre.textContent = "";
  pre.classList.add("is-loading");
  $("#text-download").onclick = () => downloadFile(path);
  overlays.open("text-modal", "#text-close");
  try {
    const data = await api("GET",
      `/api/volumes/${encodeURIComponent(fsState.key)}/preview?path=${encPath(path)}`);
    $("#text-meta").textContent = `${humanSize(data.size)} · ${data.encoding || "文本"}`;
    pre.textContent = data.content || "（空文件）";
  } catch (err) {
    overlays.close("text-modal");
    toast(err.message, "err");
  } finally {
    pre.classList.remove("is-loading");
  }
}

function downloadFile(path) {
  const a = document.createElement("a");
  a.href = rawUrl(fsState.key, path, true);
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/* ============================================================ 上传 */

async function uploadFile(file, overwrite) {
  const bar = $("#fs-upload-progress");
  const fill = bar.querySelector(".progress-fill");
  const label = bar.querySelector(".progress-label");
  bar.hidden = false;
  fill.style.width = "0%";
  label.textContent = `上传 ${file.name}：0%`;

  const form = new FormData();
  form.append("path", fsState.path);
  form.append("overwrite", String(overwrite));
  form.append("file", file);

  await new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/volumes/${encodeURIComponent(fsState.key)}/upload`);
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) return;
      const pct = Math.round((ev.loaded / ev.total) * 100);
      fill.style.width = `${pct}%`;
      label.textContent = `上传 ${file.name}：${pct}%`;
    };
    xhr.onload = () => {
      bar.hidden = true;
      let detail = "";
      try { detail = JSON.parse(xhr.responseText).detail || ""; } catch { /* 非 JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        toast(`已上传 ${file.name}`, "ok");
        loadFsDir(fsState.path);
      } else if (xhr.status === 409 && detail.includes("同名")
        && window.confirm(`「${file.name}」已存在，覆盖它？`)) {
        uploadFile(file, 1).then(resolve);
        return;
      } else {
        toast(detail || `上传失败（HTTP ${xhr.status}）`, "err");
      }
      resolve();
    };
    xhr.onerror = () => {
      bar.hidden = true;
      toast("上传失败：网络错误", "err");
      resolve();
    };
    xhr.send(form);
  });
}

/* ============================================================ 删除 / 重命名 */

async function deleteEntry(path, isDir) {
  const name = path.split("/").pop() || path;
  const tip = isDir
    ? `确认删除文件夹「${name}」及其全部内容？此操作不可恢复。`
    : `确认删除文件「${name}」？此操作不可恢复。`;
  if (!window.confirm(tip)) return;
  try {
    await api("DELETE", `/api/volumes/${encodeURIComponent(fsState.key)}/entry`
      + `?path=${encPath(path)}${isDir ? "&recursive=1" : ""}`);
    toast("已删除", "ok");
    loadFsDir(fsState.path);
  } catch (err) {
    toast(err.message, "err");
  }
}

function openRename(path, name) {
  renameState.path = path;
  renameState.name = name;
  $("#rename-error").hidden = true;
  $("#rename-input").value = name;
  overlays.open("rename-modal");
  const input = $("#rename-input");
  input.focus();
  input.select();
}

async function submitRename() {
  const newName = $("#rename-input").value.trim();
  const errBox = $("#rename-error");
  errBox.hidden = true;
  if (!newName) { errBox.textContent = "名称不能为空"; errBox.hidden = false; return; }
  if (newName === renameState.name) { overlays.close("rename-modal"); return; }
  try {
    await api("POST", `/api/volumes/${encodeURIComponent(fsState.key)}/rename`, {
      path: renameState.path, newName,
    });
    toast("已重命名", "ok");
    overlays.close("rename-modal");
    loadFsDir(fsState.path);
  } catch (err) {
    errBox.textContent = err.message;
    errBox.hidden = false;
    $("#rename-input").focus();
  }
}

/* ============================================================ 移动 / 复制 */

async function openMove(items, wantOp) {
  const targets = writableTargets();
  if (!targets.length) {
    toast("没有可写的存储作为目标（本地 / 远程恒可写，外接需以读写模式挂载）", "err");
    return;
  }
  const src = findVolume(fsState.key);
  moveState.srcKey = fsState.key;
  moveState.srcItems = items;
  moveState.srcWritable = src ? (!src.part || src.part.mode === "rw") : false;
  moveState.op = moveState.srcWritable ? wantOp : "copy";
  moveState.busy = false;

  const label = items.length === 1 ? items[0].name : `${items.length} 个项目`;
  $("#move-src-name").textContent = label;
  $("#move-src-name").title = items.map((it) => it.name).join("\n");
  $("#move-error").hidden = true;
  const hint = $("#move-hint");
  hint.hidden = moveState.srcWritable;
  if (!moveState.srcWritable) hint.textContent = "源硬盘为只读挂载，只能复制（源文件会保留）。";
  $("#move-keep-row").hidden = !moveState.srcWritable;
  $("#move-keep").checked = moveState.op === "copy";

  const sel = $("#move-disk-select");
  const groups = new Map();
  for (const t of targets) {
    if (!groups.has(t.group)) groups.set(t.group, []);
    groups.get(t.group).push(t);
  }
  sel.innerHTML = [...groups].map(([title, items]) =>
    `<optgroup label="${esc(title)}">${items.map((t) =>
      `<option value="${esc(t.key)}">${esc(t.label)}</option>`).join("")}</optgroup>`).join("");
  const other = targets.find((t) => t.key !== fsState.key) || targets[0];
  moveState.destKey = other.key;
  sel.value = other.key;

  $("#move-confirm").disabled = false;
  paintMoveMode();
  overlays.open("move-modal", "#move-disk-select");
  await loadMoveDir("");
}

function paintMoveMode() {
  const isCopy = moveState.op === "copy";
  $("#move-src-kicker").textContent = isCopy ? "复制项目" : "移动项目";
  $("#move-heading").textContent = isCopy ? "复制到…" : "移动到…";
  $("#move-confirm-label").textContent = isCopy ? "开始复制" : "开始移动";
  const ic = $("#move-confirm").querySelector(".ic");
  if (ic) ic.className = `ic ${isCopy ? "ic-copy" : "ic-move"}`;
}

async function loadMoveDir(path) {
  const list = $("#move-dir-list");
  list.innerHTML = `<div class="drow-note"><i class="ic ic-loader ic-spin"></i> 读取中…</div>`;
  try {
    const data = await api("GET",
      `/api/volumes/${encodeURIComponent(moveState.destKey)}/browse?path=${encPath(path)}`);
    moveState.destPath = data.path || "";
    moveState.crumbs = data.crumbs || [];
    moveState.entries = (data.entries || []).filter((e) => e.isDir);
    renderMoveDirs();
  } catch (err) {
    moveState.entries = [];
    moveState.crumbs = [];
    list.innerHTML = `<div class="drow-note">${esc(err.message)}</div>`;
  }
}

function renderMoveDirs() {
  paintCrumbs("#move-crumbs", moveState.crumbs);
  const list = $("#move-dir-list");
  const rows = [];
  if (moveState.destPath) {
    const up = moveState.destPath.split("/").slice(0, -1).join("/");
    rows.push(`<button class="drow" type="button" data-path="${esc(up)}">
      <i class="ic ic-arrow-up up"></i>返回上级</button>`);
  }
  for (const e of moveState.entries) {
    rows.push(`<button class="drow" type="button"
      data-path="${esc(moveState.destPath ? `${moveState.destPath}/${e.name}` : e.name)}">
      <i class="ic ic-folder"></i>${esc(e.name)}</button>`);
  }
  rows.push(`<div class="drow-note">目标位置：${esc(currentTargetLabel())}</div>`);
  list.innerHTML = rows.join("");
}

function currentTargetLabel() {
  const vol = volumeDisplayName(moveState.destKey);
  return moveState.destPath ? `${vol}/${moveState.destPath}` : `${vol}（当前位置）`;
}

async function submitMove() {
  if (moveState.busy) return;
  moveState.busy = true;
  const btn = $("#move-confirm");
  btn.disabled = true;
  const errBox = $("#move-error");
  errBox.hidden = true;
  const items = moveState.srcItems;
  const failures = [];
  try {
    for (const item of items) {
      try {
        await api("POST", `/api/volumes/${encodeURIComponent(moveState.srcKey)}/move`, {
          path: item.path,
          destKey: moveState.destKey,
          destPath: moveState.destPath,
          mode: moveState.op,
        });
      } catch (err) {
        failures.push({ name: item.name, error: err.message });
      }
    }
    if (failures.length === items.length) {
      // 全部失败（空间不足 / 目标与源相同等）：留在对话框内展示原因
      const shown = failures.slice(0, 3)
        .map((f) => `${f.name}：${f.error}`).join("；");
      errBox.textContent = failures.length > 3
        ? `${shown}；等 ${failures.length} 项失败` : shown;
      errBox.hidden = false;
      moveState.busy = false;
      btn.disabled = false;
      return;
    }
    overlays.close("move-modal");
    const verb = moveState.op === "copy" ? "复制" : "移动";
    const done = items.length - failures.length;
    if (failures.length) {
      toast(`已提交 ${done} 个${verb}任务，${failures.length} 项失败：`
        + `${failures[0].name}（${failures[0].error}）`, "err");
    } else {
      toast(done === 1
        ? `已开始后台${verb}「${items[0].name}」`
        : `已开始后台${verb} ${done} 个项目`, "ok");
    }
    loadFsDir(fsState.path);
    await reloadJobs();
  } catch (err) {
    // 空间不足等预检失败在此内联显示，不用 toast 一闪而过
    errBox.textContent = err.message;
    errBox.hidden = false;
    moveState.busy = false;
    btn.disabled = false;
  }
}
