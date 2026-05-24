"""HandlerProjectionSwarm — project swarm dispatch terminal events to DB.

Consumes onex.evt.omnimarket.swarm-dispatch-completed.v1 and
onex.evt.omnimarket.swarm-dispatch-failed.v1, then UPSERTs into the
swarm_runs table (keyed by run_id). Replay-safe and idempotent.

Freshness SLA: max_lag_seconds=30, degraded_after_seconds=60.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_swarm.models.enums import (
    EnumFreshnessState,
    EnumSwarmRunStatus,
)
from omnimarket.nodes.node_projection_swarm.models.model_projection_freshness import (
    ModelProjectionFreshness,
)
from omnimarket.nodes.node_projection_swarm.models.model_swarm_run_projection import (
    ModelSwarmRunProjection,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "swarm_runs"
CONFLICT_KEY = "run_id"

# Freshness thresholds from contract.yaml freshness_sla
MAX_LAG_SECONDS = 30
DEGRADED_AFTER_SECONDS = 60


class ModelSwarmDispatchEvent(BaseModel):
    """Inbound event from swarm-dispatch-completed or swarm-dispatch-failed."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    run_id: str = Field(..., description="Unique swarm run identifier.")
    correlation_id: str = Field(default="")
    status: str = Field(
        ..., description="Terminal status: succeeded/degraded/failed/timeout."
    )
    task_hash: str = Field(default="")
    subtask_count: int = Field(default=0, ge=0)
    succeeded_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    models_used: tuple[str, ...] = Field(default=())
    machines_used: tuple[str, ...] = Field(default=())
    total_cost_usd: float = Field(default=0.0)
    cloud_equivalent_cost_usd: float = Field(default=0.0)
    savings_usd: float = Field(default=0.0)
    parallelism_speedup_ratio: float = Field(default=1.0)
    decomposition_latency_ms: int = Field(default=0, ge=0)
    dispatch_wall_latency_ms: int = Field(default=0, ge=0)
    aggregation_latency_ms: int = Field(default=0, ge=0)
    total_latency_ms: int = Field(default=0, ge=0)
    endpoint_registry_hash: str = Field(default="")
    registry_schema_version: str = Field(default="")
    emitted_at: str | None = Field(
        default=None, description="ISO 8601 event emission timestamp."
    )
    source_topic: str = Field(default="")
    source_partition: int = Field(default=0, ge=0)
    source_offset: int = Field(default=0, ge=0)
    event_id: str = Field(default="")


class ModelProjectionAppliedEvent(BaseModel):
    """Outbound onex.evt.omnimarket.projection-swarm-applied.v1 payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    rows_upserted: int
    table: str = TABLE
    projection: dict[str, object]
    freshness: dict[str, object]
    applied_at: str


def _compute_freshness(
    emitted_at: str | None,
    now: datetime,
    source_topic: str,
    source_partition: int,
    source_offset: int,
    event_id: str,
) -> ModelProjectionFreshness:
    """Compute freshness state based on event timestamp vs wall clock."""
    observed_at = now.isoformat()

    if emitted_at is None:
        return ModelProjectionFreshness(
            freshness_state=EnumFreshnessState.DEGRADED,
            degraded_reason="emitted_at missing from source event",
            source_topic=source_topic,
            source_partition=source_partition,
            source_offset=source_offset,
            source_event_id=event_id,
            observed_at=observed_at,
        )

    try:
        event_ts = datetime.fromisoformat(emitted_at.replace("Z", "+00:00"))
    except ValueError:
        return ModelProjectionFreshness(
            freshness_state=EnumFreshnessState.DEGRADED,
            degraded_reason=f"unparseable emitted_at: {emitted_at!r}",
            source_topic=source_topic,
            source_partition=source_partition,
            source_offset=source_offset,
            source_event_id=event_id,
            observed_at=observed_at,
        )

    lag_seconds = (now - event_ts.astimezone(UTC)).total_seconds()

    if lag_seconds <= MAX_LAG_SECONDS:
        state = EnumFreshnessState.FRESH
        reason = ""
    elif lag_seconds <= DEGRADED_AFTER_SECONDS:
        state = EnumFreshnessState.STALE
        reason = f"lag {lag_seconds:.1f}s exceeds max_lag_seconds={MAX_LAG_SECONDS}"
    else:
        state = EnumFreshnessState.DEGRADED
        reason = f"lag {lag_seconds:.1f}s exceeds degraded_after_seconds={DEGRADED_AFTER_SECONDS}"

    return ModelProjectionFreshness(
        freshness_state=state,
        degraded_reason=reason,
        source_topic=source_topic,
        source_partition=source_partition,
        source_offset=source_offset,
        source_event_id=event_id,
        observed_at=observed_at,
    )


class HandlerProjectionSwarm:
    """Project swarm dispatch terminal events into the swarm_runs table."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Expects a DatabaseAdapter at input_data['_db'].
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event = ModelSwarmDispatchEvent(**input_data)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelSwarmDispatchEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionAppliedEvent:
        """UPSERT a single swarm dispatch event and emit the applied event."""
        now = datetime.now(tz=UTC)

        try:
            status = EnumSwarmRunStatus(event.status)
        except ValueError:
            status = EnumSwarmRunStatus.FAILED

        projection = ModelSwarmRunProjection(
            run_id=event.run_id,
            correlation_id=event.correlation_id,
            status=status,
            task_hash=event.task_hash,
            subtask_count=event.subtask_count,
            succeeded_count=event.succeeded_count,
            failed_count=event.failed_count,
            skipped_count=event.skipped_count,
            models_used=event.models_used,
            machines_used=event.machines_used,
            total_cost_usd=event.total_cost_usd,
            cloud_equivalent_cost_usd=event.cloud_equivalent_cost_usd,
            savings_usd=event.savings_usd,
            parallelism_speedup_ratio=event.parallelism_speedup_ratio,
            decomposition_latency_ms=event.decomposition_latency_ms,
            dispatch_wall_latency_ms=event.dispatch_wall_latency_ms,
            aggregation_latency_ms=event.aggregation_latency_ms,
            total_latency_ms=event.total_latency_ms,
            endpoint_registry_hash=event.endpoint_registry_hash,
            registry_schema_version=event.registry_schema_version,
            created_at=event.emitted_at or now.isoformat(),
        )

        freshness = _compute_freshness(
            emitted_at=event.emitted_at,
            now=now,
            source_topic=event.source_topic,
            source_partition=event.source_partition,
            source_offset=event.source_offset,
            event_id=event.event_id,
        )

        row = projection.model_dump(mode="json")
        db.upsert(TABLE, CONFLICT_KEY, row)

        return ModelProjectionAppliedEvent(
            run_id=event.run_id,
            rows_upserted=1,
            projection=row,
            freshness=freshness.model_dump(mode="json"),
            applied_at=now.isoformat(),
        )

    def project_batch(
        self,
        events: list[ModelSwarmDispatchEvent],
        db: DatabaseAdapter,
    ) -> list[ModelProjectionAppliedEvent]:
        """UPSERT a batch of swarm dispatch events."""
        return [self.project(event, db) for event in events]


__all__: list[str] = [
    "HandlerProjectionSwarm",
    "ModelProjectionAppliedEvent",
    "ModelSwarmDispatchEvent",
]
