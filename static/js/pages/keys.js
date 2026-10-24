import { api } from "../core/api.js";
import { $, esc, fmtTime } from "../core/dom.js";
import { copyText, confirmModal, openModal, statusBadge, toast } from "../core/ui.js";
import { pageHeader } from "../core/page.js";

const limitFields = (key = {}) => `
  <div class="form-row"><label for="key-model-scope">模型白名单</label><input id="key-model-scope" placeholder="留空表示不限制，多个模型用逗号分隔" value="${esc((key.allowed_models || []).join(", "))}"></div>
  <div class="form-row"><label for="key-rpm">每分钟请求上限</label><input id="key-rpm" type="number" min="0" step="1" placeholder="0 表示不限" value="${key.rpm_limit || 0}"></div>
  <div class="form-row"><label for="key-concurrency">并发上限（非流式）</label><input id="key-concurrency" type="number" min="0" step="1" placeholder="0 表示不限" value="${key.concurrency_limit || 0}"></div>
  <p class="form-help">白名单留空表示可用全部已上架模型；0 表示不限。限流命中时网关返回 429。</p>`;

const readLimits = () => ({
  allowed_models: ($("#key-model-scope").value || "").split(/[,\s]+/).filter(Boolean),
  rpm_limit: Math.max(0, parseInt($("#key-rpm").value, 10) || 0),
  concurrency_limit: Math.max(0, parseInt($("#key-concurrency").value, 10) || 0),
});

const limitSummary = (key) => {
  const models = (key.allowed_models || []).length ? `模型 ${esc(key.allowed_models.join(", "))}` : "全部模型";
  const rpm = key.rpm_limit ? `${key.rpm_limit} 次/分` : "RPM 不限";
  const concurrency = key.concurrency_limit ? `并发 ${key.concurrency_limit}` : "并发不限";
  return `<div class="subtle">${models}</div><div class="subtle">${rpm} · ${concurrency}</div>`;
};

