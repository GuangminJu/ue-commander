"""
MCP server exposing UE launch/close/compile tools.

Design principle: AI should never need to know the exact paths or command syntax.
Every operation goes through this server, which uses the detected config.
"""

from typing import Literal
import threading

from mcp.server.fastmcp import FastMCP

from .config import detect_config, find_uproject, BuildConfig, BuildPlatform, BuildTarget
from . import ue_process, ue_build, ue_discover, ue_editor, ue_debug
from .bridge.capability_registry import CapabilityRegistry
from .bridge.plugin_bridge import PluginBridge
from .ue_build_session import BuildSessionStore
from .ue_launch_session import LaunchSessionStore

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

_capability_registry = CapabilityRegistry()
_plugin_bridge = PluginBridge(mcp, _capability_registry)

# Lazy-init config — resolved once per server session
_cfg = None
_build_store = None
_launch_store = None


def _get_cfg():
    global _cfg
    if _cfg is None:
        uproject = find_uproject()
        _cfg = detect_config(uproject)
    return _cfg


def _get_build_store():
    global _build_store
    if _build_store is None:
        _build_store = BuildSessionStore(_get_cfg())
    return _build_store


def _get_launch_store():
    global _launch_store
    if _launch_store is None:
        _launch_store = LaunchSessionStore(_get_cfg())
    return _launch_store


def _derive_launch_phase(plugin_ready: bool, game_thread_ok: bool, editor_running: bool) -> str:
    if not editor_running:
        return "closed"
    if plugin_ready and game_thread_ok:
        return "ready"
    if plugin_ready and not game_thread_ok:
        return "blocked"
    return "loading"


def _build_session_response(session, *, include_full_log: bool) -> dict:
    import os

    log_text = ""
    if session.log_path and os.path.exists(session.log_path):
        try:
            with open(session.log_path, encoding="utf-8", errors="replace") as f:
                log_text = f.read()
        except Exception:
            pass

    response = {
        "build_id": session.build_id,
        "running": session.status in {"queued", "running"},
        "status": session.status,
        "result": session.result,
        "ok": session.result == "succeeded",
        "started_at": session.started_at,
        "finished_at": session.finished_at,
        "config": session.config,
        "target": session.target,
        "platform": session.platform,
        "project_path": session.project_path,
        "command": session.command,
        "log_path": session.log_path,
        "log_file": session.log_path,
        "exit_code": session.exit_code,
        "return_code": session.exit_code,
        "artifact_status": session.artifact_status,
        "error_count": session.errors,
        "warning_count": session.warnings,
        "errors": session.error_lines,
        "warnings": session.warning_lines,
    }
    if include_full_log:
        response["log"] = log_text if log_text else session.output_tail
    elif session.status in {"queued", "running"}:
        response["log_tail"] = "".join(log_text.splitlines(keepends=True)[-30:])
    return response


def _capability_step(tool_name: str, *, purpose: str, params: dict | None = None, optional: bool = False) -> dict:
    capability = _capability_registry.get(f"ue_{tool_name.lower()}")
    step = {
        "tool": capability.name if capability is not None else tool_name,
        "mcp_tool": f"ue_{tool_name.lower()}",
        "purpose": purpose,
        "optional": optional,
        "params": params or {},
    }
    if capability is not None:
        step["category"] = capability.category
        step["workflow_hint"] = capability.workflow_hint
        step["recommended_reads"] = capability.recommended_reads
    return step


def _blueprint_mutation_policy() -> dict:
    return {
        "preferred_path": [
            "GetBlueprintInfo",
            "ListBlueprintGraphs",
            "ListBlueprintNodes",
            "GetBlueprintGraph",
            "AddBlueprintNodeByType",
            "ConnectBlueprintPinsByGuid",
            "SetBlueprintPinValueByGuid",
            "RemoveBlueprintNodeByGuid",
            "CompileBlueprint",
            "ValidateBlueprintDeep",
        ],
        "compatibility_tools": [
            "AddBlueprintNode",
            "ConnectBlueprintPins",
            "SetBlueprintPinValue",
        ],
        "conditional_tools": [
            "ConnectPinChain",
            "DisconnectBlueprintPins",
            "AddBlueprintGenericNode",
        ],
        "rules": [
            "Prefer GUID-based mutation after raw graph reads when exact node identity matters.",
            "Use name-based mutation tools only as compatibility helpers for simple, unambiguous graphs.",
            "Use escape-hatch and batch rewiring tools only when specialized structured tools are insufficient.",
            "Compile and deep-validate after structural Blueprint edits before handoff.",
        ],
    }


def _widget_interaction_policy() -> dict:
    return {
        "preferred_path": [
            "SearchWidgets",
            "FocusWidget",
            "TypeText",
            "ClickWidget",
            "PressKey",
            "TakeScreenshot",
        ],
        "conditional_tools": [
            "DoubleClickWidget",
            "DragWidget",
            "ScrollWidget",
        ],
        "rules": [
            "Locate the widget first before trying to click, type, or drag.",
            "Set focus before keyboard or text input when the target control is ambiguous.",
            "Use drag, double-click, and scroll only when the UI flow specifically requires those gestures.",
            "Capture a screenshot or read widget state after mutation when visual confirmation matters.",
        ],
    }


def _normalize_widget_workflow_intent(intent: str) -> str:
    normalized = (intent or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "inspect": "inspect_widget",
        "inspect_widget": "inspect_widget",
        "click": "click_widget_flow",
        "click_widget": "click_widget_flow",
        "click_widget_flow": "click_widget_flow",
        "type": "text_input_flow",
        "text_input": "text_input_flow",
        "text_input_flow": "text_input_flow",
        "drag": "drag_widget_flow",
        "drag_widget": "drag_widget_flow",
        "drag_widget_flow": "drag_widget_flow",
    }
    return aliases.get(normalized, normalized)


