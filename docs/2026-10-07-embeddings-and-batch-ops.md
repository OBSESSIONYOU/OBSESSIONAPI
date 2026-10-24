# 2026-10-07 embeddings 端点与渠道批量操作

## 范围

在既有 FastAPI + SQLite + 原生前端架构上继续补齐能力：新增 OpenAI 兼容的 embeddings 转发，渠道页新增批量操作。保留全部现有接口契约与协议适配。

## 1. `/v1/embeddings`

- 新增 `POST /v1/embeddings` 与重复路径 `/v1/v1/embeddings`（后者带 `X-OBSESSIONAPI-Path-Warning`）。
- 复用与对话完全一致的准入链：`_authorize_request`（Key 状态、额度、模型白名单、RPM）→ `_resolve_candidates`（模型上下架、渠道冷却/速率上限）→ 渠道级速率消费 → 熔断记录 → 用量与日志写入（`endpoint=/v1/embeddings`，`completion_tokens=0`）。
- 出网走统一的 `_http_client(channel)`，因此继承渠道代理、超时覆盖与连接池上限。
- 仅支持 `openai_chat` / `openai_responses` 协议的渠道；Anthropic / Gemini 渠道返回 400 `unsupported_protocol`（不做静默降级）。
- 本地 mock 渠道按 `sha256(model:input)` 生成确定性 16 维向量，可离线验证整条链路。
- 请求体校验 `_validate_embedding_request`：`model` 必须是字符串、`input` 必须是字符串或非空字符串数组，否则 400 `invalid_request_error`。
- usage 优先取上游 `prompt_tokens`（`usage_source=upstream`），缺失时按输入字符估算。

### 顺带重构

把 `_chat_inner` 里内联的鉴权/额度/白名单/RPM 与模型-渠道候选解析抽成 `_authorize_request` 和 `_resolve_candidates`，供对话与 embeddings 共用，避免两处逻辑漂移。

## 2. 渠道批量操作

- 新增 `POST /api/channels/batch`，`action` 支持 `probe` / `enable` / `disable` / `recover`，`ids` 去重并过滤不存在的渠道。
  - `probe`：逐个调用 `health_probe.run_probe`，返回每个渠道的 `ok` / `error_category` / `latency_ms`。
  - `enable` / `disable`：批量改 `channels.status`（手动启停语义，不影响自动禁用状态）。
  - `recover`：批量清除冷却与自动禁用。
  - 非法 `action` / 空 `ids` → 400；全部 ID 不存在 → 404；未登录 → 401。
- 渠道页新增勾选列与「批量探测 / 批量启用 / 批量停用 / 批量恢复」按钮，操作后刷新并提示成功/失败数量。

## 3. 控制台

- 「接入」页新增 embeddings 区块：Endpoint 一键复制 + 按当前 Base URL 生成的 cURL 示例，并说明协议限制。

## 验证

```powershell
python -m unittest discover -s tests -p 'test_*.py' -q   # 169 passed
Get-ChildItem static -Filter *.js -Recurse | ForEach-Object { node --check $_.FullName }
```

- `tests/test_optimizations.py` 增至 29 个用例，新增覆盖：embeddings 返回向量与 usage、确定性、入参校验、鉴权与白名单、非 OpenAI 协议拒绝、重复路径告警；渠道批量 启停/恢复、批量探测结果、非法入参与鉴权。
- 新增 1 个前端契约用例锁定 embeddings 区块与渠道批量按钮标记。
- 用 conda `tools` 环境（`python-env.txt` 指定）跑全量测试通过。

## 边界与未知项

- embeddings 只做 OpenAI 兼容转发；未实现 `/v1/rerank`、`/v1/audio/*`、`/v1/images/*`。
- 上游 embeddings 的真实维度与计费口径未在真实环境验收（无授权凭据），本地只验证了 mock 与协议契约。
- 批量探测会真实访问上游；对未授权上游不要批量触发。
