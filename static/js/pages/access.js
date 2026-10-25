import { api } from "../core/api.js";
import { $, esc } from "../core/dom.js";
import { badge, emptyState, toast } from "../core/ui.js";
import { pageHeader } from "../core/page.js";

const META = {
  opencode_free: { label: "OpenCode 免费池", vendor: "opencode.ai", login: "匿名免费额度（按出口 IP 计量）", icon: "OC" },
  lobsterai: { label: "LobsterAI", vendor: "有道", login: "浏览器 OAuth", icon: "LB" },
  gemini: { label: "Gemini", vendor: "Google Code Assist", login: "本地回调 OAuth 免费线", icon: "GE" },
  minimax: { label: "MiniMax Code", vendor: "MiniMax", login: "设备码 + PKCE，每日签到", icon: "MM" },
  cline: { label: "Cline", vendor: "Cline", login: "WorkOS 设备码", icon: "CL" },
  qoder: { label: "Qoder", vendor: "阿里", login: "设备码（骨架）", icon: "QD" },
  trae: { label: "TRAE", vendor: "字节", login: "私有协议（骨架）", icon: "TR" },
};

export async function renderAccess(el, { navigate, initialFilters = {}, isCurrent } = {}) {
  if (isCurrent && !isCurrent()) return;
  el._pageCleanup?.(); el._pageCleanup = null;
  el.innerHTML = `<div class="content-inner">${pageHeader("白嫖模型接入", "登录一次，免费额度直接进网关", "")}<div id="acc-grid" class="mt-16" style="display:grid;gap:16px">${emptyState("加载中…")}</div></div>`;
  try {
    const result = await api("/account-channels");
    const grid = $("#acc-grid");
    grid.innerHTML = result.cards.map((c) => cardHtml(c)).join("");
    grid.querySelectorAll("[data-add-account]").forEach((b) => { b.onclick = () => addAccount(b.dataset.addAccount, Number(b.dataset.channelId || 0)); });
    grid.querySelectorAll("[data-relogin]").forEach((b) => { b.onclick = () => relogin(b, b.dataset.relogin, Number(b.dataset.channelId || 0)); });
    grid.querySelectorAll("[data-refresh-balance]").forEach((b) => { b.onclick = () => refreshBalance(b, b.dataset.refreshBalance); });
    grid.querySelectorAll("[data-signin]").forEach((b) => { b.onclick = () => doSignin(b, b.dataset.signin); });
  } catch (e) { grid.innerHTML = emptyState("加载失败", e.message); }
}

function cardHtml(c) {
  const meta = META[c.family] || { label: c.name, vendor: c.vendor, login: "", icon: "?" };
  const state = c.connected ? (c.enabled ? { label: "已接入", type: "success" } : { label: "已停用", type: "warning" }) : { label: "未接入", type: "" };
  const target = c.channels.length ? c.channels[0].id : 0;
  const actions = [];
  if (c.login === "oauth" || c.login === "device") actions.push(`<button class="btn sm primary" data-add-account="${c.family}" data-channel-id="${target}" type="button">添加账号</button>`);
  if (c.connected && c.login === "oauth") actions.push(`<button class="btn sm" data-relogin="${c.family}" data-channel-id="${target}" type="button">续期</button>`);
  if (c.family === "lobsterai" && c.connected) actions.push(`<button class="btn sm" data-refresh-balance="${target}" type="button">刷新余额</button>`);
  if (c.family === "minimax" && c.connected) actions.push(`<button class="btn sm" data-signin="${target}" type="button">一键领取</button>`);
  return `<article class="card" data-family="${c.family}" style="border-left:3px solid var(--accent)">` +
    `<div class="toolbar"><strong style="font-size:15px">${esc(meta.label)}</strong><span class="subtle">${esc(meta.vendor)}</span><span class="spacer"></span>${badge(state.label, state.type)}</div>` +
    `<p class="subtle" style="margin:8px 0">${esc(meta.login)}</p>` +
    `<p class="subtle">账号 ${c.accounts}/${c.accounts} · 模型 ${c.models} · 剩余额度 <span data-balance-for="${c.family}">—</span></p>` +
    `<div class="toolbar mt-16">${actions.join("")}</div>` +
    `</article>`;
}