def _widget_workflow_steps(intent: str, query: str, widget_path: str) -> list[dict]:
    if intent == "inspect_widget":
        return [
            _capability_step("search_widgets", purpose="Locate candidate widgets by text or type before interaction.", params={"query": query}),
            _capability_step("take_screenshot", purpose="Capture the widget or window for visual confirmation after discovery.", params={"widget_path": widget_path}, optional=True),
        ]
    if intent == "click_widget_flow":
        return [
            _capability_step("search_widgets", purpose="Locate the target widget before clicking.", params={"query": query}),
            _capability_step("focus_widget", purpose="Move focus to the target widget when focus state matters.", params={"widget_path": widget_path}, optional=True),
            _capability_step("click_widget", purpose="Perform the primary click interaction on the resolved widget.", params={"widget_path": widget_path}),
            _capability_step("take_screenshot", purpose="Capture the post-click UI state when confirmation is needed.", params={"widget_path": widget_path}, optional=True),
        ]
    if intent == "text_input_flow":
        return [
            _capability_step("search_widgets", purpose="Locate the target text widget before sending input.", params={"query": query}),
            _capability_step("focus_widget", purpose="Ensure the correct widget owns keyboard focus.", params={"widget_path": widget_path}),
            _capability_step("type_text", purpose="Send the desired text to the focused widget.", params={"widget_path": widget_path}),
            _capability_step("press_key", purpose="Send follow-up keys such as Enter or Tab if the flow needs them.", params={"widget_path": widget_path}, optional=True),
            _capability_step("take_screenshot", purpose="Capture the resulting UI state after text input.", params={"widget_path": widget_path}, optional=True),
        ]
    if intent == "drag_widget_flow":
        return [
            _capability_step("search_widgets", purpose="Locate the widgets involved in the drag flow.", params={"query": query}),
            _capability_step("drag_widget", purpose="Execute the drag-and-drop interaction.", params={"source_path": widget_path}, optional=False),
            _capability_step("scroll_widget", purpose="Scroll if the drop target is off-screen or clipped.", params={"widget_path": widget_path}, optional=True),
            _capability_step("take_screenshot", purpose="Capture the UI state after drag-and-drop completes.", params={"widget_path": widget_path}, optional=True),
        ]
    raise ValueError(f"Unsupported widget workflow intent: {intent}")


def _normalize_blueprint_workflow_intent(intent: str) -> str:
    normalized = (intent or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "inspect": "inspect_blueprint",
        "read": "inspect_blueprint",
        "inspect_blueprint": "inspect_blueprint",
        "edit": "guided_graph_edit",
        "edit_graph": "guided_graph_edit",
        "guided_graph_edit": "guided_graph_edit",
        "guid_edit": "guided_graph_edit",
        "add_function": "create_function_flow",
        "create_function": "create_function_flow",
        "create_function_flow": "create_function_flow",
        "disconnect": "disconnect_pin_flow",
        "disconnect_pin": "disconnect_pin_flow",
        "disconnect_pin_flow": "disconnect_pin_flow",
        "replace_node": "replace_node_flow",
        "replace_node_flow": "replace_node_flow",
        "event_entry": "event_entry_flow",
        "event_entry_flow": "event_entry_flow",
        "function_signature": "function_signature_flow",
        "function_signature_flow": "function_signature_flow",
        "default_value": "default_value_flow",
        "default_pin_value": "default_value_flow",
        "default_value_flow": "default_value_flow",
        "set_pin": "set_pin_value_flow",
        "set_pin_value": "set_pin_value_flow",
        "set_pin_value_flow": "set_pin_value_flow",
    }
    return aliases.get(normalized, normalized)


