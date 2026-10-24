import { api } from "../core/api.js";
import { $, esc } from "../core/dom.js";
import { badge, emptyState, toast } from "../core/ui.js";
import { pageHeader } from "../core/page.js";
import { renderChannels } from "./channels.js";
import { renderInsights } from "./insights.js";

const TABS = [[
  ["free-models", "免费模型"],
  ["eac", "EAC 模型"],
  ["access", "白嫖模型接入"],
  ["channels", "渠道管理"],
  ["insights", "数据看板"],
]];

export async function renderFree(el, { navigate, initialFilters = {}, isCurrent } = {}) {
  if (isCurrent && !isCurrent()) return;
  const active = TABS.some(([id]) => id === initialFilters.tab) ? initialFilters.tab : "free-models";
  const refresh = () => renderFree(el, { navigate, initialFilters: { tab: active }, isCurrent });
  el._pageCleanup?.(); el._pageCleanup = null;
  el.innerHTML = `<div class="content-inner">
    ${pageHeader("白嫖", "免费额度聚合：OpenCode 免费池 · EAC 共付 · 账号反代 · 渠道管理", "")}
    <div class="toolbar mb-16" id="free-tabs">${TABS.map(([id, label]) => `<button class="btn sm ${id === active ? "primary" : ""}" data-tab="${id}" type="button">${label}</button>`).join("")}</div>
    <div id="free-body"></div>
  </div>`;
  el.querySelectorAll("[data-tab]").forEach((button) => { button.onclick = () => renderFree(el, { navigate, initialFilters: { tab: button.dataset.tab }, isCurrent }); });
  const body = $("#free-body");
  if (active === "channels") { await renderChannels(body, { navigate, isCurrent }); return; }
  if (active === "access") { body.innerHTML = '<div class="card"><p class="subtle">账号反代已独立为「接入反代」页，请从左侧导航进入。</p></div>'; return; }
  if (active === "eac") { await renderEac(body, refresh); return; }
  if (active === "insights") { await renderInsights(body); return; }
  await renderFreeModels(body, refresh);
}

async function renderFreeModels(body, refresh) {
  body.innerHTML = `<div class="card"><div class="card-heading"><h2>OpenCode 免费池模型</h2><button class="btn sm" id="fm-refresh" type="button">刷新清单</button></div><div id="fm-list">${emptyState("加载中…", "")}</div></div>`;
  const load = async () => {
    const box = $("#fm-list");
    box.innerHTML = emptyState("正在拉取上游清单…");
    try {
      const result = await api("/account-channels/free-models");
      if (!result.ok) { box.innerHTML = emptyState("拉取失败", result.error || "上游不可用"); return; }
      const models = result.models || [];
      box.innerHTML = models.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>#</th><th>模型 ID</th><th>类型</th></tr></thead><tbody>${models.map((m, i) => `<tr><td>${i + 1}</td><td class="mono">${esc(m)}</td><td>${String(m).includes("-free") || ["big-pickle", "union-alpha", "space-bunny-free"].includes(m) ? badge("免费", "success") : badge("未标记", "")}</td></tr>`).join("")}</tbody></table></div>` : emptyState("清单为空");
    } catch (e) { box.innerHTML = emptyState("拉取失败", e.message); }
  };
  $("#fm-refresh").onclick = load;
  await load();
}

async function renderEac(body, refresh) {
  body.innerHTML = `<div class="card"><div class="card-heading"><h2>EAC 共付模型（beichenapi）</h2></div>
    <p class="subtle">EAC 是第三方共付中转（beichenapi.com）：GitHub 授权登录其网站后，把分发的 Base URL 与 API Key 作为 <strong>OpenAI Chat</strong> 渠道添加即可，模型与额度由该服务提供。</p>
    <div class="toolbar mt-16"><a class="btn" href="https://beichenapi.com/register?aff=DL49WAC2DQNG" target="_blank" rel="noreferrer">打开 EAC 官网</a></div>
  </div>`;
}

async function renderAccess(body, refresh) {
  body.innerHTML = `<div class="card"><div class="card-heading"><h2>白嫖模型接入</h2><span class="subtle">登录一次，免费额度直接进网关</span></div><div class="grid three-col mt-16" id="acc-grid">${emptyState("加载中…")}</div></div>`;
  try {
    const result = await api("/account-channels");
    const cards = result.cards || [];
    $("#acc-grid").innerHTML = cards.length ? cards.map((c) => `
      <article class="card stat-card" style="min-width:0">
        <div class="toolbar"><strong>${esc(c.name)}</strong><span class="spacer"></span>${badge(c.connected ? (c.enabled ? "已接入" : "已停用") : "未接入", c.connected ? (c.enabled ? "success" : "warning") : "")}</div>
        <p class="subtle" style="margin:6px 0">${esc(c.desc || "")}</p>
        <p class="subtle">${esc(c.vendor || "")} · 账号 ${c.accounts} · 模型 ${c.models}</p>
        <div class="toolbar mt-16">
          ${c.login === "oauth" && c.channels.length ? `<button class="btn sm" data-lb-login="${c.channels[0].id}" type="button">登录 / 续期</button>` : ""}
          <span class="spacer"></span><button class="btn sm" data-manage type="button">去渠道页</button>
        </div>
      </article>`).join("") : emptyState("暂无账号反代渠道");
    body.querySelectorAll("[data-manage]").forEach((b) => { b.onclick = () => refresh(); });
    body.querySelectorAll("[data-lb-login]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true; b.textContent = "等待授权…";
        try {
          const start = await api("/channels/lobsterai/login/start", { method: "POST", body: JSON.stringify({ channel_id: Number(b.dataset.lbLogin) }) });
          window.open(start.login_url, "_blank");
          for (;;) {
            await new Promise((r) => setTimeout(r, 2500));
            try {
              const poll = await api(`/channels/lobsterai/login/poll?state=${encodeURIComponent(start.state)}`);
              if (poll.status === "done") { toast(`登录成功：${poll.nickname || "已绑定"}`, "success"); break; }
              if (poll.status === "error") { toast(`失败：${poll.error}`, "err"); break; }
            } catch { /* 会话结束 */ break; }
          }
        } catch (e) { toast(`发起失败：${e.message}`, "err"); }
        finally { b.disabled = false; b.textContent = "登录 / 续期"; }
      };
    });
  } catch (e) { $("#acc-grid").innerHTML = emptyState("加载失败", e.message); }
}
