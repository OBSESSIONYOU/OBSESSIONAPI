import { api } from "../core/api.js";
import { $, esc } from "../core/dom.js";
import { badge, emptyState, toast } from "../core/ui.js";
import { pageHeader } from "../core/page.js";

export async function renderAccess(el, { navigate, initialFilters = {}, isCurrent } = {}) {
  if (isCurrent && !isCurrent()) return;
  el._pageCleanup?.(); el._pageCleanup = null;
  el.innerHTML = `<div class="content-inner">
    ${pageHeader("白嫖模型接入", "各软件账号反代：登录一次，免费额度直接进网关", "")}
    <div class="grid three-col mt-16" id="acc-grid">${emptyState("加载中…")}</div>
    <div class="card mt-16"><div class="card-heading"><h2>使用说明</h2></div>
    <p class="subtle">1）先到「渠道」页为对应软件创建渠道（协议选对应类型，保存拿到渠道 ID）→ 2）回到本页点「登录」，授权成功后凭据自动写入渠道 → 3）渠道模型栏填入该家模型 ID 即可调用。</p></div>
  </div>`;
  const grid = $("#acc-grid");
  try {
    const result = await api("/account-channels");
    const cards = result.cards || [];
    grid.innerHTML = cards.length ? cards.map((c) => cardHtml(c)).join("") : emptyState("暂无反代家族");
    grid.querySelectorAll("[data-login-family]").forEach((button) => { button.onclick = () => startLogin(button, button.dataset.loginFamily, Number(button.dataset.channelId || 0)); });
    grid.querySelectorAll("[data-goto-channels]").forEach((button) => { button.onclick = () => navigate("channels"); });
    grid.querySelectorAll("[data-signin]").forEach((button) => {
      button.onclick = async () => {
        button.disabled = true; button.textContent = "签到中…";
        try {
          const result = await api("/account-channels/signin/" + button.dataset.signin, { method: "POST" });
          toast(result.ok ? (result.already ? "今天已签过" : "签到成功") : "签到失败：" + (result.error || ""), result.ok ? "success" : "err");
        } catch (e) { toast("签到失败：" + e.message, "err"); }
        finally { button.disabled = false; button.textContent = "一键签到"; }
      };
    });
  } catch (e) { grid.innerHTML = emptyState("加载失败", e.message); }
}

function cardHtml(c) {
  const target = c.channels.length ? c.channels[0].id : 0;
  return `<article class="card stat-card" style="min-width:0">
    <div class="toolbar"><strong>${esc(c.name)}</strong><span class="spacer"></span>${badge(c.connected ? (c.enabled ? "已接入" : "已停用") : "未接入", c.connected ? (c.enabled ? "success" : "warning") : "")}</div>
    <p class="subtle" style="margin:6px 0">${esc(c.desc || "")}</p>
    <p class="subtle">${esc(c.vendor || "")} · 账号 ${c.accounts} · 模型 ${c.models}</p>
    <div class="toolbar mt-16">
      ${c.login !== "none" ? `<button class="btn sm primary" data-login-family="${c.family}" data-channel-id="${target}" type="button">登录 / 续期</button>` : ""}
      ${c.family === "minimax" && c.channels.length ? `<button class="btn sm" data-signin="${c.channels[0].id}" type="button">一键签到</button>` : ""}
      <span class="spacer"></span><button class="btn sm" data-goto-channels type="button">去建渠道</button>
    </div>
  </article>`;
}

async function startLogin(button, family, channelId) {
  if (!channelId) { toast("请先在渠道页创建该类型渠道并保存", "info"); return; }
  button.disabled = true;
  const original = button.textContent;
  try {
    const start = await api("/account-channels/login/start", { method: "POST", body: JSON.stringify({ family, channel_id: channelId }) });
    if (!start.ok) { toast(start.error || "不支持", "err"); return; }
    if (start.login_url) {
      window.open(start.login_url, "_blank");
      button.textContent = "等待浏览器授权…";
    } else if (start.verify_url) {
      window.open(start.verify_url, "_blank");
      toast("设备码：" + (start.user_code || "") + "，请在打开的页面输入", "info");
      button.textContent = "等待设备确认…";
    }
    const deadline = Date.now() + 620000;
    for (;;) {
      await new Promise((r) => setTimeout(r, 2500));
      let poll;
      try { poll = await api("/account-channels/login/poll?state=" + encodeURIComponent(start.state)); }
      catch { toast("登录会话已结束", "err"); return; }
      if (poll.status === "credential") {
        const bind = await api("/account-channels/login/bind", { method: "POST", body: JSON.stringify({ state: start.state, channel_id: channelId }) });
        toast("登录成功：" + (bind.nickname || "已绑定"), "success");
        return;
      }
      if (poll.status === "error") { toast("登录失败：" + (poll.error || ""), "err"); return; }
      if (Date.now() > deadline) { toast("登录超时", "err"); return; }
    }
  } catch (e) { toast("发起失败：" + e.message, "err"); }
  finally { button.disabled = false; button.textContent = original; }
}
