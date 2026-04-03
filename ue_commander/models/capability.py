from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CapabilityParam:
    """Normalized parameter metadata for a capability."""

    name: str
    original_name: str
    param_type: str
    python_type: type
    default: Any


@dataclass(slots=True)
class Capability:
    """A normalized view of an MCP capability."""

    name: str
    mcp_name: str
    description: str
    source: str
    availability: str
    safety: str
    requires_editor: bool
    requires_map_loaded: bool = False
    requires_asset_context: bool = False
    timeout_class: str = "normal"
    category: str = ""
    workflow_hint: str = ""
    recommended_reads: str = ""
    deprecated: bool = False
    canonical_tool: str = ""
    version: str = "1"
    params: list[CapabilityParam] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "mcp_name": self.mcp_name,
            "description": self.description,
            "source": self.source,
            "availability": self.availability,
            "safety": self.safety,
            "requires_editor": self.requires_editor,
            "requires_map_loaded": self.requires_map_loaded,
            "requires_asset_context": self.requires_asset_context,
            "timeout_class": self.timeout_class,
            "category": self.category,
            "workflow_hint": self.workflow_hint,
            "recommended_reads": self.recommended_reads,
            "deprecated": self.deprecated,
            "canonical_tool": self.canonical_tool,
            "version": self.version,
            "params": [
                {
                    "name": param.name,
                    "original_name": param.original_name,
                    "type": param.param_type,
                }
                for param in self.params
            ],
        }
