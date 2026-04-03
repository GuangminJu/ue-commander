import sys
from typing import Callable

from mcp.server.fastmcp import FastMCP

from .. import ue_editor
from ..models.state import BridgeState
from .capability_registry import CapabilityRegistry


def _camel_to_snake(name: str) -> str:
    chars: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index > 0 and not name[index - 1].isupper():
            chars.append("_")
        chars.append(char.lower())
    return "".join(chars)


class PluginBridge:
    """Owns plugin connectivity, capability refresh, and dynamic tool registration."""

    def __init__(self, mcp: FastMCP, registry: CapabilityRegistry) -> None:
        self._mcp = mcp
        self._registry = registry
        self._registered_plugin_tools: set[str] = set()

    def get_state(self) -> BridgeState:
        crash = ue_editor.read_crash_info()
        if crash is not None:
            return BridgeState(
                state="crashed",
                plugin_ready=False,
                game_thread_responsive=False,
                crash_info=crash,
                detail="Crash marker present.",
            )

        ping = ue_editor.ping_plugin()
        plugin_ready = ping.get("ok", False)
        game_thread_responsive = ping.get("game_thread_responsive", False)

        if not plugin_ready:
            return BridgeState(
                state="disconnected",
                plugin_ready=False,
                game_thread_responsive=False,
                detail="Plugin not reachable.",
            )
        if not game_thread_responsive:
            return BridgeState(
                state="blocked",
                plugin_ready=True,
                game_thread_responsive=False,
                detail="Plugin reachable but game thread is not responsive.",
            )
        return BridgeState(
            state="ready",
            plugin_ready=True,
            game_thread_responsive=True,
        )

    def plugin_status(self) -> dict:
        state = self.get_state()
        if state.crash_info is not None:
            return {
                "ok": False,
                "crashed": True,
                "crash_info": state.crash_info,
                "hint": "UE crashed. Check crash_info for details. Fix the issue and relaunch UE.",
                "bridge_state": state.to_dict(),
            }
        if not state.plugin_ready:
            return {
                "ok": False,
                "error": "Plugin not reachable. Is UE running with OhMyUnrealEngine loaded?",
                "bridge_state": state.to_dict(),
            }

        tools = ue_editor.list_plugin_tools()
        if "error" in tools:
            return {
                "ok": False,
                "error": tools["error"],
                "bridge_state": state.to_dict(),
            }

        self.refresh_plugin_tools(tools)
        return {
            "ok": True,
            "bridge_state": state.to_dict(),
            **tools,
        }

    def list_capabilities(self, include_core: bool = True, include_plugin: bool = True) -> dict:
        caps = []
        for capability in self._registry.list_capabilities():
            if capability.source == "core" and not include_core:
                continue
            if capability.source == "plugin" and not include_plugin:
                continue
            caps.append(capability.to_dict())
        return {
            "ok": True,
            "bridge_state": self.get_state().to_dict(),
            "capabilities": caps,
            "count": len(caps),
        }

    def refresh_plugin_tools(self, tools_data: dict | None = None) -> int:
        if tools_data is None:
            tools_data = ue_editor.list_plugin_tools()
        if "error" in tools_data:
            print("[ue-commander] Plugin not reachable, skipping capability refresh.", file=sys.stderr)
            return 0

        tool_items = tools_data.get("items") or tools_data.get("tools") or []
        detailed_tools = tool_items
        if tool_items and "params" not in tool_items[0]:
            schema_data = ue_editor.list_plugin_tool_schemas()
            if "error" in schema_data:
                print(
                    "[ue-commander] Plugin schema endpoint unavailable, falling back to /api/tools?detail=full.",
                    file=sys.stderr,
                )
                schema_data = ue_editor.list_plugin_tools(detail="full")
            if "error" in schema_data:
                print(f"[ue-commander] Failed to load plugin tool schemas: {schema_data['error']}", file=sys.stderr)
                return 0
            detailed_tools = schema_data.get("items") or schema_data.get("tools") or []

        capabilities = self._registry.upsert_plugin_tools(detailed_tools)
        existing = set(self._mcp._tool_manager._tools.keys())
        registered = 0

        for capability in capabilities:
            if capability.mcp_name in existing or capability.mcp_name in self._registered_plugin_tools:
                continue
            self._mcp.tool()(self._make_plugin_tool(capability))
            self._registered_plugin_tools.add(capability.mcp_name)
            registered += 1

        total = len(detailed_tools)
        print(f"[ue-commander] Auto-registered {registered}/{total} plugin tools.", file=sys.stderr)
        return registered

    def _make_plugin_tool(self, capability) -> Callable[..., dict]:
        def tool_fn(**kwargs) -> dict:
            plugin_kwargs = {}
            normalized_kwargs = {key.lower(): value for key, value in kwargs.items()}
            snake_kwargs = {_camel_to_snake(key): value for key, value in kwargs.items()}
            for param in capability.params:
                if param.original_name in kwargs:
                    plugin_kwargs[param.original_name] = kwargs[param.original_name]
                    continue
                if param.name in kwargs:
                    plugin_kwargs[param.original_name] = kwargs[param.name]
                    continue
                original_lower = param.original_name.lower()
                if original_lower in normalized_kwargs:
                    plugin_kwargs[param.original_name] = normalized_kwargs[original_lower]
                    continue
                if param.name in snake_kwargs:
                    plugin_kwargs[param.original_name] = snake_kwargs[param.name]
            return ue_editor.call_plugin(capability.name, **plugin_kwargs)

        tool_fn.__name__ = capability.mcp_name
        tool_fn.__qualname__ = capability.mcp_name
        tool_fn.__doc__ = capability.description
        tool_fn.__signature__ = self._registry.make_signature(capability)
        tool_fn.__annotations__ = {param.name: param.python_type for param in capability.params}
        tool_fn.__annotations__["return"] = dict
        return tool_fn
