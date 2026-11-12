/* 通用工具与请求封装 */
"use strict";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

export function humanSize(bytes) {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

export function humanRate(bps) {
  if (!bps || bps < 1) return "0 B/s";
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let v = bps, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

export function fmtEta(sec) {
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

export function humanDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function clock(ts) {
  const d = new Date(ts);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const VIDEO_EXT = ["mp4", "m4v", "mkv", "webm", "mov", "avi", "ts", "mpg", "mpeg", "3gp", "flv", "wmv"];
const AUDIO_EXT = ["mp3", "flac", "aac", "m4a", "ogg", "oga", "wav", "opus", "wma"];
const IMAGE_EXT = ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "heif"];
// 与后端 preview 白名单保持一致
const TEXT_EXT = ["txt", "log", "ini", "inf", "conf", "cfg", "config", "properties", "prop",
  "env", "md", "markdown", "json", "xml", "csv", "tsv", "yml", "yaml", "toml",
  "sh", "bash", "zsh", "bat", "cmd", "ps1", "py", "js", "mjs", "ts", "css",
  "scss", "less", "html", "htm", "svg", "c", "h", "cpp", "cc", "hpp", "java",
  "go", "rs", "rb", "php", "pl", "lua", "sql"];

export function extOf(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

export function fileKind(name) {
  const ext = extOf(name);
  if (VIDEO_EXT.includes(ext)) return "video";
  if (AUDIO_EXT.includes(ext)) return "audio";
  if (IMAGE_EXT.includes(ext)) return "image";
  if (TEXT_EXT.includes(ext)) return "text";
  return "other";
}

export function toast(msg, tone = "") {
  const el = document.createElement("div");
  el.className = "toast";
  if (tone) el.dataset.tone = tone;
  const icon = tone === "ok" ? "ic-check"
    : tone === "err" ? "ic-alert"
    : tone === "busy" ? "ic-loader ic-spin"
    : "";
  el.innerHTML = `${icon ? `<i class="ic ${icon}"></i>` : ""}<span>${esc(msg)}</span>`;
  $("#toast-container").appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

export async function api(method, url, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  let data = null;
  try { data = await resp.json(); } catch { /* 无响应体 */ }
  if (!resp.ok) throw new Error(data?.detail || `请求失败（HTTP ${resp.status}）`);
  return data;
}

export const encPath = (p) => encodeURIComponent(p || "");

export function rawUrl(key, path, download) {
  return `/api/volumes/${encodeURIComponent(key)}/raw?path=${encPath(path)}${download ? "&download=1" : ""}`;
}

export async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(`已复制${label}`, "ok");
}

export { VIDEO_EXT, AUDIO_EXT, IMAGE_EXT, TEXT_EXT };
