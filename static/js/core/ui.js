import { $, esc } from "./dom.js";

const toastTypes = {
  ok: { name: "success", icon: "✓", duration: 3200 },
  success: { name: "success", icon: "✓", duration: 3200 },
  err: { name: "error", icon: "!", duration: 5600 },
  error: { name: "error", icon: "!", duration: 5600 },
  warning: { name: "warning", icon: "!", duration: 5000 },
  info: { name: "info", icon: "i", duration: 4200 },
};
const internalError = /(?:ReferenceError|TypeError|SyntaxError|RangeError| is not defined|Cannot read properties|\bat\s+\S+\s*\()/i;
const toastText = (value) => {
  const text = String(value?.message ?? value ?? "操作失败").trim();
  return !text || internalError.test(text) ? "操作失败，请重试" : text;
};

export function toast(message, type = "ok") {
  const root = $("#toast-root");
  if (!root) return;
  const style = toastTypes[type] || toastTypes.info;
  const text = toastText(message);
  const duplicate = [...root.children].find((item) => item.dataset.message === text && item.dataset.type === style.name);
  if (duplicate) { duplicate.classList.remove("toast-pulse"); void duplicate.offsetWidth; duplicate.classList.add("toast-pulse"); return; }
  const item = document.createElement("div");
  item.className = `toast ${style.name}`;
  item.dataset.message = text; item.dataset.type = style.name;
  item.setAttribute("role", style.name === "error" ? "alert" : "status");
  item.innerHTML = `<span class="toast-icon" aria-hidden="true">${style.icon}</span><span class="toast-message"></span><button class="toast-close" type="button" aria-label="关闭提示">×</button>`;
  $(".toast-message", item).textContent = text;
  $(".toast-close", item).onclick = () => item.remove();
  root.append(item);
  while (root.children.length > 3) root.firstElementChild.remove();
  window.setTimeout(() => item.remove(), style.duration);
  if (internalError.test(String(message?.message ?? message ?? ""))) console.error(message);
}

export async function copyText(value, message = "已复制到剪贴板") {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(String(value));
    else {
      const input = document.createElement("textarea");
      input.value = String(value); document.body.append(input); input.select(); document.execCommand("copy"); input.remove();
    }
    toast(message);
  } catch { toast("复制失败", "err"); }
}

let activeDialog = null;
let restoreFocus = null;
const focusable = (dialog) => [...dialog.querySelectorAll("button, input, select, textarea, [href], [tabindex]")]
  .filter((node) => !node.disabled && node.tabIndex !== -1 && !node.hidden && !node.closest("[hidden]") && getComputedStyle(node).display !== "none" && getComputedStyle(node).visibility !== "hidden");
export function openModal(title, bodyHTML, onOk, okText = "保存", options = {}) {
  closeModal();
  restoreFocus = document.activeElement;
  const root = $("#modal-root");
  root.innerHTML = `<dialog class="modal-dialog" id="app-dialog" aria-labelledby="modal-title">
    <div class="modal-head"><h2 id="modal-title">${esc(title)}</h2><button class="modal-close" type="button" aria-label="关闭">×</button></div>
    <div class="modal-body">${bodyHTML}</div>
    <div class="modal-foot"><button class="btn" type="button" data-modal-cancel>${options.cancelText || "取消"}</button>${onOk ? `<button class="btn primary" type="button" data-modal-ok>${esc(okText)}</button>` : ""}</div>
  </dialog>`;
  const dialog = $("#app-dialog");
  activeDialog = dialog;
  const cleanup = () => {
    if (activeDialog === dialog) activeDialog = null;
    document.documentElement.classList.remove("modal-open"); document.body.classList.remove("modal-open");
    dialog.remove();
    if (restoreFocus?.isConnected && !restoreFocus.disabled) restoreFocus.focus();
    restoreFocus = null;
  };
  dialog.addEventListener("close", cleanup, { once: true });
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); dialog.close(); return; }
    if (event.key !== "Tab") return;
    const items = focusable(dialog);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  $(".modal-close", dialog).onclick = () => dialog.close();
  $("[data-modal-cancel]", dialog).onclick = () => dialog.close();
  const ok = $("[data-modal-ok]", dialog);
  if (ok) ok.onclick = async () => { ok.disabled = true; try { const result = await onOk(); if (result !== false) dialog.close(); } catch (error) { toast(error, "error"); } finally { ok.disabled = false; } };
  dialog.showModal();
  document.documentElement.classList.add("modal-open"); document.body.classList.add("modal-open");
  const preferred = options.initialFocus ? $(options.initialFocus, dialog) : null;
  const target = preferred || $("[autofocus]", dialog) || focusable(dialog).find((node) => /^(INPUT|SELECT|TEXTAREA)$/.test(node.tagName)) || focusable(dialog).find((node) => node.tagName === "BUTTON");
  target?.focus();
  return dialog;
}
export function closeModal() { if (activeDialog?.open) activeDialog.close(); else { $("#modal-root").innerHTML = ""; activeDialog = null; } }
export function confirmModal(title, bodyHTML, onOk, okText = "确认") { return openModal(title, bodyHTML, onOk, okText); }

export const badge = (text, type = "") => `<span class="badge ${type}"><i class="status-dot ${type || "normal"}"></i>${esc(text)}</span>`;
export const statusBadge = (enabled) => badge(enabled ? "启用" : "停用", enabled ? "success" : "danger");
export const statusCell = (row) => row.error_category === "incomplete_output" ? badge(`${row.status_code || 200} 输出不完整`, "warning") : row.status === "success" ? badge(`${row.status_code || 200} 成功`, "success") : badge(`${row.status_code || "失败"} 失败`, "danger");
export const emptyState = (title = "暂无数据", hint = "完成配置后，这里会出现真实记录。") => `<div class="empty-state" role="status"><strong>${esc(title)}</strong><p>${esc(hint)}</p></div>`;
export const loadingState = () => `<div class="empty-state" aria-busy="true"><span class="skeleton" style="display:inline-block;width:120px"></span></div>`;
export const errorState = (message) => `<div class="empty-state"><strong>加载失败</strong><p>${esc(message)}</p></div>`;
