import { api, download } from "../core/api.js";
import { $, esc, fmtDuration, fmtNum, fmtRate, fmtTime, qs } from "../core/dom.js";
import { copyText, statusCell, toast } from "../core/ui.js";
import { pageHeader } from "../core/page.js";
import { pager, tableEmpty } from "../core/table.js";

const suggestions = {
  auth: "检查渠道 API Key、认证方式和服务商白名单。", rate_limit: "稍后重试，或检查渠道限流策略和候选渠道顺序。", timeout: "检查上游延迟与超时配置，必要时启用备用渠道。", upstream_5xx: "查看上游服务状态，确认备用渠道是否可用。", model_not_found: "确认模型 ID 已在渠道中绑定，或调整模型映射。", content_filter: "调整请求内容或上游安全策略；此类错误不会自动切换渠道。", invalid_request: "检查请求参数、消息格式和工具定义；此类错误不会自动切换渠道。", network: "检查 Base URL、DNS、代理和本机网络连通性。", protocol_error: "HTTP 状态可能为 200，但响应没有有效文本、工具调用或拒答内容；先用 request ID 关联上游日志。", incomplete_output: "上游已返回但输出预算耗尽；调整本次请求的 max_tokens/max_completion_tokens 后重试，不会自动切换渠道。", tool_not_supported: "检查上游是否支持当前工具协议，以及工具定义是否被完整转发。",
};
const usageSourceLabel = { upstream: "上游 usage", estimated: "本地估算", synthetic: "本地模拟", unknown: "来源未知" };