async function addAccount(family, channelId) {
  if (channelId) return startLoginFlow(family, channelId);
  const typeMap = { lobsterai: "lobsterai", gemini: "access_gemini", minimax: "access_minimax", cline: "access_cline" };
  const type = typeMap[family];
  if (!type) { toast("该家族暂不支持一键接入", "info"); return; }
  try {
    const created = await api("/channels", { method: "POST", body: JSON.stringify({ name: META[family].label + "-" + Date.now() % 100000, type, api_key: "", models: "" }) });
    if (created.id) { toast("渠道已创建，开始登录", "success"); await startLoginFlow(family, created.id); }
  } catch (e) { toast("创建渠道失败：" + e.message, "err"); }
}

async function startLoginFlow(family, channelId) {
  if (family === "lobsterai") return reloginDirect(channelId);
  try {
    const start = await api("/account-channels/login/start", { method: "POST", body: JSON.stringify({ family, channel_id: channelId }) });
    if (!start.ok) { toast(start.error || "暂不支持", "err"); return; }
    if (start.login_url) window.open(start.login_url, "_blank");
    else if (start.verify_url) { window.open(start.verify_url, "_blank"); toast("设备码：" + (start.user_code || ""), "info"); }
    for (;;) {
      await new Promise((r) => setTimeout(r, 2500));
      let poll;
      try { poll = await api("/account-channels/login/poll?state=" + encodeURIComponent(start.state)); }
      catch { toast("登录会话已结束", "err"); return; }
      if (poll.status === "credential") {
        const bind = await api("/account-channels/login/bind", { method: "POST", body: JSON.stringify({ state: start.state, channel_id: channelId }) });
        toast("登录成功" + (bind.models_synced ? "，已同步 " + bind.models_synced + " 个模型" : ""), "success");
        location.reload();
        return;
      }
      if (poll.status === "error") { toast("登录失败：" + (poll.error || ""), "err"); return; }
    }
  } catch (e) { toast("发起失败：" + e.message, "err"); }
}

async function reloginDirect(channelId) {
  try {
    const start = await api("/channels/lobsterai/login/start", { method: "POST", body: JSON.stringify({ channel_id: channelId }) });
    window.open(start.login_url, "_blank");
    toast("已打开登录页，等待授权…", "info");
  } catch (e) { toast("失败：" + e.message, "err"); }
}

async function relogin(button, family, channelId) {
  if (!channelId) { toast("请先创建渠道", "info"); return; }
  button.disabled = true;
  try {
    const start = await api("/channels/lobsterai/login/start", { method: "POST", body: JSON.stringify({ channel_id: channelId }) });
    window.open(start.login_url, "_blank");
    toast("已打开登录页", "info");
  } catch (e) { toast("失败：" + e.message, "err"); }
  finally { button.disabled = false; }
}

async function refreshBalance(button, channelId) {
  button.disabled = true;
  try {
    const result = await api("/account-channels/balance/" + channelId);
    if (result.ok) {
      const value = result.remaining === null ? "—" : result.remaining + (result.kind === "credits" ? "" : " 分");
      toast("剩余额度：" + value, "success");
    } else { toast("查询失败：" + (result.error || ""), "err"); }
  } catch (e) { toast("失败：" + e.message, "err"); }
  finally { button.disabled = false; }
}

async function doSignin(button, channelId) {
  button.disabled = true; button.textContent = "签到中…";
  try {
    const result = await api("/account-channels/signin/" + channelId, { method: "POST" });
    toast(result.ok ? (result.already ? "今天已签过" : "签到成功") : "签到失败：" + (result.error || ""), result.ok ? "success" : "err");
  } catch (e) { toast("失败：" + e.message, "err"); }
  finally { button.disabled = false; button.textContent = "一键领取"; }
}
