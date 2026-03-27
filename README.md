# ue-commander

[English](#english) | [中文](#中文)

---

<a id="english"></a>

MCP server for safely managing Unreal Engine from AI assistants (Claude Code, Cursor, Windsurf, etc.).

AI agents frequently get UE commands wrong — wrong paths, wrong build flags, launching duplicate editors. This tool wraps all UE operations behind correct, safe MCP tools so the AI never needs to guess.

## Features

- **Launch / Close** — start the editor with auto-compile, prevent duplicate instances, graceful shutdown
- **Compile** — correct UBT invocation with IDE config auto-detection (Rider / VS Code)
- **Discover** — find all engines and projects across all drives in milliseconds (Everything) or seconds (fallback)
- **Logs** — read editor logs, extract structured compile errors

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Unreal Engine 4.27+ / 5.x
- Windows (macOS support planned)
- [Everything](https://www.voidtools.com/) + [es.exe CLI](https://www.voidtools.com/downloads/#cli) (optional, for fast disk search)

## Install

```bash
git clone https://github.com/GuangminJu/ue-commander.git
cd ue-commander
uv sync
```

## Configure

### Claude Code CLI

Add to `.claude/settings.json` in your UE project:

```json
{
  "mcpServers": {
    "ue-commander": {
      "command": "uv",
      "args": [
        "--directory", "C:\\path\\to\\ue-commander",
        "run", "ue-commander"
      ],
      "env": {
        "UE_PROJECT_PATH": "C:\\path\\to\\YourProject"
      }
    }
  }
}
```

### Cursor / Windsurf

Add the same config to your MCP settings file (`.cursor/mcp.json` or equivalent).

## MCP Tools

| Tool | Description |
|------|-------------|
| `ue_project_info` | Show detected project, engine path, IDE build config |
| `ue_status` | Check if editor is running (PID, memory, uptime) |
| `ue_launch` | Compile + launch editor (blocks if already running) |
| `ue_close` | Graceful close with timeout, optional force kill |
| `ue_close_all` | Kill all UE instances |
| `ue_compile` | Compile C++ via UBT (defaults to IDE config) |
| `ue_get_log` | Tail the most recent editor log |
| `ue_get_compile_errors` | Extract structured errors from log |
| `ue_discover_all` | Find all engines + projects on the machine |
| `ue_find_projects` | Search specific drives for projects |

## Engine Detection

Supports all installation types:

| Type | Source |
|------|--------|
| Source builds | `HKCU\SOFTWARE\Epic Games\Unreal Engine\Builds` |
| Launcher installs | `HKLM\SOFTWARE\EpicGames\Unreal Engine\{version}` |
| LauncherInstalled.dat | `%PROGRAMDATA%\Epic\UnrealEngineLauncher\` |
| Unregistered | Auto-discovered from Templates/ paths during project scan |

## Project Filtering

A single UE project can contain many `.uproject` copies. ue-commander filters them:

- Skips `Intermediate/`, `Saved/`, `DerivedDataCache/`, `Binaries/`
- Skips engine `Templates/`, `Samples/`, `FeaturePacks/`
- Validates JSON has `EngineAssociation` field
- Deduplicates by project root directory

## Environment Variables

| Variable | Description |
|----------|-------------|
| `UE_ENGINE_PATH` | Override engine root (skips registry detection) |
| `UE_PROJECT_PATH` | Override project path (skips directory walk) |

## License

MIT

---

<a id="中文"></a>

# ue-commander

[English](#english) | [中文](#中文)

面向 AI 助手（Claude Code、Cursor、Windsurf 等）的 Unreal Engine MCP 管理工具。

AI 经常写错 UE 命令——路径不对、编译参数错误、重复打开编辑器。本工具将所有 UE 操作封装为安全的 MCP 工具，AI 无需猜测。

## 功能

- **启动 / 关闭** — 自动编译后启动编辑器，防止重复实例，优雅关闭
- **编译** — 正确调用 UBT，自动检测 IDE 配置（Rider / VS Code）
- **发现** — 毫秒级全盘搜索所有引擎和项目（Everything），或秒级降级扫描
- **日志** — 读取编辑器日志，提取结构化编译错误

## 环境要求

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip
- Unreal Engine 4.27+ / 5.x
- Windows（macOS 支持计划中）
- [Everything](https://www.voidtools.com/) + [es.exe 命令行工具](https://www.voidtools.com/downloads/#cli)（可选，用于快速全盘搜索）

## 安装

```bash
git clone https://github.com/GuangminJu/ue-commander.git
cd ue-commander
uv sync
```

## 配置

### Claude Code CLI

在 UE 项目的 `.claude/settings.json` 中添加：

```json
{
  "mcpServers": {
    "ue-commander": {
      "command": "uv",
      "args": [
        "--directory", "C:\\你的路径\\ue-commander",
        "run", "ue-commander"
      ],
      "env": {
        "UE_PROJECT_PATH": "C:\\你的路径\\YourProject"
      }
    }
  }
}
```

### Cursor / Windsurf

将相同配置添加到对应的 MCP 配置文件（`.cursor/mcp.json` 等）。

## MCP 工具列表

| 工具 | 说明 |
|------|------|
| `ue_project_info` | 显示项目、引擎路径、IDE 编译配置 |
| `ue_status` | 检查编辑器是否运行中（PID、内存、运行时长） |
| `ue_launch` | 编译 + 启动编辑器（已运行则阻止重复启动） |
| `ue_close` | 优雅关闭，支持超时和强制终止 |
| `ue_close_all` | 关闭本机所有 UE 实例 |
| `ue_compile` | 通过 UBT 编译 C++（默认使用 IDE 当前配置） |
| `ue_get_log` | 读取最近的编辑器日志尾部 |
| `ue_get_compile_errors` | 从日志中提取结构化编译错误 |
| `ue_discover_all` | 发现本机所有引擎 + 项目 |
| `ue_find_projects` | 在指定盘符搜索项目 |

## 引擎检测

支持所有安装方式：

| 类型 | 来源 |
|------|------|
| 源码编译版 | `HKCU\SOFTWARE\Epic Games\Unreal Engine\Builds` |
| Launcher 安装版 | `HKLM\SOFTWARE\EpicGames\Unreal Engine\{version}` |
| LauncherInstalled.dat | `%PROGRAMDATA%\Epic\UnrealEngineLauncher\` |
| 未注册引擎 | 项目扫描时从 Templates/ 路径自动发现 |

## 项目过滤

一个 UE 项目内部可能包含多份 `.uproject` 副本，ue-commander 智能过滤：

- 跳过 `Intermediate/`、`Saved/`、`DerivedDataCache/`、`Binaries/`
- 跳过引擎内的 `Templates/`、`Samples/`、`FeaturePacks/`
- 验证 JSON 包含 `EngineAssociation` 字段
- 按项目根目录去重

## 环境变量

| 变量 | 说明 |
|------|------|
| `UE_ENGINE_PATH` | 覆盖引擎根目录（跳过注册表检测） |
| `UE_PROJECT_PATH` | 覆盖项目路径（跳过目录扫描） |

## 许可证

MIT
