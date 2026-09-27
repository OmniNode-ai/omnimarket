# SPDX-License-Identifier: MIT
"""Validate each demo terminal against its producer's exact result model."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from omnimarket.events.demo_readiness import EnumDemoDashboardConfiguration
from omnimarket.events.topics import (
    DEMO_DRIFT_DETECTED_TOPIC_V1,
    DEMO_REHEARSED_TOPIC_V1,
)
from omnimarket.nodes.node_demo_drift_detector.handlers.handler_demo_drift_detector import (
    ModelDemoDriftDetectResult,
)
from omnimarket.nodes.node_demo_rehearsal.handlers.handler_demo_rehearsal import (
    ModelDemoRehearsalResult,
)
from omnimarket.nodes.node_projection_demo_readiness.models import (
    EnumDemoReadinessStatus,
    ModelDemoReadinessRow,
)
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.error_classification import PoisonEventError

REHEARSAL_TOPIC = DEMO_REHEARSED_TOPIC_V1
DRIFT_TOPIC = DEMO_DRIFT_DETECTED_TOPIC_V1
TERMINAL_TOPICS = frozenset((REHEARSAL_TOPIC, DRIFT_TOPIC))


def _source_event_id(data: dict[str, Any]) -> UUID:
    raw: object = data.get("_envelope_id")
    if raw is None:
        envelope = data.get("_envelope")
        if isinstance(envelope, dict):
            raw = envelope.get("envelope_id")
    if raw is None:
        raise PoisonEventError("demo terminal has no source envelope ID")
    try:
        return UUID(str(raw))
    except ValueError as exc:
        raise PoisonEventError(
            "demo terminal has malformed source envelope ID"
        ) from exc


def _status(
    *,
    configuration: EnumDemoDashboardConfiguration,
    dry_run: bool,
    source_status: EnumDemoReadinessStatus,
) -> EnumDemoReadinessStatus:
    if configuration is EnumDemoDashboardConfiguration.UNCONFIGURED:
        return EnumDemoReadinessStatus.UNCONFIGURED
    if dry_run:
        return EnumDemoReadinessStatus.DRY_RUN
    return source_status


def parse_demo_terminal(topic: str, data: dict[str, Any]) -> ModelDemoReadinessRow:
    """Reject malformed, legacy or topic-mismatched terminals; never parse prose."""
    event_id = _source_event_id(data)
    payload = strip_runner_injected_keys(data)
    payload.pop("_db", None)
    if topic == REHEARSAL_TOPIC:
        result = ModelDemoRehearsalResult.model_validate(payload)
        if result.node_id != "demo_rehearsal":
            raise PoisonEventError("rehearsal terminal node_id does not match topic")
        bundle = result.rehearsal_bundle
        if bundle.rehearsal_id != result.run_id:
            raise PoisonEventError("rehearsal run IDs disagree")
        if bundle.overall_status.value != result.overall_status:
            raise PoisonEventError("rehearsal status disagrees with evidence bundle")
        if len(bundle.failures) != result.failure_count:
            raise PoisonEventError(
                "rehearsal failure count disagrees with evidence bundle"
            )
        try:
            source_status = EnumDemoReadinessStatus(result.overall_status)
        except ValueError as exc:
            raise PoisonEventError("rehearsal has unknown source status") from exc
        if (
            result.dashboard_configuration
            is EnumDemoDashboardConfiguration.UNCONFIGURED
            and source_status is not EnumDemoReadinessStatus.BROKEN
        ):
            raise PoisonEventError(
                "unconfigured rehearsal cannot claim non-broken source status"
            )
        return ModelDemoReadinessRow(
            node_id=result.node_id,
            run_id=result.run_id,
            status=_status(
                configuration=result.dashboard_configuration,
                dry_run=result.dry_run,
                source_status=source_status,
            ),
            dashboard_configuration=result.dashboard_configuration,
            observed_at=bundle.timestamp_utc,
            source_event_id=event_id,
            evidence_path=None if result.dry_run else result.bundle_path,
            dry_run=result.dry_run,
            failure_count=result.failure_count,
            demo_blocker_count=None,
            demo_degraded_count=None,
            total_finding_count=None,
        )
    if topic == DRIFT_TOPIC:
        drift = ModelDemoDriftDetectResult.model_validate(payload)
        if drift.node_id != "demo_drift_detector":
            raise PoisonEventError("drift terminal node_id does not match topic")
        report = drift.drift_report
        if report.run_id != drift.run_id:
            raise PoisonEventError("drift run IDs disagree")
        if (
            report.demo_blocker_count != drift.demo_blocker_count
            or report.demo_degraded_count != drift.demo_degraded_count
            or len(report.findings) != drift.total_finding_count
        ):
            raise PoisonEventError("drift counts disagree with evidence report")
        if (
            drift.dashboard_configuration is EnumDemoDashboardConfiguration.UNCONFIGURED
            and drift.demo_blocker_count == 0
        ):
            raise PoisonEventError("unconfigured drift must carry a demo blocker")
        source_status = (
            EnumDemoReadinessStatus.BROKEN
            if drift.demo_blocker_count > 0
            else EnumDemoReadinessStatus.DEGRADED
            if drift.demo_degraded_count > 0
            else EnumDemoReadinessStatus.GREEN
        )
        return ModelDemoReadinessRow(
            node_id=drift.node_id,
            run_id=drift.run_id,
            status=_status(
                configuration=drift.dashboard_configuration,
                dry_run=drift.dry_run,
                source_status=source_status,
            ),
            dashboard_configuration=drift.dashboard_configuration,
            observed_at=report.detected_at,
            source_event_id=event_id,
            evidence_path=None if drift.dry_run else drift.report_path,
            dry_run=drift.dry_run,
            failure_count=None,
            demo_blocker_count=drift.demo_blocker_count,
            demo_degraded_count=drift.demo_degraded_count,
            total_finding_count=drift.total_finding_count,
        )
    raise PoisonEventError(f"undeclared demo terminal topic: {topic}")
