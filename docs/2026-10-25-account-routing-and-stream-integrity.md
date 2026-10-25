# 2026-10-25 账号转发与流完整性修复

参考本地 `dsh-our-free-model-main` 2.5.9 的专用账号传输、请求级故障分类、SSE 分帧及取消边界，继续维护现有 Python 网关。参考项目文档及脚本只作为分析材料，没有执行其中的登录、推理或部署命令。保留现有数据、配置、依赖锁文件和运行服务。

## 修复行为

- Gemini Cloud Code、MiniMax、Cline 使用各自的固定请求地址、原生请求形状和 access token；不再将完整凭据 JSON 放入 Bearer。Cloud Code 使用 `request` 信封并解包 `response`，MiniMax 转 Anthropic，Cline 使用 `/api/v1/chat/completions`。刷新后保存完整凭据，临时 token 只用于本次传输。
- LobsterAI 流式请求复用同一个响应进入 relay，消除成功请求后的第二次发送与首连接泄漏。账号流异常不会补发同渠道 `stream=false` 请求。
- OpenCode 和账号非流式桥校验 SSE 错误、JSON、有效输出与终态；错误或截断不会伪造 `stop` 成功。不支持的账号协议参数明确拒绝，避免静默丢失语义。
- 通用后台推理测试使用上述专用传输，不写用量日志。通用模型列表探测、协议检测及模型发现对账号渠道明确返回不适用，避免发送错误鉴权；仍可使用账号页面的清单或手动配置模型。
- Chat/Anthropic 流按 SSE 事件拼接，支持 `data:` 后无空格、BOM、CRLF 和多行 data；坏帧及显式错误终止流并返回错误事件。文本尾缓冲在 finish 前输出。取消按 `499/client_disconnected` 记录，不恢复也不累计熔断，取消期间关闭响应及客户端。
- 请求参数错误与内容过滤不累计渠道故障；认证及渠道配置故障继续保留现有冷却行为。
- 渠道候选在实际尝试前再次原子检查并消费 RPM，避免并发故障切换超限。embeddings 与 Chat 共享 Key 非流式并发预算，取消及异常后释放槽位。
- `Retry-After` 支持标准 HTTP 日期，正数等待时间向上取整，避免小数秒提前重试。

## 验证与范围

修改前原有 243 项 unittest 全部通过。本轮新增回归使用合成凭据、MockTransport、临时 SQLite 和 localhost HTTP 上游，先验证失败，再验证修复。最终测试结果见本次交付说明。

本轮不等同于真实供应商验收：没有登录真实账号或调用真实上游推理，也未重启现有 8000 服务。加载修改需重启对应实例，再以实际渠道进行推理验证；账号有效期、可用模型及供应商策略仍由实际服务决定。

普通 API 渠道原有的“流在输出前失败后尝试非流式”兼容策略仍保留；已有输出之后不会切换或重放。渠道 RPM 与并发状态为进程内限制，多进程实例不共享窗口。Qoder/TRAE 仍是原有骨架，不属于本轮已接通协议。

参考源主要为 `src/route/classify.js`、`src/http.js`、`src/adapter.js`、`src/core/completion.js`、`src/stream.js` 与 vendor channel pack；许可见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
