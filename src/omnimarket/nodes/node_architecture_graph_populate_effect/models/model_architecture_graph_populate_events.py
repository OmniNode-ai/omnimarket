# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Event and data models for architecture graph populate request/response."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ModelArchitectureGraphPopulateRequestedEvent",
    "ModelArchitectureGraphPopulateResponseEvent",
    "ModelGraphEdgeSpec",
    "ModelGraphNodeSpec",
    "ModelGraphPopulateSourceAuthority",
    "ModelGraphSnapshotMeta",
]

ArchPopulateOperation = Literal[
    "populate_from_contracts",
    "populate_from_imports",
    "populate_from_pyproject",
    "populate_all",
]

ArchPopulateStatus = Literal["success", "error", "dry_run"]


class ModelGraphPopulateSourceAuthority(StrEnum):
    """Classification of evidence strength for a graph edge source."""

    AUTHORITATIVE = "authoritative"
    EVIDENCE = "evidence"


class ModelGraphNodeSpec(BaseModel):
    """Specification for a graph node to be MERGEd into the graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(..., description="Stable unique identifier for MERGE key")
    label: str = Field(
        ...,
        description="Node label: Repository | ONEXNode | KafkaTopic | PythonModule",
    )
    properties: dict[str, str] = Field(
        default_factory=dict,
        description="Node properties to set on MERGE",
    )


class ModelGraphEdgeSpec(BaseModel):
    """Specification for a directed graph edge to be MERGEd into the graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(..., description="Source node_id")
    target_id: str = Field(..., description="Target node_id")
    edge_type: str = Field(
        ...,
        description="Relationship type: DEPENDS_ON | IMPORTS | PUBLISHES_TO | SUBSCRIBES_TO | CONTAINS",
    )
    source_authority: str = Field(
        ...,
        description="Evidence classification: authoritative (contract) or evidence (static-analysis/pyproject)",
    )
    properties: dict[str, str] = Field(
        default_factory=dict,
        description="Edge properties to set on MERGE",
    )


class ModelGraphSnapshotMeta(BaseModel):
    """Metadata tracking fields for a graph snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_schema_version: str = Field(
        ..., description="Schema version of the graph at populate time"
    )
    graph_snapshot_id: str = Field(
        ..., description="UUID identifying this specific snapshot run"
    )
    populated_from_commit_set: tuple[str, ...] = Field(
        default=(),
        description="Git commit SHAs from which this snapshot was derived",
    )
    repo_count: int = Field(default=0, description="Number of repos discovered")
    node_count: int = Field(default=0, description="Number of graph nodes written")
    edge_count: int = Field(default=0, description="Number of graph edges written")


class ModelArchitectureGraphPopulateRequestedEvent(BaseModel):
    """Inbound request event for an architecture graph populate operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    populate_id: str = Field(..., description="Unique identifier for this populate run")
    operation: ArchPopulateOperation = Field(
        ...,
        description="Operation: populate_from_contracts | populate_from_imports | populate_from_pyproject | populate_all",
    )
    omni_home: str = Field(
        ...,
        description="Path to omni_home root where all repos are checked out",
    )
    correlation_id: str | None = Field(
        default=None, description="Correlation ID for tracing"
    )
    dry_run: bool = Field(
        default=False,
        description="If true, parse sources and build specs but skip graph writes",
    )
    repos: tuple[str, ...] = Field(
        default=(),
        description="Restrict populate to these repo names; empty = all repos under omni_home",
    )


class ModelArchitectureGraphPopulateResponseEvent(BaseModel):
    """Outbound response event from an architecture graph populate operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    populate_id: str = Field(..., description="Echoed populate identifier")
    operation: str = Field(..., description="Operation that was performed")
    status: ArchPopulateStatus = Field(..., description="Result status")
    snapshot_meta: ModelGraphSnapshotMeta | None = Field(
        default=None,
        description="Graph snapshot tracking metadata",
    )
    nodes_written: tuple[ModelGraphNodeSpec, ...] = Field(
        default=(),
        description="Node specs that were (or would be in dry_run) MERGEd",
    )
    edges_written: tuple[ModelGraphEdgeSpec, ...] = Field(
        default=(),
        description="Edge specs that were (or would be in dry_run) MERGEd",
    )
    error_message: str | None = Field(
        default=None, description="Error details when status is 'error'"
    )
    execution_time_ms: float | None = Field(
        default=None, description="Populate execution time in milliseconds"
    )
    correlation_id: str | None = Field(
        default=None, description="Correlation ID echoed from request"
    )

    @classmethod
    def from_error(
        cls,
        *,
        populate_id: str,
        operation: str,
        error_message: str,
        correlation_id: str | None = None,
    ) -> ModelArchitectureGraphPopulateResponseEvent:
        return cls(
            populate_id=populate_id,
            operation=operation,
            status="error",
            error_message=error_message,
            correlation_id=correlation_id,
        )
