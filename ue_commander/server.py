"""
MCP server exposing UE launch/close/compile tools.

Design principle: AI should never need to know the exact paths or command syntax.
Every operation goes through this server, which uses the detected config.
"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

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
    Also reports the detected IDE build configuration.
    """
    cfg = _get_cfg()
    info = ue_process.get_status(cfg)
    result = {
        "project": cfg.project_name,
        "engine_path": str(cfg.engine_path),
        "editor_running": info.running,
    }
    if info.running:
        result.update({
            "pid": info.pid,
            "uptime_seconds": info.uptime_seconds,
            "memory_mb": info.memory_mb,
            "launched_by": info.launched_by,
        })
    if cfg.ide_build:
        result["ide_build_config"] = {
            "config": cfg.ide_build.config,
            "target": cfg.ide_build.target,
            "platform": cfg.ide_build.platform,
            "detected_from": cfg.ide_build.source,
        }
    return result


@mcp.tool()
def ue_launch(
    extra_args: list[str] | None = None,
    compile_first: bool = True,
) -> dict:
    """
    Launch the Unreal Editor for this project.

    By default, compiles C++ modules first so the editor never shows a
    "modules out of date — rebuild?" dialog. Also passes -auto and
    -skipcompile flags to suppress any remaining startup prompts.

    Safety: returns an error (does NOT launch) if the editor is already running,
    preventing duplicate instances.

    Args:
        extra_args: Optional additional arguments passed to UnrealEditor.exe,
                    e.g. ["-log", "-game"]. Leave empty for normal editor launch.
        compile_first: If True (default), run UBT compile before launching.
                       Set to False only when you know modules are up to date.
    """
    cfg = _get_cfg()
    return ue_process.launch(cfg, extra_args=extra_args, compile_first=compile_first)


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
def ue_compile(
    config: BuildConfig | None = None,
    target: BuildTarget | None = None,
    platform: BuildPlatform | None = None,
    timeout: int = 600,
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

    result = ue_build.compile(
        cfg,
        config=resolved_config,
        target=resolved_target,
        platform=resolved_platform,
        timeout=timeout,
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
        "output_tail": result.output_tail,
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
        roots = [f"{d.rstrip(':\\/ ')}:\\" for d in drives] if drives else ue_discover._get_drive_letters()
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
def ue_list_windows() -> dict:
    """
    List all top-level editor windows (title, size, position).
    Requires UE editor running with OhMyUnrealEngine plugin.
    """
    return ue_editor.call_plugin("ListWindows")


@mcp.tool()
def ue_get_widget_tree(
    window_index: int = 0,
    max_depth: int = 3,
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """
    Browse the widget tree under a specific editor window.
    Returns a paginated flat list with depth info.

    Use ue_list_windows first to find the window index.
    Use offset/limit for pagination — NEVER request the full tree.

    Args:
        window_index: Window index from ue_list_windows result.
        max_depth: How deep to traverse. Default 3 (keeps response small).
        offset: Skip first N widgets (for pagination).
        limit: Max widgets to return. Default 50, max 200.
    """
    return ue_editor.call_plugin(
        "GetWidgetTree",
        WindowIndex=window_index,
        MaxDepth=max_depth,
        Offset=offset,
        Limit=limit,
    )


@mcp.tool()
def ue_search_widgets(query: str, search_type: str = "text", limit: int = 20) -> dict:
    """
    Search for widgets across all windows by type name or display text.
    Much faster than browsing the tree manually.

    Args:
        query: Search string, e.g. "SButton", "Content Browser", "Details".
        search_type: "type" to match widget class name, "text" to match display text.
        limit: Max results. Default 20.
    """
    return ue_editor.call_plugin(
        "SearchWidgets",
        Query=query,
        SearchType=search_type,
        Limit=limit,
    )


@mcp.tool()
def ue_get_widget_detail(widget_path: str) -> dict:
    """
    Get detailed info about a single widget by its path.
    Path format: "WindowIndex/ChildIndex/ChildIndex/..."
    (obtained from ue_get_widget_tree or ue_search_widgets results).

    Returns: type, tag, text, position, size, visibility, and a summary
    of immediate children.
    """
    return ue_editor.call_plugin("GetWidgetDetail", WidgetPath=widget_path)


@mcp.tool()
def ue_list_plugin_tools() -> dict:
    """
    List all tools available in the OhMyUnrealEngine plugin.
    These are auto-discovered via UE reflection — any UFUNCTION
    tagged with meta=(MCP) in UOhMyToolkit is listed here.
    """
    return ue_editor.list_plugin_tools()


# ---------------------------------------------------------------------------
# Widget interaction tools (require OhMyUnrealEngine plugin)
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_click_widget(widget_path: str, button: str = "left") -> dict:
    """
    Simulate a mouse click on a widget WITHOUT moving the user's real cursor.
    The click is routed through Slate internally.

    Args:
        widget_path: Widget path from ue_get_widget_tree or ue_search_widgets.
        button: "left", "right", or "middle". Default "left".
    """
    return ue_editor.call_plugin("ClickWidget", WidgetPath=widget_path, Button=button)


@mcp.tool()
def ue_hover_widget(widget_path: str) -> dict:
    """
    Simulate mouse hover over a widget (triggers OnMouseEnter/OnMouseMove).
    Does NOT move the user's real cursor.

    Args:
        widget_path: Widget path from ue_get_widget_tree or ue_search_widgets.
    """
    return ue_editor.call_plugin("HoverWidget", WidgetPath=widget_path)


@mcp.tool()
def ue_focus_widget(widget_path: str) -> dict:
    """
    Set keyboard focus to a widget. Required before typing text.

    Args:
        widget_path: Widget path from ue_get_widget_tree or ue_search_widgets.
    """
    return ue_editor.call_plugin("FocusWidget", WidgetPath=widget_path)


@mcp.tool()
def ue_type_text(text: str, widget_path: str = "") -> dict:
    """
    Type text into a widget. If widget_path is given, focuses it first.
    Each character is sent as an individual keyboard event.

    Args:
        text: The text to type.
        widget_path: Optional path to focus before typing. Empty = use current focus.
    """
    return ue_editor.call_plugin("TypeText", Text=text, WidgetPath=widget_path)


@mcp.tool()
def ue_press_key(key: str, widget_path: str = "", modifiers: str = "") -> dict:
    """
    Simulate a key press (down + up).

    Args:
        key: UE key name — "Enter", "Tab", "Escape", "Delete", "Backspace",
             "Up", "Down", "Left", "Right", "A"-"Z", "F1"-"F12", etc.
        widget_path: Optional path to focus before pressing.
        modifiers: Comma-separated: "ctrl", "shift", "alt", "cmd".
    """
    return ue_editor.call_plugin(
        "PressKey", Key=key, WidgetPath=widget_path, Modifiers=modifiers
    )


@mcp.tool()
def ue_scroll_widget(widget_path: str, delta: float = 3.0) -> dict:
    """
    Simulate mouse scroll on a widget.

    Args:
        widget_path: Widget path from ue_get_widget_tree or ue_search_widgets.
        delta: Scroll amount. Positive = up, negative = down. Default 3.0.
    """
    return ue_editor.call_plugin("ScrollWidget", WidgetPath=widget_path, Delta=delta)


@mcp.tool()
def ue_right_click_widget(widget_path: str) -> dict:
    """
    Simulate right-click on a widget (opens context menu).

    Args:
        widget_path: Widget path from ue_get_widget_tree or ue_search_widgets.
    """
    return ue_editor.call_plugin("RightClickWidget", WidgetPath=widget_path)


@mcp.tool()
def ue_drag_widget(source_path: str, target_path: str) -> dict:
    """
    Simulate drag from one widget to another (e.g., drag asset to viewport).

    Args:
        source_path: Source widget path (drag start).
        target_path: Target widget path (drop target).
    """
    return ue_editor.call_plugin("DragWidget", SourcePath=source_path, TargetPath=target_path)


# ---------------------------------------------------------------------------
# Editor command tools (require OhMyUnrealEngine plugin)
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_exec(command: str) -> dict:
    """
    Execute an Unreal Editor console/exec command. Bypasses UI — very powerful.
    Output is captured and returned.

    Examples: "BUILDLIGHTING", "OBJ LIST", "stat fps", "stat unit",
              "MAP SAVE", "ACTOR SELECT ALL", etc.

    Args:
        command: The editor command to execute.
    """
    return ue_editor.call_plugin("ExecuteCommand", Command=command)


@mcp.tool()
def ue_cvar(name: str, value: str = "") -> dict:
    """
    Get or set a console variable (cvar).

    Args:
        name: CVar name (e.g. "r.DetailMode", "t.MaxFPS").
        value: If non-empty, sets the cvar. If empty, reads the current value.
    """
    return ue_editor.call_plugin("ConsoleVariable", Name=name, Value=value)


@mcp.tool()
def ue_double_click_widget(widget_path: str, button: str = "left") -> dict:
    """
    Simulate a double-click on a widget. Useful for opening assets,
    expanding tree nodes, etc.

    Args:
        widget_path: Widget path from ue_get_widget_tree or ue_search_widgets.
        button: "left", "right", or "middle". Default "left".
    """
    return ue_editor.call_plugin("DoubleClickWidget", WidgetPath=widget_path, Button=button)


# ---------------------------------------------------------------------------
# Asset tools (require OhMyUnrealEngine plugin)
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_list_assets(
    path: str = "/Game",
    filter: str = "",
    recursive: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """
    List assets in a content directory with optional class filter.
    Returns paginated results.

    Args:
        path: Content path (e.g. "/Game", "/Game/Maps", "/Game/Blueprints").
        filter: Optional class filter (e.g. "Blueprint", "StaticMesh", "Material").
        recursive: Search subdirectories. Default False.
        offset: Pagination offset.
        limit: Max results. Default 50.
    """
    return ue_editor.call_plugin(
        "ListAssets", Path=path, Filter=filter,
        Recursive=recursive, Offset=offset, Limit=limit,
    )


@mcp.tool()
def ue_get_asset_info(asset_path: str) -> dict:
    """
    Get detailed information about a specific asset (class, tags, etc.).

    Args:
        asset_path: Full asset path (e.g. "/Game/Maps/MyMap.MyMap").
    """
    return ue_editor.call_plugin("GetAssetInfo", AssetPath=asset_path)


@mcp.tool()
def ue_delete_asset(asset_path: str) -> dict:
    """
    Delete an asset. Use with caution — this is irreversible.

    Args:
        asset_path: Full path to asset (e.g. "/Game/Blueprints/MyBP").
    """
    return ue_editor.call_plugin("DeleteAsset", AssetPath=asset_path)


@mcp.tool()
def ue_duplicate_asset(source_path: str, dest_name: str, dest_path: str) -> dict:
    """
    Duplicate an asset to a new location.

    Args:
        source_path: Source asset path.
        dest_name: Name for the new asset.
        dest_path: Destination package path (e.g. "/Game/NewFolder").
    """
    return ue_editor.call_plugin(
        "DuplicateAsset", SourcePath=source_path, DestName=dest_name, DestPath=dest_path,
    )


# ---------------------------------------------------------------------------
# Actor tools (require OhMyUnrealEngine plugin)
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_list_actors(
    class_filter: str = "",
    name_filter: str = "",
    limit: int = 50,
) -> dict:
    """
    List actors in the current editor level.

    Args:
        class_filter: Optional class name filter (e.g. "StaticMeshActor", "PointLight").
        name_filter: Optional substring match on actor name or label.
        limit: Max results. Default 50.
    """
    return ue_editor.call_plugin(
        "ListActors", ClassFilter=class_filter, NameFilter=name_filter, Limit=limit,
    )


@mcp.tool()
def ue_get_actor_info(actor_name: str) -> dict:
    """
    Get detailed info about an actor (transform, components, tags).

    Args:
        actor_name: Actor name or label from ue_list_actors.
    """
    return ue_editor.call_plugin("GetActorInfo", ActorName=actor_name)


@mcp.tool()
def ue_set_actor_transform(
    actor_name: str,
    location_x: float = 0, location_y: float = 0, location_z: float = 0,
    rotation_pitch: float = 0, rotation_yaw: float = 0, rotation_roll: float = 0,
    scale_x: float = 1, scale_y: float = 1, scale_z: float = 1,
) -> dict:
    """
    Set an actor's location, rotation, and scale.

    Args:
        actor_name: Actor name or label.
        location_x/y/z: World position.
        rotation_pitch/yaw/roll: Rotation in degrees.
        scale_x/y/z: Scale factors. Default 1.
    """
    return ue_editor.call_plugin(
        "SetActorTransform", ActorName=actor_name,
        LocationX=location_x, LocationY=location_y, LocationZ=location_z,
        RotationPitch=rotation_pitch, RotationYaw=rotation_yaw, RotationRoll=rotation_roll,
        ScaleX=scale_x, ScaleY=scale_y, ScaleZ=scale_z,
    )


@mcp.tool()
def ue_delete_actor(actor_name: str) -> dict:
    """
    Delete an actor from the current level.

    Args:
        actor_name: Actor name or label.
    """
    return ue_editor.call_plugin("DeleteActor", ActorName=actor_name)


@mcp.tool()
def ue_spawn_actor(
    class_name: str,
    label: str = "",
    location_x: float = 0, location_y: float = 0, location_z: float = 0,
    rotation_pitch: float = 0, rotation_yaw: float = 0, rotation_roll: float = 0,
) -> dict:
    """
    Spawn a new actor in the current level.

    Args:
        class_name: Actor class (e.g. "StaticMeshActor", "PointLight", "CameraActor",
                    "PlayerStart", "TriggerBox", "BlockingVolume").
        label: Display label for the new actor.
        location_x/y/z: World position.
        rotation_pitch/yaw/roll: Rotation in degrees.
    """
    return ue_editor.call_plugin(
        "SpawnActor", ClassName=class_name, Label=label,
        LocationX=location_x, LocationY=location_y, LocationZ=location_z,
        RotationPitch=rotation_pitch, RotationYaw=rotation_yaw, RotationRoll=rotation_roll,
    )


@mcp.tool()
def ue_select_actors(actor_names: str = "") -> dict:
    """
    Select actors in the editor viewport. Empty = deselect all.

    Args:
        actor_names: Comma-separated actor names. Empty to deselect all.
    """
    return ue_editor.call_plugin("SelectActors", ActorNames=actor_names)


# ---------------------------------------------------------------------------
# Blueprint tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_create_blueprint(name: str, path: str = "/Game", parent_class: str = "Actor") -> dict:
    """
    Create a new Blueprint class.

    Args:
        name: Blueprint name.
        path: Package path (e.g. "/Game/Blueprints").
        parent_class: Parent class (e.g. "Actor", "Pawn", "Character", "PlayerController").
    """
    return ue_editor.call_plugin("CreateBlueprint", Name=name, Path=path, ParentClass=parent_class)


@mcp.tool()
def ue_add_blueprint_variable(
    blueprint_path: str, var_name: str, var_type: str, default_value: str = "",
) -> dict:
    """
    Add a variable to a Blueprint.

    Args:
        blueprint_path: Asset path of the Blueprint.
        var_name: Variable name.
        var_type: Type: "bool", "int", "float", "string", "Vector", "Rotator", "Transform".
        default_value: Optional default value.
    """
    return ue_editor.call_plugin(
        "AddBlueprintVariable", BlueprintPath=blueprint_path,
        VarName=var_name, VarType=var_type, DefaultValue=default_value,
    )


@mcp.tool()
def ue_add_blueprint_node(
    blueprint_path: str, function_name: str,
    function_class: str = "", connect_to_event: str = "",
    node_pos_x: int = 300, node_pos_y: int = 0,
) -> dict:
    """
    Add a function call node to a Blueprint's EventGraph.

    Args:
        blueprint_path: Asset path of the Blueprint.
        function_name: Function to call (e.g. "PrintString", "SetActorLocation").
        function_class: Class owning the function (e.g. "KismetSystemLibrary"). Auto-detected if empty.
        connect_to_event: Connect exec pin to this event (e.g. "BeginPlay"). Empty = unconnected.
        node_pos_x/y: Position in graph.
    """
    return ue_editor.call_plugin(
        "AddBlueprintNode", BlueprintPath=blueprint_path,
        FunctionName=function_name, FunctionClass=function_class,
        ConnectToEvent=connect_to_event, NodePosX=node_pos_x, NodePosY=node_pos_y,
    )


@mcp.tool()
def ue_compile_blueprint(blueprint_path: str) -> dict:
    """
    Compile a Blueprint.

    Args:
        blueprint_path: Asset path of the Blueprint.
    """
    return ue_editor.call_plugin("CompileBlueprint", BlueprintPath=blueprint_path)


@mcp.tool()
def ue_get_blueprint_info(blueprint_path: str) -> dict:
    """
    Get Blueprint info: variables, graphs, nodes, parent class, compile status.

    Args:
        blueprint_path: Asset path of the Blueprint.
    """
    return ue_editor.call_plugin("GetBlueprintInfo", BlueprintPath=blueprint_path)


@mcp.tool()
def ue_list_blueprint_nodes(blueprint_path: str, graph_name: str = "") -> dict:
    """
    List all nodes in a Blueprint graph with their pins and connections.
    Essential for understanding graph structure before editing.

    Args:
        blueprint_path: Asset path of the Blueprint.
        graph_name: Graph name (e.g. "EventGraph"). Empty = first ubergraph.
    """
    return ue_editor.call_plugin("ListBlueprintNodes", BlueprintPath=blueprint_path, GraphName=graph_name)


@mcp.tool()
def ue_remove_blueprint_node(blueprint_path: str, node_name: str) -> dict:
    """
    Remove a node from a Blueprint graph.

    Args:
        blueprint_path: Asset path of the Blueprint.
        node_name: Node name to remove (from ue_list_blueprint_nodes).
    """
    return ue_editor.call_plugin("RemoveBlueprintNode", BlueprintPath=blueprint_path, NodeName=node_name)


@mcp.tool()
def ue_add_blueprint_variable_node(
    blueprint_path: str, var_name: str, setter: bool = False,
    node_pos_x: int = 0, node_pos_y: int = 0,
) -> dict:
    """
    Add a variable Get or Set node to a Blueprint event graph.

    Args:
        blueprint_path: Asset path of the Blueprint.
        var_name: Variable name (must exist in the Blueprint).
        setter: True for Set node, False for Get node.
        node_pos_x/y: Position in graph.
    """
    return ue_editor.call_plugin(
        "AddBlueprintVariableNode", BlueprintPath=blueprint_path,
        VarName=var_name, bSetter=setter, NodePosX=node_pos_x, NodePosY=node_pos_y,
    )


@mcp.tool()
def ue_add_blueprint_cast_node(
    blueprint_path: str, target_class: str, pure: bool = False,
    node_pos_x: int = 0, node_pos_y: int = 0,
) -> dict:
    """
    Add a Cast To node to a Blueprint graph.

    Args:
        blueprint_path: Asset path of the Blueprint.
        target_class: Class to cast to (e.g. "ShooterCharacter", "Pawn").
        pure: Pure cast (no exec pins). Default False.
        node_pos_x/y: Position in graph.
    """
    return ue_editor.call_plugin(
        "AddBlueprintCastNode", BlueprintPath=blueprint_path,
        TargetClass=target_class, bPure=pure,
        NodePosX=node_pos_x, NodePosY=node_pos_y,
    )


@mcp.tool()
def ue_add_blueprint_branch_node(
    blueprint_path: str, node_pos_x: int = 0, node_pos_y: int = 0,
) -> dict:
    """
    Add a Branch (if/else) node to a Blueprint graph.

    Args:
        blueprint_path: Asset path of the Blueprint.
        node_pos_x/y: Position in graph.
    """
    return ue_editor.call_plugin(
        "AddBlueprintBranchNode", BlueprintPath=blueprint_path,
        NodePosX=node_pos_x, NodePosY=node_pos_y,
    )


@mcp.tool()
def ue_add_blueprint_generic_node(
    blueprint_path: str, node_class_name: str,
    node_pos_x: int = 0, node_pos_y: int = 0,
) -> dict:
    """
    Add any Blueprint node by its class name. The ultimate escape hatch.

    Common node classes:
    - K2Node_Self: self reference
    - K2Node_IfThenElse: branch
    - K2Node_DynamicCast: cast
    - K2Node_MakeArray: make array
    - K2Node_Select: select
    - K2Node_ForEachElementInEnum: for each enum

    Args:
        blueprint_path: Asset path of the Blueprint.
        node_class_name: UClass name of the node.
        node_pos_x/y: Position in graph.
    """
    return ue_editor.call_plugin(
        "AddBlueprintGenericNode", BlueprintPath=blueprint_path,
        NodeClassName=node_class_name,
        NodePosX=node_pos_x, NodePosY=node_pos_y,
    )


@mcp.tool()
def ue_add_blueprint_component(
    blueprint_path: str, component_class: str,
    component_name: str = "", attach_to_root: bool = True,
) -> dict:
    """
    Add a component to a Blueprint CLASS (template level).
    All instances of this Blueprint will have this component.

    Args:
        blueprint_path: Asset path of the Blueprint.
        component_class: Component class (e.g. "StaticMeshComponent", "SphereComponent",
                         "PointLightComponent", "BoxCollisionComponent").
        component_name: Name for the component. Auto-generated if empty.
        attach_to_root: Attach to root component. Default True.
    """
    return ue_editor.call_plugin(
        "AddBlueprintComponent", BlueprintPath=blueprint_path,
        ComponentClass=component_class, ComponentName=component_name,
        bAttachToRoot=attach_to_root,
    )


@mcp.tool()
def ue_set_blueprint_component_property(
    blueprint_path: str, component_name: str,
    property_name: str, value: str,
) -> dict:
    """
    Set a property on a Blueprint component template.
    Affects all instances of the Blueprint.

    Examples:
        ue_set_blueprint_component_property(bp, "MeshComp", "StaticMesh",
            "/Game/LootSet/Meshes/Atlas/atlas_gem_01.atlas_gem_01")
        ue_set_blueprint_component_property(bp, "MeshComp", "CollisionEnabled",
            "NoCollision")

    Args:
        blueprint_path: Asset path of the Blueprint.
        component_name: Component variable name in the Blueprint.
        property_name: Property to set.
        value: Value in UE text format.
    """
    return ue_editor.call_plugin(
        "SetBlueprintComponentProperty", BlueprintPath=blueprint_path,
        ComponentName=component_name, PropertyName=property_name, Value=value,
    )


@mcp.tool()
def ue_set_blueprint_pin_value(
    blueprint_path: str, node_name: str, pin_name: str, value: str,
) -> dict:
    """
    Set a default value on a Blueprint node pin.

    Args:
        blueprint_path: Asset path of the Blueprint.
        node_name: Node name (from ue_add_blueprint_node result).
        pin_name: Pin name (from the node's pins list).
        value: Default value as string.
    """
    return ue_editor.call_plugin(
        "SetBlueprintPinValue", BlueprintPath=blueprint_path,
        NodeName=node_name, PinName=pin_name, Value=value,
    )


@mcp.tool()
def ue_connect_blueprint_pins(
    blueprint_path: str,
    source_node: str, source_pin: str,
    target_node: str, target_pin: str,
) -> dict:
    """
    Connect two pins between Blueprint nodes.

    Args:
        blueprint_path: Asset path of the Blueprint.
        source_node: Source node name.
        source_pin: Source pin name.
        target_node: Target node name.
        target_pin: Target pin name.
    """
    return ue_editor.call_plugin(
        "ConnectBlueprintPins", BlueprintPath=blueprint_path,
        SourceNodeName=source_node, SourcePinName=source_pin,
        TargetNodeName=target_node, TargetPinName=target_pin,
    )


# ---------------------------------------------------------------------------
# PIE (Play In Editor) tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_start_pie() -> dict:
    """Start Play In Editor — run the game in the editor viewport."""
    return ue_editor.call_plugin("StartPIE")


@mcp.tool()
def ue_stop_pie() -> dict:
    """Stop Play In Editor."""
    return ue_editor.call_plugin("StopPIE")


@mcp.tool()
def ue_pie_status() -> dict:
    """Check if Play In Editor is running (and if paused)."""
    return ue_editor.call_plugin("GetPIEStatus")


# ---------------------------------------------------------------------------
# Viewport camera tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_get_viewport_camera() -> dict:
    """Get the editor viewport camera position and rotation."""
    return ue_editor.call_plugin("GetViewportCamera")


@mcp.tool()
def ue_set_viewport_camera(
    location_x: float = 0, location_y: float = 0, location_z: float = 0,
    rotation_pitch: float = 0, rotation_yaw: float = 0, rotation_roll: float = 0,
) -> dict:
    """
    Set the editor viewport camera position and rotation.

    Args:
        location_x/y/z: Camera world position.
        rotation_pitch/yaw/roll: Camera rotation in degrees.
    """
    return ue_editor.call_plugin(
        "SetViewportCamera",
        LocationX=location_x, LocationY=location_y, LocationZ=location_z,
        RotationPitch=rotation_pitch, RotationYaw=rotation_yaw, RotationRoll=rotation_roll,
    )


# ---------------------------------------------------------------------------
# Editor utility tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_undo() -> dict:
    """Undo the last editor action."""
    return ue_editor.call_plugin("Undo")


@mcp.tool()
def ue_redo() -> dict:
    """Redo the last undone action."""
    return ue_editor.call_plugin("Redo")


@mcp.tool()
def ue_save_all() -> dict:
    """Save all modified assets and levels."""
    return ue_editor.call_plugin("SaveAll")


# ---------------------------------------------------------------------------
# Level tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_get_current_level() -> dict:
    """Get info about the current level (name, actor count)."""
    return ue_editor.call_plugin("GetCurrentLevel")


@mcp.tool()
def ue_load_level(level_path: str) -> dict:
    """
    Load a level by content path.

    Args:
        level_path: Content path (e.g. "/Game/Maps/MyMap").
    """
    return ue_editor.call_plugin("LoadLevel", LevelPath=level_path)


@mcp.tool()
def ue_new_level(level_path: str = "/Game/Maps/NewLevel") -> dict:
    """
    Create a new empty level.

    Args:
        level_path: Desired content path for the new level.
    """
    return ue_editor.call_plugin("NewLevel", LevelPath=level_path)


# ---------------------------------------------------------------------------
# Import tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_import_asset(file_path: str, dest_path: str = "/Game", dest_name: str = "") -> dict:
    """
    Import an external file as a UE asset (FBX, PNG, WAV, etc.).

    Args:
        file_path: Absolute path to the file on disk.
        dest_path: Destination content path (e.g. "/Game/Meshes").
        dest_name: Optional asset name. Auto-derived from filename if empty.
    """
    return ue_editor.call_plugin("ImportAsset", FilePath=file_path, DestPath=dest_path, DestName=dest_name)


# ---------------------------------------------------------------------------
# Python scripting
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_run_python(code: str) -> dict:
    """
    Execute Python code inside the UE editor. Requires PythonScriptPlugin.
    This is the ultimate escape hatch — any UE operation expressible in Python
    can be done through this tool.

    Example:
        ue_run_python("import unreal; print(unreal.EditorAssetLibrary.list_assets('/Game/'))")

    Args:
        code: Python code to execute. Can be multi-line.
    """
    return ue_editor.call_plugin("RunPython", Code=code)


# ---------------------------------------------------------------------------
# Component tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_add_component(actor_name: str, component_class: str, component_name: str = "") -> dict:
    """
    Add a component to an actor.

    Args:
        actor_name: Actor name.
        component_class: Component class (e.g. "StaticMeshComponent", "PointLightComponent",
                         "BoxCollisionComponent", "AudioComponent", "ArrowComponent").
        component_name: Optional name. Auto-generated if empty.
    """
    return ue_editor.call_plugin(
        "AddComponent", ActorName=actor_name,
        ComponentClass=component_class, ComponentName=component_name,
    )


# ---------------------------------------------------------------------------
# Hierarchy tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_get_actor_hierarchy(actor_name: str = "") -> dict:
    """
    Get actor parent/child hierarchy. Empty = show root actors.

    Args:
        actor_name: Actor name. Empty shows all root actors with child counts.
    """
    return ue_editor.call_plugin("GetActorHierarchy", ActorName=actor_name)


@mcp.tool()
def ue_attach_actor(child_name: str, parent_name: str) -> dict:
    """
    Attach an actor to a parent actor.

    Args:
        child_name: Actor to attach.
        parent_name: Parent actor.
    """
    return ue_editor.call_plugin("AttachActor", ChildName=child_name, ParentName=parent_name)


@mcp.tool()
def ue_detach_actor(actor_name: str) -> dict:
    """
    Detach an actor from its parent.

    Args:
        actor_name: Actor to detach.
    """
    return ue_editor.call_plugin("DetachActor", ActorName=actor_name)


# ---------------------------------------------------------------------------
# Log / notification tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_get_editor_log(count: int = 20, filter: str = "") -> dict:
    """
    Get recent editor log messages.

    Args:
        count: Number of messages. Default 20.
        filter: Optional text filter.
    """
    return ue_editor.call_plugin("GetEditorLog", Count=count, Filter=filter)


@mcp.tool()
def ue_show_notification(message: str, type: str = "info") -> dict:
    """
    Show a notification popup in the editor.

    Args:
        message: Notification text.
        type: "info", "success", "warning", or "error". Default "info".
    """
    return ue_editor.call_plugin("ShowNotification", Message=message, Type=type)


# ---------------------------------------------------------------------------
# Screenshot tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_screenshot(widget_path: str = "0", filename: str = "") -> dict:
    """
    Take a screenshot of a widget or the main editor window.
    Saves to Saved/Screenshots/ as PNG and returns the file path.

    Args:
        widget_path: Widget path to capture. "0" = main window (default).
        filename: Output filename (auto-generated if empty).
    """
    return ue_editor.call_plugin("TakeScreenshot", WidgetPath=widget_path, Filename=filename)


# ---------------------------------------------------------------------------
# Function call tools — reflection-based, works on any UObject
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_call_function(target: str, function_name: str, args: str = "{}") -> dict:
    """
    Call ANY UFUNCTION on any UObject via UE reflection.
    One tool to replace hundreds of specialized tools.

    Examples:
        ue_call_function("PointLight1", "SetActorHiddenInGame", '{"bNewHidden": "true"}')
        ue_call_function("SM_Cube.StaticMeshComponent0", "SetMaterial",
                         '{"ElementIndex": "0", "Material": "/Game/Materials/M_Red"}')
        ue_call_function("MyActor", "K2_SetActorLocation",
                         '{"NewLocation": "(X=100,Y=200,Z=300)", "bSweep": "false", "bTeleport": "true"}')

    Args:
        target: Actor name, "Actor.Component", or full object path.
        function_name: UFUNCTION name.
        args: JSON object with param name→value pairs. Values in UE text format.
    """
    return ue_editor.call_plugin("CallFunction", Target=target, FunctionName=function_name, Args=args)


@mcp.tool()
def ue_list_functions(target: str, filter: str = "", offset: int = 0, limit: int = 50) -> dict:
    """
    List all callable UFUNCTIONs on any UObject. Use to discover available functions.

    Args:
        target: Actor name, "Actor.Component", or full object path.
        filter: Optional substring filter on function name.
        offset: Pagination offset.
        limit: Max results. Default 50.
    """
    return ue_editor.call_plugin("ListFunctions", Target=target, Filter=filter, Offset=offset, Limit=limit)


@mcp.tool()
def ue_call_static_function(class_name: str, function_name: str, args: str = "{}") -> dict:
    """
    Call a static UFUNCTION on a class (no instance needed).
    WorldContextObject is auto-filled.

    Examples:
        ue_call_static_function("GameplayStatics", "GetAllActorsOfClass",
            '{"ActorClass": "/Script/Engine.PointLight"}')
        ue_call_static_function("KismetSystemLibrary", "GetDisplayName",
            '{"Object": "/Game/MyActor"}')

    Args:
        class_name: Class name (e.g. "GameplayStatics", "KismetSystemLibrary").
        function_name: Static function name.
        args: JSON with param values.
    """
    return ue_editor.call_plugin("CallStaticFunction", ClassName=class_name, FunctionName=function_name, Args=args)


@mcp.tool()
def ue_find_objects(class_name: str, name_filter: str = "", limit: int = 20) -> dict:
    """
    Find UObject instances in memory by class name.

    Args:
        class_name: Class name (e.g. "StaticMesh", "MaterialInstance", "EditorAssetSubsystem").
        name_filter: Optional name substring filter.
        limit: Max results. Default 20.
    """
    return ue_editor.call_plugin("FindObjects", ClassName=class_name, NameFilter=name_filter, Limit=limit)


@mcp.tool()
def ue_get_subsystem(subsystem_name: str) -> dict:
    """
    Get an editor subsystem path. Use the returned path as target for ue_call_function.

    Args:
        subsystem_name: Subsystem class (e.g. "EditorAssetSubsystem", "EditorActorSubsystem").
    """
    return ue_editor.call_plugin("GetSubsystem", SubsystemName=subsystem_name)


@mcp.tool()
def ue_get_recent_logs(count: int = 30, filter: str = "") -> dict:
    """
    Get recent editor log messages captured from memory.
    Much better than reading log files — real-time with category and verbosity.

    Args:
        count: Number of messages. Default 30.
        filter: Optional text filter on message or category.
    """
    return ue_editor.call_plugin("GetRecentLogs", Count=count, Filter=filter)


@mcp.tool()
def ue_get_asset_references(asset_path: str) -> dict:
    """
    Get what an asset depends on and what references it.
    Essential for safe refactoring and understanding asset relationships.

    Args:
        asset_path: Full asset path.
    """
    return ue_editor.call_plugin("GetAssetReferences", AssetPath=asset_path)


@mcp.tool()
def ue_rename_asset(source_path: str, dest_path: str, new_name: str) -> dict:
    """
    Rename or move an asset.

    Args:
        source_path: Current asset path.
        dest_path: New package path.
        new_name: New asset name.
    """
    return ue_editor.call_plugin("RenameAsset", SourcePath=source_path, DestPath=dest_path, NewName=new_name)


# ---------------------------------------------------------------------------
# Property tools — reflection-based, works on any UObject
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_list_properties(target: str, offset: int = 0, limit: int = 50) -> dict:
    """
    List all properties of any UObject via UE reflection.
    Works on actors, components, assets — anything.

    Args:
        target: Actor name, "ActorName.ComponentName", or full object path.
        offset: Pagination offset.
        limit: Max properties to return. Default 50.
    """
    return ue_editor.call_plugin("ListProperties", Target=target, Offset=offset, Limit=limit)


@mcp.tool()
def ue_get_property(target: str, property_name: str) -> dict:
    """
    Get any property value from a UObject via UE reflection.
    Returns the value in UE text format.

    Args:
        target: Actor name, "ActorName.ComponentName", or full object path.
        property_name: Property name (e.g. "RelativeLocation", "Mobility", "bHidden").
    """
    return ue_editor.call_plugin("GetProperty", Target=target, PropertyName=property_name)


@mcp.tool()
def ue_set_property(target: str, property_name: str, value: str) -> dict:
    """
    Set any property on a UObject via UE reflection.
    Value uses UE text format (same as editor copy/paste).

    Examples:
        ue_set_property("SM_Cube16", "RelativeLocation", "(X=100,Y=200,Z=300)")
        ue_set_property("SM_Cube16", "bHidden", "true")
        ue_set_property("SM_Cube16.StaticMeshComponent0", "CastShadow", "false")
        ue_set_property("PointLight1.PointLightComponent0", "Intensity", "5000")

    Args:
        target: Actor name, "ActorName.ComponentName", or full object path.
        property_name: Property name.
        value: New value in UE text format.
    """
    return ue_editor.call_plugin("SetProperty", Target=target, PropertyName=property_name, Value=value)
