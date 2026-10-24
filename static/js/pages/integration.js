import { api } from "../core/api.js";
import { $, esc } from "../core/dom.js";
import { badge, copyText, toast } from "../core/ui.js";
import { pageHeader } from "../core/page.js";

const tabs = ["接入配置", "使用示例", "安全设置"];
const clientTabs = ["cURL", "PowerShell", "Python (OpenAI SDK)", "Cherry Studio", "Continue", "Open WebUI"];
const DEFAULT_TEST_MAX_TOKENS = 4096;

function protocolSpec(origin, protocol) {
  const root = origin.replace(/\/$/, "");
  const specs = {
    openai_chat: { label: "OpenAI Chat", base: root + "/v1", endpoint: root + "/v1/chat/completions", auth: "Authorization: Bearer <OBSESSION_KEY>", path: "/v1/chat/completions" },
    anthropic: { label: "Anthropic Messages", base: root, endpoint: root + "/v1/messages", auth: "x-api-key: <OBSESSION_KEY>", path: "/v1/messages", note: "Anthropic SDK 使用根地址，避免 SDK 再追加 /v1 后变成 /v1/v1/messages。" },
    openai_responses: { label: "OpenAI Responses", base: root + "/v1", endpoint: root + "/v1/responses", auth: "Authorization: Bearer <OBSESSION_KEY>", path: "/v1/responses" },
    gemini: { label: "Gemini", base: root, endpoint: root + "/v1beta/models/{model}:generateContent", auth: "x-goog-api-key: <OBSESSION_KEY>", path: "/v1beta/models/{model}:generateContent" },
  };
  return specs[protocol] || specs.openai_chat;
}

function textFrom(value) {
  if (typeof value === "string") return value.trim();
  if (Array.isArray(value)) return value.map((item) => textFrom(item)).filter(Boolean).join("").trim();
  if (value && typeof value === "object") return textFrom(value.text || value.content || value.output_text || "");
  return "";
}

function meaningfulToolCall(call) {
  const fn = call && typeof call === "object" ? (call.function || call) : null;
  return Boolean(fn && (textFrom(fn.name) || textFrom(fn.arguments)));
}

function validChatResponse(payload) {
  const choice = payload && Array.isArray(payload.choices) ? payload.choices[0] : null;
  const message = choice && choice.message;
  return Boolean(message && (textFrom(message.content) || textFrom(message.refusal) || (Array.isArray(message.tool_calls) && message.tool_calls.some(meaningfulToolCall)) || meaningfulToolCall(message.function_call)));
}

function validAnthropicResponse(payload) {
  return Boolean(payload && Array.isArray(payload.content) && payload.content.some((block) => block?.type === "tool_use" || textFrom(block?.text)));
}

function validResponsesResponse(payload) {
  return Boolean(payload && (textFrom(payload.output_text) || textFrom(payload.output)));
}

function validGeminiResponse(payload) {
  const candidate = payload && Array.isArray(payload.candidates) ? payload.candidates[0] : null;
  const parts = candidate && candidate.content && candidate.content.parts;
  return Boolean(Array.isArray(parts) && parts.some((part) => textFrom(part.text) || textFrom(part.functionCall)));
}

export function validateProtocolResponse(protocol, payload) {
  const valid = protocol === "anthropic" ? validAnthropicResponse(payload) : protocol === "openai_responses" ? validResponsesResponse(payload) : protocol === "gemini" ? validGeminiResponse(payload) : validChatResponse(payload);
  const choice = payload && Array.isArray(payload.choices) ? payload.choices[0] : null;
  const finishReason = choice && choice.finish_reason;
  const incomplete = payload?.stop_reason === "max_tokens" || payload?.status === "incomplete" || finishReason === "length" || payload?.candidates?.[0]?.finishReason === "MAX_TOKENS";
  if (valid) return { ok: true, state: "valid", finishReason, message: "已取得有效回答" };
  if (incomplete) return { ok: false, state: "incomplete", finishReason: payload?.stop_reason || finishReason || "length", message: "请求已返回，但输出预算耗尽，未取得最终回答" };
  return { ok: false, state: "protocol_error", finishReason, message: "HTTP 200 响应没有可展示的文本、工具调用或拒答内容" };
}

