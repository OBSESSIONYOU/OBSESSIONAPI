# OBSESSIONAPI

个人自用的 AI API 聚合网关与本地控制台：FastAPI + SQLite 后端，原生 HTML/CSS/JavaScript 前端，无构建步骤、无 CDN、无前端依赖。客户端使用一个 Base URL、一个 API Key 和统一模型名，网关负责渠道探测、映射、重试、冷却、故障分类与日志追踪。

## 控制台

| 页面 | 用途 |
| --- | --- |
| 总览 | 服务、渠道、模型、请求、Token、延迟（均值/P95/P99）、趋势、模型健康、待处理问题 |
| 渠道 | 上游地址、协议识别、模型映射、优先级/权重、代理与超时覆盖、速率上限、批量探测/启停/恢复、真实测试、探测、自动禁用与恢复 |
| 模型 | 公开模型上下架、渠道绑定、批量操作与空态管理 |
| 密钥 | 创建、重置、显示、复制、禁用和删除客户端 Key；模型白名单与 RPM/并发限流；列表只返回掩码值 |
| 日志 | 请求 ID、模型、渠道、状态码、错误分类、日期筛选、详情、脱敏导出 |
| 设置 | 管理员密码、重试/退避/超时/熔断、自动禁用与恢复、响应缓存、模型鉴权、探测、日志保留、配置导入导出 |
| API 接入 | Base URL、Key、兼容协议、embeddings 示例、cURL/PowerShell/Python/Cherry Studio/Continue/Open WebUI 示例 |

控制台不提供账单、收费、用户/角色、配额或金额工作流；Token、耗时和请求日志只用于个人运维诊断。没有默认用户、模型、渠道、Key 或演示流量，所有页面数据来自 SQLite 和真实请求。

## 快速开始

Windows 下双击 `OBSESSIONAPI.bat` 即可一键启动：脚本会自动选择已装好依赖的 Python（都没有时在项目内建 `.venv` 并安装）、检查 8000 端口占用、在独立窗口启动服务，**轮询 `/health` 直到服务真正就绪后再自动打开浏览器**（未登录时直接落在登录界面，首次使用先进入创建管理员账户）。

要指定用哪个 Python 解释器（例如 conda 的某个环境），在项目根目录的 `python-env.txt` 里写出完整路径即可，脚本会优先使用它，缺少依赖时**只安装到该环境**，不会写入 conda `base`：

```text
C:\Users\你的用户名\anaconda3\envs\你的环境名\python.exe
```

留空或删除 `python-env.txt` 会回退到自动探测。

手动启动：

```powershell
pip install -r requirements.txt
python run.py
```

