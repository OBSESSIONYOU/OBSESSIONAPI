import { api } from "../core/api.js";
import { $, esc, fmtDuration, fmtNum, fmtRate, fmtTime, qs } from "../core/dom.js";
import { badge, confirmModal, copyText, emptyState, openModal, toast } from "../core/ui.js";
import { drawCombo, bindTooltip, observeCanvas } from "../core/charts.js";
import { pageHeader, statusStrip } from "../core/page.js";

const typeLabel = { auto: "自动", openai: "OpenAI", openai_chat: "OpenAI Chat", openai_responses: "Responses", anthropic: "Anthropic", gemini: "Gemini", mock: "本地 mock" };
const statusLabel = { unknown: "未知", normal: "正常", warning: "需要关注", error: "异常", cooling: "冷却中", disabled: "已自动禁用" };
const statusType = (status) => status === "normal" ? "success" : status === "warning" || status === "cooling" ? "warning" : status === "error" || status === "disabled" ? "danger" : "info";

export async function renderChannels(el, { navigate, initialFilters = {} } = {}) {
  const range = initialFilters.range === "30d" ? "30d" : "7d";
  const filters = { range, q: initialFilters.q || "", status: initialFilters.status || "" };
  const refresh = () => renderChannels(el, { navigate, initialFilters: filters });
  el._pageCleanup?.(); el._pageCleanup = null;
  const [channels, overview, dashboard, settings] = await Promise.all([
    api("/channels"), api(`/channels/overview?${qs({ range })}`), api(`/dashboard?${qs({ range })}`), api("/settings"),
  ]);
  el.innerHTML = `<div class="content-inner">${pageHeader("渠道监控", "请求分配与上游可用性", `<select id="channel-range" class="range-select" aria-label="统计范围"><option value="7d">最近 7 天</option><option value="30d">最近 30 天</option></select><button class="btn primary" id="channel-add" type="button">添加渠道</button>`)}
    ${statusStrip([{ label: "启用渠道", value: `${overview.items.filter((item) => item.status_enabled).length}/${channels.length}`, type: "success" }, { label: "冷却渠道", value: overview.items.filter((item) => item.cooling).length, type: overview.items.some((item) => item.cooling) ? "warning" : "normal" }, { label: "自动重试", value: `最多尝试 ${settings.retry_max_attempts} 个渠道`, type: settings.retry_max_attempts > 1 ? "success" : "warning" }])}
    <div class="grid two-col"><section class="card chart-card"><div class="card-heading"><h2>用量趋势</h2><div class="legend"><span><i></i>客户端请求</span><span><i class="line"></i>Token</span></div></div><div class="chart-body" id="channel-chart-body"><canvas id="channel-chart" aria-label="按日客户端请求和 Token 趋势"></canvas><div class="chart-tooltip" id="channel-tooltip"></div></div></section><section class="card"><div class="card-heading"><h2>请求分配</h2><span class="subtle">按最近实际请求统计；分配按上游尝试，重试单独计数</span></div><div class="progress-list" id="channel-allocation"></div></section></div>
    <section class="card mt-16"><div class="card-heading"><h2>渠道健康</h2><div class="toolbar" style="margin:0"><input id="channel-search" aria-label="搜索渠道" placeholder="搜索渠道" style="width:170px"><select id="channel-filter" aria-label="筛选渠道"><option value="">全部状态</option><option value="unknown">未知</option><option value="normal">正常</option><option value="warning">需要关注</option><option value="error">异常</option><option value="cooling">冷却中</option><option value="disabled">已自动禁用</option></select><button class="btn sm" id="channel-batch-probe" type="button" title="对选中渠道执行可用性探测">批量探测</button><button class="btn sm" id="channel-batch-enable" type="button">批量启用</button><button class="btn sm" id="channel-batch-disable" type="button">批量停用</button><button class="btn sm" id="channel-batch-recover" type="button" title="清除选中渠道的冷却与自动禁用">批量恢复</button></div></div><div class="table-wrap"><table class="data-table"><thead><tr><th style="width:30px"></th><th>渠道</th><th>协议</th><th>成功率</th><th>平均延迟</th><th>探测</th><th>状态</th><th style="text-align:right">操作</th></tr></thead><tbody id="channel-rows"></tbody></table></div></section></div>`;
  $("#channel-range").value = range;
  $("#channel-search").value = filters.q; $("#channel-filter").value = filters.status;
  $("#channel-add").onclick = () => channelForm(null, refresh);
  $("#channel-range").onchange = (event) => navigate("channels", { range: event.target.value });
  const chartBody = $("#channel-chart-body"); const series = dashboard.series || [];
  if (!series.some((point) => point.requests || point.tokens)) chartBody.innerHTML = emptyState("暂无请求数据", "完成一次真实调用后，这里会显示每日请求与 Token。");
  else {
    const chart = $("#channel-chart"); const cleanup = observeCanvas(chart, () => drawCombo(chart, series, "both"));
    bindTooltip(chart, $("#channel-tooltip"), (point) => `${esc(point.date)}<br>请求 ${fmtNum(point.requests)}<br>Token ${fmtNum(point.tokens)}`);
    el._pageCleanup = cleanup;
  }
  const total = overview.items.reduce((sum, item) => sum + Number(item.attempts ?? item.requests ?? 0), 0);
  $("#channel-allocation").innerHTML = total ? overview.items.map((item) => { const attempts = Number(item.attempts ?? item.requests ?? 0); const share = Math.round(attempts * 100 / total); return `<div class="progress-row"><div class="progress-label"><span>${esc(item.name)} <small class="subtle">${fmtNum(item.client_requests)} 请求 · ${fmtNum(attempts)} 尝试</small></span><span>${share}%</span></div><div class="progress"><span style="width:${Math.min(100, share)}%"></span></div></div>`; }).join("") : (overview.items.length ? emptyState("暂无请求分配", "当前范围没有实际请求；完成调用后显示各渠道上游尝试占比。") : emptyState("暂无渠道"));
  const rows = $("#channel-rows");
  const draw = () => {
    filters.q = $("#channel-search").value.trim(); filters.status = $("#channel-filter").value;
    const keyword = filters.q.toLowerCase(); const filter = filters.status;
    const data = overview.items.filter((item) => (!keyword || item.name.toLowerCase().includes(keyword)) && (!filter || item.status === filter));
    rows.innerHTML = data.length ? data.map((item) => `<tr><td><input class="checkbox" type="checkbox" data-channel-select="${item.id}" aria-label="选择 ${esc(item.name)}"></td><td><strong>${esc(item.name)}</strong><div class="subtle mono">优先级 ${item.priority} · ${fmtNum(item.client_requests)} 个客户端请求 · ${fmtNum(item.attempts ?? item.requests)} 次上游尝试 · 重试 ${fmtNum(item.retries)}</div></td><td>${badge(typeLabel[item.type] || item.type, "info")}</td><td>${fmtRate(item.success_rate)}<div class="subtle">按上游尝试</div></td><td>${fmtDuration(item.avg_latency_ms)}</td><td>${item.probe ? badge(`${item.probe.ok ? "通过" : item.probe.error_category === "model_list_unavailable" ? "模型列表不可用" : "失败"} · ${fmtTime(item.probe.created_at)}`, item.probe.ok || item.probe.error_category === "model_list_unavailable" ? "info" : "danger") : badge("未探测", "")}</td><td>${badge(statusLabel[item.status] || "未知", statusType(item.status))}${item.cooling ? `<div class="subtle">失败 ${fmtNum(item.consecutive_failures)} 次 · 冷却 ${fmtNum(item.cooldown_remaining_s)}s</div>` : ""}${item.auto_disabled ? `<div class="subtle">${esc(item.disabled_reason || "已自动禁用")} · ${item.recover_in_s ? `约 ${fmtNum(item.recover_in_s)}s 后自动恢复` : "等待手动恢复"}</div>` : ""}</td><td><div class="table-actions"><button class="btn sm primary" data-action="test" data-id="${item.id}" type="button" title="发送一次最小真实生成请求">测试</button><button class="btn sm" data-action="edit" data-id="${item.id}" type="button">编辑</button><button class="btn sm" data-action="toggle" data-id="${item.id}" type="button">${item.status_enabled ? "停用" : "启用"}</button>${item.auto_disabled ? `<button class="btn sm" data-action="recover" data-id="${item.id}" type="button">恢复</button>` : ""}<button class="btn sm" data-action="logs" data-id="${item.id}" type="button">日志</button><button class="btn sm" data-action="probe" data-id="${item.id}" type="button" title="仅检查渠道可用性，不发送生成请求">探测</button><button class="btn sm danger" data-action="delete" data-id="${item.id}" type="button">删除</button></div></td></tr>`).join("") : `<tr><td colspan="8" style="padding:0">${emptyState("没有匹配的渠道", "调整搜索或状态筛选试试。")}</td></tr>`;
    rows.querySelectorAll("[data-action]").forEach((button) => { button.onclick = async () => {
      const item = data.find((row) => row.id === Number(button.dataset.id)); const original = channels.find((row) => row.id === item?.id); if (!item || !original) return;
      try {
        if (button.dataset.action === "test") return channelTestPanel(original);
        if (button.dataset.action === "edit") return channelForm(original, refresh);
        if (button.dataset.action === "logs") return navigate("logs", { channel_id: item.id });
        if (button.dataset.action === "probe") { await api(`/channels/${item.id}/probe`, { method: "POST" }); toast("探测完成", "success"); return refresh(); }
        if (button.dataset.action === "recover") { await api(`/channels/${item.id}/recover`, { method: "POST" }); toast("渠道已恢复", "success"); return refresh(); }
        if (button.dataset.action === "toggle") { await api(`/channels/${item.id}`, { method: "PUT", body: JSON.stringify({ status: original.status ? 0 : 1 }) }); toast(original.status ? "渠道已停用" : "渠道已启用", "success"); return refresh(); }
        if (button.dataset.action === "delete") return confirmModal("删除渠道？", `<p class="form-help">删除后该渠道配置与密钥引用都会移除，已有请求日志会保留。</p>`, async () => { await api(`/channels/${item.id}`, { method: "DELETE" }); toast("渠道已删除", "success"); refresh(); }, "删除");
      } catch (error) { toast(error, "error"); }
    }; });
  };
  const selectedChannels = () => [...rows.querySelectorAll("[data-channel-select]:checked")].map((input) => Number(input.dataset.channelSelect));
  const batchLabels = { probe: "探测", enable: "启用", disable: "停用", recover: "恢复" };
  const runBatch = async (action) => {
    const ids = selectedChannels();
    if (!ids.length) return toast("请先勾选渠道", "err");
    try {
      const result = await api("/channels/batch", { method: "POST", body: JSON.stringify({ action, ids }) });
      if (action === "probe") {
        const failed = (result.results || []).filter((entry) => !entry.ok).length;
        toast(failed ? `已探测 ${result.affected} 个渠道，${failed} 个失败` : `已探测 ${result.affected} 个渠道，全部通过`, failed ? "err" : "success");
      } else {
        toast(`已${batchLabels[action] || action} ${result.affected} 个渠道`, "success");
      }
      refresh();
    } catch (error) { toast(error, "error"); }
  };
  $("#channel-batch-probe").onclick = () => runBatch("probe");
  $("#channel-batch-enable").onclick = () => runBatch("enable");
  $("#channel-batch-disable").onclick = () => runBatch("disable");
  $("#channel-batch-recover").onclick = () => runBatch("recover");
  $("#channel-search").oninput = draw; $("#channel-filter").onchange = draw; draw();
}

