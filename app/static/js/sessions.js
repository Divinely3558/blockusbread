/* SFTP 会话视图：连接数、打开中的文件 */
"use strict";

import { $, esc } from "./core.js";
import { state, on } from "./store.js";
import { setBadge } from "./nav.js";

export function init() {
  on("live", render);
}

export function render() {
  const body = $("#sessions-body");
  if (!body) return;
  const info = state.sessions;
  const files = (info && info.openFiles) || [];
  setBadge("sessions", info ? info.connections : 0, "accent");

  if (!info || !info.connections) {
    body.innerHTML = `<div class="empty"><div class="big"><i class="ic ic-activity"></i></div>
      <p>当前没有 SFTP 连接</p>
      <p class="sub">用 WinSCP / FileZilla / VLC 或 sftp 命令连上本机后，这里会实时列出正在读写的文件</p></div>`;
    return;
  }
  const users = (info.users || []).join(" · ") || "—";
  const rows = files.map((f) => {
    const name = f.path ? f.path.split("/").pop() : "/";
    return `<tr class="frow">
      <td class="c-verb"><span class="verb ${f.writable ? "write" : "read"}">${f.writable ? "写入" : "读取"}</span></td>
      <td class="c-name"><div class="cell-name"><i class="ic ic-file"></i>
        <span class="txt" title="${esc(f.path)}">${esc(name)}</span></div>
        <span class="cell-sub">${esc(f.path)}</span></td>
      <td class="c-vol"><span class="sess-vol">${esc(f.volumeName || f.volume || "—")}</span></td>
      <td class="c-user"><span class="f-meta">${esc(f.user || "—")}</span></td>
    </tr>`;
  }).join("");

  body.innerHTML = `
    <div class="callout">
      <span class="lead"><i class="ic ic-activity"></i>${info.connections} 个连接</span>
      <span class="row"><span class="lbl">登录用户</span><code>${esc(users)}</code></span>
      <span class="row"><span class="lbl">打开中的文件</span><code>${files.length}</code></span>
    </div>
    ${files.length ? `<div class="storage-wrap"><table class="table sess">
        <thead><tr><th scope="col" class="c-verb">动作</th><th scope="col" class="c-name">文件</th>
          <th scope="col" class="c-vol">所在卷</th><th scope="col" class="c-user">用户</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
      : `<div class="empty"><p>已连接，暂无打开的文件</p>
         <p class="sub">空闲连接不影响弹出硬盘</p></div>`}`;
}
