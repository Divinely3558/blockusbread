/* 入口模块：装配各视图，负责登录态、SSE、轮询心跳与顶部横幅 */
"use strict";

import { $, api, esc, clock, toast, copyText } from "./js/core.js";
import { state, refreshVolumes, refreshJobs, applyJobs, liveTick } from "./js/store.js";
import * as overlays from "./js/overlays.js";
import * as nav from "./js/nav.js";
import * as storage from "./js/storage.js";
import * as browser from "./js/browser.js";
import * as jobs from "./js/jobs.js";
import * as sessions from "./js/sessions.js";
import * as remote from "./js/remote.js";

const LIVE_MS = 2000;
const SESSION_EVERY = 3;   // /api/sessions 频率低一档：每 3 个心跳取一次

let evtSource = null;
let liveTimer = null;
let liveTicks = 0;
let liveFails = 0;
let bound = false;

boot();

async function boot() {
  bind();
  loadVersion();
  try {
    const me = await api("GET", "/api/auth/me");
    await enterApp(me.username);
  } catch {
    showLogin();
  }
}

/* ============================================================ 装配 */

function bind() {
  if (bound) return;
  bound = true;

  remote.init();
  browser.init();
  storage.init();
  jobs.init();
  sessions.init();

  nav.onView("storage", () => storage.render());
  nav.onView("jobs", () => jobs.render());
  nav.onView("sessions", () => sessions.render());
  nav.init();

  $("#login-form").addEventListener("submit", onLogin);
  document.addEventListener("app:logout", showLogin);
  document.addEventListener("visibilitychange", onVisibility);
  // 复制按钮统一走 data 属性：内联 onclick 会被含特殊字符的密码 / 路径截断
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".js-copy");
    if (btn) copyText(btn.dataset.copy || "", btn.dataset.label || "");
  });
}

/* ============================================================ 登录态 */

async function loadVersion() {
  try {
    const h = await fetch("/health", { credentials: "same-origin" }).then((r) => r.json());
    if (h && h.version) $("#login-version").textContent = h.version;
  } catch { /* 版本取不到保留占位 */ }
}

async function onLogin(e) {
  e.preventDefault();
  const btn = e.target.querySelector('button[type="submit"]');
  const errBox = $("#login-error");
  errBox.hidden = true;
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  try {
    const data = await api("POST", "/api/auth/login", {
      username: $("#login-username").value.trim(),
      password: $("#login-password").value,
    });
    $("#login-password").value = "";
    await enterApp(data.username);
  } catch (err) {
    errBox.textContent = err.message;
    errBox.hidden = false;
    $("#login-password").select();
  } finally {
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
  }
}

async function enterApp(username) {
  state.username = username;
  $("#login").hidden = true;
  $("#shell").hidden = false;
  // 先取共享信息：卷行内的 SFTP 路径要带 host / 端口 / 账号
  await loadShareInfo();
  try {
    await refreshVolumes();
  } catch {
    showLogin();
    return;
  }
  paintGlobalBanner();
  refreshJobs();
  nav.markFresh();
  openEvents();
  startLive();
}

function showLogin() {
  state.username = null;
  state.share = null;
  state.jobs = [];
  state.sessions = null;
  state.rates = {};
  state.usage = {};
  state.smart = {};
  state.lastVolSig = null;
  closeEvents();
  stopLive();
  overlays.closeAll();
  $("#share-banner").hidden = true;
  $("#global-banner").hidden = true;
  $("#job-dock").hidden = true;
  $("#shell").hidden = true;
  $("#login").hidden = false;
  $("#login-error").hidden = true;
  nav.setFreshness("off", "未登录");
  $("#login-username").focus();
}

/* ============================================================ 顶部横幅 */

async function loadShareInfo() {
  try {
    state.share = await api("GET", "/api/share");
  } catch {
    state.share = null;
  }
  renderShareBanner();
}

