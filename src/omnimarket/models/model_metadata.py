"""Pydantic model for validating node metadata.yaml files."""

from __future__ import annotations

from pydantic import BaseModel, Field

from omnimarket.enums.enum_node_role import EnumNodeRole


class MetadataEntryPoint(BaseModel):
    """Entry point mapping for onex.nodes."""

    onex_nodes: dict[str, str] = Field(default_factory=dict, alias="onex.nodes")


class MetadataCapabilities(BaseModel):
    """Capability flags for a marketplace node."""

    standalone: bool = True
    full_runtime: bool = True
    requires_network: bool = False
    requires_repo: bool = False
    requires_secrets: bool = False
    requires_docker: bool = False
    side_effect_class: str = "read_only"


class MetadataSchema(BaseModel):
    """Schema for node metadata.yaml files."""

    name: str
    version: str
    description: str
    omnibase_core_compat: str = ">=0.39.0,<1.0.0"
    entry_points: MetadataEntryPoint = Field(default_factory=MetadataEntryPoint)
    capabilities: MetadataCapabilities = Field(default_factory=MetadataCapabilities)
    dependencies: list[str] = Field(default_factory=list)
    authors: list[str] = Field(default_factory=list)
    license: str = "MIT"
    tags: list[str] = Field(default_factory=list)
    deprecated: bool = False
    deprecated_by: str | None = None
    deprecated_reason: str | None = None
    pack: str | None = None
    display_name: str | None = None
    node_role: EnumNodeRole | None = None
    entry_flags: dict[str, str] | None = Field(
        default=None,
        description=(
            "FSM entry point flags for orchestrator nodes (node_role=orchestrator). "
            "Keys are flag names (e.g. 'dry_run', 'fix_only'); values are descriptions. "
            "Only meaningful when node_role is 'orchestrator'."
        ),
    )
