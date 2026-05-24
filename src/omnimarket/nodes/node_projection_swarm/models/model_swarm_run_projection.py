"""ModelSwarmRunProjection — row model for the swarm_runs projection table."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_swarm.models.enums import EnumSwarmRunStatus


class ModelSwarmRunProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., description="Unique swarm run identifier (conflict key).")
    correlation_id: str = Field(
        ..., description="Correlation ID from originating request."
    )
    status: EnumSwarmRunStatus = Field(
        ..., description="Terminal status of the swarm run."
    )
    task_hash: str = Field(..., description="Hash of the decomposed task.")
    subtask_count: int = Field(..., ge=0, description="Number of subtasks dispatched.")
    succeeded_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    models_used: tuple[str, ...] = Field(
        default=(), description="Model IDs used in this run."
    )
    machines_used: tuple[str, ...] = Field(
        default=(), description="Machine identifiers used."
    )
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    cloud_equivalent_cost_usd: float = Field(default=0.0, ge=0.0)
    savings_usd: float = Field(default=0.0)
    parallelism_speedup_ratio: float = Field(default=1.0, ge=0.0)
    decomposition_latency_ms: int = Field(default=0, ge=0)
    dispatch_wall_latency_ms: int = Field(default=0, ge=0)
    aggregation_latency_ms: int = Field(default=0, ge=0)
    total_latency_ms: int = Field(default=0, ge=0)
    endpoint_registry_hash: str = Field(default="")
    registry_schema_version: str = Field(default="")
    created_at: str = Field(
        ..., description="ISO 8601 timestamp when the run was created."
    )


__all__: list[str] = ["ModelSwarmRunProjection"]
