# 2026-09-09 前端重构与功能加固记录

## 范围

本次实现以四张控制台设计稿为视觉参考，保留现有 FastAPI、SQLite、`/v1` 网关和管理 API 契约。前端改为原生模块化 HTML/CSS/JavaScript：共享 DOM 转义、API 鉴权、Toast、原生 dialog、表格分页、Canvas 图表和统一空态；页面数据全部来自接口，不写入设计稿中的示例模型、数字或域名。

## 页面结果

- 总览：服务状态、启用/冷却渠道、上架模型、请求/Token/成功率/延迟、趋势、失败 Top 和渠道健康。
- 渠道：统计范围、分配趋势、健康表、协议/模型映射/工具策略、测试、启停、探测和日志入口。
- 模型：公开模型上下架、渠道绑定、批量操作、导入和卡片/表格视图；无计费或价格 UI。
- 密钥：掩码列表、一次性创建/重置值、主动显示/复制、启停和删除。
- 日志：请求 ID/模型/渠道/状态/状态码/错误分类/日期筛选，分页详情、渠道链路、脱敏上游响应和 CSV/JSON 导出。
- 设置：管理员密码、重试与超时、失败阈值/冷却、可选探测、日志保留、清理、配置导入导出和健康信息。
- API 接入：Base URL、Key、OpenAI 兼容能力、连接测试和 cURL/Python/Cherry Studio/Continue/Open WebUI 示例。

## 后端加固

- 新增 AES-GCM 渠道密钥存储。旧明文会在初始化迁移；业务查询仍透明解密，直接 SQL 写入也会被数据库层保护。
- 新增持久化网关设置：重试次数/退避、连接/读取超时、失败阈值、冷却时长、探测开关/间隔和日志保留天数。
- 统一错误分类：`auth`、`rate_limit`、`timeout`、`upstream_5xx`、`model_not_found`、`content_filter`、`invalid_request`、`network`；保留旧分类字段兼容既有日志契约。
- 增加 request ID、健康信息、可选低成本渠道探测、按保留期清理日志、配置导入导出和日志导出脱敏。
- stream 请求使用更长的读取超时；不可安全切换的请求不会自动 failover。

## 证据与边界

```powershell
python -m unittest discover -s tests -p 'test_*.py' -q
Get-ChildItem static -Filter *.js -Recurse | ForEach-Object { node --check $_.FullName }
```

当前回归为 104 个 unittest 全部通过；另有独立 hardening 契约覆盖密钥加密、设置/日志/仪表盘接口和错误分类。隔离空数据库浏览器 smoke 已验证首次初始化、总览、渠道、模型、密钥、日志、设置与 API 接入的真实路由和空态。真实上游可用性、真实模型质量和生产部署仍需在明确授权后另行验收。