function embeddingsExample(base, key, model) {
  const safeModel = model || "你的向量模型名";
  const safeKey = key || "$env:OBSESSION_KEY";
  return "curl " + base + "/embeddings \\\n  -H \"Authorization: Bearer " + safeKey + "\" \\\n  -H \"Content-Type: application/json\" \\\n  -d '{\"model\":\"" + safeModel + "\",\"input\":[\"你好\",\"world\"]}'";
}

function examples(spec, key, model, type) {
  const safeModel = model || "你的模型名";
  const safeKey = key || "$env:OBSESSION_KEY";
  if (type === "PowerShell") {
    const headers = spec.label === "Anthropic Messages" ? '@{ "x-api-key" = "' + safeKey + '"; "anthropic-version" = "2023-06-01"; "Content-Type" = "application/json" }' : spec.label === "Gemini" ? '@{ "x-goog-api-key" = "' + safeKey + '"; "Content-Type" = "application/json" }' : '@{ Authorization = "Bearer ' + safeKey + '"; "Content-Type" = "application/json" }';
    const body = spec.label === "Anthropic Messages" ? '@{ model = "' + safeModel + '"; max_tokens = 256; messages = @(@{ role = "user"; content = "你好" }) }' : spec.label === "Gemini" ? '@{ contents = @(@{ role = "user"; parts = @(@{ text = "你好" }) }) }' : spec.label === "OpenAI Responses" ? '@{ model = "' + safeModel + '"; input = "你好" }' : '@{ model = "' + safeModel + '"; messages = @(@{ role = "user"; content = "你好" }) }';
    return "$headers = " + headers + "\n$body = (" + body + " | ConvertTo-Json -Depth 8)\nInvoke-RestMethod -Uri \"" + spec.endpoint.replace("{model}", safeModel) + "\" -Method Post -Headers $headers -Body $body";
  }
  if (spec.label === "Anthropic Messages") {
    if (type === "Python (OpenAI SDK)") return 'import anthropic\n\nclient = anthropic.Anthropic(base_url="' + spec.base + '", api_key="' + safeKey + '")\nmessage = client.messages.create(model="' + safeModel + '", max_tokens=256, messages=[{"role": "user", "content": "你好"}])\nprint(message.content)';
    return "curl " + spec.endpoint + " \\\n  -H \"x-api-key: " + safeKey + "\" \\\n  -H \"anthropic-version: 2023-06-01\" \\\n  -H \"Content-Type: application/json\" \\\n  -d '{\"model\":\"" + safeModel + "\",\"max_tokens\":256,\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}'";
  }
  if (spec.label === "Gemini") return "curl \"" + spec.endpoint.replace("{model}", safeModel) + "\" \\\n  -H \"x-goog-api-key: " + safeKey + "\" \\\n  -H \"Content-Type: application/json\" \\\n  -d '{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"你好\"}]}]}'";
  if (type === "Python (OpenAI SDK)") {
    const method = spec.label === "OpenAI Responses" ? "responses.create" : "chat.completions.create";
    const input = spec.label === "OpenAI Responses" ? 'input="你好"' : 'messages=[{"role": "user", "content": "你好"}]';
    // OpenAI Responses SDK call: client.responses.create(...)
    return 'from openai import OpenAI\n\nclient = OpenAI(base_url="' + spec.base + '", api_key="' + safeKey + '")\nresponse = client.' + method + '(model="' + safeModel + '", ' + input + ')\nprint(response)';
  }
  if (type === "Cherry Studio") return "Base URL: " + spec.base + "\nEndpoint: " + spec.path + "\nAPI Key: " + safeKey + "\n模型: " + safeModel;
  if (type === "Continue") return "name: OBSESSIONAPI\napiBase: " + spec.base + "\napiKey: " + safeKey + "\nmodel: " + safeModel;
  if (type === "Open WebUI") return "连接地址: " + spec.base + "\nAPI Key: " + safeKey + "\n模型: " + safeModel;
  const body = spec.label === "OpenAI Responses" ? '{"model":"' + safeModel + '","input":"你好"}' : '{"model":"' + safeModel + '","messages":[{"role":"user","content":"你好"}]}';
  return "curl " + spec.endpoint + " \\\n  -H \"" + spec.auth.replace("<OBSESSION_KEY>", safeKey) + "\" \\\n  -H \"Content-Type: application/json\" \\\n  -d '" + body + "'";
}

