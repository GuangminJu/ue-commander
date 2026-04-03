"""
Blueprint protocol regression tests.

Validates the Blueprint data-layer contract introduced by the UE data
simplification work:
- summary/detail/raw read layering
- compatibility shim behavior
- workflow metadata on plugin tool schemas

Requires:
- UE editor running
- OhMyUnrealEngine plugin loaded and reachable
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ue_commander import server, ue_editor  # noqa: E402


def _resolve_bp_path() -> str:
    candidates = [
        "/Game/SurvivorsTemplate/Blueprints/System/BP_Game_Manager",
        "/Game/SurvivorsTemplate/Blueprints/System/BP_Gameplay_GameMode",
        "/Game/SurvivorsRoguelike/System/BP_Base_GameMode",
    ]
    for candidate in candidates:
        result = ue_editor.call_plugin("GetBlueprintInfo", BlueprintPath=candidate)
        if result.get("ok") is True:
            return candidate
    raise RuntimeError(f"Failed to resolve a Blueprint test target from candidates: {candidates}")


def _resolve_main_menu_widget_path() -> str:
    candidates = [
        "/Game/SurvivorsTemplate/Widgets/WB_MainMenu",
        "/Game/SurvivorsRoguelike/Widgets/WB_MainMenu",
    ]
    for candidate in candidates:
        result = ue_editor.call_plugin("GetBlueprintInfo", BlueprintPath=candidate)
        if result.get("ok") is True:
            return candidate
    raise RuntimeError(f"Failed to resolve a main menu widget test target from candidates: {candidates}")


BP_PATH = _resolve_bp_path()
GRAPH_NAME = "EventGraph"
MAIN_MENU_WIDGET_PATH = _resolve_main_menu_widget_path()


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _call(function: str, **params) -> dict:
    result = ue_editor.call_plugin(function, **params)
    _assert("error" not in result, f"{function} returned error: {result}")
    return result


def _schema(name: str) -> dict:
    result = ue_editor.list_plugin_tool_schemas(name=name)
    _assert(result.get("ok") is True, f"schema for {name} failed: {result}")
    item = result.get("item")
    _assert(isinstance(item, dict), f"schema for {name} missing item: {result}")
    return item


def test_blueprint_info_summary() -> None:
    result = _call("GetBlueprintInfo", BlueprintPath=BP_PATH)
    _assert(result.get("detail_level") == "summary", f"unexpected detail level: {result}")
    _assert(result.get("type") == "blueprint", f"unexpected type: {result}")
    summary = result.get("summary")
    _assert(isinstance(summary, dict), f"missing summary: {result}")
    for field in ["name", "path", "parent_class", "status", "graph_count", "variable_count"]:
        _assert(field in summary, f"summary missing {field}: {summary}")
    _assert("detail" not in result, f"summary response should not include detail: {result}")


def test_blueprint_info_detail() -> None:
    result = _call("GetBlueprintInfo", BlueprintPath=BP_PATH, DetailLevel="detail")
    _assert(result.get("detail_level") == "detail", f"unexpected detail level: {result}")
    detail = result.get("detail")
    _assert(isinstance(detail, dict), f"missing detail: {result}")
    for field in ["parent_chain", "variables", "graphs", "interfaces", "components"]:
        _assert(field in detail, f"detail missing {field}: {detail}")


def test_blueprint_graphs_summary_vs_detail() -> None:
    summary = _call("ListBlueprintGraphs", BlueprintPath=BP_PATH)
    detail = _call("ListBlueprintGraphs", BlueprintPath=BP_PATH, DetailLevel="detail")
    _assert(summary.get("detail_level") == "summary", f"unexpected summary level: {summary}")
    _assert(detail.get("detail_level") == "detail", f"unexpected detail level: {detail}")

    summary_function = next((g for g in summary.get("graphs", []) if g.get("type") == "function"), None)
    detail_function = next((g for g in detail.get("graphs", []) if g.get("type") == "function"), None)
    _assert(summary_function is not None, f"expected function graph in summary: {summary}")
    _assert(detail_function is not None, f"expected function graph in detail: {detail}")
    _assert("inputs" not in summary_function and "outputs" not in summary_function,
            f"summary function graph should not include signature arrays: {summary_function}")
    _assert("inputs" in detail_function and "outputs" in detail_function,
            f"detail function graph should include signature arrays: {detail_function}")


def test_blueprint_nodes_summary_vs_detail() -> None:
    summary = _call("ListBlueprintNodes", BlueprintPath=BP_PATH, GraphName=GRAPH_NAME, Limit=3)
    detail = _call("ListBlueprintNodes", BlueprintPath=BP_PATH, GraphName=GRAPH_NAME, Limit=3, DetailLevel="detail")
    _assert(summary.get("detail_level") == "summary", f"unexpected summary level: {summary}")
    _assert(detail.get("detail_level") == "detail", f"unexpected detail level: {detail}")

    summary_nodes = summary.get("nodes", [])
    detail_nodes = detail.get("nodes", [])
    _assert(summary_nodes and detail_nodes, "expected nodes in both summary and detail responses")
    _assert("pin_count" in summary_nodes[0], f"summary node should include pin_count: {summary_nodes[0]}")
    _assert("pins" not in summary_nodes[0], f"summary node should not include pins: {summary_nodes[0]}")
    _assert("pins" in detail_nodes[0], f"detail node should include pins: {detail_nodes[0]}")


def test_blueprint_graph_raw() -> None:
    result = _call("GetBlueprintGraph", BlueprintPath=BP_PATH, GraphName=GRAPH_NAME)
    _assert(result.get("detail_level") == "raw", f"graph read should default to raw: {result}")
    _assert(result.get("raw") is True, f"graph read should mark raw=true: {result}")
    nodes = result.get("nodes", [])
    _assert(nodes, f"raw graph should include nodes: {result}")
    _assert("pins" in nodes[0], f"raw graph node should include pins: {nodes[0]}")


def test_blueprint_context_compatibility_shim() -> None:
    result = _call("GetBlueprintContext", BlueprintPath=BP_PATH)
    _assert(result.get("deprecated") is True, f"context shim should be deprecated: {result}")
    _assert(result.get("canonical_tool") == "GetBlueprintInfo", f"unexpected canonical tool: {result}")
    _assert(result.get("detail_level") == "detail", f"context shim should return detail payload: {result}")


def test_blueprint_workflow_metadata() -> None:
    info = _schema("GetBlueprintInfo")
    graphs = _schema("ListBlueprintGraphs")
    nodes = _schema("ListBlueprintNodes")
    raw = _schema("GetBlueprintGraph")
    add = _schema("AddBlueprintNodeByType")
    add_compat = _schema("AddBlueprintNode")
    add_escape = _schema("AddBlueprintGenericNode")
    connect = _schema("ConnectBlueprintPinsByGuid")
    connect_name = _schema("ConnectBlueprintPins")
    disconnect = _schema("DisconnectBlueprintPins")
    chain = _schema("ConnectPinChain")
    create_function = _schema("CreateBlueprintFunction")
    custom_event = _schema("CreateBlueprintCustomEvent")
    set_pin_name = _schema("SetBlueprintPinValue")
    set_pin_guid = _schema("SetBlueprintPinValueByGuid")
    compat = _schema("GetBlueprintContext")

    _assert(info.get("category") == "blueprint.read.resource", f"bad info category: {info}")
    _assert(graphs.get("category") == "blueprint.read.graph_index", f"bad graphs category: {graphs}")
    _assert(nodes.get("category") == "blueprint.read.node_index", f"bad nodes category: {nodes}")
    _assert(raw.get("category") == "blueprint.read.graph_raw", f"bad raw category: {raw}")
    _assert(add.get("category") == "blueprint.edit.guid_based", f"bad add category: {add}")
    _assert(add_compat.get("category") == "blueprint.edit.compat", f"bad compat add category: {add_compat}")
    _assert(add_escape.get("category") == "blueprint.edit.escape_hatch", f"bad escape hatch category: {add_escape}")
    _assert(connect.get("category") == "blueprint.edit.guid_based", f"bad connect category: {connect}")
    _assert(connect_name.get("category") == "blueprint.edit.name_based", f"bad name-based connect category: {connect_name}")
    _assert(disconnect.get("category") == "blueprint.edit.rewire", f"bad disconnect category: {disconnect}")
    _assert(chain.get("category") == "blueprint.edit.rewire", f"bad chain category: {chain}")
    _assert(create_function.get("category") == "blueprint.edit.structure", f"bad create function category: {create_function}")
    _assert(custom_event.get("category") == "blueprint.edit.graph", f"bad custom event category: {custom_event}")
    _assert(set_pin_name.get("category") == "blueprint.edit.name_based", f"bad name-based set pin category: {set_pin_name}")
    _assert(set_pin_guid.get("category") == "blueprint.edit.guid_based", f"bad guid set pin category: {set_pin_guid}")

    _assert(graphs.get("recommended_reads") == "GetBlueprintInfo(summary)", f"bad graph recommended_reads: {graphs}")
    _assert(nodes.get("recommended_reads") == "GetBlueprintInfo(summary),ListBlueprintGraphs(summary)",
            f"bad node recommended_reads: {nodes}")
    _assert(raw.get("recommended_reads") == "GetBlueprintInfo(summary),ListBlueprintGraphs(summary),ListBlueprintNodes(detail)",
            f"bad raw recommended_reads: {raw}")
    _assert(disconnect.get("recommended_reads") == "GetBlueprintInfo(summary),ListBlueprintNodes(detail),GetBlueprintGraph(raw)",
            f"bad disconnect recommended_reads: {disconnect}")
    _assert(chain.get("recommended_reads") == "GetBlueprintInfo(summary),ListBlueprintNodes(detail)",
            f"bad chain recommended_reads: {chain}")
    _assert(set_pin_name.get("recommended_reads") == "GetBlueprintInfo(summary),ListBlueprintNodes(detail)",
            f"bad name-based set pin recommended_reads: {set_pin_name}")
    _assert("workflow_hint" in add and add["workflow_hint"], f"add tool missing workflow_hint: {add}")
    _assert("escape hatch" in add_escape.get("workflow_hint", "").lower(), f"escape hatch hint missing: {add_escape}")
    _assert("workflow_hint" in create_function and create_function["workflow_hint"], f"create function missing workflow_hint: {create_function}")
    _assert("workflow_hint" in custom_event and custom_event["workflow_hint"], f"custom event missing workflow_hint: {custom_event}")
    _assert(add_compat.get("deprecated") is True, f"compat add should be deprecated: {add_compat}")
    _assert(add_compat.get("canonical_tool") == "AddBlueprintNodeByType", f"compat add canonical mismatch: {add_compat}")
    _assert(connect_name.get("deprecated") is True, f"name-based connect should be deprecated: {connect_name}")
    _assert(connect_name.get("canonical_tool") == "ConnectBlueprintPinsByGuid", f"name-based connect canonical mismatch: {connect_name}")
    _assert(set_pin_name.get("deprecated") is True, f"name-based set pin should be deprecated: {set_pin_name}")
    _assert(set_pin_name.get("canonical_tool") == "SetBlueprintPinValueByGuid", f"name-based set pin canonical mismatch: {set_pin_name}")
    _assert(compat.get("deprecated") is True, f"compat schema should be deprecated: {compat}")
    _assert(compat.get("canonical_tool") == "GetBlueprintInfo", f"compat canonical mismatch: {compat}")


def test_recent_metadata_cleanup_targets() -> None:
    spawn = _schema("SpawnActors")
    camera = _schema("SetViewportCamera")
    preview = _schema("SetPreviewLanguage")
    pie = _schema("SetPIEPropertyValue")
    phys = _schema("SetPhysicsMaterial")
    nanite = _schema("SetNaniteSettings")
    bt = _schema("AddBTTask")
    blackboard = _schema("AddBlackboardKey")

    _assert(spawn.get("safety") == "mutating", f"SpawnActors safety still wrong: {spawn}")
    _assert(spawn.get("category") == "actor.edit.batch_spawn", f"SpawnActors category wrong: {spawn}")
    _assert(camera.get("category") == "viewport.edit.camera", f"SetViewportCamera category wrong: {camera}")
    _assert(preview.get("category") == "editor.localization.preview", f"SetPreviewLanguage category wrong: {preview}")
    _assert(pie.get("category") == "pie.runtime.edit", f"SetPIEPropertyValue category wrong: {pie}")
    _assert(phys.get("category") == "physics.edit.material", f"SetPhysicsMaterial category wrong: {phys}")
    _assert(nanite.get("category") == "mesh.nanite.edit", f"SetNaniteSettings category wrong: {nanite}")
    _assert(bt.get("category") == "ai.behavior_tree.edit", f"AddBTTask category wrong: {bt}")
    _assert(blackboard.get("category") == "ai.blackboard.edit", f"AddBlackboardKey category wrong: {blackboard}")

    for item in [spawn, camera, preview, pie, phys, nanite, bt, blackboard]:
        _assert(item.get("timeout_class") == "normal", f"timeout missing on {item.get('name')}: {item}")
        _assert(item.get("workflow_hint"), f"workflow_hint missing on {item.get('name')}: {item}")


def test_actor_pie_widget_metadata_cleanup_targets() -> None:
    widget_search = _schema("SearchWidgets")
    actor_transform = _schema("SetActorTransform")
    actor_location = _schema("SetActorLocation")
    actor_rotation = _schema("SetActorRotation")
    actor_scale = _schema("SetActorScale")
    actor_visibility = _schema("SetActorVisibility")
    actor_select = _schema("SelectActors")
    physics_break = _schema("SimulatePhysicsBreak")
    pie_input = _schema("SendPIEInput")

    _assert(widget_search.get("safety") == "safe", f"SearchWidgets safety wrong: {widget_search}")
    _assert(widget_search.get("category") == "widget.read.search", f"SearchWidgets category wrong: {widget_search}")
    _assert(actor_transform.get("category") == "actor.edit.transform", f"SetActorTransform category wrong: {actor_transform}")
    _assert(actor_location.get("category") == "actor.edit.transform", f"SetActorLocation category wrong: {actor_location}")
    _assert(actor_rotation.get("category") == "actor.edit.transform", f"SetActorRotation category wrong: {actor_rotation}")
    _assert(actor_scale.get("category") == "actor.edit.transform", f"SetActorScale category wrong: {actor_scale}")
    _assert(actor_visibility.get("category") == "actor.edit.visibility", f"SetActorVisibility category wrong: {actor_visibility}")
    _assert(actor_select.get("category") == "editor.selection.actor", f"SelectActors category wrong: {actor_select}")
    _assert(physics_break.get("category") == "physics.edit.impulse", f"SimulatePhysicsBreak category wrong: {physics_break}")
    _assert(pie_input.get("category") == "pie.runtime.input", f"SendPIEInput category wrong: {pie_input}")

    for item in [widget_search, actor_transform, actor_location, actor_rotation, actor_scale, actor_visibility, actor_select, physics_break, pie_input]:
        _assert(item.get("timeout_class") == "normal", f"timeout missing on {item.get('name')}: {item}")
        _assert(item.get("workflow_hint"), f"workflow_hint missing on {item.get('name')}: {item}")


def test_asset_widget_audio_metadata_cleanup_targets() -> None:
    spawn_actor = _schema("SpawnActor")
    open_editor = _schema("OpenAssetEditor")
    run_tests = _schema("RunAutomationTests")
    play_sound = _schema("PlaySoundInEditor")
    click = _schema("ClickWidget")
    double_click = _schema("DoubleClickWidget")
    drag = _schema("DragWidget")
    type_text = _schema("TypeText")
    press_key = _schema("PressKey")
    scroll = _schema("ScrollWidget")

    _assert(spawn_actor.get("category") == "actor.edit.spawn", f"SpawnActor category wrong: {spawn_actor}")
    _assert(open_editor.get("category") == "asset.editor.open", f"OpenAssetEditor category wrong: {open_editor}")
    _assert(run_tests.get("category") == "automation.test.run", f"RunAutomationTests category wrong: {run_tests}")
    _assert(play_sound.get("category") == "audio.preview.play", f"PlaySoundInEditor category wrong: {play_sound}")
    _assert(click.get("category") == "widget.edit.interact", f"ClickWidget category wrong: {click}")
    _assert(double_click.get("category") == "widget.edit.interact", f"DoubleClickWidget category wrong: {double_click}")
    _assert(drag.get("category") == "widget.edit.interact", f"DragWidget category wrong: {drag}")
    _assert(type_text.get("category") == "widget.edit.text", f"TypeText category wrong: {type_text}")
    _assert(press_key.get("category") == "widget.edit.interact", f"PressKey category wrong: {press_key}")
    _assert(scroll.get("category") == "widget.edit.interact", f"ScrollWidget category wrong: {scroll}")

    for item in [spawn_actor, open_editor, run_tests, play_sound, click, double_click, drag, type_text, press_key, scroll]:
        _assert(item.get("workflow_hint"), f"workflow_hint missing on {item.get('name')}: {item}")


def test_modal_widget_metadata_targets() -> None:
    modal_state = _schema("GetModalState")
    modal_tree = _schema("GetModalWidgetTree")
    modal_search = _schema("SearchModalWidgets")
    modal_action = _schema("ActOnModalDialog")

    _assert(modal_state.get("category") == "widget.read.modal", f"GetModalState category wrong: {modal_state}")
    _assert(modal_tree.get("category") == "widget.read.modal", f"GetModalWidgetTree category wrong: {modal_tree}")
    _assert(modal_search.get("category") == "widget.read.modal", f"SearchModalWidgets category wrong: {modal_search}")
    _assert(modal_action.get("category") == "widget.edit.modal", f"ActOnModalDialog category wrong: {modal_action}")

    _assert(modal_state.get("safety") == "safe", f"GetModalState safety wrong: {modal_state}")
    _assert(modal_tree.get("safety") == "safe", f"GetModalWidgetTree safety wrong: {modal_tree}")
    _assert(modal_search.get("safety") == "safe", f"SearchModalWidgets safety wrong: {modal_search}")
    _assert(modal_action.get("safety") == "mutating", f"ActOnModalDialog safety wrong: {modal_action}")

    for item in [modal_state, modal_tree, modal_search, modal_action]:
        _assert(item.get("workflow_hint"), f"workflow_hint missing on {item.get('name')}: {item}")


def test_list_assets_offset_and_limit_contract() -> None:
    result = _call("ListAssets", Path="/Game/SurvivorsTemplate/Blueprints", Recursive=True, Limit=5, Offset=0)
    _assert(result.get("ok") is True, f"ListAssets failed: {result}")
    _assert(result.get("offset") == 0, f"ListAssets should preserve offset=0: {result}")
    _assert(result.get("returned") > 0, f"ListAssets should return first page assets: {result}")
    _assert(result.get("returned") <= 5, f"ListAssets should respect limit=5: {result}")
    assets = result.get("assets", [])
    _assert(isinstance(assets, list) and assets, f"ListAssets should return asset entries: {result}")


def test_bridge_capability_output() -> None:
    result = server.ue_list_capabilities(include_core=False, include_plugin=True)
    _assert(result.get("ok") is True, f"capability listing failed: {result}")
    capabilities = result.get("capabilities", [])
    _assert(capabilities, f"expected plugin capabilities: {result}")

    graph = next((cap for cap in capabilities if cap.get("mcp_name") == "ue_get_blueprint_graph"), None)
    compat = next((cap for cap in capabilities if cap.get("mcp_name") == "ue_get_blueprint_context"), None)
    add_compat = next((cap for cap in capabilities if cap.get("mcp_name") == "ue_add_blueprint_node"), None)
    connect_name = next((cap for cap in capabilities if cap.get("mcp_name") == "ue_connect_blueprint_pins"), None)
    set_pin_name = next((cap for cap in capabilities if cap.get("mcp_name") == "ue_set_blueprint_pin_value"), None)

    _assert(graph is not None, f"missing ue_get_blueprint_graph capability: {result}")
    _assert(graph.get("category") == "blueprint.read.graph_raw", f"bad graph capability category: {graph}")
    _assert(graph.get("workflow_hint"), f"graph capability missing workflow_hint: {graph}")
    _assert(graph.get("recommended_reads") == "GetBlueprintInfo(summary),ListBlueprintGraphs(summary),ListBlueprintNodes(detail)",
            f"bad graph capability recommended_reads: {graph}")

    _assert(compat is not None, f"missing ue_get_blueprint_context capability: {result}")
    _assert(compat.get("deprecated") is True, f"compat capability should be deprecated: {compat}")
    _assert(compat.get("canonical_tool") == "GetBlueprintInfo", f"bad compat canonical tool: {compat}")
    _assert(add_compat is not None and add_compat.get("deprecated") is True, f"compat add should be deprecated in capability output: {add_compat}")
    _assert(add_compat.get("canonical_tool") == "AddBlueprintNodeByType", f"bad compat add canonical tool: {add_compat}")
    _assert(connect_name is not None and connect_name.get("deprecated") is True, f"name-based connect should be deprecated in capability output: {connect_name}")
    _assert(connect_name.get("canonical_tool") == "ConnectBlueprintPinsByGuid", f"bad name-based connect canonical tool: {connect_name}")
    _assert(set_pin_name is not None and set_pin_name.get("deprecated") is True, f"name-based set pin should be deprecated in capability output: {set_pin_name}")
    _assert(set_pin_name.get("canonical_tool") == "SetBlueprintPinValueByGuid", f"bad name-based set pin canonical tool: {set_pin_name}")


def test_blueprint_workflow_planner() -> None:
    result = server.ue_blueprint_workflow(
        intent="edit_graph",
        blueprint_path=BP_PATH,
        graph_name=GRAPH_NAME,
    )
    _assert(result.get("ok") is True, f"workflow planner failed: {result}")
    _assert(result.get("intent") == "guided_graph_edit", f"unexpected normalized intent: {result}")

    steps = result.get("steps", [])
    _assert(steps, f"workflow should include steps: {result}")
    expected_tools = [
        "GetBlueprintInfo",
        "ListBlueprintGraphs",
        "ListBlueprintNodes",
        "AddBlueprintNodeByType",
        "GetBlueprintGraph",
        "ConnectBlueprintPinsByGuid",
        "AutoLayoutBlueprintGraph",
        "CompileBlueprint",
        "ValidateBlueprintDeep",
    ]
    _assert([step.get("tool") for step in steps] == expected_tools, f"unexpected workflow steps: {steps}")

    phases = result.get("phases", {})
    policy = result.get("policy", {})
    _assert(len(phases.get("read", [])) >= 3, f"workflow read phase too small: {phases}")
    _assert(any(step.get("tool") == "CompileBlueprint" for step in phases.get("verify", [])),
            f"verify phase missing compile: {phases}")
    _assert(any(step.get("tool") == "ValidateBlueprintDeep" for step in phases.get("verify", [])),
            f"verify phase missing deep validation: {phases}")
    _assert("ConnectBlueprintPinsByGuid" in policy.get("preferred_path", []), f"policy missing preferred guid connect path: {policy}")
    _assert("ConnectBlueprintPins" in policy.get("compatibility_tools", []), f"policy missing compatibility connect tool: {policy}")
    _assert("AddBlueprintGenericNode" in policy.get("conditional_tools", []), f"policy missing escape hatch tool: {policy}")


def test_blueprint_extended_workflow_planner() -> None:
    replace_result = server.ue_blueprint_workflow(
        intent="replace_node",
        blueprint_path=BP_PATH,
        graph_name=GRAPH_NAME,
    )
    _assert(replace_result.get("ok") is True, f"replace workflow failed: {replace_result}")
    _assert(replace_result.get("intent") == "replace_node_flow", f"unexpected replace intent: {replace_result}")
    replace_tools = [step.get("tool") for step in replace_result.get("steps", [])]
    _assert("ConnectPinChain" in replace_tools, f"replace workflow missing ConnectPinChain: {replace_result}")
    _assert("RemoveBlueprintNodeByGuid" in replace_tools, f"replace workflow missing RemoveBlueprintNodeByGuid: {replace_result}")

    disconnect_result = server.ue_blueprint_workflow(
        intent="disconnect_pin",
        blueprint_path=BP_PATH,
        graph_name=GRAPH_NAME,
    )
    _assert(disconnect_result.get("ok") is True, f"disconnect workflow failed: {disconnect_result}")
    _assert(disconnect_result.get("intent") == "disconnect_pin_flow", f"unexpected disconnect intent: {disconnect_result}")
    disconnect_tools = [step.get("tool") for step in disconnect_result.get("steps", [])]
    _assert("DisconnectBlueprintPins" in disconnect_tools, f"disconnect workflow missing disconnect step: {disconnect_result}")

    signature_result = server.ue_blueprint_workflow(
        intent="function_signature",
        blueprint_path=BP_PATH,
        graph_name=GRAPH_NAME,
    )
    _assert(signature_result.get("ok") is True, f"signature workflow failed: {signature_result}")
    _assert(signature_result.get("intent") == "function_signature_flow", f"unexpected signature intent: {signature_result}")
    signature_tools = [step.get("tool") for step in signature_result.get("steps", [])]
    _assert(signature_tools.count("ListBlueprintGraphs") == 2, f"signature workflow should re-read graph summaries: {signature_result}")
    _assert("ModifyBlueprintFunctionParams" in signature_tools, f"signature workflow missing modify step: {signature_result}")

    event_result = server.ue_blueprint_workflow(
        intent="event_entry",
        blueprint_path=BP_PATH,
        graph_name=GRAPH_NAME,
    )
    _assert(event_result.get("ok") is True, f"event entry workflow failed: {event_result}")
    _assert(event_result.get("intent") == "event_entry_flow", f"unexpected event intent: {event_result}")
    event_tools = [step.get("tool") for step in event_result.get("steps", [])]
    _assert("CreateBlueprintCustomEvent" in event_tools, f"event workflow missing custom event step: {event_result}")
    _assert("ConnectBlueprintPinsByGuid" in event_tools, f"event workflow missing guid connect step: {event_result}")

    default_result = server.ue_blueprint_workflow(
        intent="default_value",
        blueprint_path=BP_PATH,
        graph_name=GRAPH_NAME,
    )
    _assert(default_result.get("ok") is True, f"default value workflow failed: {default_result}")
    _assert(default_result.get("intent") == "default_value_flow", f"unexpected default value intent: {default_result}")
    default_tools = [step.get("tool") for step in default_result.get("steps", [])]
    _assert("SetBlueprintPinValue" in default_tools, f"default value workflow missing compatibility path: {default_result}")
    _assert("SetBlueprintPinValueByGuid" in default_tools, f"default value workflow missing preferred guid path: {default_result}")


def test_widget_workflow_planner() -> None:
    click_result = server.ue_widget_interaction_workflow(
        intent="click_widget",
        query="Play",
        widget_path="/Window/Main/Button_Play",
    )
    _assert(click_result.get("ok") is True, f"widget click workflow failed: {click_result}")
    _assert(click_result.get("intent") == "click_widget_flow", f"unexpected widget click intent: {click_result}")
    click_tools = [step.get("tool") for step in click_result.get("steps", [])]
    _assert(click_tools == ["SearchWidgets", "FocusWidget", "ClickWidget", "TakeScreenshot"], f"unexpected widget click steps: {click_result}")

    text_result = server.ue_widget_interaction_workflow(
        intent="text_input",
        query="Name",
        widget_path="/Window/Main/TextBox_Name",
    )
    _assert(text_result.get("ok") is True, f"widget text workflow failed: {text_result}")
    _assert(text_result.get("intent") == "text_input_flow", f"unexpected widget text intent: {text_result}")
    policy = text_result.get("policy", {})
    _assert("SearchWidgets" in policy.get("preferred_path", []), f"widget policy missing SearchWidgets: {policy}")
    _assert("DragWidget" in policy.get("conditional_tools", []), f"widget policy missing DragWidget: {policy}")
    text_tools = [step.get("tool") for step in text_result.get("steps", [])]
    _assert("TypeText" in text_tools and "PressKey" in text_tools, f"text input workflow missing expected steps: {text_result}")


def test_ue_close_save_mode_forwarding() -> None:
    original_get_cfg = server._get_cfg
    original_get_launch_store = server._get_launch_store
    original_get_status = server.ue_process.get_status
    original_close = server.ue_process.close

    class _DummyProcInfo:
        running = False
        pid = None

    class _DummyLaunchStore:
        def find_by_pid(self, pid):
            return None

        def get_active_session(self):
            return None

        def mark_closed(self, launch_id):
            raise AssertionError(f"mark_closed should not be called: {launch_id}")

    recorded: dict = {}

    def _fake_close(cfg, force=False, timeout=30, user_override=False, save_mode="auto_save"):
        recorded["force"] = force
        recorded["timeout"] = timeout
        recorded["user_override"] = user_override
        recorded["save_mode"] = save_mode
        return {"ok": True, "save_mode": save_mode}

    try:
        server._get_cfg = lambda: object()
        server._get_launch_store = lambda: _DummyLaunchStore()
        server.ue_process.get_status = lambda cfg: _DummyProcInfo()
        server.ue_process.close = _fake_close

        result = server.ue_close(timeout=42, user_override=True, save_mode="prompt")
        _assert(result.get("ok") is True, f"ue_close failed: {result}")
        _assert(recorded == {
            "force": False,
            "timeout": 42,
            "user_override": True,
            "save_mode": "prompt",
        }, f"ue_close did not forward save_mode correctly: {recorded}")
    finally:
        server._get_cfg = original_get_cfg
        server._get_launch_store = original_get_launch_store
        server.ue_process.get_status = original_get_status
        server.ue_process.close = original_close


def test_ue_process_close_rejects_unsupported_save_mode() -> None:
    result = server.ue_process.close(object(), save_mode="weird_mode")  # type: ignore[arg-type]
    _assert(result.get("ok") is False, f"unsupported save_mode should fail: {result}")
    _assert("Unsupported save_mode" in result.get("error", ""), f"unexpected error for unsupported save_mode: {result}")


def test_ue_close_all_save_mode_forwarding() -> None:
    original_close_all = server.ue_process.close_all_ue
    recorded: dict = {}

    def _fake_close_all(force=False, timeout=30, save_mode="discard"):
        recorded["force"] = force
        recorded["timeout"] = timeout
        recorded["save_mode"] = save_mode
        return {"ok": True, "save_mode": save_mode}

    try:
        server.ue_process.close_all_ue = _fake_close_all
        result = server.ue_close_all(timeout=18, save_mode="discard")
        _assert(result.get("ok") is True, f"ue_close_all failed: {result}")
        _assert(recorded == {
            "force": False,
            "timeout": 18,
            "save_mode": "discard",
        }, f"ue_close_all did not forward save_mode correctly: {recorded}")
    finally:
        server.ue_process.close_all_ue = original_close_all


def test_ue_process_close_all_rejects_unsupported_save_mode() -> None:
    result = server.ue_process.close_all_ue(save_mode="unknown_mode")
    _assert(result.get("ok") is False, f"unsupported bulk save_mode should fail: {result}")
    _assert("Unsupported save_mode" in result.get("error", ""), f"unexpected bulk save_mode error: {result}")


def test_plugin_bridge_accepts_original_and_snake_case_params() -> None:
    capability = server._capability_registry.get("ue_get_blueprint_info")
    _assert(capability is not None, "missing ue_get_blueprint_info capability")

    original_call_plugin = server.ue_editor.call_plugin
    calls: list[tuple[str, dict]] = []

    def _fake_call_plugin(function_name: str, **params):
        calls.append((function_name, params))
        return {"ok": True, "detail_level": params.get("DetailLevel", "summary")}

    try:
        server.ue_editor.call_plugin = _fake_call_plugin
        tool_fn = server._plugin_bridge._make_plugin_tool(capability)

        result_snake = tool_fn(blueprint_path=BP_PATH, detail_level="detail")
        result_original = tool_fn(BlueprintPath=BP_PATH, DetailLevel="detail")

        _assert(result_snake.get("detail_level") == "detail", f"snake_case params not forwarded: {result_snake}")
        _assert(result_original.get("detail_level") == "detail", f"original params not forwarded: {result_original}")
        _assert(calls[0] == ("GetBlueprintInfo", {"BlueprintPath": BP_PATH, "DetailLevel": "detail"}),
                f"unexpected snake_case forwarding: {calls[0]}")
        _assert(calls[1] == ("GetBlueprintInfo", {"BlueprintPath": BP_PATH, "DetailLevel": "detail"}),
                f"unexpected original-name forwarding: {calls[1]}")
    finally:
        server.ue_editor.call_plugin = original_call_plugin


def test_reflection_validation_errors_are_structured() -> None:
    unknown = ue_editor.call_plugin("GetBlueprintInfo", BlueprintPath=BP_PATH, BogusField="x")
    _assert(unknown.get("error_code") == "invalid_params", f"unknown-param error_code mismatch: {unknown}")
    unknown_errors = unknown.get("validation_errors", [])
    _assert(any(issue.get("code") == "unknown_param" and issue.get("param") == "BogusField" for issue in unknown_errors),
            f"unknown-param details missing BogusField: {unknown}")

    invalid = ue_editor.call_plugin("ListAssets", Path="/Game/SurvivorsTemplate/Blueprints", Limit={"bad": 1})
    _assert(invalid.get("error_code") == "invalid_params", f"invalid-type error_code mismatch: {invalid}")
    invalid_errors = invalid.get("validation_errors", [])
    _assert(any(issue.get("code") == "invalid_param_type" and issue.get("param") == "Limit" for issue in invalid_errors),
            f"invalid-type details missing Limit: {invalid}")


def test_component_bound_event_node_type_support() -> None:
    schema = _schema("AddBlueprintNodeByType")
    hint = schema.get("workflow_hint", "")
    _assert(hint, f"AddBlueprintNodeByType should keep workflow hint: {schema}")

    result = ue_editor.call_plugin(
        "AddBlueprintNodeByType",
        BlueprintPath=MAIN_MENU_WIDGET_PATH,
        GraphName="EventGraph",
        NodeType="ComponentBoundEvent:Button_Host.OnClicked",
        LocationX=-1200,
        LocationY=2200,
    )
    _assert(result.get("ok") is True, f"component bound event creation failed: {result}")
    _assert(result.get("node_type") == "ComponentBoundEvent", f"unexpected node type: {result}")
    _assert(result.get("info"), f"component bound event should explain whether it created or reused a node: {result}")
    if result.get("already_exists") is True:
        _assert(result.get("repaired_existing") is False, f"existing component event should report no repair by default: {result}")

    graph = _call("GetBlueprintGraph", BlueprintPath=MAIN_MENU_WIDGET_PATH, GraphName="EventGraph")
    nodes = graph.get("nodes", [])
    host_events = [node for node in nodes if node.get("title") == "On Clicked (Button_Host)"]
    _assert(host_events, f"expected Button_Host bound event in graph: {graph}")


def test_engine_event_node_sets_override_flag() -> None:
    temp_name = "OhMyUE_BP_EventOverrideProbe"
    temp_bp = f"/Game/{temp_name}"
    existing = ue_editor.call_plugin("GetBlueprintInfo", BlueprintPath=temp_bp)
    if existing.get("ok") is True:
        _call("DeleteAsset", AssetPath=f"{temp_bp}.{temp_name}")

    created = _call("CreateBlueprint", Path="/Game", Name=temp_name, ParentClass="Actor")
    _assert(created.get("ok") is True, f"failed to create probe blueprint: {created}")

    add_result = _call(
        "AddBlueprintNodeByType",
        BlueprintPath=temp_bp,
        GraphName="EventGraph",
        NodeType="Event:BeginPlay",
        LocationX=0,
        LocationY=0,
    )
    _assert(add_result.get("ok") is True, f"failed to add begin play event: {add_result}")

    export_result = _call(
        "ExportBlueprintGraphText",
        BlueprintPath=temp_bp,
        GraphName="EventGraph",
        OutputDir=str(ROOT / "tests" / "_tmp_exports"),
    )
    export_path = export_result.get("file_path")
    _assert(export_path, f"missing export file path: {export_result}")
    export_text = Path(export_path).read_text(encoding="utf-8")
    _assert('MemberName="ReceiveBeginPlay"' in export_text, f"expected ReceiveBeginPlay event in export: {export_text}")
    _assert("bOverrideFunction=True" in export_text, f"engine event node should set override flag: {export_text}")

    _call("DeleteAsset", AssetPath=f"{temp_bp}.{temp_name}")


def test_engine_event_node_repairs_legacy_non_override_event() -> None:
    temp_name = "OhMyUE_BP_EventRepairProbe"
    temp_bp = f"/Game/{temp_name}"
    existing = ue_editor.call_plugin("GetBlueprintInfo", BlueprintPath=temp_bp)
    if existing.get("ok") is True:
        _call("DeleteAsset", AssetPath=f"{temp_bp}.{temp_name}")

    created = _call("CreateBlueprint", Path="/Game", Name=temp_name, ParentClass="Actor")
    _assert(created.get("ok") is True, f"failed to create repair probe blueprint: {created}")

    _call(
        "AddBlueprintNodeByType",
        BlueprintPath=temp_bp,
        GraphName="EventGraph",
        NodeType="Event:BeginPlay",
        LocationX=0,
        LocationY=0,
    )
    export_result = _call(
        "ExportBlueprintGraphText",
        BlueprintPath=temp_bp,
        GraphName="EventGraph",
        OutputDir=str(ROOT / "tests" / "_tmp_exports"),
    )
    export_path = Path(export_result["file_path"])
    legacy_path = export_path.with_name(f"{export_path.stem}_legacy.copy")
    export_text = export_path.read_text(encoding="utf-8")
    legacy_text = export_text.replace("   bOverrideFunction=True\n", "", 1)
    legacy_path.write_text(legacy_text, encoding="utf-8")

    graph = _call("GetBlueprintGraph", BlueprintPath=temp_bp, GraphName="EventGraph")
    begin_play = next(node for node in graph.get("nodes", []) if node.get("title") == "Event BeginPlay")
    _call("RemoveBlueprintNodeByGuid", BlueprintPath=temp_bp, GraphName="EventGraph", NodeId=begin_play["id"])
    _call("ImportBlueprintGraphText", BlueprintPath=temp_bp, GraphName="EventGraph", FilePath=str(legacy_path))

    repaired = _call(
        "AddBlueprintNodeByType",
        BlueprintPath=temp_bp,
        GraphName="EventGraph",
        NodeType="Event:BeginPlay",
        LocationX=0,
        LocationY=0,
    )
    _assert(repaired.get("repaired_existing") is True, f"legacy event should be repaired in place: {repaired}")

    repaired_export = _call(
        "ExportBlueprintGraphText",
        BlueprintPath=temp_bp,
        GraphName="EventGraph",
        OutputDir=str(ROOT / "tests" / "_tmp_exports"),
    )
    repaired_text = Path(repaired_export["file_path"]).read_text(encoding="utf-8")
    _assert("bOverrideFunction=True" in repaired_text, f"repaired export should restore override flag: {repaired_text}")

    _call("DeleteAsset", AssetPath=f"{temp_bp}.{temp_name}")


def test_ping_plugin_uses_file_bridge_fallback() -> None:
    original_urlopen = ue_editor.urllib.request.urlopen
    original_available = ue_editor._file_bridge_available
    original_request = ue_editor._request_file_bridge

    def _raise_url_error(*args, **kwargs):
        raise urllib.error.URLError("timeout")

    def _fake_request(payload: dict, *, timeout: int, poll_interval: float = 0.1) -> dict:
        _assert(payload.get("op") == "ping", f"unexpected ping payload: {payload}")
        return {"ok": True, "transport": "file"}

    try:
        ue_editor.urllib.request.urlopen = _raise_url_error
        ue_editor._file_bridge_available = lambda max_age_seconds=5.0: True
        ue_editor._request_file_bridge = _fake_request
        result = ue_editor.ping_plugin()
    finally:
        ue_editor.urllib.request.urlopen = original_urlopen
        ue_editor._file_bridge_available = original_available
        ue_editor._request_file_bridge = original_request

    _assert(result.get("ok") is True, f"ping fallback failed: {result}")
    _assert(result.get("transport") == "file", f"ping fallback transport mismatch: {result}")


def test_call_plugin_uses_file_bridge_fallback() -> None:
    original_urlopen = ue_editor.urllib.request.urlopen
    original_available = ue_editor._file_bridge_available
    original_request = ue_editor._request_file_bridge

    def _raise_url_error(*args, **kwargs):
        raise urllib.error.URLError("unreachable")

    def _fake_request(payload: dict, *, timeout: int, poll_interval: float = 0.1) -> dict:
        _assert(payload.get("op") == "call", f"unexpected op: {payload}")
        _assert(payload.get("function") == "GetCurrentLevel", f"unexpected function payload: {payload}")
        _assert(payload.get("params") == {"Verbose": True}, f"unexpected params payload: {payload}")
        return {"ok": True, "transport": "file", "level": "/Game/Test"}

    try:
        ue_editor.urllib.request.urlopen = _raise_url_error
        ue_editor._file_bridge_available = lambda max_age_seconds=5.0: True
        ue_editor._request_file_bridge = _fake_request
        result = ue_editor.call_plugin("GetCurrentLevel", Verbose=True)
    finally:
        ue_editor.urllib.request.urlopen = original_urlopen
        ue_editor._file_bridge_available = original_available
        ue_editor._request_file_bridge = original_request

    _assert(result.get("ok") is True, f"call fallback failed: {result}")
    _assert(result.get("transport") == "file", f"call fallback transport mismatch: {result}")


def main() -> int:
    tests = [
        test_blueprint_info_summary,
        test_blueprint_info_detail,
        test_blueprint_graphs_summary_vs_detail,
        test_blueprint_nodes_summary_vs_detail,
        test_blueprint_graph_raw,
        test_blueprint_context_compatibility_shim,
        test_blueprint_workflow_metadata,
        test_recent_metadata_cleanup_targets,
        test_actor_pie_widget_metadata_cleanup_targets,
        test_asset_widget_audio_metadata_cleanup_targets,
        test_modal_widget_metadata_targets,
        test_list_assets_offset_and_limit_contract,
        test_bridge_capability_output,
        test_blueprint_workflow_planner,
        test_blueprint_extended_workflow_planner,
        test_widget_workflow_planner,
        test_ue_close_save_mode_forwarding,
        test_ue_process_close_rejects_unsupported_save_mode,
        test_ue_close_all_save_mode_forwarding,
        test_ue_process_close_all_rejects_unsupported_save_mode,
        test_plugin_bridge_accepts_original_and_snake_case_params,
        test_reflection_validation_errors_are_structured,
        test_component_bound_event_node_type_support,
        test_engine_event_node_sets_override_flag,
        test_engine_event_node_repairs_legacy_non_override_event,
        test_ping_plugin_uses_file_bridge_fallback,
        test_call_plugin_uses_file_bridge_fallback,
    ]

    failures: list[str] = []
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{test.__name__}: {exc}")
            print(f"FAIL {test.__name__}: {exc}")

    if failures:
        print("\nBlueprint protocol regression failures:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("\nAll blueprint protocol regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
