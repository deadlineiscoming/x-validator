# X Validator

在已绑定的个人微信 ClawBot 私聊发一张 X 截图。找到候选并成功截图时，程序先发候选帖截图，再回复原帖比对、账号可信度和独立事实核验。事实核验列出来源名称。可以一起发原帖链接，也可以随后补发，或追问「原帖在哪」「证据是什么」。

架构、比对规则和验收范围见 [X Validator 微信截图核验设计](docs/design.md)。

欢迎反馈问题或提交 PR，参与方式见 [贡献指南](CONTRIBUTING.md)。

## 使用许可

本项目采用**源码可见、限非商业用途**的许可方式。按许可证条款，你可以为非商业目的使用、修改和分发本仓库代码。商业用途需另行取得权利人授权。具体允许的用途以许可证全文为准。 [PolyForm Noncommercial License 1.0.0](LICENSE)

## 免 Key 离线演示

克隆或解压仓库后，在浏览器中打开 `docs/demo/index.html`，点击「用示例试一次」。页面用虚构帖子和固定证据依次展示候选截图、原帖比对、账号评估和事实核验。演示只读取仓库内的文件，无需安装依赖或注册账号。

Windows 上也可从仓库目录打开演示。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Deploy-XValidator.ps1 -Action Demo
```

## 部署前准备 X API 权限

1. 使用已开通开发者访问权限的 X 账号，在 X Developer Console 创建自己的应用，并从该应用的凭证页取得 **App-only Bearer Token**。部署脚本会提示输入该令牌。 [X Developer Console](https://console.x.com/) · [X API 快速入门](https://docs.x.com/make-your-first-request)
2. 应用需要能读取公开帖子、按用户名查公开账号，以及搜索最近 7 天的帖子。这些操作使用只读应用凭证。 [X App-only 认证](https://docs.x.com/fundamentals/authentication/oauth-2-0/application-only)
3. 要按截图搜索更早的帖子，应用还需能调用 **Full-Archive Search**。只需近期搜索时，可在启动网关前设置 `X_VALIDATOR_ARCHIVE=0`。X API 的用量和额度在开发者控制台查看。 [搜索范围](https://docs.x.com/x-api/posts/search/introduction) · [X API 计费](https://docs.x.com/x-api/getting-started/pricing)

## Windows 上运行微信助手

在 Windows 上克隆仓库后，从项目目录运行一键部署脚本。Windows PowerShell 和 PowerShell 7 均可作为入口。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Deploy-XValidator.ps1
```

脚本检查 PowerShell 7、Python 3.10 或更新版本、受支持的 Node.js LTS 和 Chrome 或 Edge。缺少可用的系统依赖时会调用 WinGet，再安装项目运行时。首次配置时，向导会提示输入 X App-only Bearer Token、选择模型与取证厂商，再用微信扫码。默认可用一把 DeepSeek Key。凭证输入时遮盖，以当前 Windows 用户加密保存。就绪后网关在后台运行，只监听本机 `18647` 端口。重复执行会复用已有凭证和微信授权。

向导可以跳过在线自检。选择运行自检时，程序会用内置虚构截图、固定查询和示例网址测试凭证、模型能力和接口权限，厂商可能会按用量扣费。拥有 X 全历史搜索权限时，可以在向导里启用它。

若只需准备运行时和本机配置，在同一条命令后加 `-PrepareOnly`。程序配置、凭证、微信状态和运行日志保存在 `%LOCALAPPDATA%\X Validator`。

### 管理本机服务

| PowerShell 7 命令 | 用途 |
| --- | --- |
| `./Deploy-XValidator.ps1 -Action Configure` | 重新打开配置向导。按提示选择新厂商、模型或凭证 |
| `./Deploy-XValidator.ps1 -Action Doctor` | 用当前配置运行在线自检，可能产生 API 用量 |
| `./Deploy-XValidator.ps1 -Action Status` | 查看网关、微信状态、运行中的 API 路线和最近一次核验统计 |
| `./Deploy-XValidator.ps1 -Action Stop` | 停止当前仓库管理的本机网关 |
| `./Deploy-XValidator.ps1 -Action Start` | 启动后台网关。首次启动按提示扫码 |
| `./Deploy-XValidator.ps1 -Action Restart` | 以新配置重启后台网关 |

统计只在本机保留最近一次核验的耗时、是否完成以及 X、模型和网页取证请求次数。请求次数包含失败尝试，计费以厂商账单为准。网关异常终止时，未取得的请求统计会显示为不可用。

发图时，每条消息发送一张清晰截图。推文中涉及的独立来源不足时，回复会标明证据不足。最近一次核验结果在内存中保留 30 分钟，供同一私聊补链接和追问。X、模型和网页取证服务分别按所用厂商的规则计费。

截图显示完整的发帖日期和钟点时，优先在该账号的时间范围内查帖，再比对可见正文。原文短语搜索和账号时间搜索可以互为补充。X 嵌入帖的时间也可能在正文下方，程序会区分它和顶部被回复帖的日期。

发 X 译文截图时，尽量保留 `@账号`、发帖时间和页面上的翻译标记。外层账号被截断时，程序可以用同一作者在引用卡片上显示的完整账号补全，再按账号和时间范围查一页候选。已有原帖链接时直接查询。否则，首轮缺少完整账号或译文缺少可用的发帖时间时，额外识别一次主帖和引用卡片的标头。相对时间优先结合可识别的手机状态栏时钟估算。回复只展示排名第一的候选，由后台浏览器截取 X 官方嵌入页。嵌入页无法显示时，回复注明截图不可用并附该候选的正文节选。

