/* 传输任务视图 + 底部常驻进度条 + 导航徽标 */
"use strict";

import { $, esc, api, toast, humanSize, humanRate, fmtEta, humanDate } from "./core.js";
import { state, on, routeLabel, refreshJobs } from "./store.js";
import { setBadge } from "./nav.js";

const STATUS = {
  queued: ["排队中", "off"],
  running: ["传输中", "accent"],
  done: ["已完成", "accent"],
  error: ["失败", "danger"],
  canceled: ["已取消", "off"],
};

let filter = "all";

export function init() {
  on("jobs", render);
  document.addEventListener("click", onClick);
  $("#jobs-filter").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-filter]");
    if (!btn) return;
    filter = btn.dataset.filter;
    for (const b of $("#jobs-filter").querySelectorAll("button")) b.classList.toggle("active", b === btn);
    render();
  });
}

export async function reloadJobs() {
  await refreshJobs();
}

function visibleJobs() {
  const jobs = state.jobs;
  if (filter === "active") return jobs.filter((j) => j.status === "queued" || j.status === "running");
  if (filter === "finished") return jobs.filter((j) => j.status !== "queued" && j.status !== "running");
  return jobs;
}

export function render() {
  renderList();
  renderDock();
  renderBadges();
}

function renderList() {
  const body = $("#jobs-body");
  const jobs = visibleJobs();
  if (!state.jobs.length) {
    body.innerHTML = `<div class="empty"><div class="big"><i class="ic ic-move"></i></div>
      <p>暂无传输任务</p><p class="sub">在文件浏览里选中项目移动到别的硬盘，任务会出现在这里</p></div>`;
    return;
  }
  if (!jobs.length) {
    body.innerHTML = `<div class="empty"><p>当前筛选条件下没有任务</p>
      <p class="sub">共 ${state.jobs.length} 个任务</p></div>`;
    return;
  }
  body.innerHTML = jobs.map(jobCard).join("");
}

function jobCard(j) {
  const pct = j.bytesTotal ? Math.min(100, Math.round((j.bytesDone / j.bytesTotal) * 100)) : 0;
  const [text, tone] = STATUS[j.status] || [j.status, "off"];
  const opLabel = j.op === "copy" ? "复制" : "移动";
  const active = j.status === "queued" || j.status === "running";
  const route = `${routeLabel(j.srcKey, j.srcPath)} → ${routeLabel(j.dstKey, j.dstPath)}`;
  return `<div class="job-card" data-status="${esc(j.status)}">
    <div class="job-head">
      <span class="job-name" title="${esc(j.name)}">
        <i class="ic ${j.op === "copy" ? "ic-copy" : "ic-move"}"></i>${esc(j.name)}</span>
      ${j.status === "running" ? `<span class="job-rate">${esc(humanRate(j.rateBps))}</span>` : ""}
      <span class="chip" data-tone="${tone}">${active ? '<i class="ic ic-loader ic-spin"></i>' : ""}${
        esc(opLabel)} · ${esc(text)}</span>
    </div>
    <div class="job-route" title="${esc(route)}">
      <span class="seg-item">${esc(routeLabel(j.srcKey, j.srcPath))}</span>
      <i class="ic ic-arrow-right"></i>
      <span class="seg-item">${esc(routeLabel(j.dstKey, j.dstPath))}</span>
    </div>
    ${active ? `<div class="progress-track"><span class="progress-fill" style="width:${pct}%"></span></div>
      <div class="job-foot">
        <span class="job-progress">${esc(progressText(j, pct))}</span>
        <button class="btn s danger" type="button" data-action="cancel" data-id="${esc(j.id)}">
          <i class="ic ic-close"></i>取消</button>
      </div>` : ""}
    ${j.error ? `<div class="error-line"><i class="ic ic-alert"></i><span>${esc(j.error)}</span></div>` : ""}
    <div class="job-meta">
      <span>创建 ${esc(humanDate(j.createdAt))}</span>
      ${j.finishedAt ? `<span>结束 ${esc(humanDate(j.finishedAt))}</span>` : ""}
      <span>${esc(j.id)}</span>
    </div>
  </div>`;
}

function progressText(j, pct) {
  if (j.status !== "running") return j.status === "queued" ? "等待前面的任务完成" : "";
  const size = j.bytesTotal
    ? `${pct}% · ${humanSize(j.bytesDone)} / ${humanSize(j.bytesTotal)}`
    : humanSize(j.bytesDone);
  const files = `${j.filesDone}/${j.filesTotal} 文件`;
  const eta = j.etaSeconds != null ? `剩余约 ${fmtEta(j.etaSeconds)}` : "剩余时间计算中…";
  return `${size} · ${files} · ${eta}${j.current ? ` · ${j.current}` : ""}`;
}

/* ------------------------------------------------------------ 底部常驻条 */

function renderDock() {
  const dock = $("#job-dock");
  const active = state.jobs.filter((j) => j.status === "queued" || j.status === "running");
  if (!active.length) {
    dock.hidden = true;
    dock.dataset.id = "";
    return;
  }
  const j = active[0];
  dock.hidden = false;
  dock.classList.toggle("multi", active.length > 1);
  dock.dataset.id = j.id;
  const pct = j.bytesTotal ? Math.min(100, Math.round((j.bytesDone / j.bytesTotal) * 100)) : 0;
  dock.querySelector("[data-dock=name]").textContent = j.name;
  dock.querySelector("[data-dock=meta]").textContent = active.length > 1
    ? `${active.length} 个任务 · ` + dockMeta(j, pct)
    : dockMeta(j, pct);
  dock.querySelector(".meter-fill").style.width = `${pct}%`;
}

function dockMeta(j, pct) {
  if (j.status === "queued") return "排队中";
  const eta = j.etaSeconds != null ? `剩余 ${fmtEta(j.etaSeconds)}` : "计算剩余时间…";
  return `${pct}% · ${humanSize(j.bytesDone)}${j.bytesTotal ? ` / ${humanSize(j.bytesTotal)}` : ""} · ${eta}`;
}

function renderBadges() {
  const active = state.jobs.filter((j) => j.status === "queued" || j.status === "running");
  setBadge("jobs", active.length, "accent");
}

/* ------------------------------------------------------------ 交互 */

async function onClick(e) {
  const btn = e.target.closest("[data-action=cancel], [data-action=dock-open], [data-action=dock-cancel]");
  if (!btn) return;
  const { action, id } = btn.dataset;
  if (action === "dock-open") {
    location.hash = "#/jobs";
    return;
  }
  const jobId = id || $("#job-dock").dataset.id;
  if (!jobId) return;
  if (!window.confirm("取消该传输任务？已复制过去的文件不会自动回滚。")) return;
  btn.disabled = true;
  try {
    await api("POST", `/api/jobs/${encodeURIComponent(jobId)}/cancel`);
    toast("正在取消任务…", "busy");
    await refreshJobs();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
  }
}