function channelTestPanel(channel) {
  const models = String(channel.models || "").split(",").map((model) => model.trim()).filter(Boolean);
  const modelOptions = models.length ? models.map((model) => `<option value="${esc(model)}">${esc(model)}</option>`).join("") : `<option value="">请先绑定模型</option>`;
  const dialog = openModal(`测试渠道 · ${channel.name}`, `<p class="form-help">测试：发送一次最小真实生成请求；探测：仅检查可用性，不发送生成请求。</p><div class="form-row"><label for="channel-test-model">测试模型</label><select id="channel-test-model" autofocus>${modelOptions}</select></div><div class="form-row"><label for="channel-test-budget">输出预算</label><input id="channel-test-budget" type="number" min="1" max="32768" value="4096"><span class="form-help">本次测试使用 Reply only OK，不自动放大重试。</span></div><div class="hint-box" id="channel-test-result" role="status">尚未执行测试。</div>`, async () => {
    const model = $("#channel-test-model", dialog).value.trim(); if (!model) throw new Error("请先绑定并选择一个测试模型");
    const max_tokens = Number($("#channel-test-budget", dialog).value); if (!Number.isInteger(max_tokens) || max_tokens < 1 || max_tokens > 32768) throw new Error("输出预算必须是 1 到 32768 之间的整数");
    const resultBox = $("#channel-test-result", dialog); resultBox.className = "hint-box"; resultBox.textContent = "正在发送最小测试请求…";
    try {
      const result = await api(`/channels/${channel.id}/test`, { method: "POST", body: JSON.stringify({ model, max_tokens }) });
      const latency = Number(result.latency_ms); const summary = String(result.response_summary || result.message || "无摘要").replace(/；?真实推理测试可能产生极少量额度|（不会产生上游额度）/g, "").trim();
      const diagnostic = { channel_name: result.channel_name || channel.name, public_model: result.public_model || result.test_model || model, upstream_model: result.upstream_model || "—", protocol: result.protocol || "—", final_url: result.final_url || result.request_url || "—", status_code: result.status_code ?? "—", upstream_content_type: result.upstream_content_type || "—", latency_ms: latency > 0 ? latency : null, upstream_request_id: result.upstream_request_id || "—", sent_header_names: result.sent_header_names || [], error_category: result.error_category || "", response_summary: summary || "—", request_id: result.request_id || "—", failover: Boolean(result.failover) };
      const resultState = result.completion_state || (result.ok ? "valid" : "error");
      resultBox.className = `test-result ${resultState === "incomplete" ? "warning" : result.ok ? "success" : "danger"}`;
      resultBox.innerHTML = `<strong>${result.ok ? "已取得有效回答" : resultState === "incomplete" ? "输出预算耗尽" : "测试失败"}</strong>${[["渠道名称", diagnostic.channel_name], ["公开模型", diagnostic.public_model], ["上游模型", diagnostic.upstream_model], ["协议", diagnostic.protocol], ["最终 URL", diagnostic.final_url], ["HTTP", diagnostic.status_code], ["Content-Type", diagnostic.upstream_content_type], ["测试预算", result.max_tokens || max_tokens], ["停止原因", result.completion_state === "incomplete" ? "length / max_tokens" : "—"], ["耗时", diagnostic.latency_ms ? fmtDuration(diagnostic.latency_ms) : "—"], ["上游请求 ID", diagnostic.upstream_request_id], ["发送的 Header 名称", diagnostic.sent_header_names.join(", ") || "—"], ["错误分类", diagnostic.error_category || "—"], ["脱敏摘要", diagnostic.response_summary]].map(([label, value]) => `<div class="detail-kv"><span>${esc(label)}</span><span class="mono">${esc(value)}</span></div>`).join("")}<p class="form-help mono">请求 ID：${esc(diagnostic.request_id)}</p><button class="btn sm mt-16" id="channel-test-copy" type="button">复制诊断信息</button>`;
      $("#channel-test-copy", dialog).onclick = () => copyText(JSON.stringify(diagnostic, null, 2), "诊断信息已复制");
    } catch (error) { const status = error.status || error.data?.status_code || "—"; resultBox.className = "test-result danger"; resultBox.innerHTML = `<strong>测试失败</strong><div class="detail-kv"><span>HTTP</span><span>${esc(status)}</span></div><p>${esc(error.message || "请求失败")}</p>`; }
    return false;
  }, "运行测试", { initialFocus: "#channel-test-model" });
  return dialog;
}

