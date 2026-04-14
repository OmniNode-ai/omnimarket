"""NodeDataFlowSweep — End-to-end data flow verification.

Verifies the complete pipeline for each data flow: producer status,
consumer lag, DB table row counts, and field mapping correctness.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EnumProducerStatus(StrEnum):
    """Producer topic status."""

    ACTIVE = "ACTIVE"
    EMPTY = "EMPTY"
    MISSING = "MISSING"


class EnumFlowStatus(StrEnum):
    """End-to-end flow status."""

    FLOWING = "FLOWING"
    STALE = "STALE"
    LAGGING = "LAGGING"
    EMPTY_TABLE = "EMPTY_TABLE"
    MISSING_TABLE = "MISSING_TABLE"
    PRODUCER_DOWN = "PRODUCER_DOWN"
    TOPIC_STALE = "TOPIC_STALE"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelFlowInput(BaseModel):
    """Input for a single data flow to verify."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    handler_name: str
    table_name: str
    dashboard_route: str | None = None
    producer_status: EnumProducerStatus = EnumProducerStatus.ACTIVE
    consumer_lag: int = 0
    table_row_count: int = 0
    table_has_recent_data: bool = False
    field_mapping_valid: bool = True
    newest_message_age_seconds: float | None = None
    stale_threshold_seconds: float = 1800.0  # 30 min default (OMN-8691)


class ModelFlowResult(BaseModel):
    """Verification result for a single data flow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    handler_name: str
    table_name: str
    producer_status: EnumProducerStatus
    flow_status: EnumFlowStatus
    consumer_lag: int
    table_row_count: int
    field_mapping_valid: bool
    message: str


class DataFlowSweepRequest(BaseModel):
    """Input for the data flow sweep handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flows: list[ModelFlowInput] = Field(default_factory=list)
    dry_run: bool = False


class DataFlowSweepResult(BaseModel):
    """Output of the data flow sweep handler."""

    model_config = ConfigDict(extra="forbid")

    flow_results: list[ModelFlowResult] = Field(default_factory=list)
    flows_checked: int = 0
    healthy: int = 0
    broken: int = 0
    status: str = "healthy"  # healthy | issues_found | error
    dry_run: bool = False

    @property
    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for fr in self.flow_results:
            counts[fr.flow_status] = counts.get(fr.flow_status, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeDataFlowSweep:
    """Verify end-to-end data flows from Kafka to DB.

    Pure compute handler — operates on pre-collected flow metadata.
    """

    def handle(self, request: DataFlowSweepRequest) -> DataFlowSweepResult:
        """Execute the data flow sweep across flow inputs."""
        results: list[ModelFlowResult] = []
        healthy_count = 0
        broken_count = 0

        for flow in request.flows:
            result = self._verify_flow(flow)
            results.append(result)
            if result.flow_status == EnumFlowStatus.FLOWING:
                healthy_count += 1
            else:
                broken_count += 1

        status = "healthy" if broken_count == 0 else "issues_found"

        return DataFlowSweepResult(
            flow_results=results,
            flows_checked=len(request.flows),
            healthy=healthy_count,
            broken=broken_count,
            status=status,
            dry_run=request.dry_run,
        )

    def _verify_flow(self, flow: ModelFlowInput) -> ModelFlowResult:
        """Verify a single data flow end-to-end."""
        if flow.producer_status == EnumProducerStatus.MISSING:
            return ModelFlowResult(
                topic=flow.topic,
                handler_name=flow.handler_name,
                table_name=flow.table_name,
                producer_status=flow.producer_status,
                flow_status=EnumFlowStatus.PRODUCER_DOWN,
                consumer_lag=flow.consumer_lag,
                table_row_count=flow.table_row_count,
                field_mapping_valid=flow.field_mapping_valid,
                message=f"Topic {flow.topic} does not exist",
            )

        if flow.producer_status == EnumProducerStatus.EMPTY:
            return ModelFlowResult(
                topic=flow.topic,
                handler_name=flow.handler_name,
                table_name=flow.table_name,
                producer_status=flow.producer_status,
                flow_status=EnumFlowStatus.PRODUCER_DOWN,
                consumer_lag=flow.consumer_lag,
                table_row_count=flow.table_row_count,
                field_mapping_valid=flow.field_mapping_valid,
                message=f"Topic {flow.topic} exists but has 0 messages",
            )

        if flow.table_row_count == 0:
            return ModelFlowResult(
                topic=flow.topic,
                handler_name=flow.handler_name,
                table_name=flow.table_name,
                producer_status=flow.producer_status,
                flow_status=EnumFlowStatus.EMPTY_TABLE,
                consumer_lag=flow.consumer_lag,
                table_row_count=0,
                field_mapping_valid=flow.field_mapping_valid,
                message=f"Messages in topic but 0 rows in {flow.table_name}",
            )

        if flow.consumer_lag > 0:
            return ModelFlowResult(
                topic=flow.topic,
                handler_name=flow.handler_name,
                table_name=flow.table_name,
                producer_status=flow.producer_status,
                flow_status=EnumFlowStatus.LAGGING,
                consumer_lag=flow.consumer_lag,
                table_row_count=flow.table_row_count,
                field_mapping_valid=flow.field_mapping_valid,
                message=f"Consumer lag: {flow.consumer_lag}",
            )

        # OMN-8691: Check topic-level message staleness before table staleness.
        # This catches cases where the topic producer stopped (e.g. heartbeat loop
        # never started) even when the DB table has existing rows.
        if (
            flow.newest_message_age_seconds is not None
            and flow.newest_message_age_seconds > flow.stale_threshold_seconds
        ):
            age_min = int(flow.newest_message_age_seconds / 60)
            threshold_min = int(flow.stale_threshold_seconds / 60)
            return ModelFlowResult(
                topic=flow.topic,
                handler_name=flow.handler_name,
                table_name=flow.table_name,
                producer_status=flow.producer_status,
                flow_status=EnumFlowStatus.TOPIC_STALE,
                consumer_lag=flow.consumer_lag,
                table_row_count=flow.table_row_count,
                field_mapping_valid=flow.field_mapping_valid,
                message=(
                    f"Newest message in {flow.topic} is {age_min}min old "
                    f"(threshold: {threshold_min}min)"
                ),
            )

        if not flow.table_has_recent_data:
            return ModelFlowResult(
                topic=flow.topic,
                handler_name=flow.handler_name,
                table_name=flow.table_name,
                producer_status=flow.producer_status,
                flow_status=EnumFlowStatus.STALE,
                consumer_lag=flow.consumer_lag,
                table_row_count=flow.table_row_count,
                field_mapping_valid=flow.field_mapping_valid,
                message="Data older than 24h",
            )

        return ModelFlowResult(
            topic=flow.topic,
            handler_name=flow.handler_name,
            table_name=flow.table_name,
            producer_status=flow.producer_status,
            flow_status=EnumFlowStatus.FLOWING,
            consumer_lag=0,
            table_row_count=flow.table_row_count,
            field_mapping_valid=flow.field_mapping_valid,
            message="Flow healthy",
        )
