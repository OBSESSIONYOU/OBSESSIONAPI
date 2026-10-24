const TOKEN_KEY = "obs_token";
let token = localStorage.getItem(TOKEN_KEY) || "";
let onUnauthorized = () => {};

export function setUnauthorizedHandler(handler) { onUnauthorized = handler; }
export function getToken() { return token; }
export function setToken(value) {
  token = value || "";
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export async function api(path, options = {}) {
  const headers = { ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(options.headers || {}) };
  const response = await fetch(`/api${path}`, { ...options, headers });
  if (response.status === 401) {
    setToken("");
    onUnauthorized();
    throw new Error("登录已失效");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data?.detail || data?.message || data?.error?.message || `请求失败（${response.status}）`;
    const error = new Error(String(message)); error.status = response.status; error.data = data; throw error;
  }
  return data;
}

export async function publicJSON(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(String(data?.detail || data?.message || `请求失败（${response.status}）`));
  return data;
}

export async function download(path, filename) {
  const response = await fetch(path.startsWith("/") ? path : `/api${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (response.status === 401) { setToken(""); onUnauthorized(); throw new Error("登录已失效"); }
  if (!response.ok) throw new Error(`下载失败（${response.status}）`);
  const link = document.createElement("a"); link.href = URL.createObjectURL(await response.blob()); link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
