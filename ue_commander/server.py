"""
MCP server exposing UE launch/close/compile tools.

Design principle: AI should never need to know the exact paths or command syntax.
Every operation goes through this server, which uses the detected config.
"""

from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from .config import detect_config, find_uproject, BuildConfig, BuildPlatform, BuildTarget
from . import ue_process, ue_build, ue_discover, ue_editor

mcp = FastMCP(
    name="ue-commander",
    instructions=(
        "Tools for managing Unreal Engine: launching the editor, closing it, "
        "compiling C++ code, reading logs, and discovering all engines/projects "
        "on the machine. Always use these tools instead of running raw shell "
        "commands for UE operations. The tools handle correct paths, prevent "
        "duplicate instances, and integrate with your IDE build configuration."
    ),
)

# Lazy-init config — resolved once per server session
_cfg = None


def _get_cfg():
    global _cfg
    if _cfg is None:
        uproject = find_uproject()
        _cfg = detect_config(uproject)
    return _cfg


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_status() -> dict:
    """
    Check whether Unreal Editor is currently running for this project.
    Returns process info (PID, memory, uptime) if running.
    Also probes the plugin HTTP endpoint to report whether the editor
    is fully loaded and ready to accept commands (plugin_ready field).
    """
    cfg = _get_cfg()
    info = ue_process.get_status(cfg)
    result = {
        "project": cfg.project_name,
        "engine_path": str(cfg.engine_path),
        "editor_running": info.running,
    }
    if info.running:
        plugin_ready = ue_editor.is_plugin_available()
        phase = "ready" if plugin_ready else "loading"
        result.update({
            "pid": info.pid,
            "uptime_seconds": info.uptime_seconds,
            "memory_mb": info.memory_mb,
            "launched_by": info.launched_by,
            "plugin_ready": plugin_ready,
            "phase": phase,
        })
    else:
        result["phase"] = "not_running"
    if cfg.ide_build:
        result["ide_build_config"] = {
            "config": cfg.ide_build.config,
            "target": cfg.ide_build.target,
            "platform": cfg.ide_build.platform,
            "detected_from": cfg.ide_build.source,
        }
    return result


@mcp.tool()
async def ue_launch(
    extra_args: list[str] | None = None,
    compile_first: bool = False,
    wait_ready: bool = False,
    ready_timeout: int = 180,
    ctx: Context | None = None,
) -> dict:
    """
    Launch the Unreal Editor for this project.

    Passes -auto and -skipcompile flags to suppress startup prompts.
    Call ue_compile BEFORE this if you changed C++ code.

    After launching, waits for the editor to fully load by polling the
    plugin HTTP endpoint. The response includes:
      - phase: "ready" (plugin responding), "loading" (timed out), or "crashed_during_startup"
      - startup_time_seconds: how long it took to become ready

    Safety: returns an error (does NOT launch) if the editor is already running,
    preventing duplicate instances.

    Args:
        extra_args: Optional additional arguments passed to UnrealEditor.exe,
                    e.g. ["-log", "-game"]. Leave empty for normal editor launch.
        compile_first: If True, run UBT compile before launching (slow, blocks for minutes).
                       Default False — call ue_compile separately for better progress feedback.
        wait_ready: If True (default), wait for the plugin HTTP endpoint to respond
                    before returning. Set False for fire-and-forget.
        ready_timeout: Max seconds to wait for editor readiness. Default 180 (3 min).
    """
    cfg = _get_cfg()
    return await ue_process.launch(
        cfg, extra_args=extra_args, compile_first=compile_first,
        wait_ready=wait_ready, ready_timeout=ready_timeout, ctx=ctx,
    )


@mcp.tool()
def ue_close(force: bool = False, timeout: int = 30, user_override: bool = False) -> dict:
    """
    Close the Unreal Editor for this project.

    Ownership rules:
      - If the editor was launched by ue_launch (AI), it can be closed freely.
      - If the editor was launched by the USER (e.g. from Rider, Explorer, or
        desktop shortcut), this tool REFUSES to close it — the AI must not
        close what the user opened. Set user_override=true ONLY if the user
        has explicitly asked you to close their editor.

    Args:
        force: If True, immediately kill the process. If False (default),
               sends a graceful close signal and waits up to `timeout` seconds.
        timeout: Seconds to wait for graceful close before reporting failure.
                 Only used when force=False.
        user_override: Set to True ONLY when the user explicitly asks you to
                       close their manually-launched editor. Never set this
                       on your own initiative.
    """
    cfg = _get_cfg()
    return ue_process.close(cfg, force=force, timeout=timeout, user_override=user_override)


