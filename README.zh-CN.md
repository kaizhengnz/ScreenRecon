# ScreenRecon

[English](README.md) | 简体中文

盯住屏幕上的一块矩形区域。当鼠标**在区域内停留**几秒钟，ScreenRecon 会截取该区域，
送给 AI，把答案打印在终端里，再把截图和答案推送到你的 Telegram，同时在本地归档两者。

开源、可 `pip install`、自带密钥。没有托管后端：你的 API key 和截图只会发往你自己配置的
AI 服务商和你自己的 Telegram 会话，不去别处。

```
鼠标在区域内停留 ──> 截图 ──┬──> 保存 JPEG
                            └──> AI ──┬──> 终端
                                      ├──> 保存 TXT
                                      └──> Telegram
```

## 环境要求

- Python 3.10+
- Windows 10/11、macOS 12+（Intel 或 Apple Silicon），或使用 X11 的 Linux
- 下列任一服务的 API key：Anthropic、OpenAI、Google Gemini，或 OpenAI 兼容端点（DeepSeek / Moonshot / Doubao）。见下方 [各家服务商与所需 extras](#各家服务商与所需-extras)。
- 一个 Telegram bot token 和 chat ID

Linux 注意：不支持 Wayland —— 在 Wayland 下读不到光标位置，XWayland 也不是变通方案
（它在原生 Wayland 窗口上报告的是过期坐标，截出来是黑的），所以 ScreenRecon 在 Wayland
会话中会拒绝启动。请改用 X11/Xorg 会话登录。如果你的应用确实全是 X11，
`SCREENRECON_FORCE_X11=1` 可以跳过这项检查。

## 安装

```bash
pip install screenrecon
```

从源码检出安装：

```bash
pip install -e ".[dev]"
```

## 快速开始

```bash
# 1. 初始化配置：交互式选取区域（在屏幕上拖出一个矩形），
#    然后填写凭据和归档目录。
screenrecon --configure

# 2. 开始盯屏。
screenrecon
```

把鼠标移进区域内并停留够配置的时长（默认 3 秒），截图就会触发。想再触发一次，把鼠标
移出区域再移回来 —— 光标一直停在里面不会重复触发。

## 命令

| 命令 | 作用 |
| --- | --- |
| `screenrecon` | 盯住已配置的区域 |
| `screenrecon --configure` | 交互式设置：拖拽选取区域，然后填写凭据（会联网校验） |
| `screenrecon --screen` | 只重新选取区域，其他配置项一概不动 |
| `screenrecon --key` | 为当前服务商输入新的 API key |
| `screenrecon --model` | 只更换 AI 模型 |
| `screenrecon --prompt` | 只更换默认提示词 |
| `screenrecon --dwell` | 只设置停留秒数 |
| `screenrecon --save-dir` | 只设置归档目录 |
| `screenrecon --telegram` | 输入 Telegram bot token + chat ID（成对设置） |
| `screenrecon --show` | 打印当前配置（凭据已掩码）并退出 |
| `screenrecon --mode NAME` | 用 `prompts.NAME` 预设代替默认提示词来盯屏 |
| `screenrecon ask "question"` | 截一次图，回答一个问题，然后退出 |
| `screenrecon ask` | 截一次图，然后就这张截图持续追问 |
| `screenrecon --mode NAME ask` | 截一次图并用 `prompts.NAME` 预设提问 |
| `screenrecon --config PATH` | 使用另一个配置文件 |
| `screenrecon --debug` | 照常盯屏，并在区域周围画一圈常驻红框（用于肉眼核对） |
| `screenrecon --version` | 打印版本号 |

全局参数要放在子命令**前面**：`screenrecon --config PATH ask "..."`，
而不是 `screenrecon ask "..." --config PATH`。

API 调用失败时 `ask` 会以非零状态退出，所以 `screenrecon ask "..." && ...`
在出错时不会继续执行。

## 配置

默认位置：如果设置了 `$XDG_CONFIG_HOME`，则为 `$XDG_CONFIG_HOME/screenrecon/config.json`，
否则为 `~/.config/screenrecon/config.json`。在 macOS 和 Linux 上，该文件及其目录以
仅属主可访问的权限创建（`0600` / `0700`）。

提示词预设通过直接编辑配置文件添加 —— 向导不会问这一项。

```json
{
  "region": { "left": 100, "top": 100, "width": 600, "height": 400 },
  "provider": "anthropic",
  "model": "claude-haiku-4-5",
  "api_key": "sk-ant-...",
  "base_url": "",
  "telegram_bot_token": "123456:ABC-...",
  "telegram_chat_id": "123456789",
  "save_dir": "~/ScreenRecon",
  "prompt": "Describe what is in this screenshot. Be concise and lead with the key information.",
  "prompts": {
    "log": "Find the error messages in this screenshot and explain the likely cause.",
    "table": "Transcribe this table as CSV."
  },
  "dwell_seconds": 3
}
```

| 字段 | 说明 |
| --- | --- |
| `region` | 屏幕矩形。`width`/`height` 必须为正；`left`/`top` 可以为负，用于位于主显示器左侧或上方的显示器。 |
| `monitor` | 可选，由 `--configure` / `--screen` 写入：`{"index": N, "of": M}` 记录区域是在哪块显示器上选的，以便盯屏横幅原样显示。每次重新选取区域都会重新生成；删掉它可强制实时重算。 |
| `dwell_seconds` | 鼠标须在区域内停留多久才触发。允许小数。 |
| `provider` | `anthropic` / `openai` / `google` / `openai_compatible`。留空表示"按模型名前缀推断"（`claude-*` → Anthropic，`gpt-*` / `o*` → OpenAI，`gemini-*` → Google）。兼容端点这条路必须显式设置本字段，并同时设置 `base_url`。 |
| `model` | 所选服务商下任意具备视觉能力的模型 ID。默认 `claude-haiku-4-5`；向导会按服务商给出精选清单（`claude-opus-5`、`gpt-5`、`gemini-2.5-pro`、`deepseek-vl2` 等），也接受手动输入任意 ID。 |
| `api_key` | 所选服务商的密钥。可用 `--key` 重写；向导在初始化时会提示输入。0.1.5 的旧配置里可能还留着 `anthropic_api_key` —— 读取时作为回退兼容，并在下次保存时迁移。 |
| `base_url` | 仅当 `provider` 为 `openai_compatible` 时使用。填写 Chat-Completions 兼容的端点（DeepSeek / Moonshot / Doubao 的预设由向导自动填入）。 |
| `prompts` | 具名预设，用 `--mode NAME` 选用。 |
| `save_dir` | `~` 会被展开，目录不存在时自动创建。 |

0.1.6 起不再支持环境变量覆盖：配置文件是凭据的唯一来源。从 0.1.5 升级上来、原先依赖
`ANTHROPIC_API_KEY` 的用户，跑一次 `screenrecon --key` 把值挪进配置文件即可。

### 各家服务商与所需 extras

可选依赖让安装保持精简 —— 只装你真正会用到的 SDK。

| 服务商 | 安装命令 | 示例模型 |
| --- | --- | --- |
| Anthropic（默认） | `pip install screenrecon` | `claude-haiku-4-5`、`claude-opus-5` |
| OpenAI | `pip install 'screenrecon[openai]'` | `gpt-5`、`gpt-5-mini` |
| Google Gemini | `pip install 'screenrecon[google]'` | `gemini-2.5-pro`、`gemini-2.5-flash` |
| OpenAI 兼容（DeepSeek / Kimi / Doubao / 自定义） | `pip install 'screenrecon[openai]'` | `deepseek-vl2`、`moonshot-v1-8k-vision-preview`、`doubao-1-5-vision-pro-32k-...` |
| 全部 | `pip install 'screenrecon[all]'` | 以上任意 |

如果你选了某个服务商但其 SDK 未安装，程序会打印对应的 `pip install 'screenrecon[...]'`
命令并退出，不会碰网络。

### 获取 API key

**Anthropic（Claude）。**
1. 在 [console.anthropic.com](https://console.anthropic.com/) 注册或登录。
2. 进入 **Settings → API Keys → Create Key**，起个名字比如 `screenrecon`，复制出现的密钥（以 `sk-ant-...` 开头）。Anthropic 只会完整显示一次。
3. 在 **Settings → Billing** 里充值 —— 视觉调用需要付费余额，免费额度不足以支撑持续使用。

**OpenAI（GPT）。** [platform.openai.com](https://platform.openai.com/) → **API keys → Create new secret key**（以 `sk-...` 开头）。在 **Settings → Billing** 充值。

**Google（Gemini）。** [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) → **Create API key**（以 `AIza...` 开头）。免费额度够日常桌面使用；要写脚本前先确认当前限额。

**DeepSeek / Moonshot（Kimi） / Doubao。** 各自的控制台（`platform.deepseek.com`、`platform.moonshot.cn`、`console.volcengine.com/ark`）。按各家流程预付或绑定计费。复制 OpenAI 兼容的密钥；当你在 `screenrecon --configure` 里选中对应预设时，匹配的 `base_url` 会自动填好。

建议为每个工具单独建一个密钥，而不是复用现有的：这样可以独立吊销或更换，账号级的用量
报表也能一眼看出 ScreenRecon 花了多少钱。

### 获取 Telegram bot token 和 chat ID

1. 给 [@BotFather](https://t.me/BotFather) 发消息，发送 `/newbot`，复制它给你的 token。
2. 给你新建的 bot 随便发一条消息。
3. 打开 `https://api.telegram.org/bot<TOKEN>/getUpdates`，读取 `result[0].message.chat.id`。

`screenrecon --configure` 在结束时会发一条测试消息，所以两个值对不对你当场就知道。

## 输出

每次触发都会往 `save_dir` 写入两个文件：

```
20260812_143052.jpg    截取的区域（JPEG，质量 90）
20260812_143052.txt    识别出的文本（UTF-8）
```

同一段文本会打印到终端并发送到 Telegram。如果答案超过 Telegram 1024 字符的图注上限，
图片会带一段截断的图注，完整文本作为单独一条消息紧随其后。

四路输出彼此独立：Telegram 挂了不影响本地归档，磁盘满了也不影响 Telegram 推送。

## 坐标是怎么回事

设置阶段的选取器和盯屏循环，与截图走的是同一套 API，所以选取器返回的坐标正是盯屏时
使用的坐标。

在 Windows 上，进程会在第一次截图前声明 per-monitor DPI 感知，因此在缩放显示器上
选取器报告的是**物理**像素。有一个后果值得知道：坐标是物理的，所以在 150% 缩放下记录的
区域，等你改了显示缩放之后就会指向别处。改完缩放请重新跑一次 `screenrecon --configure`。

区域落在屏幕之外对截图后端来说不算错误 —— 它会返回黑色。ScreenRecon 在启动时会拿区域
和桌面比对，若区域在屏幕外或被裁切会给出告警。

长边超过 2576 px 的截图在发送前会被缩小，因为视觉模型本来也会把更大的图缩掉。本地归档
保留全分辨率图像，只有上传的那份被缩小。

在 Retina Mac 上，截图回来的是逻辑（1×）分辨率而非原生 2×，因为截图后端请求的是标称
分辨率。坐标不受影响，但很小的文字所捕获的细节只有同等非 Retina 显示器的一半。实际的
变通办法是把区域或源文字放大。

## macOS 屏幕录制权限

在 macOS 上第一次截图需要授权：

**系统设置 → 隐私与安全性 → 屏幕录制 →** 启用你的终端（Terminal、iTerm、VS Code……），
然后**彻底退出终端并重新打开**。

没有授权时 macOS 并不会让截图失败 —— 它会不声不响地返回桌面壁纸和菜单栏、抹掉你的窗口，
于是 AI 会信心十足地描述你的壁纸。ScreenRecon 在启动时向系统查询该权限状态并打印这段
提醒，而不是试图从像素里猜。

## 安全与隐私

- **自带密钥。** 凭据只存在于你的配置文件里，任何凭据都不会出现在终端输出、日志或
  堆栈里 —— 只显示前 8 个字符。第三方错误文本在打印前会被清洗，因为 HTTP 库会把请求
  URL（其中含有 Telegram bot token）嵌进异常信息。设置向导读取凭据时不回显。
- **给每个服务商单独建一个 API key** 专供本工具使用，这样吊销和用量核算都能与你其他
  工作负载分开。
- **截图可能包含敏感信息。** 它们会被发往你配置的 AI 服务商和你自己的 Telegram 会话，
  并存放在 `save_dir` —— 除非 `ANTHROPIC_BASE_URL`（Anthropic）、`HTTPS_PROXY`，或
  OpenAI 兼容服务商的显式 `base_url` 把它们导向别处。归档目录的管理由你自己负责。
- **只使用你自己写的配置文件。** `--config` 接受任意路径，而配置文件携带的凭据决定了
  你的截图发往哪个 Telegram 会话、走哪个 API 账号。盯屏横幅会打印掩码后的 chat ID 和
  解析出的归档目录，所以配置文件被掉包是看得出来的。
- 在 macOS 和 Linux 上，配置文件、归档目录以及每一张截图都以仅属主可访问的权限创建。
  见 [SECURITY.md](SECURITY.md)。

## 成本与延迟

默认模型是 `claude-haiku-4-5` —— 又快又便宜，做 OCR 和简短描述绰绰有余。复杂画面要更高
准确度的话，设置 `"model": "claude-opus-5"`（内部使用 `low` 档 effort，所以查一下依然
很快）。不接受 effort 参数的模型会被自动识别并去掉该参数。

## 开发

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

测试套件覆盖了触发状态机（进入 → 停留 → 触发 → 不重复 → 离开 → 重新待命）、配置加载与
校验、设置向导、CLI 路由与退出码、本地归档及其权限、光标后端选择与 Wayland 检测、截图
转换与区域检查、Telegram 图注拆分、AI 错误翻译表，以及一次触发的四路输出彼此独立。全部
测试都不碰网络和屏幕。

发布前，请在 `pyproject.toml` 里补上指向真实仓库的 `[project.urls]` 段。

## v1 不做的事

图形化区域选择、同时盯多个区域、内容变化触发、托管后端，以及独立的 `.exe`/`.app` 打包。

## 许可证

Apache License 2.0 —— 见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
