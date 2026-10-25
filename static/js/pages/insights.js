import { api } from "../core/api.js";
import { $, esc } from "../core/dom.js";
import { emptyState } from "../core/ui.js";

export async function renderInsights(body) {
  body.innerHTML = `<div class="card"><div class="card-heading"><h2>Data Insights</h2><span class="subtle">30-day pivot by channel and model</span></div><div id="ins-body">${emptyState("loading")}</div></div>`;
  try {
    const result = await api("/account-channels/insights?days=30");
    const t = result.total || {};
    const fmt = (n) => Number(n || 0).toLocaleString();
    const okRate = t.requests ? Math.round(100 * (t.ok || 0) / t.requests) : 0;
    const kpi = (label, value) => `<article class="card stat-card" style="min-width:0"><span class="subtle">${label}</span><strong style="font-size:22px">${value}</strong></article>`;
    const row = (r, nameKey) => `<tr><td class="mono">${esc(r[nameKey] || "")}</td><td>${fmt(r.requests)}</td><td>${r.requests ? Math.round(100 * r.ok / r.requests) : 0}%</td><td>${fmt(r.pt)}</td><td>${fmt(r.ct)}</td><td>${fmt(Math.round(r.avg_ms || 0))}ms</td></tr>`;
    const head = (first) => `<div class="table-wrap"><table class="data-table"><thead><tr><th>${first}</th><th>req</th><th>ok%</th><th>in-tok</th><th>out-tok</th><th>latency</th></tr></thead><tbody>`;
    const chTable = (result.by_channel || []).length ? head("channel") + result.by_channel.map((r) => row(r, "channel_name")).join("") + "</tbody></table></div>" : emptyState("no data");
    const dayRows = result.by_day || [];
    const dayTable = dayRows.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>day</th><th>requests</th><th>ok%</th><th>tokens</th></tr></thead><tbody>` + dayRows.map((r) => `<tr><td class="mono">${esc(r.day || "")}</td><td>${fmt(r.requests)}</td><td>${r.requests ? Math.round(100 * r.ok / r.requests) : 0}%</td><td>${fmt(r.tokens)}</td></tr>`).join("") + "</tbody></table></div>" : emptyState("no data");
    const dayCard = `<div class="card mt-16"><div class="card-heading"><h2>by day</h2></div>${dayTable}</div>`;
    const mTable = (result.by_model || []).length ? head("model") + result.by_model.map((r) => row(r, "model")).join("") + "</tbody></table></div>" : emptyState("no data");
    $("#ins-body").innerHTML = `<div class="grid metrics-grid">${kpi("requests", fmt(t.requests))}${kpi("success", okRate + "%")}${kpi("input tok", fmt(t.pt))}${kpi("output tok", fmt(t.ct))}</div>` + `<div class="card mt-16"><div class="card-heading"><h2>by channel</h2></div>${chTable}</div>` + `<div class="card mt-16"><div class="card-heading"><h2>by model</h2></div>${mTable}</div>` + dayCard;
  } catch (e) { $("#ins-body").innerHTML = emptyState("failed", e.message); }
}