export async function renderLogs(el, { initialFilters = {}, navigate, isCurrent } = {}) {
  if (window.__logTimer) { clearInterval(window.__logTimer); window.__logTimer = null; }
  const [models, channels] = await Promise.all([api("/models"), api("/channels")]);
  if (isCurrent && !isCurrent()) return;
  el.innerHTML = `<div class="content-inner">${pageHeader("请求日志", "区分客户端请求与每一次上游尝试，追踪失败切换链路", `<button class="btn" id="logs-export" type="button">导出日志</button>`)}<div id="logs-alert"></div><div class="toolbar subtle" id="logs-summary" aria-live="polite"></div><section class="card card-pad"><div class="toolbar"><input id="logs-q" placeholder="请求 ID 或模型" aria-label="请求 ID 或模型"><select id="logs-channel" aria-label="渠道"><option value="">全部渠道</option>${channels.map((channel) => `<option value="${channel.id}">${esc(channel.name)}</option>`).join("")}</select><select id="logs-status" aria-label="状态"><option value="">全部状态</option><option value="success">成功</option><option value="error">失败</option></select><select id="logs-code" aria-label="状态码"><option value="">全部状态码</option><option value="200">200</option><option value="429">429</option><option value="5xx">5xx</option></select><select id="logs-category" aria-label="错误分类"><option value="">全部分类</option>${Object.keys(suggestions).map((category) => `<option value="${category}">${category}</option>`).join("")}</select><input id="logs-since" type="date" aria-label="开始日期"><input id="logs-until" type="date" aria-label="结束日期"><button class="btn" id="logs-query" type="button">筛选</button><label class="subtle"><input class="checkbox" id="logs-auto" type="checkbox"> 5 秒刷新</label></div></section><div class="grid logs-layout mt-16"><section class="card log-table-card"><div id="logs-table"></div></section><aside class="detail-panel" id="log-detail"><div class="empty-state"><strong>选择一条请求</strong><p>点击左侧记录查看状态、渠道链路和脱敏上游响应。</p></div></aside></div></div>`;
  $("#logs-q").value = initialFilters.q || ""; $("#logs-channel").value = initialFilters.channel_id || ""; $("#logs-status").value = initialFilters.status || ""; $("#logs-code").value = initialFilters.status_code || ""; $("#logs-category").value = initialFilters.error_category || ""; $("#logs-since").value = initialFilters.since || ""; $("#logs-until").value = initialFilters.until || "";
  const state = { page: 1, pageSize: 12, items: [], selected: null };
  let loadVersion = 0; let detailVersion = 0;
  const active = () => !isCurrent || isCurrent();
  const readFilters = () => ({ q: $("#logs-q").value.trim(), channel_id: $("#logs-channel").value, status: $("#logs-status").value, status_code: $("#logs-code").value, error_category: $("#logs-category").value, since: $("#logs-since").value, until: $("#logs-until").value });
  const clearDetail = () => { detailVersion += 1; state.selected = null; $("#log-detail").innerHTML = `<div class="empty-state"><strong>选择一条请求</strong><p>点击左侧记录查看详情。</p></div>`; };
  const showDetail = async (id) => {
    const version = ++detailVersion; state.selected = id; $("#log-detail").innerHTML = `<div class="empty-state">正在加载详情…</div>`;
    try {
      const detail = await api(`/logs/${id}`); if (version !== detailVersion || !active()) return;
      const item = detail.item;
      $("#log-detail").innerHTML = `<button class="icon-btn detail-close" id="log-detail-close" type="button" aria-label="关闭详情">×</button><h2>请求详情</h2>${statusCell(item)}<div class="detail-kv"><span>请求时间</span><span>${esc(fmtTime(item.created_at))}</span></div><div class="detail-kv"><span>客户端请求</span><span>${fmtNum(detail.request_summary?.requests)} · 上游尝试 ${fmtNum(detail.request_summary?.attempts)} · 重试 ${fmtNum(detail.request_summary?.retries)}</span></div><div class="detail-kv"><span>模型</span><span class="mono">${esc(item.model)}</span></div><div class="detail-kv"><span>渠道</span><span>${esc(item.channel_name || "网关")}</span></div><div class="detail-kv"><span>耗时</span><span>${esc(fmtDuration(item.latency_ms))}</span></div><div class="detail-kv"><span>Tokens</span><span>${fmtNum(item.prompt_tokens)} / ${fmtNum(item.completion_tokens)} · ${usageSourceLabel[item.usage_source] || usageSourceLabel.unknown}</span></div><div class="detail-kv"><span>IP</span><span class="mono">${esc(item.ip || "—")}</span></div><h3 class="mt-16">渠道切换链路</h3><div class="timeline">${(detail.channel_chain || []).map((attempt, index) => `<div class="timeline-item ${attempt.status === "success" ? "success" : "error"}"><strong>${index + 1}. ${esc(attempt.channel_name || "网关")}</strong><div class="timeline-meta"><span>HTTP ${esc(attempt.status_code)}</span><span>${attempt.will_retry ? "已切换" : "结束"}</span></div></div>`).join("")}</div><h3 class="mt-16">上游响应（已脱敏）</h3><pre class="code-box">${esc(detail.upstream_response || "无错误响应")}</pre><h3 class="mt-16">建议操作</h3><p class="form-help">${esc(suggestions[item.error_category] || "先检查上游响应和渠道配置，再决定是否调整路由。")}</p><div class="toolbar mt-16"><button class="btn" id="copy-log-detail" type="button">复制详情</button><button class="btn primary" id="copy-log-curl" type="button">复制为 cURL</button></div>`;
      const requestIdRow = document.createElement("div");
      requestIdRow.className = "detail-kv";
      requestIdRow.innerHTML = '<span>request ID</span><span class="key-value"><code class="mono">' + esc(item.request_id || "—") + '</code><button class="btn sm" id="copy-log-request-id" type="button">复制</button></span>';
      $("#log-detail h2").after(requestIdRow);
      $("#copy-log-request-id").onclick = () => copyText(item.request_id || "", "request ID 已复制");
      $("#log-detail-close").onclick = () => { clearDetail(); load().catch((error) => active() && toast(error, "error")); };
      $("#copy-log-detail").onclick = () => copyText(JSON.stringify(detail, null, 2), "详情已复制");
      $("#copy-log-curl").onclick = () => copyText(`curl ${location.origin}${item.endpoint || "/v1/chat/completions"} -H "Authorization: Bearer $OBSESSION_KEY" -H "Content-Type: application/json" --data-raw '${JSON.stringify({ model: item.model, messages: [] })}'`, "cURL 已复制");
    } catch (error) { if (version === detailVersion && active()) $("#log-detail").innerHTML = `<div class="empty-state"><strong>详情加载失败</strong><p>${esc(error.message)}</p></div>`; }
  };
  const load = async () => {
    const version = ++loadVersion; const filters = readFilters(); const response = await api(`/logs?${qs({ ...filters, page: state.page, page_size: state.pageSize })}`);
    if (version !== loadVersion || !active()) return;
    state.items = response.items || [];
    if (state.selected !== null && !state.items.some((item) => item.id === state.selected)) clearDetail();
    $("#logs-summary").innerHTML = `客户端请求 ${fmtNum(response.client_requests)} · 上游尝试 ${fmtNum(response.attempts)} · 重试 ${fmtNum(response.retries)} · 失败请求 ${fmtNum(response.failed_count)}（失败尝试 ${fmtNum(response.failed_attempts)}） · Token：上游 ${fmtNum(response.upstream_tokens)} / 估算 ${fmtNum(response.estimated_tokens)} / 未知 ${fmtNum(response.unknown_tokens)}`;
    $("#logs-alert").innerHTML = response.failed_count ? `<div class="alert warning"><span>发现 ${fmtNum(response.failed_count)} 个失败客户端请求，建议检查上游响应。</span><button class="btn link" id="logs-only-failed" type="button">仅看失败</button></div>` : "";
    $("#logs-table").innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th>时间</th><th>request ID</th><th>模型</th><th>渠道</th><th>耗时</th><th>状态</th></tr></thead><tbody>${state.items.length ? state.items.map((row) => `<tr class="log-row ${state.selected === row.id ? "selected" : ""}" data-log-row="${row.id}"><td class="subtle">${esc(fmtTime(row.created_at))}</td><td class="mono request-id-cell" title="${esc(row.request_id)}">${esc(row.request_id)}</td><td><strong>${esc(row.model)}</strong></td><td>${esc(row.channel_name || "网关")}</td><td>${esc(fmtDuration(row.latency_ms))}</td><td>${statusCell(row)}</td></tr>`).join("") : tableEmpty(6, "没有匹配记录", "调整筛选条件试试。")}</tbody></table></div>${pager(response.page, response.page_size, response.total)}`;
    $("#logs-table").querySelectorAll("[data-log-row]").forEach((row) => { row.onclick = () => showDetail(Number(row.dataset.logRow)); });
    $("#logs-table").querySelectorAll("[data-page]").forEach((button) => { button.onclick = () => { state.page += button.dataset.page === "next" ? 1 : -1; load().catch((error) => active() && toast(error, "error")); }; });
    $("#logs-only-failed")?.addEventListener("click", () => { $("#logs-status").value = "error"; state.page = 1; load().catch((error) => active() && toast(error, "error")); });
  };
  $("#logs-query").onclick = () => { state.page = 1; load().catch((error) => active() && toast(error, "error")); };
  $("#logs-auto").onchange = (event) => { if (window.__logTimer) clearInterval(window.__logTimer); window.__logTimer = event.target.checked ? setInterval(() => load().catch(() => {}), 5000) : null; };
  $("#logs-export").onclick = async () => { try { await download(`/logs/export?${qs({ ...readFilters(), format: "csv" })}`, "obsessionapi-logs.csv"); } catch (error) { toast(error, "error"); } };
  await load();
  void navigate; void models;
}
