import { esc } from "./dom.js";

export const pageHeader = (title, description, actions = "") => `<div class="page-header"><div><h1>${esc(title)}</h1><p>${esc(description)}</p></div><div class="page-actions">${actions}</div></div>`;
export const statusStrip = (items) => `<div class="status-strip">${items.map((item) => `<span class="status-item"><i class="status-dot ${esc(item.type || "normal")}"></i><span>${esc(item.label)}</span> <strong>${esc(item.value)}</strong></span>`).join("")}</div>`;
export const cardHeading = (title, right = "") => `<div class="card-heading"><h2>${esc(title)}</h2>${right}</div>`;