@mcp.tool()
def ue_close_all(force: bool = False) -> dict:
    """
    Close ALL running Unreal Editor instances on this machine.
    Use this when multiple UE windows are open and need to be cleaned up.

    Args:
        force: If True, kill all instances immediately.
    """
    return ue_process.close_all_ue(force=force)


@mcp.tool()
async def ue_compile(
    config: BuildConfig | None = None,
    target: BuildTarget | None = None,
    platform: BuildPlatform | None = None,
    timeout: int = 600,
    ctx: Context | None = None,
) -> dict:
    """
    Compile the project's C++ code using UnrealBuildTool.

    By default, uses the build configuration detected from your IDE (Rider/VS Code).
    Override any parameter only when you have a specific reason to deviate from
    the current IDE configuration — doing so may produce binaries incompatible
    with your IDE debugger.

    Valid values:
      config:   Debug | DebugGame | Development | Shipping | Test
      target:   Editor | Game | Client | Server
      platform: Win64 | Win32 | Mac | Linux

    Args:
        config:   Build configuration. Defaults to IDE-detected config.
        target:   Build target. Defaults to IDE-detected target.
        platform: Target platform. Defaults to IDE-detected platform.
        timeout:  Max seconds to wait for compilation. Default 600 (10 min).
    """
    cfg = _get_cfg()

    # Fill from IDE config when not explicitly overridden
    ide = cfg.ide_build
    resolved_config: BuildConfig = config or (ide.config if ide else "Development")
    resolved_target: BuildTarget = target or (ide.target if ide else "Editor")
    resolved_platform: BuildPlatform = platform or (ide.platform if ide else "Win64")

    result = await ue_build.compile(
        cfg,
        config=resolved_config,
        target=resolved_target,
        platform=resolved_platform,
        timeout=timeout,
        ctx=ctx,
    )

    return {
        "ok": result.ok,
        "return_code": result.return_code,
        "command": result.command,
        "resolved_config": resolved_config,
        "resolved_target": resolved_target,
        "resolved_platform": resolved_platform,
        "ide_config_source": ide.source if ide else "none",
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "errors": result.errors,
        "warnings": result.warnings,
        "build_output": result.output_tail,
    }


@mcp.tool()
def ue_get_log(lines: int = 80) -> dict:
    """
    Read the tail of the most recent Unreal Editor log file.
    Useful for diagnosing crashes, assertion failures, or runtime errors.

    Args:
        lines: Number of lines to return from the end of the log. Default 80.
    """
    cfg = _get_cfg()
    return ue_build.get_recent_log(cfg, lines=lines)


@mcp.tool()
def ue_get_compile_errors() -> dict:
    """
    Parse the most recent log file and extract only compile errors and warnings.
    Returns structured data (not raw log) — use this instead of ue_get_log
    when you specifically want to see why a build failed.
    """
    cfg = _get_cfg()
    return ue_build.get_compile_errors(cfg)


@mcp.tool()
def ue_project_info() -> dict:
    """
    Return full information about the detected project and engine setup.
    Use this first to verify that paths and configurations are correct
    before running compile or launch operations.
    """
    cfg = _get_cfg()
    info = ue_process.get_status(cfg)
    return {
        "project_name": cfg.project_name,
        "project_path": str(cfg.project_path),
        "engine_path": str(cfg.engine_path),
        "editor_exe": str(cfg.editor_exe),
        "build_bat_exists": cfg.build_bat.exists(),
        "plugins": cfg.plugin_names,
        "editor_running": info.running,
        "editor_pid": info.pid,
        "ide_build_config": {
            "config": cfg.ide_build.config,
            "target": cfg.ide_build.target,
            "platform": cfg.ide_build.platform,
            "detected_from": cfg.ide_build.source,
        } if cfg.ide_build else None,
    }


