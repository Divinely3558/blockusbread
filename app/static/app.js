/* BlockUSBRead 管理页：磁盘列表、解锁/挂载/弹出，状态由 SSE 实时驱动。 */
"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  username: null,
  disks: [],
  rememberEnabled: false,
  share: null,
  evtSource: null,
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
    <div class="banner-title"><i class="ic ic-network"></i>SMB 共享已就绪（账号同网页登录）</div>
    <div class="banner-line">
      <span class="banner-label">Windows：</span>
      <code>${esc(s.unc)}</code>
      <button class="btn mini js-copy" data-copy="${esc(s.unc)}" data-label="Windows 路径">复制</button>
    </div>
    <div class="banner-line">
      <span class="banner-label">macOS / Linux / 手机播放器（VLC 等）：</span>
      <code>${esc(s.uri)}</code>
      <button class="btn mini js-copy" data-copy="${esc(s.uri)}" data-label="SMB 地址">复制</button>
    </div>
    <div class="banner-line">
      <span class="banner-label">账号：</span>
      <code>${esc(s.username)}</code>
      <button class="btn mini js-copy" data-copy="${esc(s.username)}" data-label="账号">复制</button>
      <span class="banner-label" style="margin-left:10px">密码：</span>
      <code class="pw-mask" title="密码已隐藏，点右侧按钮可复制">${"•".repeat(Math.max(8, String(s.password).length))}</code>
      <button class="btn mini js-copy" data-copy="${esc(s.password)}" data-label="密码">复制密码</button>
    </div>
    <div class="banner-sub">挂载后的每个卷在共享内对应 &lt;磁盘ID&gt;/part&lt;序号&gt;/fs 目录；密码即网页登录密码，页面不显示明文</div>`;
  banner.hidden = false;
}

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
  ["state", "unlocking", "mounting", "unmounting"].forEach((name) =>
    es.addEventListener(name, refreshDisks)
  );
  es.addEventListener("ejected", async () => {
    await refreshDisks();
    toast("已安全弹出，可以拔除硬盘", "ok");
  });
  es.addEventListener("removed", async () => {
    await refreshDisks();
    toast("硬盘已拔出，挂载已自动清理");
  });
  es.addEventListener("forgot", refreshDisks);
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
    const snap = await api("GET", "/api/disks");
    // 数据无变化不重绘：避免卡片闪烁，也避免清空用户正在输入的解锁密码
    const sig = JSON.stringify(snap);
    if (sig === state.lastSig) return;
    state.lastSig = sig;
    state.disks = snap.disks || [];
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
  const list = $("#disk-list");
  if (!state.disks.length) {
    list.innerHTML = `
      <div class="empty">
        <div class="big"><i class="ic ic-empty"></i></div>
        <div>未检测到 USB 硬盘</div>
        <div style="margin-top:6px;font-size:12px">插入 BitLocker 或普通外接硬盘后，列表会自动出现</div>
      </div>`;
    return;
  }
  list.innerHTML = state.disks.map(renderDisk).join("");
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
      ${disk.partitions.length
        ? disk.partitions.map(renderPartition).join("")
        : `<div class="part-row"><div class="part-meta">该磁盘没有可识别的分区</div></div>`}
    </div>`;
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
    const unc = smbPath(p, "unc");
    return `
      <span class="tag ${p.mode === "rw" ? "rw" : "ro"}">
        ${p.mode === "rw" ? "读写模式" : "只读模式"}
      </span>
      <button class="btn primary js-copy" data-copy="${esc(unc)}" data-label="卷共享路径">
        <i class="ic ic-copy"></i>复制 SMB 路径
      </button>
      <button class="btn danger" onclick="ejectVolume('${esc(p.key)}')"><i class="ic ic-eject"></i>安全弹出</button>
      ${p.remembered
        ? `<button class="btn" onclick="forgetCredential('${esc(p.key)}')">忘记凭据</button>`
        : ""}
      <div class="smb-line"><i class="ic ic-folder"></i><code>${esc(unc)}</code></div>`;
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
          <label class="checkline">
            <input type="checkbox" class="remember-check">
            记住此卷（凭据加密保存，插入/重启后自动解锁）
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

function smbPath(p, form) {
  const sub = `${p.diskId}/part${p.number}/fs`;
  const s = state.share;
  const host = s ? s.host : location.hostname;
  const share = s ? s.share : "usb";
  return form === "uri"
    ? `smb://${host}/${share}/${sub}`
    : `\\\\${host}\\${share}\\${sub.replaceAll("/", "\\")}`;
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

boot();
