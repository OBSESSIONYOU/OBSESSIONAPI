# 2026-09-27 网关韧性、权限限流、代理与缓存加固记录

## 范围

在既有 FastAPI + SQLite + 原生前端架构上继续加固聚合网关，参考 one-api / new-api 的常见做法补齐四类能力；保留全部现有接口契约、`/v1` 协议适配与控制台结构，不引入计费、用户角色或第三方依赖。

## 1. 渠道熔断持久化与自动禁用/恢复

- 新增 `channel_states` 表（`streak`、`cooldown_until`、`cooldown_count`、`auto_disabled`、`disabled_reason`、`disabled_at`、`updated_at`）。
- `_breaker_mark` 在更新进程内 `_BREAKER` 热缓存的同时写穿数据库；`load_breaker_state()` 在启动时恢复未过期冷却与自动禁用状态，**重启不再丢失熔断**。
- 连续失败达 `fail_threshold` 进入冷却；冷却累计达 `auto_disable_after`（默认 3）升级为「自动禁用」，从路由候选中排除（`_breaker_unavailable`）。
- 后台 `auto_recover_loop` 每 30 秒检查，自动禁用超过 `auto_recover_seconds`（默认 1800）的渠道自动恢复；新增 `POST /api/channels/{cid}/recover` 支持手动恢复。
- 手动禁用（`channels.status`）与自动禁用保持分离，语义互不覆盖。
- 控制台：渠道页新增「已自动禁用」状态、失败/冷却/恢复倒计时与「恢复」按钮；总览与设置页展示自动禁用渠道数。

## 2. Key 级模型权限与限流

- `api_keys` 新增 `allowed_models`（逗号分隔白名单，空=全部）、`rpm_limit`（每分钟请求数，0=不限）、`concurrency_limit`（非流式并发，0=不限）。
- 网关在鉴权后按序执行：模型白名单（403 `model_not_allowed`）→ RPM 滑动窗口（429 `rate_limit_exceeded`）→ 非流式并发准入（429 `concurrency_limit_exceeded`，`_chat` 包装层用 try/finally 释放）。
- 控制台密钥页可编辑白名单与两个上限，并在列表展示限制摘要。

## 3. 渠道代理与网络增强

- `channels` 新增 `proxy_url`（http/https/socks5/socks4）、`connect_timeout_s`、`read_timeout_s`、`max_concurrency`。
- 统一出网入口 `_http_client(channel, stream)`：按渠道应用代理、超时覆盖与 `httpx.Limits` 连接池上限；网关、渠道测试、模型发现、协议探测与后台探测全部改走该入口。
- 代理地址经 `_parse_proxy_url` 校验；列表与配置导出用 `_mask_proxy` 脱敏凭据。
- 控制台渠道表单新增「网络与代理」高级设置，含清除代理选项。

## 4. 响应缓存与统计分位

- 新增设置 `cache_enabled`（默认关）、`cache_ttl_seconds`（默认 300）；仅缓存非流式、`temperature=0` 的成功响应，命中返回 `X-Cache: HIT`，不计额度、按 `usage_source=synthetic` 记录。
- `_rollup_totals` 增加 `p95/p99/max` 延迟；`/api/dashboard` 增加 `metrics.p95_latency_ms/p99_latency_ms/max_latency_ms`、`status_strip.auto_disabled_channels` 与按模型的 `model_health`。
- 控制台总览新增模型健康表与延迟分位展示。

## 附带修复

- 修复真实缺陷：客户端发送 `{"model": [...]}` 或 `{"messages": ["hi"]}` 时网关 500 崩溃。新增 `_validate_chat_request`，在鉴权后、模型查询前返回 400 `invalid_request_error`。原 `test_hardening.py` 中该用例此前为红灯。

## 新增设置项

`auto_disable_after`(3)、`auto_recover_seconds`(1800)、`cache_enabled`(False)、`cache_ttl_seconds`(300)。

## 验证

