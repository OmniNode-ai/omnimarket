# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the seam parallel executor node.

Defines the task graph, protocol shim contract, and execution result types
for deterministic wave-based parallel execution.

Related:
    - OMN-8032: node_seam_parallel_executor in omnimarket
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumSeamTaskStatus(StrEnum):
    """Task lifecycle states for seam parallel execution.

    Mirrors the overseer EnumTaskStatus subset relevant to parallel execution.
    Will be replaced by omnibase_compat.overseer.EnumTaskStatus once OMN-8026 merges.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ModelSeamTask(BaseModel):
    """A single task in the seam parallel execution graph.

    Each task references a callable_key that maps to a protocol shim,
    and declares dependencies on other tasks by task_id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(..., description="Unique identifier for this task.")
    callable_key: str = Field(
        ...,
        description="Key mapping to a registered protocol shim callable.",
    )
    depends_on: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Task IDs that must complete before this task runs.",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary payload passed to the protocol shim.",
    )
    timeout_seconds: float | None = Field(
        default=None,
        description="Per-task timeout in seconds. None means use the global default.",
    )


class ModelSeamParallelInput(BaseModel):
    """Input to the seam parallel executor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Execution correlation ID.")
    tasks: tuple[ModelSeamTask, ...] = Field(
        ..., description="Tasks to execute with dependency graph."
    )
    timeout_seconds: float = Field(
        default=60.0,
        description="Global timeout for the entire execution.",
    )


class ModelSeamTaskResult(BaseModel):
    """Result from executing a single seam task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(..., description="Task ID this result belongs to.")
    status: EnumSeamTaskStatus = Field(..., description="Final task status.")
    output: Any = Field(default=None, description="Output from the protocol shim.")
    error: str | None = Field(default=None, description="Error message if failed.")


class ModelSeamParallelResult(BaseModel):
    """Result from the seam parallel executor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Execution correlation ID.")
    all_succeeded: bool = Field(
        ..., description="Whether all tasks completed successfully."
    )
    task_results: tuple[ModelSeamTaskResult, ...] = Field(
        ..., description="Per-task results in execution order."
    )
    shims_removed: bool = Field(
        ..., description="Whether protocol shims were cleaned up."
    )
    waves_executed: int = Field(..., description="Number of parallel waves executed.")


__all__: list[str] = [
    "EnumSeamTaskStatus",
    "ModelSeamParallelInput",
    "ModelSeamParallelResult",
    "ModelSeamTask",
    "ModelSeamTaskResult",
]