@mcp.tool()
def ue_discover_all() -> dict:
    """
    Scan the entire machine for all Unreal Engine installations and projects.

    Engines are found via Windows Registry and Epic Games Launcher data.
    Projects are found by searching for .uproject files across all drives.

    Uses Everything (es.exe) for millisecond-speed search if available,
    falls back to directory walking otherwise.

    Duplicate .uproject copies (in Intermediate/, Saved/, etc.) are
    automatically filtered out — only real project roots are returned.
    """
    result = ue_discover.discover_all()
    return {
        "engines": [
            {
                "path": e.path,
                "version": e.version,
                "type": e.engine_type,
                "association": e.association,
            }
            for e in result.engines
        ],
        "projects": [
            {
                "name": p.name,
                "path": p.path,
                "engine_association": p.engine_association,
                "engine_path": p.engine_path,
                "has_source": p.has_source,
                "has_content": p.has_content,
                "has_plugins": p.has_plugins,
                "is_engine_sample": p.is_engine_sample,
                "module_count": p.module_count,
            }
            for p in result.projects
        ],
        "skipped_uproject_copies": result.skipped_count,
        "search_method": result.search_method,
        "errors": result.errors,
    }


@mcp.tool()
def ue_find_projects(drives: list[str] | None = None) -> dict:
    """
    Search for Unreal Engine projects on specific drives (or all drives).

    This is a lighter version of ue_discover_all that skips engine
    discovery if you only need the project list.

    Args:
        drives: List of drive letters to search, e.g. ["C:", "D:"].
                If None, searches all available drives.
    """
    engines = ue_discover.discover_engines()
    engine_roots = {str(__import__("pathlib").Path(e.path).resolve()).lower() for e in engines}

    use_everything = ue_discover._has_everything()
    search_method = "everything" if use_everything else "os_walk"

    raw_paths: list[str] = []
    if use_everything:
        try:
            if drives:
                for d in drives:
                    d = d.rstrip(":\\/ ")
                    raw_paths.extend(ue_discover._search_everything(f"{d}:\\*.uproject"))
            else:
                raw_paths = ue_discover._search_everything("*.uproject")
        except RuntimeError:
            use_everything = False
            search_method = "os_walk (everything failed)"

    if not use_everything:
        sep = ":\\"
        roots = [d.rstrip(":\\/ ") + sep for d in drives] if drives else ue_discover._get_drive_letters()
        raw_paths = ue_discover._walk_for_uprojects(roots)

    ue_discover._cached_engines = engines
    projects: list[dict] = []
    skipped = 0
    seen: set[str] = set()

    for path_str in raw_paths:
        is_real, info = ue_discover._is_real_project(path_str, engine_roots)
        if not is_real:
            skipped += 1
            continue
        root_key = str(__import__("pathlib").Path(path_str).parent.resolve()).lower()
        if root_key in seen:
            skipped += 1
            continue
        seen.add(root_key)
        projects.append({
            "name": info.name,
            "path": info.path,
            "engine_association": info.engine_association,
            "engine_path": info.engine_path,
            "has_source": info.has_source,
            "is_engine_sample": info.is_engine_sample,
        })

    projects.sort(key=lambda p: (p["is_engine_sample"], p["name"].lower()))

    return {
        "projects": projects,
        "count": len(projects),
        "skipped_copies": skipped,
        "search_method": search_method,
    }


# ---------------------------------------------------------------------------
# Editor UI tools (require OhMyUnrealEngine plugin running in UE)
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_plugin_status() -> dict:
    """
    Check if the OhMyUnrealEngine plugin is reachable inside the running editor.
    Returns the list of available plugin tools if connected.
    Also checks for crash info if the plugin is not reachable.
    """
    # First check if there's a crash file from a previous crash
    crash = ue_editor.read_crash_info()
    if crash is not None:
        return {
            "ok": False,
            "crashed": True,
            "crash_info": crash,
            "hint": "UE crashed. Check crash_info for details. "
                    "Fix the issue and relaunch UE.",
        }

    if not ue_editor.is_plugin_available():
        return {
            "ok": False,
            "error": "Plugin not reachable. Is UE running with OhMyUnrealEngine loaded?",
        }
    tools = ue_editor.list_plugin_tools()
    return {"ok": True, **tools}


@mcp.tool()
def ue_clear_crash() -> dict:
    """
    Clear the crash info file after reviewing a crash.
    Call this after you've fixed the issue that caused the crash,
    before relaunching UE.
    """
    crash = ue_editor.read_crash_info()
    ue_editor.clear_crash_info()
    return {
        "ok": True,
        "cleared": crash is not None,
        "previous_crash": crash,
    }