账号可信度根据 X 公开资料和可核对的站外链接评估身份线索。蓝标、注册时间和关注者数量只提供背景。完整 `@账号` 可单独查询，即使暂时找不到原帖。程序优先复用查帖返回的作者资料，必要时补一次 X 账号查询，另最多一次站外检索或抓取。帖内说法由独立事实核验判断。

## AI 与网页取证

默认用同一个 DeepSeek Key。`deepseek-flash` 识图并初判证据，复杂、冲突或需要确认的反驳交给 `deepseek-v4-pro` 复核。困难截图定向复读默认用 Flash 并提高图片细节。网页取证默认使用 DeepSeek Search/Fetch，也可切换到 Kimi API Platform 的 Web Search Basic 和 URL Fetch。查 X 原帖继续使用 X API。 [Kimi 网页搜索](https://platform.kimi.ai/docs/api/tools-search) · [Kimi 网页抓取](https://platform.kimi.ai/docs/api/tools-fetch)

两条模型路线可选同一家厂商，也可混搭。识图模型需支持图像输入和结构化 JSON 输出，文字复核模型需支持结构化 JSON 输出。模型名称按自己的厂商控制台填写。

| 厂商预设 | 额外配置 |
| --- | --- |
| `deepseek` | 默认 API 地址和模型。 [DeepSeek 文档](https://api-docs.deepseek.com/) |
| `openai` | 填所用模型名称。 [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) |
| `gemini` | 填所用模型名称。 [Gemini OpenAI 兼容接口](https://ai.google.dev/gemini-api/docs/openai) |
| `qwen` | 填控制台给出的地区或工作空间 Base URL 和模型名称，选支持非思考模式和 JSON 的模型。 [阿里云百炼 Chat API](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions) |
| `doubao` | 填火山方舟模型 ID 或接入点 ID。 [火山方舟 Chat API](https://docs.volcengine.com/docs/ark/chat-api) |
| `kimi` | 默认使用 Kimi API Platform 的 `https://api.moonshot.ai/v1`，填所用模型名称。 [Kimi Chat Completions](https://platform.kimi.ai/docs/api/chat) |
| `claude` | 填所用模型名称，使用已作用域到工作空间的 API Key。 [Claude OpenAI 兼容接口](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk) |
| `compatible` | 填基础地址、模型名称和此地址对应的 Key。接口需支持 Chat Completions 和 `json_object`。 |

| 环境变量 | 用途 |
| --- | --- |
| `X_VALIDATOR_FAST_PROVIDER`、`X_VALIDATOR_REVIEW_PROVIDER` | 识图与初判、文字复核的厂商预设 |
| `X_VALIDATOR_FAST_MODEL`、`X_VALIDATOR_REVIEW_MODEL` | 两条路线各自的模型 |
| `X_VALIDATOR_FAST_BASE_URL`、`X_VALIDATOR_REVIEW_BASE_URL` | 可覆盖预设的基础地址。填到 API 版本层级，程序追加 `/chat/completions` |
| `DEEPSEEK_API_KEY` | 可供 DeepSeek 官方模型和网页取证共享 |
| `X_VALIDATOR_FAST_API_KEY`、`X_VALIDATOR_REVIEW_API_KEY` | 指定各模型路线的 Key。同一官方接口地址可以复用识图 Key |
| `X_VALIDATOR_REVIEW_VISION_MODEL` | 可选。复核接口上能接收图片的模型名称，供定向复读使用 |
| `X_VALIDATOR_WEB_PROVIDER`、`X_VALIDATOR_WEB_API_KEY` | 网页取证厂商与 Key。厂商可选 `deepseek` 或 `kimi`。同一官方 Kimi API Platform 地址的模型 Key 可用于 Kimi 取证 |

例如，想用一把 Kimi API Platform Key 运行两条模型路线和网页取证，可以在 PowerShell 7 中执行以下命令。环境变量设置在当前窗口生效，请在该窗口启动部署脚本。

```powershell
$env:X_VALIDATOR_FAST_PROVIDER = 'kimi'
$env:X_VALIDATOR_FAST_MODEL = 'kimi-k2.6'
$env:X_VALIDATOR_REVIEW_PROVIDER = 'kimi'
$env:X_VALIDATOR_REVIEW_MODEL = 'kimi-k2.6'
$env:X_VALIDATOR_WEB_PROVIDER = 'kimi'
./Start-XValidator.ps1 -Action ModelCredentials -Slot Fast
./Start-XValidator.ps1 -Action CheckAI
./Deploy-XValidator.ps1
```

`ModelCredentials -Slot Fast`、`-Slot Review`、`-Slot Web` 各自只改一项加密凭证。直接运行 `ModelCredentials` 会依次配置三项。留空清除此项覆盖值。`CheckAI` 显示配置选中的厂商与模型，不调用网络、不消耗额度。向导会保存跨 PowerShell 窗口使用的设置，显式设置的环境变量优先。修改凭证或模型设置后，用 `-Action Restart` 加载。

识图服务接收截图。初判服务和按需复核服务接收待核验说法与证据片段。网页取证服务接收检索词和待抓取的公开 URL。请按所选厂商的数据条款处理截图里的敏感信息。

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
