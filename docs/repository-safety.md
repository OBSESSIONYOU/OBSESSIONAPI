# 仓库安全维护

Dependabot 每周检查依赖和 GitHub Actions；漏洞提醒和安全修复 PR 已开启。更新 PR 通过现有测试及 Privacy Guard 后，由维护者检查并合入，不自动批准或合并。

本机新克隆仍需执行 `python scripts/privacy_guard.py install`。本机钩子负责上传前拦截，CI 在对象上传后检查，分支保护只约束正式分支的合入，不能代替上传前检查。

发布分支启用分支保护时，使用 PR 合入并等待要求的检查通过；维护者无需第二个账号批准自己的 PR。自动更新也走更新分支和 PR。禁止强推和删除发布分支。

## Python 依赖锁定

直接依赖维护在 `requirements.in` 和 `requirements-dev.in`。对应 `.txt` 文件是包含精确版本和 SHA-256 的完整跨平台锁定结果，支持 Python 3.10+；原有 pip、Docker 和 Windows 启动入口继续使用这两个文件。pip 会自动对带哈希的文件启用哈希检查。

需要重新解析依赖时，使用 uv 0.12.9 或兼容版本：

```powershell
uv pip compile --universal --python-version 3.10 --generate-hashes requirements.in -o requirements.txt
uv pip compile --universal --python-version 3.10 --generate-hashes -c requirements.txt requirements-dev.in -o requirements-dev.txt
```

更新后重新运行测试和漏洞检查；不要直接用当前电脑的 pip freeze 覆盖跨平台锁文件。
