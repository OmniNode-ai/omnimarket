"""ModelMcpToolProjection — row model for the mcp_tools projection table."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_mcp_tools.models.enums import EnumMcpToolStatus


class ModelMcpToolProjection(BaseModel):
    """Immutable row model for a single MCP tool entry in the mcp_tools table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str = Field(
        ..., description="Unique MCP tool identifier (conflict key)."
    )
    description: str = Field(default="", description="Human-readable tool description.")
    model_id: str = Field(
        default="", description="Primary model ID associated with this tool."
    )
    correlation_id: str = Field(
        default="",
        description="Correlation ID from the originating registration request.",
    )
    status: EnumMcpToolStatus = Field(
        default=EnumMcpToolStatus.ACTIVE,
        description="Tool registration status.",
    )
    is_active: bool = Field(default=True, description="Whether the tool is active.")
    mcp_tags: tuple[str, ...] = Field(
        default=(), description="MCP capability tags from the registration event."
    )
    metadata: dict[str, object] = Field(
        default_factory=dict,
        description="Supplementary metadata (description, model_id, etc.) from contract.",
    )
    registered_at: str = Field(
        ..., description="ISO 8601 timestamp of initial registration."
    )
    projected_at: str = Field(
        ..., description="ISO 8601 timestamp when this projection row was written."
    )


__all__: list[str] = ["ModelMcpToolProjection"]
