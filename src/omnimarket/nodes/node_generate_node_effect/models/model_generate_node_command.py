"""ModelGenerateNodeCommand — command to scaffold a new ONEX node."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumNodeType(StrEnum):
    """Supported ONEX node types for generation."""

    EFFECT = "effect"
    COMPUTE = "compute"
    REDUCER = "reducer"
    ORCHESTRATOR = "orchestrator"


class ModelGenerateNodeCommand(BaseModel):
    """Command to scaffold a new ONEX node via template expansion and file writes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Run correlation ID.")
    node_name: str = Field(
        ...,
        description="Snake-case node name, e.g. node_my_feature_effect.",
        pattern=r"^node_[a-z][a-z0-9_]*$",
    )
    node_type: EnumNodeType = Field(..., description="Node archetype to scaffold.")
    output_dir: str = Field(
        ...,
        description="Absolute or repo-relative path where the node directory is written.",
    )
    template_args: dict[str, str] = Field(
        default_factory=dict,
        description="Additional key-value pairs forwarded to the Jinja2 template engine.",
    )
    dry_run: bool = Field(
        default=False,
        description="When True, compute the file manifest without writing to disk.",
    )


__all__: list[str] = ["EnumNodeType", "ModelGenerateNodeCommand"]