打开 <http://127.0.0.1:8000>，首次使用创建管理员账户。健康检查：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health
```

也可以使用 Docker Compose：

```powershell
docker compose config
docker compose build
docker compose up -d
docker compose logs -f obsessionapi
```

Compose 只把宿主机本地端口映射到容器，并把现有 `data` 目录挂载到 `/app/data`。需要 CORS 或固定密钥根密钥时，复制 `.env.example` 为 `.env`；不要把 API Key、密码或密钥写入仓库。

## 网关调用

在“渠道”页添加真实上游，在“模型”页上架公开模型，在“密钥”页创建客户端 Key。控制台“API 接入”页会要求明确选择协议、模型和有效网关 Key，再生成对应配置：

支持的客户端协议入口：OpenAI Chat 使用 /v1/chat/completions，OpenAI Responses 使用 /v1/responses，Anthropic Messages 使用 /v1/messages，Gemini 使用 /v1beta/models/{model}:generateContent。Responses SDK 使用 client.responses.create(...)；Anthropic SDK 使用 client.messages.create(...)。Anthropic 软件若会自动追加 /v1/messages，Base URL 填根地址，不要填 /v1。

文本向量化使用 OpenAI 兼容的 `POST /v1/embeddings`（`{"model": "...", "input": ["..."]}`），复用同一套鉴权、模型白名单、限流、熔断与用量记录；仅支持 OpenAI 兼容渠道，Anthropic / Gemini 渠道返回 400 `unsupported_protocol`。本地 mock 渠道会按输入文本生成确定性向量，便于离线验证链路。

网关对非流式 HTTP 200 响应仍会校验是否包含有效文本、工具调用、function call 或 refusal；只有 reasoning/thinking 或空 error 对象的响应会记录为 protocol_error，并在日志中关联 request ID、公开/上游模型、渠道、上游状态与网关状态。

Responses、Anthropic、Gemini 的函数工具调用及结果回传会转换到统一对话格式，并保留并行调用 ID。Responses 流包含输出项的开始、增量、结束事件与完整终态；输出预算耗尽返回 `incomplete`，不会把正常结束误判为渠道失败。跨协议工具结果目前支持文本；Responses 的 `previous_response_id` / `conversation` 服务端状态及内建工具暂不支持，会明确拒绝，调用方应传完整历史。详见 [2026-10-22 协议修复与验证记录](docs/2026-10-22-protocol-streaming-and-tools.md)。

账号通道（Gemini Cloud Code、MiniMax、Cline、LobsterAI）使用专用传输，OpenCode 非流式桥会校验流终态；SSE 异常、客户端取消、故障切换限额和 embeddings 并发的修复范围见 [2026-10-25 修复记录](docs/2026-10-25-account-routing-and-stream-integrity.md)。账号接线已用离线协议夹具验证，真实登录与供应商推理需单独验收。

```powershell
$headers = @{ Authorization = "Bearer $env:OBSESSION_KEY"; "Content-Type" = "application/json" }
$body = (@{ model = "你的模型名"; messages = @(@{ role = "user"; content = "你好" }) } | ConvertTo-Json -Depth 8)
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/chat/completions" -Method Post -Headers $headers -Body $body
```

当前支持自动识别、OpenAI Chat、OpenAI Responses、Anthropic Messages、Gemini 和本地 mock 渠道。上游密钥在 SQLite 中以 AES-GCM 密文保存；旧版明文渠道会在数据库初始化时迁移。网关继续提供 `/v1/models`、`/v1/chat/completions` 和兼容的 `/v1/completions`，流式响应沿用现有协议适配。

## 配置与安全

- 未设置 `OBSESSIONAPI_SECRET_KEY` 时，首次启动生成 `data/obsessionapi.secret`；建议生产环境显式设置长随机值并妥善备份。
- `GET /health` 返回服务、数据库可写性、启用/冷却/自动禁用渠道和近一小时错误率，并附带 `X-Request-ID`。
- 默认重试 3 次、退避上限 10000 毫秒（指数 + 25% 抖动）、连接超时 5 秒、读取超时 120 秒、失败阈值 3 次、冷却 300 秒、冷却累计 3 次自动禁用、1800 秒后自动恢复、日志保留 30 天；设置页可持久化修改。
- 熔断状态写入 `channel_states` 表，重启后冷却与自动禁用不丢失；后台任务按 `auto_recover_seconds` 自动恢复，也可在渠道页手动「恢复」。
- 渠道支持独立代理（`http/https/socks5`）、连接/读取超时覆盖、连接池并发上限与速率上限；代理凭据在列表与导出中脱敏。
- 渠道达到自身速率上限时自动跳过，全部达限返回 429 `channel_rate_limited`；客户端断开连接后不再继续重试。
- 客户端 Key 可配置模型白名单、每分钟请求上限与非流式并发上限，命中限流返回 429（`rate_limit_exceeded` / `concurrency_limit_exceeded`）。
- `GET /v1/models` 默认无需 Key（保持兼容）；携带有效 Key 时按其模型白名单过滤；开启 `models_require_key` 后必须提供有效 Key。
- 可选的非流式响应缓存只缓存显式指定数值 `temperature=0` 的成功响应，命中时返回 `X-Cache: HIT` 且不计额度；省略温度不缓存，默认关闭。
- 认证失败、限流、超时、上游 5xx、模型不存在、内容过滤、请求无效、网络、协议错误和工具不支持分别分类；不可安全切换的请求不会盲目重试。
- API 响应、日志详情、配置导出和 CSV/JSON 导出会移除上游 Key、管理 Key、成本字段和用户标识；配置导出默认不含渠道密钥。
- 后台渠道探测默认关闭；开启前请确认上游允许模型列表探测。

## 备份、升级与恢复

停服务后备份整个 `data` 目录，确保 SQLite 主库和 WAL 文件一致：

```powershell
docker compose down
$stamp = Get-Date -Format yyyyMMdd-HHmmss
New-Item -ItemType Directory -Force backups | Out-Null
Copy-Item -Recurse -Force data (Join-Path backups "data-$stamp")
```

升级后继续挂载同一个 `data` 目录即可；数据库初始化会补齐新增表、索引和字段。删除 `data/obsessionapi.db` 后重启会回到首次初始化页面，不会重新生成演示数据。

## 验证

```powershell
python -m unittest discover -s tests -p 'test_*.py' -q
Get-ChildItem static -Filter *.js -Recurse | ForEach-Object { node --check $_.FullName }
```

当前测试覆盖旧版兼容契约、前端模块契约，以及渠道密钥加密、熔断持久化与自动禁用/恢复、Key 模型白名单与限流、渠道代理与渠道级限流、指数退避、`/v1/models` 鉴权与白名单、`/v1/embeddings` 转发与校验、渠道批量操作、响应缓存、分位统计、日志索引、设置/日志/仪表盘接口和错误分类。硬件、真实上游可用性、真实代理连通性和实际模型质量仍需在用户明确授权后用真实环境单独验收。

## 结构

```text
OBSESSIONAPI.bat      # Windows 一键启动（GBK 编码，含中文提示）
python-env.txt        # 可选：指定使用的 Python 解释器路径（如 conda 环境）
run.py                # 手动启动入口
app/
  main.py             # FastAPI 装配、健康检查、静态资源、启动维护、自动恢复任务
  database.py         # SQLite 建表、迁移、密文读写兼容
  secrets.py          # AES-GCM 渠道密钥保护
  settings_store.py   # 持久化网关设置
  gateway.py          # 路由、重试、冷却、自动禁用、限流、代理、缓存、协议适配
  health_probe.py     # 可选渠道探测
  admin_api.py        # 控制台 API、日志、配置导入导出
static/
  index.html
  style.css
  app.js
  js/                 # 页面模块与共享 DOM/API/图表工具
tests/                # unittest 回归与 hardening/优化契约
```
