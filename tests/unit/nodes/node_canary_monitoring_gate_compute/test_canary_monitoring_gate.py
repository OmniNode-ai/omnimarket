# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_canary_monitoring_gate_compute (OMN-14735, B10).

Coverage:
    - no threshold spec for a reading -> UNRESOLVED, overall BLOCKED_PENDING_A6
    - unresolved threshold spec (A6 pending, both fields None) -> UNRESOLVED,
      overall BLOCKED_PENDING_A6 (never conflated with PASS)
    - resolved spec, clean reading -> PASS
    - resolved spec, warn-only breach -> WARN
    - resolved spec, abort breach -> ABORT + DEFAULT_ABORT_ACTION
    - "gte" vs "lte" comparison direction
    - RDS abort breach during declared single-AZ maintenance window ->
      downgraded to WARN with the maintenance reason (non-RDS signals are
      NOT downgraded)
    - aggregation priority: ABORT > BLOCKED_PENDING_A6 > WARN > PASS
    - the async handler agrees with the pure function
    - model validation: half-resolved threshold rejected; resolved threshold
      without a real source rejected; frozen/extra-forbid
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_canary_monitoring_gate_compute.handlers.handler_canary_monitoring_gate import (
    HandlerCanaryMonitoringGate,
    evaluate_monitoring_gate,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_monitoring_gate_input import (
    ModelMonitoringGateInput,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_monitoring_gate_result import (
    DEFAULT_ABORT_ACTION,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_reading import (
    ModelSignalReading,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_threshold_spec import (
    UNRESOLVED_SOURCE,
    ModelThresholdSpec,
)

pytestmark = pytest.mark.unit


def _reading(signal_name: str, value: float) -> ModelSignalReading:
    return ModelSignalReading(signal_name=signal_name, value=value, unit="unit")  # type: ignore[arg-type]


def _unresolved_spec(signal_name: str, comparison: str = "gte") -> ModelThresholdSpec:
    return ModelThresholdSpec(signal_name=signal_name, comparison=comparison)  # type: ignore[arg-type]


def _resolved_spec(
    signal_name: str,
    *,
    comparison: str = "gte",
    warn: float,
    abort: float,
    source: str = "A6:test-fixture",
) -> ModelThresholdSpec:
    return ModelThresholdSpec(
        signal_name=signal_name,  # type: ignore[arg-type]
        comparison=comparison,  # type: ignore[arg-type]
        warn_threshold=warn,
        abort_threshold=abort,
        source=source,
    )


# ---------------------------------------------------------------------------
# A6-pending: unresolved thresholds are never conflated with PASS
# ---------------------------------------------------------------------------


class TestUnresolvedIsBlockedNotPass:
    def test_no_spec_supplied_is_unresolved(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(readings=(_reading("auth", 0.01),), thresholds=())
        )
        assert result.overall_status == "BLOCKED_PENDING_A6"
        assert result.verdicts[0].status == "UNRESOLVED"
        assert result.verdicts[0].threshold_source == UNRESOLVED_SOURCE
        assert result.unresolved_signals == ("auth",)
        assert result.abort_action is None

    def test_unresolved_spec_is_unresolved(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("tls", 1.0),),
                thresholds=(_unresolved_spec("tls"),),
            )
        )
        assert result.overall_status == "BLOCKED_PENDING_A6"
        assert result.verdicts[0].status == "UNRESOLVED"
        assert result.verdicts[0].threshold_source == UNRESOLVED_SOURCE

    def test_all_five_signal_domains_default_unresolved(self) -> None:
        readings = tuple(
            _reading(name, 1.0) for name in ("auth", "tls", "broker", "lag", "rds")
        )
        result = evaluate_monitoring_gate(ModelMonitoringGateInput(readings=readings))
        assert result.overall_status == "BLOCKED_PENDING_A6"
        assert set(result.unresolved_signals) == {
            "auth",
            "tls",
            "broker",
            "lag",
            "rds",
        }


# ---------------------------------------------------------------------------
# Resolved thresholds: normal classification
# ---------------------------------------------------------------------------


class TestResolvedClassification:
    def test_clean_reading_passes(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("lag", 1.0),),
                thresholds=(_resolved_spec("lag", warn=5.0, abort=10.0),),
            )
        )
        assert result.overall_status == "PASS"
        assert result.verdicts[0].status == "PASS"
        assert result.abort_action is None

    def test_warn_breach(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("lag", 6.0),),
                thresholds=(_resolved_spec("lag", warn=5.0, abort=10.0),),
            )
        )
        assert result.overall_status == "WARN"
        assert result.verdicts[0].status == "WARN"
        assert result.abort_action is None

    def test_abort_breach(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("lag", 11.0),),
                thresholds=(_resolved_spec("lag", warn=5.0, abort=10.0),),
            )
        )
        assert result.overall_status == "ABORT"
        assert result.verdicts[0].status == "ABORT"
        assert result.abort_action == DEFAULT_ABORT_ACTION

    def test_lte_comparison_direction(self) -> None:
        # broker in-sync-replica count: lower is worse.
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("broker", 1.0),),
                thresholds=(
                    _resolved_spec("broker", comparison="lte", warn=2.0, abort=1.0),
                ),
            )
        )
        assert result.verdicts[0].status == "ABORT"

    def test_lte_comparison_pass(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("broker", 3.0),),
                thresholds=(
                    _resolved_spec("broker", comparison="lte", warn=2.0, abort=1.0),
                ),
            )
        )
        assert result.verdicts[0].status == "PASS"


