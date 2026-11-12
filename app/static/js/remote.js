/* 远程 SFTP 存储挂载对话框 */
"use strict";

import { $, api, toast } from "./core.js";
import { refreshVolumes } from "./store.js";
import * as overlays from "./overlays.js";

export function init() {
  overlays.register("remote-modal");

  $("#remote-quick-url").addEventListener("input", (e) => {
    const url = e.target.value.trim();
    if (!url) return;
    // sftp://user@host:port/path | sftp://host:port/path | sftp://host/path
    const m = url.match(/^sftp:\/\/(?:(.+?)@)?([^:\/]+)(?::(\d+))?(\/.*)?$/);
    if (!m) return;
    const [, username, host, port, path] = m;
    if (username) $("#remote-username").value = username;
    if (host) $("#remote-host").value = host;
    if (port) $("#remote-port").value = port;
    if (path) $("#remote-path").value = path;
  });

  $("#remote-auth-seg").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-auth]");
    if (!btn) return;
    for (const b of $("#remote-auth-seg").querySelectorAll("button")) b.classList.toggle("active", b === btn);
    const isKey = btn.dataset.auth === "key";
    $("#remote-password-row").hidden = isKey;
    $("#remote-key-row").hidden = !isKey;
  });

  $("#remote-form").addEventListener("submit", onSubmit);
}

export function openRemote() {
  $("#remote-error").hidden = true;
  overlays.open("remote-modal", "#remote-quick-url");
}

async function onSubmit(e) {
  e.preventDefault();
  const errBox = $("#remote-error");
  errBox.hidden = true;
  const authMode = $("#remote-auth-seg button.active")?.dataset.auth || "password";
  const body = {
    group: $("#remote-group").value.trim(),
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
  btn.setAttribute("aria-busy", "true");
  try {
    await api("POST", "/api/remote-stores", body);
    overlays.close("remote-modal");
    $("#remote-form").reset();
    $("#remote-password-row").hidden = false;
    $("#remote-key-row").hidden = true;
    $("#remote-password").value = "";
    $("#remote-key").value = "";
    toast("远程存储已添加，正在挂载…", "ok");
    await refreshVolumes();
  } catch (err) {
    errBox.textContent = err.message;
    errBox.hidden = false;
  } finally {
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
  }
}
