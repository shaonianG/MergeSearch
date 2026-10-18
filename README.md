# MergeSearch

**v0.1.0**

多平台搜索聚合 MCP 服务（Windows 桌面应用）：将 DeepSeek、Tavily 的搜索/抓取能力聚合为一个本地 MCP 服务，配合图形化管理界面，开箱即用。

## 功能特性

- **MCP 服务**：streamable-http 协议，默认 `http://127.0.0.1:8765/mcp`，提供 4 个工具
  | 工具 | 说明 |
  |---|---|
  | `search` | 联网搜索，支持 Tavily 高级参数（垂直频道、时间范围、域名黑白名单、LLM 摘要等） |
  | `fetch` | 抓取网页正文返回 Markdown，跨平台自动降级 |
  | `crawl` | 整站递归爬取（Tavily） |
  | `research` | 深度研究，生成带引用的研究报告（Tavily，耗时较长） |
- **多平台多 Key**：DeepSeek / Tavily 各自可配置多个 API Key，支持平台启停、自定义 base_url
- **负载均衡**：平台间支持主备（failover）/ 轮询 / 随机三种策略，平台优先级可拖拽排序；平台内 Key 独立轮换策略，失败 Key 自动拉黑 5 分钟
- **Clash 风格代理分流**：直连 / 自动 / 全局三种模式，自动模式按规则匹配（`DOMAIN` / `DOMAIN-SUFFIX` / `DOMAIN-KEYWORD` / `IP-CIDR` / `REGEX` / `MATCH`）
- **图形界面**：Web 后台风格（pywebview），仪表盘含服务状态、用量统计、一键接入提示词；关闭窗口最小化到系统托盘
- **便携单文件**：PyInstaller 打包为单个 exe，无需安装 Python

## 快速开始

### 方式一：使用预打包 exe

1. 从 Releases 下载 `MergeSearch.exe`
2. 双击运行，托盘图标右键可退出
3. 打开主界面 → 平台与密钥，填入你的 API Key：
   - DeepSeek：[platform.deepseek.com](https://platform.deepseek.com/api_keys)
   - Tavily：[app.tavily.com](https://app.tavily.com/home)（免费额度 1000 积分/月）

### 方式二：从源码运行

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
python main.py            # Web 界面（默认）
python main.py --tk       # tkinter 界面（回退）
python main.py --headless # 无界面，仅 MCP 服务
```

### 接入 MCP 客户端

在 Kimi Code / Claude Code / Cherry Studio 等客户端中添加：

- 协议：streamable-http
- 地址：`http://127.0.0.1:8765/mcp`

或在应用仪表盘中点击「复制提示词」，把生成的一键接入提示词直接发给 AI 客户端自动完成配置。

### 自行打包

```bat
build.bat
```

产物在 `dist\MergeSearch.exe`。注意：重打包前请先退出托盘中的旧进程。

## 配置文件

首次运行后在 exe 同目录生成 `config.json`（模板见 [config.example.json](config.example.json)），主要结构：

```jsonc
{
  "platforms": {           // 平台与密钥
    "deepseek": { "enabled": true, "keys": ["sk-..."], "base_url": "https://api.deepseek.com" },
    "tavily":   { "enabled": true, "keys": ["tvly-..."], "base_url": "https://api.tavily.com" }
  },
  "load_balance": {        // 负载均衡
    "strategy": "failover",            // failover | round_robin | random
    "order": ["tavily", "deepseek"]    // 平台优先级
  },
  "key_strategy": "round_robin",       // 平台内 Key 轮换
  "server": { "host": "127.0.0.1", "port": 8765, "auto_start": true },
  "proxy": {
    "mode": "auto",                    // direct | auto | global
    "address": "http://127.0.0.1:7890",
    "rules": ["DOMAIN-SUFFIX,google.com,PROXY", "MATCH,DIRECT"]
  }
}
```

用量统计持久化在同目录 `stats.json`。

## 计费提示

- DeepSeek：search / fetch 各 5 元/1000 次成功请求，每账号 5 QPS
- Tavily：search 基础档 1 积分/次（高级档 2），extract 每 5 个成功 URL 1 积分，research 按模型档位计费；免费额度 1000 积分/月

## 项目结构

```
main.py          # 入口（--tk / --headless）
webui.py         # pywebview 界面 + 托盘 + JS 桥接
web/index.html   # 前端（单文件，无外部依赖）
mcp_service.py   # FastMCP 服务（4 个工具）
providers.py     # DeepSeek / Tavily 统一抽象、Key 池、负载均衡路由
proxy_router.py  # Clash 风格分流规则
stats.py         # 用量统计
app_config.py    # 配置管理、旧配置迁移、开机自启
build.bat        # PyInstaller 打包脚本
```

## License

MIT