# ---------------------------------------------------------------------------
# RDS single-AZ maintenance-window exception (A6/B10 acceptance detail)
# ---------------------------------------------------------------------------


class TestRdsMaintenanceWindowException:
    def test_rds_abort_downgraded_during_maintenance_window(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("rds", 100.0),),
                thresholds=(_resolved_spec("rds", warn=50.0, abort=90.0),),
                rds_single_az_maintenance_window=True,
            )
        )
        assert result.verdicts[0].status == "WARN"
        assert "maintenance" in result.verdicts[0].reason
        assert result.overall_status == "WARN"

    def test_rds_abort_not_downgraded_without_flag(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("rds", 100.0),),
                thresholds=(_resolved_spec("rds", warn=50.0, abort=90.0),),
                rds_single_az_maintenance_window=False,
            )
        )
        assert result.verdicts[0].status == "ABORT"

    def test_non_rds_abort_not_downgraded_during_maintenance_window(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("lag", 11.0),),
                thresholds=(_resolved_spec("lag", warn=5.0, abort=10.0),),
                rds_single_az_maintenance_window=True,
            )
        )
        assert result.verdicts[0].status == "ABORT"


# ---------------------------------------------------------------------------
# Aggregation priority across multiple signals
# ---------------------------------------------------------------------------


class TestAggregationPriority:
    def test_abort_outranks_unresolved_and_warn(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(
                    _reading("auth", 1.0),  # unresolved (no spec)
                    _reading("lag", 6.0),  # WARN
                    _reading("broker", 99.0),  # ABORT
                ),
                thresholds=(
                    _resolved_spec("lag", warn=5.0, abort=10.0),
                    _resolved_spec("broker", warn=50.0, abort=90.0),
                ),
            )
        )
        assert result.overall_status == "ABORT"
        assert result.abort_action == DEFAULT_ABORT_ACTION

    def test_blocked_outranks_warn(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(
                    _reading("auth", 1.0),  # unresolved (no spec)
                    _reading("lag", 6.0),  # WARN
                ),
                thresholds=(_resolved_spec("lag", warn=5.0, abort=10.0),),
            )
        )
        assert result.overall_status == "BLOCKED_PENDING_A6"
        assert result.unresolved_signals == ("auth",)

    def test_empty_readings_pass(self) -> None:
        result = evaluate_monitoring_gate(ModelMonitoringGateInput())
        assert result.overall_status == "PASS"
        assert result.verdicts == ()


# ---------------------------------------------------------------------------
# Async handler parity
# ---------------------------------------------------------------------------


class TestAsyncHandler:
    def test_handler_matches_pure_function(self) -> None:
        request = ModelMonitoringGateInput(
            correlation_id=uuid4(),
            readings=(_reading("lag", 11.0),),
            thresholds=(_resolved_spec("lag", warn=5.0, abort=10.0),),
        )
        handler = HandlerCanaryMonitoringGate()
        via_handler = asyncio.run(handler.handle(request))
        via_function = evaluate_monitoring_gate(request)
        assert via_handler == via_function

    def test_handler_classification(self) -> None:
        handler = HandlerCanaryMonitoringGate()
        assert handler.handler_type == "NODE_HANDLER"
        assert handler.handler_category == "COMPUTE"


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestModelValidation:
    def test_half_resolved_threshold_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelThresholdSpec(
                signal_name="lag",
                comparison="gte",
                warn_threshold=5.0,
                abort_threshold=None,
            )

    def test_resolved_threshold_requires_real_source(self) -> None:
        with pytest.raises(ValidationError):
            ModelThresholdSpec(
                signal_name="lag",
                comparison="gte",
                warn_threshold=5.0,
                abort_threshold=10.0,
                source=UNRESOLVED_SOURCE,
            )

    def test_threshold_spec_is_frozen(self) -> None:
        spec = _unresolved_spec("lag")
        with pytest.raises(ValidationError):
            spec.warn_threshold = 5.0  # type: ignore[misc]

    def test_reading_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ModelSignalReading(
                signal_name="lag",  # type: ignore[call-arg]
                value=1.0,
                bogus="x",  # type: ignore[call-arg]
            )

    def test_reading_rejects_unknown_signal_name(self) -> None:
        with pytest.raises(ValidationError):
            ModelSignalReading(signal_name="unknown_signal", value=1.0)  # type: ignore[arg-type]