function renderShareBanner() {
  const banner = $("#share-banner");
  const s = state.share;
  if (!s) {
    banner.hidden = true;
    banner.innerHTML = "";
    return;
  }
  // 口令不下发也不上屏：它与登录管理页的口令是同一个，客户端里由用户自己填
  banner.innerHTML = `
    <span class="lead"><i class="ic ic-network"></i>SFTP 共享已就绪 · 账号与网页登录相同</span>
    <span class="row"><span class="lbl">连接地址</span><code>${esc(s.uri)}</code>
      <button class="btn s js-copy" type="button" data-copy="${esc(s.uri)}" data-label="SFTP 地址">
        <i class="ic ic-copy"></i>复制</button></span>
    <span class="row"><span class="lbl">账号</span><code>${esc(s.username)}</code>
      <button class="btn s js-copy" type="button" data-copy="${esc(s.username)}" data-label="账号">
        <i class="ic ic-copy"></i>复制</button>
      <span class="lbl">密码</span><code class="pw-hint">与登录管理页的相同</code></span>
    <span class="note">挂载后的每个卷直接出现在 SFTP 根目录（以卷标命名，无卷标用盘 ID，重名自动加 -2、-3；
      未挂载的卷不显示）。Windows 推荐 WinSCP / FileZilla，macOS / Linux 可用 sftp 命令或文件管理器，
      手机播放器（VLC 等）可直接添加 SFTP 源。</span>`;
  banner.hidden = false;
}

function paintGlobalBanner() {
  const el = $("#global-banner");
  if (state.rememberEnabled) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.className = "callout warning";
  el.innerHTML = `
    <span class="lead"><i class="ic ic-alert"></i>未配置 SECRET_KEY：需要保存凭据的功能不可用</span>
    <span class="note">外接盘的「记住此卷」与「添加远程 SFTP 存储」都要用 SECRET_KEY 加密保存凭据。
      在容器环境变量里设置 SECRET_KEY 并重启服务即可启用；当前仍可用一次性密码解锁外接盘。</span>`;
  el.hidden = false;
}

/* ============================================================ SSE */

function openEvents() {
  closeEvents();
  const es = new EventSource("/api/events");
  evtSource = es;
  es.onopen = () => nav.markFresh();
  // 后端只发 state（reason 区分原因）与 jobs 两类事件
  es.addEventListener("state", async (ev) => {
    if (es !== evtSource) return;              // 重连后旧实例的迟到消息
    let reason = "";
    try { reason = JSON.parse(ev.data || "{}")?.reason || ""; } catch { /* 坏消息忽略 */ }
    try {
      await refreshVolumes();
    } catch {
      showLogin();
      return;
    }
    paintGlobalBanner();
    nav.markFresh();
    // 物理拔出时后端已自动清理挂载，HTTP 调用方收不到响应，只能靠 SSE 提示
    if (reason === "removed") toast("硬盘已拔出，挂载已自动清理", "err");
  });
  es.addEventListener("jobs", (ev) => {
    if (es !== evtSource) return;
    try {
      applyJobs(JSON.parse(ev.data)?.jobs || []);
    } catch { /* 坏消息忽略 */ }
  });
  es.onerror = () => {
    // EventSource 自行重连；401 由心跳里的鉴权复检发现
    nav.setFreshness("off", "实时连接已断开 · 自动重连中");
  };
}

function closeEvents() {
  if (evtSource) {
    evtSource.close();
    evtSource = null;
  }
}

/* ============================================================ 心跳轮询 */

function startLive() {
  stopLive();
  liveTicks = 0;
  liveFails = 0;
  pulse();
  liveTimer = setInterval(pulse, LIVE_MS);
}

function stopLive() {
  if (liveTimer) {
    clearInterval(liveTimer);
    liveTimer = null;
  }
}

function onVisibility() {
  if (!state.username) return;
  if (document.hidden) stopLive();
  else startLive();      // 回到前台立刻补一次，不等下一个周期
}

async function pulse() {
  if (document.hidden || !state.username) return;
  liveTicks++;
  try {
    await liveTick(liveTicks % SESSION_EVERY === 1);
    liveFails = 0;
    nav.markFresh();
  } catch {
    nav.setFreshness("warn", `数据获取失败 · 重试中（${liveFails + 1}）`);
    if (++liveFails >= SESSION_EVERY) recheckAuth();
  }
}

async function recheckAuth() {
  liveFails = 0;
  try {
    await api("GET", "/api/auth/me");
  } catch {
    showLogin();
  }
}
