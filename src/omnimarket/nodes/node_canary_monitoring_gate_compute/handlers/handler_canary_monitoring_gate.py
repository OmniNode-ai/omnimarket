# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCanaryMonitoringGate — classify monitoring signals against thresholds (OMN-14735, B10).

Pure COMPUTE. Wires each observed signal reading (auth/TLS/broker/lag/RDS —
the five domains named in the managed-staging execution plan's A6/B10 tasks)
to its threshold spec and classifies the result as ``PASS``/``WARN``/
``ABORT``/``UNRESOLVED``.

**This is a scaffold, not a live monitoring integration.** The numeric
``warn_threshold``/``abort_threshold`` values are a contractor deliverable
(A6, ``docs/plans/2026-07-17-managed-staging-verified-state-and-task-split.md``)
that has not been supplied as of this scaffold. Every threshold spec built
from real inputs today is therefore unresolved by construction, and every
reading classifies as ``UNRESOLVED`` — never a fabricated PASS/WARN/ABORT.
Once A6 lands, the same threshold specs gain real numbers and a real
``source`` citation, and the identical logic below starts producing real
verdicts with no code change required.

No I/O, no clock, no randomness, no live bus/AWS/Kubernetes dependency.
"""

from __future__ import annotations

import logging
from typing import Literal

from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_monitoring_gate_input import (
    ModelMonitoringGateInput,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_monitoring_gate_result import (
    DEFAULT_ABORT_ACTION,
    ModelMonitoringGateResult,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_reading import (
    SignalName,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_verdict import (
    ModelSignalVerdict,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_threshold_spec import (
    UNRESOLVED_SOURCE,
    ModelThresholdSpec,
)

logger = logging.getLogger(__name__)

_NO_SPEC_REASON = "no threshold spec supplied for this signal domain"
_UNRESOLVED_REASON = "threshold spec is unresolved — A6 numeric input pending"
_RDS_MAINTENANCE_REASON = (
    "RDS abort breach downgraded to WARN: single-AZ instance is in a "
    "declared maintenance window (not a canary failure)"
)


def _breach(value: float, threshold: float, comparison: Literal["gte", "lte"]) -> bool:
    if comparison == "gte":
        return value >= threshold
    return value <= threshold


def _classify_reading(
    *,
    signal_name: SignalName,
    value: float,
    spec: ModelThresholdSpec | None,
    rds_single_az_maintenance_window: bool,
) -> ModelSignalVerdict:
    if spec is None:
        return ModelSignalVerdict(
            signal_name=signal_name,
            status="UNRESOLVED",
            value=value,
            threshold_source=UNRESOLVED_SOURCE,
            reason=_NO_SPEC_REASON,
        )

    if not spec.is_resolved:
        return ModelSignalVerdict(
            signal_name=signal_name,
            status="UNRESOLVED",
            value=value,
            threshold_source=spec.source,
            reason=_UNRESOLVED_REASON,
        )

    # is_resolved guarantees both are non-None; assert for mypy narrowing.
    assert spec.abort_threshold is not None
    assert spec.warn_threshold is not None

    if _breach(value, spec.abort_threshold, spec.comparison):
        if signal_name == "rds" and rds_single_az_maintenance_window:
            return ModelSignalVerdict(
                signal_name=signal_name,
                status="WARN",
                value=value,
                threshold_source=spec.source,
                reason=_RDS_MAINTENANCE_REASON,
            )
        return ModelSignalVerdict(
            signal_name=signal_name,
            status="ABORT",
            value=value,
            threshold_source=spec.source,
            reason=(
                f"value {value} breaches abort threshold "
                f"{spec.abort_threshold} ({spec.comparison})"
            ),
        )

    if _breach(value, spec.warn_threshold, spec.comparison):
        return ModelSignalVerdict(
            signal_name=signal_name,
            status="WARN",
            value=value,
            threshold_source=spec.source,
            reason=(
                f"value {value} breaches warn threshold "
                f"{spec.warn_threshold} ({spec.comparison})"
            ),
        )

    return ModelSignalVerdict(
        signal_name=signal_name,
        status="PASS",
        value=value,
        threshold_source=spec.source,
        reason="within thresholds",
    )


def evaluate_monitoring_gate(
    request: ModelMonitoringGateInput,
) -> ModelMonitoringGateResult:
    """Classify each reading against its threshold spec. PURE.

    Aggregation rule (checked in this priority order):
        1. any verdict is ``ABORT`` -> overall ``ABORT``
        2. else any verdict is ``UNRESOLVED`` -> overall ``BLOCKED_PENDING_A6``
        3. else any verdict is ``WARN`` -> overall ``WARN``
        4. else -> overall ``PASS``

    ``BLOCKED_PENDING_A6`` outranks ``WARN``/``PASS`` because an unresolved
    threshold means the gate cannot make an honest safety claim for that
    signal — it must not be reported as if the signal were healthy.
    """
    thresholds_by_signal = {spec.signal_name: spec for spec in request.thresholds}

    verdicts = tuple(
        _classify_reading(
            signal_name=reading.signal_name,
            value=reading.value,
            spec=thresholds_by_signal.get(reading.signal_name),
            rds_single_az_maintenance_window=request.rds_single_az_maintenance_window,
        )
        for reading in request.readings
    )

    unresolved_signals = tuple(
        verdict.signal_name for verdict in verdicts if verdict.status == "UNRESOLVED"
    )

    if any(verdict.status == "ABORT" for verdict in verdicts):
        overall_status: Literal["PASS", "WARN", "ABORT", "BLOCKED_PENDING_A6"] = "ABORT"
        abort_action: str | None = DEFAULT_ABORT_ACTION
    elif unresolved_signals:
        overall_status = "BLOCKED_PENDING_A6"
        abort_action = None
    elif any(verdict.status == "WARN" for verdict in verdicts):
        overall_status = "WARN"
        abort_action = None
    else:
        overall_status = "PASS"
        abort_action = None

    return ModelMonitoringGateResult(
        correlation_id=request.correlation_id,
        verdicts=verdicts,
        overall_status=overall_status,
        abort_action=abort_action,
        unresolved_signals=unresolved_signals,
    )


class HandlerCanaryMonitoringGate:
    """Pure COMPUTE handler: monitoring-signal-to-threshold gate classification."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        request: ModelMonitoringGateInput,
    ) -> ModelMonitoringGateResult:
        result = evaluate_monitoring_gate(request)
        logger.info(
            "canary_monitoring_gate: %d reading(s), overall=%s, unresolved=%s",
            len(request.readings),
            result.overall_status,
            result.unresolved_signals,
        )
        return result


__all__ = ["HandlerCanaryMonitoringGate", "evaluate_monitoring_gate"]
