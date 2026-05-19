# jimeng-mcp

> 即梦 AI × Claude Code —— 让 AI 助手直接调用即梦进行文生图、美术素材创作的 MCP 插件。

无需 API Key，无需付费接口。利用 Playwright CDP 接管本地浏览器，全自动操作即梦网页端完成图片生成与下载。

---

## 🚀 极致安装体验（一行命令）

**不需要克隆项目，不需要手动安装依赖。** 只要电脑里有 [`uv`](https://docs.astral.sh/uv/) 和 Chrome 浏览器：

```bash
claude mcp add jimeng -- uvx --from git+https://github.com/NB4747/jimeng-tools.git jimeng-mcp
```

> 如果你 fork 了本项目，把 `NB4747/jimeng-tools` 换成你自己的 GitHub 用户名和仓库名。

执行完毕后重启 Claude Code，MCP 服务即注册完成。

---

## 🔑 登录配置（双轨制）

### 方式 A：自动浏览器扫码（推荐）

默认模式，**零配置**。首次触发图片生成时：

1. 系统自动弹出一个独立的 Chrome 窗口，打开即梦首页。
2. 在窗口中扫码登录你的即梦账号。
3. 登录成功后，保持窗口在后台（关闭也可以，Session 会被记录在 `%LOCALAPPDATA%\jimeng_mcp_chrome_profile` 中）。
4. 后续使用无需再次登录。

> 如果你提前用 `--remote-debugging-port=9222` 启动了 Chrome，系统会优先复用该实例，不会另外弹窗。

### 方式 B：环境变量静默模式（高级用户）

适合服务器环境或不想弹窗的用户。设置环境变量后，程序进入 **完全无头（headless）模式**，全程零界面。

```powershell
# Windows PowerShell
$env:JIMENG_COOKIE = "your_cookie_string_here"
```

```bash
# Linux / macOS
export JIMENG_COOKIE="your_cookie_string_here"
```

> **Cookie 获取方式**：在 Chrome 中按 F12 → Application → Cookies → jimeng.jianying.com，将需要的 cookie 复制拼接为 `name1=value1; name2=value2` 格式。

设置后运行 MCP 服务，即可静默后台生成。

---

## 🤖 使用示例

在 Claude Code 中直接用大白话对话，工具会自动感知意图：

| 你说的话 | Claude 的行为 |
|----------|--------------|
| "帮我画一只像素风的猫咪" | 自动调用 `generate_game_asset`，prompt 为 "pixel art cat, cute, 8-bit style" |
| "用即梦生成一张赛博朋克城市夜景" | 自动生成 cyberpunk city night scene |
| "做一个武侠风格的游戏背景" | 自动生成 wuxia game background |
| "设计一个 Q 版头像，日系风格" | 自动生成 anime chibi avatar |
| "generate a fantasy castle game asset" | 自动生成游戏素材 |

你也可以直接指定工具：

```
/mcp 用 generate_game_asset 生成一张油画风格的森林狐狸
```

---

## 📁 项目结构

```
jimeng-mcp-bridge/
├── pyproject.toml          # uv 项目配置，声明依赖与入口
├── config.json             # CDP 端口、超时、下载路径等配置
├── requirements.txt        # 传统 pip 依赖（供参考）
├── README.md
└── src/
    ├── __init__.py
    ├── main.py             # FastMCP 服务入口 + 双轨制登录逻辑
    ├── jimeng_client.py    # Playwright CDP 核心控制 + 网络拦截
    └── utils.py            # 异步图片下载
```

---

## ⚙️ 配置说明

`config.json`：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `cdp_url` | string | `"http://localhost:9222"` | Chrome CDP 地址 |
| `default_output_dir` | string | `"./downloads"` | 图片保存目录 |
| `task_timeout` | number | `60` | 生图任务超时（秒） |
| `api_patterns` | array | `["api/v1/task", ...]` | 拦截 API 的 URL 正则 |
| `poll_interval` | number | `1.0` | 轮询间隔（秒） |

---

## 🔧 本地开发

```bash
# 1. 克隆项目
git clone https://github.com/NB4747/jimeng-tools.git
cd jimeng-tools

# 2. 安装依赖
uv sync

# 3. 启动 Chrome CDP（可选；不启动则自动弹窗）
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\ChromeProfileForAgent"

# 4. 注册到 Claude Code
claude mcp add jimeng -- uv run jimeng-mcp
```

---

## 📄 License

MIT
