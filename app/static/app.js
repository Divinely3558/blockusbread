/* BlockUSBRead 管理页：磁盘列表、解锁/挂载/弹出，状态由 SSE 实时驱动。 */
"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  username: null,
  disks: [],
  local: [],
  remote: [],
  rememberEnabled: false,
  share: null,
  evtSource: null,
  speedTimer: null,
  lastSig: null,
};

// ------------------------------------------------------------ 工具

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function humanSize(bytes) {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

function humanRate(bps) {
  if (!bps || bps < 1) return "0 B/s";
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let v = bps, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  const icon = kind === "ok" ? '<i class="ic ic-check"></i>'
    : kind === "err" ? '<i class="ic ic-alert"></i>'
    : kind === "busy" ? '<i class="ic ic-loader ic-spin"></i>'
    : "";
  el.innerHTML = `${icon}<span>${esc(msg)}</span>`;
  $("#toast-container").appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

async function api(method, url, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  let data = null;
  try { data = await resp.json(); } catch { /* 无响应体 */ }
  if (!resp.ok) {
    throw new Error(data?.detail || `请求失败（HTTP ${resp.status}）`);
  }
  return data;
}

// ------------------------------------------------------------ 登录态

async function boot() {
  loadVersion();
  try {
    const me = await api("GET", "/api/auth/me");
    enterApp(me.username);
  } catch {
    showLogin();
  }
}

async function loadVersion() {
  try {
    const h = await fetch("/health", { credentials: "same-origin" }).then((r) => r.json());
    if (h && h.version) $("#login-version").textContent = h.version;
  } catch { /* 版本号获取失败保持占位 */ }
}

function showLogin() {
  closeEvents();
  closeSpeeds();
  state.lastSig = null;
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-error").hidden = true;
}

function enterApp(username) {
  state.username = username;
  $("#login").hidden = true;
  $("#app").hidden = false;
  loadShareInfo();
  refreshDisks();
  openEvents();
  openSpeeds();
  loadJobs();
}

// ------------------------------------------------------------ 传输速率

async function pollSpeeds() {
  try {
    const [rates, usage, sessions] = await Promise.all([
      api("GET", "/api/speeds"),
      api("GET", "/api/usage"),
      api("GET", "/api/sessions"),
    ]);
    document.querySelectorAll(".speed-meter").forEach((el) => {
      const s = rates[el.dataset.speedKey] || {};
      const rx = s.rx || 0;
      const tx = s.tx || 0;
      el.querySelector(".speed-rx .speed-val").textContent = humanRate(rx);
      el.querySelector(".speed-tx .speed-val").textContent = humanRate(tx);
      el.classList.toggle("idle", rx < 1 && tx < 1);
    });
    document.querySelectorAll(".usage-bar").forEach((el) => {
      const u = usage[el.dataset.usageKey];
      if (!u || !u.total) return;
      const pct = Math.min(100, Math.round((u.used / u.total) * 100));
      el.querySelector(".usage-fill").style.width = `${pct}%`;
      el.querySelector(".usage-pct").textContent = `${pct}%`;
      el.querySelector(".usage-detail").textContent =
        `已用 ${humanSize(u.used)} / ${humanSize(u.total)} · 可用 ${humanSize(u.avail)}`;
      el.classList.toggle("near-full", pct >= 90);
    });
    renderSessions(sessions);
  } catch {
    /* 401 或瞬时错误：下个周期自然恢复/转登录页 */
  }
}

// ------------------------------------------------------------ SFTP 会话

state.sessionsOpen = false;

function renderSessions(info) {
  const bar = $("#sessions-bar");
  if (!info || !info.connections) {
    bar.hidden = true;
    state.sessionsOpen = false;
    return;
  }
  bar.hidden = false;
  $("#sessions-summary").textContent =
    `${info.connections} 个 SFTP 连接`;

  const detail = $("#sessions-detail");
  const files = info.openFiles || [];
  if (state.sessionsOpen) {
    detail.hidden = false;
    if (!files.length) {
      detail.innerHTML = `<div class="sessions-empty">已连接，暂无打开的文件</div>`;
    } else {
      detail.innerHTML = files.map((f) => {
        const name = f.path.split("/").pop() || "/";
        const verb = f.writable ? "写入" : "读取";
        return `<div class="session-row">
          <span class="session-verb ${f.writable ? "is-write" : "is-read"}">${verb}</span>
          <span class="session-file" title="${esc(f.path)}">${esc(name)}</span>
          <span class="session-vol">${esc(f.volumeName || f.volume)}</span>
        </div>`;
      }).join("");
    }
  } else {
    detail.hidden = true;
  }
}

document.addEventListener("click", (ev) => {
  if (ev.target.closest("#sessions-toggle")) {
    state.sessionsOpen = !state.sessionsOpen;
    pollSpeeds();
  }
});

function openSpeeds() {
  closeSpeeds();
  pollSpeeds();
  state.speedTimer = setInterval(pollSpeeds, 2000);
}

function closeSpeeds() {
  if (state.speedTimer) {
    clearInterval(state.speedTimer);
    state.speedTimer = null;
  }
}

async function loadShareInfo() {
  try {
    state.share = await api("GET", "/api/share");
    renderShareBanner();
  } catch {
    state.share = null;
  }
}

function renderShareBanner() {
  const banner = $("#share-banner");
  const s = state.share;
  if (!s) { banner.hidden = true; return; }
  banner.innerHTML = `
    <div class="banner-title"><i class="ic ic-network"></i>SFTP 共享已就绪（账号同网页登录）</div>
    <div class="banner-line">
      <span class="banner-label">连接地址：</span>
      <code>${esc(s.uri)}</code>
      <button class="btn mini js-copy" data-copy="${esc(s.uri)}" data-label="SFTP 地址">复制</button>
    </div>
    <div class="banner-line">
      <span class="banner-label">账号：</span>
      <code>${esc(s.username)}</code>
      <button class="btn mini js-copy" data-copy="${esc(s.username)}" data-label="账号">复制</button>
      <span class="banner-label" style="margin-left:10px">密码：</span>
      <code class="pw-mask" title="密码已隐藏，点右侧按钮可复制">${"•".repeat(Math.max(8, String(s.password).length))}</code>
      <button class="btn mini js-copy" data-copy="${esc(s.password)}" data-label="密码">复制密码</button>
    </div>
    <div class="banner-sub">挂载后的每个卷对应 SFTP 子目录：外接存储 &lt;磁盘ID&gt;/part&lt;序号&gt;/fs、本地存储 local/&lt;名称&gt;、远程存储 remote/&lt;名称&gt;；Windows 推荐 WinSCP / FileZilla，macOS / Linux 可用 sftp 命令或文件管理器，手机播放器（VLC 等）可直接添加 SFTP；密码即网页登录密码，页面不显示明文</div>`;
  banner.hidden = false;
}

// 顶栏「挂载远程存储」按钮：打开弹窗表单（仅 SECRET_KEY 已配置时显示，见 render()）
$("#btn-remote").addEventListener("click", () => {
  $("#remote-modal").hidden = false;
  $("#remote-name").focus();
});
$("#remote-close").addEventListener("click", () => {
  $("#remote-modal").hidden = true;
});

// 快速连接 URL 解析
$("#remote-quick-url").addEventListener("input", (e) => {
  const url = e.target.value.trim();
  if (!url) return;

  // 支持格式：sftp://user@host:port/path 或 sftp://host:port/path 或 sftp://host/path
  const match = url.match(/^sftp:\/\/(?:(.+?)@)?([^:\/]+)(?::(\d+))?(\/.*)?$/);
  if (!match) return;

  const [, username, host, port, path] = match;
  if (username) $("#remote-username").value = username;
  if (host) $("#remote-host").value = host;
  if (port) $("#remote-port").value = port;
  if (path) $("#remote-path").value = path;

  // 自动生成名称（使用主机名）
  if (host && !$("#remote-name").value) {
    $("#remote-name").value = host;
  }
});

// 认证方式分段开关（密码 / SSH 私钥）
document.addEventListener("click", (e) => {
  const authBtn = e.target.closest("#remote-auth-seg button");
  if (!authBtn) return;
  document.querySelectorAll("#remote-auth-seg button").forEach((b) =>
    b.classList.toggle("active", b === authBtn));
  const isKey = authBtn.dataset.auth === "key";
  $("#remote-password-row").hidden = isKey;
  $("#remote-key-row").hidden = !isKey;
});

document.addEventListener("submit", async (e) => {
  if (e.target.id !== "remote-form") return;
  e.preventDefault();
  const errBox = $("#remote-error");
  errBox.hidden = true;
  const authMode = document.querySelector("#remote-auth-seg button.active")?.dataset.auth || "password";
  const body = {
    name: $("#remote-name").value.trim(),
    host: $("#remote-host").value.trim(),
    port: parseInt($("#remote-port").value, 10) || 22,
    username: $("#remote-username").value.trim(),
    remotePath: $("#remote-path").value.trim() || "/",
    authMode,
  };
  if (authMode === "key") body.privateKey = $("#remote-key").value;
  else body.password = $("#remote-password").value;
  const btn = e.target.querySelector('button[type="submit"]');
  btn.disabled = true;
  try {
    await api("POST", "/api/remote-stores", body);
    $("#remote-modal").hidden = true;
    $("#remote-form").reset();
    $("#remote-password-row").hidden = false;
    $("#remote-key-row").hidden = true;
    $("#remote-password").value = "";
    $("#remote-key").value = "";
    toast("远程存储已添加，正在挂载…", "ok");
    await refreshDisks();
  } catch (err) {
    errBox.textContent = err.message;
    errBox.hidden = false;
  } finally {
    btn.disabled = false;
  }
});

// 所有复制按钮统一走 data 属性（避免内联 onclick 中引号截断，兼容含特殊字符的密码/路径）
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".js-copy");
  if (btn) copyText(btn.dataset.copy || "", btn.dataset.label || "");
});

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`已复制${label}`, "ok");
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast(`已复制${label}`, "ok");
  }
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errBox = $("#login-error");
  errBox.hidden = true;
  try {
    const data = await api("POST", "/api/auth/login", {
      username: $("#login-username").value.trim(),
      password: $("#login-password").value,
    });
    $("#login-password").value = "";
    enterApp(data.username);
  } catch (err) {
    errBox.textContent = err.message;
    errBox.hidden = false;
  }
});

