# 参与贡献

欢迎反馈问题、建议功能、改进文档或提交代码。中文和英文都可以。

1. 在 [Issues](https://github.com/deadlineiscoming/x-validator/issues) 搜索已有讨论。反馈问题或提建议时，可以使用 [Issue 表单](https://github.com/deadlineiscoming/x-validator/issues/new/choose)。安全问题请按 [安全报告说明](SECURITY.md) 私下提交。
2. Fork 仓库，从 `main` 创建分支，修改后运行下面的本地检查。涉及可见行为变化时，请一起更新相关文档。
3. 向 `main` 提交 PR，说明改动、原因和验证结果。如果 PR 解决了已有 Issue，可以在描述中写 `Fixes #编号`。

请使用虚构或脱敏示例。公开 Issue、PR 和日志中请删除 API Key、Bearer Token、微信私聊截图及个人信息。

## 本地检查

在仓库根目录运行以下命令。CI 使用 Windows、Python 3.14、Node.js 24 和 PowerShell 7。`python` 须指向已安装的 Python 解释器，Node 测试也会调用它。

```powershell
python -B -m unittest discover -s tests
node --test tests/wechat-plugin.test.mjs
```

这些测试使用本地样例，无需 API 凭证或微信扫码。CI 也会解析 PowerShell 脚本以检查语法。

提交贡献前，请阅读 [项目许可证](LICENSE) 和 [行为准则](CODE_OF_CONDUCT.md)，并确认你有权按项目许可证提交你的改动。
