import inspect
import re
from typing import Any

from ..models.capability import Capability, CapabilityParam

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


def pascal_to_snake(name: str) -> str:
    """Convert PascalCase to snake_case."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    return s.lower()


def python_type_for_plugin_type(type_name: str) -> type:
    return _TYPE_MAP.get(type_name, str)


def python_default_for_type(py_type: type) -> Any:
    return _TYPE_DEFAULTS.get(py_type, "")


class CapabilityRegistry:
    """Stores normalized core and plugin capability metadata."""

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        self._capabilities[capability.mcp_name] = capability

    def register_manual_tool(
        self,
        mcp_name: str,
        description: str,
        *,
        source: str = "core",
        availability: str = "offline",
        safety: str = "safe",
        requires_editor: bool = False,
        timeout_class: str = "normal",
    ) -> None:
        self.register(
            Capability(
                name=mcp_name.removeprefix("ue_"),
                mcp_name=mcp_name,
                description=description,
                source=source,
                availability=availability,
                safety=safety,
                requires_editor=requires_editor,
                timeout_class=timeout_class,
            )
        )

    def register_core_tool(
        self,
        mcp_name: str,
        description: str,
        *,
        safety: str = "safe",
        timeout_class: str = "normal",
    ) -> None:
        self.register_manual_tool(
            mcp_name,
            description,
            source="core",
            availability="offline",
            safety=safety,
            requires_editor=False,
            timeout_class=timeout_class,
        )

    def register_plugin_tool(self, tool: dict) -> Capability:
        params: list[CapabilityParam] = []
        for raw_param in tool.get("params", []):
            py_type = python_type_for_plugin_type(raw_param["type"])
            params.append(
                CapabilityParam(
                    name=pascal_to_snake(raw_param["name"]),
                    original_name=raw_param["name"],
                    param_type=raw_param["type"],
                    python_type=py_type,
                    default=python_default_for_type(py_type),
                )
            )

        capability = Capability(
            name=tool["name"],
            mcp_name=f"ue_{pascal_to_snake(tool['name'])}",
            description=tool.get("description") or tool.get("summary", ""),
            source="plugin",
            availability="online",
            safety=tool.get("safety", "unknown"),
            requires_editor=True,
            requires_map_loaded=tool.get("requires_map_loaded", False),
            requires_asset_context=tool.get("requires_asset_context", False),
            timeout_class=tool.get("timeout_class", "normal"),
            category=tool.get("category", ""),
            workflow_hint=tool.get("workflow_hint", ""),
            recommended_reads=tool.get("recommended_reads", ""),
            deprecated=tool.get("deprecated", False),
            canonical_tool=tool.get("canonical_tool", ""),
            params=params,
        )
        self.register(capability)
        return capability

    def upsert_plugin_tools(self, tools: list[dict]) -> list[Capability]:
        return [self.register_plugin_tool(tool) for tool in tools]

    def list_capabilities(self) -> list[Capability]:
        return sorted(self._capabilities.values(), key=lambda item: item.mcp_name)

    def to_dicts(self) -> list[dict]:
        return [capability.to_dict() for capability in self.list_capabilities()]

    def get(self, mcp_name: str) -> Capability | None:
        return self._capabilities.get(mcp_name)

    def make_signature(self, capability: Capability) -> inspect.Signature:
        params = [
            inspect.Parameter(
                param.name,
                inspect.Parameter.KEYWORD_ONLY,
                default=param.default,
                annotation=param.python_type,
            )
            for param in capability.params
        ]
        return inspect.Signature(params, return_annotation=dict)