$("#btn-logout").addEventListener("click", async () => {
  try { await api("POST", "/api/auth/logout"); } catch { /* ignore */ }
  showLogin();
});

// ------------------------------------------------------------ SSE

function openEvents() {
  closeEvents();
  const es = new EventSource("/api/events");
  state.evtSource = es;
  // 后端只发 state（载荷 reason 区分原因）与 jobs 两类事件
  es.addEventListener("state", async (ev) => {
    await refreshDisks();
    let reason = "";
    try {
      reason = JSON.parse(ev.data || "{}")?.reason || "";
    } catch { /* 忽略坏消息 */ }
    // 物理拔出时后端已自动清理挂载，HTTP 调用方收不到响应，只能靠 SSE 提示
    if (reason === "removed") toast("硬盘已拔出，挂载已自动清理");
  });
  es.addEventListener("jobs", (ev) => {
    try {
      jobsState.jobs = (JSON.parse(ev.data)?.jobs) || [];
      renderJobs();
    } catch { /* 忽略坏消息 */ }
  });
  es.onerror = () => {
    // 401 由下一次 me 检查发现；EventSource 会自动重连，无需手动处理
  };
}

function closeEvents() {
  if (state.evtSource) {
    state.evtSource.close();
    state.evtSource = null;
  }
}

// ------------------------------------------------------------ 数据刷新

async function refreshDisks() {
  try {
    const snap = await api("GET", "/api/volumes");
    // 数据无变化不重绘：避免卡片闪烁，也避免清空用户正在输入的解锁密码
    const sig = JSON.stringify(snap);
    if (sig === state.lastSig) return;
    state.lastSig = sig;
    state.disks = snap.disks || [];
    state.local = snap.local || [];
    state.remote = snap.remote || [];
    state.rememberEnabled = !!snap.rememberEnabled;
    render();
  } catch {
    showLogin();
  }
}

$("#btn-refresh").addEventListener("click", async () => {
  await api("POST", "/api/rescan");
  toast("正在刷新设备列表…", "busy");
  setTimeout(refreshDisks, 800);
});

// ------------------------------------------------------------ 渲染

function render() {
  const hasLocal = state.local.length > 0;
  const hasDisks = state.disks.length > 0;
  const hasRemote = state.remote.length > 0;
  $("#sec-local").hidden = !hasLocal;
  $("#sec-disks").hidden = !hasDisks;
  $("#sec-remote").hidden = !hasRemote;
  $("#storage-empty").hidden = hasLocal || hasDisks || hasRemote;
  // 顶栏「挂载远程存储」入口：SECRET_KEY 未配置时隐藏（凭据无法加密保存）
  $("#btn-remote").hidden = !state.rememberEnabled;
  if (hasLocal) $("#local-list").innerHTML = state.local.map(renderLocalCard).join("");
  if (hasDisks) {
    $("#disk-list").innerHTML = state.disks.map(renderDisk).join("");
    loadMissingSmart();
  }
  if (hasRemote) $("#remote-list").innerHTML = state.remote.map(renderRemoteCard).join("");
}

