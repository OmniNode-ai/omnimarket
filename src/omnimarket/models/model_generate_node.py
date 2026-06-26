# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared I/O models for node generation scaffolding.

OMN-13605 (Phase 0.1): these models are imported by BOTH
``node_generate_node_effect`` (the scaffolder that owns the file-write effect)
and ``node_generation_consumer`` (the generation spine that invokes the
scaffolder to materialize the full canonical package). A model shared by two
in-repo node consumers lives in the shared ``omnimarket.models`` package — never
in a sibling node's models package — so it is not a cross-node reach-in
(test_no_cross_node_reach_in.py / OMN-9263).
"""

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


class ModelGenerateNodeResult(BaseModel):
    """Result listing the files created by a generate-node run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Mirrors the command correlation ID.")
    node_name: str = Field(..., description="Name of the scaffolded node.")
    created_files: tuple[str, ...] = Field(
        default=(),
        description="Paths of files written to disk, relative to output_dir.",
    )
    output_dir: str = Field(..., description="Directory where files were written.")
    dry_run: bool = Field(
        default=False,
        description="True when no files were actually written (dry-run mode).",
    )


__all__: list[str] = [
    "EnumNodeType",
    "ModelGenerateNodeCommand",
    "ModelGenerateNodeResult",
]
