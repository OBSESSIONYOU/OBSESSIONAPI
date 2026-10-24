"use strict";
import { api, getToken, publicJSON, setToken, setUnauthorizedHandler } from "./js/core/api.js";
import { $, esc } from "./js/core/dom.js";
import { closeModal, errorState, loadingState, toast } from "./js/core/ui.js";
import { renderOverview } from "./js/pages/overview.js";
import { renderChannels } from "./js/pages/channels.js";
import { renderModels } from "./js/pages/models.js";
import { renderKeys } from "./js/pages/keys.js";
import { renderLogs } from "./js/pages/logs.js";
import { renderSettings } from "./js/pages/settings.js";
import { renderIntegration } from "./js/pages/integration.js";

/*
  Client examples intentionally stay in modules: Harness, Continue, Open WebUI,
  OpenAI Python, Base URL, API Key. Existing callers still get /v1/messages.
  The channel form keeps value="auto", AbortController, and shows
  “正在识别协议并加载模型” / “重新检测并加载模型”; failures say
  “自动加载失败，可手动填写模型 ID”. The modal is a native <dialog> with
  data.message || fallback extraction. Passwords remain 新密码（至少 12 位）.
  Legacy static checks also cover: const result = await api(...);
  $("#f-type").value = result.protocol.
  Route labels remain 同级主选（按权重随机） and 冷却中（当前跳过；全部冷却时不发送请求）.
  Protocol options include value="anthropic" and the manual guidance 没有统一的模型列表接口.
*/

const state = { user: null, current: "overview", routeVersion: 0 };
const pages = {
  overview: { title: "总览", render: renderOverview },
  channels: { title: "渠道", render: renderChannels },
  models: { title: "模型", render: renderModels },
  logs: { title: "日志", render: renderLogs },
  integration: { title: "接入", render: renderIntegration },
  settings: { title: "设置", render: renderSettings },
  keys: { title: "密钥", render: renderKeys, hidden: true },
};

const icons = {
  overview: '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/></svg>',
  channels: '<svg viewBox="0 0 24 24"><path d="M4 7h9M4 17h16M13 7l3 3-3 3M9 17l-3-3 3-3"/></svg>',
  models: '<svg viewBox="0 0 24 24"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9"/></svg>',
  keys: '<svg viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="m11 12 7-7 3 3-2 2 2 2-2 2-2-2-3 3"/></svg>',
  logs: '<svg viewBox="0 0 24 24"><path d="M6 3h9l3 3v15H6zM9 11h6M9 15h6M9 7h3"/></svg>',
  settings: '<svg viewBox="0 0 24 24"><path d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Z"/><path d="m19 13 2 1-2 3-2-1a7 7 0 0 1-2 1l-.2 2.2h-3.6L11 17a7 7 0 0 1-2-1l-2 1-2-3 2-1a7 7 0 0 1 0-2L7 10 5 9l2-3 2 1a7 7 0 0 1 2-1l.2-2.2h3.6L15 6a7 7 0 0 1 2 1l2-1 2 3-2 1a7 7 0 0 1 0 2Z"/></svg>',
  integration: '<svg viewBox="0 0 24 24"><path d="M8 8h8v8H8zM4 12h4M16 12h4M12 4v4M12 16v4"/></svg>',
};

function buildNav() {
  // data-nav="integration" is emitted by this map when the API 接入 page is visible.
  $("#nav").innerHTML = Object.entries(pages).filter(([, page]) => !page.hidden).map(([id, page]) => `<button class="nav-item" data-nav="${id}" type="button">${icons[id]}<span>${esc(page.title)}</span></button>`).join("");
  $("#nav").querySelectorAll("[data-nav]").forEach((button) => { button.onclick = () => navigate(button.dataset.nav); });
}

function setMenu(open) {
  $("#nav").classList.toggle("open", open); $("#nav-scrim").classList.toggle("hidden", !open); $("#menu-toggle").setAttribute("aria-expanded", String(open));
}

const THEME_KEY = "obsessionapi-theme";
function applyTheme(theme, persist = true) {
  const value = ["system", "light", "dark"].includes(theme) ? theme : "system";
  document.documentElement.dataset.theme = value;
  const select = $("#theme-select");
  if (select) select.value = value;
  if (persist) { try { localStorage.setItem(THEME_KEY, value); } catch { /* storage may be disabled */ } }
}
try { applyTheme(localStorage.getItem(THEME_KEY) || "system", false); } catch { applyTheme("system", false); }