// 容量条 / 速率计 / SFTP 路径行：本地、远程与外接共用同一套视觉
function usageBarHtml(key) {
  return `
    <span class="usage-bar" data-usage-key="${esc(key)}" title="存储容量与剩余空间">
      <span class="usage-track"><span class="usage-fill" style="width:0%"></span></span>
      <span class="usage-meta"><span class="usage-pct">—</span><span class="usage-detail">—</span></span>
    </span>`;
}

function speedMeterHtml(key) {
  return `
    <span class="speed-meter idle" data-speed-key="${esc(key)}"
          title="实时传输速度：下载（存储至客户端）/ 上传（客户端至存储）">
      <span class="speed speed-rx"><i class="ic ic-arrow-down"></i><span class="speed-val">0 B/s</span></span>
      <span class="speed speed-tx"><i class="ic ic-arrow-up"></i><span class="speed-val">0 B/s</span></span>
    </span>`;
}

function sftpLineHtml(sftp) {
  return `<div class="sftp-line"><i class="ic ic-folder"></i><code>${esc(sftp)}</code></div>`;
}

function renderLocalCard(v) {
  const sftp = sftpUrlFor(v.sftpPath);
  return `
    <div class="part-row state-mounted" data-key="${esc(v.key)}">
      <div class="part-main">
        <div class="part-info">
          <div class="part-name"><span class="tag fs">本地</span> ${esc(v.name)}</div>
        </div>
        ${usageBarHtml(v.key)}
        <div class="part-actions">
          <span class="tag rw">读写模式</span>
          <button class="btn primary" onclick="openBrowser('${esc(v.key)}')">
            <i class="ic ic-folder-open"></i>浏览文件
          </button>
          <button class="btn primary js-copy" data-copy="${esc(sftp)}" data-label="存储 SFTP 路径">
            <i class="ic ic-copy"></i>复制 SFTP 路径
          </button>
          ${speedMeterHtml(v.key)}
          ${sftpLineHtml(sftp)}
        </div>
      </div>
    </div>`;
}

function renderRemoteCard(v) {
  const sftp = sftpUrlFor(v.sftpPath);
  const stateCls = v.state === "error" ? "error" : v.state === "mounting" ? "mounting" : "mounted";
  const stateTag = v.state === "mounted"
    ? `<span class="tag rw">读写模式</span>`
    : v.state === "mounting"
      ? `<span class="tag busy"><i class="ic ic-loader ic-spin"></i>挂载中…</span>`
      : `<span class="tag unsupported">连接失败</span>`;
  return `
    <div class="part-row state-${stateCls}" data-key="${esc(v.key)}">
      <div class="part-main">
        <div class="part-info">
          <div class="part-name"><span class="tag fs">远程</span> ${esc(v.name)}</div>
          <div class="part-meta">${esc(v.host)}:${esc(v.port)} · 远端路径 ${esc(v.remotePath)}</div>
        </div>
        ${v.state === "mounted" ? usageBarHtml(v.key) : ""}
        <div class="part-actions">
          ${stateTag}
          ${v.state === "mounted" ? `
            <button class="btn primary" onclick="openBrowser('${esc(v.key)}')">
              <i class="ic ic-folder-open"></i>浏览文件
            </button>
            <button class="btn primary js-copy" data-copy="${esc(sftp)}" data-label="存储 SFTP 路径">
              <i class="ic ic-copy"></i>复制 SFTP 路径
            </button>
            ${speedMeterHtml(v.key)}
            ${sftpLineHtml(sftp)}` : ""}
          <button class="btn ${v.state === "error" ? "primary" : ""}"
                  onclick="reconnectRemote('${esc(v.key)}')">
            <i class="ic ic-refresh"></i>重新连接
          </button>
          <button class="btn danger" onclick="deleteRemote('${esc(v.key)}')">
            <i class="ic ic-trash"></i>删除
          </button>
        </div>
      </div>
      ${v.state === "error" && v.error
        ? `<div class="part-error"><i class="ic ic-alert"></i><span>${esc(v.error)}</span></div>` : ""}
    </div>`;
}

function renderDisk(disk) {
  const mountedCount = disk.partitions.filter((p) => p.state === "mounted").length;
  const meta = [
    disk.serial ? `序列号 ${esc(disk.serial)}` : "",
    disk.tran ? esc(disk.tran.toUpperCase()) : "",
    humanSize(disk.size),
    `<span class="desktop-only">${esc(disk.name)}</span>`,
  ].filter(Boolean).join(" · ");

  return `
    <div class="disk-card">
      <div class="disk-head">
        <div class="disk-title">
          <span class="disk-icon"><i class="ic ic-disk"></i></span>
          <div>
            <div class="disk-name">${esc(disk.displayName)}</div>
            <div class="disk-meta">${meta}</div>
          </div>
        </div>
        <div class="disk-actions">
          ${mountedCount ? `
            <button class="btn danger" onclick="ejectDisk('${esc(disk.id)}')">
              <i class="ic ic-eject"></i>安全弹出整块硬盘
            </button>` : ""}
        </div>
      </div>
      ${renderSmart(disk)}
      ${disk.partitions.length
        ? disk.partitions.map(renderPartition).join("")
        : `<div class="part-row"><div class="part-meta">该磁盘没有可识别的分区</div></div>`}
    </div>`;
}

// ------------------------------------------------------------ SMART 健康

state.smart = {};

function smartChipsHtml(info) {
  if (info.status === "unavailable") return "";
  const health = info.healthy === null || info.healthy === undefined
    ? ""
    : `<span class="smart-chip ${info.healthy ? "ok" : "bad"}">
         <i class="ic ${info.healthy ? "ic-check" : "ic-alert"}"></i>${info.healthy ? "健康" : "健康异常"}
       </span>`;
  const hot = (info.tempC ?? 0) >= 55;
  const temp = info.tempC !== null && info.tempC !== undefined
    ? `<span class="smart-chip ${hot ? "bad" : ""}"><i class="ic ic-thermometer"></i>${info.tempC}°C</span>`
    : "";
  const hours = info.powerOnHours !== null && info.powerOnHours !== undefined
    ? `<span class="smart-chip"><i class="ic ic-clock"></i>${info.powerOnHours.toLocaleString()} 小时</span>`
    : "";
  return `<div class="smart-chips">${health}${temp}${hours}</div>`;
}

function renderSmart(disk) {
  const info = state.smart[disk.id];
  if (info && info.status === "unavailable") return "";
  return `
    <div class="smart-row" data-smart-id="${esc(disk.id)}">
      ${info ? smartChipsHtml(info) : `<span class="smart-loading"><i class="ic ic-loader ic-spin"></i>读取 SMART…</span>`}
    </div>`;
}

