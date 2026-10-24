import { api } from "../core/api.js";
import { $, esc, fmtDuration, fmtNum, fmtRate, qs } from "../core/dom.js";
import { drawCombo, bindTooltip, observeCanvas } from "../core/charts.js";
import { badge, copyText, emptyState } from "../core/ui.js";
import { cardHeading, pageHeader, statusStrip } from "../core/page.js";

const statusLabel = { normal: "正常", warning: "需要关注", error: "异常", cooling: "冷却中", disabled: "已自动禁用" };
const statusType = (status) => status === "normal" ? "success" : status === "warning" || status === "cooling" ? "warning" : "danger";

export async function renderOverview(el, { navigate, initialFilters = {}, isCurrent } = {}) {
  if (isCurrent && !isCurrent()) return;
  const range = initialFilters.range === "30d" ? "30d" : "7d";
  const updatedAt = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date());
  const refresh = () => renderOverview(el, { navigate, initialFilters: { range }, isCurrent });
  el._pageCleanup?.(); el._pageCleanup = null;
  el.innerHTML = `<div class="content-inner"><div id="overview-root">${pageHeader("总览", "网关运行状态与请求概览 · 最近更新 " + updatedAt, `<select id="overview-range" class="range-select" aria-label="统计范围"><option value="7d">最近 7 天</option><option value="30d">最近 30 天</option></select><button class="btn" id="overview-refresh" type="button">刷新</button>`)}</div><div class="empty-state"><span class="skeleton" style="display:inline-block;width:180px"></span></div></div>`;
  $("#overview-range").value = range;
  $("#overview-range").onchange = (event) => navigate("overview", { range: event.target.value });
  $("#overview-refresh").onclick = refresh;
  const [dashboard, gateway] = await Promise.all([api(`/dashboard?${qs({ range })}`), api("/gateway")]);
  if (isCurrent && !isCurrent()) return;
  const metrics = dashboard.metrics || {};
  const strip = dashboard.status_strip || {};
  const autoDisabled = strip.auto_disabled_channels || 0;
  const healthy = strip.cooling_channels || autoDisabled ? "warning" : "success";
  const attention = dashboard.attention || [];
  const channels = dashboard.channel_health || [];
  const modelHealth = dashboard.model_health || [];
  $("#overview-root").innerHTML = `${pageHeader("总览", "网关运行状态与请求概览 · 最近更新 " + updatedAt, `<select id="overview-range" class="range-select" aria-label="统计范围"><option value="7d">最近 7 天</option><option value="30d">最近 30 天</option></select><button class="btn" id="overview-refresh" type="button">刷新</button>`)}
    ${statusStrip([
      { label: "服务", value: strip.service === "normal" ? "正常" : "异常", type: healthy },
      { label: "启用渠道", value: `${strip.active_channels || 0}/${strip.total_channels || 0}`, type: "success" },
      { label: "冷却渠道", value: strip.cooling_channels || 0, type: strip.cooling_channels ? "warning" : "normal" },
      ...(autoDisabled ? [{ label: "自动禁用", value: autoDisabled, type: "danger" }] : []),
      { label: "上架模型", value: strip.published_models || 0, type: "success" },
    ])}
    <div class="grid metrics-grid">
      <article class="card stat-card"><span class="stat-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M4 19h16M6.5 15l3.5-3.5 2.5 2L18 7"/></svg></span><span class="stat-label">今日客户端请求</span><strong class="stat-value">${fmtNum(metrics.today_requests)}</strong><span class="stat-foot">上游尝试 ${fmtNum(metrics.today_attempts)} · 重试 ${fmtNum(metrics.today_retries)} · 按 UTC 日期统计</span></article>
      <article class="card stat-card"><span class="stat-icon ${metrics.success_rate !== null && metrics.success_rate < 90 ? "warning" : "success"}" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M20 6.5 9.5 17.5 4 12"/></svg></span><span class="stat-label">成功率</span><strong class="stat-value">${fmtRate(metrics.success_rate)}</strong><span class="stat-foot ${metrics.success_rate !== null && metrics.success_rate < 90 ? "warning" : ""}">${metrics.success_rate !== null && metrics.success_rate < 90 ? "需要关注" : "当前请求结果"}</span></article>
      <article class="card stat-card"><span class="stat-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3.2 2"/></svg></span><span class="stat-label">平均延迟</span><strong class="stat-value">${fmtDuration(metrics.avg_latency_ms)}</strong><span class="stat-foot">P95 ${fmtDuration(metrics.p95_latency_ms)} · P99 ${fmtDuration(metrics.p99_latency_ms)} · 峰值 ${fmtDuration(metrics.max_latency_ms)}</span></article>
      <article class="card stat-card"><span class="stat-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/></svg></span><span class="stat-label">今日 Token</span><strong class="stat-value">${fmtNum(metrics.today_tokens)}</strong><span class="stat-foot">上游 ${fmtNum(metrics.today_upstream_tokens)} · 估算 ${fmtNum(metrics.today_estimated_tokens)} · 模拟 ${fmtNum(metrics.today_synthetic_tokens)} · 未知 ${fmtNum(metrics.today_unknown_tokens)}</span></article>
    </div>
    <div class="grid two-col mt-16">
      <section class="card chart-card"><div class="card-heading"><h2>用量趋势</h2><div class="legend"><span><i></i>客户端请求</span><span><i class="line"></i>Token</span><div class="chart-switch" aria-label="图表指标"><button class="active" data-chart-mode="both" type="button">全部</button><button data-chart-mode="requests" type="button">请求</button><button data-chart-mode="tokens" type="button">Token</button></div></div></div><div class="chart-body" id="overview-chart-body"><canvas id="overview-chart" aria-label="按日客户端请求和 Token 趋势"></canvas><div class="chart-tooltip" id="overview-tooltip"></div></div></section>
      <section class="card"><div class="card-heading"><h2>待处理问题</h2><span class="subtle">失败 Top 5</span></div><div class="issue-list" id="overview-issues"></div></section>
    </div>
    <section class="card mt-16"><div class="integration-bar"><strong>API 接入</strong><code class="mono" id="overview-base-url">${esc(`${location.origin}/v1`)}</code><button class="btn primary" id="overview-copy-base" type="button">复制</button></div></section>
    <section class="card mt-16"><div class="card-heading"><h2>渠道健康</h2><button class="btn link" id="overview-channels" type="button">查看全部</button></div><div class="table-wrap"><table class="data-table"><thead><tr><th>渠道</th><th>上游尝试</th><th>成功率</th><th>平均延迟</th><th>状态</th></tr></thead><tbody id="overview-channel-rows"></tbody></table></div></section>
    <section class="card mt-16"><div class="card-heading"><h2>模型健康</h2><span class="subtle">按客户端请求统计</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>模型</th><th>客户端请求</th><th>上游尝试</th><th>成功率</th><th>Token</th><th>平均延迟</th></tr></thead><tbody id="overview-model-rows"></tbody></table></div></section>
  </div>`;
  $("#overview-range").value = range;
  $("#overview-range").onchange = (event) => navigate("overview", { range: event.target.value });
  $("#overview-refresh").onclick = refresh;
  $("#overview-copy-base").onclick = () => copyText(`${location.origin}/v1`, "Base URL 已复制");
  $("#overview-channels").onclick = () => navigate("channels");
  const series = dashboard.series || [];
  const chartBody = $("#overview-chart-body");
  if (!series.some((point) => point.requests || point.tokens)) {
    chartBody.innerHTML = emptyState("暂无请求数据", "完成一次真实调用后，这里会显示每日请求与 Token。 ");
  } else {
    const chart = $("#overview-chart"); const tooltip = $("#overview-tooltip");
    const draw = (mode = "both") => drawCombo(chart, series, mode);
    const cleanup = observeCanvas(chart, () => draw("both")); el._pageCleanup = cleanup;
    bindTooltip(chart, tooltip, (point) => `${esc(point.date)}<br>客户端请求 ${fmtNum(point.requests)}<br>Token ${fmtNum(point.tokens)}`);
    el.querySelectorAll("[data-chart-mode]").forEach((button) => { button.onclick = () => { el.querySelectorAll("[data-chart-mode]").forEach((item) => item.classList.remove("active")); button.classList.add("active"); draw(button.dataset.chartMode); }; });
  }
  $("#overview-issues").innerHTML = attention.length ? attention.map((item) => `<div class="issue-row"><div class="issue-main"><strong>${esc(item.model || "未知模型")}</strong><small>${esc(item.channel_name || "未知渠道")} · ${fmtNum(item.failures)} 个失败客户端请求 · ${fmtNum(item.attempts)} 次尝试</small></div><button class="btn link" data-issue='${esc(JSON.stringify(item.jump || {}))}' type="button">查看日志</button></div>`).join("") : emptyState("暂无待处理问题", "当前统计范围内没有失败客户端请求。");
  $("#overview-issues").querySelectorAll("[data-issue]").forEach((button) => { button.onclick = () => { const filter = JSON.parse(button.dataset.issue); navigate("logs", filter); }; });
  $("#overview-channel-rows").innerHTML = channels.length ? channels.map((channel) => `<tr><td><strong>${esc(channel.name)}</strong><div class="subtle">${fmtNum(channel.client_requests)} 个客户端请求${channel.auto_disabled ? ` · ${esc(channel.disabled_reason || "已自动禁用")}` : ""}</div></td><td>${fmtNum(channel.attempts ?? channel.requests)}<div class="subtle">重试 ${fmtNum(channel.retries)}</div></td><td>${fmtRate(channel.success_rate)}</td><td>${fmtDuration(channel.avg_latency_ms)}</td><td>${badge(statusLabel[channel.status] || "未知", statusType(channel.status))}</td></tr>`).join("") : `<tr><td colspan="5" style="padding:0">${emptyState("还没有渠道", "先在渠道页添加一个真实上游或本地 mock 渠道。")}</td></tr>`;
  $("#overview-model-rows").innerHTML = modelHealth.length ? modelHealth.map((model) => `<tr><td><strong>${esc(model.model)}</strong></td><td>${fmtNum(model.requests)}</td><td>${fmtNum(model.attempts)}</td><td>${fmtRate(model.success_rate)}</td><td>${fmtNum(model.tokens)}</td><td>${fmtDuration(model.avg_latency_ms)}</td></tr>`).join("") : `<tr><td colspan="6" style="padding:0">${emptyState("暂无模型用量", "完成一次真实调用后，这里会显示按模型的成功率与延迟。")}</td></tr>`;
  void gateway;
}
