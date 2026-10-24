import { esc } from "./dom.js";
import { emptyState } from "./ui.js";

export const tableShell = (columns, body = "") => `<div class="table-wrap"><table class="data-table"><thead><tr>${columns.map((column) => `<th>${esc(column)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`;
export const tableEmpty = (columns, title, hint) => `<tr><td colspan="${columns}" style="padding:0">${emptyState(title, hint)}</td></tr>`;
export const pager = (page, pageSize, total) => `<div class="pager"><span>共 ${Number(total || 0).toLocaleString("zh-CN")} 条</span><button class="btn sm" data-page="prev" ${page <= 1 ? "disabled" : ""}>上一页</button><span>${page} / ${Math.max(1, Math.ceil(total / pageSize))}</span><button class="btn sm" data-page="next" ${page >= Math.ceil(total / pageSize) ? "disabled" : ""}>下一页</button></div>`;
