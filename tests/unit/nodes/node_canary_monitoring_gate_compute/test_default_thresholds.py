# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the real A6 threshold wiring (OMN-14948, B10->B11).

Proves the load-bearing claim of this ticket: with the actual delivered A6
numbers (not synthetic fixture specs), the gate

    (1) ABORTs when a real signal crosses its real threshold, and
    (2) does NOT abort -- downgrades to WARN -- for the RDS single-AZ
        maintenance-window case, so a routine maintenance window never
        reads as a canary failure mid-soak.

Also proves the config->model wiring itself: all five signal domains from
OMN-14732 are declared, resolved (not BLOCKED_PENDING_A6), and cite the real
A6 source -- never the UNRESOLVED_SOURCE sentinel.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_canary_monitoring_gate_compute.handlers.handler_canary_monitoring_gate import (
    evaluate_monitoring_gate,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.handlers.threshold_config_loader import (
    default_threshold_specs,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_monitoring_gate_input import (
    ModelMonitoringGateInput,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_reading import (
    ModelSignalReading,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_threshold_spec import (
    UNRESOLVED_SOURCE,
)

pytestmark = pytest.mark.unit


def _reading(signal_name: str, value: float) -> ModelSignalReading:
    return ModelSignalReading(signal_name=signal_name, value=value, unit="count")  # type: ignore[arg-type]


class TestDefaultThresholdSpecsWiring:
    def test_all_five_a6_signal_domains_are_declared_and_resolved(self) -> None:
        specs = default_threshold_specs()
        by_name = {spec.signal_name: spec for spec in specs}
        assert set(by_name) == {"auth", "tls", "broker", "lag", "rds"}
        for spec in specs:
            assert spec.is_resolved, f"{spec.signal_name} threshold is not resolved"
            assert spec.source != UNRESOLVED_SOURCE
            assert "OMN-14732" in spec.source

    def test_real_numeric_values_match_the_delivered_a6_artifact(self) -> None:
        by_name = {spec.signal_name: spec for spec in default_threshold_specs()}
        # Values transcribed verbatim from thresholds.json / the A6 memo
        # attached to OMN-14732 (Daniyal, 2026-07-22).
        assert by_name["auth"].abort_threshold == 2
        assert by_name["tls"].abort_threshold == 1
        assert by_name["broker"].abort_threshold == 3
        assert by_name["lag"].abort_threshold == 5
        assert by_name["rds"].abort_threshold == 2

    def test_default_threshold_specs_is_cached_and_stable(self) -> None:
        assert default_threshold_specs() == default_threshold_specs()


class TestRealThresholdsAbortOnBreach:
    """Abort FIRES when a real signal crosses its real A6 threshold."""

    def test_auth_failure_count_aborts_at_threshold(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("auth", 2.0),),
                thresholds=default_threshold_specs(),
            )
        )
        assert result.overall_status == "ABORT"
        assert result.verdicts[0].status == "ABORT"

    def test_auth_failure_count_below_threshold_passes(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("auth", 1.0),),
                thresholds=default_threshold_specs(),
            )
        )
        assert result.overall_status == "PASS"

    def test_tls_cert_error_is_zero_tolerance(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("tls", 1.0),),
                thresholds=default_threshold_specs(),
            )
        )
        assert result.overall_status == "ABORT"

    def test_broker_connection_errors_abort_at_threshold(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("broker", 3.0),),
                thresholds=default_threshold_specs(),
            )
        )
        assert result.overall_status == "ABORT"

    def test_consumer_lag_aborts_at_threshold(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("lag", 5.0),),
                thresholds=default_threshold_specs(),
            )
        )
        assert result.overall_status == "ABORT"

    def test_all_five_real_signals_clean_reading_passes(self) -> None:
        readings = tuple(
            _reading(name, 0.0) for name in ("auth", "tls", "broker", "lag", "rds")
        )
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=readings, thresholds=default_threshold_specs()
            )
        )
        assert result.overall_status == "PASS"


class TestRealRdsMaintenanceWindowDistinction:
    """The single-AZ-maintenance case must NOT read as a canary failure."""

    def test_rds_breach_without_maintenance_window_aborts(self) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("rds", 2.0),),
                thresholds=default_threshold_specs(),
                rds_single_az_maintenance_window=False,
            )
        )
        assert result.overall_status == "ABORT"
        assert result.verdicts[0].status == "ABORT"

    def test_rds_breach_during_declared_maintenance_window_does_not_abort(
        self,
    ) -> None:
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("rds", 2.0),),
                thresholds=default_threshold_specs(),
                rds_single_az_maintenance_window=True,
            )
        )
        assert result.overall_status != "ABORT"
        assert result.overall_status == "WARN"
        assert result.verdicts[0].status == "WARN"
        assert "maintenance" in result.verdicts[0].reason

    def test_maintenance_flag_never_suppresses_a_non_rds_abort(self) -> None:
        """rds_single_az_maintenance_window must only ever affect the RDS signal."""
        result = evaluate_monitoring_gate(
            ModelMonitoringGateInput(
                readings=(_reading("broker", 3.0),),
                thresholds=default_threshold_specs(),
                rds_single_az_maintenance_window=True,
            )
        )
        assert result.overall_status == "ABORT"

    def test_rds_below_threshold_passes_regardless_of_maintenance_state(
        self,
    ) -> None:
        for maintenance in (True, False):
            result = evaluate_monitoring_gate(
                ModelMonitoringGateInput(
                    readings=(_reading("rds", 1.0),),
                    thresholds=default_threshold_specs(),
                    rds_single_az_maintenance_window=maintenance,
                )
            )
            assert result.overall_status == "PASS"
