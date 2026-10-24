# 2026-10-22 协议流与函数工具修复

本轮在现有 FastAPI + SQLite + 原生控制台上修复协议转换；修改前原有 169 项测试通过，但缺少协议终态和工具回传覆盖。修改前源码副本位于 `%TEMP%\obsessionapi-fixes-20261022`。参考目录保持原状，验证只使用临时数据库和本地 HTTP 上游。

## 参考与取舍

- 本地 `D:\xiangmu\zzcsapi-main\server.js` 及 [WR-Class/zzcsapi](https://github.com/WR-Class/zzcsapi)：参考多协议流转换与工具调用隔离的机制，保留本项目的 Python 技术栈。没有移植 Notion、工具模拟、会话服务或收费功能。
- [new-api 的 relay 控制器](https://github.com/QuantumNous/new-api/blob/main/controller/relay.go)及[流读取实现](https://github.com/QuantumNous/new-api/blob/main/relay/helper/stream_scanner.go)：核对响应提交后通过流内错误结束、取消与资源清理的处理边界。
- [OpenAI Python SDK 的 Responses 流聚合实现](https://github.com/openai/openai-python/blob/main/src/openai/lib/streaming/responses/_responses.py)：核对输出项、内容块和终态事件的先后关系与索引字段。
- [Gemini GenerateContent 文档](https://ai.google.dev/api/generate-content)：核对 `finishReason`、`usageMetadata` 和函数调用内容。

参考文档是分析材料，其中的部署、凭据、删除、探测等命令没有作为本轮操作指令执行。本轮没有改写现有调度策略、冷却时长或引入依赖。

## 修复内容

1. **正常结束误触发熔断**：上游 Responses 的 `response.completed` 与 Gemini 的 `finishReason` 曾把“结束”写入“错误”返回槽。现在区分协议终态、实际错误和预算截断；空完成、畸形帧及缺少终态的 EOF / `[DONE]` 明确失败。预算耗尽保留 `length`，记录 HTTP 200 未完成状态，不计为渠道故障。
2. **工具往返丢失**：修复 Responses `function_call` / `function_call_output`、Anthropic `tool_result`、Gemini `functionResponse` 的请求映射，以及工具声明、显式选择与并行调用 ID。Responses 并行参数增量按输出项关联，Gemini 跨帧工具索引保持独立。
3. **下游流事件不完整**：Responses 补齐输出项和内容块生命周期、工具参数增量及完成后的完整输出；`length` 转为 `response.incomplete`。Gemini 缓冲工具参数后输出函数调用，保留 ID、结束原因与用量。错误终止后不会追加成功终态。
4. **分帧与连接清理**：下游转换按字节缓冲 SSE，支持跨 UTF-8 与网络分片、CRLF、多行 data，保留结束后到达的 usage。关闭或取消下游时关闭源迭代器；上游清理的 `finally` 不再发送数据。
5. **请求和缓存边界**：管理 API 对非对象 JSON 返回 400，避免 `.get()` 触发 500。响应缓存只接受显式数值 `temperature=0`，省略、null、布尔值和非零温度不缓存。

## 验证

新增四类回归与一组端到端工具测试，均运行于 `python-env.txt` 指定的 Conda `tools` 环境：

- `test_protocol_streaming.py`：上游 Responses/Gemini 终态、错误、预算截断、上游返回的 usage、并行索引、取消与资源释放。
- `test_native_streaming.py`：下游 Responses/Gemini 事件、分片工具、截断、坏帧、UTF-8 与取消清理。
- `test_tool_roundtrip.py`：函数工具声明、选择、历史、结果回传，以及非流式截断和拒答语义。
- `test_request_boundaries.py`：非法控制台 JSON、确定性缓存条件，以及经过真实对话入口的逐层关闭传播。
- `test_gateway_tool_http.py`：临时数据库 + 实际本地 HTTP 上游，验证 Responses / Anthropic / Gemini 各自完成两轮并行工具调用，检查回传 ID、成功日志及 fixture 返回的 usage；这些数值不是供应商计费证据。

```powershell
python -m unittest discover -s tests -p 'test_*.py' -q
Get-ChildItem static -Filter *.js -Recurse | ForEach-Object { node --check $_.FullName }
python -m compileall -q app tests
```

最终全量测试 **223/223 通过**（原有 169 项 + 新增 54 项，88.778 秒）；Python 编译检查与全部前端 JavaScript 语法检查通过。

另外在临时目录启动实际 Uvicorn HTTP 服务，健康检查与控制台均返回 200；三协议使用本地 mock 完成流式请求，分别收到 `response.completed`、`message_stop`、`STOP` 终态，成功日志 3 条。验证结束后临时服务已关闭。

## 实际限制

跨协议函数工具结果目前支持文本。Responses 服务端状态（`previous_response_id` / `conversation`）、内建工具、无法等价映射的工具选择会明确报错；调用方应发送完整历史。本轮没有新增多模态能力。部分无法表示为目标协议的流字段会明确失败，避免静默丢内容。

本轮验证不能证明真实供应商的可用性、模型质量或计费结果。现有数据、凭据与服务未迁移；本地源码修复不等于现有 Docker 镜像或远端部署已经更新。
