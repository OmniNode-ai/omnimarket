# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Event models for architecture graph query request/response."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ModelArchQueryGraphEdge",
    "ModelArchQueryGraphNode",
    "ModelArchitectureGraphQueryRequestedEvent",
    "ModelArchitectureGraphQueryResponseEvent",
]

ArchQueryOperation = Literal[
    "dependency_path",
    "blast_radius",
    "cross_repo_imports",
    "circular_deps",
]

ArchQueryStatus = Literal["success", "error", "no_results"]


class ModelArchQueryGraphNode(BaseModel):
    """A node in an architecture dependency graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., description="Node name (e.g. module or repo identifier)")
    node_type: str = Field(
        ..., description="Node type (e.g. 'module', 'repo', 'package')"
    )
    repo: str | None = Field(default=None, description="Owning repository, if known")
    properties: dict[str, str] | None = Field(
        default=None, description="Additional node properties"
    )


class ModelArchQueryGraphEdge(BaseModel):
    """A directed edge in an architecture dependency graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(..., description="Source node name")
    target: str = Field(..., description="Target node name")
    edge_type: str = Field(
        ..., description="Relationship type (e.g. 'DEPENDS_ON', 'IMPORTS')"
    )
    properties: dict[str, str] | None = Field(
        default=None, description="Additional edge properties"
    )


class ModelArchitectureGraphQueryRequestedEvent(BaseModel):
    """Inbound request event for an architecture graph query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str = Field(..., description="Unique identifier for this query")
    operation: str = Field(
        ...,
        description="Operation to perform: dependency_path | blast_radius | cross_repo_imports | circular_deps",
    )
    correlation_id: str | None = Field(
        default=None, description="Correlation ID for tracing"
    )

    # dependency_path operands
    from_node: str | None = Field(
        default=None, description="Source node for dependency_path"
    )
    to_node: str | None = Field(
        default=None, description="Target node for dependency_path"
    )

    # blast_radius / circular_deps operand
    target: str | None = Field(default=None, description="Target node for blast_radius")

    # cross_repo_imports / circular_deps operand
    repo: str | None = Field(
        default=None,
        description="Repository scope for cross_repo_imports or circular_deps",
    )


class ModelArchitectureGraphQueryResponseEvent(BaseModel):
    """Outbound response event from an architecture graph query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str = Field(..., description="Echoed query identifier")
    operation: str = Field(..., description="Operation that was performed")
    status: ArchQueryStatus = Field(..., description="Result status")
    nodes: tuple[ModelArchQueryGraphNode, ...] = Field(
        default=(),
        description="Graph nodes returned by the operation",
    )
    edges: tuple[ModelArchQueryGraphEdge, ...] = Field(
        default=(),
        description="Graph edges returned by the operation",
    )
    path_length: int | None = Field(
        default=None,
        description="Shortest path length (dependency_path only)",
    )
    error_message: str | None = Field(
        default=None, description="Error details when status is 'error'"
    )
    execution_time_ms: float | None = Field(
        default=None, description="Query execution time in milliseconds"
    )
    correlation_id: str | None = Field(
        default=None, description="Correlation ID echoed from request"
    )

    @classmethod
    def from_error(
        cls,
        *,
        query_id: str,
        operation: str,
        error_message: str,
        correlation_id: str | None = None,
    ) -> ModelArchitectureGraphQueryResponseEvent:
        return cls(
            query_id=query_id,
            operation=operation,
            status="error",
            error_message=error_message,
            correlation_id=correlation_id,
        )