export async function renderKeys(el, { user, isCurrent } = {}) {
  const keys = await api("/keys");
  if (isCurrent && !isCurrent()) return;
  const revealTimers = new Map();
  const cleanup = () => {
    revealTimers.forEach((timer) => clearTimeout(timer)); revealTimers.clear();
    el.querySelectorAll("[data-key-value]").forEach((node) => { node.textContent = node.dataset.masked || "••••"; });
  };
  el._pageCleanup = cleanup;
  el.innerHTML = `<div class="content-inner">${pageHeader("密钥", "管理个人客户端接入密钥", `<button class="btn primary" id="key-add" type="button">新建密钥</button>`)}<div class="hint-box">密钥只用于网关鉴权。完整值只在创建、重置或主动显示时读取。可为每个 Key 限定可用模型与限流。</div><section class="card mt-16"><div class="table-wrap"><table class="data-table"><thead><tr><th>名称</th><th>Key</th><th>权限与限流</th><th>状态</th><th>最近使用</th><th style="text-align:right">操作</th></tr></thead><tbody id="key-rows"></tbody></table></div></section></div>`;
  const draw = () => {
    $("#key-rows").innerHTML = keys.length ? keys.map((key) => `<tr><td><strong>${esc(key.name)}</strong><div class="subtle">创建于 ${esc(fmtTime(key.created_at))}</div></td><td><span class="mono" data-key-value="${key.id}" data-masked="${esc(key.key)}">${esc(key.key)}</span><div class="table-actions key-actions"><button class="btn sm" data-key-show="${key.id}" type="button">显示</button><button class="btn sm" data-key-copy="${key.id}" type="button" aria-label="复制 API Key">复制</button></div></td><td>${limitSummary(key)}</td><td>${statusBadge(key.status)}</td><td class="subtle">${esc(fmtTime(key.last_used_at))}</td><td><div class="table-actions"><button class="btn sm" data-key-edit="${key.id}" type="button">编辑</button><button class="btn sm" data-key-reset="${key.id}" type="button">重置</button><button class="btn sm" data-key-toggle="${key.id}" type="button">${key.status ? "禁用" : "启用"}</button><button class="btn sm danger" data-key-delete="${key.id}" type="button">删除</button></div></td></tr>`).join("") : `<tr><td colspan="6" style="padding:0"><div class="empty-state"><strong>还没有密钥</strong><p>创建一个密钥即可接入客户端。</p></div></td></tr>`;
    bind();
  };
  const reveal = async (key, valueNode, showButton) => {
    const value = (await api(`/keys/${key.id}/reveal`)).key;
    valueNode.textContent = value; showButton.textContent = "隐藏"; showButton.dataset.revealed = "1";
    clearTimeout(revealTimers.get(key.id));
    revealTimers.set(key.id, setTimeout(() => { valueNode.textContent = valueNode.dataset.masked; showButton.textContent = "显示"; delete showButton.dataset.revealed; revealTimers.delete(key.id); }, 10000));
    return value;
  };
  const bind = () => {
    $("#key-rows").querySelectorAll("[data-key-show]").forEach((button) => { button.onclick = async () => { const key = keys.find((item) => item.id === Number(button.dataset.keyShow)); const valueNode = $(`[data-key-value="${key.id}"]`, $("#key-rows")); if (button.dataset.revealed) { clearTimeout(revealTimers.get(key.id)); revealTimers.delete(key.id); valueNode.textContent = valueNode.dataset.masked; button.textContent = "显示"; delete button.dataset.revealed; return; } try { await reveal(key, valueNode, button); } catch (error) { toast(error.message, "err"); } }; });
    $("#key-rows").querySelectorAll("[data-key-copy]").forEach((button) => { button.onclick = async () => { const key = keys.find((item) => item.id === Number(button.dataset.keyCopy)); try { const value = (await api(`/keys/${key.id}/reveal`)).key; await copyText(value, "API Key 已复制"); } catch (error) { toast(error.message, "err"); } }; });
    $("#key-rows").querySelectorAll("[data-key-edit]").forEach((button) => { button.onclick = () => { const key = keys.find((item) => item.id === Number(button.dataset.keyEdit)); openModal("编辑密钥", `<div class="form-row"><label for="key-name">名称</label><input id="key-name" autofocus value="${esc(key.name)}"></div>${limitFields(key)}`, async () => { const name = $("#key-name").value.trim() || key.name; const limits = readLimits(); await api(`/keys/${key.id}`, { method: "PUT", body: JSON.stringify({ name, ...limits }) }); Object.assign(key, { name, ...limits }); toast("密钥已更新"); draw(); }, "保存"); }; });
    $("#key-rows").querySelectorAll("[data-key-reset]").forEach((button) => { button.onclick = () => { const key = keys.find((item) => item.id === Number(button.dataset.keyReset)); confirmModal("重置这个密钥？", `<p class="form-help">旧值会立即失效，现有客户端需要更新配置。</p>`, async () => { const result = await api(`/keys/${key.id}/reset`, { method: "POST" }); showNewKey(result.key, "密钥已重置"); key.key = `${result.key.slice(0, 5)}••••••••${result.key.slice(-4)}`; draw(); }, "确认重置"); }; });
    $("#key-rows").querySelectorAll("[data-key-toggle]").forEach((button) => { button.onclick = async () => { const key = keys.find((item) => item.id === Number(button.dataset.keyToggle)); try { await api(`/keys/${key.id}`, { method: "PUT", body: JSON.stringify({ status: key.status ? 0 : 1 }) }); key.status = key.status ? 0 : 1; toast(key.status ? "密钥已启用" : "密钥已禁用"); draw(); } catch (error) { toast(error, "error"); } }; });
    $("#key-rows").querySelectorAll("[data-key-delete]").forEach((button) => { button.onclick = () => { const key = keys.find((item) => item.id === Number(button.dataset.keyDelete)); confirmModal("删除这个密钥？", `<p class="form-help">删除后使用它的客户端会立即收到 401。</p>`, async () => { await api(`/keys/${key.id}`, { method: "DELETE" }); keys.splice(keys.indexOf(key), 1); toast("密钥已删除"); draw(); }, "确认删除"); }; });
  };
  const showNewKey = (value, title) => { const dialog = openModal(title, `<p class="form-help">请立即复制保存，离开此窗口后不会再次完整展示。</p><pre class="code-box" id="new-key-value">${esc(value)}</pre><button class="btn primary mt-16" id="new-key-copy" type="button">复制密钥</button>`, null, "完成"); $("#new-key-copy", dialog).onclick = () => copyText(value, "API Key 已复制"); };
  $("#key-add").onclick = () => openModal("新建密钥", `<div class="form-row"><label for="key-new-name">名称</label><input id="key-new-name" autofocus placeholder="客户端名称"></div>${limitFields()}`, async () => { const name = $("#key-new-name").value.trim() || "未命名密钥"; const limits = readLimits(); const result = await api("/keys", { method: "POST", body: JSON.stringify({ name, user_id: user.id, ...limits }) }); showNewKey(result.key, "密钥创建成功"); keys.unshift({ id: result.id, name, key: `${result.key.slice(0, 5)}••••••••${result.key.slice(-4)}`, status: 1, created_at: "", last_used_at: null, ...limits }); draw(); }, "创建");
  draw();
}