@mcp.tool()
def ue_batch(calls: str) -> dict:
    """
    Execute multiple plugin tool calls in a single request.
    All calls run sequentially in one undo transaction — Ctrl+Z reverts everything.

    Args:
        calls: JSON array of calls. Each element: {"function":"ToolName","params":{...}}

    Example:
        [
          {"function":"SpawnActor","params":{"ClassName":"StaticMeshActor","Label":"Cube1","LocationX":100}},
          {"function":"SpawnActor","params":{"ClassName":"PointLight","Label":"Light1","LocationZ":300}},
          {"function":"SetProperty","params":{"Target":"Light1.PointLightComponent0","PropertyName":"Intensity","Value":"8000"}}
        ]
    """
    import json as _json
    try:
        parsed = _json.loads(calls)
    except _json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}
    if not isinstance(parsed, list):
        return {"error": "Expected a JSON array of calls"}
    return ue_editor.call_plugin_batch(parsed)


# ---------------------------------------------------------------------------
# Auto-registered plugin tools
# ---------------------------------------------------------------------------
# All tools exposed by the UE plugin (via /api/tools) are auto-registered
# as MCP tools at startup. No manual Python wrappers needed — just add
# a UFUNCTION(meta=(MCP, Desc="...")) in C++ and it appears here.
#
# Python-only tools (compile, launch, status, etc.) are defined above.
# ---------------------------------------------------------------------------

import re as _re
import inspect as _inspect


def _pascal_to_snake(name: str) -> str:
    """Convert PascalCase to snake_case: 'BlueprintPath' -> 'blueprint_path'."""
    s = _re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = _re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    return s.lower()


_TYPE_MAP: dict[str, type] = {
    "FString": str,
    "bool": bool,
    "int32": int,
    "float": float,
    "double": float,
}

_TYPE_DEFAULTS: dict[type, object] = {
    str: "",
    bool: False,
    int: 0,
    float: 0.0,
}


def _make_plugin_tool(tool_name: str, tool_desc: str, params: list[dict]):
    """Create a function with a real inspect.Signature and register it as MCP tool."""
    # Prepare parameter metadata
    p_infos = []
    for p in params:
        py_type = _TYPE_MAP.get(p["type"], str)
        p_infos.append((_pascal_to_snake(p["name"]), p["name"], py_type))

    # Build a real signature so inspect.signature() returns named params
    sig_params = [
        _inspect.Parameter(
            snake, _inspect.Parameter.KEYWORD_ONLY,
            default=_TYPE_DEFAULTS.get(py_type, ""),
            annotation=py_type,
        )
        for snake, _pascal, py_type in p_infos
    ]
    sig = _inspect.Signature(sig_params, return_annotation=dict)

    # Closure captures tool_name + p_infos
    def tool_fn(**kwargs) -> dict:
        plugin_kwargs = {}
        for snake, pascal, _t in p_infos:
            if snake in kwargs:
                plugin_kwargs[pascal] = kwargs[snake]
        return ue_editor.call_plugin(tool_name, **plugin_kwargs)

    mcp_name = f"ue_{_pascal_to_snake(tool_name)}"
    tool_fn.__name__ = mcp_name
    tool_fn.__qualname__ = mcp_name
    tool_fn.__doc__ = tool_desc
    tool_fn.__signature__ = sig
    # __annotations__ for typing.get_type_hints() used by context detection
    tool_fn.__annotations__ = {s: t for s, _p, t in p_infos}
    tool_fn.__annotations__["return"] = dict

    mcp.tool()(tool_fn)


def _register_plugin_tools():
    """
    Fetch the tool list from the UE plugin and auto-register each one as
    an MCP tool. Skips tools that already have manual Python definitions.
    """
    import sys

    tools_data = ue_editor.list_plugin_tools()
    if "error" in tools_data:
        print("[ue-commander] Plugin not reachable, skipping auto-registration.", file=sys.stderr)
        return 0

    existing = set(mcp._tool_manager._tools.keys())

    registered = 0
    for tool in tools_data.get("tools", []):
        mcp_name = f"ue_{_pascal_to_snake(tool['name'])}"
        if mcp_name in existing:
            continue
        _make_plugin_tool(tool["name"], tool.get("description", ""), tool.get("params", []))
        registered += 1

    total = len(tools_data.get("tools", []))
    print(f"[ue-commander] Auto-registered {registered}/{total} plugin tools.", file=sys.stderr)
    return registered


# Best-effort at import time (UE may not be running yet)
try:
    _register_plugin_tools()
except Exception as _e:
    import sys
    print(f"[ue-commander] Auto-registration deferred: {_e}", file=sys.stderr)