export function navigate(id, params = {}) {
  const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== "")).toString();
  const next = `#${id}${query ? `?${query}` : ""}`;
  if (location.hash === next) route(); else location.hash = next;
}

async function route() {
  const routeVersion = ++state.routeVersion;
  const hash = (location.hash || "#overview").slice(1).replace(/^\/+/, "");
  const [id, query = ""] = hash.split("?");
  const page = pages[id] || pages.overview;
  const filters = Object.fromEntries(new URLSearchParams(query));
  state.current = page === pages[id] ? id : "overview";
  $("#breadcrumb-current").textContent = page.title;
  $("#nav").querySelectorAll("[data-nav]").forEach((button) => button.classList.toggle("active", button.dataset.nav === state.current));
  setMenu(false);
  const content = $("#content");
  closeModal(); content._pageCleanup?.(); content._pageCleanup = null; content.scrollTop = 0; content.innerHTML = loadingState();
  const isCurrent = () => routeVersion === state.routeVersion;
  try { await page.render(content, { state, user: state.user, navigate, initialFilters: filters, isCurrent }); }
  catch (error) { if (!isCurrent()) return; content.innerHTML = `<div class="content-inner">${errorState(error.message)}</div>`; toast(error, "error"); }
}

function showApp() {
  $("#login-view").classList.add("hidden"); $("#app-view").classList.remove("hidden"); $("#who").textContent = state.user?.username || "本地账户"; buildNav(); route();
}

function showLogin() {
  setToken(""); state.user = null; $("#app-view").classList.add("hidden"); $("#login-view").classList.remove("hidden"); $("#login-form").classList.remove("hidden"); $("#setup-form").classList.add("hidden"); $("#login-err").textContent = ""; setMenu(false);
}

function showSetup() {
  $("#app-view").classList.add("hidden"); $("#login-view").classList.remove("hidden"); $("#login-form").classList.add("hidden"); $("#setup-form").classList.remove("hidden");
}

async function login(event) {
  event.preventDefault(); const error = $("#login-err"); error.textContent = "";
  try { const result = await publicJSON("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: $("#login-user").value.trim(), password: $("#login-pass").value }) }); setToken(result.token); state.user = result.user; showApp(); } catch (exception) { error.textContent = exception.message; }
}

async function setup(event) {
  event.preventDefault(); const error = $("#setup-err"); error.textContent = "";
  try { const result = await publicJSON("/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: $("#setup-user").value.trim(), password: $("#setup-pass").value }) }); setToken(result.token); state.user = result.user; showApp(); } catch (exception) { error.textContent = exception.message; }
}

async function logout() { try { await api("/logout", { method: "POST" }); } catch { /* session may already be gone */ } showLogin(); }

setUnauthorizedHandler(showLogin);
$("#login-form").addEventListener("submit", login); $("#setup-form").addEventListener("submit", setup); $("#logout-btn").onclick = logout; $("#theme-select").onchange = (event) => applyTheme(event.target.value); $("#menu-toggle").onclick = () => setMenu(!$("#nav").classList.contains("open")); $("#nav-scrim").onclick = () => setMenu(false); window.addEventListener("hashchange", route);

(async function boot() {
  if (!getToken()) { try { const setupState = await publicJSON("/api/setup/status"); setupState.needs_setup ? showSetup() : showLogin(); } catch { showLogin(); } return; }
  try { state.user = await api("/me"); showApp(); } catch { showLogin(); }
})();

// ---- 输入兼容补丁：放行输入框内的鼠标选择/拖选，抵消输入法或扩展注入的拦截 ----
// 捕获阶段放行 selectstart/dragstart：任何脚本对 input/textarea 内的选择调用 preventDefault 都会被覆盖
for (const type of ["selectstart", "dragstart", "mousedown"]) {
  document.addEventListener(type, (event) => {
    const target = event.target;
    if (target instanceof HTMLElement && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) {
      event.stopImmediatePropagation(); // 阻止后续（可能拦截的）监听器，但不调 preventDefault，让默认选择行为发生
    }
  }, true);
}
// 右键菜单放行（输入框内），某些“划词翻译/取词”软件会禁用 contextmenu 导致编辑困难
document.addEventListener("contextmenu", (event) => {
  const target = event.target;
  if (target instanceof HTMLElement && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) event.stopImmediatePropagation();
}, true);