```powershell
python -m unittest discover -s tests -p 'test_*.py' -q   # 152 passed
Get-ChildItem static -Filter *.js -Recurse | ForEach-Object { node --check $_.FullName }
```

- 新增 `tests/test_optimizations.py`（13 个用例）覆盖熔断持久化、自动禁用/恢复、Key 白名单/RPM/并发、代理校验与脱敏、`_http_client` 代理注入、缓存命中/跳过、分位与模型健康、`/health` 自动禁用计数。
- 新增 1 个前端契约用例锁定恢复/限流/代理/缓存/分位 UI 标记。
- 用真实 `data/obsessionapi.db` 副本验证迁移：新增列与 `channel_states` 均补齐，`load_breaker_state` 正常。

## 未知项与边界

- 真实上游、真实 SOCKS 代理连通性与实际模型质量未在真实环境验收（无授权凭据）。
- 缓存仅覆盖非流式确定性请求；流式请求不做缓存。
- Key 并发上限只作用于非流式请求（流式响应在 `_chat` 返回后仍持续，无法安全内联释放），已在设置页文案标注。

---

# 第二轮：路由精细化、模型鉴权与查询性能

## 1. `/v1/models` 鉴权与白名单

- 携带有效网关 Key 时，`/v1/models` 按其 `allowed_models` 白名单过滤（与网关调用权限一致）。
- 新增设置 `models_require_key`（默认关闭，保持既有匿名可访问契约）；开启后无有效 Key 返回 401。
- 被禁用的 Key / 用户访问时返回 403 `permission_denied`。

## 2. 渠道级速率上限（保护上游）

- `channels` 新增 `rpm_limit`（次/分，0=不限）。路由候选阶段用只读检查跳过已达上限的渠道，选中时才消费窗口配额。
- 模型全部渠道达限时返回 429 `channel_rate_limited` 并带 `Retry-After: 60`，与 `channels_cooling`、`no_available_channel` 区分。
- 控制台渠道表单新增「渠道速率上限」；`/api/routes` 规则说明同步更新。

## 3. 指数退避 + 抖动

- `_retry_pause` 由线性改为 `base * 2^attempt`，叠加 ±`retry_backoff_jitter_pct`% 抖动，并以 `retry_backoff_max_ms` 封顶（避免重试风暴与惊群）。
- 新增设置 `retry_backoff_max_ms`(10000)、`retry_backoff_jitter_pct`(25)。`retry_backoff_ms=0`（默认）时不引入任何延迟，保持既有行为。

## 4. 客户端断连停止重试

- 每次重试前检查 `request.is_disconnected()`；客户端已断开则返回 499 `client_disconnected`，不再发起新的上游请求，避免重试放大上游消耗。
- 检查用 try/except 包裹，任何异常都按"未断开"处理，不影响正常转发。

## 5. 日志查询索引

- 新增 `idx_logs_request_id`、`idx_logs_category`、`idx_logs_channel_created`、`idx_logs_status_created`，覆盖日志详情（按 `request_id` 取尝试链）、错误分类筛选与渠道+时间范围聚合。
- 索引在 `executescript(SCHEMA)` 中以 `IF NOT EXISTS` 创建，存量库启动即补齐。

## 验证（第二轮）

- `tests/test_optimizations.py` 增至 20 个用例，新增覆盖：`/v1/models` 白名单过滤与强制鉴权、渠道级限流跳过与 429、渠道速率上限可配置可见、指数退避的取值与抖动区间、日志索引存在性。
- 前端契约用例新增 `f-rpm`/`rpm_limit`/`retry_backoff_max_ms`/`retry_backoff_jitter_pct`/`models_require_key` 标记断言。
- 真实 `data/obsessionapi.db` 副本再次验证迁移：`rpm_limit` 列与四个新索引均补齐。

## 边界（第二轮）

- 渠道级限流为进程内滑动窗口，重启后清零；多进程部署时不共享配额。
- 断连检查只在重试前生效，不中断进行中的上游请求（避免与流式 relay 的清理逻辑耦合）。