async function loadSmart(diskId, force = false) {
  try {
    const info = await api("GET", `/api/disks/${encodeURIComponent(diskId)}/smart${force ? "?force=1" : ""}`);
    state.smart[diskId] = info;
    const row = document.querySelector(`.smart-row[data-smart-id="${CSS.escape(diskId)}"]`);
    if (!row) return;
    if (info.status === "unavailable") {
      row.remove();
    } else {
      row.innerHTML = smartChipsHtml(info);
    }
  } catch {
    /* 查询失败：清除占位，下次重绘时自动重试 */
    delete state.smart[diskId];
  }
}

function loadMissingSmart() {
  for (const disk of state.disks || []) {
    if (!(disk.id in state.smart)) {
      state.smart[disk.id] = null;   // 占位，避免重复请求
      loadSmart(disk.id);
    }
  }
}

function renderPartition(p) {
  const tags = [
    p.bitlocker ? `<span class="tag bitlocker"><i class="ic ic-lock"></i>BitLocker</span>` : "",
    p.fstype && !p.bitlocker ? `<span class="tag fs">${esc(p.fstype.toUpperCase())}</span>` : "",
  ].join(" ");

  const title = `${esc(p.label || `分区 ${p.number}`)} · ${humanSize(p.size)}
    <span class="desktop-only">· ${esc(p.path)}</span>`;

  return `
    <div class="part-row state-${esc(p.state)}" data-key="${esc(p.key)}">
      <div class="part-main">
        <div class="part-info">
          <div class="part-name">${tags} ${title}</div>
        </div>
        ${p.state === "mounted" ? `
        <span class="usage-bar" data-usage-key="${esc(p.key)}" title="卷容量与剩余空间">
          <span class="usage-track"><span class="usage-fill" style="width:0%"></span></span>
          <span class="usage-meta"><span class="usage-pct">—</span><span class="usage-detail">—</span></span>
        </span>` : ""}
        <div class="part-actions">${renderActions(p)}</div>
      </div>
      ${p.state === "error" && p.error
        ? `<div class="part-error"><i class="ic ic-alert"></i><span>${esc(p.error)}</span></div>` : ""}
      ${renderActionPanel(p)}
    </div>`;
}

function renderActions(p) {
  if (["unlocking", "mounting", "unmounting"].includes(p.state)) {
    return `<span class="tag busy"><i class="ic ic-loader ic-spin"></i>处理中…</span>`;
  }
  if (p.state === "mounted") {
    const sftp = sftpUrl(p);
    return `
      <span class="tag ${p.mode === "rw" ? "rw" : "ro"}">
        ${p.mode === "rw" ? "读写模式" : "只读模式"}
      </span>
      <button class="btn primary" onclick="openBrowser('${esc(p.key)}')">
        <i class="ic ic-folder-open"></i>浏览文件
      </button>
      <button class="btn primary js-copy" data-copy="${esc(sftp)}" data-label="卷 SFTP 路径">
        <i class="ic ic-copy"></i>复制 SFTP 路径
      </button>
      <button class="btn danger" onclick="ejectVolume('${esc(p.key)}')"><i class="ic ic-eject"></i>安全弹出</button>
      ${p.remembered
        ? `<button class="btn" onclick="forgetCredential('${esc(p.key)}')">忘记凭据</button>`
        : ""}
      <span class="speed-meter idle" data-speed-key="${esc(p.key)}"
            title="实时传输速度：下载（U 盘至客户端）/ 上传（客户端至 U 盘）">
        <span class="speed speed-rx"><i class="ic ic-arrow-down"></i><span class="speed-val">0 B/s</span></span>
        <span class="speed speed-tx"><i class="ic ic-arrow-up"></i><span class="speed-val">0 B/s</span></span>
      </span>
      <div class="sftp-line"><i class="ic ic-folder"></i><code>${esc(sftp)}</code></div>`;
  }
  if (!p.supported) {
    return `<span class="tag unsupported">不支持${p.fstype ? "：" + esc(p.fstype) : ""}</span>`;
  }
  return "";
}

function renderActionPanel(p) {
  if (["unlocking", "mounting", "unmounting", "mounted"].includes(p.state)) return "";
  if (!p.supported) return "";

  if (p.bitlocker) {
    return `
      <div class="unlock-panel">
        <div class="seg" role="tablist">
          <button type="button" class="active" data-kind="password"
                  onclick="switchKind('${esc(p.key)}', 'password')">密码解锁</button>
          <button type="button" data-kind="recovery"
                  onclick="switchKind('${esc(p.key)}', 'recovery')">恢复密钥</button>
        </div>
        <input type="password" class="cred-input" autocomplete="off"
               placeholder="输入 BitLocker 密码"
               style="width:100%;padding:9px 10px;border:1px solid var(--border);border-radius:6px">
        <div class="unlock-options">
          <label class="checkline">
            <input type="checkbox" class="rw-check">
            以读写模式解锁（默认只读；写入 BitLocker/NTFS 存在损坏风险）
          </label>
          <label class="checkline" title="${state.rememberEnabled ? "" : "未设置 SECRET_KEY，功能不可用"}">
            <input type="checkbox" class="remember-check"
                   ${state.rememberEnabled ? "" : "disabled"}>
            记住此卷（${state.rememberEnabled
              ? "凭据加密保存，插入/重启后自动解锁"
              : "需设置 SECRET_KEY"}）
          </label>
        </div>
        <button class="btn primary unlock-submit"
                onclick="unlockVolume('${esc(p.key)}')"><i class="ic ic-unlock"></i>解锁并挂载</button>
      </div>`;
  }

  return `
    <div class="unlock-panel">
      <div class="unlock-options">
        <label class="checkline">
          <input type="checkbox" class="rw-check">
          以读写模式挂载（默认只读）
        </label>
      </div>
      <button class="btn primary unlock-submit"
              onclick="mountVolume('${esc(p.key)}')">挂载分区</button>
    </div>`;
}

function sftpUrlFor(sub) {
  const s = state.share;
  const host = s ? s.host : location.hostname;
  const port = s ? s.port : 2222;
  const auth = s && s.username ? `${s.username}@` : "";
  return `sftp://${auth}${host}:${port}/${sub}`;
}

function sftpUrl(p) {
  return sftpUrlFor(`${p.diskId}/part${p.number}/fs`);
}

// ------------------------------------------------------------ 操作

function rowOf(key) {
  return document.querySelector(`.part-row[data-key="${CSS.escape(key)}"]`);
}

