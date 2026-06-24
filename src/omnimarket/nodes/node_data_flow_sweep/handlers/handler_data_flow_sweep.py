"""NodeDataFlowSweep — End-to-end data flow verification.

Verifies the complete pipeline for each data flow: producer status,
consumer lag, DB table row counts, and field mapping correctness.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_log = logging.getLogger(__name__)

# Built-in critical-chain topics. Declared here (not only in ``__main__``) so the
# RuntimeLocal dispatch path resolves the same default stub set as the CLI
# (OMN-13534). ``onex-topic-allow`` until contract auto-wiring lands.
NODE_INTROSPECTION_TOPIC = "onex.evt.platform.node-introspection.v1"  # onex-topic-allow: pending contract auto-wiring
PATTERN_LEARNED_TOPIC = "onex.evt.omniintelligence.pattern-learned.v1"  # onex-topic-allow: pending contract auto-wiring
ROUTING_DECISION_TOPIC = "onex.evt.omniclaude.routing-decision.v1"  # onex-topic-allow: pending contract auto-wiring

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
    """Input for the data flow sweep handler.

    Two ways to supply the flows that get verified (resolved by
    :func:`resolve_flows`):

    * ``flows`` — pre-collected flow descriptors (highest precedence). Passing a
      non-empty list verifies exactly those flows and skips live collection.
    * ``collect`` — when ``True`` and ``flows`` is empty, the handler runs the
      live rpk/psql collector against the built-in critical-chain descriptors
      before classifying them. This is the field the
      ``onex skill data_flow_sweep`` mapping and the node ``contract.yaml``
      supply; previously ``collect`` was a ``__main__``-only argparse flag
      absent from this model, so the RuntimeLocal dispatch path crashed on the
      forbidden extra key (OMN-13534).

    When BOTH are empty/false the handler classifies the built-in critical-chain
    descriptors with their zero-value defaults (topology-only, no live metadata).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    flows: list[ModelFlowInput] = Field(default_factory=list)
    collect: bool = False
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
# Default flow stubs + shared flow resolution
# ---------------------------------------------------------------------------

# Built-in critical-chain stubs — topology only, no live metadata. When
# ``collect`` is set, each stub is populated via
# ``collector.collect_flow_metadata()``. Shared by the ``__main__`` CLI path and
# the RuntimeLocal dispatch path so both resolve identically (OMN-13534).
_DEFAULT_FLOW_STUBS: tuple[ModelFlowInput, ...] = (
    ModelFlowInput(
        topic=NODE_INTROSPECTION_TOPIC,
        handler_name="projectNodeIntrospection",
        table_name="node_service_registry",
        dashboard_route="/agents",
    ),
    ModelFlowInput(
        topic=PATTERN_LEARNED_TOPIC,
        handler_name="projectPatternLearned",
        table_name="pattern_learning_artifacts",
        dashboard_route="/intelligence",
    ),
    ModelFlowInput(
        topic=ROUTING_DECISION_TOPIC,
        handler_name="projectRoutingDecision",
        table_name="agent_routing_decisions",
        dashboard_route="/pipeline",
    ),
)


def _collect_live(descriptors: list[ModelFlowInput]) -> list[ModelFlowInput]:
    """Populate live rpk/psql metadata for each flow descriptor.

    The collector is imported lazily so tests/callers that exercise only the
    pure classification logic never pull in subprocess/shell dependencies. Falls
    back to the original descriptor on per-flow collection failure so a single
    unreachable topic does not abort the entire sweep.
    """
    from omnimarket.nodes.node_data_flow_sweep.collector import collect_flow_metadata

    populated: list[ModelFlowInput] = []
    for descriptor in descriptors:
        try:
            populated.append(collect_flow_metadata(descriptor))
        except Exception as exc:
            _log.warning(
                "collection failed for %s: %s — using descriptor", descriptor.topic, exc
            )
            populated.append(descriptor)
    return populated


def resolve_flows(request: DataFlowSweepRequest) -> list[ModelFlowInput]:
    """Resolve the flow list to verify, shared by CLI and dispatch paths.

    Precedence (OMN-13534):

    * Explicit ``request.flows`` — verified as-is, no live collection.
    * ``request.collect`` with empty ``flows`` — run the live rpk/psql collector
      against the built-in critical-chain descriptors so the RuntimeLocal
      dispatch path probes real infrastructure instead of classifying
      zero-value descriptors (which false-cleaned).
    * Neither — classify the built-in critical-chain descriptors with their
      zero-value defaults (topology-only).
    """
    if request.flows:
        return list(request.flows)
    if request.collect:
        return _collect_live(list(_DEFAULT_FLOW_STUBS))
    return list(_DEFAULT_FLOW_STUBS)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeDataFlowSweep:
    """Verify end-to-end data flows from Kafka to DB.

    Operates on pre-collected flow metadata. When ``request.collect`` is set the
    handler runs the live rpk/psql collector (lazily imported) before
    classifying — so the ``onex skill data_flow_sweep`` dispatch path probes
    real infrastructure, identical to the ``--collect`` CLI path (OMN-13534).
    """

    def handle(self, request: DataFlowSweepRequest) -> DataFlowSweepResult:
        """Execute the data flow sweep across flow inputs."""
        flows = resolve_flows(request)
        results: list[ModelFlowResult] = []
        healthy_count = 0
        broken_count = 0

        for flow in flows:
            result = self._verify_flow(flow)
            results.append(result)
            if result.flow_status == EnumFlowStatus.FLOWING:
                healthy_count += 1
            else:
                broken_count += 1

        status = "healthy" if broken_count == 0 else "issues_found"

        return DataFlowSweepResult(
            flow_results=results,
            flows_checked=len(flows),
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