def _blueprint_workflow_steps(intent: str, blueprint_path: str, graph_name: str) -> list[dict]:
    if intent == "inspect_blueprint":
        return [
            _capability_step("get_blueprint_info", purpose="Read Blueprint resource summary first.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_graphs", purpose="Choose the target graph without loading raw graph state.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_nodes", purpose="Inspect node index and pin counts before heavier reads.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "detail"}),
            _capability_step("get_blueprint_graph", purpose="Escalate to raw graph only if exact GUID-level links are required.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "raw"}, optional=True),
        ]
    if intent == "guided_graph_edit":
        return [
            _capability_step("get_blueprint_info", purpose="Confirm Blueprint identity and overall shape.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_graphs", purpose="Pick the graph to mutate.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_nodes", purpose="Read node index before adding or wiring nodes.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "detail"}),
            _capability_step("add_blueprint_node_by_type", purpose="Create structured graph nodes with stable GUIDs.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("get_blueprint_graph", purpose="Fetch raw graph state when exact GUID connections are needed.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "raw"}),
            _capability_step("connect_blueprint_pins_by_guid", purpose="Perform precise pin connections using GUIDs from the raw graph.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("auto_layout_blueprint_graph", purpose="Normalize graph layout after structural edits.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("compile_blueprint", purpose="Surface compile regressions immediately after edits.", params={"blueprint_path": blueprint_path}),
            _capability_step("validate_blueprint_deep", purpose="Catch broken pins, phantom errors, and duplicate names before handoff.", params={"blueprint_path": blueprint_path, "b_auto_fix": False}),
        ]
    if intent == "create_function_flow":
        return [
            _capability_step("get_blueprint_info", purpose="Inspect Blueprint structure before adding a new function graph.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_graphs", purpose="Read current graph index to avoid naming collisions.", params={"blueprint_path": blueprint_path, "detail_level": "detail"}),
            _capability_step("create_blueprint_function", purpose="Create the target function graph.", params={"blueprint_path": blueprint_path}),
            _capability_step("modify_blueprint_function_params", purpose="Adjust function inputs or outputs after graph creation.", params={"blueprint_path": blueprint_path}, optional=True),
            _capability_step("list_blueprint_nodes", purpose="Inspect the new function graph before inserting logic.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "detail"}),
            _capability_step("add_blueprint_node_by_type", purpose="Insert function body nodes.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("auto_layout_blueprint_graph", purpose="Keep the new function graph readable.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("compile_blueprint", purpose="Compile after function creation and graph edits.", params={"blueprint_path": blueprint_path}),
            _capability_step("validate_blueprint_deep", purpose="Run deep validation before using the new function downstream.", params={"blueprint_path": blueprint_path, "b_auto_fix": False}),
        ]
    if intent == "event_entry_flow":
        return [
            _capability_step("get_blueprint_info", purpose="Confirm the Blueprint target before adding an event entry.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_graphs", purpose="Select the graph that should receive the custom event.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_nodes", purpose="Inspect the existing graph before inserting a new event entry node.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "detail"}),
            _capability_step("create_blueprint_custom_event", purpose="Create the custom event entry point in the target graph.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("add_blueprint_node_by_type", purpose="Insert downstream logic that the custom event should drive.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("get_blueprint_graph", purpose="Fetch raw graph state if the new event must be wired by GUID.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "raw"}),
            _capability_step("connect_blueprint_pins_by_guid", purpose="Connect the custom event to the new downstream logic precisely.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("auto_layout_blueprint_graph", purpose="Keep the entry flow readable after event insertion.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("compile_blueprint", purpose="Compile after adding the event entry flow.", params={"blueprint_path": blueprint_path}),
            _capability_step("validate_blueprint_deep", purpose="Validate after event insertion to catch broken execution links.", params={"blueprint_path": blueprint_path, "b_auto_fix": False}),
        ]
    if intent == "default_value_flow":
        return [
            _capability_step("get_blueprint_info", purpose="Confirm the Blueprint target before editing pin defaults.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_nodes", purpose="Inspect the target graph to locate the node and target pin.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "detail"}),
            _capability_step("set_blueprint_pin_value", purpose="Compatibility path for simple name-based pin default edits.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}, optional=True),
            _capability_step("get_blueprint_graph", purpose="Escalate to raw graph when exact GUID-level pin editing is required.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "raw"}),
            _capability_step("set_blueprint_pin_value_by_guid", purpose="Preferred precise pin default edit using the node GUID.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("compile_blueprint", purpose="Compile after changing pin defaults.", params={"blueprint_path": blueprint_path}),
            _capability_step("validate_blueprint_deep", purpose="Validate after the default value update before handoff.", params={"blueprint_path": blueprint_path, "b_auto_fix": False}, optional=True),
        ]
    if intent == "set_pin_value_flow":
        return [
            _capability_step("get_blueprint_info", purpose="Confirm the Blueprint target before GUID-based pin edits.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("get_blueprint_graph", purpose="Load raw graph to discover stable node GUIDs and pin names.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "raw"}),
            _capability_step("set_blueprint_pin_value_by_guid", purpose="Set the target pin value using exact node GUID and pin name.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("compile_blueprint", purpose="Compile immediately after mutating pin defaults.", params={"blueprint_path": blueprint_path}),
            _capability_step("validate_blueprint_deep", purpose="Validate to catch latent graph breakage after the pin update.", params={"blueprint_path": blueprint_path, "b_auto_fix": False}, optional=True),
        ]
    if intent == "disconnect_pin_flow":
        return [
            _capability_step("get_blueprint_info", purpose="Confirm the Blueprint and avoid rewiring the wrong asset.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_graphs", purpose="Select the graph that contains the link to remove.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_nodes", purpose="Inspect candidate node names and pins before disconnecting.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "detail"}),
            _capability_step("get_blueprint_graph", purpose="Escalate to raw graph if the node has ambiguous wiring.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "raw"}, optional=True),
            _capability_step("disconnect_blueprint_pins", purpose="Break the incorrect connection or all links on the target node pin.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("compile_blueprint", purpose="Compile after disconnecting to surface broken execution paths quickly.", params={"blueprint_path": blueprint_path}),
            _capability_step("validate_blueprint_deep", purpose="Deep-validate after rewiring cleanup before handoff.", params={"blueprint_path": blueprint_path, "b_auto_fix": False}, optional=True),
        ]
    if intent == "replace_node_flow":
        return [
            _capability_step("get_blueprint_info", purpose="Confirm the Blueprint target and current asset context.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_graphs", purpose="Choose the graph where the node replacement will occur.", params={"blueprint_path": blueprint_path, "detail_level": "summary"}),
            _capability_step("list_blueprint_nodes", purpose="Inspect the existing node index before replacing anything.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "detail"}),
            _capability_step("get_blueprint_graph", purpose="Read raw graph state to capture exact GUIDs and existing links.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "raw"}),
            _capability_step("add_blueprint_node_by_type", purpose="Insert the replacement node and capture its GUID.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("connect_pin_chain", purpose="Reconnect simple name-based links in batch where pin names are stable.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}, optional=True),
            _capability_step("connect_blueprint_pins_by_guid", purpose="Perform precise reconnects for ambiguous or GUID-level wiring.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("remove_blueprint_node_by_guid", purpose="Delete the obsolete node after replacement wiring is in place.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("auto_layout_blueprint_graph", purpose="Clean up graph readability after replacement.", params={"blueprint_path": blueprint_path, "graph_name": graph_name}),
            _capability_step("compile_blueprint", purpose="Compile immediately after node replacement.", params={"blueprint_path": blueprint_path}),
            _capability_step("validate_blueprint_deep", purpose="Catch latent pin or type regressions after replacement.", params={"blueprint_path": blueprint_path, "b_auto_fix": False}),
        ]
    if intent == "function_signature_flow":
        return [
            _capability_step("get_blueprint_info", purpose="Inspect Blueprint detail before editing function signatures.", params={"blueprint_path": blueprint_path, "detail_level": "detail"}),
            _capability_step("list_blueprint_graphs", purpose="Confirm the target function graph and current signature.", params={"blueprint_path": blueprint_path, "detail_level": "detail"}),
            _capability_step("modify_blueprint_function_params", purpose="Apply the function input or output signature change.", params={"blueprint_path": blueprint_path}),
            _capability_step("list_blueprint_graphs", purpose="Re-read graph summaries to confirm the new signature.", params={"blueprint_path": blueprint_path, "detail_level": "detail"}),
            _capability_step("list_blueprint_nodes", purpose="Inspect the affected function graph for downstream node impacts.", params={"blueprint_path": blueprint_path, "graph_name": graph_name, "detail_level": "detail"}, optional=True),
            _capability_step("compile_blueprint", purpose="Compile after the signature change to surface broken call sites.", params={"blueprint_path": blueprint_path}),
            _capability_step("validate_blueprint_deep", purpose="Run deep validation after signature edits before handoff.", params={"blueprint_path": blueprint_path, "b_auto_fix": False}),
        ]
    raise ValueError(f"Unsupported Blueprint workflow intent: {intent}")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ue_status(log_lines: int = 0) -> dict:
    """
    Check whether any Unreal Editor is currently running.
    Returns process info (PID, memory, uptime, which project is loaded) if running.
    Also probes the plugin HTTP endpoint to report whether the editor
    is fully loaded and ready to accept commands (plugin_ready field).

    When the editor is in "loading" phase and log_lines > 0, includes the
    tail of the editor log so you can monitor startup progress.

    Args:
        log_lines: Number of log tail lines to include (0 = no log, default).
                   Useful during loading phase to see startup progress.
    """
    cfg = _get_cfg()
    info = ue_process.get_status(cfg)
    monitor = ue_process.get_monitor()
    bridge_state = _plugin_bridge.get_state()
    plugin_ready = bridge_state.plugin_ready
    launch_store = _get_launch_store()
    active_launch = launch_store.find_by_pid(info.pid) or launch_store.get_active_session()

    # Check monitor for crash — but ONLY if the process is actually dead.
    # If the process is running (new launch), stale monitor crash data
    # from a previous session must not override the live status.
    if monitor and monitor.crashed and not info.running:
        if active_launch is not None:
            active_launch = launch_store.mark_closed(active_launch.launch_id, phase="failed")
        result = {
            "project": info.project or cfg.project_name,
            "engine_path": str(cfg.engine_path),
            "editor_running": False,
            "phase": "crashed",
            "crash_reason": monitor.crash_reason,
            "exit_code": monitor.exit_code,
            "recent_log": monitor.recent_log[-10:],
        }
        if cfg.ide_build:
            result["ide_build_config"] = {
                "config": cfg.ide_build.config,
                "target": cfg.ide_build.target,
                "platform": cfg.ide_build.platform,
                "detected_from": cfg.ide_build.source,
            }
        if active_launch is not None:
            result["launch_id"] = active_launch.launch_id
            result["launch_status"] = active_launch.status
            result["linked_build_id"] = active_launch.linked_build_id
        return result

    # If any UE process is running (even a different project), report it
    editor_running = info.running or plugin_ready
    result = {
        "project": info.project if info.running else cfg.project_name,
        "engine_path": str(cfg.engine_path),
        "editor_running": editor_running,
    }
    if editor_running:
        game_thread_ok = bridge_state.game_thread_responsive
        phase = _derive_launch_phase(plugin_ready, game_thread_ok, editor_running)

        if active_launch is not None:
            active_launch = launch_store.update_runtime(
                active_launch.launch_id,
                editor_pid=info.pid,
                launched_by=info.launched_by,
                plugin_ready=plugin_ready,
                phase=phase,
            )

        result.update({
            "pid": info.pid,
            "uptime_seconds": info.uptime_seconds,
            "memory_mb": info.memory_mb,
            "launched_by": info.launched_by,
            "plugin_ready": plugin_ready,
            "phase": phase,
            "bridge_state": bridge_state.to_dict(),
        })
        # Include monitor log tail when loading/blocked
        if monitor and phase != "ready" and log_lines > 0:
            result["recent_log"] = monitor.recent_log[-log_lines:]
    else:
        if active_launch is not None:
            active_launch = launch_store.mark_closed(active_launch.launch_id)
        result["phase"] = "not_running"
        result["bridge_state"] = bridge_state.to_dict()
    if active_launch is not None:
        result["launch_id"] = active_launch.launch_id
        result["launch_status"] = active_launch.status
        result["linked_build_id"] = active_launch.linked_build_id
        result["launch_started_at"] = active_launch.started_at
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
    project_path: str | None = None,
    extra_args: list[str] | None = None,
    linked_build_id: str | None = None,
) -> dict:
    """
    Launch the Unreal Editor. Returns IMMEDIATELY — does NOT block.

    Call ue_status to poll for readiness (check plugin_ready field).
    Call ue_compile BEFORE this if you changed C++ code.

    Safety: returns an error (does NOT launch) if the editor is already running,
    preventing duplicate instances.

    Args:
        project_path: Optional path to a .uproject file or project directory.
                      If omitted, launches the default configured project (OhMyUE).
        extra_args: Optional additional arguments passed to UnrealEditor.exe,
                    e.g. ["-log", "-game"]. Leave empty for normal editor launch.
    """
    if project_path:
        from pathlib import Path
        from .config import detect_config
        p = Path(project_path)
        if p.is_dir():
            matches = list(p.glob("*.uproject"))
            if not matches:
                return {"ok": False, "error": f"No .uproject found in {p}"}
            p = matches[0]
        if not p.exists():
            return {"ok": False, "error": f"Project file not found: {p}"}
        cfg = detect_config(p)
    else:
        cfg = _get_cfg()
    result = ue_process.launch(cfg, extra_args=extra_args)
    if not result.get("ok"):
        return result

    build_store = _get_build_store()
    if linked_build_id:
        build_session = build_store.get_session(linked_build_id)
    else:
        build_session = build_store.get_last_session()
        if build_session is not None and build_session.result != "succeeded":
            build_session = None
    resolved_build_id = build_session.build_id if build_session is not None else None

    log_path = str(cfg.project_path.parent / "Saved" / "Logs" / f"{cfg.project_name}.log")
    launch_session = _get_launch_store().create_session(
        editor_pid=result.get("pid"),
        project_path=str(cfg.project_path),
        command=result.get("command", ""),
        launched_by=result.get("launched_by", "ue-commander"),
        linked_build_id=resolved_build_id,
        log_path=log_path,
    )
    result["launch_id"] = launch_session.launch_id
    result["linked_build_id"] = launch_session.linked_build_id
    result["phase"] = launch_session.phase
    result["launch_status"] = launch_session.status
    result["log_path"] = launch_session.log_path
    return result


@mcp.tool()
def ue_close(
    force: bool = False,
    timeout: int = 30,
    user_override: bool = False,
    save_mode: Literal["auto_save", "prompt", "discard", "force"] = "auto_save",
) -> dict:
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
        save_mode: Shutdown save policy. `auto_save` saves without prompting,
                   `prompt` asks via UE save dialog, `discard` closes without saving,
                   and `force` immediately kills the process.
    """
    cfg = _get_cfg()
    proc_info = ue_process.get_status(cfg)
    active_launch = _get_launch_store().find_by_pid(proc_info.pid) if proc_info.running else _get_launch_store().get_active_session()
    result = ue_process.close(cfg, force=force, timeout=timeout, user_override=user_override, save_mode=save_mode)
    if result.get("ok") and active_launch is not None:
        _get_launch_store().mark_closed(active_launch.launch_id)
        result["launch_id"] = active_launch.launch_id
    return result


@mcp.tool()
def ue_close_all(
    force: bool = False,
    timeout: int = 30,
    save_mode: Literal["auto_save", "prompt", "discard", "force"] = "discard",
) -> dict:
    """
    Close ALL running Unreal Editor instances on this machine.
    Use this when multiple UE windows are open and need to be cleaned up.

    Args:
        force: If True, kill all instances immediately.
        timeout: Seconds to wait for graceful or terminate-based shutdown.
        save_mode: Bulk shutdown save policy. `discard` is the default because
                   multi-instance auto-save/prompt cannot be guaranteed.
    """
    return ue_process.close_all_ue(force=force, timeout=timeout, save_mode=save_mode)


@mcp.tool()
def ue_compile(
    config: BuildConfig | None = None,
    target: BuildTarget | None = None,
    platform: BuildPlatform | None = None,
    timeout: int = 600,
) -> dict:
    """
    Start a background C++ compilation and return IMMEDIATELY.

    Does NOT block — returns as soon as UBT is launched.
    Prefer ue_build_sessions to inspect compilation progress and results.
    ue_compile_status remains available as a compatibility shim.

    Valid values:
      config:   Debug | DebugGame | Development | Shipping | Test
      target:   Editor | Game | Client | Server
      platform: Win64 | Win32 | Mac | Linux
    """
    import os

    build_store = _get_build_store()
    if build_store.has_running():
        active = build_store.get_active_session()
        return {
            "ok": False,
            "error": "Compilation already running. Call ue_build_sessions to inspect the active session.",
            "build_id": active.build_id if active else None,
        }

    cfg = _get_cfg()
    ide = cfg.ide_build
    resolved_config: BuildConfig = config or (ide.config if ide else "Development")
    resolved_target: BuildTarget = target or (ide.target if ide else "Editor")
    resolved_platform: BuildPlatform = platform or (ide.platform if ide else "Win64")

    session = build_store.create_session(
        config=resolved_config,
        target=resolved_target,
        platform=resolved_platform,
        project_path=str(cfg.project_path),
    )
    log_path = session.log_path

    def _run():
        import asyncio as _asyncio
        loop = _asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(ue_build.compile(
                cfg,
                config=resolved_config,
                target=resolved_target,
                platform=resolved_platform,
                timeout=timeout,
            ))
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(result.output_tail)
            build_store.finalize(session.build_id, result)
        except Exception as exc:
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n[ue-commander] build session failed unexpectedly: {exc}\n")
            except Exception:
                pass
            build_store.mark_failed(session.build_id, f"Build session failed unexpectedly: {exc}")
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True)
    build_store.mark_running(session.build_id, t)
    t.start()

    return {
        "ok": True,
        "build_id": session.build_id,
        "status": "started",
        "message": "Compilation started in background. Call ue_build_sessions to inspect progress.",
        "log_file": log_path,
        "config": resolved_config,
        "target": resolved_target,
        "platform": resolved_platform,
    }


@mcp.tool()
def ue_compile_status(build_id: str | None = None) -> dict:
    """
    Compatibility shim for checking one build session.

    Prefer ue_build_sessions for the canonical build-session interface.
    If build_id is omitted, returns the current active build or the most recent build.
    """
    build_store = _get_build_store()
    session = build_store.get_session(build_id) if build_id else build_store.get_active_session()
    if session is None:
        session = build_store.get_last_session()
    if session is None:
        return {
            "running": False,
            "status": "not_started",
            "deprecated": True,
            "canonical_tool": "ue_build_sessions",
        }

    response = _build_session_response(session, include_full_log=session.status not in {"queued", "running"})
    response["deprecated"] = True
    response["canonical_tool"] = "ue_build_sessions"
    return response


@mcp.tool()
def ue_build_sessions(limit: int = 10, build_id: str | None = None) -> dict:
    """
    List recent build sessions for the current project.
    This is the canonical interface for build-session inspection.

    Args:
        limit: Max sessions to return when build_id is not provided.
        build_id: Optional specific build session to fetch.
    """
    build_store = _get_build_store()
    if build_id:
        session = build_store.get_session(build_id)
        sessions = [_build_session_response(session, include_full_log=session.status not in {"queued", "running"})] if session else []
    else:
        sessions = [
            _build_session_response(session, include_full_log=False)
            for session in build_store.list_sessions(limit=max(1, min(limit, 20)))
        ]
    return {
        "ok": True,
        "count": len(sessions),
        "canonical": True,
        "sessions": sessions,
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
    return _plugin_bridge.plugin_status()


@mcp.tool()
def ue_list_capabilities(include_core: bool = True, include_plugin: bool = True) -> dict:
    """
    Return normalized capability metadata for the current MCP surface.
    Core capabilities are always listed. Plugin capabilities appear when they
    have been discovered from a reachable UE editor session.

    Args:
        include_core: Include process/runtime capabilities owned by the Python server.
        include_plugin: Include editor/plugin capabilities discovered from UE.
    """
    if include_plugin:
        _plugin_bridge.refresh_plugin_tools()
    return _plugin_bridge.list_capabilities(include_core=include_core, include_plugin=include_plugin)


@mcp.tool()
def ue_blueprint_workflow(
    intent: str = "guided_graph_edit",
    blueprint_path: str = "",
    graph_name: str = "EventGraph",
) -> dict:
    """
    Return a recommended Blueprint read/edit/verify workflow for a common task.

    This planner turns plugin capability metadata into a stable step sequence so
    agents do not need to infer Blueprint editing order from raw tool lists.

    Args:
        intent: One of inspect_blueprint, guided_graph_edit, create_function_flow,
                event_entry_flow, default_value_flow, set_pin_value_flow,
                disconnect_pin_flow, replace_node_flow, or function_signature_flow.
                Common aliases such as inspect, edit_graph, add_function,
                event_entry, default_value, disconnect_pin, replace_node,
                and set_pin_value are accepted.
        blueprint_path: Optional target Blueprint asset path.
        graph_name: Target graph name for graph-scoped workflows.
    """
    normalized_intent = _normalize_blueprint_workflow_intent(intent)
    supported = [
        "inspect_blueprint",
        "guided_graph_edit",
        "create_function_flow",
        "event_entry_flow",
        "default_value_flow",
        "set_pin_value_flow",
        "disconnect_pin_flow",
        "replace_node_flow",
        "function_signature_flow",
    ]
    if normalized_intent not in supported:
        return {
            "ok": False,
            "error": f"Unsupported Blueprint workflow intent: {intent}",
            "supported_intents": supported,
        }

    _plugin_bridge.refresh_plugin_tools()
    steps = _blueprint_workflow_steps(normalized_intent, blueprint_path, graph_name)
    return {
        "ok": True,
        "intent": normalized_intent,
        "blueprint_path": blueprint_path,
        "graph_name": graph_name,
        "policy": _blueprint_mutation_policy(),
        "phases": {
            "read": [step for step in steps if ".read." in step.get("category", "")],
            "mutate": [step for step in steps if ".edit." in step.get("category", "") and "validate" not in step.get("category", "") and "compile" not in step.get("category", "")],
            "verify": [step for step in steps if step.get("tool") in {"CompileBlueprint", "ValidateBlueprintDeep"}],
        },
        "steps": steps,
    }


@mcp.tool()
def ue_widget_interaction_workflow(
    intent: str = "click_widget_flow",
    query: str = "",
    widget_path: str = "",
) -> dict:
    """
    Return a recommended widget discovery/interact/verify workflow for common UI tasks.

    Args:
        intent: One of inspect_widget, click_widget_flow, text_input_flow, or
                drag_widget_flow. Aliases such as inspect, click_widget, type,
                text_input, drag, and drag_widget are also accepted.
        query: Optional widget search query used for discovery-oriented steps.
        widget_path: Optional concrete widget path for direct interaction steps.
    """
    normalized_intent = _normalize_widget_workflow_intent(intent)
    supported = [
        "inspect_widget",
        "click_widget_flow",
        "text_input_flow",
        "drag_widget_flow",
    ]
    if normalized_intent not in supported:
        return {
            "ok": False,
            "error": f"Unsupported widget workflow intent: {intent}",
            "supported_intents": supported,
        }

    _plugin_bridge.refresh_plugin_tools()
    steps = _widget_workflow_steps(normalized_intent, query, widget_path)
    return {
        "ok": True,
        "intent": normalized_intent,
        "query": query,
        "widget_path": widget_path,
        "policy": _widget_interaction_policy(),
        "phases": {
            "discover": [step for step in steps if ".read." in step.get("category", "") or step.get("tool") == "SearchWidgets"],
            "interact": [step for step in steps if ".edit." in step.get("category", "") or step.get("tool") in {"FocusWidget", "ClickWidget", "TypeText", "PressKey", "DragWidget", "ScrollWidget"}],
            "verify": [step for step in steps if step.get("tool") == "TakeScreenshot"],
        },
        "steps": steps,
    }


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
def ue_auto_layout_blueprint_graph(
    blueprint_path: str = "",
    graph_name: str = "",
    col_spacing: int = 0,
    row_spacing: int = 0,
) -> dict:
    """Auto-layout Blueprint graph nodes using layered topological sort. Call after building a Blueprint to produce clean, readable node positions."""
    return ue_editor.call_plugin(
        "AutoLayoutBlueprintGraph",
        BlueprintPath=blueprint_path,
        GraphName=graph_name,
        ColSpacing=col_spacing,
        RowSpacing=row_spacing,
    )


# ---------------------------------------------------------------------------
# Scene SDF analysis tools
# ---------------------------------------------------------------------------

_cached_sdf = None  # type: ignore


@mcp.tool()
def sdf_snapshot(voxel_size: float = 100.0) -> str:
    """
    Capture scene and build SDF using per-mesh distance fields (fast, CPU-only).
    Call this first before any sdf_* queries.
    voxel_size: Resolution in cm (100=1m, 50=0.5m). Smaller = more detail but slower.
    Returns an overview of the scene after building the SDF.
    """
    from . import scene_sdf as _sdf_mod
    import json as _json

    response = ue_editor.call_plugin(
        "BuildSceneSDF",
        VoxelSize=voxel_size,
        bIncludeActorMeta=True,
        bIncludeLandscape=True,
        timeout=120,
    )

    if "error" in response:
        return _json.dumps(response)

    global _cached_sdf
    _cached_sdf = _sdf_mod.SceneSDF.from_probe_response(response)

    analyzer = _sdf_mod.SDFAnalyzer(_cached_sdf)
    return _json.dumps(analyzer.overview(), indent=2)


@mcp.tool()
def sdf_overview() -> str:
    """Get scene overview statistics from the cached SDF (call sdf_snapshot first)."""
    import json as _json
    from . import scene_sdf as _sdf_mod
    if _cached_sdf is None:
        return _json.dumps({"error": "No SDF cached. Call sdf_snapshot first."})
    return _json.dumps(_sdf_mod.SDFAnalyzer(_cached_sdf).overview(), indent=2)


@mcp.tool()
def sdf_find_spaces(min_volume_m3: float = 10.0) -> str:
    """Find open rooms, corridors, and chokepoints in the scene (requires scipy)."""
    import json as _json
    from . import scene_sdf as _sdf_mod
    if _cached_sdf is None:
        return _json.dumps({"error": "No SDF cached. Call sdf_snapshot first."})
    return _json.dumps(_sdf_mod.SDFAnalyzer(_cached_sdf).find_spaces(min_volume_m3), indent=2)


@mcp.tool()
def sdf_issues() -> str:
    """Detect layout problems: floating objects, unlit areas, isolated actors."""
    import json as _json
    from . import scene_sdf as _sdf_mod
    if _cached_sdf is None:
        return _json.dumps({"error": "No SDF cached. Call sdf_snapshot first."})
    return _json.dumps(_sdf_mod.SDFAnalyzer(_cached_sdf).detect_issues(), indent=2)


@mcp.tool()
def sdf_query_point(x: float = 0, y: float = 0, z: float = 0) -> str:
    """Sample the SDF at a specific world position (cm). Returns distance, occupied flag, nearest actor, lighting."""
    import json as _json
    import numpy as np
    from . import scene_sdf as _sdf_mod
    if _cached_sdf is None:
        return _json.dumps({"error": "No SDF cached. Call sdf_snapshot first."})
    sdf = _cached_sdf
    pos = np.array([x, y, z], dtype=np.float32)
    distance = sdf.sample(pos)
    nearest = min(sdf.actors, key=lambda a: float(np.linalg.norm(a.location - pos)),
                  default=None)
    lit_by = [a.name for a in sdf.actors
               if a.attenuation_radius > 0
               and float(np.linalg.norm(a.location - pos)) < a.attenuation_radius]
    return _json.dumps({
        "distance_cm": round(float(distance), 1),
        "occupied": bool(distance < 0),
        "nearest_actor": nearest.name if nearest else None,
        "nearest_actor_distance_cm": round(float(np.linalg.norm(nearest.location - pos)), 1)
            if nearest else None,
        "lit": len(lit_by) > 0,
        "lit_by": lit_by,
    }, indent=2)


@mcp.tool()
def sdf_trace_ray(
    origin_x: float = 0, origin_y: float = 0, origin_z: float = 100,
    dir_x: float = 1, dir_y: float = 0, dir_z: float = 0,
    max_distance: float = 10000,
) -> str:
    """Cast a sphere-trace ray through the SDF and find the first intersection."""
    import json as _json
    import numpy as np
    from . import scene_sdf as _sdf_mod
    if _cached_sdf is None:
        return _json.dumps({"error": "No SDF cached. Call sdf_snapshot first."})
    result = _sdf_mod.SDFAnalyzer(_cached_sdf).trace_ray(
        np.array([origin_x, origin_y, origin_z]),
        np.array([dir_x, dir_y, dir_z]),
        max_distance,
    )
    return _json.dumps(result, indent=2)


@mcp.tool()
def sdf_slice(
    axis: str = "z",
    position: float = 0,
    show_actors: bool = True,
    show_lights: bool = True,
) -> str:
    """
    Generate a 2D cross-section image of the scene SDF (requires matplotlib).
    axis: 'x', 'y', or 'z'. position: world coordinate in cm.
    Returns path to the saved PNG image.
    """
    import json as _json
    from . import scene_sdf as _sdf_mod
    if _cached_sdf is None:
        return _json.dumps({"error": "No SDF cached. Call sdf_snapshot first."})
    out = f"sdf_slice_{axis}_{position:.0f}.png"
    path = _sdf_mod.SDFRenderer(_cached_sdf).render_slice(
        axis, position, show_actors, show_lights, output_path=out)
    return _json.dumps({"image_path": path})


@mcp.tool()
def sdf_render_map(
    height_min: float | None = None,
    height_max: float | None = None,
) -> str:
    """
    Generate a top-down floor-plan from the SDF (requires matplotlib).
    height_min/max: optional Z range in cm to project. Returns path to PNG.
    """
    import json as _json
    from . import scene_sdf as _sdf_mod
    if _cached_sdf is None:
        return _json.dumps({"error": "No SDF cached. Call sdf_snapshot first."})
    height_range = (height_min, height_max) if height_min is not None else None
    path = _sdf_mod.SDFRenderer(_cached_sdf).render_top_down_map(
        output_path="sdf_topdown.png", height_range=height_range)
    return _json.dumps({"image_path": path})


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
# Debug tools (CDB)
# ---------------------------------------------------------------------------


@mcp.tool()
def ue_debug_attach() -> str:
    """
    Attach a lightweight debugger (CDB) to the running UE editor.
    The editor process is PAUSED on attach — call ue_debug_continue to resume.

    Auto-detects symbol paths for engine and project binaries.
    Requires 'Debugging Tools for Windows' (Windows SDK component).
    """
    cfg = _get_cfg()
    proc = ue_process.find_project_ue_process(cfg)
    if not proc:
        return "No UE process found. Launch the editor first."

    try:
        session = ue_debug.create_session(proc.pid)
    except RuntimeError as e:
        return str(e)

    symbol_paths = [
        str(cfg.engine_path / "Engine" / "Binaries" / "Win64"),
        str(cfg.project_path.parent / "Binaries" / "Win64"),
    ]

    try:
        output = session.attach(symbol_paths=symbol_paths)
    except RuntimeError as e:
        return str(e)

    return f"Attached to UE (PID {proc.pid}). Process is PAUSED.\n\n{output}"


@mcp.tool()
def ue_debug_stacks() -> str:
    """
    Get call stacks of ALL threads. The process must be paused.
    Use this to diagnose hangs, deadlocks, or inspect what UE is doing.
    """
    session = ue_debug.get_session()
    if not session:
        return "No debug session. Call ue_debug_attach first."
    return session.command("~*kb")


@mcp.tool()
def ue_debug_break() -> str:
    """
    Pause the running UE process. Use after ue_debug_continue to pause again.
    After pausing, use ue_debug_stacks or ue_debug_eval to inspect state.
    """
    session = ue_debug.get_session()
    if not session:
        return "No debug session. Call ue_debug_attach first."
    return session.send_break()


@mcp.tool()
def ue_debug_continue() -> str:
    """Resume UE execution after a pause or breakpoint hit."""
    session = ue_debug.get_session()
    if not session:
        return "No debug session. Call ue_debug_attach first."
    return session.command("g")


@mcp.tool()
def ue_debug_eval(expression: str) -> str:
    """
    Evaluate a C++ expression in the current debug context.
    Process must be paused. Uses CDB's ?? (typed expression evaluator).

    Examples:
      expression="this"           — current object
      expression="GEngine"        — global engine pointer
      expression="MyVar.Num()"    — TArray count
    """
    session = ue_debug.get_session()
    if not session:
        return "No debug session. Call ue_debug_attach first."
    return session.command(f"?? {expression}")


@mcp.tool()
def ue_debug_breakpoint(action: Literal["set", "remove", "list"] = "list", location: str = "") -> str:
    """
    Manage breakpoints.

    Args:
        action: "set", "remove", or "list"
        location: For set — symbol name or source:line (e.g. "AActor::BeginPlay", "MyFile.cpp:42").
                  For remove — breakpoint number from list output.

    Examples:
        action="set", location="AActor::BeginPlay"
        action="set", location="`MyActor.cpp:120`"
        action="list"
        action="remove", location="0"
    """
    session = ue_debug.get_session()
    if not session:
        return "No debug session. Call ue_debug_attach first."

    if action == "list":
        return session.command("bl")
    elif action == "set":
        if not location:
            return "Error: location required for 'set' action."
        return session.command(f"bp {location}")
    elif action == "remove":
        if not location:
            return "Error: breakpoint number required for 'remove' action."
        return session.command(f"bc {location}")
    else:
        return f"Unknown action '{action}'. Use: set, remove, list."


@mcp.tool()
def ue_debug_command(command: str) -> str:
    """
    Send a raw CDB command. Escape hatch for advanced debugging.

    Common commands:
      ~*kb          — all thread stacks
      !analyze -v   — automated crash analysis
      lm            — list loaded modules
      .sympath      — show symbol search path
      dt <type>     — display type layout
      dv            — display local variables
      !heap -s      — heap summary
    """
    session = ue_debug.get_session()
    if not session:
        return "No debug session. Call ue_debug_attach first."
    return session.command(command)


@mcp.tool()
def ue_debug_detach() -> str:
    """
    Detach the debugger from UE. The editor continues running normally.
    Always call this when done debugging.
    """
    return ue_debug.close_session()


def _register_manual_capabilities() -> None:
    manual_tools = [
        ("ue_status", "safe", "fast", False),
        ("ue_launch", "mutating", "long", False),
        ("ue_close", "mutating", "normal", False),
        ("ue_close_all", "destructive", "normal", False),
        ("ue_compile", "mutating", "long", False),
        ("ue_compile_status", "safe", "fast", False),
        ("ue_build_sessions", "safe", "fast", False),
        ("ue_get_log", "safe", "fast", False),
        ("ue_get_compile_errors", "safe", "fast", False),
        ("ue_project_info", "safe", "fast", False),
        ("ue_discover_all", "safe", "normal", False),
        ("ue_find_projects", "safe", "normal", False),
        ("ue_plugin_status", "safe", "fast", True),
        ("ue_list_capabilities", "safe", "fast", False),
        ("ue_blueprint_workflow", "safe", "fast", True),
        ("ue_widget_interaction_workflow", "safe", "fast", True),
        ("ue_clear_crash", "mutating", "fast", False),
        ("ue_auto_layout_blueprint_graph", "mutating", "normal", True),
        ("sdf_snapshot", "mutating", "long", True),
        ("sdf_overview", "safe", "fast", False),
        ("sdf_find_spaces", "safe", "normal", False),
        ("sdf_issues", "safe", "normal", False),
        ("sdf_query_point", "safe", "fast", False),
        ("sdf_trace_ray", "safe", "normal", False),
        ("sdf_slice", "safe", "long", False),
        ("sdf_render_map", "safe", "long", False),
        ("ue_batch", "mutating", "long", True),
        ("ue_debug_attach", "mutating", "normal", False),
        ("ue_debug_stacks", "safe", "normal", False),
        ("ue_debug_break", "mutating", "fast", False),
        ("ue_debug_continue", "mutating", "fast", False),
        ("ue_debug_eval", "safe", "fast", False),
        ("ue_debug_breakpoint", "mutating", "fast", False),
        ("ue_debug_command", "mutating", "normal", False),
        ("ue_debug_detach", "mutating", "fast", False),
    ]

    for tool_name, safety, timeout_class, requires_editor in manual_tools:
        tool = globals()[tool_name]
        description = (tool.__doc__ or "").strip().splitlines()[0] if tool.__doc__ else tool_name
        _capability_registry.register_manual_tool(
            tool_name,
            description,
            source="bridge" if requires_editor else "core",
            availability="online" if requires_editor else "offline",
            safety=safety,
            requires_editor=requires_editor,
            timeout_class=timeout_class,
        )


_register_manual_capabilities()

# Best-effort at import time (UE may not be running yet)
try:
    _plugin_bridge.refresh_plugin_tools()
except Exception as _e:
    import sys
    print(f"[ue-commander] Auto-registration deferred: {_e}", file=sys.stderr)