function channelForm(channel, done) {
  let mappingRows = Object.entries(channel?.model_mapping || {}).map(([publicModel, upstreamModel]) => ({ publicModel, upstreamModel }));
  const configuredHeaders = Array.isArray(channel?.extra_headers) ? channel.extra_headers : Object.keys(channel?.extra_headers || {});
  let extraRows = configuredHeaders.map((name) => ({ name, value: "" })); let forwardHeaders = Array.isArray(channel?.forward_headers) ? [...channel.forward_headers] : [];
  let headersEdited = !channel; let controller = null; let lastSuggestedName = ""; let channelNameEditedByUser = Boolean(channel); let applyingSuggestion = false;
  const dialog = openModal(channel ? "编辑渠道" : "新增渠道", `
    <div class="form-grid"><div class="form-row"><label for="f-name">渠道名称</label><input id="f-name" autofocus value="${esc(channel?.name || "")}"></div><div class="form-row"><label for="f-type">协议</label><select id="f-type"><option value="auto">自动识别</option><option value="openai_chat">OpenAI Chat</option><option value="openai_responses">OpenAI Responses</option><option value="anthropic">Anthropic Messages</option><option value="gemini">Gemini</option><option value="mock">本地 mock</option><option value="opencode_free">OpenCode 免费</option><option value="lobsterai">LobsterAI (有道账号)</option></select></div></div>
    <div class="form-row" id="f-url-row"><label for="f-url">Base URL</label><input id="f-url" class="mono" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" value="${esc(channel?.base_url || "")}" placeholder="上游兼容 API 的根地址"><span class="form-help" id="f-url-hint">填写地址后可自动识别协议和模型。</span></div>
    <div class="form-row" id="f-key-row"><label for="f-key">上游 API Key</label><div class="key-value"><input id="f-key" type="password" autocomplete="new-password" placeholder="留空表示保持现有密钥不变"><button class="btn sm" id="f-key-toggle" type="button">显示</button><button class="btn sm" id="f-key-copy" type="button">复制当前输入</button><button class="btn sm hidden" id="f-lb-login" type="button">登录有道账号</button></div><span class="form-help">编辑时不会回填数据库中的密钥；输入为空时不会假装复制成功。</span></div>
    <div class="form-grid"><div class="form-row"><label for="f-models">第三方软件使用的公开模型名</label><input id="f-models" class="mono" value="${esc(channel?.models || "")}" placeholder="例如：glm-5.3,deepseek-v4"></div><div class="form-row"><label for="f-policy">工具调用策略</label><select id="f-policy"><option value="preserve">保留工具调用</option><option value="strip">仅文本兼容</option></select></div></div>
    <div class="form-grid"><div class="form-row"><label for="f-priority">优先级</label><input id="f-priority" type="number" value="${esc(channel?.priority ?? 0)}"></div><div class="form-row"><label for="f-weight">权重</label><input id="f-weight" type="number" min="1" value="${esc(channel?.weight ?? 1)}"></div></div>
    <details class="mt-16"><summary>高级设置：网络与代理</summary><div class="form-row"><label for="f-proxy">渠道代理地址</label><input id="f-proxy" class="mono" placeholder="${channel ? "留空表示保持现有代理不变" : "例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080"}"><span class="form-help">${channel?.proxy_url ? `当前：${esc(channel.proxy_url)}（编辑时不回填）` : "支持 http/https/socks5；留空表示直连。"}</span></div>${channel?.proxy_url ? `<label class="switch-row"><span>清除现有代理<span class="form-help">勾选后保存即改为直连。</span></span><input class="checkbox" id="f-proxy-clear" type="checkbox"></label>` : ""}<div class="form-grid"><div class="form-row"><label for="f-connect">连接超时覆盖（秒）</label><input id="f-connect" type="number" min="0" value="${esc(channel?.connect_timeout_s ?? "")}" placeholder="留空沿用全局"></div><div class="form-row"><label for="f-read">读取超时覆盖（秒）</label><input id="f-read" type="number" min="0" value="${esc(channel?.read_timeout_s ?? "")}" placeholder="留空沿用全局"></div></div><div class="form-row"><label for="f-concurrency">最大并发连接数</label><input id="f-concurrency" type="number" min="0" value="${esc(channel?.max_concurrency ?? 0)}"><span class="form-help">0 表示不限制；用于限制上游连接池。</span></div><div class="form-row"><label for="f-rpm">渠道速率上限（次/分）</label><input id="f-rpm" type="number" min="0" value="${esc(channel?.rpm_limit ?? 0)}"><span class="form-help">0 表示不限制；达到上限的渠道会被自动跳过，用于保护上游不被封禁。</span></div></details>
    <div class="toolbar"><button class="btn" id="f-discover" type="button">重新检测并加载模型</button><button class="btn" id="f-preview" type="button">预览地址</button></div><dl class="hint-box" id="f-preview-box"><dt>保存前预览</dt><dd>填写 Base URL 后可查看规范化地址。</dd></dl>
    <details class="mt-16"><summary>模型映射（可选）</summary><p class="form-help">第三方软件使用的公开模型名 → 上游实际模型 ID。</p><div class="mapping-list mt-16" id="f-mapping-rows"></div><button class="btn sm mt-16" id="f-add-mapping" type="button">添加映射</button></details>
    <details class="mt-16"><summary>高级设置：自定义请求头</summary><div class="mapping-list mt-16" id="f-extra-headers"></div><button class="btn sm mt-16" id="f-add-header" type="button">添加请求头</button><div class="form-row mt-16"><label for="f-user-agent">渠道 User-Agent</label><input id="f-user-agent" placeholder="留空表示保持现有配置"><span class="form-help">仅在明确知道上游要求时填写。</span></div><div class="form-row"><label for="f-forward-headers">安全透传的客户端 Header 名称</label><textarea id="f-forward-headers" rows="2" placeholder="anthropic-beta, x-stainless-*, x-client-*">${esc(forwardHeaders.join(", "))}</textarea><span class="form-help">禁止透传 authorization、x-api-key、host、cookie 等敏感头。</span></div></details>
    <p class="form-help mt-16" id="f-detect-status">模型列表接口不可用时仍可手动填写模型并运行测试。</p><p class="form-error mt-16" id="f-save-status" role="alert"></p>`, async () => {
    const name = $("#f-name", dialog).value.trim(); const type = $("#f-type", dialog).value; const baseUrl = $("#f-url", dialog).value.trim(); const modelsValue = $("#f-models", dialog).value.trim();
    if (!name) throw new Error("请填写渠道名称"); if (type !== "mock" && !baseUrl) throw new Error("非 mock 渠道必须填写 Base URL"); if (!modelsValue) throw new Error("至少填写一个公开模型 ID");
    const mapping = {}; mappingRows.forEach((row) => { if (row.publicModel.trim() && row.upstreamModel.trim()) mapping[row.publicModel.trim()] = row.upstreamModel.trim(); });
    const payload = { name, type, base_url: baseUrl, api_key: $("#f-key", dialog).value, models: modelsValue, model_mapping: mapping, tool_policy: $("#f-policy", dialog).value, priority: Number($("#f-priority", dialog).value || 0), weight: Math.max(1, Number($("#f-weight", dialog).value || 1)), connect_timeout_s: $("#f-connect", dialog).value, read_timeout_s: $("#f-read", dialog).value, max_concurrency: Number($("#f-concurrency", dialog).value || 0), rpm_limit: Number($("#f-rpm", dialog).value || 0) };
    const proxyInput = $("#f-proxy", dialog).value.trim(); const clearProxy = $("#f-proxy-clear", dialog)?.checked;
    if (proxyInput) payload.proxy_url = proxyInput; else if (clearProxy) payload.proxy_url = "";
    if (!channel || headersEdited) { const extra = {}; extraRows.forEach((row) => { const headerName = row.name.trim().toLowerCase(); if (headerName && row.value) extra[headerName] = row.value; }); payload.extra_headers = extra; payload.forward_headers = $("#f-forward-headers", dialog).value.split(/[,\n]+/).map((value) => value.trim()).filter(Boolean); payload.user_agent = $("#f-user-agent", dialog).value.trim(); }
    $("#f-save-status", dialog).textContent = "正在保存…";
    try { if (channel) await api(`/channels/${channel.id}`, { method: "PUT", body: JSON.stringify(payload) }); else await api("/channels", { method: "POST", body: JSON.stringify(payload) }); toast(channel ? "渠道已保存" : "渠道已创建", "success"); done(); } catch (error) { $("#f-save-status", dialog).textContent = error.message || "保存失败"; throw error; }
  }, "保存");
  dialog.addEventListener("close", () => controller?.abort(), { once: true }); $("#f-type", dialog).value = channel?.type || "auto"; $("#f-policy", dialog).value = channel?.tool_policy || "preserve";
  const syncProtocolFields = () => { const type = $("#f-type", dialog).value; const mock = type === "mock"; $("#f-url-row", dialog).classList.toggle("hidden", mock); $("#f-key-row", dialog).classList.toggle("hidden", mock); $("#f-lb-login", dialog)?.classList.toggle("hidden", $("#f-type", dialog).value !== "lobsterai"); $("#f-preview", dialog).disabled = mock; $("#f-discover", dialog).disabled = mock; $("#f-url-hint", dialog).textContent = mock ? "本地 mock 不会访问网络。" : type === "anthropic" ? "Anthropic Messages 根地址，例如 https://api.example.com。" : type === "gemini" ? "Gemini API 根地址，例如 https://generativelanguage.googleapis.com。" : "填写地址后可自动识别协议和模型；自定义路径前缀会保留。"; };
  $("#f-type", dialog).onchange = syncProtocolFields; syncProtocolFields(); $("#f-name", dialog).oninput = () => { if (!applyingSuggestion) channelNameEditedByUser = true; };
  // LobsterAI 账号登录：协议选择 lobsterai 时展示登录按钮（先保存渠道拿到 id，再走 OAuth）
  const lbLogin = $("#f-lb-login", dialog);
  if (lbLogin) lbLogin.onclick = async () => {
    const channelId = channel?.id;
    if (!channelId) return toast("请先保存渠道，再点击登录", "info");
    lbLogin.disabled = true; lbLogin.textContent = "正在发起登录…";
    try {
      const start = await api("/channels/lobsterai/login/start", { method: "POST", body: JSON.stringify({ channel_id: channelId }) });
      window.open(start.login_url, "_blank");
      lbLogin.textContent = "等待浏览器完成授权…";
      const deadline = Date.now() + 600000;
      for (;;) {
        await new Promise((r) => setTimeout(r, 2000));
        const poll = await api(`/channels/lobsterai/login/poll?state=${encodeURIComponent(start.state)}`);
        if (poll.status === "done") { toast(`登录成功：${poll.nickname || "已绑定账号"}`, "success"); break; }
        if (poll.status === "error") { toast(`登录失败：${poll.error || "未知错误"}`, "err"); break; }
        if (Date.now() > deadline) { toast("登录超时", "err"); break; }
      }
    } catch (error) { toast(`登录发起失败：${error.message}`, "err"); }
    finally { lbLogin.disabled = false; lbLogin.textContent = "登录有道账号"; }
  };

  // 新增渠道时清掉浏览器自动填充进 URL/Key 框的残留值，保持留白
  if (!channel) { for (const id of ["#f-url", "#f-key"]) { const input = $(id, dialog); if (input && input.value && input.value !== input.defaultValue) { input.value = ""; } } setTimeout(() => { for (const id of ["#f-url", "#f-key"]) { const input = $(id, dialog); if (input && !input.value && input.dataset.keepAutofill !== "1") input.value = ""; } }, 120); }
  $("#f-key-toggle", dialog).onclick = () => { const input = $("#f-key", dialog); input.type = input.type === "password" ? "text" : "password"; $("#f-key-toggle", dialog).textContent = input.type === "password" ? "显示" : "隐藏"; };
  $("#f-key-copy", dialog).onclick = () => { const value = $("#f-key", dialog).value; if (!value) return toast(channel ? "现有密钥已安全保存，编辑时不会回填" : "请先输入上游 API Key", "info"); copyText(value, "上游 API Key 已复制"); };
  const mappingEl = $("#f-mapping-rows", dialog); const drawMapping = () => { mappingEl.innerHTML = mappingRows.length ? `<div class="mapping-row mapping-head"><span>公开模型名</span><span>上游实际模型 ID</span><span></span></div>${mappingRows.map((row, index) => `<div class="mapping-row"><input data-map-public="${index}" value="${esc(row.publicModel)}" placeholder="公开模型，例如 glm-5.3"><input data-map-upstream="${index}" value="${esc(row.upstreamModel)}" placeholder="上游模型，例如 GLM-5-Base"><button class="btn sm danger" data-map-delete="${index}" type="button">删除</button></div>`).join("")}` : `<div class="form-help">暂无映射，公开模型名会原样发送给上游。</div>`; mappingEl.querySelectorAll("input").forEach((input) => { input.oninput = () => { const index = Number(input.dataset.mapPublic ?? input.dataset.mapUpstream); mappingRows[index][input.dataset.mapPublic !== undefined ? "publicModel" : "upstreamModel"] = input.value; }; }); mappingEl.querySelectorAll("[data-map-delete]").forEach((button) => { button.onclick = () => { mappingRows.splice(Number(button.dataset.mapDelete), 1); drawMapping(); }; }); };
  $("#f-add-mapping", dialog).onclick = () => { mappingRows.push({ publicModel: "", upstreamModel: "" }); drawMapping(); }; drawMapping();
  const headerEl = $("#f-extra-headers", dialog); const drawHeaders = () => { headerEl.innerHTML = extraRows.length ? extraRows.map((row, index) => `<div class="mapping-row"><input data-header-name="${index}" value="${esc(row.name)}" placeholder="Header 名称"><input data-header-value="${index}" type="password" value="${esc(row.value)}" placeholder="Header 值（不会回填已有值）"><button class="btn sm danger" data-header-delete="${index}" type="button">删除</button></div>`).join("") : `<div class="form-help">暂无自定义请求头。</div>`; headerEl.querySelectorAll("input").forEach((input) => { input.oninput = () => { headersEdited = true; const index = Number(input.dataset.headerName ?? input.dataset.headerValue); extraRows[index][input.dataset.headerName !== undefined ? "name" : "value"] = input.value; }; }); headerEl.querySelectorAll("[data-header-delete]").forEach((button) => { button.onclick = () => { headersEdited = true; extraRows.splice(Number(button.dataset.headerDelete), 1); drawHeaders(); }; }); };
  $("#f-add-header", dialog).onclick = () => { headersEdited = true; extraRows.push({ name: "", value: "" }); drawHeaders(); }; drawHeaders(); $("#f-forward-headers", dialog).oninput = () => { headersEdited = true; }; $("#f-user-agent", dialog).oninput = () => { headersEdited = true; };
  const applySuggestion = (result) => { const suggestion = result.suggested_name || result.name || ""; const current = $("#f-name", dialog).value.trim(); if (!channel && suggestion && !channelNameEditedByUser && (!current || current === lastSuggestedName)) { applyingSuggestion = true; $("#f-name", dialog).value = suggestion; applyingSuggestion = false; lastSuggestedName = suggestion; } };
  const renderPreview = (result) => { applySuggestion(result); const protocol = result.recommended_protocol || result.protocol || "—"; const chatUrl = result.chat_url || result.messages_url || result.responses_url || "手动填写模型"; $("#f-preview-box", dialog).innerHTML = `<dt>建议渠道名称</dt><dd>${esc(result.suggested_name || result.name || "—")}</dd><dt>建议协议</dt><dd>${esc(protocol)}</dd><dt>规范化地址</dt><dd class="mono">${esc(result.normalized_base_url || result.base_url || "—")}</dd><dt>模型列表</dt><dd class="mono">${esc(result.models_url || "模型列表接口不可用")}</dd><dt>聊天端点</dt><dd class="mono">${esc(chatUrl)}</dd>`; };
  const preview = async () => { const url = $("#f-url", dialog).value.trim(); if (!url || $("#f-type", dialog).value === "mock") return; try { const result = await api("/channels/preview-url", { method: "POST", body: JSON.stringify({ type: $("#f-type", dialog).value, base_url: url }) }); if (dialog.open) renderPreview(result); } catch (error) { if (dialog.open) $("#f-preview-box", dialog).innerHTML = `<dt>预览失败</dt><dd>${esc(error.message)}</dd>`; } };
  $("#f-preview", dialog).onclick = preview; $("#f-url", dialog).onblur = preview;
  $("#f-discover", dialog).onclick = async () => { controller?.abort(); controller = new AbortController(); const button = $("#f-discover", dialog); button.disabled = true; button.textContent = "正在识别协议并加载模型"; $("#f-detect-status", dialog).textContent = "正在识别协议并加载模型…"; const headerPayload = headersEdited ? { extra_headers: Object.fromEntries(extraRows.map((row) => [row.name.trim().toLowerCase(), row.value]).filter(([name, value]) => name && value)), forward_headers: $("#f-forward-headers", dialog).value.split(/[,\n]+/).map((value) => value.trim()).filter(Boolean), user_agent: $("#f-user-agent", dialog).value.trim() } : {}; try { const result = await api("/channels/detect", { method: "POST", signal: controller.signal, body: JSON.stringify({ channel_id: channel?.id, base_url: $("#f-url", dialog).value.trim(), api_key: $("#f-key", dialog).value, model: $("#f-models", dialog).value.split(",")[0].trim(), protocol: $("#f-type", dialog).value, ...headerPayload }) }); if (!dialog.open) return; applySuggestion(result); if (result.protocol && result.protocol !== "auto") $("#f-type", dialog).value = result.protocol; const models = (result.models || []).map((item) => item.id).filter(Boolean); if (models.length) $("#f-models", dialog).value = models.join(","); $("#f-detect-status", dialog).textContent = result.ok ? `已识别 ${result.protocol}，加载 ${models.length} 个模型。` : `${result.message || "自动加载失败，可手动填写模型 ID"}${result.manual_test_available ? "；已填写模型仍可直接运行测试。" : ""}`; syncProtocolFields(); } catch (error) { if (error.name !== "AbortError" && dialog.open) $("#f-detect-status", dialog).textContent = `自动加载失败，可手动填写模型 ID：${error.message}`; } finally { if (dialog.open) { button.disabled = false; button.textContent = "重新检测并加载模型"; } } };
  return dialog;
}