export async function renderIntegration(el, { navigate, isCurrent } = {}) {
  const [modelsPayload, keysPayload] = await Promise.all([api("/models"), api("/keys")]);
  if (isCurrent && !isCurrent()) return;
  const enabledModels = (Array.isArray(modelsPayload) ? modelsPayload : []).filter((model) => model.status);
  const usableKeys = (Array.isArray(keysPayload) ? keysPayload : []).filter((key) => key.status);
  const origin = location.origin;
  const state = { protocol: "openai_chat", model: "", keyId: "", client: clientTabs[0], revealedKey: "", testBudget: DEFAULT_TEST_MAX_TOKENS };

  el.innerHTML = '<div class="content-inner">' + pageHeader("API 接入", "按协议、模型和网关 Key 生成可复制的真实配置", '<button class="btn" id="integration-back" type="button">返回设置</button>') + '<div class="tabs" id="integration-tabs">' + tabs.map((tab, index) => '<button type="button" class="' + (index === 0 ? "active" : "") + '" data-tab="' + esc(tab) + '">' + esc(tab) + "</button>").join("") + '</div><div id="integration-panel"></div></div>';
  $("#integration-back").onclick = () => navigate("settings");
  const panel = $("#integration-panel");

  const selectedModel = () => enabledModels.find((model) => String(model.model_name) === String(state.model));
  const selectedKey = () => usableKeys.find((key) => String(key.id) === String(state.keyId));
  const selectionMarkup = (prefix) => '<div class="integration-selection"><div class="form-row"><label for="' + prefix + '-model">模型</label><select id="' + prefix + '-model"><option value="">选择已上架模型</option>' + enabledModels.map((model) => '<option value="' + esc(model.model_name) + '"' + (String(state.model) === String(model.model_name) ? " selected" : "") + ">" + esc(model.display_name || model.model_name) + "</option>").join("") + '</select></div><div class="form-row"><label for="' + prefix + '-key">网关 Key</label><select id="' + prefix + '-key"><option value="">选择有效网关 Key</option>' + usableKeys.map((key) => '<option value="' + esc(key.id) + '"' + (String(state.keyId) === String(key.id) ? " selected" : "") + ">" + esc(key.name || key.id) + "</option>").join("") + "</select></div></div>";
  const bindSelection = (prefix, rerender) => {
    $("#" + prefix + "-model").onchange = (event) => { state.model = event.target.value; rerender(); };
    $("#" + prefix + "-key").onchange = (event) => { state.keyId = event.target.value; state.revealedKey = ""; rerender(); };
  };
  const getKey = async () => {
    const key = selectedKey();
    if (!key) throw new Error("请先选择有效网关 Key");
    if (!state.revealedKey) state.revealedKey = (await api("/keys/" + encodeURIComponent(key.id) + "/reveal")).key;
    return state.revealedKey;
  };
  const testBudget = () => {
    const input = $("#integration-test-budget");
    const value = Number(input?.value || state.testBudget);
    if (!Number.isInteger(value) || value < 1 || value > 32768) throw new Error("测试输出预算必须是 1 到 32768 之间的整数");
    state.testBudget = value;
    return value;
  };
  const requestOptions = async (spec) => {
    const model = selectedModel();
    if (!model) throw new Error("请先选择已上架模型");
    const key = await getKey();
    const budget = testBudget();
    if (state.protocol === "anthropic") return { endpoint: spec.endpoint, headers: { "x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json" }, body: { model: model.model_name, max_tokens: budget, messages: [{ role: "user", content: "Reply only OK" }] }, budget };
    if (state.protocol === "openai_responses") return { endpoint: spec.endpoint, headers: { Authorization: "Bearer " + key, "Content-Type": "application/json" }, body: { model: model.model_name, max_output_tokens: budget, input: "Reply only OK" }, budget };
    if (state.protocol === "gemini") return { endpoint: spec.endpoint.replace("{model}", encodeURIComponent(model.model_name)), headers: { "x-goog-api-key": key, "Content-Type": "application/json" }, body: { contents: [{ role: "user", parts: [{ text: "Reply only OK" }] }], generationConfig: { maxOutputTokens: budget } }, budget };
    return { endpoint: spec.endpoint, headers: { Authorization: "Bearer " + key, "Content-Type": "application/json" }, body: { model: model.model_name, max_tokens: budget, messages: [{ role: "user", content: "Reply only OK" }] }, budget };
  };
  const requestCurrent = async (spec) => {
    const options = await requestOptions(spec);
    const response = await window.fetch(options.endpoint, { method: "POST", headers: options.headers, body: JSON.stringify(options.body) });
    const payload = await response.json().catch(() => ({}));
    const requestId = response.headers.get("x-request-id") || "未知";
    if (!(response.status >= 200 && response.status < 300)) return { ok: false, state: "upstream_error", status: response.status, requestId, budget: options.budget, message: payload?.error?.message || "网关请求失败" };
    const validation = validateProtocolResponse(state.protocol, payload);
    return { ...validation, status: response.status, requestId, budget: options.budget };
  };
  const renderProtocolTabs = () => '<div class="tabs" id="integration-protocol-tabs">' + ["openai_chat", "anthropic", "openai_responses", "gemini"].map((id) => '<button type="button" class="' + (id === state.protocol ? "active" : "") + '" data-protocol="' + id + '">' + esc(protocolSpec(origin, id).label) + "</button>").join("") + "</div>";
  const bindProtocolTabs = (rerender) => $("#integration-protocol-tabs").querySelectorAll("[data-protocol]").forEach((button) => { button.onclick = () => { state.protocol = button.dataset.protocol; rerender(); }; });
  const renderConnection = () => {
    const spec = protocolSpec(origin, state.protocol);
    const key = selectedKey();
    const masked = key ? key.key || "已选择 Key" : "尚未选择有效 Key";
    panel.innerHTML = `
      ${renderProtocolTabs()}
      <div class="grid two-col">
        <section class="card card-pad">
          ${selectionMarkup("integration")}
          <h2 class="section-title mt-16">${esc(spec.label)} 接入配置</h2>
          <div class="form-row mt-16"><label for="integration-base">Base URL</label><div class="key-value"><input id="integration-base" class="mono" readonly value="${esc(spec.base)}"><button class="btn" id="integration-base-copy" type="button">复制</button></div></div>
          <div class="form-row"><label for="integration-endpoint">完整 Endpoint</label><div class="key-value"><input id="integration-endpoint" class="mono" readonly value="${esc(spec.endpoint)}"><button class="btn" id="integration-endpoint-copy" type="button">复制</button></div></div>
          <div class="form-row"><label for="integration-key-value">API Key</label><div class="key-value"><input id="integration-key-value" class="mono" readonly value="${esc(masked)}"><button class="btn" id="integration-key-toggle" type="button"${key ? "" : " disabled"}>显示</button><button class="btn" id="integration-key-copy" type="button"${key ? "" : " disabled"}>复制</button></div></div>
          <div class="form-row"><label for="integration-test-budget">测试输出预算</label><input id="integration-test-budget" type="number" min="1" max="32768" step="1" value="${esc(state.testBudget)}"><span class="form-help">仅用于本次测试；提示词为 “Reply only OK”，不会自动重试放大。</span></div>
          <div class="detail-kv"><span>认证</span><span class="mono">${esc(spec.auth)}</span></div>
          ${spec.note ? '<div class="hint-box mt-16">' + esc(spec.note) + "</div>" : ""}
          <p class="mt-16">${badge(state.protocol === "anthropic" ? "Anthropic Messages" : state.protocol === "gemini" ? "Gemini Compatible" : "OpenAI Compatible", "success")}</p>
          <div class="toolbar mt-16"><button class="btn" id="integration-manage" type="button">管理密钥</button><button class="btn" id="integration-test" type="button">测试当前协议</button><button class="btn primary" id="integration-diagnose" type="button">运行兼容性检查</button></div>
          <div id="integration-test-result"><div class="hint-box mt-16">未测试</div></div>
          <div id="integration-diagnose-result"></div>
        </section>
        <section class="card card-pad">
          <h2 class="section-title">接入边界</h2>
          <ol class="quick-list mt-16"><li><b>先选模型和 Key</b><span>本页不会默认偷选第一项，测试请求会使用当前选择。</span></li><li><b>按协议复制</b><span>Responses 使用 responses.create；Anthropic 使用 messages.create。</span></li><li><b>错误可追踪</b><span>HTTP 200 会区分输出预算耗尽与协议错误，并关联 request ID。</span></li></ol>
          <div class="hint-box mt-16">渠道上游 Key 只在服务端保存；这里的 Key 是客户端访问网关用的 Key。</div>
        </section>
      </div>
      <section class="card card-pad mt-16" id="integration-embeddings">
        <h2 class="section-title">文本向量化（embeddings）</h2>
        <p class="form-help mt-16">网关同时转发 OpenAI 兼容的 <span class="mono">POST /v1/embeddings</span>，鉴权、模型白名单、限流、熔断与用量记录和对话完全一致。仅支持 OpenAI 兼容渠道；Anthropic / Gemini 渠道会返回 400 unsupported_protocol。</p>
        <div class="form-row mt-16"><label for="integration-embeddings-endpoint">Endpoint</label><div class="key-value"><input id="integration-embeddings-endpoint" class="mono" readonly value="${esc(spec.base + "/embeddings")}"><button class="btn" id="integration-embeddings-copy" type="button">复制</button></div></div>
        <pre class="code-box" id="integration-embeddings-code">${esc(embeddingsExample(spec.base, "$env:OBSESSION_KEY", selectedModel()?.model_name || ""))}</pre>
      </section>`;
    bindSelection("integration", renderConnection);
    bindProtocolTabs(renderConnection);
    $("#integration-base-copy").onclick = () => copyText(spec.base, "Base URL 已复制");
    $("#integration-embeddings-copy").onclick = () => copyText(spec.base + "/embeddings", "Embeddings Endpoint 已复制");
    $("#integration-endpoint-copy").onclick = () => copyText(spec.endpoint.replace("{model}", selectedModel()?.model_name || "{model}"), "Endpoint 已复制");
    $("#integration-manage").onclick = () => navigate("keys");
    if (key) {
      $("#integration-key-toggle").onclick = async () => { try { const value = await getKey(); const input = $("#integration-key-value"); const showing = input.value === value; input.value = showing ? masked : value; $("#integration-key-toggle").textContent = showing ? "显示" : "隐藏"; } catch (error) { toast(error.message, "err"); } };
      $("#integration-key-copy").onclick = async () => { try { await copyText(await getKey(), "API Key 已复制"); } catch (error) { toast(error.message, "err"); } };
    }
    $("#integration-test").onclick = async () => {
      const box = $("#integration-test-result"); box.innerHTML = '<p class="form-help mt-16">请求中：正在发送测试预算 ' + esc(state.testBudget) + '…</p>';
      try { const result = await requestCurrent(spec); if (isCurrent && !isCurrent()) return; const type = result.state === "incomplete" ? "warning" : result.ok ? "success" : "danger"; const label = result.ok ? "已取得有效回答" : result.state === "incomplete" ? "输出预算耗尽" : result.state === "protocol_error" ? "协议转换错误" : "上游错误"; box.innerHTML = '<div class="test-result ' + type + ' mt-16\"><strong>' + label + '</strong>：' + esc(result.message) + ' · HTTP ' + esc(result.status) + ' · 预算 ' + esc(result.budget) + ' · finish_reason ' + esc(result.finishReason || "未知") + ' · request ID ' + esc(result.requestId) + '</div>'; } catch (error) { box.innerHTML = '<div class="test-result danger mt-16\"><strong>上游错误</strong>：' + esc(error.message) + "</div>"; }
    };
    $("#integration-diagnose").onclick = () => runDiagnostics(spec);
  };
  const runDiagnostics = async (spec) => {
    const box = $("#integration-diagnose-result");
    if (!selectedModel() || !selectedKey()) { box.innerHTML = '<div class="hint-box danger mt-16">请先选择已上架模型和有效网关 Key，再运行检查。</div>'; return; }
    box.innerHTML = '<div class="hint-box mt-16">正在检查健康、模型列表和当前协议…</div>';
    const lines = [];
    const check = async (label, request, validator) => {
      try {
        const response = await window.fetch(request.url, request.options);
        const payload = await response.json().catch(() => ({}));
        const goodStatus = response.status >= 200 && response.status < 300;
        const validation = validator && goodStatus ? validator(payload) : { ok: goodStatus, message: goodStatus ? "响应可达" : (payload?.error?.message || "HTTP " + response.status) };
        lines.push({ ok: goodStatus && validation.ok, label, detail: goodStatus && validation.ok ? "HTTP " + response.status + " · " + validation.message : "HTTP " + response.status + " · " + (payload?.error?.message || validation.message) });
      } catch (error) { lines.push({ ok: false, label, detail: error.message }); }
    };
    try {
      const current = await requestOptions(spec);
      await check("GET /health", { url: origin + "/health", options: {} });
      await check("GET /v1/models", { url: origin + "/v1/models", options: { headers: { Authorization: current.headers.Authorization || "Bearer " + (await getKey()) } } });
      await check(spec.label + " 当前 Endpoint", { url: current.endpoint, options: { method: "POST", headers: current.headers, body: JSON.stringify(current.body) } }, (payload) => validateProtocolResponse(state.protocol, payload));
    } catch (error) { lines.push({ ok: false, label: "诊断准备", detail: error.message }); }
    box.innerHTML = '<div class="diagnostic-list mt-16">' + lines.map((line) => '<div class="diagnostic-row ' + (line.ok ? "success" : "danger") + '"><span>' + (line.ok ? "✓" : "✗") + "</span><strong>" + esc(line.label) + "</strong><span>" + esc(line.detail) + "</span></div>").join("") + "</div>";
  };
  const renderExamples = () => {
    const spec = protocolSpec(origin, state.protocol);
    panel.innerHTML = '<div class="card card-pad">' + renderProtocolTabs() + '<div class="hint-box mt-16">示例会使用当前协议；请先选择已上架模型。Key 以占位符显示，不会把明文写入代码。</div>' + selectionMarkup("example") + '<div class="mt-16" id="example-tabs"><div class="tabs">' + clientTabs.map((name, index) => '<button type="button" class="' + (index === 0 ? "active" : "") + '" data-client-tab="' + esc(name) + '">' + esc(name) + "</button>").join("") + '</div><div class="toolbar"><button class="btn" id="example-copy" type="button">复制代码</button></div><pre class="code-box" id="example-code"></pre></div></div>';
    bindSelection("example", renderExamples);
    bindProtocolTabs(renderExamples);
    const updateCode = () => { $("#example-code").textContent = examples(spec, "$env:OBSESSION_KEY", state.model, state.client); };
    updateCode();
    $("#example-copy").onclick = () => copyText($("#example-code").textContent, "示例代码已复制");
    $("#example-tabs").querySelectorAll("[data-client-tab]").forEach((button) => { button.onclick = () => { state.client = button.dataset.clientTab; $("#example-tabs").querySelectorAll("[data-client-tab]").forEach((item) => item.classList.toggle("active", item === button)); updateCode(); }; });
  };
  const renderTab = (tab) => {
    if (tab === "接入配置") return renderConnection();
    if (tab === "使用示例") return renderExamples();
    panel.innerHTML = '<div class="card settings-section"><h2>安全边界</h2><div class="hint-box">网关 Key 只在创建、重置或主动复制时读取。渠道上游 Key 使用服务器端加密存储，管理页面不会回填明文。</div><div class="detail-kv"><span>认证方式</span><span class="mono">Authorization / x-api-key / x-goog-api-key</span></div><div class="detail-kv"><span>重复路径兼容</span><span>仅对已知 /v1/v1 端点提供 alias，并返回警告头。</span></div></div>';
  };
  $("#integration-tabs").querySelectorAll("[data-tab]").forEach((button) => { button.onclick = () => { $("#integration-tabs").querySelectorAll("[data-tab]").forEach((item) => item.classList.toggle("active", item === button)); renderTab(button.dataset.tab); }; });
  renderTab(tabs[0]);
}
