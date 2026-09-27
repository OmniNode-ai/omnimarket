# SPDX-License-Identifier: MIT
"""Typed source, pure fold and declared bus exposure for OMN-19861."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from pydantic import ValidationError

from omnimarket.events.demo_readiness import (
    EnumDemoDashboardConfiguration,
    EnumDemoRehearsalStatus,
    ModelRehearsalBundle,
)
from omnimarket.nodes.node_demo_drift_detector.handlers.handler_demo_drift_detector import (
    ModelDemoDriftDetectResult,
    ModelDemoDriftReport,
)
from omnimarket.nodes.node_demo_rehearsal.handlers.handler_demo_rehearsal import (
    ModelDemoRehearsalResult,
)
from omnimarket.nodes.node_projection_demo_readiness.handlers.handler_demo_readiness_writer import (
    DemoReadinessProjectionWriter,
)
from omnimarket.nodes.node_projection_demo_readiness.handlers.handler_projection_demo_readiness import (
    HandlerProjectionDemoReadiness,
)
from omnimarket.nodes.node_projection_demo_readiness.models import (
    EnumDemoReadinessStatus,
    ModelDemoReadinessProjectionRequest,
)
from omnimarket.nodes.node_projection_demo_readiness.terminal import (
    DRIFT_TOPIC,
    REHEARSAL_TOPIC,
    parse_demo_terminal,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.error_classification import (
    PoisonEventError,
    ProjectionErrorClass,
    classify_projection_error,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_demo_readiness/contract.yaml"
)
OBSERVED = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _rehearsal(
    *,
    observed_at: datetime = OBSERVED,
    configuration: EnumDemoDashboardConfiguration = EnumDemoDashboardConfiguration.CONFIGURED,
    dry_run: bool = False,
) -> ModelDemoRehearsalResult:
    status = (
        EnumDemoRehearsalStatus.BROKEN
        if configuration is EnumDemoDashboardConfiguration.UNCONFIGURED
        else EnumDemoRehearsalStatus.GREEN
    )
    failures = (
        [{"msg": "DEMO_DASHBOARD_URL unconfigured"}] if status.value == "BROKEN" else []
    )
    return ModelDemoRehearsalResult(
        node_id="demo_rehearsal",
        run_id="rehearsal-1",
        bundle_path="/evidence/rehearsal_bundle.json",
        overall_status=status.value,
        failure_count=len(failures),
        dashboard_configuration=configuration,
        rehearsal_bundle=ModelRehearsalBundle(
            rehearsal_id="rehearsal-1",
            timestamp_utc=observed_at,
            overall_status=status,
            failures=failures,
        ),
        dry_run=dry_run,
    )


def _drift(
    *,
    configuration: EnumDemoDashboardConfiguration = EnumDemoDashboardConfiguration.CONFIGURED,
) -> ModelDemoDriftDetectResult:
    blockers = int(configuration is EnumDemoDashboardConfiguration.UNCONFIGURED)
    findings = (
        [
            {
                "finding_id": "dashboard-unconfigured",
                "dimension": "dashboard",
                "criticality": "demo_blocker",
                "summary": "Dashboard unconfigured",
            }
        ]
        if blockers
        else []
    )
    return ModelDemoDriftDetectResult(
        node_id="demo_drift_detector",
        run_id="drift-1",
        report_path="/evidence/drift_report.json",
        demo_blocker_count=blockers,
        demo_degraded_count=0,
        total_finding_count=blockers,
        dashboard_configuration=configuration,
        drift_report=ModelDemoDriftReport(
            run_id="drift-1",
            detected_at=OBSERVED,
            proof_of_green_rehearsal_id="rehearsal-0",
            findings=findings,
            demo_blocker_count=blockers,
        ),
        dry_run=False,
    )


def _wire(
    result: ModelDemoRehearsalResult | ModelDemoDriftDetectResult,
) -> dict[str, object]:
    return {**result.model_dump(mode="json"), "_envelope_id": str(uuid4())}


def test_rehearsal_unconfigured_is_explicit_non_green() -> None:
    row = parse_demo_terminal(
        REHEARSAL_TOPIC,
        _wire(_rehearsal(configuration=EnumDemoDashboardConfiguration.UNCONFIGURED)),
    )
    assert row.status is EnumDemoReadinessStatus.UNCONFIGURED
    assert row.dashboard_configuration is EnumDemoDashboardConfiguration.UNCONFIGURED
    assert row.failure_count == 1
    assert row.evidence_path == "/evidence/rehearsal_bundle.json"


def test_drift_unconfigured_is_explicit_non_green() -> None:
    row = parse_demo_terminal(
        DRIFT_TOPIC,
        _wire(_drift(configuration=EnumDemoDashboardConfiguration.UNCONFIGURED)),
    )
    assert row.status is EnumDemoReadinessStatus.UNCONFIGURED
    assert row.demo_blocker_count == 1
    assert row.failure_count is None


def test_dry_run_is_not_durable_green() -> None:
    row = parse_demo_terminal(REHEARSAL_TOPIC, _wire(_rehearsal(dry_run=True)))
    assert row.status is EnumDemoReadinessStatus.DRY_RUN
    assert row.evidence_path is None


def test_row_refuses_ambiguous_count_and_evidence_shapes() -> None:
    row = parse_demo_terminal(REHEARSAL_TOPIC, _wire(_rehearsal()))
    with pytest.raises(ValidationError, match="invalid count shape"):
        type(row).model_validate(
            {**row.model_dump(mode="python"), "failure_count": None}
        )
    with pytest.raises(ValidationError, match="requires an evidence path"):
        type(row).model_validate(
            {**row.model_dump(mode="python"), "evidence_path": None}
        )


def test_missing_discriminator_fails_closed() -> None:
    wire = _wire(_rehearsal())
    del wire["dashboard_configuration"]
    with pytest.raises(ValidationError, match="dashboard_configuration"):
        parse_demo_terminal(REHEARSAL_TOPIC, wire)


def test_topic_node_mismatch_and_missing_envelope_fail_closed() -> None:
    wire = _wire(_rehearsal())
    wire["node_id"] = "demo_drift_detector"
    with pytest.raises((ValidationError, PoisonEventError)):
        parse_demo_terminal(REHEARSAL_TOPIC, wire)
    wire = _wire(_rehearsal())
    del wire["_envelope_id"]
    with pytest.raises(PoisonEventError, match="source envelope ID"):
        parse_demo_terminal(REHEARSAL_TOPIC, wire)


def test_semantic_refusal_is_quarantined_not_retried_forever() -> None:
    wire = _wire(_rehearsal())
    wire["run_id"] = "different-run"
    with pytest.raises(PoisonEventError) as refusal:
        parse_demo_terminal(REHEARSAL_TOPIC, wire)
    assert classify_projection_error(refusal.value) is ProjectionErrorClass.POISON


def test_real_envelope_shape_unwraps_to_typed_row() -> None:
    import json

    result = _rehearsal()
    envelope = {
        "envelope_id": str(uuid4()),
        "payload": result.model_dump(mode="json"),
    }
    unwrapped = unwrap_envelope(json.dumps(envelope).encode())
    assert unwrapped is not None
    row = parse_demo_terminal(REHEARSAL_TOPIC, unwrapped)
    assert row.run_id == "rehearsal-1"


def test_pure_fold_is_append_order_invariant_and_idempotent() -> None:
    handler = HandlerProjectionDemoReadiness()
    older = parse_demo_terminal(REHEARSAL_TOPIC, _wire(_rehearsal()))
    newer = parse_demo_terminal(
        REHEARSAL_TOPIC,
        _wire(_rehearsal(observed_at=OBSERVED + timedelta(minutes=1))),
    )
    accepted = handler.handle(
        ModelDemoReadinessProjectionRequest(previous_row=older, observation=newer)
    )
    assert accepted.applied
    stale = handler.handle(
        ModelDemoReadinessProjectionRequest(
            previous_row=accepted.row, observation=older
        )
    )
    assert not stale.applied
    assert stale.row == newer
    replay = handler.handle(
        ModelDemoReadinessProjectionRequest(
            previous_row=accepted.row, observation=newer
        )
    )
    assert not replay.applied


def test_equal_observation_time_uses_stable_event_id_tiebreaker() -> None:
    handler = HandlerProjectionDemoReadiness()
    lower_wire = _wire(_rehearsal())
    upper_wire = _wire(_rehearsal())
    lower_wire["_envelope_id"] = str(UUID(int=1))
    upper_wire["_envelope_id"] = str(UUID(int=2))
    lower = parse_demo_terminal(REHEARSAL_TOPIC, lower_wire)
    upper = parse_demo_terminal(REHEARSAL_TOPIC, upper_wire)
    forward = handler.handle(
        ModelDemoReadinessProjectionRequest(previous_row=lower, observation=upper)
    )
    reverse = handler.handle(
        ModelDemoReadinessProjectionRequest(previous_row=upper, observation=lower)
    )
    assert forward.applied
    assert forward.row == upper
    assert not reverse.applied
    assert reverse.row == upper


def test_contract_exposes_bus_backed_on_demand_two_row_read_model() -> None:
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert (
        raw["terminal_event"]
        == "onex.evt.omnimarket.projection-demo-readiness-applied.v1"
    )
    assert raw["event_bus"]["publish_topics"] == [raw["terminal_event"]]
    exposure = load_projection_exposures_from_contract(raw, raw["name"], CONTRACT)[0]
    assert exposure.topic == "onex.snapshot.projection.demo-readiness.v1"
    assert exposure.bus_backed
    assert exposure.key_grain == "mutable"
    assert exposure.key_columns == ("node_id",)
    assert exposure.expected_event_interval_seconds is None
    assert exposure.limit == 2
    assert DemoReadinessProjectionWriter.onex_runtime_inprocess_dispatch


def test_only_writer_is_runtime_routed() -> None:
    from omnibase_infra.runtime.auto_wiring.discovery import _parse_contract

    parsed = _parse_contract(
        contract_path=CONTRACT,
        entry_point_name="node_projection_demo_readiness",
        package_name="omnimarket",
        package_version="test",
    )
    assert parsed.handler_routing is not None
    assert [entry.handler.name for entry in parsed.handler_routing.handlers] == [
        "DemoReadinessProjectionWriter"
    ]


@pytest.mark.asyncio
async def test_duplicate_redelivery_retries_snapshot_after_db_commit() -> None:
    """A failed post-commit publish must not strand the accepted DB row."""
    wire = _wire(_rehearsal())
    observation = parse_demo_terminal(REHEARSAL_TOPIC, wire)
    prior = observation.model_copy(update={"projection_cursor": 1})
    writer = DemoReadinessProjectionWriter()
    writer._db = AsyncMock()  # type: ignore[assignment]
    writer._db.execute.return_value = [prior.model_dump(mode="python")]
    writer.publish_snapshot_delta = AsyncMock(return_value=True)  # type: ignore[method-assign]
    result = await writer._project_event(
        REHEARSAL_TOPIC,
        wire,
        MessageMeta(partition=0, offset=12, fallback_id="", topic=REHEARSAL_TOPIC),
    )
    assert result is None
    assert writer._db.execute.await_count == 1
    writer.publish_snapshot_delta.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stale_event_never_republishes_snapshot() -> None:
    older = _wire(_rehearsal())
    newer = parse_demo_terminal(
        REHEARSAL_TOPIC,
        _wire(_rehearsal(observed_at=OBSERVED + timedelta(minutes=1))),
    )
    prior = newer.model_copy(update={"projection_cursor": 2})
    writer = DemoReadinessProjectionWriter()
    writer._db = AsyncMock()  # type: ignore[assignment]
    writer._db.execute.return_value = [prior.model_dump(mode="python")]
    writer.publish_snapshot_delta = AsyncMock(return_value=True)  # type: ignore[method-assign]
    result = await writer._project_event(
        REHEARSAL_TOPIC,
        older,
        MessageMeta(partition=0, offset=11, fallback_id="", topic=REHEARSAL_TOPIC),
    )
    assert result is None
    writer.publish_snapshot_delta.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_reused_envelope_id_with_changed_payload_is_poison() -> None:
    original = _wire(_rehearsal())
    prior = parse_demo_terminal(REHEARSAL_TOPIC, original).model_copy(
        update={"projection_cursor": 1}
    )
    changed = _wire(_rehearsal(observed_at=OBSERVED + timedelta(minutes=1)))
    changed["_envelope_id"] = original["_envelope_id"]
    writer = DemoReadinessProjectionWriter()
    writer._db = AsyncMock()  # type: ignore[assignment]
    writer._db.execute.return_value = [prior.model_dump(mode="python")]
    with pytest.raises(PoisonEventError, match="reused an event identity"):
        await writer._project_event(
            REHEARSAL_TOPIC,
            changed,
            MessageMeta(partition=0, offset=13, fallback_id="", topic=REHEARSAL_TOPIC),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_node", "topic", "result_factory"),
    [
        ("node_demo_rehearsal", REHEARSAL_TOPIC, _rehearsal),
        ("node_demo_drift_detector", DRIFT_TOPIC, _drift),
    ],
)
async def test_declared_runtime_terminal_publish_round_trips_to_projector(
    source_node: str,
    topic: str,
    result_factory: Callable[[], ModelDemoRehearsalResult | ModelDemoDriftDetectResult],
) -> None:
    """Use the actual producer contract and generic result applier, not a mock topic."""
    from omnibase_infra.enums import EnumDispatchStatus
    from omnibase_infra.models.dispatch.model_dispatch_result import ModelDispatchResult
    from omnibase_infra.runtime.service_dispatch_result_applier import (
        build_contract_result_applier,
    )

    contract_path = CONTRACT.parents[1] / source_node / "contract.yaml"
    result = result_factory()
    bus = AsyncMock(spec=ProtocolEventBusPublisher)
    applier = build_contract_result_applier(
        event_bus=bus,
        contract_path=contract_path,
        publish_topics=[topic],
        terminal_event=topic,
    )
    correlation_id = uuid4()
    await applier.apply(
        ModelDispatchResult(
            status=EnumDispatchStatus.SUCCESS,
            topic="onex.cmd.omnimarket.demo-start.v1",
            started_at=OBSERVED,
            correlation_id=correlation_id,
            dispatcher_id="omn19861-terminal-proof",
            output_events=[result],
        ),
        correlation_id=correlation_id,
    )
    bus.publish_envelope.assert_awaited_once()
    published = bus.publish_envelope.await_args.kwargs
    assert published["topic"] == topic
    envelope = published["envelope"].model_dump(mode="json")
    import json

    unwrapped = unwrap_envelope(json.dumps(envelope).encode())
    assert unwrapped is not None
    row = parse_demo_terminal(topic, unwrapped)
    assert row.dashboard_configuration is EnumDemoDashboardConfiguration.CONFIGURED
    assert row.status is EnumDemoReadinessStatus.GREEN
