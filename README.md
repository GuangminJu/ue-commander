# ue-commander

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
