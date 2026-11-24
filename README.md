# X Validator

在已绑定的个人微信 ClawBot 私聊发一张 X 截图。找到候选并成功截图时，程序先发候选帖截图，再回复原帖比对、账号可信度和独立事实核验。事实核验列出来源名称。可以一起发原帖链接，也可以随后补发，或追问「原帖在哪」「证据是什么」。

架构、比对规则和验收范围见 [X Validator 微信截图核验设计](docs/design.md)。

## Windows 上运行微信助手

在 Windows 上克隆仓库后，从项目目录运行一键部署脚本。Windows PowerShell 和 PowerShell 7 均可作为入口。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Deploy-XValidator.ps1
```

脚本检查 PowerShell 7、Python 3.10 或更新版本、受支持的 Node.js LTS 和 Chrome 或 Edge。缺少可用的系统依赖时会调用 WinGet，再安装项目运行时。首次运行按提示输入 X App-only Bearer Token 和 DeepSeek API Key，并用微信扫码。凭证输入时遮盖，以当前 Windows 用户加密保存。就绪后网关在后台运行，只监听本机 `18647` 端口。重复执行会复用已有凭证和微信授权。

若只需准备运行时和本机配置，在同一条命令后加 `-PrepareOnly`。程序配置、凭证、微信状态和运行日志保存在 `%LOCALAPPDATA%\X Validator`。在 PowerShell 7 中可用 `./Start-XValidator.ps1 -Action Status` 查看微信通道状态。

发图时，每条消息发送一张清晰截图。推文中涉及的独立来源不足时，回复会标明证据不足。最近一次核验结果在内存中保留 30 分钟，供同一私聊补链接和追问。X 查询与 DeepSeek 服务按各自套餐计费。设置环境变量 `X_VALIDATOR_ARCHIVE=0` 可以关闭全量搜索。

截图显示完整的发帖日期和钟点时，优先在该账号的时间范围内查帖，再比对可见正文。原文短语搜索和账号时间搜索可以互为补充。X 嵌入帖的时间也可能在正文下方，程序会区分它和顶部被回复帖的日期。

发 X 译文截图时，尽量保留 `@账号`、发帖时间和页面上的翻译标记。外层账号被截断时，程序可以用同一作者在引用卡片上显示的完整账号补全，再按账号和时间范围查一页候选。已有原帖链接时直接查询。否则，首轮缺少完整账号或译文缺少可用的发帖时间时，额外识别一次主帖和引用卡片的标头。相对时间优先结合可识别的手机状态栏时钟估算。回复只展示排名第一的候选，由后台浏览器截取 X 官方嵌入页。嵌入页无法显示时，回复注明截图不可用并附该候选的正文节选。

账号可信度根据 X 公开资料和可核对的站外链接评估身份线索。蓝标、注册时间和关注者数量只提供背景。完整 `@账号` 可单独查询，即使暂时找不到原帖。程序优先复用查帖返回的作者资料，必要时补一次 X 账号查询，另最多一次站外检索或抓取。帖内说法由独立事实核验判断。

## 在本机验证 X API

在项目目录打开 PowerShell。使用 Python 3.10 或更新版本，运行下面的命令，把示例 ID 换成自己能打开的公开原帖链接或帖子 ID。

```powershell
python .\x_validator.py lookup '1460323737035677698'
```

终端会提示输入 **App-only Bearer Token**，输入时不会回显。也可以通过 `X_BEARER_TOKEN` 环境变量提供凭证。

有账号和帖子文字片段时，可搜索最近 7 天。查询更早的帖子时加 `--archive`。

```powershell
python .\x_validator.py search '@XDevelopers' '一小段原文'
python .\x_validator.py search '@XDevelopers' '一小段原文' --archive
```

搜索最多返回首批 10 条匹配。X API 按返回的资源计费，收费以开发者控制台为准。无匹配时返回空列表，原帖仍待确认。

相关文档见 [X 按 ID 查帖](https://docs.x.com/x-api/posts/get-post-by-id)、[搜索公开帖子](https://docs.x.com/x-api/posts/search/introduction)和 [X API 计费](https://docs.x.com/x-api/getting-started/pricing)。