function switchKind(key, kind) {
  const row = rowOf(key);
  row.querySelectorAll(".seg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.kind === kind));
  const input = row.querySelector(".cred-input");
  input.placeholder = kind === "recovery"
    ? "输入 48 位 BitLocker 恢复密钥"
    : "输入 BitLocker 密码";
}

async function unlockVolume(key) {
  const row = rowOf(key);
  const kind = row.querySelector(".seg button.active").dataset.kind;
  const secret = row.querySelector(".cred-input").value.trim();
  const writable = row.querySelector(".rw-check").checked;
  const remember = row.querySelector(".remember-check").checked;
  if (!secret) { toast("请先输入密码或恢复密钥", "err"); return; }
  if (writable && !window.confirm(
    "读写模式通过第三方工具链（dislocker + NTFS-3G）写入 BitLocker 卷，\n" +
    "存在数据损坏风险。确认以读写模式解锁吗？"
  )) return;

  try {
    await api("POST", `/api/volumes/${encodeURIComponent(key)}/unlock`,
      { kind, secret, writable, remember });
    toast("正在解锁挂载…", "busy");
  } catch (err) {
    toast(err.message, "err");
  }
  refreshDisks();
}

async function mountVolume(key) {
  const row = rowOf(key);
  const writable = row.querySelector(".rw-check").checked;
  if (writable && !window.confirm("确认以读写模式挂载该分区吗？")) return;
  try {
    await api("POST", `/api/volumes/${encodeURIComponent(key)}/mount`, { writable });
    toast("正在挂载…", "busy");
  } catch (err) {
    toast(err.message, "err");
  }
  refreshDisks();
}

async function ejectVolume(key) {
  if (!window.confirm("安全弹出该分区？将先卸载文件系统并断开解密连接。")) return;
  try {
    const r = await api("POST", `/api/volumes/${encodeURIComponent(key)}/eject`);
    if (r.safeToRemove) toast("已卸载", "ok");
  } catch (err) {
    toast(err.message, "err");
    refreshDisks();
  }
}

async function ejectDisk(id) {
  if (!window.confirm("将卸载该硬盘上所有已挂载的分区，确认安全弹出整块硬盘？")) return;
  try {
    const r = await api("POST", `/api/disks/${encodeURIComponent(id)}/eject`);
    if (r.safeToRemove) toast("整块硬盘已弹出，可以安全拔除", "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function forgetCredential(key) {
  if (!window.confirm("删除已保存的解锁凭据？之后重新插入将需要手动输入。")) return;
  try {
    await api("DELETE", `/api/volumes/${encodeURIComponent(key)}/credential`);
    toast("已忘记该卷的凭据");
    refreshDisks();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function reconnectRemote(key) {
  try {
    await api("POST", `/api/remote-stores/${encodeURIComponent(key)}/reconnect`);
    toast("正在重新连接…", "busy");
  } catch (err) {
    toast(err.message, "err");
  }
  refreshDisks();
}

async function deleteRemote(key) {
  if (!window.confirm("确认删除该远程存储？仅删除挂载配置，不会删除远端文件。")) return;
  try {
    await api("DELETE", `/api/remote-stores/${encodeURIComponent(key)}`);
    toast("已删除远程存储", "ok");
    refreshDisks();
  } catch (err) {
    toast(err.message, "err");
  }
}

// ------------------------------------------------------------ 网页文件浏览

const fsState = {
  key: null,
  label: "",
  path: "",
  writable: false,
  entries: [],
  crumbs: [],
};

const VIDEO_EXT = ["mp4", "m4v", "mkv", "webm", "mov", "avi", "ts", "mpg", "mpeg", "3gp", "flv", "wmv"];
const AUDIO_EXT = ["mp3", "flac", "aac", "m4a", "ogg", "oga", "wav", "opus", "wma"];
const IMAGE_EXT = ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "heif"];
// 可在线预览的纯文本/代码/配置类型（与后端白名单保持一致）
const TEXT_EXT = ["txt", "log", "ini", "inf", "conf", "cfg", "config", "properties", "prop",
  "env", "md", "markdown", "json", "xml", "csv", "tsv", "yml", "yaml", "toml",
  "sh", "bash", "zsh", "bat", "cmd", "ps1", "py", "js", "mjs", "ts", "css",
  "scss", "less", "html", "htm", "svg", "c", "h", "cpp", "cc", "hpp", "java",
  "go", "rs", "rb", "php", "pl", "lua", "sql"];

function extOf(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}
function fileKind(name) {
  const ext = extOf(name);
  if (VIDEO_EXT.includes(ext)) return "video";
  if (AUDIO_EXT.includes(ext)) return "audio";
  if (IMAGE_EXT.includes(ext)) return "image";
  if (TEXT_EXT.includes(ext)) return "text";
  return "other";
}
function humanDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function encPath(p) { return encodeURIComponent(p || ""); }
function rawUrl(key, path, download) {
  return `/api/volumes/${encodeURIComponent(key)}/raw?path=${encPath(path)}${download ? "&download=1" : ""}`;
}
function findVolume(key) {
  if (key.startsWith("local:")) {
    const v = (state.local || []).find((x) => x.key === key);
    return v ? { name: v.name } : null;
  }
  if (key.startsWith("remote:")) {
    const v = (state.remote || []).find((x) => x.key === key);
    return v ? { name: v.name } : null;
  }
  for (const d of state.disks) {
    const p = d.partitions.find((x) => x.key === key);
    if (p) return { disk: d, part: p };
  }
  return null;
}

function volumeDisplayName(key) {
  const found = findVolume(key);
  if (!found) return key;
  if (found.part) {
    return found.part.label || `${found.disk.displayName} · 分区 ${found.part.number}`;
  }
  return found.name || key;
}

async function openBrowser(key) {
  const found = findVolume(key);
  if (!found) { toast("卷不可用", "err"); return; }
  fsState.key = key;
  fsState.label = volumeDisplayName(key);
  $("#fs-title").textContent = fsState.label;
  $("#fs-modal").hidden = false;
  await loadFsDir("");
}

function closeBrowser() {
  $("#fs-modal").hidden = true;
  fsState.key = null;
  fsState.entries = [];
}

async function loadFsDir(path) {
  const list = $("#fs-list");
  list.innerHTML = `<div class="fs-loading"><i class="ic ic-loader ic-spin"></i> 读取中…</div>`;
  try {
    const data = await api("GET", `/api/volumes/${encodeURIComponent(fsState.key)}/browse?path=${encPath(path)}`);
    fsState.path = data.path || "";
    fsState.writable = !!data.writable;
    fsState.entries = data.entries || [];
    fsState.crumbs = data.crumbs || [];
    renderFs();
  } catch (err) {
    toast(err.message, "err");
    closeBrowser();
  }
}

function renderFs() {
  $("#fs-upload").hidden = !fsState.writable;
  $("#fs-crumbs").innerHTML = fsState.crumbs.map((c, i) => {
    const last = i === fsState.crumbs.length - 1;
    return last
      ? `<span class="crumb cur">${esc(c.name)}</span>`
      : `<button class="crumb" data-path="${esc(c.path)}">${esc(c.name)}</button><span class="crumb-sep">/</span>`;
  }).join("");

  const list = $("#fs-list");
  if (!fsState.entries.length) {
    list.innerHTML = `<div class="fs-empty">空文件夹</div>`;
    return;
  }
  list.innerHTML = fsState.entries.map((e) => {
    const full = fsState.path ? `${fsState.path}/${e.name}` : e.name;
    const icon = e.isDir ? "folder" : ({ video: "film", audio: "music", image: "image", text: "file" }[fileKind(e.name)] || "file");
    const nameCell = `
      <span class="fs-ic fs-ic-${icon}"><i class="ic ic-${icon === "folder" ? "folder" : icon}"></i></span>
      <span class="fs-name" title="${esc(e.name)}">${esc(e.name)}</span>`;
    let actions;
    if (e.isDir) {
      actions = (writableTargets().length ? transferAction(full, e.name) : "")
        + (fsState.writable ? fsWriteActions(full, e.name, true) : "");
    } else {
      actions = `<button class="btn mini" title="下载" data-act="download" data-path="${esc(full)}"><i class="ic ic-download"></i></button>`
        + (writableTargets().length ? transferAction(full, e.name) : "")
        + (fsState.writable ? fsWriteActions(full, e.name, false) : "");
    }
    const meta = e.isDir ? "文件夹" : `${humanSize(e.size)} · ${humanDate(e.mtime)}`;
    return `
      <div class="fs-row" data-path="${esc(full)}" data-dir="${e.isDir ? 1 : 0}">
        <div class="fs-main">${nameCell}</div>
        <div class="fs-meta desktop-only">${esc(meta)}</div>
        <div class="fs-actions">${actions}</div>
      </div>`;
  }).join("");
}

// 传输按钮：只读盘只能复制出去；读写盘默认移动（弹窗内可勾选改为复制）
function transferAction(full, name) {
  if (fsState.writable) {
    return `
    <button class="btn mini" title="移动到其他位置（弹窗内可改为复制）" data-act="move" data-op="move" data-path="${esc(full)}" data-name="${esc(name)}">
      <i class="ic ic-move"></i>
    </button>`;
  }
  return `
    <button class="btn mini" title="复制到其他硬盘（只读盘不能移动）" data-act="move" data-op="copy" data-path="${esc(full)}" data-name="${esc(name)}">
      <i class="ic ic-copy"></i>
    </button>`;
}

// 当前可作为移动 / 复制目标的卷（本地、远程恒可写；外接需以读写模式挂载）
function writableTargets() {
  const out = [];
  for (const v of state.local || []) {
    out.push({ key: v.key, label: v.name, group: "本地存储" });
  }
  for (const d of state.disks || []) {
    for (const p of d.partitions || []) {
      if (p.state === "mounted" && p.mode === "rw") {
        out.push({ key: p.key, label: volumeLabel(p, d), group: "外接存储" });
      }
    }
  }
  for (const v of state.remote || []) {
    if (v.state === "mounted") {
      out.push({ key: v.key, label: v.name, group: "远程存储" });
    }
  }
  return out;
}

function fsWriteActions(full, name, isDir) {
  return `
    <button class="btn mini" title="重命名" data-act="rename" data-path="${esc(full)}" data-name="${esc(name)}">
      <i class="ic ic-edit"></i>
    </button>
    <button class="btn mini danger" title="删除" data-act="delete" data-path="${esc(full)}" data-dir="${isDir ? 1 : 0}">
      <i class="ic ic-trash"></i>
    </button>`;
}

$("#fs-crumbs").addEventListener("click", (e) => {
  const btn = e.target.closest(".crumb");
  if (btn) loadFsDir(btn.dataset.path || "");
});

$("#fs-list").addEventListener("click", (e) => {
  const actBtn = e.target.closest("[data-act]");
  const row = e.target.closest(".fs-row");
  if (actBtn) {
    e.stopPropagation();
    const { act, path } = actBtn.dataset;
    if (act === "download") downloadFile(path);
    else if (act === "move") openMove(path, actBtn.dataset.name, actBtn.dataset.op || "move");
    else if (act === "rename") openRename(path, actBtn.dataset.name);
    else if (act === "delete") deleteEntry(path, actBtn.dataset.dir === "1");
    return;
  }
  if (row && row.dataset.dir === "1") loadFsDir(row.dataset.path);
  else if (row) openEntry(row.dataset.path);
});

function openEntry(path) {
  const name = path.split("/").pop() || path;
  const kind = fileKind(name);
  if (kind === "other") { downloadFile(path); return; }
  if (kind === "text") { openTextPreview(path, name); return; }
  const url = rawUrl(fsState.key, path, false);
  $("#player-title").textContent = name;
  const body = $("#player-body");
  if (kind === "video") {
    body.innerHTML = `<video src="${esc(url)}" controls autoplay preload="metadata" playsinline></video>`;
  } else if (kind === "audio") {
    body.innerHTML = `<div class="audio-wrap"><i class="ic ic-music audio-art"></i>
      <audio src="${esc(url)}" controls autoplay preload="metadata"></audio></div>`;
  } else {
    body.innerHTML = `<img src="${esc(url)}" alt="${esc(name)}">`;
  }
  $("#player-modal").hidden = false;
}

async function openTextPreview(path, name) {
  const modal = $("#text-modal");
  $("#text-title").textContent = name;
  $("#text-meta").textContent = "读取中…";
  // textContent 赋值：盘内 HTML/脚本只作纯文本显示，不会执行
  const pre = $("#text-body");
  pre.textContent = "";
  pre.classList.add("is-loading");
  $("#text-download").onclick = () => downloadFile(path);
  modal.hidden = false;
  try {
    const data = await api(
      "GET",
      `/api/volumes/${encodeURIComponent(fsState.key)}/preview?path=${encPath(path)}`
    );
    $("#text-meta").textContent = `${humanSize(data.size)} · ${data.encoding || "文本"}`;
    pre.textContent = data.content || "（空文件）";
  } catch (err) {
    pre.classList.remove("is-loading");
    modal.hidden = true;
    toast(err.message, "err");
  } finally {
    pre.classList.remove("is-loading");
  }
}

function closeTextPreview() {
  $("#text-modal").hidden = true;
  $("#text-body").textContent = "";
}

function downloadFile(path) {
  const a = document.createElement("a");
  a.href = rawUrl(fsState.key, path, true);
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function closePlayer() {
  $("#player-modal").hidden = true;
  $("#player-body").innerHTML = "";
}

$("#fs-close").addEventListener("click", closeBrowser);
$("#fs-reload").addEventListener("click", () => loadFsDir(fsState.path));
$("#player-close").addEventListener("click", closePlayer);
$("#text-close").addEventListener("click", closeTextPreview);

// 遮罩点击 / Esc 关闭弹窗
[["#fs-modal", closeBrowser], ["#player-modal", closePlayer], ["#text-modal", closeTextPreview]]
  .forEach(([sel, fn]) => $(sel).addEventListener("click", (e) => {
    if (e.target === $(sel)) fn();
  }));

// ------------------------------------------------------------ 移动 / 后台传输

const moveState = {
  srcKey: null,
  srcPath: "",
  srcName: "",
  destKey: "",
  destPath: "",
  srcWritable: false,   // 源卷是否可写（可写=可移动，只读=只能复制）
  op: "move",           // move | copy
  entries: [],
  crumbs: [],
  busy: false,
};
const jobsState = { jobs: [] };

function volumeLabel(part, disk) {
  return part.label || `${disk.displayName || disk.id} · 分区 ${part.number}`;
}

async function openMove(path, name, wantOp = "move") {
  const targets = writableTargets();
  if (!targets.length) {
    toast("没有可写的存储作为目标（本地 / 远程恒可写，外接需以读写模式挂载）", "err");
    return;
  }
  const src = findVolume(fsState.key);
  moveState.srcKey = fsState.key;
  moveState.srcPath = path;
  moveState.srcName = name;
  // 外接卷看挂载模式；本地 / 远程恒可写
  moveState.srcWritable = src ? (!src.part || src.part.mode === "rw") : false;
  // 只读源强制复制；读写源默认移动，用户可在弹窗勾选改为复制
  moveState.op = moveState.srcWritable ? wantOp : "copy";

  $("#move-src-name").textContent = name;
  const hint = $("#move-hint");
  const keepRow = $("#move-keep-row");
  if (!moveState.srcWritable) {
    // 只读硬盘：只能复制
    hint.textContent = "源硬盘为只读挂载，只能复制（源文件会保留）。";
    hint.hidden = false;
    keepRow.hidden = true;
  } else {
    hint.hidden = true;
    keepRow.hidden = false;
  }
  $("#move-keep").checked = moveState.op === "copy";
  updateMoveModeUI();

  // 按 本地 / 外接 / 远程 分组渲染下拉选项
  const groups = [];
  for (const t of targets) {
    let g = groups.find((x) => x.title === t.group);
    if (!g) { g = { title: t.group, items: [] }; groups.push(g); }
    g.items.push(t);
  }
  const sel = $("#move-disk-select");
  sel.innerHTML = groups.map((g) =>
    `<optgroup label="${esc(g.title)}">${g.items.map((t) =>
      `<option value="${esc(t.key)}">${esc(t.label)}</option>`).join("")}</optgroup>`).join("");
  // 默认选另一个存储（跨存储移动是主要场景）
  const other = targets.find((t) => t.key !== fsState.key) || targets[0];
  sel.value = other.key;
  moveState.destKey = other.key;

  $("#move-confirm").disabled = false;
  $("#move-modal").hidden = false;
  await loadMoveDir("");
}

function updateMoveModeUI() {
  const isCopy = moveState.op === "copy";
  $("#move-title").textContent = isCopy ? "复制到…" : "移动到…";
  $("#move-confirm-label").textContent = isCopy ? "开始复制" : "开始移动";
  $("#move-confirm").querySelector(".ic").className = `ic ${isCopy ? "ic-copy" : "ic-move"}`;
}

$("#move-keep").addEventListener("change", (e) => {
  moveState.op = e.target.checked ? "copy" : "move";
  updateMoveModeUI();
});

function closeMove() {
  $("#move-modal").hidden = true;
  moveState.busy = false;
}

async function loadMoveDir(path) {
  const list = $("#move-dir-list");
  list.innerHTML = `<div class="fs-loading"><i class="ic ic-loader ic-spin"></i> 读取中…</div>`;
  try {
    const data = await api("GET", `/api/volumes/${encodeURIComponent(moveState.destKey)}/browse?path=${encPath(path)}`);
    moveState.destPath = data.path || "";
    moveState.crumbs = data.crumbs || [];
    moveState.entries = (data.entries || []).filter((e) => e.isDir);
    renderMoveDirs();
  } catch (err) {
    toast(err.message, "err");
  }
}

function renderMoveDirs() {
  $("#move-crumbs").innerHTML = moveState.crumbs.map((c, i) => {
    const last = i === moveState.crumbs.length - 1;
    return last
      ? `<span class="crumb cur">${esc(c.name)}</span>`
      : `<button class="crumb" data-path="${esc(c.path)}">${esc(c.name)}</button><span class="crumb-sep">/</span>`;
  }).join("");
  const list = $("#move-dir-list");
  if (!moveState.entries.length) {
    list.innerHTML = `<div class="fs-empty">该文件夹下没有子文件夹，将放到当前位置</div>`;
    return;
  }
  list.innerHTML = moveState.entries.map((e) => `
    <div class="fs-row move-dir-row" data-path="${esc(moveState.destPath ? `${moveState.destPath}/${e.name}` : e.name)}">
      <div class="fs-main">
        <span class="fs-ic fs-ic-folder"><i class="ic ic-folder"></i></span>
        <span class="fs-name">${esc(e.name)}</span>
      </div>
    </div>`).join("");
}

$("#move-disk-select").addEventListener("change", async (e) => {
  moveState.destKey = e.target.value;
  await loadMoveDir("");
});
$("#move-crumbs").addEventListener("click", (e) => {
  const btn = e.target.closest(".crumb");
  if (btn) loadMoveDir(btn.dataset.path || "");
});
$("#move-dir-list").addEventListener("click", (e) => {
  const row = e.target.closest(".move-dir-row");
  if (row) loadMoveDir(row.dataset.path);
});
$("#move-close").addEventListener("click", closeMove);
$("#move-cancel-btn").addEventListener("click", closeMove);
$("#move-modal").addEventListener("click", (e) => {
  if (e.target === $("#move-modal")) closeMove();
});

$("#move-confirm").addEventListener("click", async () => {
  if (moveState.busy) return;
  moveState.busy = true;
  const btn = $("#move-confirm");
  btn.disabled = true;
  try {
    const res = await api("POST", `/api/volumes/${encodeURIComponent(moveState.srcKey)}/move`, {
      path: moveState.srcPath,
      destKey: moveState.destKey,
      destPath: moveState.destPath,
      mode: moveState.op,
    });
    closeMove();
    toast(res.op === "copy"
      ? `已开始后台复制「${moveState.srcName}」，源文件保留`
      : `已开始后台移动「${moveState.srcName}」`, "ok");
    loadFsDir(fsState.path);
    openJobs(false);
    await loadJobs();
  } catch (err) {
    toast(err.message, "err");
    moveState.busy = false;
    btn.disabled = false;
  }
});

// ---------------- 任务中心

async function loadJobs() {
  try {
    const data = await api("GET", "/api/jobs");
    jobsState.jobs = data.jobs || [];
    renderJobs();
  } catch { /* 未登录等场景忽略 */ }
}

function fmtEta(sec) {
  if (sec < 60) return `${sec} 秒`;
  if (sec < 3600) {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return s ? `${m} 分 ${s} 秒` : `${m} 分`;
  }
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
}

function renderJobs() {
  const active = jobsState.jobs.filter((j) => j.status === "queued" || j.status === "running");
  const badge = $("#jobs-badge");
  const btnJobs = $("#btn-jobs");
  if (active.length) {
    btnJobs.hidden = false;
    badge.hidden = false;
    badge.textContent = active.length;
  } else {
    badge.hidden = true;
    btnJobs.hidden = jobsState.jobs.length === 0;
  }

  const list = $("#jobs-list");
  if (!jobsState.jobs.length) {
    list.innerHTML = `<div class="fs-empty">暂无传输任务</div>`;
    return;
  }
  list.innerHTML = jobsState.jobs.map((j) => {
    const pct = j.bytesTotal ? Math.min(100, Math.round((j.bytesDone / j.bytesTotal) * 100)) : 0;
    const statusMap = {
      queued: ["排队中", "tag busy"],
      running: ["传输中", "tag busy"],
      done: ["已完成", "tag done"],
      error: ["失败", "tag err"],
      canceled: ["已取消", "tag"],
    };
    const [statusText, tagCls] = statusMap[j.status] || [j.status, "tag"];
    const opLabel = j.op === "copy" ? "复制" : "移动";
    const canCancel = j.status === "queued" || j.status === "running";
    let progress = "";
    if (j.status === "running") {
      progress = j.bytesTotal
        ? `${pct}% · ${humanSize(j.bytesDone)} / ${humanSize(j.bytesTotal)} · ${j.filesDone}/${j.filesTotal} 文件`
        : `${humanSize(j.bytesDone)} · ${j.filesDone}/${j.filesTotal} 文件`;
      // 预计剩余时间：速率不足以估算（<1KB/s）时提示计算中
      progress += j.etaSeconds != null
        ? ` · 剩余约 ${fmtEta(j.etaSeconds)}`
        : " · 剩余时间计算中…";
    }
    return `
      <div class="job-item job-${j.status}">
        <div class="job-head">
          <div class="job-name" title="${esc(j.name)}"><i class="ic ${j.op === "copy" ? "ic-copy" : "ic-move"}"></i>${esc(j.name)}</div>
          <span class="${tagCls}">${opLabel} · ${statusText}</span>
        </div>
        <div class="job-route">${esc(shortKey(j.srcKey))}<i class="ic ic-arrow-right"></i>${esc(shortKey(j.dstKey))}${j.dstPath ? " / " + esc(j.dstPath) : ""}</div>
        ${j.status === "running" || j.status === "queued" ? `
          <div class="job-track"><div class="job-fill" style="width:${pct}%"></div></div>
          <div class="job-foot">
            <span class="job-progress">${esc(progress || " ")}${j.current ? ` · ${esc(j.current)}` : ""}</span>
            ${canCancel ? `<button class="btn mini danger" data-job-cancel="${esc(j.id)}">取消</button>` : ""}
          </div>` : ""}
        ${j.error ? `<div class="job-error"><i class="ic ic-alert"></i>${esc(j.error)}</div>` : ""}
      </div>`;
  }).join("");
}

function shortKey(key) {
  // local:<名> / remote:<名> 直接取名称；外接 key 截断避免路由行过长
  const m = key.match(/^(local|remote):(.+)$/);
  if (m) return m[2];
  // FC30383E5705D-p1 -> FC30…D-p1
  return key.length > 14 ? `${key.slice(0, 6)}…${key.slice(-4)}` : key;
}

function openJobs(load = true) {
  $("#jobs-modal").hidden = false;
  if (load) loadJobs();
}
function closeJobs() { $("#jobs-modal").hidden = true; }

$("#btn-jobs").addEventListener("click", () => openJobs(true));
$("#jobs-close").addEventListener("click", closeJobs);
$("#jobs-modal").addEventListener("click", (e) => {
  if (e.target === $("#jobs-modal")) closeJobs();
});
$("#jobs-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-job-cancel]");
  if (!btn) return;
  btn.disabled = true;
  try {
    await api("POST", `/api/jobs/${encodeURIComponent(btn.dataset.jobCancel)}/cancel`);
    await loadJobs();
  } catch (err) {
    toast(err.message, "err");
  }
});

// ------------------------------------------------------------ 上传

$("#fs-upload").addEventListener("click", () => $("#fs-file-input").click());

$("#fs-file-input").addEventListener("change", async function onPick() {
  const file = this.files && this.files[0];
  this.value = "";
  if (!file) return;
  await uploadFile(file, 0);
});

async function uploadFile(file, overwrite) {
  const bar = $("#fs-upload-progress");
  const fill = bar.querySelector(".upload-fill");
  const text = bar.querySelector(".upload-text");
  bar.hidden = false;
  fill.style.width = "0%";
  text.textContent = `上传 ${file.name}：0%`;

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
      text.textContent = `上传 ${file.name}：${pct}%`;
    };
    xhr.onload = () => {
      bar.hidden = true;
      let detail = "";
      try { detail = JSON.parse(xhr.responseText).detail || ""; } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        toast(`已上传 ${file.name}`, "ok");
        loadFsDir(fsState.path);
      } else if (xhr.status === 409 && detail.includes("同名") && window.confirm(`「${file.name}」已存在，覆盖它？`)) {
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

// ------------------------------------------------------------ 删除 / 重命名

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

const renameState = { path: "", name: "" };

function openRename(path, name) {
  renameState.path = path;
  renameState.name = name;
  $("#rename-error").hidden = true;
  $("#rename-input").value = name;
  $("#rename-modal").hidden = false;
  $("#rename-input").focus();
  $("#rename-input").select();
}

function closeRename() {
  $("#rename-modal").hidden = true;
}

async function submitRename() {
  const newName = $("#rename-input").value.trim();
  if (!newName) { $("#rename-error").textContent = "名称不能为空"; $("#rename-error").hidden = false; return; }
  if (newName === renameState.name) { closeRename(); return; }
  try {
    await api("POST", `/api/volumes/${encodeURIComponent(fsState.key)}/rename`, {
      path: renameState.path,
      newName,
    });
    toast("已重命名", "ok");
    closeRename();
    loadFsDir(fsState.path);
  } catch (err) {
    $("#rename-error").textContent = err.message;
    $("#rename-error").hidden = false;
  }
}

$("#rename-close").addEventListener("click", closeRename);
$("#rename-cancel").addEventListener("click", closeRename);
$("#rename-ok").addEventListener("click", submitRename);
$("#rename-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitRename();
  if (e.key === "Escape") closeRename();
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#jobs-modal").hidden) closeJobs();
  else if (!$("#move-modal").hidden) closeMove();
  else if (!$("#text-modal").hidden) closeTextPreview();
  else if (!$("#player-modal").hidden) closePlayer();
  else if (!$("#fs-modal").hidden) closeBrowser();
});

boot();
